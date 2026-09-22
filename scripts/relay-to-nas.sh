#!/usr/bin/env bash
# Copy a directory into nas02 from a machine that cannot write there itself,
# by piping it through this one.
#
# Written for the t480, which sits on another network (192.168.1.x) and only
# reaches nas02 over a Tailscale DERP relay, while its sshd refuses agent
# forwarding. Piping through here needs no key on the source machine and no
# private key copied anywhere. Measured 2026-09-21: t480 -> here 11 MB/s,
# here -> nas02 8.5 MB/s.
#
# Lands under a directory truenas_admin can write (/mnt/RAID/docker is 777).
# Library directories such as romm-library are root:root 755, so placing files
# there is a second step done from a pod.
#
# Verifies by comparing every file's logical size at both ends, never du: ZFS
# compression makes du report a fraction of the real size on the NAS side.
# Re-running is the retry: an item that verifies is skipped, and a partial one
# resumes with only the files still missing or short.
#
# Usage: relay-to-nas.sh <src-host> <src-parent-dir> <item> <nas-dest-dir>
#   relay-to-nas.sh t480 /mnt/nas "ONE PIECE ODYSSEY" /mnt/RAID/docker/game-staging
#
# To watch it from the source machine, copy scripts/relay-progress.sh there and
# run `watch -n 2 ./relay-progress.sh <GB>`, using the GB this script prints.
set -euo pipefail
# Byte order everywhere. The source, the NAS and this machine each sort by
# their own locale (the t480 is es_ES), so names like "_crack" and "Engine"
# came back in different orders: comm refused the input and the equality
# check could never have matched either.
export LC_ALL=C

SRC_HOST=$1 SRC_DIR=$2 ITEM=$3 DEST=$4
NAS=truenas_admin@100.72.0.41   # Tailscale IP; `Host nas02` resolves to NetBird v6
NAS_SSH=(ssh -o BatchMode=yes -i "$HOME/.ssh/nas" "$NAS")

listing_src() { ssh -o BatchMode=yes "$SRC_HOST" "cd '$SRC_DIR' && find '$ITEM' -type f -printf '%s %p\n' | LC_ALL=C sort"; }
listing_dst() { "${NAS_SSH[@]}" "cd '$DEST' 2>/dev/null && find '$ITEM' -type f -printf '%s %p\n' 2>/dev/null | LC_ALL=C sort"; }

src=$(listing_src)
[ -n "$src" ] || { echo "nothing found at $SRC_HOST:$SRC_DIR/$ITEM" >&2; exit 1; }

if [ "$src" = "$(listing_dst)" ]; then
  echo "already there and verified: $ITEM"
  exit 0
fi

# Only the files whose size does not already match at the destination. A
# transfer that dies halfway then resumes file by file instead of starting the
# whole item over — which matters because the t480's USB enclosure dropped off
# the bus on its own once already (2026-09-22 00:03). A half-written file has
# the wrong size, so it lands in this list and gets rewritten whole.
todo=$(comm --check-order -23 <(echo "$src") <(listing_dst)) \
  || { echo "cannot diff the two listings for $ITEM; refusing to guess what to send" >&2; exit 1; }
echo "copying $ITEM: $(wc -l <<<"$todo") of $(wc -l <<<"$src") files, $(awk '{s+=$1} END {printf "%.1f GB", s/1e9}' <<<"$todo")"
cut -d' ' -f2- <<<"$todo" | tr '\n' '\0' \
  | ssh -o BatchMode=yes "$SRC_HOST" "tar -C '$SRC_DIR' --null -T - -cf -" \
  | "${NAS_SSH[@]}" "mkdir -p '$DEST' && tar -C '$DEST' -xf -"

if [ "$src" = "$(listing_dst)" ]; then
  echo "verified: $ITEM, $(wc -l <<<"$src") files"
else
  echo "MISMATCH after copy: $ITEM" >&2
  diff <(echo "$src") <(listing_dst) | head -20 >&2
  exit 1
fi
