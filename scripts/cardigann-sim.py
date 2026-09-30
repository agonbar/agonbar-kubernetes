#!/usr/bin/env nix-shell
#!nix-shell -i python3 -p "python3.withPackages (ps: [ ps.pyyaml ps.beautifulsoup4 ])"
"""Run a Cardigann definition's search against the live site, the way Prowlarr 2.6.x would.

Covers the subset of Cardigann the custom definitions here use: HTML responses, path templates
with {{ if .Keywords }}, keywordsfilters, row selectors, text/selector/attribute fields,
the regexp/re_replace/diacritics/replace/trim filters, the andmatch row filter and
download selectors. Anything else raises, so a definition that outgrows it fails loudly.

Usage:
  cardigann-sim.py <definition.yml | manifest with a ConfigMap> [--grab N] [query ...]
  cardigann-sim.py deployments/piracy/prowlarr.yml "" "cronica de una muerte anunciada"
A manifest is searched for the first ConfigMap key that is a Cardigann definition.
An empty query ("") is the keywordless test search Prowlarr runs when you add the indexer.
"""
import argparse
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request

import yaml
from bs4 import BeautifulSoup

UA = "Prowlarr/2.6.5.5623 (alpine 3.22)"
COMMON_WORDS = {"and", "the", "an", "of"}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    opener = urllib.request.build_opener(NoRedirect)
    with opener.open(req, timeout=40) as r:
        return r.status, r.read().decode("utf-8", errors="replace")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # Prowlarr does not follow redirects on search requests unless followredirect is set.
    def redirect_request(self, *a, **k):
        return None


def url_encode(s):
    # System.Net.WebUtility.UrlEncode keeps A-Za-z0-9 and -_.!*() and turns spaces into +.
    return urllib.parse.quote_plus(s, safe="-_.!*()")


def go_template(tpl, v, encode=None):
    enc = encode or (lambda x: x)

    def if_block(m):
        return m.group(2) if v.get(m.group(1)) else (m.group(3) or "")

    tpl = re.sub(r"\{\{ if (\.[\w.]+) \}\}(.*?)(?:\{\{ else \}\}(.*?))?\{\{ end \}\}", if_block, tpl)
    out = re.sub(r"\{\{ (\.[\w.]+) \}\}", lambda m: enc(str(v.get(m.group(1)) or "")), tpl)
    if "{{" in out:
        raise NotImplementedError(f"template not supported: {tpl}")
    return out


def apply_filters(data, filters, v):
    for f in filters or []:
        name, args = f["name"], f.get("args")
        if name == "regexp":
            m = re.search(args, data)
            data = m.group(1) if m else ""
        elif name == "re_replace":
            data = re.sub(args[0], go_template(args[1], v), data)
        elif name == "replace":
            data = data.replace(args[0], args[1])
        elif name == "trim":
            data = data.strip(args) if args else data.strip()
        elif name == "diacritics" and args == "replace":
            data = "".join(c for c in unicodedata.normalize("NFD", data) if unicodedata.category(c) != "Mn")
        else:
            raise NotImplementedError(f"filter not supported: {name}")
    return data


def handle_selector(block, el, v):
    if "text" in block:
        return apply_filters(go_template(str(block["text"]), v), block.get("filters"), v)
    sel = el
    if block.get("selector"):
        sel = el if el.css.match(block["selector"]) else el.select_one(block["selector"])
        if sel is None:
            raise LookupError(f"selector {block['selector']!r} matched nothing")
    if block.get("attribute"):
        value = sel.get(block["attribute"])
        if value is None:
            raise LookupError(f"attribute {block['attribute']!r} missing")
    else:
        value = sel.get_text()
    return apply_filters(value.strip(), block.get("filters"), v)


def andmatch(release, term):
    # IndexerBase.FilterReleasesByQuery: at least two of the query words (one if there is only
    # one) must appear, case-insensitively and accent-sensitively, in the title or description.
    terms = [t for t in re.split(r"[^\w]+", term) if len(t) > 1 and t.lower() not in COMMON_WORDS]
    hay = [(release.get("title") or "").lower(), (release.get("description") or "").lower()]
    hits = [t for t in terms if any(t.lower() in h for h in hay)]
    return len(hits) >= 2 if len(terms) > 1 else len(hits) >= 1


def search(d, base, query):
    s = d["search"]
    keywords = apply_filters(query, s.get("keywordsfilters"), {})
    v = {".Keywords": keywords, ".Query.Keywords": query}
    releases, seen = [], []
    for p in s["paths"]:
        url = urllib.parse.urljoin(base, go_template(p["path"], v, url_encode).replace("+", "%20"))
        if url in seen:
            continue
        seen.append(url)
        status, html = fetch(url)
        rows = BeautifulSoup(html, "html.parser").select(s["rows"]["selector"])
        print(f"  GET {url} -> {status}, {len(rows)} rows", file=sys.stderr)
        for row in rows:
            rv = dict(v)
            rel = {}
            for name, block in s["fields"].items():
                value = handle_selector(block, row, rv)
                rv[f".Result.{name}"] = value
                rel[name] = value
            # Prowlarr resolves relative links against the page they came from, not the site root.
            rel["_page"] = url
            releases.append(rel)
        time.sleep(d.get("requestDelay", 0))
    has_andmatch = any(f["name"] == "andmatch" for f in s["rows"].get("filters") or [])
    kept = [r for r in releases if not (has_andmatch and query.strip()) or andmatch(r, query)]
    return releases, kept


def grab(d, base, link):
    url = urllib.parse.urljoin(base, link)
    status, html = fetch(url)
    doc = BeautifulSoup(html, "html.parser")
    for sel in d["download"]["selectors"]:
        el = doc.select_one(sel["selector"])
        if el is not None and el.get(sel["attribute"]):
            return status, el.get(sel["attribute"])
    return status, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("definition")
    ap.add_argument("--grab", type=int, default=3, help="rows per query to resolve to a magnet")
    ap.add_argument("--show", type=int, default=6)
    ap.add_argument("queries", nargs="*", default=[""])
    a = ap.parse_args()
    docs = [x for x in yaml.safe_load_all(open(a.definition, encoding="utf-8")) if x]
    d = next((yaml.safe_load(v) for x in docs if x.get("kind") == "ConfigMap"
              for v in x.get("data", {}).values() if "search:" in v), docs[0])
    base = d["links"][0]
    for q in a.queries:
        print(f"\n=== query {q!r}")
        raw, kept = search(d, base, q)
        print(f"  {len(raw)} rows parsed, {len(kept)} after andmatch")
        for i, r in enumerate(kept[: a.show]):
            line = f"  - {r['title']} | {r['details']} | size {r['size']} | date {r['date']}"
            if i < a.grab:
                status, magnet = grab(d, r["_page"], r["download"])
                m = re.search(r"btih:([0-9A-Fa-f]{40})", magnet or "")
                line += f" | magnet {'yes ' + m.group(1)[:12] + '…' if m else 'NO (HTTP ' + str(status) + ')'}"
                time.sleep(d.get("requestDelay", 0))
            print(line)


if __name__ == "__main__":
    main()
