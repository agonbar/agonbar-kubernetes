#!/usr/bin/env python3
"""Builds the client delivery sheet for bench "ofertas personalizadas" (A4, same look as the ESG
coverage delivery in senseivision/reports).

It compares every verified personal mortgage offer (PersonalizedOffer.alertWorthy, fixed rate, post
dated 2025 or later) with the best bonified fixed TIN the same bank published on the post date,
taken from the catalogue history (Mortgage FIXED -> Plan bonus=true -> Term.created).

Inputs are two JSON exports from hip-cloud (MongoDB MCP `export`, relaxed EJSON):
  offers.json   PersonalizedOffer, find {} (all fields)
  catalog.json  Mortgage aggregate:
                [{$match:{subtypeForAiChatbot:"FIXED"}},
                 {$lookup:{from:"Bank",localField:"bankId",foreignField:"_id",as:"b"}},
                 {$lookup:{from:"Plan",localField:"_id",foreignField:"mortgageId",as:"p"}},{$unwind:"$p"},
                 {$lookup:{from:"Term",localField:"p._id",foreignField:"planId",as:"t"}},{$unwind:"$t"},
                 {$match:{"t.tin":{$gt:0},"t.created":{$gte:ISODate("2024-06-01")}}},
                 {$group:{_id:{b:{$first:"$b.name"},bonus:"$p.bonus",
                          d:{$dateToString:{format:"%Y-%m-%d",date:"$t.created"}}},min:{$min:"$t.tin"}}},
                 {$project:{_id:0,b:"$_id.b",bonus:"$_id.bonus",d:"$_id.d",tin:"$min"}}]
plus the hand-kept scripts/bench-entrega-ofertas.content.json (examples, source table, next steps,
limits).

  scripts/bench-entrega-ofertas.py offers.json catalog.json scripts/bench-entrega-ofertas.content.json \
      --date 2026-10-20 > index.html
"""
import argparse
import bisect
import collections
import datetime as dt
import html
import json
import statistics as st

ALIAS = {"Banco Santander": "Santander", "Banco Sabadell": "Sabadell", "Caixabank": "CaixaBank",
         "KutxaBank": "Kutxabank", "ABANCA": "Abanca", "Unicaja": "Unicaja Banco",
         "Ibercaja": "IberCaja"}
SHOW = {"Unicaja Banco": "Unicaja", "IberCaja": "Ibercaja"}
MAX_CATALOG_AGE_DAYS = 120
BLUE, ORANGE, GREEN, GREY, AXIS, INK, MUTED = ("#2a78d6", "#eb6834", "#1baf7a", "#9aa3b2",
                                              "#c3c2b7", "#0b0b0b", "#52514e")
FONT = 'font-family="Inter, Helvetica Neue, Helvetica, Arial, sans-serif"'
MESES = "ene feb mar abr may jun jul ago sep oct nov dic".split()


def es(x, nd=2):
    return f"{x:.{nd}f}".replace(".", ",")


def miles(n):
    return f"{n:,}".replace(",", ".")


def pct(a, b):
    return f"{round(100 * a / b)} %"


def esc(s):
    return html.escape(str(s))


def comparables(offers, catalog):
    hist = collections.defaultdict(list)
    for c in catalog:
        if c.get("bonus") is True:
            hist[c["b"]].append((c["d"], c["tin"]))
    for h in hist.values():
        h.sort()

    def published(bank, day):
        h = hist.get(bank)
        if not h:
            return None
        i = bisect.bisect_right(h, (day, 99.0)) - 1
        if i < 0:
            return None
        d, tin = h[i]
        if (dt.date.fromisoformat(day) - dt.date.fromisoformat(d)).days > MAX_CATALOG_AGE_DAYS:
            return None
        return tin

    rows, missing = [], collections.Counter()
    for o in offers:
        post = o.get("postDate") or ""
        # euribor > 0 means a variable offer; a fixed TIN is the only thing the catalogue band
        # here can be compared with.
        if (not o.get("alertWorthy") or o.get("productType") != 0 or post < "2025-01-01"
                or not o.get("tin") or (o.get("euribor") or 0) > 0):
            continue
        bank = ALIAS.get(o["bankName"], o["bankName"])
        pub = published(bank, post[:10])
        if pub is None:
            missing[bank] += 1
            continue
        rows.append({"bank": bank, "tin": o["tin"], "pub": pub, "post": post[:10],
                     "url": o.get("sourceReference", "")})
    return rows, missing


def dumbbell(stats):
    lo, hi = 1.8, 3.4
    left, right, row = 110, 600, 17
    h = 30 + row * len(stats)
    x = lambda v: left + (v - lo) / (hi - lo) * (right - left)
    out = [f'<svg viewBox="0 0 720 {h}" width="100%" role="img" {FONT}>']
    out.append(f'<circle cx="{left + 4}" cy="7" r="4" fill="{GREY}"/><text x="{left + 12}" y="10.5" '
               f'font-size="9.5" fill="{MUTED}">Mejor tipo publicado</text>'
               f'<circle cx="{left + 122}" cy="7" r="4" fill="{BLUE}"/><text x="{left + 130}" '
               f'y="10.5" font-size="9.5" fill="{MUTED}">Tipo de la oferta personal</text>')
    for t in (2.0, 2.5, 3.0):
        out.append(f'<line x1="{x(t):.1f}" y1="16" x2="{x(t):.1f}" y2="{h - 4}" stroke="#ecebe6"/>'
                   f'<text x="{x(t):.1f}" y="{h}" font-size="8" fill="{MUTED}" '
                   f'text-anchor="middle">{es(t, 1)} %</text>')
    for i, s in enumerate(stats):
        y = 24 + i * row
        bold = ' font-weight="700"' if s["bank"] == "Abanca" else ""
        out.append(f'<text x="{left - 6}" y="{y + 3.5}" font-size="10" fill="{INK}" '
                   f'text-anchor="end"{bold}>{esc(SHOW.get(s["bank"], s["bank"]))}</text>')
        out.append(f'<line x1="{x(s["tin"]):.1f}" y1="{y}" x2="{x(s["pub"]):.1f}" y2="{y}" '
                   f'stroke="{AXIS}" stroke-width="2.5"/>')
        out.append(f'<circle cx="{x(s["pub"]):.1f}" cy="{y}" r="4.2" fill="{GREY}"><title>'
                   f'Publicado: {es(s["pub"])} %</title></circle>')
        out.append(f'<circle cx="{x(s["tin"]):.1f}" cy="{y}" r="4.2" fill="{BLUE}"><title>'
                   f'Oferta personal: {es(s["tin"])} %</title></circle>')
        out.append(f'<text x="{right + 8}" y="{y + 3.5}" font-size="9.5" fill="{INK}" '
                   f'font-weight="600">{"−" if s["gap"] < 0 else "+"}{es(abs(s["gap"]))}</text>'
                   f'<text x="{right + 44}" y="{y + 3.5}" font-size="9" fill="{MUTED}">'
                   f'{s["n"]} ofertas</text>')
    out.append("</svg>")
    return "".join(out)


def monthly_lines(series):
    lo, hi = 1.8, 3.2
    left, right, top, bottom = 30, 330, 18, 92
    n = len(series)
    x = lambda i: left + i * (right - left) / max(1, n - 1)
    y = lambda v: bottom - (v - lo) / (hi - lo) * (bottom - top)
    out = [f'<svg viewBox="0 0 360 108" width="100%" role="img" {FONT}>']
    out.append(f'<rect x="{left}" y="2" width="9" height="9" rx="2" fill="{GREY}"/><text x="{left + 13}"'
               f' y="10.5" font-size="9.5" fill="{MUTED}">Mejor tipo publicado (mediana)</text>'
               f'<rect x="{left + 165}" y="2" width="9" height="9" rx="2" fill="{BLUE}"/><text '
               f'x="{left + 178}" y="10.5" font-size="9.5" fill="{MUTED}">Ofertas personales '
               f'(mediana)</text>')
    for t in (2.0, 2.5, 3.0):
        out.append(f'<line x1="{left}" y1="{y(t):.1f}" x2="{right}" y2="{y(t):.1f}" stroke="#ecebe6"/>'
                   f'<text x="{left - 4}" y="{y(t) + 3:.1f}" font-size="8" fill="{MUTED}" '
                   f'text-anchor="end">{es(t, 1)}</text>')
    for key, color in (("pub", GREY), ("tin", BLUE)):
        pts = " ".join(f"{x(i):.1f},{y(m[key]):.1f}" for i, m in enumerate(series))
        out.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2"/>')
        for i, m in enumerate(series):
            out.append(f'<circle cx="{x(i):.1f}" cy="{y(m[key]):.1f}" r="3" fill="{color}"><title>'
                       f'{m["label"]}: {es(m[key])} %</title></circle>')
    for i, m in enumerate(series):
        out.append(f'<text x="{x(i):.1f}" y="104" font-size="8.5" fill="{MUTED}" '
                   f'text-anchor="middle">{m["label"]} · {m["n"]}</text>')
    out.append("</svg>")
    return "".join(out)


def volume_bars(months, counts):
    left, right, base = 6, 354, 92
    peak = max(sum(c.values()) for c in counts.values()) or 1
    w = (right - left) / len(months)
    out = [f'<svg viewBox="0 0 360 108" width="100%" role="img" {FONT}>',
           f'<line x1="{left}" y1="{base}" x2="{right}" y2="{base}" stroke="{AXIS}"/>',
           f'<rect x="{left}" y="2" width="9" height="9" rx="2" fill="{BLUE}"/><text x="{left + 13}" '
           f'y="10.5" font-size="9.5" fill="{MUTED}">Mediavida</text><rect x="{left + 80}" y="2" '
           f'width="9" height="9" rx="2" fill="{ORANGE}"/><text x="{left + 93}" y="10.5" '
           f'font-size="9.5" fill="{MUTED}">Soloarquitectura</text>']
    for i, m in enumerate(months):
        c = counts.get(m, {})
        x0, yy = left + i * w + w * 0.18, base
        for src, color in (("mediavida", BLUE), ("soloarquitectura", ORANGE)):
            v = c.get(src, 0)
            if v:
                hh = v / peak * 64
                yy -= hh
                out.append(f'<rect x="{x0:.1f}" y="{yy:.1f}" width="{w * 0.64:.1f}" height="{hh:.1f}" '
                           f'fill="{color}"><title>{m} · {src}: {v}</title></rect>')
        tot = sum(c.values())
        if tot:
            out.append(f'<text x="{x0 + w * 0.32:.1f}" y="{yy - 3:.1f}" font-size="9" fill="{INK}" '
                       f'text-anchor="middle" font-weight="600">{tot}</text>')
        label = MESES[int(m[5:7]) - 1]
        out.append(f'<text x="{x0 + w * 0.32:.1f}" y="104" font-size="8" fill="{MUTED}" '
                   f'text-anchor="middle">{label}</text>')
    out.append("</svg>")
    return "".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("offers")
    ap.add_argument("catalog")
    ap.add_argument("content")
    ap.add_argument("--date", required=True, help="fecha de los datos, AAAA-MM-DD")
    a = ap.parse_args()
    offers = json.load(open(a.offers))
    catalog = json.load(open(a.catalog))
    content = json.load(open(a.content))

    rows, missing = comparables(offers, catalog)
    gaps = [r["tin"] - r["pub"] for r in rows]
    below = sum(g < 0 for g in gaps)
    by_bank = collections.defaultdict(list)
    for r in rows:
        by_bank[r["bank"]].append(r)
    stats = sorted(({"bank": b, "n": len(rs), "tin": st.median(r["tin"] for r in rs),
                     "pub": st.median(r["pub"] for r in rs),
                     "gap": st.median(r["tin"] - r["pub"] for r in rs)}
                    for b, rs in by_bank.items() if len(rs) >= 2), key=lambda s: -s["n"])
    abanca = next((s for s in stats if s["bank"] == "Abanca"), None)

    by_month = collections.defaultdict(list)
    for r in rows:
        if r["post"] >= "2026-01-01":
            by_month[r["post"][:7]].append(r)
    series = [{"label": MESES[int(m[5:7]) - 1], "n": len(rs),
               "tin": st.median(r["tin"] for r in rs), "pub": st.median(r["pub"] for r in rs)}
              for m, rs in sorted(by_month.items()) if len(rs) >= 3]

    personal = [o for o in offers if o.get("alertWorthy")]
    forum = [o for o in personal if o.get("inputChannel") != "FORM"]
    vol = collections.defaultdict(collections.Counter)
    for o in forum:
        p = (o.get("postDate") or "")[:7]
        if p >= "2026-03":
            host = o.get("sourceReference", "").split("/")[2] if "//" in o.get(
                "sourceReference", "") else ""
            vol[p]["soloarquitectura" if "soloarquitectura" in host else "mediavida"] += 1
    months = []
    d = dt.date(2026, 3, 1)
    last = max(vol)
    while d.strftime("%Y-%m") <= last:
        months.append(d.strftime("%Y-%m"))
        d = (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    banks_personal = {ALIAS.get(o["bankName"], o["bankName"]) for o in personal if o.get("bankName")}

    fecha = dt.date.fromisoformat(a.date)
    fecha_txt = f"{fecha.day} de {['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre'][fecha.month - 1]} de {fecha.year}"
    first = min(r["post"] for r in rows)
    lastp = max(r["post"] for r in rows)
    css = CSS

    ej = "".join(
        f'<tr><td><b>{esc(e["banco"])}</b></td><td class="n">{esc(e["tin"])}</td>'
        f'<td>{esc(e["condiciones"])}</td><td>{esc(e["estado"])}</td>'
        f'<td class="n"><a href="{esc(e["url"])}">{esc(e["fecha"])}</a></td></tr>'
        for e in content["ejemplos"])
    fu = "".join(
        f'<tr><td><b>{esc(f["fuente"])}</b></td><td>{esc(f["tipo"])}</td>'
        f'<td class="n">{esc(f["semana"])}</td><td>{esc(f["nota"])}</td></tr>'
        for f in content["fuentes"])
    pasos = "".join(f"<li>{p}</li>" for p in content["pasos"])
    limites = "".join(f"<li>{p}</li>" for p in content["limites"])

    abanca_txt = ""
    if abanca:
        abanca_txt = (f" En Abanca, las {abanca['n']} ofertas comparables se firmaron con una mediana "
                      f"de {es(abanca['tin'])} % frente al {es(abanca['pub'])} % publicado.")

    print(f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>Ofertas hipotecarias personalizadas · Mercado español</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
{css}
  .lead b {{ color: #111722; }}
  td a {{ color: #1f6f8b; text-decoration: none; }}
  ol.cadena {{ margin: 0; padding-left: 5mm; }}
  ol.cadena li {{ margin-bottom: 1.4mm; }}
  ol.cadena li::marker {{ font-weight: 600; color: #1f6f8b; }}
  .cols h3 {{ font-size: 9pt; margin: 0; color: #111722; }}
  .cols p {{ font-size: 8.5pt; line-height: 1.38; margin: .6mm 0 1.2mm; }}
</style>
</head>
<body>

<main class="hoja">
<div class="eyebrow">Benchmark bancario · Ofertas personalizadas</div>
<h1>Hipotecas: el tipo que se firma frente al tipo que se publica</h1>
<div class="meta">Mercado español · Hipotecas a tipo fijo · Ofertas de {first[:4]} y {lastp[:4]} · Datos a {fecha_txt}</div>

<p class="lead">Bench localiza en foros públicos las condiciones hipotecarias que los particulares
declaran haber recibido o firmado. Extrae el banco, el tipo, el importe y la fecha, y contrasta cada
oferta con el catálogo oficial que ese banco tenía publicado el mismo día. De {len(rows)} ofertas
comparables, <b>{below} ({pct(below, len(rows))}) se firmaron por debajo del mejor tipo fijo
bonificado que el propio banco anunciaba</b>, con una diferencia mediana de
{es(abs(st.median(gaps)))} puntos.{abanca_txt}</p>

<div class="tiles">
  <div class="tile"><b>{miles(len(offers))}</b><span>condiciones de mercado procesadas desde abril de 2026</span></div>
  <div class="tile"><b>{len(personal)}</b><span>ofertas personales verificadas, de {len(banks_personal)} entidades</span></div>
  <div class="tile"><b>{len(rows)}</b><span>comparables con el catálogo oficial en la fecha del post, {len(by_bank)} entidades</span></div>
  <div class="tile accent"><b>{pct(below, len(rows))}</b><span>por debajo del mejor tipo publicado, {es(abs(st.median(gaps)))} puntos de diferencia mediana</span></div>
</div>

<section class="fila">
  <h3>Tipo de la oferta personal frente al mejor tipo publicado, por entidad</h3>
  <p>Mediana por entidad con dos o más ofertas comparables. El tipo publicado es el TIN fijo
  bonificado más bajo del catálogo del banco vigente en la fecha de cada oferta. La cifra de la
  derecha es la diferencia en puntos.</p>
  {dumbbell(stats)}
</section>

<div class="cols fila">
  <div>
    <h3>Evolución en 2026</h3>
    <p>Mediana mensual de las ofertas personales y del tipo publicado por las mismas entidades.
    Bajo cada mes, el número de ofertas comparables.</p>
    {monthly_lines(series)}
  </div>
  <div>
    <h3>Ofertas personales verificadas por mes</h3>
    <p>Por fecha de publicación del mensaje en el foro, de marzo a agosto de 2026.</p>
    {volume_bars(months, vol)}
  </div>
</div>

<section class="fila">
  <h3>Ejemplos: lo que el particular declara y lo que bench registra</h3>
  <p>{esc(content["ejemplos_intro"])}</p>
  <table>
    <tr><th>Entidad</th><th class="n">TIN</th><th>Condiciones declaradas</th><th>Estado</th><th class="n">Mensaje</th></tr>
    {ej}
  </table>
</section>
</main>

<main class="hoja">
<div class="eyebrow">Benchmark bancario · Ofertas personalizadas</div>
<h1 style="font-size:14pt">Cómo se obtiene el dato y capacidad de captura</h1>

<div class="cols fila">
  <div>
    <h3>Cadena de captura y validación</h3>
    <ol class="cadena">{"".join(f"<li>{p}</li>" for p in content["cadena"])}</ol>
  </div>
  <div>
    <h3>Filtro de oferta personal</h3>
    <p>{content["filtro"]["intro"]}</p>
    <ol class="cadena">{"".join(f"<li>{p}</li>" for p in content["filtro"]["condiciones"])}</ol>
    <p>{content["filtro"]["cierre"]}</p>
  </div>
</div>

<section class="fila">
  <h3>Fuentes de ofertas personales</h3>
  <p>{content["fuentes_intro"]}</p>
  <table>
    <tr><th>Fuente</th><th>Tipo</th><th class="n">Ofertas por semana</th><th>Observaciones</th></tr>
    {fu}
  </table>
</section>

<div class="cols fila">
  <div class="box"><h2>Próximos pasos</h2><ul class="limites">{pasos}</ul></div>
  <div class="box"><h2>Alcance y límites del dato</h2><ul class="limites">{limites}</ul></div>
</div>
</main>

</body>
</html>""")
    if missing:
        import sys
        print(f"sin catálogo en la fecha: {dict(missing)}", file=sys.stderr)


CSS = """
  @page { size: A4; margin: 10mm 13mm 9mm; }
  * { box-sizing: border-box; }
  html { background: #e9ecf1; }
  body { margin: 0; color: #1d2330; font: 9.2pt/1.4 "Inter", "Helvetica Neue", Helvetica, Arial, sans-serif; }
  .hoja { margin: 6mm auto; width: 210mm; min-height: 297mm; padding: 10mm 13mm 9mm; background: #fff; }
  @media print {
    html { background: #fff; }
    .hoja { margin: 0; width: auto; min-height: 0; padding: 0; }
    .hoja + .hoja { break-before: page; }
  }
  .eyebrow { font-size: 7.6pt; letter-spacing: .08em; text-transform: uppercase; color: #5b6475; }
  h1 { font-size: 17pt; line-height: 1.2; margin: 1.5mm 0 1mm; color: #111722; }
  .meta { font-size: 8pt; color: #5b6475; margin-bottom: 3.5mm; }
  .lead { font-size: 9.9pt; line-height: 1.42; margin: 0 0 3.5mm; border-left: 3px solid #1f6f8b; padding-left: 3.5mm; }
  .tiles { display: grid; grid-template-columns: repeat(4, 1fr); gap: 2.5mm; margin-bottom: 4mm; }
  .tile { background: #f2f5f8; border-radius: 2mm; padding: 2.4mm 3mm; }
  .tile b { display: block; font-size: 16pt; line-height: 1.1; color: #111722; }
  .tile.accent { background: #e6f1f4; }
  .tile.accent b { color: #1f6f8b; }
  .tile span { font-size: 7.8pt; color: #4a5263; line-height: 1.3; display: block; margin-top: .6mm; }
  h2 { font-size: 10.4pt; margin: 3.2mm 0 1.3mm; color: #111722; }
  table { width: 100%; border-collapse: collapse; font-size: 8.8pt; }
  th { text-align: left; font-weight: 600; color: #4a5263; font-size: 7.8pt; border-bottom: 1px solid #c9cfd8; padding: 1mm 1.5mm; }
  td { padding: 1.2mm 1.5mm; border-bottom: 1px solid #e3e7ed; vertical-align: top; }
  td.n, th.n { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
  tr.total td { font-weight: 600; border-bottom: none; border-top: 1px solid #c9cfd8; }
  .zero { color: #b3261e; font-weight: 600; }
  ol.razones { margin: 0; padding-left: 5mm; }
  ol.razones li { margin-bottom: 1.8mm; }
  ol.razones li::marker { font-weight: 600; color: #1f6f8b; }
  .cols { display: grid; grid-template-columns: 1fr 1fr; gap: 5mm; }
  .box { background: #f7f8fa; border-radius: 2mm; padding: 2.4mm 3mm; font-size: 8.5pt; }
  .box h2 { margin-top: 0; font-size: 9.6pt; }
  ul.limites { margin: 0; padding-left: 4mm; }
  ul.limites li { margin-bottom: 1mm; }
  p { margin: 0 0 1.6mm; }
  .fuentes { margin-top: 3.5mm; padding-top: 1.8mm; border-top: 1px solid #e3e7ed; font-size: 7pt; line-height: 1.35; color: #5b6475; }
  .graficas { display: grid; grid-template-columns: 1fr 1fr; gap: 4mm 7mm; margin-top: 2mm; }
  figure h3 + p b { color: #111722; }
  figure { margin: 0; }
  figure h3 { font-size: 9pt; margin: 0; color: #111722; }
  figure p { font-size: 8.5pt; line-height: 1.38; color: #1d2330; margin: .6mm 0 1.8mm; }
  figure svg { display: block; }
  .fila { margin-top: 4mm; }
  .fila h3 { font-size: 9pt; margin: 0; color: #111722; }
  .fila > p { font-size: 8.5pt; line-height: 1.38; margin: .6mm 0 1.2mm; }
  .par { display: grid; grid-template-columns: 1fr 1fr; gap: 7mm; }
  .pais { font-size: 8pt; font-weight: 600; color: #4a5263; margin: .8mm 0 .6mm; }
  figure.ancha { grid-column: 1 / -1; display: grid; grid-template-columns: 1fr 1fr; gap: 7mm; align-items: center; }
  .eje { font-size: 7.6pt; color: #5b6475; margin: 0 0 1mm; }
"""

if __name__ == "__main__":
    main()
