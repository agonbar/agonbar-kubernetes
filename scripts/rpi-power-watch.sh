#!/usr/bin/env bash
# Measure how often a Raspberry Pi browns out, so power changes can be compared by number.
#
# rpi-aya logged 14 "Undervoltage detected!" in its first 10 minutes on a laptop USB-C
# charger. This samples the Pi over SSH for a fixed window and reports undervoltage
# events per minute plus what the firmware saw (get_throttled), to A/B a PSU, PoE or a
# lower arm_freq. Read-only: dmesg and vcgencmd only.
#
#   ./rpi-power-watch.sh                        # rpi-aya, 5 minutes
#   ./rpi-power-watch.sh dietpi@100.72.0.15 10  # any Pi, 10 minutes
set -uo pipefail

TARGET=${1:-dietpi@100.72.0.15}
MINUTES=${2:-5}
STEP=15
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=8 -i "$HOME/.ssh/rpivigo" "$TARGET")

# One line per sample: uptime_s undervoltage_count throttled_hex arm_mhz temp_c
sample(){
  "${SSH[@]}" 'printf "%s %s %s %s %s\n" \
    "$(cut -d. -f1 /proc/uptime)" \
    "$(sudo dmesg | grep -c "Undervoltage detected")" \
    "$(sudo vcgencmd get_throttled | cut -d= -f2)" \
    "$(( $(sudo vcgencmd measure_clock arm | cut -d= -f2) / 1000000 ))" \
    "$(sudo vcgencmd measure_temp | grep -oE "[0-9.]+")"' 2>/dev/null
}

# get_throttled: bit 0 = under-voltage now, bit 2 = throttled now,
# bit 16 = under-voltage since boot, bit 18 = throttled since boot
decode(){
  local v=$(( $1 )) out=()
  (( v & 0x1 ))     && out+=("UV-now")
  (( v & 0x4 ))     && out+=("throttled-now")
  (( v & 0x10000 )) && out+=("UV-since-boot")
  (( v & 0x40000 )) && out+=("throttled-since-boot")
  echo "${out[*]:-clean}"
}

read -r up0 uv0 th0 mhz0 t0 <<<"$(sample)" || true
[[ -z ${up0:-} ]] && { echo "cannot reach $TARGET" >&2; exit 1; }
echo "start: uptime ${up0}s, ${uv0} undervoltage events since boot, $(decode "$th0"), ${mhz0} MHz, ${t0}C"

samples=0 uv_now=0 thr_now=0 maxmhz=0 up=$up0 uv=$uv0
end=$((SECONDS + MINUTES * 60))
while (( SECONDS < end )); do
  sleep "$STEP"
  # Read into scratch vars: a lost sample must not blank the last good up/uv,
  # or the summary below subtracts from zero.
  read -r s_up s_uv s_th s_mhz s_t <<<"$(sample)"
  [[ -z ${s_up:-} ]] && { echo "  sample lost (Pi unreachable?)"; continue; }
  up=$s_up uv=$s_uv th=$s_th mhz=$s_mhz t=$s_t
  samples=$((samples + 1))
  (( th & 0x1 )) && uv_now=$((uv_now + 1))
  (( th & 0x4 )) && thr_now=$((thr_now + 1))
  (( mhz > maxmhz )) && maxmhz=$mhz
  printf '  +%4ss  events=%-4s %-40s %4s MHz %sC\n' "$((up - up0))" "$((uv - uv0))" "$(decode "$th")" "$mhz" "$t"
done

span=$(( up - up0 ))
events=$(( uv - uv0 ))
echo
echo "window: ${span}s, ${events} undervoltage events ($(awk -v e="$events" -v s="$span" 'BEGIN{printf "%.2f", s ? e*60/s : 0}')/min)"
echo "samples with under-voltage right now: ${uv_now}/${samples}, throttled right now: ${thr_now}/${samples}, max arm ${maxmhz} MHz"
