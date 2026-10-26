#!/usr/bin/env python3
"""Search an rbw vault even when one entry fails to decrypt.

rbw 1.15 aborts `search`/`get`/`list` with "invalid mac" if any single entry
can't be decrypted. This talks to the running rbw-agent directly and decrypts
entry by entry, skipping the broken ones.

    RBW_PROFILE=personal rbw-safe-search.py search xiaomi      # id, name, username
    RBW_PROFILE=personal rbw-safe-search.py get <id> password   # value on stdout

The agent must be unlocked (`rbw unlock`). Passwords are only printed by `get`.
"""
import glob, json, os, re, socket, sys

profile = os.environ.get("RBW_PROFILE")
suffix = f"-{profile}" if profile else ""
runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
SOCK = f"{runtime}/rbw{suffix}/socket"
CACHE = glob.glob(os.path.expanduser(f"~/.cache/rbw{suffix}/*.json"))[0]


def decrypt(sock, cipherstring, entry):
    if not cipherstring:
        return None
    req = {"tty": None, "environment": {"tty": None, "env_vars": []},
           "action": {"type": "Decrypt", "cipherstring": cipherstring,
                      "entry_key": entry.get("key"), "org_id": entry.get("org_id")}}
    sock.sendall(json.dumps(req).encode() + b"\n")
    resp = json.loads(sock.makefile().readline())
    if resp.get("type") != "Decrypt":
        raise ValueError(resp.get("error"))
    return resp["plaintext"]


def connect():
    s = socket.socket(socket.AF_UNIX)
    s.connect(SOCK)
    return s


def main():
    cmd, arg = sys.argv[1], sys.argv[2]
    entries = json.load(open(CACHE))["entries"]
    if cmd == "search":
        pat, broken = re.compile(arg, re.I), 0
        for e in entries:
            login = (e.get("data") or {}).get("Login") or {}
            try:
                with connect() as s:
                    name = decrypt(s, e["name"], e)
                with connect() as s:
                    user = decrypt(s, login.get("username"), e)
                uris = []
                for u in login.get("uris") or []:
                    with connect() as s:
                        uris.append(decrypt(s, u.get("uri"), e) or "")
            except Exception:
                broken += 1
                continue
            if any(pat.search(x or "") for x in [name, user, *uris]):
                print(e["id"], "|", name, "|", user, "|", " ".join(uris))
        print(f"({broken} entries could not be decrypted)", file=sys.stderr)
    elif cmd == "get":
        field = sys.argv[3]
        e = next(x for x in entries if x["id"] == arg)
        with connect() as s:
            print(decrypt(s, e["data"]["Login"][field], e))


main()
