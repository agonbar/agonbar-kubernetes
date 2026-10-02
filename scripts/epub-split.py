#!/usr/bin/env python3
"""Split an omnibus EPUB into one EPUB per book, keeping the same edition and translation.

Some sagas are only on offer as a single volume (EpubLibre has El Señor de los Anillos only as
one file). Each part is a range of the omnibus spine, from the document where the book starts up
to the one where the next starts, plus documents every part should carry (cover and title page
in front, notes behind, so footnote links still resolve). Images, CSS and fonts are copied as
they are; spine documents outside the part are dropped, and links into them (a shared notes file
points back at every volume's chapters) lose their href so the part still validates. The OPF is
edited as text, not re-serialized, so its namespace prefixes (opf:role, opf:file-as) survive: it
gets the part's title, its series and number (calibre:series, which BookOrbit reads) and a fresh
identifier. The NCX keeps the entries that point into the part.

Standard library only, so it runs on nas02 next to the files:

  ssh nas02 python3 - OMNIBUS.epub OUTDIR SPEC.json < scripts/epub-split.py

SPEC.json:
  {"series": "Tierra Media", "front": ["Text/cubierta.xhtml"], "back": ["Text/notas.xhtml"],
   "parts": [{"title": "La Comunidad del Anillo", "index": 1,
              "start": "Text/cubierta.xhtml", "end": "Text/volumen2.xhtml"}, ...]}
Paths are relative to the OPF. "end" is exclusive; omit it to run to the end of the spine. A part
may name its own "cover" image (an omnibus often carries one per book); the OPF cover meta then
points at it instead of the omnibus cover.
Writes OUTDIR/<NN>. <title>/<title>.epub.
"""
import copy
import json
import os
import posixpath
import re
import sys
import uuid
import xml.etree.ElementTree as ET
import zipfile

OPF = "http://www.idpf.org/2007/opf"
DC = "http://purl.org/dc/elements/1.1/"
NCX = "http://www.daisy.org/z3986/2005/ncx/"
ET.register_namespace("", NCX)


def split(src, outdir, spec):
    z = zipfile.ZipFile(src)
    container = ET.fromstring(z.read("META-INF/container.xml"))
    opf_path = container.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile").get("full-path")
    base = posixpath.dirname(opf_path)
    full = lambda href: posixpath.normpath(posixpath.join(base, href))
    opf_text = z.read(opf_path).decode("utf-8")
    opf = ET.fromstring(opf_text.encode("utf-8"))
    manifest = {i.get("id"): i for i in opf.iter(f"{{{OPF}}}item")}
    spine = [r.get("idref") for r in opf.iter(f"{{{OPF}}}itemref")]
    href_of = {i: manifest[i].get("href") for i in spine}
    pos = {href: n for n, href in enumerate(href_of[i] for i in spine)}
    ncx_id = opf.find(f"{{{OPF}}}spine").get("toc")
    ncx_path = full(manifest[ncx_id].get("href")) if ncx_id else None

    for part in spec["parts"]:
        start, end = pos[part["start"]], pos[part["end"]] if part.get("end") else len(spine)
        body = [href_of[i] for i in spine[start:end]]
        hrefs = [h for h in spec.get("front", []) if h not in body] + body + [h for h in spec.get("back", []) if h not in body]
        keep_ids = [next(i for i in spine if href_of[i] == h) for h in hrefs]
        drop = {full(href_of[i]) for i in spine if i not in keep_ids}

        uid = f"urn:uuid:{uuid.uuid4()}"  # the NCX's dtb:uid must match the OPF identifier
        new = edit_opf(opf_text, part, spec["series"], keep_ids, lambda h: full(h) in drop, uid)
        if part.get("cover"):
            cover_id = next(i for i, item in manifest.items() if item.get("href") == part["cover"])
            new = re.sub(r'(<meta\b[^>]*name="cover"[^>]*content=")[^"]*(")', lambda m: m.group(1) + cover_id + m.group(2), new)

        name = f"{part['index']:02d}. {part['title']}"
        os.makedirs(os.path.join(outdir, name), exist_ok=True)
        out = os.path.join(outdir, name, f"{part['title']}.epub")
        with zipfile.ZipFile(out, "w") as w:
            w.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            for info in z.infolist():
                if info.filename in ("mimetype",) or info.filename in drop:
                    continue
                data = z.read(info.filename)
                if info.filename == opf_path:
                    data = new.encode("utf-8")
                elif info.filename == ncx_path:
                    data = trim_ncx(data, {full(h) for h in hrefs}, posixpath.dirname(ncx_path), part["title"], uid)
                elif info.filename.endswith((".xhtml", ".html")):
                    data = unlink(data, posixpath.dirname(info.filename), drop)
                # writestr rewrites the ZipInfo's CRC and sizes in place; the source archive still
                # needs its own for the next part, so write through a copy.
                w.writestr(copy.copy(info), data, compress_type=zipfile.ZIP_DEFLATED)
        print(f"{out}  ({len(keep_ids)} documents)")


def edit_opf(text, part, series, keep_ids, dropped, uid):
    attr = lambda tag, name: (re.search(rf'\b{name}="([^"]*)"', tag) or [None, None])[1]
    text = re.sub(r"(<dc:title\b[^>]*>)[^<]*(</dc:title>)", lambda m: m.group(1) + part["title"] + m.group(2), text, count=1)
    text = re.sub(r"(<dc:identifier\b[^>]*>)urn:uuid:[^<]*(</dc:identifier>)",
                  lambda m: f"{m.group(1)}{uid}{m.group(2)}", text, count=1)
    text = re.sub(r'\s*<meta\b[^>]*name="calibre:series(_index)?"[^>]*/>', "", text)
    text = text.replace("</metadata>", f'  <meta name="calibre:series" content="{series}"/>\n'
                        f'    <meta name="calibre:series_index" content="{part["index"]}"/>\n  </metadata>', 1)
    text = re.sub(r"\s*<item\b[^>]*/>", lambda m: "" if dropped(attr(m.group(0), "href")) else m.group(0), text)
    text = re.sub(r"\s*<reference\b[^>]*/>", lambda m: "" if dropped(attr(m.group(0), "href").split("#")[0]) else m.group(0), text)
    text = re.sub(r"\s*<guide>\s*</guide>", "", text)  # a guide must keep at least one reference
    refs = "".join(f'\n    <itemref idref="{i}"/>' for i in keep_ids)
    return re.sub(r"(<spine\b[^>]*>).*?(\s*</spine>)", lambda m: m.group(1) + refs + m.group(2), text, count=1, flags=re.S)


def unlink(data, doc_dir, drop):
    """Drop the href of links into documents this part doesn't carry; the link text stays."""
    def fix(m):
        target = posixpath.normpath(posixpath.join(doc_dir, m.group(1).decode()))
        return b"" if target in drop else m.group(0)
    return re.sub(rb'\shref="([^"#:]+)(#[^"]*)?"', fix, data)


def trim_ncx(data, kept, ncx_dir, title, uid):
    root = ET.fromstring(data)
    for m in root.iter(f"{{{NCX}}}meta"):
        if m.get("name") == "dtb:uid":
            m.set("content", uid)
    doc_title = root.find(f"{{{NCX}}}docTitle/{{{NCX}}}text")
    if doc_title is not None:
        doc_title.text = title

    def prune(parent):
        for np in list(parent.findall(f"{{{NCX}}}navPoint")):
            src = np.find(f"{{{NCX}}}content").get("src").split("#")[0]
            prune(np)
            if posixpath.normpath(posixpath.join(ncx_dir, src)) not in kept and not np.findall(f"{{{NCX}}}navPoint"):
                parent.remove(np)

    nav_map = root.find(f"{{{NCX}}}navMap")
    prune(nav_map)
    for n, np in enumerate(nav_map.iter(f"{{{NCX}}}navPoint"), 1):
        np.set("playOrder", str(n))
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


if __name__ == "__main__":
    split(sys.argv[1], sys.argv[2], json.load(open(sys.argv[3], encoding="utf-8")))
