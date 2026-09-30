#!/usr/bin/env python3
"""File a reading list as BookOrbit ebook requests, one at a time, and grab EpubLibre editions.

Each request is created with its author, so the metadata and the import check have it. BookOrbit's
own search then sends "title author" to every indexer, and that is the query EpubLibre gets wrong:
its site search matches title words only, so the author's name pulls in unrelated titles, the
result is not empty, and BookOrbit never falls back to the title alone. So when the automation has
not grabbed anything, this searches again without the author and grabs the best EpubLibre result
whose title carries the author's surname.

EpubLibre answers a burst with its overload page, and Prowlarr then pauses it, so requests go in
with a pause between them and a paused EpubLibre is waited out rather than skipped. Titles already
sent are logged next to the list (<list>.sent, title and request id) and skipped on a rerun;
--retry goes back over the sent ones that still have nothing grabbed.

  BOOKORBIT_ADMIN_PASSWORD=... scripts/bookorbit-bulk-request.py list.tsv [--delay 120] [--retry] [--dry-run]

The list is tab-separated: title, author, series, index. Blank lines and # comments are skipped.
"""
import argparse
import json
import os
import sys
import time
import unicodedata
import urllib.error
import urllib.request

BASE = os.environ.get("BOOKORBIT_URL", "https://bookorbit.adriangonzalezbarbosa.eu") + "/api/v1"
EPUBLIBRE = "EpubLibre"
DONE = {"grabbed", "downloading", "importing", "available"}


def call(method, path, body=None, token=None):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def fold(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))


class Session:
    def __init__(self):
        self.token, self.at = None, 0.0

    def __call__(self, method, path, body=None):
        if time.time() - self.at > 600:  # access tokens live 15 minutes
            user = os.environ.get("BOOKORBIT_ADMIN_USER", "agonbar")
            status, resp = call("POST", "/auth/login", {"username": user, "password": os.environ["BOOKORBIT_ADMIN_PASSWORD"]})
            if status != 200:
                sys.exit(f"login failed: {status} {resp}")
            self.token, self.at = resp["accessToken"], time.time()
        return call(method, path, body, self.token)


def grab_epublibre(api, rid, title, author):
    """Search without the author and grab EpubLibre's best match by this author. Returns a note."""
    status, resp = api("POST", f"/admin/book-requests/{rid}/releases/search", {"authors": []})
    if status != 200:
        return f"search {status}"
    src = next((i for i in resp.get("indexers", []) if i.get("indexerName") == EPUBLIBRE), {})
    if not src.get("ok", True):
        return f"EpubLibre failed: {src.get('error')}"
    surname = fold(author.split()[-1]) if author else ""
    picks = [r for r in resp.get("releases", [])
             if r.get("indexerName") == EPUBLIBRE and r.get("tier") == 0
             and fold(title) in fold(r["title"]) and surname in fold(r["title"])]
    if not picks:
        return "no EpubLibre match by this author"
    best = max(picks, key=lambda r: r.get("score") or 0)
    for attempt in (1, 2):  # resolving the magnet fetches EpubLibre's details page, which can time out
        status, resp = api("POST", f"/admin/book-requests/{rid}/grab",
                           {"indexerId": best["indexerId"], "releaseGuid": best["guid"]})
        if 200 <= status < 300:
            return f"grabbed {best['title']} ({best.get('score')})"
        if attempt == 1:
            time.sleep(45)
    return f"grab {status}: {(resp or {}).get('message')}"


def settle(api, rid, title, author, tries=6, wait=600):
    """Leave it to the automation if it grabbed something, otherwise grab from EpubLibre."""
    _, req = api("GET", f"/book-requests/{rid}")
    if req.get("status") in DONE:
        return f"automation: {req.get('status')}"
    for attempt in range(tries):
        note = grab_epublibre(api, rid, title, author)
        if not note.startswith("EpubLibre failed") or attempt == tries - 1:
            return note
        time.sleep(wait)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("list")
    ap.add_argument("--delay", type=int, default=120, help="seconds between requests")
    ap.add_argument("--language", default="es")
    ap.add_argument("--retry", action="store_true", help="only revisit sent requests with nothing grabbed")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    rows = []
    for line in open(a.list, encoding="utf-8"):
        if not line.strip() or line.startswith("#"):
            continue
        title, author, series, index = (line.rstrip("\n").split("\t") + ["", "", ""])[:4]
        rows.append((title.strip(), author.strip(), series.strip(), index.strip()))

    sent_path = a.list + ".sent"
    sent = dict(l.split("\t") for l in open(sent_path, encoding="utf-8").read().splitlines()) if os.path.exists(sent_path) else {}
    api = Session()
    if a.retry:
        for title, author, _, _ in rows:
            if title in sent:
                print(f"#{sent[title]} {title} - {settle(api, sent[title], title, author)}", flush=True)
        return
    todo = [r for r in rows if r[0] not in sent]
    print(f"{len(rows)} titles, {len(rows) - len(todo)} already sent, {len(todo)} to go", flush=True)

    for n, (title, author, series, index) in enumerate(todo, 1):
        body = {"title": title, "mediaKind": "ebook", "language": a.language}
        if author:
            body["authors"] = [author]
        if series:
            body["seriesName"] = series
        if index:
            body["seriesIndex"] = int(index)
        if a.dry_run:
            print(json.dumps(body, ensure_ascii=False))
            continue
        status, resp = api("POST", "/book-requests", body)
        if not 200 <= status < 300:
            print(f"[{n}/{len(todo)}] create {status} {(resp or {}).get('message')} - {title}", flush=True)
            continue
        rid = (resp.get("request") or resp)["id"]
        with open(sent_path, "a", encoding="utf-8") as f:
            f.write(f"{title}\t{rid}\n")
        time.sleep(30)  # let the automation's own search and grab run first
        print(f"[{n}/{len(todo)}] #{rid} {title} - {settle(api, rid, title, author)}", flush=True)
        if n < len(todo):
            time.sleep(a.delay)


if __name__ == "__main__":
    main()
