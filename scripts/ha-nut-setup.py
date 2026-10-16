"""Add nas02's UPS to Home Assistant: NUT integration, sensors and a dashboard.

Runs from inside the Home Assistant container. Mints a five-minute token from
an existing refresh token, then, each step idempotent:

1. walks the nut config flow, unless an entry for the host already exists;
2. enables every NUT sensor except the ones that never change (NOISE);
3. creates the "SAI" dashboard and overwrites its config.

nas02's upsd listens on 3493 once "Remote Monitor" is on in TrueNAS; reading
variables needs no login.

    kubectl -n lamg exec -i deploy/homeassistant -c homeassistant -- \
        python3 - < scripts/ha-nut-setup.py
"""

import asyncio
import json
import sys
import time

import aiohttp
import jwt

BASE = "http://127.0.0.1:8123"
AUTH = "/config/.storage/auth"
ENTRIES = "/config/.storage/core.config_entries"
# LAN address on purpose: this Home Assistant is pinned to the lamg LAN nodes,
# and the other site will get its own NAS and UPS.
HOST = "192.168.0.29"
PORT = 3493
DASHBOARD = "sai-nas02"

# Fixed values with no history worth keeping. output.voltage.nominal is also
# wrong: the driver reports 72 V, the battery bank, for a 230 V output.
NOISE = {
    "battery.type",
    "battery.voltage.nominal",
    "output.voltage.nominal",
    "input.transfer.high",
    "input.transfer.low",
    "ups.realpower.nominal",
}


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


def dashboard(e: dict[str, str]) -> dict:
    """Lovelace config, core cards only. e maps NUT variable -> entity_id."""
    gauge = lambda key, name, **kw: {  # noqa: E731
        "type": "gauge",
        "entity": e[key],
        "name": name,
        "needle": True,
        **kw,
    }
    graph = lambda title, keys, hours=24: {  # noqa: E731
        "type": "history-graph",
        "title": title,
        "hours_to_show": hours,
        "entities": [e[k] for k in keys],
    }
    return {
        "title": "SAI",
        "views": [
            {
                "title": "SAI nas02",
                "path": "sai",
                "type": "sections",
                "max_columns": 3,
                "sections": [
                    {
                        "type": "grid",
                        "cards": [
                            {"type": "heading", "heading": "Salicru SLC TWIN PRO2"},
                            {
                                "type": "conditional",
                                "conditions": [
                                    {
                                        "condition": "state",
                                        "entity": e["ups.status"],
                                        "state_not": ["OL", "OL CHRG"],
                                    }
                                ],
                                "card": {
                                    "type": "markdown",
                                    "content": (
                                        '<ha-alert alert-type="warning">'
                                        "El SAI no está en red normal: "
                                        f"{{{{ states('{e['ups.status.display']}') }}}}"
                                        "</ha-alert>"
                                    ),
                                },
                            },
                            {"type": "tile", "entity": e["ups.status.display"], "name": "Estado"},
                            {"type": "tile", "entity": e["battery.runtime"], "name": "Autonomía"},
                            gauge(
                                "battery.charge", "Batería", min=0, max=100,
                                severity={"green": 50, "yellow": 20, "red": 0},
                            ),
                            gauge(
                                "ups.load", "Carga", min=0, max=100,
                                severity={"green": 0, "yellow": 60, "red": 85},
                            ),
                            gauge("ups.realpower", "Potencia", min=0, max=2700),
                            {"type": "tile", "entity": e["battery.voltage"], "name": "Tensión batería"},
                        ],
                    },
                    {
                        "type": "grid",
                        "cards": [
                            {"type": "heading", "heading": "Histórico"},
                            graph("Tensión entrada / salida", ["input.voltage", "output.voltage"]),
                            graph("Batería y carga", ["battery.charge", "ups.load"]),
                            graph("Potencia", ["ups.realpower"]),
                            graph("Estado", ["ups.status.display"], hours=168),
                        ],
                    },
                    {
                        "type": "grid",
                        "cards": [
                            {"type": "heading", "heading": "Detalles"},
                            {
                                "type": "entities",
                                "entities": [
                                    {"entity": e["ups.status"], "name": "Estado NUT"},
                                    {"entity": e["battery.runtime"], "name": "Autonomía"},
                                    {"entity": e["battery.voltage"], "name": "Tensión batería"},
                                    {"entity": e["input.voltage"], "name": "Tensión entrada"},
                                    {"entity": e["output.voltage"], "name": "Tensión salida"},
                                    {"entity": e["input.frequency"], "name": "Frecuencia entrada"},
                                    {"entity": e["output.frequency"], "name": "Frecuencia salida"},
                                    {"entity": e["ups.beeper.status"], "name": "Zumbador"},
                                    {"entity": e["ups.test.result"], "name": "Autodiagnóstico"},
                                ],
                            },
                        ],
                    },
                ],
            }
        ],
    }


async def main() -> int:
    auth = {"Authorization": f"Bearer {token()}"}
    async with aiohttp.ClientSession(BASE, headers=auth) as http:
        entries = json.load(open(ENTRIES))["data"]["entries"]
        if any(x["domain"] == "nut" and x["data"].get("host") == HOST for x in entries):
            print("integration: already configured")
        else:
            async with http.post(
                "/api/config/config_entries/flow", json={"handler": "nut"}
            ) as r:
                flow = await r.json()
            async with http.post(
                f"/api/config/config_entries/flow/{flow['flow_id']}",
                json={"host": HOST, "port": PORT},
            ) as r:
                result = await r.json()
            print("integration:", result.get("type"), result.get("reason"))
            if result.get("type") != "create_entry":
                return 1

        async with http.ws_connect("/api/websocket") as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": auth["Authorization"][7:]})
            await ws.receive_json()
            msg_id = 0

            async def call(**msg):
                nonlocal msg_id
                msg_id += 1
                await ws.send_json({"id": msg_id, **msg})
                while (reply := await ws.receive_json()).get("id") != msg_id:
                    pass
                if not reply["success"]:
                    raise RuntimeError(f"{msg['type']}: {reply['error']}")
                return reply["result"]

            # unique_id is "<model>_<serial>_<variable>"; the variable has no "_".
            entities = {}
            for ent in await call(type="config/entity_registry/list"):
                if ent["platform"] != "nut":
                    continue
                key = ent["unique_id"].rsplit("_", 1)[-1]
                entities[key] = ent["entity_id"]
                want = None if key not in NOISE else "user"
                if (ent["disabled_by"] is None) != (want is None):
                    await call(
                        type="config/entity_registry/update",
                        entity_id=ent["entity_id"],
                        disabled_by=want,
                    )
                    print("entity:", ent["entity_id"], "disabled" if want else "enabled")

            existing = {d["url_path"] for d in await call(type="lovelace/dashboards/list")}
            if DASHBOARD not in existing:
                await call(
                    type="lovelace/dashboards/create",
                    url_path=DASHBOARD,
                    title="SAI",
                    icon="mdi:battery-charging-high",
                    show_in_sidebar=True,
                    require_admin=False,
                    mode="storage",
                )
                print("dashboard: created")
            await call(type="lovelace/config/save", url_path=DASHBOARD, config=dashboard(entities))
            print("dashboard: saved")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
