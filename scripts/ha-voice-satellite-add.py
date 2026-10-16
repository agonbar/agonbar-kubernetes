"""Add a Voice Satellite (jxlarrea/voice-satellite-card-integration) to Home Assistant.

Runs from inside the Home Assistant container. Mints a five-minute token from
an existing refresh token, then, each step idempotent:

1. walks the voice_satellite config flow, unless an entry with NAME exists;
2. copies the pipeline and wake word selects from the TEMPLATE satellite.

The browser on the device still has to pick the satellite by name in its
Voice Satellite settings; that choice lives in the browser, not in HA.

    kubectl -n lamg exec -i deploy/homeassistant -c homeassistant -- \
        env NAME=Tablet python3 - < scripts/ha-voice-satellite-add.py
"""

import json
import os
import time
import urllib.request

import jwt

BASE = "http://127.0.0.1:8123"
AUTH = "/config/.storage/auth"
NAME = os.environ["NAME"]
TEMPLATE = os.environ.get("TEMPLATE", "pc")
# Selects worth copying; the rest keep the integration's defaults.
COPY = [
    "pipeline_1",
    "wake_word_detection",
    "wake_word_1",
    "wake_word_sensitivity",
]


def token() -> str:
    """Mint a short-lived access token from a stored refresh token."""
    data = json.load(open(AUTH))["data"]
    refresh = next(
        t
        for t in data["refresh_tokens"]
        if t["token_type"] == "normal" and t.get("jwt_key")
    )
    now = int(time.time())
    return jwt.encode(
        {"iss": refresh["id"], "iat": now, "exp": now + 300},
        refresh["jwt_key"],
        algorithm="HS256",
    )


TOKEN = token()


def call(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(
        BASE + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    return json.load(urllib.request.urlopen(req))


def main() -> None:
    entries = call("GET", "/api/config/config_entries/entry?domain=voice_satellite")
    if any(e["title"] == NAME for e in entries):
        print(f"entry {NAME!r} already exists")
    else:
        flow = call(
            "POST", "/api/config/config_entries/flow", {"handler": "voice_satellite"}
        )
        result = call(
            "POST", f"/api/config/config_entries/flow/{flow['flow_id']}", {"name": NAME}
        )
        print("created", result["type"], result.get("title"))
        time.sleep(3)

    states = {s["entity_id"]: s for s in call("GET", "/api/states")}
    slug = next(
        s["entity_id"].split(".", 1)[1]
        for s in states.values()
        if s["entity_id"].startswith("assist_satellite.")
        and s["attributes"].get("friendly_name") == NAME
    )
    for key in COPY:
        want = states[f"select.{TEMPLATE}_{key}"]["state"]
        target = f"select.{slug}_{key}"
        if states[target]["state"] != want:
            call(
                "POST",
                "/api/services/select/select_option",
                {"entity_id": target, "option": want},
            )
        print(f"{target} = {want}")


main()
