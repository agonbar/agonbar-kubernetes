#!/usr/bin/env bash
# Adds a "WoW Forever" Steam shortcut on the Deck that starts _forever_/Wow.exe
# directly, skipping the Battle.net launcher, and logs in by itself when
# ~/.config/wow-forever/login (email, newline, password; mode 600) exists.
# Trick taken from WoWForeverywhere: the client reads login.txt from its flavor
# dir at start-up. The wrapper wipes it 90 s after launch and on exit.
#
# The shortcut reuses the Battle.net shortcut's Proton prefix (2792117172) by
# overriding STEAM_COMPAT_DATA_PATH, so the install, WTF and addons are shared.
# Keep the Battle.net shortcut for patch days: the client can't update itself.
#
# Needs the CEF tunnel from steam-cef-eval.mjs:
#   ssh -i ~/.ssh/nas -fN -L 18080:127.0.0.1:8080 deck@steamdeck.local
set -euo pipefail
DECK=(ssh -i ~/.ssh/nas -o HostKeyAlias=192.168.1.218 deck@steamdeck.local)
CEF="$(dirname "$0")/steam-cef-eval.mjs"
PREFIX_APPID=2792117172
PROTON=GE-Proton11-11-x86_64  # internal name, see CompatToolMapping in config.vdf

"${DECK[@]}" 'install -Dm755 /dev/stdin "$HOME/.local/bin/wow-forever-direct"' <<'EOF'
#!/usr/bin/env bash
# Steam launch options: wow-forever-direct %command%
export STEAM_COMPAT_DATA_PATH="$HOME/.local/share/Steam/steamapps/compatdata/2792117172"
G="$STEAM_COMPAT_DATA_PATH/pfx/drive_c/Program Files (x86)/World of Warcraft/_forever_"
L="$HOME/.config/wow-forever/login"
if [ -r "$L" ]; then
  install -m600 "$L" "$G/login.txt"
  (sleep 90; rm -f "$G/login.txt") &
fi
"$@"
rm -f "$G/login.txt"
EOF

EXE="/home/deck/.local/share/Steam/steamapps/compatdata/$PREFIX_APPID/pfx/drive_c/Program Files (x86)/World of Warcraft/_forever_/Wow.exe"
DIR="${EXE%/Wow.exe}"
node "$CEF" "(async () => {
  const exe = $(printf '%s' "\"$EXE\"");
  const existing = appStore.allApps.find(a => a.display_name === 'WoW Forever');
  const id = existing ? existing.appid : await SteamClient.Apps.AddShortcut('WoW Forever', exe, '', '');
  SteamClient.Apps.SetShortcutName(id, 'WoW Forever');
  SteamClient.Apps.SetShortcutExe(id, JSON.stringify(exe));
  SteamClient.Apps.SetShortcutStartDir(id, JSON.stringify($(printf '%s' "\"$DIR\"")));
  SteamClient.Apps.SetShortcutLaunchOptions(id, '/home/deck/.local/bin/wow-forever-direct %command%');
  SteamClient.Apps.SpecifyCompatTool(id, '$PROTON');
  SteamClient.Apps.SetAppResolutionOverride(id, '1920x1080');
  return id;
})()"
