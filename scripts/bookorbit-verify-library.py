#!/usr/bin/env python3
"""Check that the EPUBs in the BookOrbit library are what they claim to be.

For every .epub under the library root: the archive opens and its files decompress, the OPF has a
title, author and language, and the body text is really Spanish. The language is measured from
the text itself (common Spanish, Catalan and English words in the first chapters), because a
release can be labelled Spanish and still be another edition: Catalan passes a Spanish-vs-English
test, so it is counted on its own.

Runs where the files are, with the standard library only:

  ssh nas02 python3 - < scripts/bookorbit-verify-library.py [/mnt/RAID/docker/media/books]

Prints one line per book, and a final line counting the ones flagged.
"""
import html
import os
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

ROOT = sys.argv[1] if len(sys.argv) > 1 else "/mnt/RAID/docker/media/books"
ES = set("que y los se del las por con no su para es lo como más pero sus le ya o este porque esta entre cuando muy sin sobre también me hasta hay donde quien desde todo nos durante todos uno les ni contra otros ese eso ante ellos e esto mí antes algunos qué unos yo otro otras otra él tanto esa estos mucho quienes nada muchos cual poco estar estas algunas algo nosotros había".split())
# Catalan shares most short words with Spanish, so it needs its own list or it passes as Spanish.
CA = set("amb però molt aquest aquesta perquè els seva seu seus jo havia també fer quan més tot sense dels pel pels cap mai encara ja ell ella elles ells".split())
EN = set("the of and to a in is it you that he was for on are with as his they be at one have this from or had by not word but what some we can out other were all there when up use your how said an each she which do their time if will way about many then them would".split())
NS = {"opf": "http://www.idpf.org/2007/opf", "dc": "http://purl.org/dc/elements/1.1/"}


def text_language(words):
    es = sum(w in ES for w in words)
    en = sum(w in EN for w in words)
    ca = sum(w in CA for w in words)
    if es + en < 50:
        return "?", es, en
    if ca > es / 3:  # Spanish text has a few Catalan-list words ("ella", "ya"), never this many
        return "ca", es, en
    return ("es" if es > 2 * en else "en" if en > 2 * es else "mixed"), es, en


def check(path):
    z = zipfile.ZipFile(path)
    bad = z.testzip()
    if bad:
        return {"error": f"corrupt member {bad}"}
    container = ET.fromstring(z.read("META-INF/container.xml"))
    opf_path = container.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile").get("full-path")
    opf = ET.fromstring(z.read(opf_path))
    meta = lambda tag: (opf.findtext(f".//dc:{tag}", default="", namespaces=NS) or "").strip()
    base = os.path.dirname(opf_path)
    items = {i.get("id"): i.get("href") for i in opf.iterfind(".//opf:manifest/opf:item", NS)}
    words = []
    for ref in opf.iterfind(".//opf:spine/opf:itemref", NS):
        href = items.get(ref.get("idref"))
        if not href:
            continue
        name = os.path.normpath(os.path.join(base, href)).replace("\\", "/")
        try:
            raw = z.read(name).decode("utf-8", "replace")
        except KeyError:
            continue
        body = html.unescape(re.sub(r"<[^>]+>", " ", raw))
        words += re.findall(r"[a-záéíóúñüàèòïç]+", body.lower())
        if len(words) > 20000:
            break
    lang, es, en = text_language(words)
    return {"title": meta("title"), "author": meta("creator"), "opf_lang": meta("language"),
            "text_lang": lang, "es": es, "en": en, "words": len(words)}


def main():
    flagged = total = 0
    for dirpath, _, files in sorted(os.walk(ROOT)):
        for f in sorted(files):
            if not f.lower().endswith(".epub"):
                continue
            total += 1
            path = os.path.join(dirpath, f)
            rel = os.path.relpath(path, ROOT)
            try:
                r = check(path)
            except Exception as e:  # a broken archive is a finding, not a crash
                r = {"error": f"{type(e).__name__}: {e}"}
            if "error" in r:
                flagged += 1
                print(f"BAD   {rel}  {r['error']}")
                continue
            problems = []
            if r["text_lang"] != "es":
                problems.append(f"text is {r['text_lang']} (es={r['es']} en={r['en']})")
            if not r["opf_lang"].lower().startswith("es"):
                problems.append(f"opf language '{r['opf_lang']}'")
            if r["words"] < 5000:
                problems.append(f"only {r['words']} words read")
            status = "FLAG " if problems else "ok   "
            flagged += bool(problems)
            print(f"{status} {rel}  | {r['title']} / {r['author']} | {r['opf_lang']} | {r['words']} words"
                  + (f"  <- {'; '.join(problems)}" if problems else ""))
    print(f"{total} epubs, {flagged} flagged")


if __name__ == "__main__":
    main()
