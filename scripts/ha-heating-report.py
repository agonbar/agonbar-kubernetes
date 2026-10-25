"""Per-room heating consumption and cost from the lamg Home Assistant recorder.

Runs INSIDE the HA pod (mints a short-lived JWT like ha-assist.py):

    kubectl --context=lamg -n lamg exec -i deploy/homeassistant -c homeassistant -- \
        python3 - [START END] < scripts/ha-heating-report.py

START/END are YYYY-MM-DD (END exclusive); default is the last 14 days.

Each room has a Xiaomi plug (sensor.calefaccion_<room>_energy) driven by a
climate.termostato_<room>. Cost is priced hour by hour with the PVPC price
statistic (sensor.esios_pvpc, EUR/kWh) and, for comparison, the flat 0.20
EUR/kWh the energy dashboard uses. Heat loss (W/K) is heating energy divided by
the indoor-outdoor degree-hours, outdoor = the balcony thermometer: it ranks
rooms by how much heat they lose per degree, which is what the setpoint costs.
"""
import asyncio, json, sys, time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import aiohttp, jwt

STORAGE = "/config/.storage/"
URL = "http://127.0.0.1:8123"
ROOMS = ["salon", "entrada", "pasillo", "estudio", "habitacion", "dormitorio", "aseo", "bano", "cocina"]
OUTDOOR = "sensor.termometro_balcon_temperature"
PRICE = "sensor.esios_pvpc"
TOTAL = "sensor.consumo_electrico_total_energy"
FLAT = 0.20
TZ = ZoneInfo("Europe/Madrid")


def period(ms):
    """2.0TD period of the hour starting at ms: valle 0-8 and weekends, punta 10-14 and 18-22."""
    t = datetime.fromtimestamp(ms / 1000, TZ)
    if t.weekday() >= 5 or t.hour < 8:
        return "valle"
    return "punta" if 10 <= t.hour < 14 or 18 <= t.hour < 22 else "llano"


def mint_token():
    auth = json.load(open(STORAGE + "auth"))["data"]
    admins = {u["id"] for u in auth["users"] if "system-admin" in u.get("group_ids", [])}
    cands = [r for r in auth["refresh_tokens"]
             if r.get("jwt_key") and r.get("token_type") != "system" and r["user_id"] in admins]
    rt = sorted(cands, key=lambda r: r.get("last_used_at") or "", reverse=True)[0]
    now = int(time.time())
    return jwt.encode({"iss": rt["id"], "iat": now, "exp": now + 900}, rt["jwt_key"], algorithm="HS256")


async def fetch(start, end):
    ids_change = [f"sensor.calefaccion_{r}_energy" for r in ROOMS] + [TOTAL]
    ids_mean = [f"sensor.termometro_{r}_temperature" for r in ROOMS] + [OUTDOOR, PRICE]
    ids_max = [f"sensor.calefaccion_{r}_power" for r in ROOMS]
    reqs = [(ids_change, ["change"]), (ids_mean, ["mean"]), (ids_max, ["max"])]
    out = {}
    async with aiohttp.ClientSession() as s, s.ws_connect(URL + "/api/websocket", max_msg_size=0) as ws:
        await ws.receive_json()
        await ws.send_json({"type": "auth", "access_token": mint_token()})
        await ws.receive_json()
        for i, (ids, types) in enumerate(reqs, start=1):
            await ws.send_json({"id": i, "type": "recorder/statistics_during_period",
                                "start_time": start.isoformat(), "end_time": end.isoformat(),
                                "statistic_ids": ids, "period": "hour", "types": types})
        while len(out) < len(reqs):
            r = await ws.receive_json()
            if r.get("type") == "result":
                out[r["id"]] = r["result"]
    merged = {}
    for res in out.values():
        merged.update(res)
    return merged


def hourly(stats, sid, key):
    return {row["start"]: row[key] for row in stats.get(sid, []) if row.get(key) is not None}


def main():
    if len(sys.argv) == 3:
        start = datetime.fromisoformat(sys.argv[1]).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(sys.argv[2]).replace(tzinfo=timezone.utc)
    else:
        end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(days=14)
    days = (end - start).total_seconds() / 86400
    st = asyncio.run(fetch(start, end))

    price = hourly(st, PRICE, "mean")
    outdoor = hourly(st, OUTDOOR, "mean")
    total_kwh = sum(v for v in hourly(st, TOTAL, "change").values() if v > 0)
    avg_price = sum(price.values()) / len(price) if price else None

    rows = []
    by_period = defaultdict(float)
    for r in ROOMS:
        kwh_h = hourly(st, f"sensor.calefaccion_{r}_energy", "change")
        tin = hourly(st, f"sensor.termometro_{r}_temperature", "mean")
        pmax = max(hourly(st, f"sensor.calefaccion_{r}_power", "max").values(), default=0)
        kwh = eur = deg_h = 0.0
        unpriced = 0
        for t, v in kwh_h.items():
            if v <= 0:
                continue  # counter resets show up as negative changes
            kwh += v
            by_period[period(t)] += v
            if t in price:
                eur += v * price[t]
            else:
                unpriced += v
        if avg_price:
            eur += unpriced * avg_price
        for t, ti in tin.items():
            if t in outdoor and ti > outdoor[t]:
                deg_h += ti - outdoor[t]
        t_in = sum(tin.values()) / len(tin) if tin else float("nan")
        ua = kwh * 1000 / deg_h if deg_h else float("nan")
        hours_on = kwh * 1000 / pmax if pmax else float("nan")
        rows.append((r, kwh / days, eur / days, kwh * FLAT / days, t_in, ua, pmax, hours_on / days, kwh))

    rows.sort(key=lambda x: -x[2])
    t_out = sum(outdoor.values()) / len(outdoor) if outdoor else float("nan")
    print(f"Periodo {start:%Y-%m-%d} -> {end:%Y-%m-%d} ({days:.1f} dias)  T exterior media {t_out:.1f} C"
          f"  PVPC medio {avg_price or float('nan'):.3f} EUR/kWh  ({len(price)} h con precio)")
    print(f"{'habitacion':<11}{'kWh/dia':>8}{'EUR/dia PVPC':>14}{'EUR/dia 0,20':>14}{'T media':>9}"
          f"{'perdida W/K':>12}{'radiador W':>11}{'h/dia on':>9}{'% calef':>8}")
    heat = sum(x[8] for x in rows)
    for r, kd, ed, fd, ti, ua, pm, hd, k in rows:
        print(f"{r:<11}{kd:>8.2f}{ed:>14.2f}{fd:>14.2f}{ti:>9.1f}{ua:>12.1f}{pm:>11.0f}{hd:>9.1f}"
              f"{(100 * k / heat if heat else 0):>7.0f}%")
    print(f"{'TOTAL':<11}{heat / days:>8.2f}{sum(x[2] for x in rows):>14.2f}{heat * FLAT / days:>14.2f}")
    if heat:
        print("Calefaccion por periodo 2.0TD: " + ", ".join(
            f"{k} {100 * by_period[k] / heat:.0f}%" for k in ("valle", "llano", "punta")))
    if total_kwh:
        print(f"Casa entera {total_kwh / days:.2f} kWh/dia; calefaccion = {100 * heat / total_kwh:.0f}% del total")


main()
