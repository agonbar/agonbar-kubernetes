#!/usr/bin/env python3
# Build a portable Kodi clone bundle from a configured Kodi (source) for another device.
# Copies only the addon closure of ROOTS (user addons dir), selected userdata, consistent
# SQLite copies, and a minimal guisettings.xml (skin/locale/weather only, no A/V settings).
# Usage: kodi-clone-bundle.py <kodi_home> <out_dir> <jellyfin_addon_data_override_dir>
# The override dir holds data.json/jellyfin_guid/settings.xml with a token minted for the
# target device (Quick Connect), so the two Kodis don't share one Jellyfin session.
# Addons33.db is copied on purpose: Kodi registers hand-copied addons as DISABLED, and a
# skin whose deps are disabled fails to load. DB names are Kodi 21 (Omega); source and
# target must run the same major. Android: push <out_dir>/.kodi to
# /sdcard/Android/data/org.xbmc.kodi/files/ and start Kodi with the screen ON (in standby
# Android pauses it before it initializes).
import os, re, sys, shutil, sqlite3, glob, xml.etree.ElementTree as ET
home, out, jf_override = sys.argv[1], sys.argv[2], sys.argv[3]
ROOTS = ["plugin.video.jellyfin", "repository.jellyfin.kodi", "repository.jurialmunkey",
         "service.upnext", "service.subs.castellano", "resource.language.es_es",
         "skin.arctic.zephyr.mod", "plugin.video.themoviedb.helper", "weather.ha"]
ADDON_DATA = ["skin.arctic.zephyr.mod", "script.skinshortcuts", "script.embuary.helper",
              "weather.ha", "service.upnext"]
GUI_KEEP = re.compile(r'^(lookandfeel\.(skin|skintheme|skincolors|font)|locale\..*|subtitles\.languages|weather\..*|services\.esenabled)$')

src_addons = os.path.join(home, "addons")
dst = os.path.join(out, ".kodi"); shutil.rmtree(dst, ignore_errors=True)
os.makedirs(os.path.join(dst, "addons")); os.makedirs(os.path.join(dst, "userdata", "Database"))

def deps(aid):
    p = os.path.join(src_addons, aid, "addon.xml")
    if not os.path.exists(p): return []
    root = ET.parse(p).getroot()
    return [i.get("addon") for i in root.findall("requires/import") if not i.get("addon","").startswith("xbmc.")]

closure, stack = set(), list(ROOTS)
while stack:
    a = stack.pop()
    if a in closure or not os.path.isdir(os.path.join(src_addons, a)):
        continue  # not in user dir => system addon (bundled with Kodi), skip
    closure.add(a); stack.extend(deps(a))
for a in sorted(closure):
    shutil.copytree(os.path.join(src_addons, a), os.path.join(dst, "addons", a))
print(f"addons: {len(closure)}")

ud_src, ud_dst = os.path.join(home, "userdata"), os.path.join(dst, "userdata")
for name in ("MyVideos131.db", "jellyfin.db", "Addons33.db", "ViewModes6.db"):
    s = sqlite3.connect(os.path.join(ud_src, "Database", name)); d = sqlite3.connect(os.path.join(ud_dst, "Database", name))
    s.backup(d); d.close(); s.close()
for sub in ("library", "playlists"):
    if os.path.isdir(os.path.join(ud_src, sub)):
        shutil.copytree(os.path.join(ud_src, sub), os.path.join(ud_dst, sub))
for a in ADDON_DATA:
    p = os.path.join(ud_src, "addon_data", a)
    if os.path.isdir(p): shutil.copytree(p, os.path.join(ud_dst, "addon_data", a))
tmdb = os.path.join(ud_src, "addon_data", "plugin.video.themoviedb.helper", "settings.xml")
if os.path.exists(tmdb):
    os.makedirs(os.path.join(ud_dst, "addon_data", "plugin.video.themoviedb.helper"))
    shutil.copy(tmdb, os.path.join(ud_dst, "addon_data", "plugin.video.themoviedb.helper"))
jf_dst = os.path.join(ud_dst, "addon_data", "plugin.video.jellyfin"); os.makedirs(jf_dst)
for f in ("data.json", "jellyfin_guid", "settings.xml"):
    shutil.copy(os.path.join(jf_override, f), jf_dst)
sync = os.path.join(ud_src, "addon_data", "plugin.video.jellyfin", "sync.json")
if os.path.exists(sync): shutil.copy(sync, jf_dst)

gs = ET.parse(os.path.join(ud_src, "guisettings.xml")).getroot()
new = ET.Element("settings", {"version": "2"})
for s in gs.iter("setting"):
    if GUI_KEEP.match(s.get("id", "")):
        e = ET.SubElement(new, "setting", {"id": s.get("id")}); e.text = s.text
ET.ElementTree(new).write(os.path.join(ud_dst, "guisettings.xml"), encoding="utf-8", xml_declaration=True)
print("guisettings keys:", [s.get("id") for s in new])
