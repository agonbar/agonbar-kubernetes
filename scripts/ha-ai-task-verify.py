"""Verify the ai_task platform end to end, from inside a Home Assistant container.

Mints a five-minute token from an existing refresh token, creates an AI task
subentry on the LiteLLM config entry if there is none, and then runs
ai_task.generate_data with a structure, so the answer has to be valid JSON
instead of prose the model narrated.

    python3 ha-ai-task-verify.py <model> [<image-model>]
"""

import json
import sys
import time

import httpx
import jwt

BASE = "http://127.0.0.1:8123"
AUTH = "/config/.storage/auth"
ENTRIES = "/config/.storage/core.config_entries"
STRUCTURE = {
    "habitacion": {
        "selector": {"text": None},
        "description": "Room the light is in",
        "required": True,
    },
    "encendida": {
        "selector": {"boolean": None},
        "description": "Whether the light is on",
        "required": True,
    },
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


def main(model: str, image_model: str | None) -> int:
    client = httpx.Client(
        base_url=BASE,
        headers={"Authorization": f"Bearer {token()}"},
        timeout=300.0,
    )

    # Right after a restart the entry is still setting up, and the subentry
    # flow aborts with entry_not_loaded instead of showing the form.
    for _attempt in range(60):
        entry = next(
            e
            for e in client.get("/api/config/config_entries/entry").json()
            if e["domain"] == "litellm"
        )
        if entry["state"] == "loaded":
            break
        time.sleep(2)
    else:
        print(f"FALLA: la entrada sigue en {entry['state']}: {entry.get('reason')}")
        return 1
    print(f"entry: {entry['title']} ({entry['entry_id']})")
    # Core only offers "conversation"; seeing ai_task_data proves the custom
    # integration is the one that loaded.
    print(f"subentry types: {sorted(entry.get('supported_subentry_types', {}))}")

    # The REST payload of a config entry carries no subentries, so read them
    # from storage. Skipping this check creates a duplicate on every run.
    stored = next(
        e
        for e in json.load(open(ENTRIES))["data"]["entries"]
        if e["entry_id"] == entry["entry_id"]
    )
    existing = [
        s for s in stored["subentries"] if s["subentry_type"] == "ai_task_data"
    ]
    if existing:
        print(f"subentry ai_task_data ya existe: {existing[0]['title']}")
    else:
        flow = client.post(
            "/api/config/config_entries/subentries/flow",
            json={"handler": [entry["entry_id"], "ai_task_data"]},
        ).json()
        # An abort still carries a flow_id, so check the type: right after a
        # restart the entry is not LOADED yet and the flow aborts on the spot.
        if flow.get("type") != "form":
            print(f"FALLA al abrir el flow: {flow.get('type')} {flow.get('reason', flow)}")
            return 1
        print(f"flow abierto, campos: {[f['name'] for f in flow['data_schema']]}")
        payload = {"model": model}
        if image_model:
            payload["image_model"] = image_model
        result = client.post(
            f"/api/config/config_entries/subentries/flow/{flow['flow_id']}",
            json=payload,
        ).json()
        if result.get("type") != "create_entry":
            print(f"FALLA al crear el subentry: {result}")
            return 1
        print(f"subentry creado: {result['title']}")

    # Adding a subentry reloads the config entry, which re-runs the coordinator
    # against the proxy, so the entity shows up a few seconds later.
    for _attempt in range(30):
        entities = [
            s
            for s in client.get("/api/states").json()
            if s["entity_id"].startswith("ai_task.")
        ]
        if entities:
            break
        time.sleep(2)
    else:
        print("FALLA: la plataforma no ha creado ninguna entidad")
        return 1

    for state in entities:
        features = state["attributes"].get("supported_features")
        print(f"entidad: {state['entity_id']} | supported_features={features}")
    entities = [state["entity_id"] for state in entities]

    print("\nai_task.generate_data con structure:")
    started = time.monotonic()
    response = client.post(
        "/api/services/ai_task/generate_data?return_response=true",
        json={
            "task_name": "verificacion",
            "entity_id": entities[0],
            "instructions": "La luz del salon esta encendida.",
            "structure": STRUCTURE,
        },
    )
    elapsed = time.monotonic() - started
    if response.status_code != 200:
        print(f"    FALLA {response.status_code}: {response.text[:400]}")
        return 1
    data = response.json().get("service_response", {}).get("data")
    print(f"    {elapsed:.1f}s -> {data!r} ({type(data).__name__})")
    return 0 if isinstance(data, dict) else 1


sys.exit(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
