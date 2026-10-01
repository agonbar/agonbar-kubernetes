#!/usr/bin/env python3
"""Give every saga in the BookOrbit library one series name, taken from the reading list.

BookOrbit names a book's series after whatever its metadata provider says, so one saga ends up
split across "Dune" and "Dune Chronicles", or "The Witcher" and three spellings of "Geralt de
Rivia". This matches each library book to a list row by title and author surname, writes the
row's series name (and its number, when the row has one), and locks both fields so a metadata
refresh doesn't undo it. Books that match no row, or a row without a series, are left alone.

  BOOKORBIT_ADMIN_PASSWORD=... scripts/bookorbit-normalize-series.py list.tsv [extra.tsv ...] [--apply]

Lists are tab-separated: title, author, series, index (same format as bookorbit-bulk-request.py).
An index of "-" clears the number, for omnibus volumes that would otherwise share one with a single
book. A later list wins over an earlier one for the same title. Without --apply it only prints the plan.
"""
import argparse
import importlib.util
import os
import re
import unicodedata

spec = importlib.util.spec_from_file_location("bulk", os.path.join(os.path.dirname(__file__), "bookorbit-bulk-request.py"))
bulk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bulk)


def key(s):
    s = "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("lists", nargs="+")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    rows = {}
    for path in a.lists:
        for line in open(path, encoding="utf-8"):
            if not line.strip() or line.startswith("#"):
                continue
            title, author, series, index = (line.rstrip("\n").split("\t") + ["", "", ""])[:4]
            rows[key(title)] = (author.strip(), series.strip(), index.strip())

    api = bulk.Session()
    books, page = [], 0
    while True:
        _, r = api("POST", "/books/query", {"pagination": {"page": page, "size": 200}})
        books += r["items"]
        if len(books) >= r["total"] or not r["items"]:
            break
        page += 1

    changes = 0
    for b in sorted(books, key=lambda b: (b.get("seriesName") or "", b["title"])):
        row = rows.get(key(b["title"]))
        if not row or not row[1]:
            continue
        author, series, index = row
        if key(author.split()[-1]) not in key(" ".join(b.get("authors") or [])):
            continue
        new_index = None if index == "-" else index or b.get("seriesIndex")
        if b.get("seriesName") == series and (b.get("seriesIndex") or None) == (new_index or None):
            continue
        changes += 1
        print(f"#{b['id']} {b['title']}: {b.get('seriesName')} #{b.get('seriesIndex')} -> {series} #{new_index}")
        if a.apply:
            locks = sorted(set(b.get("lockedFields") or []) | {"seriesName", "seriesIndex"})
            meta = {"seriesName": series, "seriesIndex": str(new_index) if new_index else None}
            status, resp = api("PATCH", f"/books/{b['id']}/metadata-and-locks", {"metadata": meta, "lockedFields": locks})
            if not 200 <= status < 300:
                print(f"  FAILED {status}: {(resp or {}).get('message')}")
    print(f"{len(books)} books, {changes} to change" + ("" if a.apply else " (dry run, pass --apply)"))


if __name__ == "__main__":
    main()
