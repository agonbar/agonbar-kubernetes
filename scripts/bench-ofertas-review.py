#!/usr/bin/env python3
"""Read-only health report for bench "ofertas personalizadas" (the nightly forum scrape).

bench-enjin runs ForumBatchWorkflow every night at 03:00 Madrid; each run leaves one
ForumScrapeBatch and the offers it extracted as PersonalizedOffer rows in bench-cloud. This reads
those two collections plus the forum sources and prints: the batches of the last --days days, the
offers saved per day, which active sources produced nothing, the review backlog, and the best
offers of the last --top-days days per product type.

bench-cloud wants a JWT, so the script logs in with the worker's own account, read from the
bench-enjin secret through kubectl. It only issues GETs.

  scripts/bench-ofertas-review.py [--days 30] [--top-days 7] [--top 8]
      [--context sensei-ovh] [--namespace dev] [--url https://bench-cloud.dev.senseizero.es]
"""
import argparse
import base64
import collections
import datetime as dt
import json
import subprocess
import urllib.parse
import urllib.request

PRODUCT = {0: "hipoteca", 1: "préstamo", 2: "tarjeta", 3: "depósito/cuenta"}
# For a deposit the saver wants the highest rate; for anything borrowed, the lowest.
HIGHER_IS_BETTER = {3}
PAGE = 2000


def secret(ctx, ns, key):
    out = subprocess.run(
        ["kubectl", f"--context={ctx}", "-n", ns, "get", "secret", "bench-enjin-external-env",
         "-o", f"jsonpath={{.data.{key}}}"],
        check=True, capture_output=True, text=True).stdout
    return base64.b64decode(out).decode()


class Cloud:
    def __init__(self, url, user, pwd):
        self.base = url.rstrip("/") + "/api/v1"
        body = json.dumps({"username": user, "password": pwd}).encode()
        req = urllib.request.Request(self.base + "/Accounts/login", data=body,
                                     headers={"Content-Type": "application/json"})
        self.token = json.load(urllib.request.urlopen(req, timeout=30))["id"]

    def get(self, path, flt=None):
        url = f"{self.base}/{path}"
        if flt:
            url += "?filter=" + urllib.parse.quote(json.dumps(flt))
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + self.token})
        return json.load(urllib.request.urlopen(req, timeout=120))

    def all(self, path, order):
        rows = []
        while True:
            page = self.get(path, {"order": [order], "limit": PAGE, "skip": len(rows)})
            rows += page
            if len(page) < PAGE:
                return rows


def ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def rate(o):
    return o.get("tae") if o.get("tae") is not None else o.get("tin")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--top-days", type=int, default=7)
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--context", default="sensei-ovh")
    ap.add_argument("--namespace", default="dev")
    ap.add_argument("--url", default="https://bench-cloud.dev.senseizero.es")
    a = ap.parse_args()

    cloud = Cloud(a.url, secret(a.context, a.namespace, "WST_ADMIN_USERNAME"),
                  secret(a.context, a.namespace, "WST_ADMIN_PWD"))
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=a.days)

    batches = cloud.all("forum-scrape-batches", "created DESC")
    sources = cloud.get("forum-sources")
    offers = cloud.all("personalized-offers", "created DESC")
    scraped = [o for o in offers if o.get("inputChannel") == "FORUM_SCRAPE"]
    recent = [o for o in scraped if ts(o["created"]) >= since]

    print(f"# {a.url}  ({now:%Y-%m-%d %H:%M} UTC)")
    print(f"ofertas: {len(offers)} en total, {len(scraped)} de scrapeo, {len(recent)} en {a.days} días")
    print(f"fuentes: {len(sources)}, activas {sum(1 for s in sources if s.get('active'))}")
    print(f"batches: {len(batches)}, el último {batches[0]['created'][:16] if batches else 'ninguno'}")

    by_batch = collections.Counter(o.get("batchId") for o in scraped)
    print(f"\n## Batches, últimos {a.days} días")
    print(f"{'inicio (UTC)':17} {'estado':10} {'ok/fallo/total':>14} {'duración':>9} {'ofertas':>8}")
    days_seen = set()
    for b in batches:
        start = ts(b["created"])
        if start < since:
            break
        days_seen.add(start.date())
        mins = (ts(b["modified"]) - start).total_seconds() / 60
        srcs = f"{b.get('completedSources', 0)}/{b.get('failedSources', 0)}/{b.get('totalSources', 0)}"
        print(f"{start:%Y-%m-%d %H:%M}  {b.get('status', '?'):10} {srcs:>14} {mins:8.0f}m {by_batch[b['id']]:8}")
    missing = [d for d in (now.date() - dt.timedelta(days=i) for i in range(a.days))
               if d not in days_seen]
    print("días sin batch:", ", ".join(str(d) for d in sorted(missing)) or "ninguno")

    print(f"\n## Ofertas guardadas por día")
    per_day = collections.defaultdict(collections.Counter)
    for o in recent:
        c = per_day[o["created"][:10]]
        c["n"] += 1
        c[(o.get("realism") or {}).get("verdict") or "sin veredicto"] += 1
        c["alerta"] += bool(o.get("alertWorthy"))
    verdicts = sorted({k for c in per_day.values() for k in c} - {"n", "alerta"})
    print(f"{'día':10} {'n':>4} {'alerta':>6}  " + "  ".join(verdicts))
    for day in sorted(per_day, reverse=True):
        c = per_day[day]
        print(f"{day} {c['n']:4} {c['alerta']:6}  " + "  ".join(f"{c[v]:>{len(v)}}" for v in verdicts))

    print(f"\n## Fuentes activas, ofertas en {a.days} días")
    by_src = collections.Counter(o.get("sourceType") for o in recent)
    ever = collections.Counter(o.get("sourceType") for o in scraped)
    last = {}
    for o in scraped:  # newest first, so the first one seen per source is its latest
        last.setdefault(o.get("sourceType"), o["created"][:10])
    for s in sorted((s for s in sources if s.get("active")), key=lambda s: -by_src[s["name"]]):
        seen = ts(s.get("lastScannedAt"))
        age = f"{(now - seen).total_seconds() / 3600:5.0f}h" if seen else "nunca"
        print(f"{by_src[s['name']]:4} ({ever[s['name']]:4} hist., última {last.get(s['name'], 'nunca'):10})"
              f"  escaneada hace {age}  {s['name']}")

    print("\n## Estado de revisión (todas las de scrapeo)")
    for k, n in collections.Counter(o.get("status") for o in scraped).most_common():
        print(f"{n:5}  {k}")
    for label, field in (("naturaleza", "offerNature"), ("confianza", "confidence")):
        print(f"{label}:", ", ".join(f"{k}={n}" for k, n in
                                     collections.Counter(o.get(field) for o in recent).most_common()))

    top_since = now - dt.timedelta(days=a.top_days)
    fresh = [o for o in recent if ts(o["created"]) >= top_since]
    print(f"\n## Lo mejor de los últimos {a.top_days} días ({len(fresh)} ofertas)")
    groups = [(name, [o for o in fresh if o.get("productType") == pt and rate(o) is not None],
               pt in HIGHER_IS_BETTER) for pt, name in PRODUCT.items()]
    # What people say they negotiated for themselves, as opposed to a published rate.
    groups.append(("negociadas (PERSONALIZED), las más recientes",
                   [o for o in fresh if o.get("offerNature") == "PERSONALIZED"], None))
    for name, rows, descending in groups:
        if descending is not None:
            rows.sort(key=rate, reverse=descending)
        print(f"\n### {name}: {len(rows)}")
        for o in rows[:a.top]:
            r = o.get("realism") or {}
            dur = f"{o['duration']} {o.get('durationUnit', '')}".lower() if o.get("duration") else "-"
            print(f"  TAE {o.get('tae', '-')} TIN {o.get('tin', '-')}  {o.get('bankName') or '?'}"
                  f"  importe {o.get('amount', '-')}  plazo {dur}"
                  f"  [{r.get('verdict', '?')} {r.get('score', '-')}, {o.get('confidence')},"
                  f" {o.get('offerNature')}{', ALERTA' if o.get('alertWorthy') else ''}]")
            print(f"      {o.get('postDate', '?')}  {o.get('sourceReference')}")
            for line in (o.get("alertReasons") or []) + (o.get("bonifications") or [])[:3]:
                print(f"      - {line}")

if __name__ == "__main__":
    main()
