#!/usr/bin/env python3
"""The RTX 3090's LEDs follow GPU load on Bazzite (VM 104).

Idle, the card breathes slowly in purple. As the load climbs it stops
breathing and slides through magenta into orange, so a game at full tilt is a
steady bright orange. The load is smoothed over ~1.5 s, so it flows instead of
flickering with every frame.

Every I2C write busy-waits in the NVIDIA driver, so animating from here cost
~8 % of a core at 45 writes/s. The breathing is the chip's own mode instead,
and under load the colour is only rewritten when it visibly changes.

The card is an MSI RTX 3090 Gaming X Trio: its RGB chip sits at 0x68 on the
NVIDIA driver's I2C port 1, and the protocol is OpenRGB's MSIGPUController,
plain SMBus byte writes. The card is found by its PCI ids, nothing is written
anywhere else, and register 0x3F (save to the card's EEPROM) is never touched.
Under the encoder def (VM 111) the same card is driven by work-vm-00's
console-panel instead (dotfiles pkgs/console-panel/leds.go).

    gpu-leds              run (what the unit does)
    gpu-leds demo         sweep 0 -> 100 % -> 0 over 20 s, to judge the palette
    gpu-leds install      copy to /usr/local/bin, install the unit, start it

Install from work-vm-00:
    scp -i ~/.ssh/nas scripts/bazzite-gpu-leds.py bazzite@bazzite.nb.senseizero.lan:/tmp/
    ssh -i ~/.ssh/nas bazzite@bazzite.nb.senseizero.lan sudo python3 /tmp/bazzite-gpu-leds.py install
"""

import fcntl
import glob
import math
import os
import shutil
import subprocess
import sys
import threading
import time

ADDR = 0x68
I2C_SLAVE = 0x0703
HZ = 4
SMOOTH_S = 1.5
STEP = 6          # rewrite a colour only when a channel moves more than this
IDLE = (0.12, 0.18)  # breathe below the first load, stop above the second
BREATHING, STATIC, SLOW = 0x04, 0x13, 0x04
# purple at idle, magenta at half load, orange at full.
PALETTE = [(0.0, (90, 0, 255)), (0.5, (255, 0, 150)), (1.0, (255, 70, 0))]
UNIT = """[Unit]
Description=RTX 3090 LEDs follow GPU load
After=multi-user.target

[Service]
ExecStart=/usr/local/bin/gpu-leds
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""


def find_bus():
    for a in glob.glob("/sys/bus/i2c/devices/i2c-*"):
        with open(a + "/name") as f:
            if not f.read().startswith("NVIDIA i2c adapter 1 at "):
                continue
        pci = os.path.realpath(a + "/..")
        ids = []
        for n in ("vendor", "device", "subsystem_vendor", "subsystem_device"):
            with open(os.path.join(pci, n)) as f:
                ids.append(f.read().strip())
        if ids == ["0x10de", "0x2204", "0x1462", "0x3884"]:
            return "/dev/" + os.path.basename(a)
    sys.exit("no MSI RTX 3090 Gaming X Trio I2C port")


class Card:
    def __init__(self):
        self.fd = os.open(find_bus(), os.O_RDWR)
        fcntl.ioctl(self.fd, I2C_SLAVE, ADDR)
        self.write(0x36, 100)  # brightness
        self.write(0x38, SLOW)  # effect speed, for breathing
        self.write(0x26, 0)
        self.mode, self.rgb = None, None

    def write(self, reg, val):
        os.write(self.fd, bytes((reg, val)))

    def show(self, mode, rgb):
        if mode != self.mode:
            self.write(0x22, mode)
            self.mode = mode
        if self.rgb is None or max(abs(a - b) for a, b in zip(rgb, self.rgb)) > STEP:
            for reg, v in zip((0x30, 0x31, 0x32), rgb):
                self.write(reg, v)
            self.rgb = rgb


def palette(u):
    for (t0, c0), (t1, c1) in zip(PALETTE, PALETTE[1:]):
        if u <= t1:
            k = (u - t0) / (t1 - t0)
            return tuple(round(a + (b - a) * k) for a, b in zip(c0, c1))
    return PALETTE[-1][1]


def watch_load(state):
    p = subprocess.Popen(
        ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits", "-lms", "250"],
        stdout=subprocess.PIPE, text=True)
    for line in p.stdout:
        try:
            state["load"] = min(max(float(line) / 100, 0), 1)
        except ValueError:
            pass
    print("nvidia-smi exited", file=sys.stderr)
    os._exit(1)  # sys.exit would only end this thread; let systemd restart us


def run(target):
    card, u, idle = Card(), 0.0, True
    alpha = 1 - math.exp(-1 / (HZ * SMOOTH_S))
    while True:
        u += (target() - u) * alpha
        idle = u < IDLE[1] if idle else u < IDLE[0]
        card.show(BREATHING if idle else STATIC, palette(u))
        time.sleep(1 / HZ)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "install":
        shutil.copy(sys.argv[0], "/usr/local/bin/gpu-leds")
        os.chmod("/usr/local/bin/gpu-leds", 0o755)
        with open("/etc/systemd/system/gpu-leds.service", "w") as f:
            f.write(UNIT)
        subprocess.run(["restorecon", "/usr/local/bin/gpu-leds", "/etc/systemd/system/gpu-leds.service"], check=True)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", "--now", "gpu-leds"], check=True)
    elif cmd == "demo":
        t0 = time.monotonic()
        threading.Timer(20, os._exit, (0,)).start()
        run(lambda: 1 - abs((time.monotonic() - t0) / 10 - 1))
    elif cmd == "run":
        state = {"load": 0.0}
        threading.Thread(target=watch_load, args=(state,), daemon=True).start()
        run(lambda: state["load"])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
