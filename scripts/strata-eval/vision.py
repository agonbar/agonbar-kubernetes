#!/usr/bin/env python3
"""One realistic vision case: an emissions table rendered like a page of an annual report.

  vision.py TARGET [--max-tokens 4096]

The page (synthetic figures, Spanish number format, units in a footnote: thousands of
tCO2e) is rendered to PNG with headless chromium, then the model has to return the
2023 column as tonnes. Checks every value, so reading the wrong column, dropping the
x1000 from the footnote, or mangling 1.234,5 shows up as a wrong field.
Results: results/vision-<target>.json
"""
import argparse, base64, json, os, re, subprocess, sys, tempfile, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import targets

ROWS = [("Alcance 1 (emisiones directas)", ["12,4", "11,9", "10,7"]),
        ("Alcance 2 – basado en mercado", ["3,1", "2,6", "2,2"]),
        ("Alcance 2 – basado en ubicación", ["8,8", "8,1", "7,9"]),
        ("Alcance 3 (categorías 1, 4, 6 y 7)", ["1.204,6", "1.187,3", "1.150,0"]),
        ("Total alcances 1 + 2 (mercado)", ["15,5", "14,5", "12,9"])]
YEARS = ["2022", "2023", "2024"]
WANT = {"scope1": 11900, "scope2_market": 2600, "scope2_location": 8100, "scope3": 1187300, "scope1_2_market": 14500}

HTML = """<html><body style="font-family:Georgia,serif;margin:40px;width:900px;color:#222">
<p style="font-size:13px;color:#777">Informe de Sostenibilidad 2024 · Capítulo 5 · Medio ambiente</p>
<h2 style="font-weight:normal">5.3 Huella de carbono del Grupo</h2>
<p style="font-size:14px">La tabla recoge las emisiones de gases de efecto invernadero del perímetro consolidado,
calculadas según GHG Protocol. Los datos de 2022 se han reexpresado tras la venta de la filial logística.</p>
<table style="border-collapse:collapse;font-size:15px;width:100%">
<tr style="background:#2f5d50;color:white"><th style="text-align:left;padding:8px">Indicador</th>{ths}</tr>
{rows}
</table>
<p style="font-size:12px;color:#555">Unidades: miles de toneladas de CO<sub>2</sub> equivalente (ktCO<sub>2</sub>e).
(*) 2022 reexpresado. Fuente: elaboración propia, verificado por tercero independiente.</p>
</body></html>"""

PROMPT = ("This is a page from a company's sustainability report. Extract the greenhouse gas emissions for the year 2023 "
          "and convert them to tonnes of CO2e (mind the units stated on the page). Reply with only a JSON object with the keys "
          "scope1, scope2_market, scope2_location, scope3, scope1_2_market, each a number in tonnes.")


def render(path):
    ths = "".join('<th style="padding:8px;text-align:right">%s%s</th>' % (y, "(*)" if y == "2022" else "") for y in YEARS)
    rows = "".join('<tr style="border-bottom:1px solid #ccc;%s"><td style="padding:8px">%s</td>%s</tr>' % (
        "font-weight:bold" if n.startswith("Total") else "", n, "".join('<td style="padding:8px;text-align:right">%s</td>' % v for v in vs))
        for n, vs in ROWS)
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as f:
        f.write(HTML.format(ths=ths, rows=rows))
    subprocess.run(["chromium", "--headless", "--disable-gpu", "--no-sandbox", "--hide-scrollbars", "--screenshot=" + path,
                    "--window-size=1000,640", "file://" + f.name], capture_output=True, timeout=120)
    os.unlink(f.name)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target")
    ap.add_argument("--max-tokens", type=int, default=4096)
    a = ap.parse_args()
    t = targets.get(a.target)
    png = "/tmp/strata-eval-vision.png"
    if not os.path.exists(png):
        render(png)
    b64 = base64.b64encode(open(png, "rb").read()).decode()
    body = dict({"model": t["model"], "temperature": 0, "max_tokens": a.max_tokens, "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}, {"type": "text", "text": PROMPT}]}]}, **t["extra"])
    req = urllib.request.Request(t["root"] + "/v1/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "Authorization": "Bearer " + t["key"]})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=1800) as r:
        d = json.load(r)
    lat = time.time() - t0
    text = (d["choices"][0]["message"].get("content") or "")
    s = re.sub(r"(?s)<think>.*?</think>", "", text)
    i, j = s.find("{"), s.rfind("}")
    try:
        got = json.loads(s[i:j + 1])
    except ValueError:
        got = {}
    fields = {k: (isinstance(got.get(k), (int, float)) and abs(got[k] - v) <= 0.005 * v) for k, v in WANT.items()}
    summ = {"target": t["name"], "ok": "%d/%d" % (sum(fields.values()), len(fields)), "latencySec": round(lat, 1),
            "promptTokens": (d.get("usage") or {}).get("prompt_tokens"), "completionTokens": (d.get("usage") or {}).get("completion_tokens")}
    print(json.dumps(summ), json.dumps(got))
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "vision-%s.json" % t["name"])
    json.dump({"summary": summ, "fields": fields, "got": got, "want": WANT, "raw": text[:2000]}, open(out, "w"), indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
