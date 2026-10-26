#!/usr/bin/env python3
"""Los cómics y el manga de ivan-2 (nas03) a las bibliotecas "Cómics" y "Manga" de BookOrbit.

  ./bookorbit-comics.py plan     lista nas03 y decide el destino de cada fichero; deja el plan en
                                 ~/.cache/bookorbit-comics/plan.json y un resumen en pantalla
  ./bookorbit-comics.py build    monta el árbol de destino en nas03 (/mnt/RAID/ivan/.comics-out):
                                 hardlinks de los cbr/cbz/pdf y un cbz por carpeta de imágenes
  ./bookorbit-comics.py copy     nas02 trae .comics-out a media/comics y media/manga-cbz con rsync,
                                 con una clave limitada a `rrsync -ro`; corre en segundo plano allí
  ./bookorbit-comics.py status   cómo va ese rsync
  ./bookorbit-comics.py revoke   quita la clave de nas03 y limpia nas02
  ./bookorbit-comics.py meta [--apply]
                                 escanea las dos bibliotecas y pone serie, número y título a cada
                                 cómic a partir de su ruta, con esos campos bloqueados

Reglas:
  - Un fichero es un libro (bibliotecas en book_per_file). La serie es su carpeta y el número
    sale del nombre. BookOrbit solo mira el nombre del fichero y deja la serie vacía.
  - Los nombres de carpeta y fichero se limpian de etiquetas de los grupos de escaneo:
    "[CRG]", "(comicrel.usuarios.org)", "[por X]", "(Completo)", la extensión doble ".rar.cbr".
  - Copias idénticas (mismo tamaño y mismo sha1) entran una vez. Se queda la de la carpeta de la
    serie antes que la de "Marvel/Cronologia Sagas" o las de "COMICS DESCARGADOS".
  - Una carpeta con imágenes y sin ficheros de cómic es un tomo: se empaqueta en un cbz sin
    comprimir. Si una carpeta tiene cbr y jpg, los jpg son portadas y no entran.
  - Los originales de nas03 no se tocan: el árbol de destino son hardlinks fuera de la carpeta
    de Syncthing, y los cbz nuevos ocupan solo lo que pesan las imágenes.
"""
import argparse, collections, hashlib, importlib.util, json, os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("bc", os.path.join(HERE, "bookorbit-calibre.py"))
bc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bc)
log, sh, sql = bc.log, bc.sh, bc.sql

SRC = "/mnt/RAID/ivan/ivan-2"
OUT = "/mnt/RAID/ivan/.comics-out"                   # nas03, fuera de la carpeta de Syncthing
DEST = "/mnt/RAID/docker/media"                       # nas02
LIBS = {"comics": "/library/comics", "manga-cbz": "/library/manga-cbz"}
STATE = os.path.expanduser("~/.cache/bookorbit-comics")
N03_TS = os.environ.get("N03_TS", "100.72.0.140")    # nas03 en el tailnet, visto desde nas02
TAG = "bookorbit-comics"
OTRAS = "Libros EPUB/Otras bibliotecas/Comics"

# (carpeta en ivan-2, biblioteca, prefijo en el destino[, aplanar])
SOURCES = [
    ("Marvel", "comics", "Marvel"),
    (OTRAS, "comics", ""),
    ("Asterix y Obelix", "comics", "Astérix y Obélix"),
    ("Alita", "manga-cbz", "Alita"),
    ("Rurouni Kenshin (de N.Watsuki)(Ed.Glenat) [Completo][por Umbriel][6to y 8vo Aniversario CRG][lamansion-crg.net]",
     "manga-cbz", "Rurouni Kenshin"),
    ("One Piece", "manga-cbz", "One Piece", True),       # capítulos sueltos en subcarpetas de un foro
    ("Manga", "manga-cbz", ""),
]
MANGA_IN_COMICS = {"Akira", "Battle Royale", "Love Hina"}
# Dentro de OTRAS: programas, y colecciones repetidas de las que entra otra versión más completa.
EXCLUDE = {"ComicRack 0.9.99", "RayViewer 1.09",
           "Tintin", "Las Aventuras de Tintin - Completo - 24 Numeros",       # entra "Tintin - 25 libros en español"
           "Las Aventuras de Tintin-Serie completa 24 libros comic español spanish pdf-Por Internete",
           "Thorgal Nº1-28 - Completo - Spanish - Español",                    # entra "Thorgal", 32 números
           "WATCHMEN COMICS"}                                                  # entra la Absolute en cbr
# Nombres de carpeta que no se arreglan limpiando etiquetas: la carpeta es la serie.
RENAME = {
    "Tintin - 25 libros en español": "Tintín",
    "Watchmen Alan Moore CBR Spanish": "Watchmen",
    "XIII Comic Spanish Español ColecciÃ³n Completa(nÂº1-16) by Halo": "XIII",
    "Frank Miller's Sin City - obra completa spanish": "Sin City",
    "Crisis.en.Tierras.Infinitas. .por.Nej.y.Hispanomic.Scan.Group": "Crisis en Tierras Infinitas",
    "The.Ultimates.vol1 .por.Erhnam.-.CRG": "The Ultimates vol1",
    "Daredevil - Born Again By Bullz COMPLETO": "Daredevil - Born Again",
    "Dragon Fall Coleccion Completa": "Dragon Fall",
    "Fathom por Masacre JCR y JDX": "Fathom",
    "La Espada Salvaje de Conan Vol.2 por Kiliam": "La Espada Salvaje de Conan Vol 2",
    "Legends COMPLETO por Nej": "Legends",
    "Planetary - Completo": "Planetary",
    "Valerian - colección completa": "Valerian",
    "New Warriors Vol4 (10 numeros)": "New Warriors Vol4",
    "The Darkness (HyH)": "The Darkness",
    "mortadelo y filemon": "Mortadelo y Filemón",
    "El jueves (publicacion de humor)": "El Jueves",
    "Numeros De La Revista El Jueves": "El Jueves",
    "Marvel Heroes - La ultima caceria Completo By Rken": "Marvel Heroes - La última cacería",
    "Thanos Quest -.El Guantelete del Infinito": "Thanos Quest - El Guantelete del Infinito",
    "kick-ass 2": "Kick-Ass 2", "KICK ASS": "Kick-Ass",
    "1985 (MINISERIE)": "1985",
    "13 RUE DEL PERCEBE": "13, Rue del Percebe",
    "DARK AVENGERS": "Dark Avengers", "FROM HELL": "From Hell", "MESSIAH COMPLEX": "Messiah Complex",
    "SECRET WARRIORS": "Secret Warriors", "THUNDERBOLTS": "Thunderbolts", "TEEN TITANS YEAR ONE": "Teen Titans Year One",
    "OJO DE LA CAMARA": "Ojo de la cámara", "HELLCAT": "Hellcat",
}
# Carpetas cajón: lo que cuelga directamente de ellas no tiene serie.
GRAB_BAGS = {"COMICS DESCARGADOS 1", "COMICS DESCARGADOS 2", "COMICS DESCARGADOS 3", "ULTIMOS COMICS", "Z COMICS", "Marvel"}
COMIC = re.compile(r"\.(cbr|cbz|cb7|pdf)$", re.I)
IMAGE = re.compile(r"\.(jpe?g|png|gif|webp)$", re.I)

# Etiquetas de grupos de escaneo, entre () o []: se van si contienen alguna de estas palabras.
TAG_WORDS = r"crg|comicrel|lamansion|completo|complete|spanish|español|espanol|traduc|scan|aniversario|por |by |cbr|steam|\.org|\.net|\.com|blogspot|edici[oó]n|ed\.|hmerl"


def clean(name, is_file=False):
    if is_file:
        name = re.sub(r"(\.rar)?\.(cbr|cbz|cb7|pdf)$", "", name, flags=re.I)
    name = re.sub(r"\[[^\]]*\]", " ", name)  # [CRG], [veronerea]: entre corchetes solo van etiquetas de escaneo
    name = re.sub(rf"\([^)]*?(?:{TAG_WORDS})[^)]*?\)", " ", name, flags=re.I)
    name = re.sub(r"\s+(?:by|por)\s+\S+$", "", name, flags=re.I)  # "... by Karlnv"; no "Locos por una gata"
    if " " not in name.strip():
        name = name.replace("_", " ").replace(".", " ")
    name = re.sub(r"[_]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" -.")
    return name or "Sin título"


def rank(path):
    """Qué copia se queda entre ficheros idénticos: el menor."""
    r = 0
    if "Cronologia Sagas" in path:
        r = 2
    if re.search(r"COMICS DESCARGADOS|ULTIMOS COMICS|Z COMICS", path):
        r = 3
    if path.startswith(OTRAS):
        r += 1  # la carpeta Marvel ya tiene casi todo lo de Marvel que está aquí repetido
    return (r, len(path))


def target(path, is_dir=False):
    """(biblioteca, ruta relativa limpia) de un fichero o carpeta de ivan-2, o None si no entra.
    Un fichero conserva su extensión; una carpeta de imágenes recibe ".cbz" en el plan."""
    for src, lib, prefix, *flat in SOURCES:
        if path == src or path.startswith(src + "/"):
            rest = path[len(src) + 1:].split("/") if path != src else []
            if src == OTRAS:
                if not rest or rest[0] in EXCLUDE:
                    return None
                if rest[0] in MANGA_IN_COMICS:
                    lib = "manga-cbz"
            if flat and rest:
                rest = rest[-1:]
            dirs = [RENAME.get(clean(p), clean(p)) for p in rest[:-1]]
            last = []
            if rest:
                ext = re.search(r"\.(cbr|cbz|cb7|pdf)$", rest[-1], re.I)
                last = [RENAME.get(clean(rest[-1]), clean(rest[-1]))] if is_dir else \
                       [clean(rest[-1], is_file=True) + "." + ext.group(1).lower()]
            return lib, "/".join(([prefix] if prefix else []) + dirs + last)
    return None


def ssh(host, cmd, inp=None):
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, cmd],
                       input=inp, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"{host}: {r.stderr.strip()[-400:]}")
    return r.stdout


def plan(_):
    os.makedirs(STATE, exist_ok=True)
    srcs = " ".join(sh(s[0]) for s in SOURCES)
    files = [l.split("\t", 1) for l in ssh("nas03", f"cd {sh(SRC)} && find {srcs} -type f -printf '%s\\t%p\\n'").splitlines()]
    comics = [(int(s), p) for s, p in files if COMIC.search(p) and target(p)]
    # Copias idénticas: mismo tamaño y luego sha1, calculado en nas03 solo para esos grupos.
    by_size = collections.defaultdict(list)
    for s, p in comics:
        by_size[s].append(p)
    suspects = [p for v in by_size.values() if len(v) > 1 for p in v]
    log(f"{len(comics)} ficheros de cómic; {len(suspects)} con el tamaño repetido, calculando sha1 en nas03")
    cache = f"{STATE}/sha1.json"  # el sha1 de 130 GB tarda; por ruta y tamaño, que no cambian
    sums = json.load(open(cache)) if os.path.exists(cache) else {}
    sizes = {p: s for s, p in comics}
    sums = {k: v for k, v in sums.items() if k.rsplit("|", 1)[0] in sizes and int(k.rsplit("|", 1)[1]) == sizes[k.rsplit("|", 1)[0]]}
    todo = [p for p in suspects if f"{p}|{sizes[p]}" not in sums]
    for i in range(0, len(todo), 2000):
        out = ssh("nas03", f"cd {sh(SRC)} && xargs -0 sha1sum", "\0".join(todo[i:i + 2000]))
        for line in out.splitlines():
            h, p = line.split("  ", 1)
            sums[f"{p}|{sizes[p]}"] = h
    json.dump(sums, open(cache, "w"))
    sums = {k.rsplit("|", 1)[0]: v for k, v in sums.items()}
    groups = collections.defaultdict(list)
    for s, p in comics:
        groups[sums.get(p, p)].append(p)
    keep = [min(v, key=rank) for v in groups.values()]
    dropped = len(comics) - len(keep)
    # Carpetas de imágenes sin ficheros de cómic: un tomo cada una.
    imgs = collections.Counter(os.path.dirname(p) for _, p in files if IMAGE.search(p))
    has_comic = {os.path.dirname(p) for _, p in files if COMIC.search(p)}
    packs = [d for d, n in imgs.items() if n >= 3 and d not in has_comic and target(d, is_dir=True)]
    items = [{"src": p, "lib": target(p)[0], "dst": target(p)[1]} for p in keep] + \
            [{"src": d, "lib": target(d, True)[0], "dst": target(d, True)[1] + ".cbz", "pack": True} for d in packs]
    # Dos orígenes que limpian al mismo nombre: el segundo lleva el número de orden detrás.
    seen = collections.Counter()
    for it in sorted(items, key=lambda x: x["src"]):
        k = (it["lib"], it["dst"].lower())
        seen[k] += 1
        if seen[k] > 1:
            base, ext = os.path.splitext(it["dst"])
            it["dst"] = f"{base} ({seen[k]}){ext}"
    json.dump(items, open(f"{STATE}/plan.json", "w"), ensure_ascii=False)
    per = collections.Counter((it["lib"], "cbz nuevo" if it.get("pack") else "fichero") for it in items)
    log(f"plan: {len(items)} cómics ({dict(per)}); {dropped} copias idénticas fuera")
    for it in items[:: max(1, len(items) // 12)]:
        print(f"  {it['lib']}/{it['dst']}   <-  {it['src']}")


BUILD_PY = r'''
import json, os, re, subprocess, sys
src, out = sys.argv[1], sys.argv[2]
items = json.load(sys.stdin)
key = lambda n: [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", n)]
done = skipped = 0
for it in items:
    dst = os.path.join(out, it["lib"], it["dst"])
    if os.path.exists(dst):
        skipped += 1
        continue
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if it.get("pack"):
        d = os.path.join(src, it["src"])
        imgs = sorted((f for f in os.listdir(d) if re.search(r"\.(jpe?g|png|gif|webp)$", f, re.I)), key=key)
        if os.path.exists(dst + ".part"):  # 7z a añade a un archivo existente; uno a medias se rehace
            os.remove(dst + ".part")
        # "--": hay imágenes que se llaman "-23.jpg" y 7z las tomaría por opciones.
        subprocess.run(["7z", "a", "-tzip", "-mx0", dst + ".part", "--"] + imgs, cwd=d, check=True, stdout=subprocess.DEVNULL)
        os.rename(dst + ".part", dst)
    else:
        os.link(os.path.join(src, it["src"]), dst)
    done += 1
print(f"{done} nuevos, {skipped} ya estaban")
'''


def build(_):
    items = json.load(open(f"{STATE}/plan.json"))
    log(f"montando {len(items)} cómics en nas03:{OUT}")
    print(ssh("nas03", f"python3 -c {sh(BUILD_PY)} {sh(SRC)} {sh(OUT)}", json.dumps(items)).strip())


ALIVE = f"{{ [ -f ~/{TAG}.pid ] && kill -0 $(cat ~/{TAG}.pid) 2>/dev/null; }}"


def copy(_):
    pub = ssh("nas02", f"[ -f ~/.ssh/{TAG} ] || ssh-keygen -q -t ed25519 -N '' -C {TAG}@nas02 -f ~/.ssh/{TAG}; cat ~/.ssh/{TAG}.pub").strip()
    print(ssh("nas03", f"python3 - {sh(pub)} {sh(OUT)}", r'''
import json, subprocess, sys
key, src = sys.argv[1], sys.argv[2]
u = json.loads(subprocess.check_output(["midclt", "call", "user.query", json.dumps([["username", "=", "truenas_admin"]])]))[0]
keys = [k for k in (u["sshpubkey"] or "").splitlines() if k.strip() and key not in k]
keys.append(f'command="/usr/bin/rrsync -ro {src}",restrict {key}')
subprocess.check_call(["midclt", "call", "user.update", str(u["id"]), json.dumps({"sshpubkey": "\n".join(keys)})], stdout=subprocess.DEVNULL)
print(f"nas03: clave de nas02 limitada a rrsync -ro {src}")
''').strip())
    # pgrep -f no sirve: el propio comando contiene "rsync" y el tag, y se encuentra a sí mismo.
    print(ssh("nas02", f"{ALIVE} && echo 'rsync ya corriendo' || {{ "
              f"nohup rsync -rt --partial -e \"ssh -i $HOME/.ssh/{TAG} -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30\" "
              f"truenas_admin@{N03_TS}:./ {DEST}/ > ~/{TAG}.log 2>&1 < /dev/null & echo $! > ~/{TAG}.pid; echo 'rsync lanzado'; }}").strip())


def status(_):
    print(ssh("nas02", f"{ALIVE} && echo 'rsync corriendo' || echo 'rsync parado'; tail -3 ~/{TAG}.log 2>/dev/null; "
                       f"du -sh {DEST}/comics {DEST}/manga-cbz").strip())


def revoke(_):
    pub = subprocess.run(["ssh", "-o", "BatchMode=yes", "nas02", f"cat ~/.ssh/{TAG}.pub"], capture_output=True, text=True).stdout.strip()
    if pub:
        print(ssh("nas03", f"python3 - {sh(pub)}", r'''
import json, subprocess, sys
u = json.loads(subprocess.check_output(["midclt", "call", "user.query", json.dumps([["username", "=", "truenas_admin"]])]))[0]
keys = [k for k in (u["sshpubkey"] or "").splitlines() if k.strip() and sys.argv[1] not in k]
subprocess.check_call(["midclt", "call", "user.update", str(u["id"]), json.dumps({"sshpubkey": "\n".join(keys)})], stdout=subprocess.DEVNULL)
print(f"nas03: clave de nas02 quitada, quedan {len(keys)}")
''').strip())
    print(ssh("nas02", f"{ALIVE} && echo 'AVISO: el rsync sigue corriendo'; rm -f ~/.ssh/{TAG} ~/.ssh/{TAG}.pub ~/{TAG}.pid; echo 'nas02 limpio'").strip())


def parse(rel):
    """(serie, número, título) de "Serie/Fichero.ext" en una biblioteca de cómics."""
    parts = rel.split("/")
    stem = os.path.splitext(parts[-1])[0]
    series = parts[-2] if len(parts) > 1 and parts[-2] not in GRAB_BAGS else None
    if series:  # "132 - Avengers Vs. X-Men", "3.- Epílogo": el número de orden es para la carpeta, no para la serie
        series = re.sub(r"^\d+\s*\.?-\s*", "", series) or series
    rest = stem
    if series and rest.lower().startswith(series.lower()):
        rest = rest[len(series):]
    # "Sin Goscinny 12. ..." o "Películas 01. ..." dentro de Astérix: subserie con su propia
    # numeración. No si el texto ya es parte del nombre de la serie ("Spiderman 023" en "Spiderman Vol1").
    pre = re.match(r"^\s*([^\d#]*[^\d#\s-])\s+(?=\d)", rest)
    if series and pre and bc.norm(pre.group(1)) not in bc.norm(series) and not re.search(r"cap[ií]tulo|n[ºo°]|vol|tomo|#", pre.group(1), re.I):
        series = f"{series} - {pre.group(1).strip()}"
        rest = rest[pre.end():]
    m = re.match(r"^\s*(?:#|n[ºo°]\.?\s*|vol(?:ume|umen)?\.?\s*|tomo\s*|cap[ií]tulo\s*)?(\d{1,4}(?:\.\d)?)(?!\d)\s*[-.:_)]*\s*(.*)$", rest, re.I) or \
        re.search(r"(?:^|\s|#|n[ºo°]\s*)(\d{1,4})(?:\s*(?:de|of)\s*\d+)?\s*$", rest, re.I)
    idx, title = None, stem
    if m:
        idx = str(int(float(m.group(1)))) if float(m.group(1)).is_integer() else m.group(1)
        tail = m.group(2).strip(" -.") if m.lastindex and m.lastindex >= 2 else ""
        # "18-Tintin - El asunto Tornasol" en Tintín: el título no repite la serie.
        head = tail.split(" - ", 1)
        if series and len(head) == 2 and bc.norm(head[0]) and bc.norm(series).startswith(bc.norm(head[0])):
            tail = head[1]
        title = f"{series} {idx}" + (f". {tail}" if tail else "") if series else stem
    return series, idx, title


def meta(args):
    api = bc.Api() if args.apply else None
    for lib_dir in LIBS.values():
        lib = sql(f"select library_id from library_folders where path='{lib_dir}'")[0]["library_id"]
        if api:
            api("POST", f"/scanner/libraries/{lib}/scan", {})
            while sql(f"select count(*) c from scan_jobs where library_id={lib} and status not in ('completed','failed','cancelled')")[0]["c"] != "0":
                bc.time.sleep(10)
        rows, last = [], 0
        while True:
            chunk = sql(f"""select b.id, f.rel_path, m.title, m.series_name s, m.series_index si from books b
                join book_files f on f.id=b.primary_file_id join book_metadata m on m.book_id=b.id
                where b.library_id={lib} and b.id>{last} order by b.id limit 3000""")
            if not chunk:
                break
            rows += chunk
            last = chunk[-1]["id"]
        changed = 0
        for r in rows:
            series, idx, title = parse(r["rel_path"])
            if (r["title"], r["s"], r["si"]) == (title, series, idx):
                continue
            changed += 1
            if changed <= 8 or not api:
                print(f"  {r['rel_path']}  ->  {series!r} #{idx}  {title!r}")
            if api:
                api("PATCH", f"/books/{r['id']}/metadata-and-locks",
                    {"metadata": {"title": title, "seriesName": series, "seriesIndex": idx},
                     "lockedFields": ["title", "seriesName", "seriesIndex"]})
        log(f"{lib_dir}: {len(rows)} cómics, {changed} {'corregidos' if api else 'por corregir (sin --apply)'}")
    if api:
        api.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("plan", "build", "copy", "status", "revoke"):
        sub.add_parser(c)
    p = sub.add_parser("meta")
    p.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    {"plan": plan, "build": build, "copy": copy, "status": status, "revoke": revoke, "meta": meta}[a.cmd](a)
