#!/usr/bin/env bash
# Remote play for a Bazzite deck image: NetBird so the box is reachable from
# anywhere, and Sunshine so Moonlight (e.g. on a Steam Deck) can stream Game
# Mode. Run this ON the Bazzite machine. Idempotent.
#
#   NB_SETUP_KEY=<one-off key> ./bazzite-remote-play.sh   # first run, enrolls NetBird
#   ./bazzite-remote-play.sh                              # later runs: idempotent, checks and restarts
#
# Make the setup key with auto_groups=[adrian]: new peers otherwise land in group
# All only and no policy lets the other personal devices see them.
#
# NetBird: static binary in /usr/local/bin rather than rpm-ostree layering, so no
# reboot. /usr/local is /var/usrlocal here, and restorecon gives it bin_t;
# without that label systemd cannot exec it under SELinux enforcing.
#
# Sunshine: the RPM from LizardByte's COPR, layered with rpm-ostree. Game Mode is
# gamescope, which Sunshine can only capture through KMS; the flatpak has no KMS
# capture. Bazzite's `ujust setup-sunshine enable-brew` does do KMS, but on NVIDIA
# the brew build loads brew's own Mesa libEGL/libgallium instead of the system
# glvnd, so CUDA cannot register the captured texture
# (CUDA_ERROR_OPERATING_SYSTEM), NVENC fails and it falls back to libx264 on the
# CPU. The RPM links against system libs and finds h264_nvenc/hevc_nvenc, and its
# %caps sets cap_sys_admin for KMS itself.
#
# Firewall: NetBird puts wt0 in firewalld's trusted zone, so Moonlight's ports
# need no rule over NetBird. Who may connect is decided by the NetBird ACLs.
set -euo pipefail

NB_VERSION=${NB_VERSION:-0.79.0}
NB_MGMT=https://netbird.senseivision.ai
NB_HOSTNAME=${NB_HOSTNAME:-$(hostname)}
SUNSHINE_CONF="$HOME/.config/sunshine/sunshine.conf"
SUNSHINE_UNIT=app-dev.lizardbyte.app.Sunshine

step() { printf '\n== %s\n' "$*"; }

set_conf() {
  mkdir -p "$(dirname "$SUNSHINE_CONF")"
  if grep -q "^$1\s*=" "$SUNSHINE_CONF" 2>/dev/null; then
    sed -i "s|^$1\s*=.*|$1 = $2|" "$SUNSHINE_CONF"
  else
    echo "$1 = $2" >> "$SUNSHINE_CONF"
  fi
}

step "NetBird"
if command -v netbird >/dev/null && netbird status 2>/dev/null | grep -q "Management: Connected"; then
  netbird status | grep -E "FQDN|NetBird IP:"
else
  if [[ -z "${NB_SETUP_KEY:-}" ]]; then
    echo "not enrolled: re-run with NB_SETUP_KEY=<one-off key with auto_groups=[adrian]>" >&2
    exit 1
  fi
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  curl -fsSL -o "$tmp/nb.tgz" \
    "https://github.com/netbirdio/netbird/releases/download/v${NB_VERSION}/netbird_${NB_VERSION}_linux_amd64.tar.gz"
  tar xzf "$tmp/nb.tgz" -C "$tmp" netbird
  sudo install -m 0755 "$tmp/netbird" /usr/local/bin/netbird
  sudo restorecon /usr/local/bin/netbird
  sudo netbird service install || true
  sudo netbird service start || true
  sleep 3
  sudo netbird up --management-url "$NB_MGMT" --setup-key "$NB_SETUP_KEY" --hostname "$NB_HOSTNAME"
fi

step "Sunshine"
if rpm -q Sunshine >/dev/null 2>&1; then
  rpm -q Sunshine
else
  # A brew install from Bazzite's recipe would shadow the RPM's user unit.
  if [[ -x /home/linuxbrew/.linuxbrew/bin/brew ]]; then
    eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv)"
    if brew list --versions sunshine >/dev/null 2>&1; then
      brew services stop sunshine || true
      brew unpin sunshine || true
      brew uninstall sunshine
      rm -f "$HOME/.config/systemd/user/$SUNSHINE_UNIT.service"
    fi
  fi
  sudo curl -fsSL -o /etc/yum.repos.d/lizardbyte-stable.repo \
    "https://copr.fedorainfracloud.org/coprs/lizardbyte/stable/repo/fedora-$(rpm -E %fedora)/lizardbyte-stable-fedora-$(rpm -E %fedora).repo"
  # --apply-live: usable now, and it is also staged for the next boot.
  sudo rpm-ostree install --apply-live --idempotent Sunshine
fi
getcap "$(readlink -f /usr/bin/sunshine)"
set_conf capture kms
set_conf system_tray disabled
systemctl --user daemon-reload
systemctl --user enable "$SUNSHINE_UNIT"
systemctl --user restart "$SUNSHINE_UNIT"
sleep 15
journalctl --user -u "$SUNSHINE_UNIT" --since "-1min" --no-pager | grep -E "Found .* encoder" \
  || echo "WARNING: no encoder line yet, check: journalctl --user -u $SUNSHINE_UNIT"

cat <<EOF

Done. Pair from Moonlight with host $(netbird status 2>/dev/null | sed -n 's/^FQDN: //p').
Set the Sunshine web UI user first: https://localhost:47990 on this machine,
or over NetBird at https://<that name>:47990.
EOF
