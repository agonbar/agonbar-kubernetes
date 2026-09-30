#!/usr/bin/env python3
"""Share the emulator save folders on one Syncthing instance.

Three folders with the same IDs everywhere: RetroDECK saves, RetroDECK states
and Eden's user saves. A client keeps them where the emulators write them. The
always-on hub (the lamg Syncthing pod) keeps them under one directory with
staggered versioning, so a bad save that syncs everywhere can be rolled back,
and so two clients that are rarely on at the same time still meet.
Idempotent: creates missing folders and adds missing peers, never removes.

usage: syncthing-saves-share.py URL APIKEY client:HOME|hub:DIR PEER_ID...
e.g.:  syncthing-saves-share.py http://127.0.0.1:18384 KEY client:/home/deck FFYCA4X-...
"""
import json
import sys
import urllib.error
import urllib.request

FOLDERS = {  # folder id -> path under a client's home
    "retrodeck-saves": "retrodeck/saves",
    "retrodeck-states": "retrodeck/states",
    "eden-saves": ".local/share/eden/nand/user/save",
}
HUB_VERSIONING = {"type": "staggered", "params": {"maxAge": str(365 * 86400), "cleanInterval": "3600"}}


def api(url, key, method, path, body=None):
    req = urllib.request.Request(url + path, method=method, headers={"X-API-Key": key},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req) as r:
            data = r.read()
            return json.loads(data) if data else None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def main():
    url, key, layout, *peers = sys.argv[1:]
    role, root = layout.split(":", 1)
    my_id = api(url, key, "GET", "/rest/system/status")["myID"]
    known = {d["deviceID"] for d in api(url, key, "GET", "/rest/config/devices")}
    missing = [p for p in peers if p not in known]
    if missing:
        sys.exit(f"pair these devices first: {missing}")

    for fid, rel in FOLDERS.items():
        path = f"{root}/{rel}" if role == "client" else f"{root}/{fid}"
        cur = api(url, key, "GET", f"/rest/config/folders/{fid}")
        if cur is None:
            folder = api(url, key, "GET", "/rest/config/defaults/folder")
            folder.update(id=fid, label=fid, path=path, type="sendreceive",
                          devices=[{"deviceID": d} for d in [my_id, *peers]])
            if role == "hub":
                folder["versioning"] = HUB_VERSIONING
            api(url, key, "PUT", f"/rest/config/folders/{fid}", folder)
            print(f"created {fid} at {path}")
            continue
        have = {d["deviceID"] for d in cur["devices"]}
        new = [p for p in peers if p not in have]
        if new:
            api(url, key, "PATCH", f"/rest/config/folders/{fid}",
                {"devices": cur["devices"] + [{"deviceID": d} for d in new]})
        print(f"exists  {fid} at {cur['path']}, added peers: {[d[:7] for d in new] or 'none'}")


if __name__ == "__main__":
    main()
