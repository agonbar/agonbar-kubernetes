#!/usr/bin/env python3
"""Import the Switch saves from EmuDeck's yuzu NAND into RomM.

EmuDeck keeps Switch saves in the emulated NAND, not in the per-emulator
`saves/` tree, so they are easy to miss:

    Emudeck/storage/yuzu/nand/user/save/0000000000000000/<profile>/<title_id>/

Each save is a directory, so this packs one zip per (profile, title_id) and
uploads it to the matching ROM. Profiles holding byte-identical data for the
same title are collapsed into a single upload.

Idempotent: RomM rejects a save whose filename already exists for the ROM
unless `overwrite` is set, and this never sets it.
"""

import argparse
import base64
import hashlib
import io
import json
import re
import subprocess
import sys
import time
import tarfile
import urllib.error
import urllib.request
import uuid
import zipfile

SYNCTHING = ("-n", "lamg", "deploy/syncthing")
ROMM = ("-n", "piracy", "deploy/romm")
NAND = "/config/Emudeck/storage/yuzu/nand/user/save/0000000000000000"
ROMM_URL = "https://romm.adriangonzalezbarbosa.eu"
PLATFORM = "switch"

# Super Mario RPG's release carries no title ID in its filename, so the
# library cannot be matched against it by name.
EXTRA_TITLE_IDS = {"0100BC0018138000": "Super Mario RPG"}

# Decoded from yuzu's nand/system/save/8000000000000010/su/avators/profiles.dat.
# The rest are orphaned accounts from earlier installs: their saves survive but
# their profile entries do not, so they stay labelled by hash.
PROFILE_NAMES = {
    "F87053BD": "perfil panda",
    "087F3D31": "perfil agonbar",
    "00000000": "sin perfil",  # yuzu's fallback account
}


def kexec(target, script, attempts=4):
    """Run a command in a pod, retrying the flaky WAN link to the apiserver."""
    cmd = ["kubectl", "--context", "lamg", "exec", *target, "--", "sh", "-c", script]
    for attempt in range(attempts):
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if out.returncode == 0:
            return out.stdout
        if attempt == attempts - 1:
            raise RuntimeError(f"kubectl exec fallo tras {attempts} intentos: {script[:80]}")
        time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


def manifest():
    """(profile, title_id, file count, content hash) for every non-empty save."""
    script = f"""
    cd {NAND} || exit 1
    for d in */*/; do
      d=${{d%/}}
      n=$(find "$d" -type f | wc -l)
      [ "$n" -eq 0 ] && continue
      h=$(find "$d" -type f | sort | xargs -d '\\n' sha256sum | sha256sum | cut -c1-16)
      echo "$d|$n|$h"
    done
    """
    rows = []
    for line in kexec(SYNCTHING, script).decode().splitlines():
        path, count, digest = line.strip().split("|")
        profile, title_id = path.split("/")
        rows.append((profile, title_id.upper(), int(count), digest))
    return rows


def mint_token():
    """Sign a short-lived bearer token with RomM's own auth key, inside the pod."""
    script = (
        "python3 -c \"import json;"
        "from datetime import timedelta;"
        "from handler.auth.base_handler import OAuthHandler;"
        "from handler.auth.constants import FULL_SCOPES;"
        "from handler.database import db_user_handler;"
        "u=[x for x in db_user_handler.get_users() if x.enabled][0];"
        "print(json.dumps({'user':u.username,'token':OAuthHandler().create_access_token("
        "{'sub':u.username,'iss':'romm:oauth','scopes':' '.join(FULL_SCOPES)},"
        "timedelta(minutes=45))}))\""
    )
    out = kexec(ROMM, script).decode().strip().splitlines()[-1]
    return json.loads(out)


def api(token, path):
    req = urllib.request.Request(f"{ROMM_URL}{path}", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def rom_index(token):
    """title_id (first 12 hex, the game key) -> (rom_id, display name).

    The title ID lives in the NSP/XCI filename inside each game folder, but
    RomM reports the folder as the ROM's fs_name, so the two are joined here.
    """
    listing = kexec(
        ROMM,
        "cd /romm/library/roms/switch && for d in */; do "
        "f=$(find \"$d\" -maxdepth 2 -type f -name '*.ns[pz]' -o -maxdepth 2 -type f -name '*.xc[iz]' | head -1); "
        "echo \"${d%/}|$(basename \"$f\")\"; done",
    ).decode()
    folder_title = {}
    for line in listing.splitlines():
        folder, fname = line.strip().split("|", 1)
        found = re.findall(r"([0-9A-Fa-f]{16})", fname)
        if found:
            folder_title[folder] = found[0].upper()
        else:
            for tid, label in EXTRA_TITLE_IDS.items():
                if label.lower() == folder.lower():
                    folder_title[folder] = tid

    platforms = api(token, "/api/platforms")
    pid = next(p["id"] for p in platforms if p["fs_slug"] == PLATFORM)
    roms = api(token, f"/api/roms?platform_ids={pid}&limit=500")["items"]

    index = {}
    for rom in roms:
        tid = folder_title.get(rom.get("fs_name") or "")
        if tid:
            index[tid[:12]] = (rom["id"], rom["name"])
    return index


CHUNK_MB = 2


def fetch_tar(profile, title_id):
    """Pull one save directory out of the pod as a tar.gz.

    kubectl exec's websocket drops large streams, so the tar is staged in the
    pod and read back in base64 chunks. Anything over ~20 MB fails without it.
    """
    remote = "/tmp/romm-save.tgz"
    size = int(
        kexec(
            SYNCTHING,
            f"cd {NAND}/.. && tar cz '0000000000000000/{profile}/{title_id}' > {remote}"
            f" && wc -c < {remote}",
        ).decode().strip()
    )
    parts = []
    for skip in range(0, (size // (CHUNK_MB << 20)) + 1):
        encoded = kexec(
            SYNCTHING,
            f"dd if={remote} bs={CHUNK_MB << 20} skip={skip} count=1 2>/dev/null | base64",
        )
        parts.append(base64.b64decode(b"".join(encoded.split())))
    kexec(SYNCTHING, f"rm -f {remote}")

    raw = b"".join(parts)
    if len(raw) != size:
        raise RuntimeError(f"{profile}/{title_id}: {len(raw)} bytes recibidos de {size}")
    return raw


def to_zip(raw):
    """Repack the tar as a zip, keeping the NAND-relative path.

    Restoring is then `unzip` into `nand/user/save/`.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar, \
            zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for member in tar:
            if not member.isfile():
                continue
            fh = tar.extractfile(member)
            if fh:
                zf.writestr(member.name, fh.read())
    return buf.getvalue()


def upload(token, rom_id, filename, payload):
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="saveFile"; filename="{filename}"\r\n'
        "Content-Type: application/zip\r\n\r\n"
    ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{ROMM_URL}/api/saves?rom_id={rom_id}&emulator=yuzu",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)


def purge(token):
    """Drop the yuzu-emulator saves, the only ones this script creates."""
    pid = next(p["id"] for p in api(token, "/api/platforms") if p["fs_slug"] == PLATFORM)
    ids = [s["id"] for s in api(token, f"/api/saves?platform_id={pid}") if s["emulator"] == "yuzu"]
    if not ids:
        return
    req = urllib.request.Request(
        f"{ROMM_URL}/api/saves/delete",
        data=json.dumps({"saves": ids}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120):
        print(f"borrados {len(ids)} saves de una ejecucion anterior", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--replace", action="store_true",
                    help="delete the saves a previous run uploaded before starting")
    args = ap.parse_args()

    session = mint_token()
    token = session["token"]
    print(f"autenticado como {session['user']}", file=sys.stderr)

    index = rom_index(token)
    if args.replace and not args.dry_run:
        purge(token)
    rows = manifest()

    seen = set()
    plan, orphans = [], []
    for profile, title_id, count, digest in sorted(rows):
        hit = index.get(title_id[:12])
        if not hit:
            orphans.append((title_id, profile, count))
            continue
        if (title_id, digest) in seen:
            continue
        seen.add((title_id, digest))
        rom_id, name = hit
        plan.append((rom_id, name, profile, title_id, count))

    for rom_id, name, profile, title_id, count in plan:
        label = PROFILE_NAMES.get(profile[:8], f"perfil {profile[:8]}")
        filename = f"{title_id} [{label}].zip"
        if args.dry_run:
            print(f"[dry] rom {rom_id:>3} {name} <- {filename} ({count} ficheros)")
            continue
        payload = to_zip(fetch_tar(profile, title_id))
        try:
            upload(token, rom_id, filename, payload)
            print(f"OK   rom {rom_id:>3} {name} <- {filename} ({len(payload)/1e6:.1f} MB)")
        except urllib.error.HTTPError as exc:
            print(f"FALLO rom {rom_id} {filename}: {exc.code} {exc.read()[:200]!r}")

    for title_id, profile, count in orphans:
        print(f"SIN JUEGO EN LA BIBLIOTECA: {title_id} (perfil {profile[:8]}, {count} ficheros)")


if __name__ == "__main__":
    main()
