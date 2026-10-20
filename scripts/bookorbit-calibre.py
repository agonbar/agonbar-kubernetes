#!/usr/bin/env python3
"""Importa la biblioteca Calibre "75.452" (la colección de Iván) en BookOrbit, con los
metadatos de Calibre como fuente canónica.

  ./bookorbit-calibre.py conform [--apply]
      Ajusta los libros que ya hay en la biblioteca a los metadatos de la 75.452: autor,
      título, saga y número, año, sinopsis, géneros. Casa por título y autor, con las
      correcciones de bookorbit-calibre-manual.tsv. Sin --apply solo enseña los cambios.

  ./bookorbit-calibre.py import --authors REGEX [--limit N] [--apply]
      Copia de nas03 al Book Dock los libros cuyo autor (orden Calibre) casa con REGEX y
      que la biblioteca aún no tiene, les pone los metadatos canónicos y los finaliza.
      El original se queda en nas03. Sin --apply solo dice cuántos entrarían.

  ./bookorbit-calibre.py split [--apply]
      Lleva cada libro ya importado a su biblioteca: "Ficción" o "No ficción" según sus
      géneros de Calibre (ver FICTION y NONFICTION). Import ya los manda a la buena.

  ./bookorbit-calibre.py dupes
      Libros de la biblioteca con el mismo título y autor normalizados.

Las reglas que hacen la base consistente:
  - Autor, título, saga y número salen de la 75.452 tal cual, aunque BookOrbit o Goodreads
    digan otra cosa ("Tolstói", "Saga de la Fundación - Ciclo de Trantor" #3).
  - Las colecciones editoriales (bookorbit-calibre-collections.txt) no son sagas: pasan a
    la etiqueta "Colección: X" y el libro queda sin serie.
  - Los campos que pone el script quedan bloqueados, para que ninguna búsqueda de
    metadatos los pise después.
  - Un libro de la 75.452 no entra si la biblioteca ya tiene uno con el mismo título y
    autor normalizados. El dedupe de BookOrbit no basta: no ve el duplicado si la saga
    tiene otro nombre.

El Book Dock vigila su carpeta con inotify, que no ve lo que se escribe por NFS desde el
NAS. Por eso la copia va a una carpeta de paso en el mismo NFS y se mueve al dock desde
dentro del pod.

Necesita kubectl con contexto lamg y ssh a nas02 y nas03. La API se usa con un token que
se firma dentro del pod con JWT_SECRET, ligado a la sesión BO_SID (por defecto 173,
"bookorbit-calibre (script)", creada para esto y revocable desde Ajustes > Sesiones).
"""
import argparse, json, os, re, socket, sqlite3, subprocess, sys, time, unicodedata, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
KUBE = ["kubectl", "--context", os.environ.get("KUBE_CTX", "lamg"), "-n", "piracy"]
CAL_REMOTE = "/mnt/RAID/ivan/ivan-2/Biblioteca 75.452 EPUB ESP/Biblioteca 75.452 EPUB ESP/Biblioteca"
CAL_DB = os.path.expanduser("~/.cache/bookorbit-calibre/metadata.db")
CAL_DIRS = os.path.expanduser("~/.cache/bookorbit-calibre/dirs.txt")
STAGE_NAS = "/mnt/SSD/torrents/.book-dock-stage"  # nas02; en el pod es /downloads/.book-dock-stage
STAGE_POD, DOCK_POD = "/downloads/.book-dock-stage", "/downloads/book-dock"
LIBRARY_ID, FOLDER_ID = 1, 1                 # "Ficción", /library/books
NF_PATH = "/library/no-ficcion"              # "No ficción"; su id se busca por la carpeta

# Ficción o no ficción, por los géneros de epublibre. Un libro es de no ficción solo si
# tiene algún género de NONFICTION y ninguno de FICTION; lo que no tiene ninguno de los
# dos ("Otros", "Variada", "Humor" solos) es casi todo cuento, poesía y novela, y va a
# ficción. Las etiquetas de tipo (Novela, Ensayo...) no sirven: solo las lleva un tercio.
FICTION = {"Novela", "Relato", "Teatro", "Poesía", "Cuentos", "Relatos cortos", "Interactivo", "Guion", "Intriga",
           "Romántico", "Policial", "Histórico", "Drama", "Ciencia ficción", "Fantástico", "Aventuras", "Juvenil",
           "Realista", "Terror", "Erótico", "Infantil", "Psicológico", "Sátira", "Filosófico", "Bélico", "Clásico"}
NONFICTION = {"Historia", "Ciencias sociales", "Divulgación", "Memorias", "Filosofía", "Espiritualidad", "Autoayuda",
              "Ciencias naturales", "Ensayo", "Comunicación", "Crítica y teoría literaria", "Psicología", "Arte",
              "Viajes", "Hogar", "Referencia", "Crónica", "Salud y bienestar", "Ciencias exactas", "Deportes y juegos",
              "Tecnología", "Sexualidad", "Manuales y cursos", "Padres e hijos", "Idiomas", "Diccionarios y enciclopedias",
              "Cocina", "Cocina y Arte", "Biografía", "Publicaciones periódicas", "Investigación", "Humanidades",
              "Sociologia", "Teología siglo XXI", "Didáctico"}
SID = int(os.environ.get("BO_SID", "173"))
PORT = int(os.environ.get("BO_PORT", "18300"))
LOCKS = ["title", "authors", "description", "publisher", "publishedYear", "language", "seriesName", "seriesIndex"]


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def key(title, authors):
    return norm(title) + "|" + " ".join(sorted(norm(" ".join(authors)).split()))


# ---------------------------------------------------------------- Calibre

class Calibre:
    def __init__(self):
        if not os.path.exists(CAL_DB):
            os.makedirs(os.path.dirname(CAL_DB), exist_ok=True)
            log("bajando metadata.db de nas03")
            subprocess.run(["scp", "-q", "-o", "BatchMode=yes", f"nas03:{CAL_REMOTE}/metadata.db", CAL_DB], check=True)
        self.db = sqlite3.connect(f"file:{CAL_DB}?immutable=1", uri=True)
        self.coll, self.saga = [], []
        for line in open(os.path.join(HERE, "bookorbit-calibre-collections.txt"), encoding="utf-8"):
            line = line.rstrip("\n")
            if line.strip() and not line.startswith("#"):
                (self.saga if line.startswith("!") else self.coll).append(re.compile(line.lstrip("!"), re.I))
        q = self.db.execute
        self.authors = {}
        for book, name in q("select l.book, a.name from books_authors_link l join authors a on a.id=l.author order by l.id"):
            self.authors.setdefault(book, []).append(name)
        self.series = dict(q("select l.book, s.name from books_series_link l join series s on s.id=l.series"))
        self.tags = {}
        for book, name in q("select l.book, t.name from books_tags_link l join tags t on t.id=l.tag order by l.id"):
            self.tags.setdefault(book, []).append(name)
        self.publisher = dict(q("select l.book, p.name from books_publishers_link l join publishers p on p.id=l.publisher"))
        self.comments = dict(q("select book, text from comments"))
        self.isbn = dict(q("select book, val from identifiers where type='isbn'"))
        self.books = {r[0]: r for r in q("select id, title, series_index, pubdate, path, author_sort from books")}
        # Nombre canónico de saga por (autor, saga normalizada), para corregir libros que no están en la 75.452.
        self.series_by_author = {}
        for b, s in self.series.items():
            saga = self.split_series(s)[0]
            for a in self.authors.get(b, []) if saga else []:
                self.series_by_author[(norm(a), norm(saga))] = saga

    def dirs(self):
        """id de Calibre -> carpeta real en nas03. La ruta de metadata.db no vale: la biblioteca
        pasó por un disco sin mayúsculas y "A. E. van Vogt" está en disco como "A. E. Van Vogt"."""
        if not os.path.exists(CAL_DIRS):
            log("listando las carpetas de la 75.452 en nas03")
            out = subprocess.run(["ssh", "-o", "BatchMode=yes", "nas03", f"cd {sh(CAL_REMOTE)} && find . -mindepth 2 -maxdepth 2 -type d -printf '%P\\n'"],
                                 capture_output=True, text=True, check=True).stdout
            open(CAL_DIRS, "w").write(out)
        out = {}
        for line in open(CAL_DIRS):
            m = re.search(r"\((\d+)\)$", line.rstrip("\n"))
            if m:
                out[int(m.group(1))] = line.rstrip("\n")
        return out

    def split_series(self, name):
        """(saga, colección) de un nombre de serie de Calibre."""
        if not name:
            return None, None
        m = re.match(r"^(.*?)\s*\((.+?),?\s*(\d+(?:\.\d+)?)?\)$", name)  # "Jazmín (Novias rebeldes, 2)"
        if m and any(p.match(m.group(1)) for p in self.coll):
            return m.group(2).strip(), m.group(1).strip()
        if any(p.match(name) for p in self.saga) or not any(p.match(name) for p in self.coll):
            return name, None
        return None, name

    def nonfiction(self, cid):
        g = set(self.tags.get(cid, []))
        return bool(g & NONFICTION) and not g & FICTION

    def canonical(self, cid):
        """Metadatos de BookOrbit para el libro cid de la 75.452."""
        _, title, idx, pubdate, _, _ = self.books[cid]
        raw = self.series.get(cid)
        saga, coll = self.split_series(raw)
        if saga and raw and saga != raw:  # el número va dentro del paréntesis
            m = re.search(r"(\d+(?:\.\d+)?)\)$", raw)
            idx = float(m.group(1)) if m else None
        md = {
            "title": title,
            "authors": self.authors.get(cid, []),
            "seriesName": saga,
            "seriesIndex": fmt_index(idx) if saga else None,
            "language": "es",
            "description": self.comments.get(cid),
            "publisher": self.publisher.get(cid),
            "genres": self.tags.get(cid, []),
            "tags": [f"Colección: {coll}"] if coll else [],
        }
        year = int(pubdate[:4]) if pubdate and pubdate[:4].isdigit() else 0
        md["publishedYear"] = year if 1000 <= year <= 2200 else None
        isbn = re.sub(r"[^0-9X]", "", self.isbn.get(cid, "") or "")
        if len(isbn) == 13:
            md["isbn13"] = isbn
        return md

    def by_key(self):
        out = {}
        for cid, (_, title, *_r) in self.books.items():
            out.setdefault(key(title, self.authors.get(cid, [])), []).append(cid)
        return out


def fmt_index(idx):
    if idx is None or idx < 0:
        return None
    return str(int(idx)) if float(idx).is_integer() else f"{idx:g}"


# ---------------------------------------------------------------- BookOrbit

def pod_node(js, *args, inp=None):
    cmd = KUBE + ["exec"] + (["-i"] if inp else []) + ["deploy/bookorbit", "--", "sh", "-c", 'cd /app && exec node -e "$0" "$@"', js, *args]
    r = subprocess.run(cmd, input=inp, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"kubectl exec: {r.stderr.strip()[-400:] or r.returncode}")
    return r.stdout


SQL_JS = r'''const {Client}=require("pg");(async()=>{const c=new Client({host:process.env.POSTGRES_HOST,port:+process.env.POSTGRES_PORT,
database:process.env.POSTGRES_DB,user:process.env.POSTGRES_USER,password:process.env.POSTGRES_PASSWORD});await c.connect();
const r=await c.query(process.argv[1]);console.log(JSON.stringify(r.rows));await c.end();})().catch(e=>{console.error(e.message);process.exit(1)})'''


def sql(query):
    return json.loads(pod_node(SQL_JS, query))


_nf = None


def nf_target():
    """(library_id, folder_id) de "No ficción", por la carpeta."""
    global _nf
    if _nf is None:
        r = sql(f"select library_id, id from library_folders where path='{NF_PATH}'")
        if not r:
            raise SystemExit(f"no hay biblioteca con la carpeta {NF_PATH}")
        _nf = (r[0]["library_id"], r[0]["id"])
    return _nf


def target(cal, cid):
    return nf_target() if cal.nonfiction(cid) else (LIBRARY_ID, FOLDER_ID)


def library_books():
    """id, title, authors, series de la biblioteca, por tramos: kubectl exec corta la salida hacia los 800 KB."""
    out, last = [], 0
    while True:
        rows = sql(f"""select b.id, b.library_id lib, m.title, m.series_name s, m.series_index si, m.published_year y,
            coalesce((select json_agg(a.name order by ba.display_order) from book_authors ba join authors a on a.id=ba.author_id
                      where ba.book_id=b.id), '[]') au,
            coalesce((select json_agg(t.name order by t.name) from book_tags bt join tags t on t.id=bt.tag_id where bt.book_id=b.id), '[]') tg
            from books b join book_metadata m on m.book_id=b.id
            where b.library_id in ({LIBRARY_ID}, {nf_target()[0]}) and b.id>{last} order by b.id limit 3000""")
        if not rows:
            return out
        out += rows
        last = rows[-1]["id"]


class Api:
    TOKEN_JS = r'''const c=require("crypto");const b=o=>Buffer.from(JSON.stringify(o)).toString("base64url");const n=Math.floor(Date.now()/1000);
const h=b({alg:"HS256",typ:"JWT"}),p=b({sub:1,ver:1,sid:+process.argv[1],amr:"password",iat:n,exp:n+43200});
console.log(h+"."+p+"."+c.createHmac("sha256",process.env.JWT_SECRET).update(h+"."+p).digest("base64url"))'''

    def __init__(self):
        self.pf = None
        self.login()
        self.forward()

    def login(self):
        self.token = pod_node(self.TOKEN_JS, str(SID)).strip()

    def forward(self):
        """kubectl port-forward se cae en una ejecución de horas; se relanza cuando muere."""
        if self.pf and self.pf.poll() is None:
            return
        self.pf = subprocess.Popen(KUBE + ["port-forward", "svc/bookorbit", f"{PORT}:3000"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", PORT), 0.2).close()
                break
            except OSError:
                time.sleep(0.2)

    def __call__(self, method, path, body=None, raw=False):
        for attempt in range(4):
            req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/v1{path}", method=method,
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=600) as r:
                    data = r.read()
                    return data if raw else (json.loads(data) if data else None)
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt < 3:  # el token dura 12 h y la importación entera, unas 40
                    self.login()
                    continue
                raise RuntimeError(f"{method} {path}: HTTP {e.code} {e.read()[:500].decode(errors='replace')}")
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                if attempt == 3:
                    raise
                time.sleep(5)
                self.forward()

    def close(self):
        self.pf.terminate()


def rename(api):
    """Con fileRenameEnabled, el renombrado coloca cada libro según sus metadatos."""
    for lib in (LIBRARY_ID, nf_target()[0]):
        pending = api("GET", f"/libraries/{lib}/bulk-rename/preview?page=1&pageSize=1")["totalByStatus"]
        if pending.get("will_rename"):
            api("POST", f"/libraries/{lib}/bulk-rename/execute", {}, raw=True)  # responde un stream de eventos
            while api("GET", f"/libraries/{lib}/bulk-rename/status")["running"]:
                time.sleep(3)


def prune():
    """Quita los autores sin libros que dejan las grafías corregidas ("J.R.R. Tolkien", "Homer") y
    las carpetas vacías que deja el renombrado."""
    # Por SQL: DELETE /authors responde 404 a un autor sin libros, porque mira el acceso a
    # través de sus libros. Solo los referencia book_authors (vacío para ellos) y
    # author_enrichment_queue (ON DELETE CASCADE).
    gone = sql("delete from authors a where not exists (select 1 from book_authors ba where ba.author_id=a.id) and not a.has_photo returning a.id")
    if gone:
        log(f"{len(gone)} autores sin libros borrados")
    pod_node(r'''const fs=require("fs"),p=require("path");const walk=d=>{for(const n of fs.readdirSync(d)){const f=p.join(d,n);
if(fs.statSync(f).isDirectory()){walk(f);if(!fs.readdirSync(f).length)fs.rmdirSync(f);}}};process.argv.slice(1).forEach(walk);''', "/library/books", NF_PATH)


def diff(cur, new):
    return {k: v for k, v in new.items() if k in ("title", "authors", "seriesName", "seriesIndex", "tags", "publishedYear")
            and (cur.get(k) or None) != (v or None)}


def apply_metadata(api, book_id, md):
    body = {k: v for k, v in md.items() if v is not None}
    for k in ("seriesName", "seriesIndex"):  # null explícito para quitar una saga que sobra
        body[k] = md.get(k)
    api("PATCH", f"/books/{book_id}/metadata-and-locks", {"metadata": body, "lockedFields": LOCKS})


# ---------------------------------------------------------------- comandos

def conform(args):
    cal = Calibre()
    manual = read_manual()
    by_key = cal.by_key()
    by_title = {}
    for cid, (_, title, *_r) in cal.books.items():
        by_title.setdefault(norm(title), []).append(cid)
    api = Api() if args.apply else None
    changed = 0
    for b in library_books():
        cur = {"title": b["title"], "authors": b["au"], "seriesName": b["s"], "seriesIndex": b["si"], "publishedYear": b["y"], "tags": b["tg"]}
        fix = manual.get(b["id"], {})
        cid = int(fix["cal"]) if "cal" in fix else None
        if cid is None:
            hits = by_key.get(key(b["title"], b["au"])) or [
                c for c in by_title.get(norm(b["title"]), [])
                if set(norm(" ".join(b["au"])).split()) & set(norm(" ".join(cal.authors.get(c, []))).split())]
            cid = hits[0] if len(hits) == 1 else None
        if cid:
            new = cal.canonical(cid)
            # La 75.452 guarda una sola serie y a veces es la colección ("Aleación de ley" es
            # Nova #238, no Nacidos de la Bruma #4). Si el libro ya tenía una saga que la
            # 75.452 conoce para ese autor, se queda, con la grafía de la 75.452.
            if not new["seriesName"] and b["s"]:
                s = next((cal.series_by_author.get((norm(a), norm(b["s"]))) for a in new["authors"]
                          if cal.series_by_author.get((norm(a), norm(b["s"])))), None)
                if s:
                    new["seriesName"], new["seriesIndex"] = s, b["si"]
        else:  # fuera de la 75.452: autor y saga con la grafía de la 75.452 si ya la tiene
            new = dict(cur)
            for a in b["au"]:
                s = cal.series_by_author.get((norm(a), norm(b["s"] or "")))
                if s:
                    new["seriesName"] = s
            if "series" in fix:
                new["seriesName"] = fix["series"] or None
                new["seriesIndex"] = fix.get("index") if fix["series"] else None
        d = diff(cur, new)
        if not d:
            continue
        changed += 1
        print(f"#{b['id']} {' & '.join(b['au'])} - {b['title']}" + (f"  [cal {cid}]" if cid else "  [fuera de la 75.452]"))
        for k, v in d.items():
            print(f"    {k}: {cur.get(k)!r} -> {v!r}")
        if api:
            apply_metadata(api, b["id"], new)
    log(f"{changed} libros {'corregidos' if api else 'por corregir (sin --apply)'}")
    if api:
        rename(api)
        prune()
        api.close()


def import_(args):
    cal = Calibre()
    have = {key(b["title"], b["au"]) for b in library_books()}
    rx = re.compile(args.authors, re.I)
    todo = []
    for cid, r in sorted(cal.books.items(), key=lambda kv: (kv[1][5] or "", kv[1][1])):
        k = key(r[1], cal.authors.get(cid, []))
        if rx.match(r[5] or "") and k not in have:
            have.add(k)  # la 75.452 también trae algún libro repetido; entra el primero
            todo.append(cid)
    if args.limit:
        todo = todo[:args.limit]
    log(f"{len(todo)} libros por importar (autores ~ /{args.authors}/)")
    if not args.apply or not todo:
        return
    dirs = cal.dirs()
    missing = [c for c in todo if c not in dirs]
    for c in missing:
        log(f"  sin carpeta en nas03, me lo salto: {cal.books[c][4]}")
    todo = [c for c in todo if c in dirs]
    api = Api()
    settings = api("GET", "/book-dock/settings")
    try:
        if settings["autoFetchMetadata"]:  # los metadatos buenos son los de Calibre; 75.000 consultas a Goodreads sobran
            api("PUT", "/book-dock/settings", {**{k: v for k, v in settings.items() if k != "bookDockPath"}, "autoFetchMetadata": False})
        fails = 0
        for i in range(0, len(todo), args.batch):
            try:
                batch(api, cal, todo[i:i + args.batch], dirs)
                fails = 0
            except Exception as e:  # un lote roto no para el resto; lo recoge la siguiente pasada
                fails += 1
                log(f"FALLA el lote {i // args.batch + 1}: {e}")
                if fails == 3:
                    raise SystemExit("3 lotes seguidos fallando, paro")
                time.sleep(60)
            log(f"progreso: {min(i + args.batch, len(todo))}/{len(todo)}")
        prune()
    finally:
        if settings["autoFetchMetadata"]:
            api("PUT", "/book-dock/settings", {k: v for k, v in settings.items() if k != "bookDockPath"})
        api.close()


def batch(api, cal, cids, dirs):
    t0 = time.time()
    paths = [dirs[c] for c in cids]  # "Autor/Título (id)"
    # tar por ssh de nas03 a nas02, aplanando a "Título (id)" que es como el dock agrupa una unidad.
    src = subprocess.Popen(["ssh", "-o", "BatchMode=yes", "nas03", f"cd {sh(CAL_REMOTE)} && tar cf - --null -T -"],
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    dst = subprocess.Popen(["ssh", "-o", "BatchMode=yes", "nas02",
                            f"mkdir -p {STAGE_NAS} && cd {STAGE_NAS} && tar xf - --transform='s,^[^/]*/,,'"], stdin=src.stdout)
    src.stdin.write(b"\0".join(p.encode() for p in paths) + b"\0")
    src.stdin.close()
    if dst.wait() or src.wait():
        raise RuntimeError("falló la copia de nas03 a nas02")
    pod_node(r'''const fs=require("fs"),p=require("path");const [s,d]=process.argv.slice(1);
for(const n of fs.readdirSync(s)){if(fs.existsSync(p.join(d,n)))fs.rmSync(p.join(s,n),{recursive:true});else fs.renameSync(p.join(s,n),p.join(d,n));}''', STAGE_POD, DOCK_POD)
    # Si ya estaba en el dock (un lote anterior se cortó) vale la que hay; la copia nueva sobra.
    t1 = time.time()
    want = {os.path.basename(p) for p in paths}
    rows = []
    for _ in range(600):
        rows = sql("select id, status, unit_directory u, error_message e from book_dock_files where unit_directory is not null")
        mine = [r for r in rows if os.path.basename(r["u"]) in want]
        if len(mine) == len(want) and all(r["status"] in ("ready", "error") for r in mine):
            break
        time.sleep(3)
    else:
        raise RuntimeError(f"el dock no terminó: {len(mine)}/{len(want)} unidades")
    t2 = time.time()
    ids, md_by_file = [], {}
    for r in mine:
        if r["status"] == "error":
            log(f"  error en el dock: {r['u']}: {r['e']}")
            continue
        cid = int(re.search(r"\((\d+)\)$", r["u"]).group(1))
        md = cal.canonical(cid)
        sel = {k: md[k] for k in ("title", "authors", "seriesName", "seriesIndex", "publishedYear", "language", "description", "publisher", "genres")}
        lib, folder = target(cal, cid)
        api("PATCH", f"/book-dock/files/{r['id']}", {"selectedMetadata": sel, "targetLibraryId": lib, "targetFolderId": folder})
        ids.append(r["id"])
        md_by_file[r["id"]] = md
    pre = api("POST", "/book-dock/finalize/preview", {"fileIds": ids, "defaultLibraryId": LIBRARY_ID, "defaultFolderId": FOLDER_ID})
    bad = {i["fileId"]: i["status"] for i in pre["items"] if i["status"] != "ready"}
    if pre["truncated"]:
        raise RuntimeError("la vista previa viene cortada; baja --batch por debajo de itemLimit")
    for fid, st in bad.items():
        log(f"  se queda en el dock ({st}): {md_by_file[fid]['authors']} - {md_by_file[fid]['title']}")
    ok = [i for i in ids if i not in bad]
    res = api("POST", "/book-dock/finalize", {"fileIds": ok, "defaultLibraryId": LIBRARY_ID, "defaultFolderId": FOLDER_ID}) if ok else {"results": []}
    done = 0
    for r in res["results"]:
        if r.get("success"):
            apply_metadata(api, r["bookId"], md_by_file[r["fileId"]])
            done += 1
        else:
            log(f"  falló al finalizar: {r.get('fileName')}: {r.get('error')}")
    log(f"lote de {len(cids)}: {done} importados, {len(bad)} en el dock para revisar; "
        f"copia {t1 - t0:.0f}s, dock {t2 - t1:.0f}s, finalizar {time.time() - t2:.0f}s")


def sh(s):
    return "'" + s.replace("'", "'\\''") + "'"


def split(args):
    """Lleva cada libro a su biblioteca, ficción o no ficción, en los dos sentidos. Los que
    no están en la 75.452 se quedan donde estén salvo que bookorbit-calibre-manual.tsv diga
    lib=nf o lib=f."""
    cal = Calibre()
    by_key = cal.by_key()
    manual = read_manual()
    nf = nf_target()
    moves = {LIBRARY_ID: [], nf[0]: []}
    for b in library_books():
        want = manual.get(b["id"], {}).get("lib")
        if want is None:
            cids = by_key.get(key(b["title"], b["au"]))
            if not cids:
                continue
            want = "nf" if cal.nonfiction(cids[0]) else "f"
        lib = nf[0] if want == "nf" else LIBRARY_ID
        if b["lib"] != lib:
            moves[lib].append(b["id"])
    for lib, ids in moves.items():
        log(f"{len(ids)} libros a la biblioteca {lib}{'' if args.apply else ' (sin --apply)'}")
        if not args.apply:
            continue
        api = Api()
        folder = nf[1] if lib == nf[0] else FOLDER_ID
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            body = {"selection": {"bookIds": chunk}, "targetLibraryId": lib, "targetFolderId": folder}
            out = api("POST", "/books/move", {**body, "collisionPolicy": "skip"}, raw=True).decode(errors="replace")
            bad = [l for l in out.splitlines() if l.startswith("data:") and '"status"' in l and '"success"' not in l]
            log(f"  movidos {i + len(chunk)}/{len(ids)}" + (f", {len(bad)} sin mover: {bad[:3]}" if bad else ""))
        api.close()
    if args.apply:
        prune()


def read_manual():
    manual = {}
    for line in open(os.path.join(HERE, "bookorbit-calibre-manual.tsv"), encoding="utf-8"):
        if line.strip() and not line.startswith("#"):
            bid, *kv = line.rstrip("\n").split("\t")
            manual[int(bid)] = dict(x.split("=", 1) for x in kv)
    return manual


def dupes(_):
    seen = {}
    for b in library_books():
        seen.setdefault(key(b["title"], b["au"]), []).append(b)
    for k, bs in seen.items():
        if len(bs) > 1:
            print("  " + " | ".join(f"#{b['id']} {' & '.join(b['au'])} - {b['title']} ({b['s']} #{b['si']})" for b in bs))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("conform")
    p.add_argument("--apply", action="store_true")
    p = sub.add_parser("import")
    p.add_argument("--authors", required=True, help="regex sobre el author_sort de Calibre, p. ej. '^A'")
    p.add_argument("--limit", type=int)
    p.add_argument("--batch", type=int, default=150)
    p.add_argument("--apply", action="store_true")
    p = sub.add_parser("split")
    p.add_argument("--apply", action="store_true")
    sub.add_parser("dupes")
    a = ap.parse_args()
    {"conform": conform, "import": import_, "split": split, "dupes": dupes}[a.cmd](a)
