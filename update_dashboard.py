import json
import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf

TICKER = "QQQ"
LOOKBACK_DAYS = 20
SPREAD_WIDTH = 1.0
NET_CREDIT = 0.10
TAKE_PROFIT_PCT = 0.60
STOP_MULTIPLIER = 2.0
MAX_HISTORY_ITEMS = 200

NY_TZ = ZoneInfo("America/New_York")
BASE_DIR = Path.cwd()
HTML_FILE = BASE_DIR / "index.html"
STATE_FILE = BASE_DIR / "state.json"
HISTORY_FILE = BASE_DIR / "history.json"

MACRO_EVENTS = [
    {"evento": "ISM Manufacturero", "datetime_ny": "2026-07-01 10:00", "impacto": "alto"},
    {"evento": "Nóminas no agrícolas (NFP)", "datetime_ny": "2026-07-02 08:30", "impacto": "alto"},
    {"evento": "Tasa de desempleo", "datetime_ny": "2026-07-02 08:30", "impacto": "alto"},
    {"evento": "IPC (CPI)", "datetime_ny": "2026-07-15 08:30", "impacto": "alto"},
    {"evento": "IPP (PPI)", "datetime_ny": "2026-07-16 08:30", "impacto": "alto"},
    {"evento": "Ventas minoristas", "datetime_ny": "2026-07-16 08:30", "impacto": "medio"},
    {"evento": "PIB", "datetime_ny": "2026-07-30 08:30", "impacto": "alto"},
    {"evento": "PCE subyacente", "datetime_ny": "2026-07-31 08:30", "impacto": "alto"},
    {"evento": "Decisión FOMC / tipos de interés", "datetime_ny": "2026-07-29 14:00", "impacto": "alto"},
]

def es_num(x):
    try:
        return x is not None and not pd.isna(x) and np.isfinite(float(x))
    except Exception:
        return False

def fmt_price(x):
    return f"${float(x):.2f}" if es_num(x) else "N/D"

def now_ny():
    return datetime.now(NY_TZ)

def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def get_price_and_history(symbol):
    df = yf.download(
        symbol,
        period="3mo",
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=False
    )
    if df is None or df.empty:
        raise ValueError("No se pudo descargar histórico de QQQ")

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df[["Open", "High", "Low", "Close"]].dropna().copy()
    current_price = float(df["Close"].iloc[-1])
    df["up_move_pct"] = (df["High"] - df["Open"]) / df["Open"]
    return current_price, df

def get_intraday(symbol):
    try:
        df = yf.download(
            symbol,
            period="1d",
            interval="5m",
            auto_adjust=True,
            progress=False,
            prepost=True,
            threads=False
        )
        if df is None or df.empty:
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna().copy()
        if df.empty:
            return None

        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC").tz_convert(NY_TZ)
        else:
            df.index = df.index.tz_convert(NY_TZ)

        return df
    except Exception:
        return None

def calc_vwap(df):
    if df is None or df.empty:
        return None
    tp = (df["High"] + df["Low"] + df["Close"]) / 3.0
    vol = df["Volume"].astype(float)
    if vol.sum() <= 0:
        return None
    return float((tp * vol).sum() / vol.sum())

def classify_session(dt):
    mins = dt.hour * 60 + dt.minute
    if mins < 570:
        return "premarket"
    if mins < 720:
        return "apertura"
    if mins < 900:
        return "media_sesion"
    if mins <= 960:
        return "power_hour"
    return "after_hours"

def prepare_macro(events):
    ahora = now_ny()
    out = []
    for ev in events:
        dt = datetime.strptime(ev["datetime_ny"], "%Y-%m-%d %H:%M").replace(tzinfo=NY_TZ)
        delta_h = (dt - ahora).total_seconds() / 3600
        if delta_h < 0:
            continue
        out.append({
            "evento": ev["evento"],
            "impacto": ev["impacto"],
            "dias": int(delta_h // 24),
            "horas": int(delta_h % 24)
        })
    return out[:5]

def html_template():
    return '''<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>QQQ Dashboard</title>
  <style>
    :root{
      --bg:#0f1115;
      --surface:#171a21;
      --text:#f3f5f7;
      --muted:#a7b0ba;
      --border:#2a2f3a;
      --green:#4ade80;
      --yellow:#fbbf24;
      --red:#fb7185;
    }
    *{box-sizing:border-box}
    body{
      margin:0;
      font-family:Inter,system-ui,sans-serif;
      background:var(--bg);
      color:var(--text);
    }
    .wrap{max-width:1100px;margin:0 auto;padding:16px}
    .card{
      background:var(--surface);
      border:1px solid var(--border);
      border-radius:16px;
      padding:16px;
      margin-bottom:16px;
    }
    .grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
    .k{font-size:12px;color:var(--muted);text-transform:uppercase}
    .v{font-size:22px;font-weight:800}
    .pill{
      display:inline-block;
      padding:8px 12px;
      border-radius:999px;
      font-weight:700;
      margin-bottom:8px;
    }
    .green{background:rgba(74,222,128,.15);color:var(--green)}
    .yellow{background:rgba(251,191,36,.15);color:var(--yellow)}
    .red{background:rgba(251,113,133,.15);color:var(--red)}
    h1,h3{margin-top:0}
    p{color:var(--muted)}
    table{width:100%;border-collapse:collapse}
    th,td{
      padding:8px;
      border-bottom:1px solid var(--border);
      text-align:left;
      color:var(--text);
    }
    th{color:var(--muted)}
    ul{margin:0;padding-left:18px}
    li{margin:8px 0;color:var(--text)}
    strong{color:var(--text)}
    @media(max-width:800px){.grid{grid-template-columns:1fr 1fr}}
    @media(max-width:520px){.grid{grid-template-columns:1fr}}
  </style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <div id="decision" class="pill yellow">Cargando...</div>
    <h1>QQQ Bear Call Spread</h1>
    <p id="updated">Actualizando...</p>
  </div>

  <div class="grid">
    <div class="card"><div class="k">Score</div><div class="v" id="score"></div></div>
    <div class="card"><div class="k">Precio</div><div class="v" id="precio"></div></div>
    <div class="card"><div class="k">Strikes</div><div class="v" id="strikes"></div></div>
    <div class="card"><div class="k">VWAP</div><div class="v" id="vwap"></div></div>
  </div>

  <div class="card">
    <h3>Contexto</h3>
    <ul id="contexto"></ul>
  </div>

  <div class="card">
    <h3>Macro</h3>
    <ul id="macro"></ul>
  </div>

  <div class="card">
    <h3>Histórico</h3>
    <table>
      <thead>
        <tr>
          <th>Hora</th>
          <th>Decisión</th>
          <th>Score</th>
          <th>Precio</th>
        </tr>
      </thead>
      <tbody id="hist"></tbody>
    </table>
  </div>
</div>

<script>
async function load(){
  const s = await (await fetch('./state.json?' + Date.now(), {cache:'no-store'})).json();
  const h = await (await fetch('./history.json?' + Date.now(), {cache:'no-store'})).json();

  const d = document.getElementById('decision');
  d.textContent = s.decisionLabel;
  d.className = 'pill ' + s.decisionTone;

  document.getElementById('updated').textContent =
    'Actualizado local: ' + s.updatedAt + ' | Hora NY: ' + s.horaNy;

  document.getElementById('score').textContent = s.score;
  document.getElementById('precio').textContent = (s.precio ?? 'N/D');
  document.getElementById('strikes').textContent = s.shortStrike + ' / ' + s.longStrike;
  document.getElementById('vwap').textContent = (s.vwap ?? 'N/D');

  document.getElementById('contexto').innerHTML =
    (s.context || []).map(x => '<li><strong>' + x[0] + ':</strong> ' + x[1] + '</li>').join('');

  document.getElementById('macro').innerHTML =
    (s.macro || []).map(x => '<li>' + x + '</li>').join('');

  document.getElementById('hist').innerHTML =
    (h.slice(-8).reverse()).map(x =>
      '<tr><td>' + x.timestamp + '</td><td>' + x.decisionLabel + '</td><td>' + x.score + '</td><td>' + x.precio + '</td></tr>'
    ).join('');
}
load();
setInterval(load, 15000);
</script>
</body>
</html>'''

def build_state():
    price, hist = get_price_and_history(TICKER)

    recent = hist.tail(LOOKBACK_DAYS).copy()
    buffer_pct = float(recent["up_move_pct"].quantile(0.75))

    short_strike = math.ceil(price * (1 + buffer_pct))
    long_strike = short_strike + SPREAD_WIDTH
    breakeven = short_strike + NET_CREDIT
    dist_to_short = short_strike - price

    intraday = get_intraday(TICKER)
    vwap = calc_vwap(intraday)
    tramo = classify_session(now_ny())

    score = 100
    if dist_to_short <= 0:
        score -= 40
    elif dist_to_short <= 2:
        score -= 18

    if es_num(vwap) and price > vwap:
        score -= 5

    if tramo == "apertura":
        score -= 6
    elif tramo == "power_hour":
        score -= 10

    score = max(0, min(100, round(score, 2)))

    if score >= 75 and price < short_strike:
        decision, tone, label = "entraría", "green", "Entraría"
    elif score >= 50:
        decision, tone, label = "esperar confirmación", "yellow", "Esperar confirmación"
    else:
        decision, tone, label = "no entraría", "red", "No entraría"

    macro = prepare_macro(MACRO_EVENTS)

    context = [
        ["Tramo", tramo.replace("_", " ").title()],
        ["Distancia al short", fmt_price(dist_to_short)],
        ["Break-even", fmt_price(breakeven)],
        ["Take profit", fmt_price(max(0.01, NET_CREDIT * (1 - TAKE_PROFIT_PCT)))],
        ["Stop", fmt_price(NET_CREDIT * STOP_MULTIPLIER)]
    ]

    return {
        "ticker": TICKER,
        "updatedAt": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        "horaNy": now_ny().strftime("%Y-%m-%d %H:%M"),
        "decision": decision,
        "decisionLabel": label,
        "decisionTone": tone,
        "score": score,
        "precio": round(price, 2),
        "shortStrike": short_strike,
        "longStrike": long_strike,
        "breakeven": round(breakeven, 2),
        "vwap": round(vwap, 2) if es_num(vwap) else None,
        "context": context,
        "macro": [f"{m['evento']} · {m['dias']}d {m['horas']}h · {m['impacto']}" for m in macro]
    }

def append_history(state):
    history = load_json(HISTORY_FILE, [])
    history.append({
        "timestamp": state["updatedAt"],
        "decisionLabel": state["decisionLabel"],
        "score": state["score"],
        "precio": state["precio"]
    })
    history = history[-MAX_HISTORY_ITEMS:]
    save_json(HISTORY_FILE, history)
    return history

def main():
    HTML_FILE.write_text(html_template(), encoding="utf-8")
    state = build_state()
    save_json(STATE_FILE, state)
    history = append_history(state)
    print(f"{state['updatedAt']} | {state['decisionLabel']} | Score {state['score']} | Historial {len(history)}")

if __name__ == "__main__":
    main()
