#!/usr/bin/env bash
# Add Azeroth Survivors (browser game, https://wow.aethus.pl) to the Steam Deck as
# a non-Steam app: a launcher that opens the flatpak Chrome in app mode, fullscreen,
# with WebGPU forced on. Run this FROM work-vm-00.
#
#   deck-azeroth-survivors.sh [host]    default 192.168.1.218 (docked), .236 on wifi
#
# Why the flags: Chrome on Linux still ships WebGPU off; --enable-unsafe-webgpu plus
# Vulkan turns it on (checked headless on the Deck: adapter "amd rdna-2"). A separate
# --user-data-dir keeps the flags from being ignored when a normal Chrome window is
# already open, since a second launch just hands the URL to the running instance.
set -euo pipefail

HOST=${1:-192.168.1.218}
DECK=(ssh -i "$HOME/.ssh/nas" -o IdentitiesOnly=yes -o BatchMode=yes -o HostKeyAlias=192.168.1.218 "deck@$HOST")

"${DECK[@]}" bash -s <<'EOF'
set -euo pipefail
title="Azeroth Survivors"
launcher=$HOME/.local/bin/azeroth-survivors.sh
desktop=$HOME/.local/share/applications/azeroth-survivors.desktop
mkdir -p "$(dirname "$launcher")" "$(dirname "$desktop")"

cat > "$launcher" <<'SH'
#!/usr/bin/env bash
exec flatpak run com.google.Chrome \
  --user-data-dir="$HOME/.var/app/com.google.Chrome/data/azeroth-survivors" \
  --enable-unsafe-webgpu --ignore-gpu-blocklist --enable-features=Vulkan \
  --no-first-run --no-default-browser-check \
  --start-fullscreen --app=https://wow.aethus.pl
SH
chmod +x "$launcher"

cat > "$desktop" <<DESK
[Desktop Entry]
Name=$title
Exec=$launcher
Icon=applications-games
Type=Application
Categories=Game;
DESK

vdf=$(ls "$HOME"/.local/share/Steam/userdata/*/config/shortcuts.vdf 2>/dev/null || true)
if [ -n "$vdf" ] && strings $vdf | grep -qxF "$title"; then
  echo "already in Steam"
elif pgrep -x steam >/dev/null; then
  steamos-add-to-steam "$desktop" </dev/null
  echo "added to Steam, look under Non-Steam"
else
  echo "Steam not running: add it later with steamos-add-to-steam $desktop"
fi
EOF
