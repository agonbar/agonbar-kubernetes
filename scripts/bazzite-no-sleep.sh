#!/usr/bin/env bash
# Bazzite (VM 104) never suspends or hibernates. Game Mode asks logind to suspend
# after its idle timer, and a suspended VM drops off the network until someone
# presses a key (system_wakeup is refused). Disabling sleep in logind covers every
# caller: Steam's timer, its power menu, anything over D-Bus. CanSuspend reads "no".
#
#   ./bazzite-no-sleep.sh          # install the drop-in and check it
#   ./bazzite-no-sleep.sh undo     # remove it
set -euo pipefail

B=(ssh -o BatchMode=yes -i "$HOME/.ssh/nas" bazzite@10.95.129.128)
F=/etc/systemd/sleep.conf.d/90-no-sleep.conf

if [[ ${1:-} == undo ]]; then
  "${B[@]}" "sudo rm -f $F"
else
  "${B[@]}" "printf '[Sleep]\nAllowSuspend=no\nAllowHibernation=no\nAllowHybridSleep=no\nAllowSuspendThenHibernate=no\n' \
    | sudo install -D -m 644 /dev/stdin $F && sudo restorecon -R /etc/systemd/sleep.conf.d"
fi
"${B[@]}" "busctl call org.freedesktop.login1 /org/freedesktop/login1 org.freedesktop.login1.Manager CanSuspend"
