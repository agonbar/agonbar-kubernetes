#!/bin/bash
# Switch proxmox-agb00 to one of its VM modes and keep at it until the mode's
# VMs are up.
#
# This runs on the host because a Home Assistant script can't do it: HA runs on
# work-vm-00 (VM 111), so a script that shuts 111 down kills itself before it
# starts anything else. HA only asks for the mode and this does the rest.
#
#   vm-mode <mode>        start the switch as the transient unit vm-mode.service
#   vm-mode run <mode>    do the switch in the foreground
#
# Home Assistant calls it over SSH with a key that authorized_keys pins to
#   command="/usr/local/sbin/vm-mode",restrict ssh-ed25519 ... home-assistant-aya
# so the requested mode arrives in SSH_ORIGINAL_COMMAND and nothing else runs.
#
# Install: scp scripts/pve-vm-mode.sh pve-agb00:/usr/local/sbin/vm-mode
# Follow:  journalctl -u vm-mode -f

set -uo pipefail

# Every VM here passes through a GPU, and 101/111 also share a disk, so any of
# them left running can block the target mode.
ALL="100 101 102 103 104 111"

targets() {
    case $1 in
        encoding) echo 111 ;;
        gaming) echo 103 ;;
        hybrid) echo 101 104 ;;
        off) echo ;;
        *) return 1 ;;
    esac
}

status() { qm status "$1" | awk '{print $2}'; }

stop_vm() {
    local id=$1
    [[ $(status "$id") == running ]] || return 0
    echo "$id: apagando"
    qm shutdown "$id" --timeout 180 --forceStop 1
    [[ $(status "$id") == running ]] && qm stop "$id" --skiplock 1
    [[ $(status "$id") == stopped ]] && return 0
    echo "$id: ERROR, sigue encendida"
    return 1
}

# Passthrough pins all guest RAM at start, and overcommitting this 31 GiB host
# OOM-thrashes it to the point sshd stops answering.
mem_ok() {
    local need=1024 id avail
    for id; do
        [[ $(status "$id") == running ]] ||
            need=$((need + $(qm config "$id" | awk '/^memory:/{print $2}')))
    done
    for _ in $(seq 30); do
        avail=$(awk '/^MemAvailable:/{print int($2 / 1024)}' /proc/meminfo)
        ((avail >= need)) && return 0
        sleep 2
    done
    echo "ERROR: hacen falta $need MB libres y hay $avail"
    return 1
}

# The guest has booted once the bridge learns its MAC on the VM's tap port,
# i.e. the OS sent its first frame. Works without a guest agent, which 101 lacks.
booted() {
    local id=$1 mac
    mac=$(qm config "$id" | sed -n 's/^net0: [a-z0-9]*=\([0-9A-Fa-f:]*\).*/\1/p' | tr A-F a-f)
    bridge fdb show brport "tap${id}i0" 2>/dev/null | grep -q "^$mac "
}

bring_up() {
    local id=$1 try
    if [[ $(status "$id") == running ]]; then
        echo "$id: ya estaba encendida"
        return 0
    fi
    for try in 1 2 3; do
        echo "$id: arrancando (intento $try)"
        if qm start "$id"; then
            for _ in $(seq 60); do
                booted "$id" && { echo "$id: arriba"; return 0; }
                [[ $(status "$id") == running ]] || break
                sleep 5
            done
            echo "$id: no ha arrancado en 5 min, la paro y reintento"
            qm stop "$id" --skiplock 1
        fi
        sleep 10
    done
    echo "$id: ERROR, no arranca tras 3 intentos"
    return 1
}

switch_to() {
    local mode=$1 want id rc=0 pids=()
    want=" $(targets "$mode") "
    echo "cambiando a modo $mode (VMs:${want% })"
    for id in $ALL; do
        [[ $want == *" $id "* ]] || { stop_vm "$id" & pids+=($!); }
    done
    for id in "${pids[@]}"; do wait "$id" || rc=1; done
    if ((rc)); then
        echo "FALLO: no he podido apagar todo, no arranco nada"
        return 1
    fi
    # shellcheck disable=SC2086
    mem_ok $want || return 1
    pids=()
    for id in $want; do bring_up "$id" & pids+=($!); done
    for id in "${pids[@]}"; do wait "$id" || rc=1; done
    if ((rc)); then
        echo "FALLO: modo $mode incompleto"
        return 1
    fi
    echo "OK: modo $mode alcanzado"
}

if [[ ${1:-} == run ]]; then mode=${2:-}; else mode=${1:-${SSH_ORIGINAL_COMMAND:-}}; fi
if ! targets "$mode" >/dev/null; then
    echo "uso: vm-mode [run] {encoding|gaming|hybrid|off}" >&2
    exit 2
fi
if [[ ${1:-} == run ]]; then
    switch_to "$mode"
    exit
fi
if systemctl is-active -q vm-mode; then
    echo "ya hay un cambio de modo en marcha: journalctl -u vm-mode -f" >&2
    exit 1
fi
# journald here keeps nothing below warning (MaxLevelStore=warning), so log at
# that level or the journal stays empty.
systemd-run --unit=vm-mode --collect -p RuntimeMaxSec=30min -p SyslogLevel=warning \
    "$(readlink -f "$0")" run "$mode"
