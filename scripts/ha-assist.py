"""Expose entities to Home Assistant Assist and query the conversation agent.

Runs INSIDE the HA pod (neither aya nor lamg has a long-lived token, so it mints
a short-lived JWT from an existing refresh_token in /config/.storage/auth).

    kubectl --context=lamg -n <ns> exec -i deploy/homeassistant -- \
        python3 - --check weather.forecast_home < scripts/ha-assist.py

Modes:
    <entity_id> [...]        expose those entities to the "conversation" assistant
    --check <entity_id>      print current exposure
    --ask "<frase>" [agent]  run a sentence through the agent (default: built-in)
"""
import asyncio, json, sys, time, jwt, aiohttp

STORAGE = "/config/.storage/"
URL = "http://127.0.0.1:8123"


def mint_token():
    auth = json.load(open(STORAGE + "auth"))["data"]
    # any refresh_token carrying a jwt_key works; avoid system tokens
    cands = [r for r in auth["refresh_tokens"] if r.get("jwt_key") and r.get("token_type") != "system"]
    if not cands:
        raise SystemExit("no usable refresh_token with a jwt_key")
    rt = sorted(cands, key=lambda r: r.get("last_used_at") or "", reverse=True)[0]
    now = int(time.time())
    return jwt.encode({"iss": rt["id"], "iat": now, "exp": now + 900}, rt["jwt_key"], algorithm="HS256")


async def ws_call(messages):
    token = mint_token()
    out = []
    async with aiohttp.ClientSession() as s, s.ws_connect(URL + "/api/websocket") as ws:
        await ws.receive_json()  # auth_required
        await ws.send_json({"type": "auth", "access_token": token})
        if (await ws.receive_json())["type"] != "auth_ok":
            raise SystemExit("auth failed")
        for i, msg in enumerate(messages, start=1):
            await ws.send_json({"id": i, **msg})
            while True:
                r = await ws.receive_json()
                if r.get("id") == i and r["type"] == "result":
                    out.append(r)
                    break
    return out


def main():
    args = sys.argv[1:]
    if args and args[0] == "--check":
        r = asyncio.run(ws_call([{"type": "homeassistant/expose_entity/list"}]))[0]
        exposed = r["result"]["exposed_entities"]
        for eid in args[1:]:
            print(eid, "->", json.dumps(exposed.get(eid, "NOT LISTED")))
    elif args and args[0] == "--ask":
        agent = args[2] if len(args) > 2 else None
        payload = {"type": "conversation/process", "text": args[1], "language": "es"}
        if agent:
            payload["agent_id"] = agent
        r = asyncio.run(ws_call([payload]))[0]
        if not r["success"]:
            print("ERROR", json.dumps(r.get("error")))
            return
        resp = r["result"]["response"]
        print("type:", resp["response_type"])
        print("speech:", resp["speech"]["plain"]["speech"])
    else:
        r = asyncio.run(ws_call([{
            "type": "homeassistant/expose_entity",
            "entity_ids": args,
            "assistants": ["conversation"],
            "should_expose": True,
        }]))[0]
        print("expose success:", r["success"], json.dumps(r.get("error", {})))


main()
