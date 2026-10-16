#!/usr/bin/env bash
# Witcher 3 (next-gen, DX12) in side-by-side 3D on the Steam Deck for the XREAL
# glasses: ReShade + SuperDepth3D. Run ON the Deck:
#   ssh -i ~/.ssh/nas deck@<deck> bash -s < scripts/steamdeck-witcher3-3d.sh
#   ssh -i ~/.ssh/nas deck@<deck> bash -s -- uninstall < scripts/steamdeck-witcher3-3d.sh
#
# ReShade goes in as dxgi.dll next to witcher3.exe. No launch options needed:
# Proton already overrides dxgi=n for DXVK, so Wine loads the copy in the exe
# directory first and ReShade chains to DXVK's dxgi in system32.
#
# The game always starts in 2D (StartupPresetPath=2D.ini, an empty preset), so
# nothing changes when playing on the Deck screen or the glasses in 2D. F9
# (KeyNextPreset) flips between 2D.ini and 3D.ini, the only two presets in
# reshade-presets/. Home opens the ReShade overlay. 3D only looks right with the
# glasses in their own SBS mode (3840x1080) and the game's scaling on Stretch.
#
# Set on the Deck outside this script (2026-10-05): the Witcher's Steam Input
# autosave config (Steam Controller Configs/<account-id>/config/292030, original
# kept as .bak-20261005) has L5=F9 and R5=Home instead of F5 and X, and the
# game has a per-game performance profile with scaling on Stretch.
#
# Re-running install refreshes ReShade and the shaders but keeps the presets,
# so depth tweaks made in the overlay survive.
set -euo pipefail

RESHADE_VERSION=${RESHADE_VERSION:-6.8.0}
DEPTH3D_REF=${DEPTH3D_REF:-d52d9fc5767a2562c9df1183010388f82c3b80e2}
GAME=${GAME:-$HOME/.local/share/Steam/steamapps/common/The Witcher 3/bin/x64_dx12}

[ -f "$GAME/witcher3.exe" ] || { echo "witcher3.exe not found in $GAME" >&2; exit 1; }

if [ "${1:-install}" = uninstall ]; then
  rm -rf "$GAME"/{dxgi.dll,ReShade.ini,ReShade.log,reshade-shaders,reshade-presets} "$HOME/.local/bin/witcher3-res"
  echo "ReShade removed from $GAME; drop witcher3-res from the Steam launch options"
  exit 0
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

curl -fsSLo "$tmp/setup.exe" "https://reshade.me/downloads/ReShade_Setup_${RESHADE_VERSION}_Addon.exe"
7z e -y -o"$tmp" "$tmp/setup.exe" ReShade64.dll >/dev/null
install -m644 "$tmp/ReShade64.dll" "$GAME/dxgi.dll"

mkdir -p "$GAME/reshade-shaders/Shaders" "$GAME/reshade-shaders/Textures" "$GAME/reshade-presets"
for f in SuperDepth3D.fx Overwatch.fxh AXAA.fxh; do
  curl -fsSLo "$GAME/reshade-shaders/Shaders/$f" \
    "https://raw.githubusercontent.com/BlueSkyDefender/Depth3D/$DEPTH3D_REF/Shaders/$f"
done

[ -f "$GAME/reshade-presets/2D.ini" ] || printf 'Techniques=\nTechniqueSorting=SuperDepth3D\n' > "$GAME/reshade-presets/2D.ini"
[ -f "$GAME/reshade-presets/3D.ini" ] || cat > "$GAME/reshade-presets/3D.ini" <<'EOF'
Techniques=SuperDepth3D
TechniqueSorting=SuperDepth3D

[SuperDepth3D.fx]
Stereoscopic_Mode=0
EOF

# VK_F9 = 120, VK_HOME = 36; each key is "keycode,ctrl,shift,alt".
cat > "$GAME/ReShade.ini" <<'EOF'
[GENERAL]
EffectSearchPaths=.\reshade-shaders\Shaders\**
TextureSearchPaths=.\reshade-shaders\Textures\**
PresetPath=.\reshade-presets\2D.ini
StartupPresetPath=.\reshade-presets\2D.ini
PresetTransitionDuration=0
SkipLoadingDisabledEffects=1

[INPUT]
KeyOverlay=36,0,0,0
KeyNextPreset=120,0,0,0
KeyEffects=0,0,0,0

[OVERLAY]
TutorialProgress=4
EOF

# Launch wrapper: the game keeps its own Resolution= and ignores the size
# gamescope offers, so set it per launch. 1920x1080 with the glasses (also in
# SBS mode, where Depth3D splits that 16:9 frame and gamescope stretches it to
# 3840x1080), the Deck's 1280x800 otherwise. Pairs with Steam's resolution
# override 1920x1080 for external displays only.
install -Dm755 /dev/stdin "$HOME/.local/bin/witcher3-res" <<'EOF'
#!/usr/bin/env bash
S="$HOME/.local/share/Steam/steamapps/compatdata/292030/pfx/drive_c/users/steamuser/Documents/The Witcher 3/dx12user.settings"
res=1280x800
grep -qx connected /sys/class/drm/card*-DP-*/status 2>/dev/null && res=1920x1080
[ -f "$S" ] && sed -i "s/^Resolution=\".*\"/Resolution=\"$res\"/" "$S"
exec "$@"
EOF

echo "ReShade $RESHADE_VERSION + SuperDepth3D @ ${DEPTH3D_REF:0:7} installed in $GAME"
echo "Steam launch options: $HOME/.local/bin/witcher3-res %command% --launcher-skip"
