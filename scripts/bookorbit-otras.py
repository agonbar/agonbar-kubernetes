#!/usr/bin/env python3
"""Las "Otras bibliotecas" de ivan-2 (nas03) a BookOrbit, sin duplicar lo que ya está y con el
estilo de la 75.452 (ver bookorbit-calibre.py).

  ./bookorbit-otras.py inventory   candidatos de cada fuente a ~/.cache/bookorbit-otras/candidates.json
  ./bookorbit-otras.py dedupe      exacto y aproximado contra BookOrbit y entre fuentes;
                                   deja dedupe.json con new / dup / check (dudosos para Strata)
  ./bookorbit-otras.py excerpt [--limit N]
                                   ~1500 palabras de cuerpo de cada nuevo y dudoso, y de los libros de
                                   BookOrbit con los que se comparan, a excerpts.jsonl (reanudable)

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
log, norm, sh, sql = bc.log, bc.norm, bc.sh, bc.sql

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


JUNK_AUTHOR = re.compile(r"@|^(administrador|admin|unknown|desconocido|autor desconocido|an[oó]nimo|usuario|user|owner|"
                        r"propietario|microsoft|pc|hp|toshiba|calibre|www\.|http)\b|\d{3,}", re.I)


def swap(name):
    """"Leon, Donna" o "Haldeman| Joe" -> "Donna Leon"."""
    m = re.match(r"^\s*([^,|]+?)\s*[,|]\s*([^,|]+?)\s*$", name)
    return f"{m.group(2)} {m.group(1)}" if m else name.strip()


def clean_meta(c):
    """Arregla lo que estropea el dedupe: autores "Apellido| Nombre" o un correo, "(c.1)" y la saga
    metida en el título, y el autor escrito dentro del título cuando el campo autor es basura."""
    t = c["title"] or ""
    t = re.sub(r"^microsoft (word|powerpoint) - ", "", t, flags=re.I)
    t = re.sub(r"\.(docx?|pdf|rtf|txt|epub|mobi|fb2|lit)$", "", t, flags=re.I)
    t = re.sub(r"\s*\(c\.\d+\)|\s*\(v\.\d+\)|\s*\((spanish edition|edici[oó]n en espa[nñ]ol|ESP|SP)\)", "", t, flags=re.I)
    if " " not in t.strip():
        t = t.replace("_", " ")
    authors = [swap(a) for a in c["authors"] if a and not JUNK_AUTHOR.search(a)]
    if not authors:
        m = re.match(r"^(?P<a>[^-]+?,[^-]+?)\s+-\s+(?P<t>.+)$", t) or re.match(r"^(?P<a>[^-]{3,40}?)\s+-\s+(?P<t>.+)$", t)
        if m:
            authors, t = [swap(m["a"])], m["t"]
    m = re.match(r"^\((?P<s>[^()]+?)\s+(?P<i>\d{1,3}(?:\.\d+)?)\)\s*(?P<t>.+)$", t)  # "(Dead End Dating 01) Citas..."
    if m and not c["series"]:
        c["series"], c["index"], t = m["s"], float(m["i"]), m["t"]
    m = re.match(r"^(?P<s>[^-]+?)\s+(?P<i>\d{1,3})\s+-\s+(?P<t>.+)$", t)       # "Corum 02 - La reina de las espadas"
    if m and not c["series"]:
        c["series"], c["index"], t = m["s"], float(m["i"]), m["t"]
    c["title"], c["authors"] = t.strip(), authors
    return c


def loose_title(t):
    """El título sin lo que cambia entre ediciones: corchetes, "(Saga 3)", subtítulo, artículo."""
    t = norm(re.sub(r"[\[(][^\])]*[\])]", " ", t or ""))
    t = re.sub(ARTICLES, "", t)
    return t


def surnames(authors):
    return {w for a in authors for w in norm(a).split() if len(w) >= 4}


def dedupe(_):
    cands = [clean_meta(c) for c in json.load(open(f"{STATE}/candidates.json"))]
    # Autor y título cruzados ("Audur Ava Ólafsdóttir" de "Rosa"): el título es un autor de la 75.452 y el autor no.
    known = {norm(a) for (a,) in sqlite3.connect(f"file:{bc.CAL_DB}?immutable=1", uri=True).execute("select name from authors")}
    for c in cands:
        if norm(c["title"]) in known and len(c["authors"]) == 1 and norm(c["authors"][0]) not in known:
            c["title"], c["authors"] = c["authors"][0], [c["title"]]
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


# Extractor de texto con la biblioteca estándar, para correrlo en nas03 y nas02 sin bajar los libros:
# epub (spine del OPF), fb2 (XML) y mobi sin comprimir o con PalmDOC. Lee rutas por stdin (separadas por
# NUL, "clave\truta") y escribe una línea JSON por libro: {"k", "text"} o {"k", "err"}.
EXTRACT_PY = r'''
import json, re, struct, sys, zipfile, html, posixpath
WORDS = (300, 1800)   # salta portada, créditos e índice; ~1500 palabras de cuerpo

def text_of(markup):
    markup = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h\d>", "\n", markup)
    return html.unescape(re.sub(r"<[^>]+>", " ", markup))

def epub(path):
    z = zipfile.ZipFile(path)
    opf = re.search(r'full-path="([^"]+)"', z.read("META-INF/container.xml").decode("utf-8", "replace")).group(1)
    o = z.read(opf).decode("utf-8", "replace")
    man = dict(re.findall(r'<item\b[^>]*?id="([^"]+)"[^>]*?href="([^"]+)"', o))
    man.update({i: h for h, i in re.findall(r'<item\b[^>]*?href="([^"]+)"[^>]*?id="([^"]+)"', o)})
    base = posixpath.dirname(opf)
    out = []
    for idref in re.findall(r'<itemref\b[^>]*?idref="([^"]+)"', o):
        h = man.get(idref)
        if not h:
            continue
        try:
            out.append(text_of(z.read(posixpath.normpath(posixpath.join(base, h))).decode("utf-8", "replace")))
        except KeyError:
            pass
        if sum(len(x.split()) for x in out) > WORDS[1] + 500:
            break
    return "\n".join(out)

def fb2(path):
    raw = open(path, "rb").read()
    enc = re.search(rb'encoding="([^"]+)"', raw[:200])
    t = raw.decode(enc.group(1).decode() if enc else "utf-8", "replace")
    t = re.sub(r"(?is)<binary[^>]*>.*?</binary>", " ", t)
    body = re.search(r"(?is)<body[^>]*>(.*)", t)
    return text_of(body.group(1) if body else t)

def palmdoc(d):
    out, i = bytearray(), 0
    while i < len(d):
        c = d[i]; i += 1
        if c == 0 or 9 <= c <= 0x7f:
            out.append(c)
        elif c <= 8:
            out += d[i:i + c]; i += c
        elif c >= 0xc0:
            out += bytes([32, c ^ 0x80])
        else:
            if i >= len(d): break
            c = (c << 8) | d[i]; i += 1
            dist, n = (c >> 3) & 0x7ff, (c & 7) + 3
            if not dist or dist > len(out): break
            for _ in range(n): out.append(out[-dist])
    return bytes(out)

def trailing(rec, flags):
    def entry(data):
        res, bit = 0, 0
        for v in reversed(data[-4:]):
            res |= (v & 0x7f) << bit; bit += 7
            if v & 0x80: break
        return res
    n, f = 0, flags >> 1
    while f:
        if f & 1: n += entry(rec[:len(rec) - n])
        f >>= 1
    if flags & 1 and len(rec) > n: n += (rec[len(rec) - n - 1] & 3) + 1
    return n

def mobi(path):
    d = open(path, "rb").read()
    nrec = struct.unpack(">H", d[76:78])[0]
    offs = [struct.unpack(">I", d[78 + 8 * i:82 + 8 * i])[0] for i in range(nrec)] + [len(d)]
    r0 = d[offs[0]:offs[1]]
    comp, _, count = struct.unpack(">HHIH", r0[:10])[0], 0, struct.unpack(">H", r0[8:10])[0]
    enc, flags = "cp1252", 0
    if r0[16:20] == b"MOBI":
        hlen = struct.unpack(">I", r0[20:24])[0]
        enc = "utf-8" if struct.unpack(">I", r0[28:32])[0] == 65001 else "cp1252"
        if hlen >= 0xe4: flags = struct.unpack(">H", r0[16 + 0xf2:16 + 0xf4])[0]
    if comp not in (1, 2):
        raise ValueError(f"compresión {comp}")
    out = []
    for i in range(1, min(count, 40) + 1):
        rec = d[offs[i]:offs[i + 1]]
        rec = rec[:len(rec) - trailing(rec, flags)] if flags else rec
        out.append(palmdoc(rec) if comp == 2 else rec)
        if sum(len(x) for x in out) > 60000: break
    return text_of(b"".join(out).decode(enc, "replace"))

for item in sys.stdin.read().split("\0"):
    if not item: continue
    k, p = item.split("\t", 1)
    try:
        ext = p.rsplit(".", 1)[-1].lower()
        t = {"epub": epub, "fb2": fb2, "mobi": mobi, "azw3": mobi, "azw": mobi, "prc": mobi}[ext](p)
        w = t.split()
        w = w[WORDS[0]:WORDS[1]] if len(w) > WORDS[1] else w[:WORDS[1]]
        print(json.dumps({"k": k, "text": " ".join(w)}, ensure_ascii=False), flush=True)
    except Exception as e:
        print(json.dumps({"k": k, "err": f"{type(e).__name__}: {e}"[:200]}), flush=True)
'''


PREF = ["epub", "fb2", "azw3", "mobi", "prc", "pdf", "doc", "docx", "rtf", "lit", "odt", "txt"]
NAS_FMT = {"epub", "fb2", "mobi", "azw3", "azw", "prc"}
NIX_TXT = ["nix", "shell", "nixpkgs#calibre", "nixpkgs#poppler-utils", "nixpkgs#catdoc", "-c"]


def best_file(c):
    fs = [f for f in c["files"] if f["fmt"] in PREF]
    return min(fs, key=lambda f: PREF.index(f["fmt"])) if fs else None


def run_extract(host, items):
    """items: [(clave, ruta en el host)] -> {clave: texto o None}."""
    out = {}
    for i in range(0, len(items), 500):
        inp = "\0".join(f"{k}\t{p}" for k, p in items[i:i + 500])
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", host, f"python3 -c {sh(EXTRACT_PY)}"], input=inp, capture_output=True, text=True)
        for line in r.stdout.splitlines():
            j = json.loads(line)
            out[j["k"]] = j.get("text") or None
        log(f"  {host}: {min(i + 500, len(items))}/{len(items)}")
    return out


def local_extract(items):
    """pdf, doc, lit y lo que el extractor del NAS no supo leer: se baja y se lee aquí."""
    import shutil, tempfile
    out = {}
    tmp = tempfile.mkdtemp(prefix="otras-", dir=STATE)
    try:
        for n, (k, rel) in enumerate(items):
            dst = os.path.join(tmp, "f." + rel.rsplit(".", 1)[-1].lower())
            r = subprocess.run(["scp", "-q", "-o", "BatchMode=yes", f"nas03:{OTRAS}/{rel}", dst], capture_output=True)
            if r.returncode:
                continue
            ext = dst.rsplit(".", 1)[-1]
            cmd = {"pdf": ["pdftotext", "-l", "12", dst, "-"], "doc": ["catdoc", dst]}.get(ext) or ["sh", "-c", f"ebook-convert {sh(dst)} {sh(tmp + '/o.txt')} >/dev/null 2>&1 && cat {sh(tmp + '/o.txt')}"]
            t = subprocess.run(["timeout", "-k", "10", "180"] + NIX_TXT + cmd, capture_output=True, text=True, errors="replace").stdout
            w = t.split()
            out[k] = " ".join(w[300:1800] if len(w) > 1800 else w[:1800]) or None
            for f in os.listdir(tmp):
                os.remove(os.path.join(tmp, f))
            if (n + 1) % 100 == 0:
                log(f"  local: {n + 1}/{len(items)}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def excerpt(args):
    cands = json.load(open(f"{STATE}/dedupe.json"))
    path = f"{STATE}/excerpts.jsonl"
    done = {json.loads(l)["k"] for l in open(path)} if os.path.exists(path) else set()
    todo_nas, todo_local, todo_bo = [], [], set()
    for c in cands:
        if c["verdict"] not in ("new", "check"):
            continue
        k = f"{c['src']}:{c['sid']}"
        f = best_file(c)
        if k not in done and f:
            (todo_nas if f["fmt"] in NAS_FMT else todo_local).append((k, f["rel"]))
        if c["verdict"] == "check":
            todo_bo.update(f"bo:{i}" for i in c["match"] if f"bo:{i}" not in done)
    if args.limit:
        todo_nas, todo_local = todo_nas[:args.limit], todo_local[:args.limit]
    log(f"por extraer: {len(todo_nas)} en nas03, {len(todo_local)} aquí, {len(todo_bo)} de BookOrbit en nas02")
    with open(path, "a") as fh:
        def save(res):
            for k, t in res.items():
                fh.write(json.dumps({"k": k, "text": t}, ensure_ascii=False) + "\n")
            fh.flush()
        res = run_extract("nas03", [(k, f"{OTRAS}/{rel}") for k, rel in todo_nas])
        retry = [(k, rel) for k, rel in todo_nas if not res.get(k)]
        save({k: t for k, t in res.items() if t})
        save(local_extract(todo_local + retry))
        if todo_bo:
            ids = ",".join(k.split(":")[1] for k in todo_bo)
            rows = sql(f"select b.id, f.absolute_path p from books b join book_files f on f.id=b.primary_file_id where b.id in ({ids})")
            save(run_extract("nas02", [(f"bo:{r['id']}", r["p"].replace("/library/", "/mnt/RAID/docker/media/", 1)) for r in rows]))
    log("hecho")


# Etiquetas de cada fuente -> géneros de epublibre (los de la 75.452). None: no dice nada.
TAG_MAP = {
    "intriga": "Intriga", "misterio": "Intriga", "suspense": "Intriga", "thriller": "Intriga", "det_action": "Intriga",
    "fantástico": "Fantástico", "fantasía": "Fantástico", "sf_fantasy": "Fantástico", "fantasy": "Fantástico", "sf_heroic": "Fantástico", "sf_epic": "Fantástico",
    "aventuras": "Aventuras", "adventure": "Aventuras", "adv_animal": "Aventuras",
    "histórico": "Histórico", "novela histórica": "Histórico", "prose_history": "Histórico", "adv_history": "Histórico",
    "ciencia ficción": "Ciencia ficción", "sf": "Ciencia ficción", "sf_social": "Ciencia ficción", "sf_action": "Ciencia ficción", "sf_space": "Ciencia ficción",
    "policíaco": "Policial", "policiaco": "Policial", "policial": "Policial", "novela negra": "Policial", "det_crime": "Policial",
    "detective": "Policial", "det_classic": "Policial", "det_police": "Policial",
    "romántico": "Romántico", "romántica": "Romántico", "love_contemporary": "Romántico", "love_history": "Romántico", "love_short": "Romántico", "love_sf": "Romántico",
    "erótico": "Erótico", "love_erotica": "Erótico",
    "drama": "Drama", "relato": "Relato", "cuentos": "Relato", "terror": "Terror", "horror": "Terror", "sf_horror": "Terror",
    "infantil y juvenil": "Juvenil", "juvenil": "Juvenil", "child_adv": "Juvenil", "infantil": "Infantil", "children": "Infantil", "child_tale": "Infantil",
    "clásico": "Clásico", "prose_classic": "Clásico", "humor": "Humor", "realista": "Realista", "prose_contemporary": "Realista",
    "teatro": "Teatro", "dramaturgy": "Teatro", "poesía": "Poesía", "poetry": "Poesía", "bélico": "Bélico", "novela": "Novela",
    "ensayo": "Ensayo", "biografía": "Memorias", "memorias": "Memorias", "nonf_biography": "Memorias", "autoayuda": "Autoayuda",
    "ciencia": "Divulgación", "divulgación": "Divulgación", "sci_popular": "Divulgación", "science": "Divulgación",
    "filosofía": "Filosofía", "sci_philosophy": "Filosofía", "historia": "Historia", "history": "Historia", "sci_history": "Historia",
    "referencia": "Referencia", "psicología": "Psicología", "sci_psychology": "Psicología", "espiritualidad": "Espiritualidad",
    "religión": "Espiritualidad", "religion": "Espiritualidad", "viajes": "Viajes", "arte": "Arte", "cocina": "Cocina", "home_cooking": "Cocina",
    "salud": "Salud y bienestar", "economía": "Ciencias sociales", "política": "Ciencias sociales", "sociología": "Ciencias sociales",
}
STOP = {"es": "que de la el en los las del se con por una para como pero más sus fue había estaba también".split(),
        "en": "the and of to was his her that with had for you not but they she said were".split(),
        "fr": "le la les et des est une que dans pas il elle qui sur avec était pour".split(),
        "it": "che il della non per una sono era gli alla nel anche questo".split(),
        "pt": "que não uma com para mais ele ela foi seu sua dos das também".split(),
        "ca": "que els les amb per una però més seu molt havia també".split()}


def lang_of(text):
    """Idioma por palabras frecuentes, sin LLM. None si el texto es demasiado corto para fiarse."""
    w = re.findall(r"[a-záéíóúüñàèòçãõ]+", (text or "").lower())
    if len(w) < 80:
        return None
    cnt = collections.Counter(w)
    score = {l: sum(cnt[x] for x in ws) / len(w) for l, ws in STOP.items()}
    best = max(score, key=score.get)
    return best if score[best] > 0.04 else None


def genres_of(c):
    return sorted({TAG_MAP[t.lower()] for t in c["tags"] if TAG_MAP.get(t.lower())})


class Strata:
    """Strata (Qwen) por port-forward al Service del clúster lamg. Sin thinking: el JSON sale igual de bien
    con 31 tokens en vez de 130."""
    def __init__(self, port=18400):
        import socket, time
        self.url = f"http://127.0.0.1:{port}/v1/chat/completions"
        self.pf = subprocess.Popen(bc.KUBE[:3] + ["-n", "strata", "port-forward", "svc/strata", f"{port}:8080"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                break
            except OSError:
                time.sleep(0.2)

    def ask(self, system, user, max_tokens=600):
        import urllib.request, time
        body = json.dumps({"model": "qwen3.8-flash-next-iq3_xxs", "temperature": 0, "max_tokens": max_tokens,
                           "response_format": {"type": "json_object"}, "chat_template_kwargs": {"enable_thinking": False},
                           "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}).encode()
        for attempt in range(4):
            try:
                req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=600) as r:
                    return json.loads(json.loads(r.read())["choices"][0]["message"]["content"])
            except Exception as e:
                if attempt == 3:
                    raise
                time.sleep(20)

    def close(self):
        self.pf.terminate()


LANG3 = {"spa": "es", "es": "es", "eng": "en", "en": "en", "fre": "fr", "fra": "fr", "ita": "it", "por": "pt", "cat": "ca", "eus": "eu", "baq": "eu", "glg": "gl", "deu": "de", "ger": "de"}
SYS = "Eres un bibliotecario. Respondes solo con el JSON que se pide, sin texto alrededor."


def tokens(*xs):
    return set(norm(" ".join(x for x in xs if x)).split())


def judge(args):
    cands = json.load(open(f"{STATE}/dedupe.json"))
    ex = {}
    for line in open(f"{STATE}/excerpts.jsonl"):
        j = json.loads(line)
        ex[j["k"]] = j["text"]
    out_path = f"{STATE}/judge.jsonl"
    done = {json.loads(l)["k"] for l in open(out_path)} if os.path.exists(out_path) else set()
    allowed = sorted({t for (t,) in sqlite3.connect(f"file:{bc.CAL_DB}?immutable=1", uri=True).execute(
        "select t.name from tags t join books_tags_link l on l.tag=t.id group by t.id having count(*) >= 20")})
    titles = {}
    if any(c["verdict"] == "check" for c in cands):
        ids = sorted({i for c in cands if c["verdict"] == "check" for i in c["match"]})
        for i in range(0, len(ids), 2000):
            for r in sql(f"select b.id, m.title, coalesce((select string_agg(a.name, ' & ') from book_authors ba join authors a on a.id=ba.author_id where ba.book_id=b.id), '') au from books b join book_metadata m on m.book_id=b.id where b.id in ({','.join(map(str, ids[i:i + 2000]))})"):
                titles[r["id"]] = f"{r['title']} ({r['au']})"
    st = Strata()
    n = 0
    with open(out_path, "a") as fh:
        def save(k, d):
            fh.write(json.dumps({"k": k, **d}, ensure_ascii=False) + "\n")
            fh.flush()
        todo = [c for c in cands if c["verdict"] in ("new", "check") and f"{c['src']}:{c['sid']}" not in done]
        if args.only:
            todo = [c for c in todo if c["src"] == args.only]
        if args.limit:
            todo = todo[:args.limit]
        batch = []
        for c in todo:
            k = f"{c['src']}:{c['sid']}"
            text = ex.get(k)
            lang = lang_of(text) or LANG3.get((c.get("lang") or "").lower())
            if lang and lang != "es":
                save(k, {"lang": lang, "skip": "idioma"})
                continue
            if c["verdict"] == "check":
                others = [(i, ex.get(f"bo:{i}")) for i in c["match"]]
                same = None
                for i, t in others:
                    if not text or not t:
                        continue
                    r = st.ask(SYS, f"Libro A: {c['title']} ({', '.join(c['authors'])})\nFragmento A: {text[:2000]}\n\n"
                                    f"Libro B: {titles.get(i, '')}\nFragmento B: {t[:2000]}\n\n"
                                    "¿Son la misma obra (aunque sean ediciones o traducciones distintas)? "
                                    'Devuelve {"misma_obra": true|false}', 60)
                    if r.get("misma_obra") is True:
                        same = i
                        break
                if same:
                    save(k, {"dup": same})
                    continue
            g = genres_of(c)
            if g and c["src"] != "biblioteca":   # etiquetas útiles y metadatos fiables: sin LLM
                save(k, {"lang": lang or "es", "genres": g})
                continue
            batch.append((k, c, text))
            if len(batch) == 5:
                classify(st, batch, allowed, save)
                batch = []
            n += 1
            if n % 50 == 0:
                log(f"  {n}")
        if batch:
            classify(st, batch, allowed, save)
    st.close()
    log("hecho")


def classify(st, batch, allowed, save):
    items = []
    for i, (k, c, text) in enumerate(batch):
        files = ", ".join(os.path.basename(f["rel"]) for f in c["files"][:2])
        items.append(f"[{i}] título: {c['title']} | autor: {', '.join(c['authors']) or '?'} | fichero: {files}\n"
                     f"fragmento: {(text or '(sin texto)')[:1500]}")
    prompt = ("Para cada libro, a partir de sus datos y su fragmento:\n"
              "- es_libro: false si es un capítulo suelto, un documento, una receta, una revista o algo que no es un libro\n"
              "- idioma: código ISO 639-1 del fragmento\n"
              "- ficcion: true si es narrativa, poesía o teatro\n"
              f"- generos: 1 o 2 de esta lista exacta: {', '.join(allowed)}\n"
              "- titulo y autor: los correctos, usando SOLO palabras que aparecen en sus datos (corrige si están cruzados "
              "o con basura; si no se sabe el autor, \"\")\n\n" + "\n\n".join(items) +
              '\n\nDevuelve {"libros": [{"i": 0, "es_libro": bool, "idioma": "es", "ficcion": bool, "generos": [...], "titulo": "...", "autor": "..."}, ...]}')
    r = st.ask(SYS, prompt, 900)
    got = {x.get("i"): x for x in (r.get("libros") or []) if isinstance(x, dict)}
    for i, (k, c, text) in enumerate(batch):
        x = got.get(i)
        if not x:
            save(k, {"err": "sin respuesta"})
            continue
        src_tok = tokens(c["title"], " ".join(c["authors"]), " ".join(os.path.basename(f["rel"]) for f in c["files"]))
        title = x.get("titulo") if x.get("titulo") and tokens(x["titulo"]) <= src_tok else c["title"]
        author = x.get("autor") if x.get("autor") and tokens(x["autor"]) <= src_tok else (c["authors"][0] if c["authors"] else "")
        save(k, {"book": bool(x.get("es_libro")), "lang": str(x.get("idioma", ""))[:2], "fiction": bool(x.get("ficcion")),
                 "genres": [g for g in (x.get("generos") or []) if g in allowed][:2], "title": title, "author": author})


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("inventory", "dedupe"):
        sub.add_parser(c)
    p = sub.add_parser("judge")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--only", help="una sola fuente, p. ej. calibre")
    p = sub.add_parser("excerpt")
    p.add_argument("--limit", type=int, default=0, help="solo los N primeros de cada tipo, para probar")
    a = ap.parse_args()
    {"inventory": inventory, "dedupe": dedupe, "excerpt": excerpt, "judge": judge}[a.cmd](a)
