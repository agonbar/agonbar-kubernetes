#!/usr/bin/env python3
"""Point aya/hyperion at the WLED strip behind the TV and give it the LED layout.
Hyperion keeps its config in a SQLite db on NFS, not in git; this is the git copy.

  ./hyperion-aya-config.py pattern            # calibration pattern on the strip, ~15 min
  ./hyperion-aya-config.py apply --start bl --dir cw
                                              # write device + layout into Hyperion

The strip is a WLED 0.11.1 on an ESP8266, 226 LEDs. Its single pings get lost to
WiFi power save, so a one-shot ping sweep misses it; scan with `ping -c3`.

pattern: sends WLED's own UDP realtime protocol (DRGB, port 21324) with a 120 s
timeout and resends it every 60 s, so the WLED falls back to its own state by itself
when this stops. Colours by strip index, for the default 73/40/73/40 split: 0-72 red,
73-112 green, 113-185 blue, 186-225 purple, and the first 3 LEDs white. Look at the
TV and note where the white LEDs are and which side each colour covers.

apply: --start is the corner where LED 0 sits, --dir the way the strip runs, both
seen from the front of the TV. --shift moves the start N LEDs further along the
strip if it does not begin exactly at a corner. The layout comes out the way the
web UI's "classic" generator builds it (clockwise from top-left, rotated by
`position`, then optionally reversed), and ledConfig.classic is written to match,
so opening the LED page in the UI does not change it.

Login: the admin password is Hyperion's default unless HYPERION_PASSWORD says
otherwise. The image ignores the WEBPASSWORD env var in the manifest.
"""
import argparse
import json
import os
import socket
import time

HYPERION = ("192.168.1.26", 19444)
WLED_HOST = "192.168.1.210"
WLED_UDP = 21324
DEPTH_H, DEPTH_V = 0.08, 0.05  # web UI defaults: top/bottom 8 %, left/right 5 %


def classic_layout(top, right, bottom, left, position, reverse):
    leds = []
    for i in range(top):  # left to right
        leds.append((i / top, (i + 1) / top, 0, DEPTH_H))
    for i in range(right):  # top to bottom
        leds.append((1 - DEPTH_V, 1, i / right, (i + 1) / right))
    for i in reversed(range(bottom)):  # right to left
        leds.append((i / bottom, (i + 1) / bottom, 1 - DEPTH_H, 1))
    for i in reversed(range(left)):  # bottom to top
        leds.append((0, DEPTH_V, i / left, (i + 1) / left))
    leds = leds[position:] + leds[:position]
    if reverse:
        leds.reverse()
    return [{"hmin": round(a, 4), "hmax": round(b, 4), "vmin": round(c, 4), "vmax": round(d, 4)} for a, b, c, d in leds]


def pattern(args):
    sides = [(args.top, (255, 0, 0)), (args.right, (0, 255, 0)), (args.bottom, (0, 0, 255)), (args.left, (160, 0, 255))]
    rgb = [c for n, c in sides for _ in range(n)]
    rgb[:3] = [(255, 255, 255)] * 3
    pkt = bytes([2, 120]) + bytes(v for c in rgb for v in c)  # DRGB, 120 s timeout
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    end = time.time() + args.minutes * 60
    while time.time() < end:
        sock.sendto(pkt, (WLED_HOST, WLED_UDP))
        time.sleep(60)
    print("pattern stopped; the WLED returns to its own state within 120 s")


def apply(args):
    corner = {"tl": 0, "tr": args.top, "br": args.top + args.right, "bl": args.top + args.right + args.bottom}[args.start]
    count = args.top + args.right + args.bottom + args.left
    position = (corner + args.shift) % count
    reverse = args.dir == "ccw"
    settings = {
        "device": {
            "type": "wled",
            "host": WLED_HOST,
            "hardwareLedCount": count,
            "colorOrder": "rgb",
            "streamProtocol": "DDP",
            # Hand the strip back to its own preset (warm orange) when Hyperion stops.
            "restoreOriginalState": True,
            "stayOnAfterStreaming": False,
            "overwriteSync": True,
            # Keep the WLED's own brightness: no power limiter is set on it, and
            # a white screen at 255 asks 226 LEDs for ~13 A.
            "overwriteBrightness": False,
            "brightness": 255,
            "latchTime": 0,
            "autoStart": True,
            "enableAttempts": 6,
            "enableAttemptsInterval": 15,
        },
        "leds": classic_layout(args.top, args.right, args.bottom, args.left, position, reverse),
        "ledConfig": {
            "classic": {
                "top": args.top, "right": args.right, "bottom": args.bottom, "left": args.left,
                "position": position, "reverse": reverse,
                "hdepth": round(DEPTH_H * 100), "vdepth": round(DEPTH_V * 100),
                "glength": 0, "gpos": 0, "overlap": 0, "edgegap": 0,
                "ptlh": 0, "ptlv": 0, "ptrh": 100, "ptrv": 0,
                "pblh": 0, "pblv": 100, "pbrh": 100, "pbrv": 100,
            },
            "matrix": {"cabling": "snake", "direction": "horizontal", "ledshoriz": 1, "ledsvert": 1, "start": "top-left"},
        },
    }
    sock = socket.create_connection(HYPERION, timeout=10)
    replies = sock.makefile("rb")

    def rpc(msg):
        sock.sendall(json.dumps(msg).encode() + b"\n")
        reply = json.loads(replies.readline())
        if not reply.get("success"):
            raise SystemExit(f"{msg['command']}/{msg.get('subcommand')}: {reply.get('error')} {reply.get('errorData') or ''}")
        return reply

    rpc({"command": "authorize", "subcommand": "login", "password": os.environ.get("HYPERION_PASSWORD", "hyperion")})
    rpc({"command": "config", "subcommand": "setconfig", "config": {"instances": [{"id": 0, "settings": settings}]}})
    got = rpc({"command": "config", "subcommand": "getconfig"})["info"]["instances"][0]["settings"]
    ok = got["device"]["type"] == "wled" and got["leds"] == settings["leds"]
    print(f"{'applied' if ok else 'NOT applied'}: {count} LEDs, start {args.start} {args.dir}, shift {args.shift} (position {position}, reverse {reverse})")
    if not ok:
        raise SystemExit(1)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=["pattern", "apply"])
    p.add_argument("--top", type=int, default=73)
    p.add_argument("--right", type=int, default=40)
    p.add_argument("--bottom", type=int, default=73)
    p.add_argument("--left", type=int, default=40)
    p.add_argument("--start", choices=["tl", "tr", "br", "bl"], default="bl")
    p.add_argument("--dir", choices=["cw", "ccw"], default="cw")
    p.add_argument("--shift", type=int, default=0)
    p.add_argument("--minutes", type=int, default=15)
    args = p.parse_args()
    pattern(args) if args.cmd == "pattern" else apply(args)


if __name__ == "__main__":
    main()
