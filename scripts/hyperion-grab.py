#!/usr/bin/env python3
"""Ambilight from Bazzite's Game Mode: grab gamescope's PipeWire stream, shrink it
and push it to Hyperion (ns aya, kube-vip-aya VIP) as raw RGB over JSON-RPC.

UNSAFE, DO NOT INSTALL. Disconnecting from gamescope's PipeWire stream crashes
gamescope (SEGV in libpipewire-module-client-node clear_buffers), and Game Mode
crash-loops into Desktop Mode. pipewiresrc gives no control over teardown order.
The working path is gamescope-led-sync with a patch that deactivates the stream
before disconnecting, see architecture/gamescope-pipewire-ambilight-capture.md
in the vault. `install` refuses unless --i-know-it-crashes is passed.

  python3 hyperion-grab.py install   # on Bazzite: copy to ~/.local/bin, enable+restart the user unit
  hyperion-grab.py                   # what the unit runs: waits for gamescope, streams while it lives
  ffmpeg -loglevel error -re -f lavfi -i testsrc2=size=64x36:rate=25 \\
    -f rawvideo -pix_fmt rgb24 - | ./hyperion-grab.py --stdin
                                     # test the Hyperion side from anywhere on the aya LAN

gamescope publishes its composited output as a PipeWire node named "gamescope".
Desktop Mode has no such node, so the unit idles there. Everything used ships in
the Bazzite image (gst-launch-1.0, the pipewiresrc plugin, pw-dump), nothing to layer.

Hyperion answers unauthenticated JSON only from its local network: this works from
192.168.1.0/24 against the VIP, and gets "No Authorization" over the tailnet.

Images go in at priority 150, behind HA's light.tele (128), so a colour set by hand
wins over the game. Each image lives DURATION_MS; pipewiresrc re-sends the last
frame every second on a static screen, and when the stream stops the LEDs fall
back to whatever is below.
"""
import argparse
import base64
import json
import os
import select
import shutil
import socket
import subprocess
import sys
import time

HOST = os.environ.get("HYPERION_HOST", "192.168.1.26")
PORT = int(os.environ.get("HYPERION_PORT", "19444"))
W, H, FPS = 64, 36, 25
PRIORITY = 150
DURATION_MS = 3000
ORIGIN = "bazzite"
FRAME = W * H * 3

PIPELINE = (
    "pipewiresrc target-object=gamescope keepalive-time=1000 always-copy=true"
    f" ! videorate drop-only=true ! video/x-raw,framerate={FPS}/1"
    f" ! videoconvertscale ! video/x-raw,format=RGB,width={W},height={H}"
    " ! fdsink fd=1"
)

UNIT = """[Unit]
Description=Ambilight: Game Mode screen to Hyperion
After=pipewire.service

[Service]
ExecStart=%h/.local/bin/hyperion-grab.py
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
"""


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def gamescope_up():
    out = subprocess.run(["pw-dump"], capture_output=True, text=True).stdout
    return any(
        o.get("type") == "PipeWire:Interface:Node"
        and o.get("info", {}).get("props", {}).get("node.name") == "gamescope"
        for o in json.loads(out or "[]")
    )


def drain(sock, buf):
    # Hyperion answers every command. Unread replies would pile up in its send
    # buffer, so read them, and surface the failures (auth, bad image size).
    while select.select([sock], [], [], 0)[0]:
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("hyperion closed the connection")
        buf += chunk
    *lines, buf = buf.split(b"\n")
    for line in lines:
        reply = json.loads(line or b"{}")
        if reply.get("success") is False:
            log("hyperion:", reply.get("error"))
    return buf


def send(stream):
    sock, buf, sent = None, b"", 0
    while True:
        frame = stream.read(FRAME)
        if len(frame) < FRAME:
            return
        msg = {
            "command": "image",
            "priority": PRIORITY,
            "origin": ORIGIN,
            "duration": DURATION_MS,
            "imagewidth": W,
            "imageheight": H,
            "imagedata": base64.b64encode(frame).decode(),
        }
        try:
            if sock is None:
                sock = socket.create_connection((HOST, PORT), timeout=5)
                buf = b""
                log(f"connected to {HOST}:{PORT}")
            sock.sendall(json.dumps(msg).encode() + b"\n")
            buf = drain(sock, buf)
        except (OSError, ConnectionError) as e:
            log("send failed, dropping frame:", e)
            sock = None
            continue
        sent += 1
        if sent % (FPS * 60) == 0:
            log(f"{sent} frames sent")


def install():
    bin_path = os.path.expanduser("~/.local/bin/hyperion-grab.py")
    unit_path = os.path.expanduser("~/.config/systemd/user/hyperion-grab.service")
    os.makedirs(os.path.dirname(bin_path), exist_ok=True)
    os.makedirs(os.path.dirname(unit_path), exist_ok=True)
    if os.path.abspath(__file__) != bin_path:
        shutil.copy(__file__, bin_path)
    os.chmod(bin_path, 0o755)
    with open(unit_path, "w") as f:
        f.write(UNIT)
    for cmd in (["daemon-reload"], ["enable", "hyperion-grab.service"], ["restart", "hyperion-grab.service"]):
        subprocess.run(["systemctl", "--user", *cmd], check=True)
    subprocess.run(["systemctl", "--user", "--no-pager", "status", "hyperion-grab.service"])


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", nargs="?", choices=["install"])
    p.add_argument("--stdin", action="store_true", help=f"read raw RGB {W}x{H} frames from stdin")
    p.add_argument("--i-know-it-crashes", action="store_true", help="allow install despite the gamescope crash")
    args = p.parse_args()
    if args.cmd == "install":
        if not args.i_know_it_crashes:
            sys.exit("refusing to install: stopping this grabber crashes gamescope (see docstring)")
        return install()
    if args.stdin:
        return send(sys.stdin.buffer)
    waiting = False
    while True:
        if not gamescope_up():
            if not waiting:
                log("no gamescope PipeWire node (Desktop Mode?), waiting")
                waiting = True
            time.sleep(5)
            continue
        waiting = False
        log("gamescope node found, streaming")
        gst = subprocess.Popen(["gst-launch-1.0", "-q", *PIPELINE.split()], stdout=subprocess.PIPE)
        send(gst.stdout)
        gst.kill()
        gst.wait()
        log(f"gst-launch exited ({gst.returncode})")
        time.sleep(2)


if __name__ == "__main__":
    main()
