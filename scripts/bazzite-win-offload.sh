#!/usr/bin/env bash
# Moves the personal folders off the Windows C: disk that Bazzite (VM 104) mounts,
# into nas02's RAID/adrian, which nas03 mirrors every night (scripts/nas03-mirror.sh).
#
#   NAS02_SUDO=... ./bazzite-win-offload.sh grant    # temp key on Bazzite + NOPASSWD rsync on nas02
#   ./bazzite-win-offload.sh run                     # rsync as a transient unit on Bazzite
#   ./bazzite-win-offload.sh status
#   ./bazzite-win-offload.sh verify                  # dry run, must list nothing
#   ./bazzite-win-offload.sh clean                   # delete the sources on C:, only if verify is empty
#   ./bazzite-win-offload.sh revoke                  # undo grant
#   ./bazzite-win-offload.sh finish                  # wait for run, clean, revoke (unattended)
#
# truenas_admin can't even traverse RAID/adrian (770 adrian:adrian), so the receiver
# runs `sudo rsync`, the same temporary grant the nas00 seed used. Revoke when done.
# Documents is copied but never deleted from C: (game saves live in it).
set -euo pipefail

C=/run/media/bazzite/C212A7EE12A7E61F
DEST=/mnt/RAID/adrian/windows-c-2026-10-06
NAS02=truenas_admin@nas02.nb.senseizero.lan   # as Bazzite resolves it, over NetBird
KEY=.ssh/win-offload                          # on Bazzite
UNIT=win-offload
# name  path-under-C:  clean (rm = whole dir, empty = contents, keep = nothing)
SRCS=(
  "backup-panda-20260908|backup-panda-20260908|rm"
  "Videos|Users/agonbar/Videos|empty"
  "Pictures|Users/agonbar/Pictures|empty"
  "Music|Users/agonbar/Music|empty"
  "Downloads|Users/agonbar/Downloads|empty"
  "Documents|Users/agonbar/Documents|keep"
)
B=(ssh -o BatchMode=yes -i "$HOME/.ssh/nas" bazzite@10.95.129.128)
N02=(ssh -o BatchMode=yes -i "$HOME/.ssh/nas" truenas_admin@100.72.0.41)

s02(){ printf '%s\n' "${NAS02_SUDO:?}" | "${N02[@]}" "sudo -S -p '' sh -c '$1'"; }

# One rsync per folder. -rt only: NTFS through lowntfs-3g has no meaningful owners or modes,
# and RAID/adrian has a restricted NFSv4 ACL (chmod is EPERM even for root), files inherit it.
rsync_lines() {  # $2 = skip-keep: only the folders clean will delete
  local extra=$1 s name path mode
  for s in "${SRCS[@]}"; do
    IFS='|' read -r name path mode <<<"$s"
    [[ ${2:-} == skip-keep && $mode == keep ]] && continue
    printf 'rsync -rt %s --chown=adrian:adrian --partial-dir=.rsync-partial -e "ssh -i $HOME/%s -o BatchMode=yes" --rsync-path="sudo rsync" "%s/%s/" "%s:%s/%s/"\n' \
      "$extra" "$KEY" "$C" "$path" "$NAS02" "$DEST" "$name"
  done
}

# Rewrites truenas_admin's key list on nas02; user.update overwrites it whole.
set_keys() {  # $1 = python expression over `keys` (list of lines) and `pub`
  local pub; pub=$("${B[@]}" "cat ~/$KEY.pub")
  "${N02[@]}" "python3 - <<'EOF'
import json, subprocess
mid = lambda *a: subprocess.run(['midclt', 'call', *a], check=True, capture_output=True, text=True).stdout
u = json.loads(mid('user.query', '[[\"username\",\"=\",\"truenas_admin\"]]'))[0]
pub = '''$pub'''.strip()
keys = [k for k in (u['sshpubkey'] or '').splitlines() if k.strip() and pub.split()[1] not in k]
$1
mid('user.update', str(u['id']), json.dumps({'sshpubkey': '\n'.join(keys), 'sudo_commands_nopasswd': nopasswd}))
print('keys:', len(keys), 'nopasswd:', nopasswd)
EOF"
}

case ${1:-} in
  grant)
    "${B[@]}" "[ -f ~/$KEY ] || ssh-keygen -q -t ed25519 -N '' -C win-offload -f ~/$KEY"
    set_keys "keys.append('restrict ' + pub); nopasswd = ['/usr/bin/rsync']"
    s02 "mkdir -p $DEST && chown adrian:adrian $DEST"
    "${B[@]}" "ssh -i ~/$KEY -o StrictHostKeyChecking=accept-new $NAS02 sudo -n rsync --version | head -1"
    ;;
  run)
    # The NetBird path to nas02 drops now and then (No route to host, rc 255); rsync
    # resumes, so each folder retries for up to ~2 h before the run gives up on it.
    script=$(printf '#!/bin/bash\nset -u\nrc=0\ntry(){ for i in $(seq 24); do "$@" && return 0; r=$?; echo "== $(date -Is) rc=$r, retry $i"; sleep 300; done; rc=$r; }\n'
             rsync_lines "--info=stats1" | sed 's/^/try /'
             printf 'echo "== $(date -Is) done rc=$rc"\n')
    "${B[@]}" "cat > ~/win-offload.sh && chmod +x ~/win-offload.sh" <<<"$script"
    # Game Mode idle-suspends the VM, which would freeze the transfer for hours.
    "${B[@]}" "XDG_RUNTIME_DIR=/run/user/1000 systemd-run --user --unit=$UNIT --collect \
      systemd-inhibit --what=sleep:idle --who=win-offload --why='copying C: to nas02' \
      bash -c '~/win-offload.sh >> ~/win-offload.log 2>&1'"
    ;;
  status)
    "${B[@]}" "XDG_RUNTIME_DIR=/run/user/1000 systemctl --user is-active $UNIT; tail -n 15 ~/win-offload.log"
    ;;
  verify)
    # Any rsync failure prints a line, so an empty result really means "all there".
    # Documents is skipped: it stays on C: and games keep writing to it.
    "${B[@]}" "bash -s" <<<"{ $(rsync_lines "-n --itemize-changes" skip-keep | sed 's/$/ 2>\&1 || echo "rsync failed: $?";/') } | grep -v '^skipping non-regular' || true"
    ;;
  clean)
    out=$("$0" verify)
    [ -z "$out" ] || { echo "verify not empty, refusing:"; echo "$out" | head; exit 1; }
    for s in "${SRCS[@]}"; do
      IFS='|' read -r name path mode <<<"$s"
      case $mode in
        rm)    "${B[@]}" "sudo rm -rf '$C/$path'" ;;
        empty) "${B[@]}" "sudo find '$C/$path' -mindepth 1 -maxdepth 1 ! -iname desktop.ini -exec rm -rf {} +" ;;
        keep)  continue ;;
      esac
      echo "cleaned $name ($mode)"
    done
    "${B[@]}" "df -h $C | tail -1"
    ;;
  revoke)
    set_keys "nopasswd = []"
    ;;
  finish)  # unattended: wait for run, then clean (only if verify is empty), always revoke
    # An unreachable Bazzite (reboot, Encoding mode) prints nothing, which keeps us waiting.
    until state=$("${B[@]}" "XDG_RUNTIME_DIR=/run/user/1000 systemctl --user is-active $UNIT" || true)
          [[ $state == inactive || $state == failed ]]; do sleep 300; done
    "${B[@]}" "tail -n 1 ~/win-offload.log"
    "$0" clean || echo "clean skipped, sources left on C:"
    "$0" revoke
    ;;
  *) sed -n '2,14p' "$0"; exit 1 ;;
esac
