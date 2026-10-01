#!/usr/bin/env bash
# Autopsy a Raspberry Pi SD card to find out why the node died.
#
# rpi-00, rpi-01 and rpi-aya (casa aya) all went NodeStatusUnknown within ~2 weeks
# in 2026 (03-31, 04-15, 04-14) and have been unreachable since. The card is the
# black box: the ext4 superblock keeps a dated error counter, and the journal keeps
# the last seconds before the power went away. Read one card per Pi, compare.
#
#   sudo ./rpi-sdcard-autopsy.sh /dev/sdb              # collect + verdict
#   sudo ./rpi-sdcard-autopsy.sh /dev/sdb --read-test  # + full-surface read pass
#   OUT=/tmp/rpi-00 sudo -E ./rpi-sdcard-autopsy.sh /dev/sdb
#
# EVERY access is read-only. The root filesystem is mounted `ro,noload`: plain `ro`
# still replays the ext4 journal, which writes to a card we may be trying to prove
# is failing. Nothing here mounts rw, fsck runs with -n, dd only reads.
set -uo pipefail

DEV=${1:-}
[[ -z $DEV ]] && { sed -n '2,15p' "$0" | sed 's/^# \?//'; exit 2; }
[[ -b $DEV ]] || { echo "not a block device: $DEV" >&2; exit 2; }
[[ $EUID -eq 0 ]] || { echo "needs root (mount, dumpe2fs)" >&2; exit 2; }
READ_TEST=no
[[ ${2:-} == --read-test ]] && READ_TEST=yes

OUT=${OUT:-/tmp/rpi-autopsy-$(basename "$DEV")-$(date +%Y%m%d-%H%M%S)}
MNT=$(mktemp -d)
mkdir -p "$OUT"

pass(){ printf '  \033[32mOK  \033[0m  %s\n' "$*"; }
warn(){ printf '  \033[33mWARN\033[0m  %s\n' "$*"; }
bad(){  printf '  \033[31mBAD \033[0m  %s\n' "$*"; }
section(){ printf '\n== %s\n' "$*"; }
findings=()
note(){ findings+=("$1"); }

# -R so the firmware partition nested under $MNT/boot comes off first
cleanup(){ mountpoint -q "$MNT" && umount -R "$MNT"; rmdir "$MNT" 2>/dev/null; }
trap cleanup EXIT

# dumpe2fs values contain colons (timestamps), so strip the key, don't split on ':'
sbfield(){ sed -n "s/^$1: *//p" "$OUT/dumpe2fs.txt" | head -1; }

# Pi layout: p1 = FAT32 firmware, p2 = ext4 root. /dev/sdb -> sdb1, /dev/mmcblk0 -> mmcblk0p1
part(){ [[ $DEV == *[0-9] ]] && echo "${DEV}p${1}" || echo "${DEV}${1}"; }
BOOTP=$(part 1); ROOTP=$(part 2)

section "card identity"
lsblk -o NAME,SIZE,FSTYPE,LABEL,MODEL "$DEV" | tee "$OUT/lsblk.txt"
udevadm info --query=property --name="$DEV" 2>/dev/null \
  | grep -E '^ID_(VENDOR|MODEL|SERIAL|NAME)=' | tee "$OUT/udev.txt"
# Native SD readers expose the card's own registers; USB readers hide them.
SYSD=/sys/block/$(basename "$DEV")/device
if [[ -d $SYSD ]]; then
  for r in cid csd name manfid oemid serial date fwrev hwrev ssr life_time pre_eol_info; do
    [[ -r $SYSD/$r ]] && printf '%-14s %s\n' "$r" "$(cat "$SYSD/$r")" | tee -a "$OUT/mmc-registers.txt"
  done
  # eMMC-only health registers; 0x01 = <10% of rated write cycles used, 0x0B = >100%
  if [[ -r $SYSD/life_time ]]; then
    lt=$(cat "$SYSD/life_time")
    note "mmc life_time=$lt (0x01 best .. 0x0B worn out)"
  fi
else
  warn "no sysfs mmc registers (USB card reader) — card age/serial unavailable"
fi

section "ext4 superblock ($ROOTP)"
if ! dumpe2fs -h "$ROOTP" >"$OUT/dumpe2fs.txt" 2>&1; then
  bad "dumpe2fs failed — superblock unreadable, card or partition table is gone"
  note "VERDICT-INPUT: root superblock unreadable"
  cat "$OUT/dumpe2fs.txt"
else
  grep -E '^(Filesystem state|FS Error count|First error|Last error|Mount count|Maximum mount|Last mount time|Last write time|Lifetime writes|Filesystem created):' \
    "$OUT/dumpe2fs.txt" | tee "$OUT/superblock-summary.txt"
  state=$(sbfield 'Filesystem state')
  errc=$(sbfield 'FS Error count')
  lastw=$(sbfield 'Last write time')
  [[ $state == clean ]] && pass "filesystem state: clean" || bad "filesystem state: $state"
  if [[ ${errc:-0} -gt 0 ]]; then
    bad "ext4 recorded $errc errors"
    note "VERDICT-INPUT: ext4 error counter = $errc ($(sbfield 'First error time') .. $(sbfield 'Last error time'), fn $(sbfield 'Last error function'))"
  else
    pass "ext4 error counter is 0 — the filesystem never saw an I/O error"
  fi
  # Last write time dates the death to the minute, independently of any log.
  note "died around: last write to root = ${lastw:-unknown}"
fi

section "read-only fsck (-n, changes nothing)"
fsck.ext4 -fn "$ROOTP" >"$OUT/fsck.txt" 2>&1
rc=$?
# 0 = clean, 4 = uncorrected errors left, 8 = operational error
if [[ $rc -eq 0 ]]; then pass "fsck found no problems"
else bad "fsck exit $rc — see $OUT/fsck.txt"; note "VERDICT-INPUT: fsck exit $rc"; fi
tail -5 "$OUT/fsck.txt"

section "mount read-only"
if mount -o ro,noload "$ROOTP" "$MNT" 2>"$OUT/mount-err.txt"; then
  pass "root mounted ro,noload at $MNT"
else
  bad "cannot mount root: $(cat "$OUT/mount-err.txt")"
  note "VERDICT-INPUT: root filesystem unmountable"
  exit 1
fi
# Bookworm puts the firmware partition at /boot/firmware, older images at /boot
mount -o ro "$BOOTP" "$MNT/boot" 2>/dev/null || mount -o ro "$BOOTP" "$MNT/boot/firmware" 2>/dev/null

grep -h PRETTY_NAME "$MNT/etc/os-release" 2>/dev/null
cat "$MNT/etc/hostname" 2>/dev/null
for f in config.txt cmdline.txt; do
  for d in "$MNT/boot" "$MNT/boot/firmware"; do
    [[ -r $d/$f ]] && cp "$d/$f" "$OUT/$f"
  done
done

section "logs"
# Offline journal read. -D works on an unbooted tree; machine-id mismatch is expected.
JDIR=$MNT/var/log/journal
if [[ -d $JDIR ]] && compgen -G "$JDIR/*/*.journal" >/dev/null; then
  pass "persistent journal present"
  journalctl -D "$JDIR" --no-pager --list-boots >"$OUT/journal-boots.txt" 2>/dev/null
  journalctl -D "$JDIR" --no-pager -n 4000 >"$OUT/journal-tail.txt" 2>/dev/null
  journalctl -D "$JDIR" --no-pager -b -1 -n 300 >"$OUT/journal-lastboot-tail.txt" 2>/dev/null
  echo "  boots recorded: $(grep -c . "$OUT/journal-boots.txt" 2>/dev/null)"
else
  warn "no persistent journal (Storage=volatile) — falling back to /var/log/*.log"
fi
for f in syslog syslog.1 kern.log kern.log.1 messages daemon.log boot.log; do
  [[ -r $MNT/var/log/$f ]] && cp "$MNT/var/log/$f" "$OUT/" 2>/dev/null
done
# zipped rotations often outlive the plain ones on a box that died months ago
for f in "$MNT"/var/log/{syslog,kern.log,messages}.*.gz; do
  [[ -r $f ]] && zcat "$f" >"$OUT/$(basename "${f%.gz}").txt" 2>/dev/null
done
cat "$OUT"/syslog* "$OUT"/kern.log* "$OUT"/messages* "$OUT"/journal-tail.txt 2>/dev/null >"$OUT/all-logs.txt"
echo "  collected $(wc -l <"$OUT/all-logs.txt" 2>/dev/null || echo 0) log lines"

# Reboot/shutdown history: a crash leaves a reboot with no matching shutdown.
if [[ -r $MNT/var/log/wtmp ]]; then
  last -f "$MNT/var/log/wtmp" -F >"$OUT/wtmp.txt" 2>/dev/null
  echo "  last 8 boot records:"; grep -E '^(reboot|shutdown)' "$OUT/wtmp.txt" | head -8 | sed 's/^/    /'
fi

section "smoking guns"
# Each pattern maps to one hardware cause. Counts matter more than presence:
# a handful of undervoltage lines is a brownout, thousands is a dying PSU.
scan(){ # label, regex, severity
  # `grep -c` already prints 0 when it matches nothing; a `|| echo 0` fallback would
  # append a second 0 on its exit-1 and break the comparison below.
  local n; n=$(grep -aEic "$2" "$OUT/all-logs.txt" 2>/dev/null); n=${n:-0}
  if [[ $n -gt 0 ]]; then
    "$3" "$1: $n hits"
    grep -aEi "$2" "$OUT/all-logs.txt" | tail -3 | sed 's/^/        /'
    note "VERDICT-INPUT: $1 x$n"
  else
    pass "$1: none"
  fi
}
scan "undervoltage (PSU/cable)"   'under-voltage|undervoltage|voltage normalised|hwmon.*[Uu]nder' bad
scan "throttling/thermal"         'temperature limit|thermal.*(throttl|critical)|soc_thermal' warn
scan "SD card I/O (mmc)"          'mmc[0-9]:.*(error|timeout|tuning|reset)|mmcblk[0-9].*(error|I/O)|card is (removed|not present)' bad
scan "filesystem errors"          'EXT4-fs (error|warning)|Remounting filesystem read-only|I/O error.*mmcblk|Buffer I/O error' bad
scan "OOM killer"                 'Out of memory: Kill|oom-kill:|invoked oom-killer' warn
scan "kernel panic / oops"        'Kernel panic|Oops:|BUG: |Internal error: Oops|watchdog: BUG' bad
scan "ethernet/PHY"               'bcmgenet.*(link is Down|timed out)|eth0: Link is Down' warn

# Inverted on purpose: finding a shutdown sequence is reassuring, its ABSENCE is the
# signal. A log that stops mid-line with no "Powering off" means the power vanished.
shut=$(grep -aEic 'systemd-shutdown|Reached target (System )?(Power-Off|Reboot|Shutdown)|Powering off' "$OUT/all-logs.txt" 2>/dev/null); shut=${shut:-0}
if [[ $shut -gt 0 ]]; then
  pass "clean shutdown recorded ($shut lines) — it was switched off, not killed"
  note "the last shutdown was orderly (someone or something powered it down)"
else
  bad "no shutdown sequence in the logs — the log just stops"
  note "VERDICT-INPUT: abrupt power loss (no shutdown sequence logged)"
fi

if [[ $READ_TEST == yes ]]; then
  head "full-surface read test (this takes a while)"
  # A worn card either throws read errors or collapses below ~1 MB/s.
  sz=$(blockdev --getsize64 "$DEV")
  echo "  reading $((sz/1024/1024)) MiB..."
  dd if="$DEV" of=/dev/null bs=4M status=progress conv=noerror,sync 2>"$OUT/readtest.txt"
  tail -3 "$OUT/readtest.txt" | sed 's/^/    /'
  speed=$(grep -oE '[0-9.]+ [kMG]B/s' "$OUT/readtest.txt" | tail -1)
  errs=$(dmesg 2>/dev/null | grep -aEc "$(basename "$DEV").*(I/O error|critical medium)"); errs=${errs:-0}
  if [[ $errs -gt 0 ]]; then bad "$errs read errors on the surface"; note "VERDICT-INPUT: $errs surface read errors"
  else pass "whole surface read without errors at $speed"; note "surface read clean at $speed"; fi
fi

section "findings"
if [[ ${#findings[@]} -eq 0 ]]; then
  echo "  nothing conclusive — the card is healthy and the logs are silent."
  echo "  That points away from the card and at power or at someone unplugging it."
else
  printf '  - %s\n' "${findings[@]}"
fi
printf '\nEvidence in %s\n' "$OUT"
printf 'Compare across the three Pis: a shared cause gives the same signature.\n'
