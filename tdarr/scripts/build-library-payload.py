"""Construct the cruddb insert payload for the AV1 pilot library.

Safety invariants encoded here:
- folderToFolderConversion: true (output goes elsewhere)
- folderToFolderConversionDeleteSource: false (originals are preserved)
- processLibrary / processTranscodes / processHealthChecks: all false (paused)
- decisionMaker only allows h264 sources
- Single Local plugin (the AV1 pilot transcoder)
"""
import json
import sys

DAYS = ["Sun", "Mon", "Tue", "Wed", "Thur", "Fri", "Sat"]
HOURS = [f"{h:02d}-{(h+1)%24:02d}" for h in range(24)]
schedule = [{"_id": f"{d}:{h}", "checked": True} for d in DAYS for h in HOURS]

library = {
    "_id": "pilot_av1_001",
    "priority": 0,
    "name": "Anime AV1 Pilot",
    "folder": "/media/tv/Cells at Work!",
    "foldersToIgnore": "",
    "foldersToIgnoreCaseInsensitive": False,
    "folderWatchScanInterval": 30,
    "scannerThreadCount": 2,
    "cache": "/transcode_cache",
    "output": "/media/tv-pilot",
    "folderToFolderConversion": True,
    "folderToFolderConversionDeleteSource": False,
    "folderToFolderRecordHistory": True,
    "copyIfConditionsMet": False,
    "container": ".mkv",
    "containerFilter": "mkv,mp4,mov,m4v,mpg,mpeg,avi,flv,webm,wmv,vob,evo,iso,m2ts,ts",
    "createdAt": 0,
    "folderWatching": False,
    "useFsEvents": False,
    "scheduledScanFindNew": False,
    "processLibrary": False,
    "processTranscodes": False,
    "processHealthChecks": False,
    "scanOnStart": False,
    "exifToolScan": True,
    "mediaInfoScan": True,
    "ffprobeShowData": False,
    "isDirectoryLibrary": False,
    "closedCaptionScan": True,
    "scanButtons": True,
    "scanFound": "Files found:0",
    "navItemSelected": "navSourceFolder",
    "pluginIDs": [
        {
            "_id": "plugin1",
            "id": "Tdarr_Plugin_anime_av1_pilot",
            "checked": True,
            "source": "Local",
            "priority": 0,
            "InputsDB": {},
        }
    ],
    "pluginCommunity": False,
    "handbrake": False,
    "ffmpeg": True,
    "handbrakescan": False,
    "ffmpegscan": True,
    "preset": "",
    "decisionMaker": {
        "settingsPlugin": True,
        "settingsFlows": False,
        "settingsVideo": False,
        # include-mode: keep only the codecs marked checked
        "videoExcludeSwitch": False,
        "video_codec_names_exclude": [
            {"codec": "h264", "checked": True},
        ],
        "video_size_range_include": {"min": 0, "max": 100000},
        "video_height_range_include": {"min": 0, "max": 3000},
        "video_width_range_include": {"min": 0, "max": 4000},
        "settingsAudio": False,
        "audioExcludeSwitch": True,
        "audio_codec_names_exclude": [],
        "audio_size_range_include": {"min": 0, "max": 10},
    },
    "schedule": schedule,
    "totalHealthCheckCount": 0,
    "totalTranscodeCount": 0,
    "sizeDiff": 0,
    "holdNewFiles": False,
    "holdFor": 3600,
    "holdForDisplayUnit": "hours",
    "pluginStackOverview": True,
    "filterResolutionsSkip": "",
    "filterCodecsSkip": "",
    "filterContainersSkip": "",
    "processPluginsSequentially": True,
}

payload = {
    "data": {
        "collection": "LibrarySettingsJSONDB",
        "mode": "insert",
        "docID": library["_id"],
        "obj": library,
    }
}
json.dump(payload, sys.stdout, indent=2)
