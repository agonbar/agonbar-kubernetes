#!/usr/bin/env bash
# Install or upgrade the Unifideck Decky plugin (Epic, GOG, Amazon, Ubisoft,
# Battle.net and GameVault libraries inside Steam) on a Decky host: the Steam
# Deck or the Bazzite VM. Run it ON the host, as the Steam user:
#   ssh deck@<steamdeck> bash -s < scripts/unifideck-install.sh
#   ssh bazzite@bazzite.nb.senseizero.lan bash -s < scripts/unifideck-install.sh
#
# One install covers every Steam account on the host: the plugin follows the
# MostRecent user in loginusers.vdf and rebinds shortcuts.vdf on a switch.
# Store logins live in ~/.config/unifideck and ~/.local/share/unifideck and are
# shared by all Steam accounts. They're not copied between hosts (Epic and GOG
# rotate refresh tokens), so log in to each store from the plugin's QAM page.
set -euo pipefail

VERSION=0.7.6
SHA256=36c9e057a22e2daa5f7406da789e85db039005de307e160f725113f2a39e6315
URL=https://github.com/mubaraknumann/unifideck/releases/download/Release-$VERSION/unifideck.prod.v$VERSION.zip
PLUGINS=$HOME/homebrew/plugins

[[ -d $PLUGINS ]] || { echo "no Decky Loader here ($PLUGINS missing)" >&2; exit 1; }
command -v unzip >/dev/null || { echo "unzip is required (python's zipfile drops the exec bits in bin/)" >&2; exit 1; }

installed=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' \
  "$PLUGINS/Unifideck/package.json" 2>/dev/null || true)
if [[ $installed == "$VERSION" ]]; then
  echo "Unifideck $VERSION already installed"
  exit 0
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
curl -fsSLo "$tmp/plugin.zip" "$URL"
echo "$SHA256  $tmp/plugin.zip" | sha256sum -c --quiet

# Same layout Decky's "install from ZIP" leaves: plugin dir owned by root, its
# contents by the Steam user.
sudo rm -rf "$PLUGINS/Unifideck"
sudo unzip -q "$tmp/plugin.zip" -d "$PLUGINS"
sudo chown -R "$(id -u):$(id -g)" "$PLUGINS/Unifideck"/*
sudo systemctl restart plugin_loader
echo "Unifideck ${installed:-none} -> $VERSION; open it from the QAM to connect stores"
