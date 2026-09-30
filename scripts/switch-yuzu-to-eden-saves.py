#!/usr/bin/env python3
"""Merge yuzu Switch saves into the Eden profile.

yuzu kept saves under several profile UUIDs: the ones in its profiles.dat plus
orphans left by earlier installs. Eden only shows its own profile, so for each
title copy the most recently written yuzu save into that profile. Titles Eden
already has are left alone, and so are the losing yuzu copies (keep a backup).
Dry run unless --apply.

usage: switch-yuzu-to-eden-saves.py YUZU_NAND EDEN_NAND [--apply]
e.g.:  ssh deck@<steamdeck> python3 - YUZU_NAND EDEN_NAND < this-script
"""
import os
import shutil
import struct
import sys
import time

DEVICE = "0" * 32  # device saves, not tied to a profile
PROFILES_DAT = "system/save/8000000000000010/su/avators/profiles.dat"


def profiles(nand):
    """Save-dir names of the profiles in profiles.dat, in slot order."""
    data = open(os.path.join(nand, PROFILES_DAT), "rb").read()
    out = []
    for i in range(8):
        uuid = data[0x10 + i * 0xC8:0x10 + i * 0xC8 + 16]
        if uuid != bytes(16):
            lo, hi = struct.unpack("<QQ", uuid)
            out.append(f"{hi:016X}{lo:016X}")
    return out


def newest(path):
    """(mtime of the newest file, file count) under path."""
    files = [os.path.join(d, f) for d, _, fs in os.walk(path) for f in fs]
    return max((os.path.getmtime(f) for f in files), default=0), len(files)


def main():
    args = [a for a in sys.argv[1:] if a != "--apply"]
    apply = "--apply" in sys.argv
    yuzu_nand, eden_nand = args
    src_root = os.path.join(yuzu_nand, "user/save/0000000000000000")
    dst_root = os.path.join(eden_nand, "user/save/0000000000000000")
    (eden_profile,) = profiles(eden_nand)

    best = {}  # title id -> (mtime, count, yuzu profile)
    for uid in sorted(os.listdir(src_root)):
        if uid == DEVICE:
            continue
        for tid in os.listdir(os.path.join(src_root, uid)):
            mtime, count = newest(os.path.join(src_root, uid, tid))
            if count and (mtime, count) > best.get(tid, (0, 0))[:2]:
                best[tid] = (mtime, count, uid)

    plan = [(os.path.join(src_root, uid, tid), os.path.join(dst_root, eden_profile, tid))
            for tid, (_, _, uid) in sorted(best.items())]
    device_src = os.path.join(src_root, DEVICE)
    if os.path.isdir(device_src):
        plan += [(os.path.join(device_src, tid), os.path.join(dst_root, DEVICE, tid))
                 for tid in sorted(os.listdir(device_src))]

    for src, dst in plan:
        if os.path.exists(dst):
            print(f"skip   {os.path.relpath(dst, dst_root)} (Eden already has it)")
            continue
        mtime, count = newest(src)
        print(f"copy   {os.path.relpath(src, src_root)} -> {os.path.relpath(dst, dst_root)} "
              f"({count} files, {time.strftime('%Y-%m-%d', time.localtime(mtime))})")
        if apply:
            shutil.copytree(src, dst, copy_function=shutil.copy2)
    if not apply:
        print("dry run, pass --apply to copy")


if __name__ == "__main__":
    main()
