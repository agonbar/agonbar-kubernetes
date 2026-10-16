#!/usr/bin/env bash
# Call Kodi's JSON-RPC on the Steam Deck (TCP 9090, localhost only, so over SSH).
#
#   scripts/kodi-rpc.sh Addons.GetAddons '{"type":"xbmc.addon.repository"}'
#   KODI_HOST=deck@192.168.1.236 scripts/kodi-rpc.sh GUI.GetProperties '{"properties":["currentwindow"]}'
#
# Builtins (InstallAddon(...), etc.) aren't in JSON-RPC; send those with
#   ssh deck@... flatpak run --command=kodi-send tv.kodi.Kodi --action='InstallAddon(x)'
set -euo pipefail
host=${KODI_HOST:-deck@192.168.1.218}
method=$1 params=${2:-"{}"}
# ssh joins argv into one remote shell line, so quote for that shell.
ssh -i ~/.ssh/nas -o BatchMode=yes "$host" "python3 - $(printf %q "$method") $(printf %q "$params")" <<'EOF'
import json, socket, sys
req = {"jsonrpc": "2.0", "id": 1, "method": sys.argv[1], "params": json.loads(sys.argv[2])}
s = socket.create_connection(("127.0.0.1", 9090), timeout=30)
s.sendall(json.dumps(req).encode())
dec, buf = json.JSONDecoder(), ""
while True:
    buf += s.recv(65536).decode()
    # Kodi pushes notifications on the same socket; skip them until our reply.
    while buf.strip():
        try:
            msg, end = dec.raw_decode(buf.lstrip())
        except ValueError:
            break
        buf = buf.lstrip()[end:]
        if msg.get("id") == 1:
            print(json.dumps(msg.get("result", msg.get("error")), indent=1, ensure_ascii=False))
            sys.exit(0)
EOF
