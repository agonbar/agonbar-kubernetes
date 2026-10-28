#!/usr/bin/env python3
"""Informática y Libros de cocina de ivan-2 (nas03) a BookOrbit: lo técnico a una biblioteca
propia, "Informática", y la cocina a "No ficción".

  ./bookorbit-informatica.py plan      decide qué entra y cómo; plan en ~/.cache/bookorbit-informatica
  ./bookorbit-informatica.py build     hardlinks de lo que BookOrbit ya lee, en nas03:/mnt/RAID/ivan/.info-out
  ./bookorbit-informatica.py convert   en este equipo, con nix (calibre, libreoffice, pandoc): chm y Word a
                                       epub, y cada carpeta de recetas sueltas de cocina a un solo epub;
                                       el resultado sube a .info-out
  ./bookorbit-informatica.py copy      nas02 trae .info-out a media/ con rsync (clave limitada a rrsync -ro)
  ./bookorbit-informatica.py status    cómo va ese rsync
  ./bookorbit-informatica.py revoke    quita la clave
  ./bookorbit-informatica.py meta [--apply]
                                       escanea y pone los metadatos: género = el tema de la carpeta,
                                       título limpio en los PDF; la cocina con autor, título y géneros

Reglas:
  - Informática va en book_per_file; su carpeta de tema (Redes, Seguridad...) es el género, no
    la serie: no hay orden de lectura entre dos libros de redes. Sí la hay en un curso troceado en
    capítulos: una subcarpeta con 3+ documentos es una serie, numerada por el nombre del fichero.
  - Fuera lo de menos de 100 KB (chuletas, ejercicios, apuntes) y las copias idénticas (sha1).
  - BookOrbit no abre chm ni doc/docx: se convierten a epub. El título y el autor de un epub o un
    chm convertido salen de sus propios metadatos; los de un PDF, del nombre del fichero limpio.
  - Cocina va a No ficción (book_per_folder) como Autor/Título/, con los géneros de epublibre
    Hogar y Cocina, que la dejan en el lado de no ficción. Las carpetas de recetas en Word de una
    página cada una ("Salsas y aliños") son un libro cada carpeta.
"""
import argparse, collections, importlib.util, json, os, re, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("comics", os.path.join(HERE, "bookorbit-comics.py"))
cm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cm)
bc, log, sh, sql, ssh = cm.bc, cm.log, cm.sh, cm.sql, cm.ssh

SRC = cm.SRC
OUT = "/mnt/RAID/ivan/.info-out"              # nas03
STATE = os.path.expanduser("~/.cache/bookorbit-informatica")
INFO_DIR, NF_DIR = "/library/informatica", "/library/no-ficcion"
MIN_BYTES = 100_000
NIX = ["nix", "shell", "nixpkgs#calibre", "nixpkgs#libreoffice", "nixpkgs#pandoc", "-c"]


def tool(secs):
    """El comando bajo `timeout`, que mata el grupo de procesos entero: el timeout de subprocess solo
    mataba a nix, y ebook-convert y su worker seguían vivos con la tubería abierta, colgando el script."""
    return ["timeout", "-k", "30", str(secs)] + NIX
READS = re.compile(r"\.(pdf|epub|mobi|azw3)$", re.I)
CONVERT = re.compile(r"\.(chm|docx?)$", re.I)
TOPIC = {"## A Ordenar ##": None, "OReilly 4 GB Collection": None, "Otros": None,
         "Manuales de soluciones existentes": "Manuales"}
PUBLISHERS = r"o'?reilly|apress|wiley|packt|manning|addison[ .-]?wesley|prentice[ .-]?hall|sams|wrox|mcgraw[ .-]?hill|no[ .-]?starch|syngress|peachpit|microsoft[ .-]press"
MONTHS = r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"


# Donde "Título - Autor" en el nombre del fichero no es un autor.
COCINA_FIX = {
    "Dieta - La Antidieta.pdf": ("La Antidieta", ["Anónimo"]),
    "Recetas de cocina para adelgazar, recetas de dietas bajas en hidratos de carbono - Dieta Atkins.pdf":
        ("Recetas de cocina para adelgazar (dieta Atkins)", ["Anónimo"]),
}


def title_from_name(name):
    t = re.sub(r"\.(pdf|epub|mobi|azw3|chm|docx?)$", "", name, flags=re.I)
    t = cm.clean(t)
    if " " not in t:
        t = t.replace(".", " ").replace("_", " ")
    t = re.sub(rf"^(?:{PUBLISHERS})\b[ ._-]*", "", t, flags=re.I)
    t = re.sub(rf"[ ._-]+(?:(?:{MONTHS})[a-z]*[ ._-]+)?(?:19|20)\d\d$", "", t, flags=re.I)  # "... Oct 2007"
    t = re.sub(r"[ ._]+", " ", t).strip(" -")
    return t or name


def cocina_meta(title):
    """"Tapas y pinchos-Karlos Arguiñano" -> título y autor; sin autor en el nombre, Anónimo."""
    m = re.match(r"^(.*\S)\s*-\s*([A-ZÁÉÍÓÚ][\w'.]+(?: [A-ZÁÉÍÓÚ][\w'.]+)+)$", title)
    return (m.group(1), [m.group(2)]) if m else (title, ["Anónimo"])


def plan(_):
    os.makedirs(STATE, exist_ok=True)
    files = [l.split("\t", 1) for l in ssh("nas03", f"cd {sh(SRC)} && find Informática 'Libros de cocina' -type f -printf '%s\\t%p\\n'").splitlines()]
    files = [(int(s), p) for s, p in files]
    items = []
    # Informática: lo que se lee o se convierte, de 100 KB para arriba, sin copias idénticas.
    info = [(s, p) for s, p in files if p.startswith("Informática/") and (READS.search(p) or CONVERT.search(p)) and s >= MIN_BYTES]
    by_size = collections.defaultdict(list)
    for s, p in info:
        by_size[s].append(p)
    suspects = [p for v in by_size.values() if len(v) > 1 for p in v]
    sums = {}
    for i in range(0, len(suspects), 2000):
        for line in ssh("nas03", f"cd {sh(SRC)} && xargs -0 sha1sum", "\0".join(suspects[i:i + 2000])).splitlines():
            h, p = line.split("  ", 1)
            sums[p] = h
    groups = collections.defaultdict(list)
    for s, p in info:
        groups[sums.get(p, p)].append(p)
    keep = [min(v, key=lambda p: (p.startswith("Informática/## A Ordenar"), len(p))) for v in groups.values()]
    # Un curso troceado ("Curso Java SE/Java16.pdf") es una serie: la subcarpeta, si al menos 3 de sus
    # documentos y el 60% llevan número de capítulo (1-3 cifras; un año de 4 no cuenta). Una carpeta
    # "Ebooks" o "Linux" con libros sueltos no lo es.
    chap = lambda p: re.search(r"(?<!\d)(\d{1,3})(?!\d)(?!.*(?<!\d)\d{1,3}(?!\d))", re.sub(r"\.\w+$", "", os.path.basename(p)))
    per_dir = collections.defaultdict(list)
    for p in keep:
        per_dir[os.path.dirname(p)].append(p)
    course = {d for d, ps in per_dir.items() if d.count("/") >= 2 and sum(1 for p in ps if chap(p)) >= max(3, 0.6 * len(ps))}
    for p in keep:
        parts = p.split("/")
        series = cm.clean(parts[-2]) if os.path.dirname(p) in course else None
        num = chap(p) if series else None
        topic = TOPIC.get(parts[1], parts[1]) if len(parts) > 2 else None
        conv = bool(CONVERT.search(p))
        rel = "/".join([cm.clean(x) for x in parts[1:-1]] + [re.sub(r"\.(chm|docx?)$", ".epub", parts[-1], flags=re.I) if conv else parts[-1]])
        items.append({"src": p, "lib": "informatica", "dst": rel, "convert": "epub" if conv else None,
                      "seriesName": series, "seriesIndex": str(int(num.group(1))) if num else None,
                      "title": None if conv or not p.lower().endswith(".pdf") else title_from_name(parts[-1]),
                      "genres": [topic] if topic else [],
                      "tags": ["O'Reilly"] if parts[1] == "OReilly 4 GB Collection" else []})
    # Cocina: los PDF son un libro cada uno; las carpetas de recetas en Word, un libro por carpeta.
    words = collections.defaultdict(list)
    for s, p in files:
        if not p.startswith("Libros de cocina/"):
            continue
        parts = p.split("/")
        if re.search(r"\.docx?$", p, re.I):
            words["/".join(parts[:-1])].append(p)
        elif p.lower().endswith(".pdf"):
            title, authors = cocina_meta(title_from_name(parts[-1]))
            items.append({"src": p, "lib": "no-ficcion", "dst": f"{authors[0]}/{title}/{title}.pdf", "title": title,
                          "authors": authors, "genres": ["Hogar", "Cocina"], "language": "es"})
    for folder, docs in words.items():
        title, authors = cocina_meta(folder.split("/")[-1] if folder != "Libros de cocina" else "Recetas sueltas")
        items.append({"src": folder, "docs": sorted(docs), "lib": "no-ficcion", "dst": f"{authors[0]}/{title}/{title}.epub",
                      "convert": "merge", "title": title, "authors": authors, "genres": ["Hogar", "Cocina"], "language": "es"})
    json.dump(items, open(f"{STATE}/plan.json", "w"), ensure_ascii=False, indent=0)
    c = collections.Counter((it["lib"], it.get("convert") or "tal cual") for it in items)
    log(f"plan: {len(items)} libros {dict(c)}; {len(info) - len(keep)} copias idénticas y "
        f"{sum(1 for s, p in files if p.startswith('Informática/') and (READS.search(p) or CONVERT.search(p)) and s < MIN_BYTES)} ficheros de menos de 100 KB fuera")
    for it in items[:: max(1, len(items) // 15)]:
        print(f"  {it['lib']}/{it['dst']}   [{it.get('title') or '(metadatos del fichero)'}; {', '.join(it.get('genres', []))}]")


BUILD_PY = r'''
import json, os, sys
src, out = sys.argv[1], sys.argv[2]
n = 0
for it in json.load(sys.stdin):
    if it.get("convert"):
        continue
    dst = os.path.join(out, it["lib"], it["dst"])
    if not os.path.exists(dst):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.link(os.path.join(src, it["src"]), dst)
        n += 1
print(f"{n} hardlinks nuevos")
'''


def build(_):
    items = json.load(open(f"{STATE}/plan.json"))
    print(ssh("nas03", f"python3 -c {sh(BUILD_PY)} {sh(SRC)} {sh(OUT)}", json.dumps(items)).strip())


def convert(_):
    items = [it for it in json.load(open(f"{STATE}/plan.json")) if it.get("convert")]
    work = os.path.join(STATE, "work")
    srcs = sorted({p for it in items for p in (it.get("docs") or [it["src"]])})
    have = set(ssh("nas03", f"cd {sh(OUT)} 2>/dev/null && find . -type f -printf '%P\\n' || true").splitlines())
    todo = [it for it in items if f"{it['lib']}/{it['dst']}" not in have]
    log(f"{len(todo)} por convertir de {len(items)}")
    if not todo:
        return
    # Bajar solo los originales que hacen falta.
    need = sorted({p for it in todo for p in (it.get("docs") or [it["src"]]) if not os.path.exists(f"{work}/src/{p}")})
    os.makedirs(f"{work}/src", exist_ok=True)
    tar = subprocess.Popen(["ssh", "-o", "BatchMode=yes", "nas03", f"cd {sh(SRC)} && tar cf - --null -T -"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    x = subprocess.Popen(["tar", "xf", "-", "-C", f"{work}/src"], stdin=tar.stdout)
    tar.stdin.write(b"\0".join(p.encode() for p in need) + (b"\0" if need else b""))
    tar.stdin.close()
    if x.wait() or tar.wait():
        raise SystemExit("falló la bajada de nas03")
    done = failed = 0
    for it in todo:
        dst = os.path.join(work, "out", it["lib"], it["dst"])
        if os.path.exists(dst) and os.path.getsize(dst):  # de una pasada anterior que se cortó
            done += 1
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = os.path.join(work, "tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        try:
            if it["convert"] == "merge":
                # Cada receta a docx (LibreOffice lee el .doc antiguo), luego a markdown con su título
                # como capítulo, y pandoc junta todo en un epub.
                subprocess.run(tool(600) + ["soffice", "--headless", "--convert-to", "docx", "--outdir", tmp] +
                               [f"{work}/src/{d}" for d in it["docs"]], check=True, capture_output=True, timeout=600)
                parts = []
                for d in it["docs"]:
                    base = os.path.splitext(os.path.basename(d))[0]
                    md = subprocess.run(tool(120) + ["pandoc", f"{tmp}/{base}.docx", "-t", "markdown", "--shift-heading-level-by=1"],
                                        check=True, capture_output=True, text=True, timeout=120).stdout
                    parts.append(f"# {base}\n\n{md}")
                open(f"{tmp}/libro.md", "w").write("\n\n".join(parts))
                subprocess.run(tool(300) + ["pandoc", f"{tmp}/libro.md", "-o", dst, "--toc", "--metadata", f"title={it['title']}",
                                      "--metadata", f"author={it['authors'][0]}", "--metadata", "lang=es"], check=True, capture_output=True, timeout=300)
            elif it["src"].lower().endswith(".chm"):
                # --flow-size 0: sin trocear el html; el troceo falla en varios O'Reilly ("SplitError ... _toc.html").
                subprocess.run(tool(900) + ["ebook-convert", f"{work}/src/{it['src']}", dst, "--flow-size", "0"], check=True, capture_output=True, timeout=960)
            else:  # doc / docx
                subprocess.run(tool(600) + ["soffice", "--headless", "--convert-to", "epub", "--outdir", tmp, f"{work}/src/{it['src']}"],
                               check=True, capture_output=True, timeout=600)
                out = [f for f in os.listdir(tmp) if f.endswith(".epub")]
                if not out:
                    raise RuntimeError("LibreOffice no dejó epub")
                shutil.move(os.path.join(tmp, out[0]), dst)
            done += 1
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError) as e:
            failed += 1
            err = getattr(e, "stderr", b"") or b""
            log(f"  FALLA {it['src']}: {str(err if isinstance(err, str) else err.decode(errors='replace'))[-200:] or e}")
            if os.path.exists(dst):
                os.remove(dst)
        if (done + failed) % 50 == 0:
            log(f"  {done + failed}/{len(todo)}")
    # Subir lo convertido a .info-out en nas03.
    up = subprocess.Popen(["tar", "cf", "-", "-C", f"{work}/out", "."], stdout=subprocess.PIPE)
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "nas03", f"mkdir -p {sh(OUT)} && tar xf - -C {sh(OUT)}"], stdin=up.stdout)
    if r.returncode or up.wait():
        raise SystemExit("falló la subida a nas03")
    shutil.rmtree(work)
    log(f"{done} convertidos, {failed} fallidos")


def _with_out(fn):
    """copy/status/revoke de bookorbit-comics, con .info-out y su propia clave."""
    def run(a):
        cm.OUT, cm.TAG = OUT, "bookorbit-informatica"
        cm.ALIVE = f"{{ [ -f ~/{cm.TAG}.pid ] && kill -0 $(cat ~/{cm.TAG}.pid) 2>/dev/null; }}"
        cm.LIBS = {"informatica": INFO_DIR}
        return fn(a)
    return run


def meta(args):
    items = json.load(open(f"{STATE}/plan.json"))
    by_rel = {(it["lib"], it["dst"]): it for it in items}
    api = bc.Api() if args.apply else None
    for lib_name, lib_dir in (("informatica", INFO_DIR), ("no-ficcion", NF_DIR)):
        lib = sql(f"select library_id from library_folders where path='{lib_dir}'")[0]["library_id"]
        if api:
            try:
                api("POST", f"/scanner/libraries/{lib}/scan", {})
            except RuntimeError as e:
                if "already running" not in str(e):
                    raise
            while sql(f"select count(*) c from scan_jobs where library_id={lib} and status not in ('completed','failed','cancelled')")[0]["c"] != "0":
                bc.time.sleep(10)
        rows, last = [], 0
        while True:
            chunk = sql(f"""select b.id, f.rel_path from books b join book_files f on f.id=b.primary_file_id
                where b.library_id={lib} and b.status='present' and b.id>{last} order by b.id limit 3000""")
            if not chunk:
                break
            rows += chunk
            last = chunk[-1]["id"]
        n = 0
        for r in rows:
            it = by_rel.get((lib_name, r["rel_path"]))
            if not it:
                continue
            fix = COCINA_FIX.get(os.path.basename(it["src"]))
            if fix:
                it = {**it, "title": fix[0], "authors": fix[1]}
            md, locks = {"genres": it.get("genres", []), "tags": it.get("tags", [])}, []
            for k in ("title", "authors", "language", "seriesName", "seriesIndex"):
                if it.get(k):
                    md[k] = it[k]
                    locks.append(k)
            n += 1
            if api:
                api("PATCH", f"/books/{r['id']}/metadata-and-locks", {"metadata": md, "lockedFields": locks})
        log(f"{lib_dir}: {n} libros {'con metadatos' if api else 'por poner (sin --apply)'}")
    if api:
        bc.rename(api)  # la cocina de No ficción a Autor/Título, como el resto de la biblioteca
        bc.prune()
        api.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("plan", "build", "convert", "copy", "status", "revoke"):
        sub.add_parser(c)
    p = sub.add_parser("meta")
    p.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    {"plan": plan, "build": build, "convert": convert, "copy": _with_out(cm.copy), "status": _with_out(cm.status),
     "revoke": _with_out(cm.revoke), "meta": meta}[a.cmd](a)
