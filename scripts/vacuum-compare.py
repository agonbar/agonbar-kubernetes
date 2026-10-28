#!/usr/bin/env python3
"""Run a full clean on two Xiaomi Gen1 vacuums one after the other and compare them.

One robot runs Valetudo (HTTP API), the other stock firmware (local miio with a
token). Both are set to fan power 60 (Valetudo "medium"). Every 30 s it logs
battery, state, cleaning time, area and error to a CSV per robot, keeps logging
while each one recharges to 100 %, and writes report.md at the end.

Meant to run detached on a host in the robots' LAN, e.g.:

  systemd-run --user --unit vacuum-compare \\
    <python-with-python-miio> vacuum-compare.py \\
      --valetudo 192.168.1.237 --stock-ip 192.168.1.204 \\
      --stock-token-file ~/valetudo-v1/extractor/out.json --out ~/vacuum-compare
"""
import argparse, csv, datetime as dt, json, os, threading, time, urllib.request

from miio import RoborockVacuum

POLL = 30
CLEAN_TIMEOUT = 3 * 3600
CHARGE_TIMEOUT = 5 * 3600


class Valetudo:
    name = "valetudo"

    def __init__(self, ip):
        self.base = f"http://{ip}/api/v2/robot"

    def _req(self, path, body=None, method=None):
        req = urllib.request.Request(self.base + path, method=method or ("PUT" if body else "GET"),
                                     data=json.dumps(body).encode() if body else None,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return json.loads(raw) if raw and raw[:1] in b"[{" else raw.decode()

    def prepare(self):
        self._req("/capabilities/FanSpeedControlCapability/preset", {"name": "medium"})

    def start(self):
        self._req("/capabilities/BasicControlCapability", {"action": "start"})

    def dock(self):
        self._req("/capabilities/BasicControlCapability", {"action": "home"})

    def sample(self):
        attrs = self._req("/state/attributes")
        st = next(a for a in attrs if a["__class"] == "StatusStateAttribute")
        bat = next(a for a in attrs if a["__class"] == "BatteryStateAttribute")
        stats = {s["type"]: s["value"] for s in self._req("/capabilities/CurrentStatisticsCapability")}
        state = st["value"]
        return {"state": state, "battery": bat["level"], "clean_s": stats.get("time", 0),
                "area_m2": round(stats.get("area", 0) / 10000, 2), "error": st.get("metaData", {}).get("error_description", "") if state == "error" else "",
                "phase": {"cleaning": "cleaning", "returning": "returning", "docked": "docked"}.get(state, state)}

    def save_map(self, path):
        with open(path, "w") as f:
            json.dump(self._req("/state/map"), f)


class Stock:
    name = "stock"

    def __init__(self, ip, token):
        self.v = RoborockVacuum(ip, token, model="rockrobo.vacuum.v1")

    def prepare(self):
        self.v.set_fan_speed(60)

    def start(self):
        self.v.start()

    def dock(self):
        self.v.home()

    def sample(self):
        s = self.v.status()
        state = s.state
        phase = ("cleaning" if s.is_on else "returning" if s.state_code == 6
                 else "docked" if s.state_code in (8, 100) else state.lower())
        return {"state": state, "battery": s.battery, "clean_s": int(s.clean_time.total_seconds()),
                "area_m2": round(s.clean_area, 2), "error": s.error if s.got_error else "", "phase": phase}

    def save_map(self, path):
        pass  # the stock map only lives in the Xiaomi cloud


def log_until(robot, writer, fh, t0, stop, timeout):
    """Poll until stop(sample) is true or timeout; return the last sample."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            s = robot.sample()
        except Exception as e:  # transient network/miio errors: log and keep going
            s = dict(last or {}, state=f"poll error: {e}"[:80])
        s["t_min"] = round((time.time() - t0) / 60, 1)
        s["at"] = dt.datetime.now().isoformat(timespec="seconds")
        writer.writerow(s); fh.flush()
        last = s
        if stop(s):
            return s
        time.sleep(POLL)
    return dict(last or {}, timeout=True)


def run(robot, out, results, cleaning_done):
    try:
        _run(robot, out, results, cleaning_done)
    except Exception as e:
        results[robot.name + "_crash"] = repr(e)
    finally:
        cleaning_done.set()  # never leave the next robot waiting


def _run(robot, out, results, cleaning_done):
    fields = ["at", "t_min", "phase", "state", "battery", "clean_s", "area_m2", "error"]
    with open(os.path.join(out, f"{robot.name}.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fields, extrasaction="ignore"); w.writeheader()
        start = robot.sample()
        robot.prepare(); robot.start()
        t0 = time.time()
        seen_clean = {"v": False}

        def back_home(s):
            seen_clean["v"] |= s.get("phase") == "cleaning"
            return seen_clean["v"] and s.get("phase") == "docked"

        end = log_until(robot, w, fh, t0, back_home, CLEAN_TIMEOUT)
        if end.get("timeout"):
            robot.dock()
        t_clean = time.time()
        robot.save_map(os.path.join(out, f"{robot.name}-map.json"))
        cleaning_done.set()
        full = log_until(robot, w, fh, t_clean, lambda s: (s.get("battery") or 0) >= 100, CHARGE_TIMEOUT)
        results[robot.name] = {
            "battery_start": start["battery"], "battery_end": end.get("battery"),
            "clean_min": round((t_clean - t0) / 60, 1), "robot_clean_min": round((end.get("clean_s") or 0) / 60, 1),
            "area_m2": end.get("area_m2"), "errors": sorted({r for r in [end.get("error")] if r}),
            "recharge_min": round((time.time() - t_clean) / 60, 1), "recharge_complete": not full.get("timeout"),
            "clean_timeout": bool(end.get("timeout")),
        }


def report(out, results):
    lines = ["# Vacuum comparison", "", f"Generated {dt.datetime.now().isoformat(timespec='minutes')}", "",
             "| | " + " | ".join(results) + " |", "|---|" + "---|" * len(results)]
    rows = [("Battery start → end (%)", lambda r: f"{r['battery_start']} → {r['battery_end']}"),
            ("Wall-clock cleaning (min)", lambda r: r["clean_min"]),
            ("Robot-reported cleaning time (min)", lambda r: r["robot_clean_min"]),
            ("Area (m²)", lambda r: r["area_m2"]),
            ("Battery used (%)", lambda r: r["battery_start"] - (r["battery_end"] or 0)),
            ("Drain (% per min of cleaning)", lambda r: round((r["battery_start"] - (r["battery_end"] or 0)) / max(r["robot_clean_min"], 0.1), 2)),
            ("Area per % battery (m²)", lambda r: round((r["area_m2"] or 0) / max(r["battery_start"] - (r["battery_end"] or 0), 1), 2)),
            ("Extrapolated runtime 100→0 % (min)", lambda r: round(100 * max(r["robot_clean_min"], 0.1) / max(r["battery_start"] - (r["battery_end"] or 0), 1))),
            ("Recharge to 100 % (min)", lambda r: f"{r['recharge_min']}{'' if r['recharge_complete'] else ' (timed out)'}"),
            ("Errors", lambda r: ", ".join(r["errors"]) or "none"),
            ("Hit cleaning timeout", lambda r: r["clean_timeout"])]
    for label, f in rows:
        lines.append(f"| {label} | " + " | ".join(str(f(r)) for r in results.values()) + " |")
    with open(os.path.join(out, "report.md"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    with open(os.path.join(out, "results.json"), "w") as fh:
        json.dump(results, fh, indent=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--valetudo", required=True)
    ap.add_argument("--stock-ip", required=True)
    ap.add_argument("--stock-token-file", required=True, help="token extractor out.json")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    data = json.load(open(os.path.expanduser(a.stock_token_file)))
    token = next(x["token"] for s in data for h in s["homes"] for x in h["devices"] if x["localip"] == a.stock_ip)
    out = os.path.join(os.path.expanduser(a.out), dt.datetime.now().strftime("%Y%m%d-%H%M"))
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "STATUS"), "w") as fh:
        fh.write("running\n")
    results = {}
    robots = [Valetudo(a.valetudo), Stock(a.stock_ip, token)]
    threads = []
    for r in robots:  # next robot starts once the previous one is back on its dock
        done = threading.Event()
        t = threading.Thread(target=run, args=(r, out, results, done)); t.start(); threads.append(t)
        done.wait()
    for t in threads:
        t.join()
    report(out, {r.name: results[r.name] for r in robots if r.name in results})
    crashes = {k: v for k, v in results.items() if k.endswith("_crash")}
    if crashes:
        with open(os.path.join(out, "report.md"), "a") as fh:
            fh.write("\nCrashes: " + json.dumps(crashes) + "\n")
    with open(os.path.join(out, "STATUS"), "w") as fh:
        fh.write("done\n")


if __name__ == "__main__":
    main()
