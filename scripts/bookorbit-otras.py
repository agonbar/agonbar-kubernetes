#!/usr/bin/env python3
"""Las "Otras bibliotecas" de ivan-2 (nas03) a BookOrbit, sin duplicar lo que ya está y con el
estilo de la 75.452 (ver bookorbit-calibre.py).

  ./bookorbit-otras.py inventory   candidatos de cada fuente a ~/.cache/bookorbit-otras/candidates.json
  ./bookorbit-otras.py dedupe      exacto y aproximado contra BookOrbit y entre fuentes;
                                   deja dedupe.json con new / dup / check (dudosos para Strata)

Fuentes, de mejores a peores metadatos (entre fuentes se queda la primera que tenga el libro):
  epubgratis, calibre (bases de Calibre), lote2016 (un .opf por libro), librosvarios
  ("Autor/Autor (año). (Serie NN) Título [id].mobi"), forococheros y biblioteca (Calibre con
  metadatos malos: sin idioma, títulos "Microsoft Word - x.doc", autor y título cruzados).

Fuera a propósito: EBOOKS/Biblioteca (copia idéntica de Biblioteca), 7000 ebooks español (está
dentro de Biblioteca) y las carpetas sin orden (Megapack, biblioteca digital...), que cuentan
capítulos, poemas y recetas como libros.
"""
import argparse, collections, html, importlib.util, json, os, re, sqlite3, subprocess, sys, unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("bc", os.path.join(HERE, "bookorbit-calibre.py"))
bc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bc)
log, norm, sh = bc.log, bc.norm, bc.sh

OTRAS = "/mnt/RAID/ivan/ivan-2/Libros EPUB/Otras bibliotecas"   # nas03
STATE = os.path.expanduser("~/.cache/bookorbit-otras")
DBS = [("epubgratis", "Biblioteca Epubgratis 5.0"), ("calibre", "Calibre"),
       ("forococheros", "ebooks forococheros/Calibre Library"), ("biblioteca", "Biblioteca")]
PRIORITY = ["epubgratis", "calibre", "lote2016", "librosvarios", "forococheros", "biblioteca"]
FORMATS = ["epub", "azw3", "mobi", "fb2", "pdf", "prc", "doc", "docx", "rtf"]   # preferencia al importar
ARTICLES = r"^(el|la|los|las|un|una|lo|the|a|an|le|les|l)\s+"


def ssh(cmd, inp=None):
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "nas03", cmd], input=inp, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"nas03: {r.stderr.strip()[-300:]}")
    return r.stdout


def from_db(name, folder):
    """Libros de una base de Calibre copiada a STATE/<name>.db."""
    db = sqlite3.connect(f"file:{STATE}/{name}.db?immutable=1", uri=True)
    q = db.execute
    au, tg, ln = {}, {}, {}
    for b, a in q("select l.book, a.name from books_authors_link l join authors a on a.id=l.author order by l.id"):
        au.setdefault(b, []).append(a)
    for b, t in q("select l.book, t.name from books_tags_link l join tags t on t.id=l.tag order by l.id"):
        tg.setdefault(b, []).append(t)
    for b, l in q("select l.book, g.lang_code from books_languages_link l join languages g on g.id=l.lang_code"):
        ln.setdefault(b, l)
    series = dict(q("select l.book, s.name from books_series_link l join series s on s.id=l.series"))
    files = {}
    for b, fmt, fname, size in q("select book, format, name, uncompressed_size from data"):
        files.setdefault(b, []).append((fmt.lower(), fname, size))
    out = []
    for b, title, idx, pubdate, path in q("select id, title, series_index, pubdate, path from books"):
        fs = [{"rel": f"{folder}/{path}/{fn}.{fmt}", "fmt": fmt, "size": size} for fmt, fn, size in files.get(b, [])]
        if not fs:
            continue  # Calibre tiene 2.126 libros sin fichero
        year = int(pubdate[:4]) if pubdate and pubdate[:4].isdigit() and 1000 <= int(pubdate[:4]) <= 2100 else None
        out.append({"src": name, "sid": b, "title": title, "authors": au.get(b, []), "series": series.get(b),
                    "index": idx if series.get(b) else None, "tags": tg.get(b, []), "lang": ln.get(b), "year": year, "files": fs})
    return out


def from_lote(rows):
    """LOTE 2016: "Apellido, Nombre/título, El/título, El - Nombre Apellido.{epub,mobi,azw3,fb2,opf}"."""
    books = collections.defaultdict(list)
    for size, rel in rows:
        if rel.startswith("LOTE 2016 TBPLUS EBOOKS/"):
            books[os.path.dirname(rel)].append((rel, size))
    opfs = {}
    paths = [r for v in books.values() for r, _ in v if r.endswith(".opf")]
    out = ssh(f"cd {sh(OTRAS)} && xargs -0 -I{{}} sh -c 'printf \"\\036%s\\037\" \"{{}}\"; cat \"{{}}\"'", "\0".join(paths))
    for chunk in out.split("\x1e")[1:]:
        p, body = chunk.split("\x1f", 1)
        opfs[p] = body
    res = []
    for d, fs in books.items():
        opf = next((opfs.get(r) for r, _ in fs if r.endswith(".opf")), "") or ""
        g = lambda tag: [html.unescape(x).strip() for x in re.findall(rf"<dc:{tag}[^>]*>([^<]+)", opf)]
        meta = lambda n: (re.search(rf'name="calibre:{n}" content="([^"]*)"', opf) or re.search(rf'content="([^"]*)" name="calibre:{n}"', opf))
        title = (g("title") or [os.path.basename(d)])[0]
        date = (g("date") or [""])[0]
        s, i = meta("series"), meta("series_index")
        res.append({"src": "lote2016", "sid": d, "title": title, "authors": g("creator"), "series": html.unescape(s.group(1)) if s else None,
                    "index": float(i.group(1)) if s and i else None, "tags": g("subject"), "lang": (g("language") or [None])[0],
                    "year": int(date[:4]) if date[:4].isdigit() and 1000 <= int(date[:4]) <= 2100 else None,
                    "files": [{"rel": r, "fmt": r.rsplit(".", 1)[-1].lower(), "size": sz} for r, sz in fs if re.search(r"\.(epub|mobi|azw3|fb2)$", r)]})
    return res


def from_libros_varios(rows):
    """"Leon, Donna/Leon, Donna (2010). (Brunetti 19) Cuestión de fe [6550].mobi"."""
    res = []
    for size, rel in rows:
        if not rel.startswith("Libros varios/") or not re.search(r"\.(mobi|epub|azw3|fb2|pdf|prc)$", rel, re.I):
            continue
        name = os.path.basename(rel)
        m = re.match(r"^(?P<au>.+?) \((?P<y>\d{4})\)\. (?:\((?P<s>.+?) (?P<i>\d+(?:\.\d+)?)\) )?(?P<t>.+?)(?: \[\d+\])?\.\w+$", name)
        if not m:
            continue
        au = m["au"]
        if ", " in au and " & " not in au:  # "Leon, Donna" -> "Donna Leon"
            last, first = au.split(", ", 1)
            au = f"{first} {last}"
        res.append({"src": "librosvarios", "sid": rel, "title": m["t"], "authors": [au], "series": m["s"],
                    "index": float(m["i"]) if m["i"] else None, "tags": [], "lang": None, "year": int(m["y"]),
                    "files": [{"rel": rel, "fmt": rel.rsplit(".", 1)[-1].lower(), "size": size}]})
    return res


def inventory(_):
    rows = []
    for line in open(f"{STATE}/folders.tsv"):
        s, p = line.rstrip("\n").split("\t", 1)
        rows.append((int(s), p))
    cands = []
    for name, folder in DBS:
        c = from_db(name, folder)
        log(f"{name}: {len(c)} libros con fichero")
        cands += c
    for fn in (from_lote, from_libros_varios):
        c = fn(rows)
        log(f"{c[0]['src'] if c else fn.__name__}: {len(c)} libros")
        cands += c
    json.dump(cands, open(f"{STATE}/candidates.json", "w"), ensure_ascii=False)
    log(f"{len(cands)} candidatos")


def loose_title(t):
    """El título sin lo que cambia entre ediciones: corchetes, "(Saga 3)", subtítulo, artículo."""
    t = norm(re.sub(r"[\[(][^\])]*[\])]", " ", t or ""))
    t = re.sub(ARTICLES, "", t)
    return t


def surnames(authors):
    return {w for a in authors for w in norm(a).split() if len(w) >= 4}


def dedupe(_):
    cands = json.load(open(f"{STATE}/candidates.json"))
    have = bc.library_books()
    exact = {bc.key(b["title"], b["au"]): b["id"] for b in have}
    by_loose = collections.defaultdict(list)
    for b in have:
        by_loose[loose_title(b["title"])].append(b)
    cands.sort(key=lambda c: PRIORITY.index(c["src"]))
    taken, out = {}, collections.Counter()
    for c in cands:
        k = bc.key(c["title"], c["authors"])
        lt = loose_title(c["title"])
        if k in exact:
            c["verdict"], c["match"] = "dup", exact[k]
        elif k in taken or (lt, frozenset(surnames(c["authors"]))) in taken:
            c["verdict"], c["match"] = "dup-src", taken.get(k) or taken[(lt, frozenset(surnames(c["authors"])))]
        else:
            near = [b for b in by_loose.get(lt, []) if surnames(c["authors"]) & surnames(b["au"]) or len(lt.split()) >= 3]
            if near:
                c["verdict"], c["match"] = "check", [b["id"] for b in near[:3]]
            else:
                c["verdict"] = "new"
            taken[k] = taken[(lt, frozenset(surnames(c["authors"])))] = f"{c['src']}:{c['sid']}"
        out[(c["src"], c["verdict"])] += 1
    json.dump(cands, open(f"{STATE}/dedupe.json", "w"), ensure_ascii=False)
    print("fuente         nuevos  dudosos  ya en BO  repetidos entre fuentes")
    for s in PRIORITY:
        print(f"  {s:13s} {out[(s, 'new')]:6d} {out[(s, 'check')]:8d} {out[(s, 'dup')]:9d} {out[(s, 'dup-src')]:9d}")
    log(f"nuevos {sum(v for (s, k), v in out.items() if k == 'new')}, dudosos {sum(v for (s, k), v in out.items() if k == 'check')}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("inventory", "dedupe"):
        sub.add_parser(c)
    a = ap.parse_args()
    {"inventory": inventory, "dedupe": dedupe}[a.cmd](a)
