"""Pick original-language audio and Spain-Spanish subtitles when a video starts.

Audio: Spanish-scene releases often flag the Spanish dub as default, and few
files flag the original track, so the original language is inferred: anime
(genre "Anime") is Japanese, movies use their production country, anything
else is assumed English.

Subtitles: Kodi and Jellyfin only look at the language code, so with two
"spa" tracks they pick whichever comes first, usually Latin American. This
picks the Castilian one by track name, falling back to any Spanish, then English.
"""
import json
import re

import xbmc

SPAIN = re.compile(r"europe|españa|espana|spain|castellano|castilian|es-es", re.I)
LATAM = re.compile(r"latin|latam|419|am[eé]rica|mexic", re.I)
EXTRA = re.compile(r"sign|song|forzad|forced|cartel", re.I)
COMMENTARY = re.compile(r"comment|comentario", re.I)

COUNTRY_LANG = {
    "united states of america": "eng", "united kingdom": "eng", "canada": "eng",
    "australia": "eng", "new zealand": "eng", "ireland": "eng",
    "japan": "jpn", "south korea": "kor", "china": "chi", "hong kong": "chi", "taiwan": "chi",
    "spain": "spa", "mexico": "spa", "argentina": "spa",
    "france": "fre", "italy": "ita", "germany": "ger",
}
# Kodi reports ISO 639-2; some languages have two codes (B and T forms).
SAME_LANG = {"fre": {"fre", "fra"}, "ger": {"ger", "deu"}, "chi": {"chi", "zho"}}


def rpc(method, **params):
    req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    return json.loads(xbmc.executeJSONRPC(json.dumps(req))).get("result")


def original_language():
    item = rpc("Player.GetItem", playerid=1, properties=["genre", "country", "tvshowid"])["item"]
    genres = list(item.get("genre") or [])
    if item.get("tvshowid", -1) > 0:
        show = rpc("VideoLibrary.GetTVShowDetails", tvshowid=item["tvshowid"], properties=["genre"])
        genres += show["tvshowdetails"]["genre"]
    if "Anime" in genres:
        return "jpn"
    for country in item.get("country") or []:
        if country.lower() in COUNTRY_LANG:
            return COUNTRY_LANG[country.lower()]
    return "eng"


def pick_audio(streams, lang):
    streams = [s for s in streams if not COMMENTARY.search(s["name"])]
    codes = SAME_LANG.get(lang, {lang})
    ranked = [s for s in streams if s.get("isoriginal")] + [s for s in streams if s["language"] in codes]
    return ranked[0] if ranked else None


def pick_subtitle(subs):
    subs = [s for s in subs if not EXTRA.search(s["name"])]
    spa = [s for s in subs if s["language"] == "spa"]
    ranked = ([s for s in spa if SPAIN.search(s["name"])]
              + [s for s in spa if not LATAM.search(s["name"])]
              + spa
              + [s for s in subs if s["language"] == "eng"])
    return ranked[0] if ranked else None


class Player(xbmc.Player):
    def onAVStarted(self):
        if not self.isPlayingVideo():
            return
        props = rpc("Player.GetProperties", playerid=1, properties=[
            "audiostreams", "currentaudiostream", "subtitles", "currentsubtitle", "subtitleenabled"])
        if not props:
            return
        start = max(0.0, self.getTime())
        changed = []

        audio = pick_audio(props["audiostreams"], original_language())
        if audio and audio["index"] != (props["currentaudiostream"] or {}).get("index"):
            rpc("Player.SetAudioStream", playerid=1, stream=audio["index"])
            changed.append("audio %s" % audio["language"])

        sub = pick_subtitle(props["subtitles"])
        if sub and not (props["subtitleenabled"] and sub["index"] == (props["currentsubtitle"] or {}).get("index")):
            rpc("Player.SetSubtitle", playerid=1, subtitle=sub["index"], enable=True)
            changed.append("subs %s (%s)" % (sub["name"], sub["language"]))

        if changed:
            # A newly opened stream only gets packets demuxed from now on, so
            # buffered subtitle lines would never show. Seeking back re-reads them.
            self.seekTime(start)
            xbmc.log("service.subs.castellano: %s from %.1fs" % (", ".join(changed), start), xbmc.LOGINFO)


player = Player()
xbmc.log("service.subs.castellano: started", xbmc.LOGINFO)
xbmc.Monitor().waitForAbort()
