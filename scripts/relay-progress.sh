#!/usr/bin/env bash
# Progress of a relay-to-nas.sh transfer, seen from the source machine.
# Reads the running tar process: /proc/PID/io for bytes read so far and
# /proc/PID/fdinfo for how far into the current file it is.
# Usage: watch -n 2 ~/relay-progress.sh [total-GB-of-this-run]
total_gb=${1:-}
p=$(pgrep -n -x tar) || { echo "no tar running: the transfer has finished or not started"; exit 0; }
now=$(date +%s)
r=$(awk "/^rchar/{print \$2}" /proc/$p/io)
st=/tmp/relay-progress.$p
[ -f "$st" ] || echo "$now $r" > "$st"
read t0 r0 < "$st"

for fd in /proc/$p/fd/*; do
  f=$(readlink "$fd") || continue
  case "$f" in /mnt/nas/*)
    pos=$(awk "/^pos/{print \$2}" /proc/$p/fdinfo/${fd##*/})
    sz=$(stat -c %s "$f")
    echo "file:  ${f#/mnt/nas/}"
    awk -v p="$pos" -v s="$sz" "BEGIN{printf \"       %5.1f%% of %.2f GB\n\", 100*p/s, s/1e9}" ;;
  esac
done

awk -v r="$r" -v r0="$r0" -v t="$now" -v t0="$t0" -v tot="$total_gb" "BEGIN{
  gb=r/1e9; rate=(t>t0)?(r-r0)/(t-t0)/1e6:0
  if (tot>0) printf \"sent:  %.2f of %.2f GB (%.1f%%)\n\", gb, tot, 100*gb/tot
  else       printf \"sent:  %.2f GB this run\n\", gb
  if (rate>0) {
    printf \"rate:  %.1f MB/s (average since you started watching)\n\", rate
    if (tot>0) printf \"eta:   %d min\n\", (tot-gb)*1e3/rate/60
  } else print \"rate:  measuring, give it a few seconds\"
}"
