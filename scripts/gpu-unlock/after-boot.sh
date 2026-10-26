#!/usr/bin/env bash
# One-shot, run by a transient user unit on the first boot after gpu-unlock is
# deployed: record what the module did, bench the 3090 again and diff it
# against the stock run. Report: results/report.txt.
#   install: scripts/gpu-unlock/after-boot.sh --arm   (then stop/start VM 111)
set -uo pipefail

here=$(cd "$(dirname "$0")" && pwd)
unit=$HOME/.config/systemd/user/gpu-unlock-bench.service
sudo=/run/wrappers/bin/sudo

if [[ ${1:-} == --arm ]]; then
  cat > "$unit" <<EOF
[Unit]
Description=gpu-unlock: bench the 3090 once after boot
[Service]
Type=oneshot
ExecStart=$here/after-boot.sh
[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable gpu-unlock-bench.service
  exit 0
fi

# Disarm first, so a failure below never repeats on every boot.
systemctl --user disable gpu-unlock-bench.service
rm -f "$unit"

report=$here/results/report.txt
mkdir -p "$here/results"
exec > "$report" 2>&1
date
echo "== kernel log"
$sudo -n journalctl -k -b --no-pager | grep -iE "gpu_unlock|wpr2|xid" | tail -20
grep -H . /sys/module/gpu_unlock/parameters/targets 2>&1

# Let the boot settle and llama load its model before taking the card away.
for _ in $(seq 60); do systemctl is-active -q llama-rpc-standalone && break; sleep 5; done
sleep 60

$sudo -n systemctl stop llama-rpc-standalone
echo "== bench"
"$here/bench.sh" unlocked 2>&1 | grep -v "^ggml\|^  Device\|^load_backend"
$sudo -n systemctl start llama-rpc-standalone

echo "== compare"
python3 "$here/compare.py" stock unlocked
