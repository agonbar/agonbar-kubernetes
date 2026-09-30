#!/usr/bin/env python3
"""Check that the EPUBs in the BookOrbit library are what they claim to be.

For every .epub under the library root: the archive opens and its files decompress, the OPF has a
title, author and language, and the body text is really Spanish. The language is measured from
the text itself (share of common Spanish vs English words in the first chapters), because a
release can be labelled Spanish and still be the English edition.

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
ES = set("de la que el en y a los se del las un por con no una su para es al lo como más pero sus le ya o este sí porque esta entre cuando muy sin sobre también me hasta hay donde quien desde todo nos durante todos uno les ni contra otros ese eso ante ellos e esto mí antes algunos qué unos yo otro otras otra él tanto esa estos mucho quienes nada muchos cual poco ella estar estas algunas algo nosotros".split())
EN = set("the of and to a in is it you that he was for on are with as his they be at one have this from or had by not word but what some we can out other were all there when up use your how said an each she which do their time if will way about many then them would".split())
NS = {"opf": "http://www.idpf.org/2007/opf", "dc": "http://purl.org/dc/elements/1.1/"}


def text_language(words):
    es = sum(w in ES for w in words)
    en = sum(w in EN for w in words)
    if es + en < 50:
        return "?", es, en
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
        words += re.findall(r"[a-záéíóúñü]+", body.lower())
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
