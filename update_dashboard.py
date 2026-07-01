import math
import json
import time
import socket
import threading
import webbrowser
import os
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

import numpy as np
import pandas as pd
import yfinance as yf


# =========================
# QQQ BEAR CALL SPREAD - V4
# local + GitHub Actions
# =========================

IN_GITHUB_ACTIONS = os.getenv("GITHUB_ACTIONS", "").lower() == "true"

TICKER = "QQQ"
LOOKBACK_DAYS = 20
SPREAD_WIDTH = 1.0
NET_CREDIT = 0.10
MODEL_BUFFER_PCT = 0.0185

TARGET_CREDIT_MIN = 0.08
TARGET_CREDIT_MAX = 0.12

TAKE_PROFIT_PCT = 0.60
STOP_MULTIPLIER = 2.0
WAIT_MINUTES_AFTER_OPEN = 5
OPENING_RANGE_MINUTES = 15
POWER_HOUR_STRICT = True

AUTO_REFRESH_SECONDS = 60
MAX_HISTORY_ITEMS = 200

SERVE_LOCAL = not IN_GITHUB_ACTIONS
OPEN_BROWSER = not IN_GITHUB_ACTIONS
RUN_FOREVER = not IN_GITHUB_ACTIONS
PORT = 8000

NY_TZ = ZoneInfo("America/New_York")

BASE_DIR = Path.cwd() / "qqq_dashboard_v4"
BASE_DIR.mkdir(exist_ok=True)

HTML_FILE = BASE_DIR / "qqq-spread-dashboard.html"
STATE_FILE = BASE_DIR / "state.json"
HISTORY_FILE = BASE_DIR / "history.json"

MACRO_EVENTS = [
    {"evento": "ISM Manufacturero", "datetime_ny": "2026-07-01 10:00", "impacto": "alto"},
    {"evento": "Nóminas no agrícolas (NFP)", "datetime_ny": "2026-07-02 08:30", "impacto": "alto"},
    {"evento": "Tasa de desempleo", "datetime_ny": "2026-07-02 08:30", "impacto": "alto"},
    {"evento": "IPC (CPI)", "datetime_ny": "2026-07-15 08:30", "impacto": "alto"},
    {"evento": "IPP (PPI)", "datetime_ny": "2026-07-16 08:30", "impacto": "alto"},
    {"evento": "PIB", "datetime_ny": "2026-07-30 08:30", "impacto": "alto"},
    {"evento": "PCE subyacente", "datetime_ny": "2026-07-31 08:30", "impacto": "alto"},
    {"evento": "Decisión FOMC / tipos de interés", "datetime_ny": "2026-07-29 14:00", "impacto": "alto"},
]


def es_valor_numerico_real(x):
    try:
        return x is not None and not pd.isna(x) and np.isfinite(float(x))
    except Exception:
        return False


def fmt_price(x, default="N/D"):
    if es_valor_numerico_real(x):
        return f"${float(x):.2f}"
    return default


def fmt_pct(x, default="N/D"):
    if es_valor_numerico_real(x):
        return f"{float(x):.2f}%"
    return default


def get_now_ny():
    return datetime.now(NY_TZ)


def traducir_fuente_precio(source):
    mapping = {
        "intraday_close": "Intradía",
        "daily_close": "Cierre diario",
        "fallback": "Fallback",
    }
    return mapping.get(source, source if source else "Fuente no disponible")


def clasificar_sesion_por_hora_ny(dt_ny):
    mins = dt_ny.hour * 60 + dt_ny.minute
    open_mins = 9 * 60 + 30
    close_mins = 16 * 60
    if mins < open_mins:
        return "Antes de la apertura"
    if mins <= close_mins:
        return "Durante la sesión"
    return "Después del cierre"


def clasificar_tramo_horario_ny(dt_ny):
    mins = dt_ny.hour * 60 + dt_ny.minute
    open_mins = 9 * 60 + 30
    noon_mins = 12 * 60
    power_hour_start = 15 * 60
    close_mins = 16 * 60

    if mins < open_mins:
        return "premarket"
    if open_mins <= mins < noon_mins:
        return "apertura"
    if noon_mins <= mins < power_hour_start:
        return "media_sesion"
    if power_hour_start <= mins <= close_mins:
        return "power_hour"
    return "after_hours"


def minutos_desde_apertura(dt_ny):
    open_dt = dt_ny.replace(hour=9, minute=30, second=0, microsecond=0)
    return int((dt_ny - open_dt).total_seconds() // 60)


def tone_from_decision(decision):
    if decision == "entraría":
        return "green", "🟢 Entraría"
    if decision == "esperar confirmación":
        return "yellow", "🟡 Esperar confirmación"
    if decision == "entraría con cautela":
        return "yellow", "🟡 Entraría con cautela"
    return "red", "🔴 No entraría"


def load_json_file(path, default):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json_file(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def flatten_columns(df):
    if df is None or df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def safe_download(**kwargs):
    try:
        df = yf.download(progress=False, auto_adjust=True, threads=False, **kwargs)
        if df is None or df.empty:
            return None
        return flatten_columns(df.copy())
    except Exception:
        return None


def get_price_and_source(ticker_symbol):
    intraday = safe_download(tickers=ticker_symbol, period="1d", interval="5m", prepost=True)
    if intraday is not None and "Close" in intraday.columns and not intraday["Close"].dropna().empty:
        return float(intraday["Close"].dropna().iloc[-1]), "intraday_close"

    daily = safe_download(tickers=ticker_symbol, period="5d", interval="1d", prepost=False)
    if daily is not None and "Close" in daily.columns and not daily["Close"].dropna().empty:
        return float(daily["Close"].dropna().iloc[-1]), "daily_close"

    raise ValueError(f"No se pudo obtener precio de {ticker_symbol}.")


def descargar_historico_base(ticker_symbol):
    hist = safe_download(tickers=ticker_symbol, period="3mo", interval="1d", prepost=False)
    if hist is None:
        raise ValueError("No se pudo descargar histórico.")
    hist = hist[["Open", "High", "Low", "Close"]].dropna().copy()
    hist["up_move_pct"] = (hist["High"] - hist["Open"]) / hist["Open"]
    return hist


def calcular_buffers(hist, lookback_days):
    recent = hist.tail(lookback_days).copy()
    return {
        "avg_up_move": float(recent["up_move_pct"].mean()),
        "median_up_move": float(recent["up_move_pct"].median()),
        "p75_up_move": float(recent["up_move_pct"].quantile(0.75)),
        "p80_up_move": float(recent["up_move_pct"].quantile(0.80)),
    }


def descargar_intradia_1m(ticker_symbol):
    df = safe_download(tickers=ticker_symbol, period="1d", interval="1m", prepost=True)
    if df is None:
        return None

    cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
    if len(cols) < 5:
        return None

    df = df[cols].dropna().copy()
    if df.empty:
        return None

    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC").tz_convert(NY_TZ)
    else:
        df.index = df.index.tz_convert(NY_TZ)

    return df


def preparar_eventos_macro(eventos):
    ahora_ny = get_now_ny()
    salida = []
    for ev in eventos:
        try:
            dt_ny = datetime.strptime(ev["datetime_ny"], "%Y-%m-%d %H:%M").replace(tzinfo=NY_TZ)
            delta = dt_ny - ahora_ny
            total_horas = delta.total_seconds() / 3600
            if total_horas < 0:
                continue
            dias = int(total_horas // 24)
            horas = int(total_horas % 24)
            salida.append({
                "evento": ev["evento"],
                "impacto": ev.get("impacto", "medio"),
                "datetime_ny": dt_ny,
                "fecha": dt_ny.date(),
                "dias": dias,
                "horas": horas,
                "total_horas": total_horas,
                "momento": clasificar_sesion_por_hora_ny(dt_ny)
            })
        except Exception:
            continue
    return sorted(salida, key=lambda x: x["datetime_ny"])


def calcular_vwap_intradia(ticker_symbol, interval="5m", include_prepost=True):
    df = safe_download(tickers=ticker_symbol, period="1d", interval=interval, prepost=include_prepost)
    if df is None:
        return None

    cols = [c for c in ["High", "Low", "Close", "Volume"] if c in df.columns]
    if len(cols) < 4:
        return None

    df = df[cols].dropna().copy()
    if df.empty:
        return None

    typical_price = (df["High"] + df["Low"] + df["Close"]) / 3.0
    vol = df["Volume"].astype(float)
    if vol.sum() <= 0:
        return None

    df["tpv"] = typical_price * vol
    df["cum_tpv"] = df["tpv"].cumsum()
    df["cum_vol"] = vol.cumsum()
    df["vwap"] = df["cum_tpv"] / df["cum_vol"]

    return {
        "vwap": float(df["vwap"].iloc[-1]),
    }


def clasificar_bias_vwap(dist_pct):
    if not es_valor_numerico_real(dist_pct):
        return "VWAP no disponible"
    if dist_pct >= 0.75:
        return "Muy por encima"
    if dist_pct >= 0.30:
        return "Por encima"
    if dist_pct <= -0.75:
        return "Muy por debajo"
    if dist_pct <= -0.30:
        return "Por debajo"
    return "Cerca del VWAP"


def penalizacion_vwap(dist_pct):
    if not es_valor_numerico_real(dist_pct):
        return 0, None, None
    if dist_pct >= 1.00:
        return -12, "Precio muy extendido por encima del VWAP", "Impulso intradía fuerte"
    if dist_pct >= 0.50:
        return -7, "Precio por encima del VWAP", None
    if dist_pct >= 0.20:
        return -3, "Precio ligeramente por encima del VWAP", None
    if dist_pct <= -1.00:
        return 5, "Precio claramente por debajo del VWAP", None
    if dist_pct <= -0.50:
        return 3, "Precio por debajo del VWAP", None
    return 0, "Cerca del VWAP", None


def construir_contexto_vwap(ticker_symbol, current_price):
    vwap_rth_data = calcular_vwap_intradia(ticker_symbol, interval="5m", include_prepost=False)
    ctx = {
        "vwap_rth": None,
        "dist_to_vwap_rth_pct": None,
        "vwap_rth_bias": "VWAP regular no disponible",
    }

    if vwap_rth_data is not None:
        ctx["vwap_rth"] = vwap_rth_data["vwap"]
        diff = current_price - ctx["vwap_rth"]
        if ctx["vwap_rth"] != 0:
            ctx["dist_to_vwap_rth_pct"] = (diff / ctx["vwap_rth"]) * 100
            ctx["vwap_rth_bias"] = clasificar_bias_vwap(ctx["dist_to_vwap_rth_pct"])

    return ctx


def clasificar_regimen_dia(current_price, premarket_high, premarket_low, opening_range_high, opening_range_low):
    if es_valor_numerico_real(opening_range_high) and current_price > opening_range_high:
        return "trend_alcista"
    if es_valor_numerico_real(opening_range_low) and current_price < opening_range_low:
        return "trend_bajista"
    if es_valor_numerico_real(premarket_high) and es_valor_numerico_real(premarket_low):
        if premarket_low <= current_price <= premarket_high:
            return "rango"
    return "indefinido"


def construir_contexto_intradia(ticker_symbol, short_strike, long_strike, current_price):
    now_ny = get_now_ny()
    tramo = clasificar_tramo_horario_ny(now_ny)

    intraday_1m = descargar_intradia_1m(ticker_symbol)
    premarket_high = None
    premarket_low = None
    opening_range_high = None
    opening_range_low = None

    if intraday_1m is not None and not intraday_1m.empty:
        premarket = intraday_1m.between_time("04:00", "09:29")
        if not premarket.empty:
            premarket_high = float(premarket["High"].max())
            premarket_low = float(premarket["Low"].min())

        session_df = intraday_1m.between_time("09:30", "16:00")
        if not session_df.empty:
            start_ts = session_df.index.min()
            end_ts = start_ts + pd.Timedelta(minutes=OPENING_RANGE_MINUTES - 1)
            orb = session_df[(session_df.index >= start_ts) & (session_df.index <= end_ts)]
            if not orb.empty:
                opening_range_high = float(orb["High"].max())
                opening_range_low = float(orb["Low"].min())

    regime = clasificar_regimen_dia(
        current_price, premarket_high, premarket_low, opening_range_high, opening_range_low
    )

    return {
        "now_ny": now_ny,
        "tramo_horario": tramo,
        "premarket_high": premarket_high,
        "premarket_low": premarket_low,
        "opening_range_high": opening_range_high,
        "opening_range_low": opening_range_low,
        "regime": regime,
    }


def construir_setup_trade(current_price, buffer_pct):
    projected_upside_price = current_price * (1 + buffer_pct)
    short_strike = math.ceil(projected_upside_price)
    long_strike = short_strike + SPREAD_WIDTH

    credit_ok = TARGET_CREDIT_MIN <= NET_CREDIT <= TARGET_CREDIT_MAX
    breakeven = short_strike + NET_CREDIT
    dist_to_short = short_strike - current_price

    if credit_ok and current_price < short_strike:
        decision = "entraría"
    elif credit_ok and current_price < breakeven:
        decision = "entraría con cautela"
    else:
        decision = "no entraría"

    return {
        "short_strike": short_strike,
        "long_strike": long_strike,
        "breakeven": breakeven,
        "dist_to_short": dist_to_short,
        "credit_ok": credit_ok,
        "decision_base": decision,
        "take_profit_price": max(0.01, NET_CREDIT * (1 - TAKE_PROFIT_PCT)),
        "stop_price": NET_CREDIT * STOP_MULTIPLIER,
        "max_loss_per_spread": max(0, (SPREAD_WIDTH - NET_CREDIT) * 100),
    }


def calcular_score_operativo(current_price, trade_setup, vwap_ctx, macro_events, intraday_ctx):
    score = 100.0
    motivos = []
    alertas = []
    bloqueo = False

    dts = trade_setup["dist_to_short"]
    if dts <= 0:
        score -= 40
        motivos.append("Precio en o por encima del short strike (-40)")
        bloqueo = True
    elif dts <= 1:
        score -= 30
        motivos.append("Short strike muy cerca (-30)")
    elif dts <= 2:
        score -= 18
        motivos.append("Short strike algo cerca (-18)")
    elif dts <= 3:
        score -= 8
        motivos.append("Margen al short limitado (-8)")

    pen, txt, alerta = penalizacion_vwap(vwap_ctx["dist_to_vwap_rth_pct"])
    score += pen
    if txt:
        motivos.append(txt)
    if alerta:
        alertas.append(alerta)

    tramo = intraday_ctx["tramo_horario"]
    if tramo == "premarket":
        score -= 3
        motivos.append("Premarket: falta validación de apertura (-3)")
    elif tramo == "apertura":
        score -= 6
        motivos.append("Apertura: volatilidad inicial alta (-6)")
        if 0 <= minutos_desde_apertura(intraday_ctx["now_ny"]) < WAIT_MINUTES_AFTER_OPEN:
            score -= 5
            motivos.append(f"Primeros {WAIT_MINUTES_AFTER_OPEN} minutos (-5)")
    elif tramo == "media_sesion":
        score += 2
        motivos.append("Media sesión más estable (+2)")
    elif tramo == "power_hour":
        score -= 10
        motivos.append("Power hour más agresiva (-10)")
        if POWER_HOUR_STRICT and trade_setup["dist_to_short"] <= 3:
            score -= 8
            motivos.append("Power hour con strike cercano (-8)")
    elif tramo == "after_hours":
        score -= 12
        motivos.append("Fuera de mercado regular (-12)")

    regime = intraday_ctx["regime"]
    if regime == "trend_alcista":
        score -= 12
        motivos.append("Régimen alcista intradía (-12)")
        alertas.append("Setup en contra de posible trend day alcista")
    elif regime == "trend_bajista":
        score += 5
        motivos.append("Régimen bajista (+5)")
    elif regime == "rango":
        score += 3
        motivos.append("Rango favorable para premium (+3)")
    else:
        motivos.append("Régimen no claro")

    if not trade_setup["credit_ok"]:
        score -= 10
        motivos.append("Crédito fuera de rango objetivo (-10)")

    for ev in macro_events:
        if ev["impacto"] != "alto":
            continue
        th = ev["total_horas"]
        momento = ev["momento"]
        if momento == "Durante la sesión":
            if th <= 6:
                score -= 35
                motivos.append(f"{ev['evento']} durante sesión en <6h (-35)")
                alertas.append(f"{ev['evento']} en {ev['dias']}d {ev['horas']}h")
                bloqueo = True
            elif th <= 24:
                score -= 25
                motivos.append(f"{ev['evento']} durante sesión en <24h (-25)")
                alertas.append(f"{ev['evento']} en {ev['dias']}d {ev['horas']}h")
                bloqueo = True
            elif th <= 48:
                score -= 12
                motivos.append(f"{ev['evento']} durante sesión en <48h (-12)")

    score = max(0, min(100, round(score, 2)))
    if score >= 75:
        semaforo = "🟢 Riesgo controlado"
    elif score >= 50:
        semaforo = "🟡 Riesgo medio"
    else:
        semaforo = "🔴 Riesgo alto"

    return {
        "score": score,
        "motivos_score": motivos,
        "alertas": alertas,
        "bloqueo_operativa": bloqueo,
        "semaforo": semaforo
    }


def ajustar_decision_final(trade_setup, intraday_ctx, score_data):
    if score_data["bloqueo_operativa"]:
        return "no entraría", ["Bloqueo operativo activado"]

    tramo = intraday_ctx["tramo_horario"]
    if tramo == "apertura":
        mins = minutos_desde_apertura(intraday_ctx["now_ny"])
        if 0 <= mins < WAIT_MINUTES_AFTER_OPEN:
            return "esperar confirmación", [f"Primeros {WAIT_MINUTES_AFTER_OPEN} minutos"]

    if score_data["score"] < 50:
        return "no entraría", ["Score insuficiente"]

    decision = trade_setup["decision_base"]
    if 50 <= score_data["score"] < 75 and decision == "entraría":
        return "esperar confirmación", ["Score medio"]

    return decision, []


def append_history(state):
    history = load_json_file(HISTORY_FILE, [])
    row = {
        "timestamp": state["updatedAt"],
        "ticker": state["ticker"],
        "decision": state["decision"],
        "decisionLabel": state["decisionLabel"],
        "score": state["score"],
        "semaforo": state["semaforo"],
        "precio": state["precio"],
        "shortStrike": state["shortStrike"],
        "longStrike": state["longStrike"],
        "vwap": state["vwap"],
        "regimen": state["contextMap"].get("Régimen"),
        "tramo": state["contextMap"].get("Tramo"),
    }

    if history and history[-1]["timestamp"] == row["timestamp"]:
        history[-1] = row
    else:
        history.append(row)

    history = history[-MAX_HISTORY_ITEMS:]
    save_json_file(HISTORY_FILE, history)
    return history


def construir_state():
    current_price, price_source = get_price_and_source(TICKER)
    hist = descargar_historico_base(TICKER)
    buffers = calcular_buffers(hist, LOOKBACK_DAYS)
    buffer_pct = MODEL_BUFFER_PCT

    trade_setup = construir_setup_trade(current_price, buffer_pct)
    vwap_ctx = construir_contexto_vwap(TICKER, current_price)
    macro_events = preparar_eventos_macro(MACRO_EVENTS)
    intraday_ctx = construir_contexto_intradia(
        TICKER, trade_setup["short_strike"], trade_setup["long_strike"], current_price
    )
    score_data = calcular_score_operativo(
        current_price, trade_setup, vwap_ctx, macro_events, intraday_ctx
    )
    decision_final, decision_notes = ajustar_decision_final(trade_setup, intraday_ctx, score_data)
    tone, decision_label = tone_from_decision(decision_final)

    reasons = []
    for txt in score_data["motivos_score"][:6]:
        rtone = "ok"
        low = txt.lower()
        if "-" in txt or "riesgo" in low or "insuficiente" in low:
            rtone = "warn"
        if "(-35)" in txt or "(-40)" in txt or "no " in low:
            rtone = "danger"
        reasons.append({"tone": rtone, "title": txt.split(" (")[0], "text": txt})

    for note in decision_notes[:2]:
        reasons.append({"tone": "warn", "title": "Decisión", "text": note})

    if not reasons:
        reasons.append({"tone": "ok", "title": "Sin penalizaciones críticas", "text": "No se detectaron bloqueos relevantes."})

    alerts = []
    for a in score_data["alertas"][:5]:
        tone_a = "warn"
        low = a.lower()
        if "bloqueo" in low or "muy cerca" in low:
            tone_a = "danger"
        alerts.append({"tone": tone_a, "title": a, "text": a})

    if not alerts:
        alerts.append({"tone": "ok", "title": "Sin alertas cercanas", "text": "No hay alertas inmediatas."})

    prox_macro = macro_events[0] if macro_events else None

    context_rows = [
        ["Tramo", intraday_ctx["tramo_horario"].replace("_", " ").title()],
        ["Régimen", intraday_ctx["regime"].replace("_", " ").title()],
        ["Premarket high", fmt_price(intraday_ctx["premarket_high"])],
        ["Premarket low", fmt_price(intraday_ctx["premarket_low"])],
        ["Buffer modelo fijo", fmt_pct(buffer_pct * 100)],
        ["P75 20d", fmt_pct(buffers["p75_up_move"] * 100)],
        ["P80 20d", fmt_pct(buffers["p80_up_move"] * 100)],
        ["Distancia al short", fmt_price(trade_setup["dist_to_short"])],
    ]

    events_rows = [
        ["Próximo macro", prox_macro["evento"] if prox_macro else "N/D"],
        ["Impacto", prox_macro["impacto"].title() if prox_macro else "N/D"],
        ["Modo CI", "Sí" if IN_GITHUB_ACTIONS else "No"],
        ["Run forever", "Sí" if RUN_FOREVER else "No"],
    ]

    state = {
        "ticker": TICKER,
        "updatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "decision": decision_final,
        "decisionLabel": decision_label,
        "decisionTone": tone,
        "score": score_data["score"],
        "semaforo": score_data["semaforo"],
        "horaNy": intraday_ctx["now_ny"].strftime("%Y-%m-%d %H:%M"),
        "precio": round(current_price, 2),
        "precioFuente": traducir_fuente_precio(price_source),
        "shortStrike": trade_setup["short_strike"],
        "longStrike": trade_setup["long_strike"],
        "breakeven": round(trade_setup["breakeven"], 2),
        "vwap": round(vwap_ctx["vwap_rth"], 2) if es_valor_numerico_real(vwap_ctx["vwap_rth"]) else None,
        "vwapBias": vwap_ctx["vwap_rth_bias"],
        "tp": round(trade_setup["take_profit_price"], 2),
        "stop": round(trade_setup["stop_price"], 2),
        "riesgoMax": round(trade_setup["max_loss_per_spread"], 2),
        "reasons": reasons,
        "alerts": alerts,
        "context": context_rows,
        "events": events_rows,
        "contextMap": {k: v for k, v in context_rows}
    }
    return state


def html_template():
    return """<!doctype html>
<html lang="es">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>QQQ Dashboard</title></head>
<body>
<h1>QQQ Dashboard</h1>
<p>Abre state.json e history.json en esta carpeta.</p>
</body>
</html>
"""


class SilentHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def start_server():
    if Path.cwd() != BASE_DIR:
        os.chdir(BASE_DIR)
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), SilentHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def write_dashboard_assets(state):
    save_json_file(STATE_FILE, state)
    history = append_history(state)
    save_json_file(HISTORY_FILE, history)
    if not HTML_FILE.exists():
        HTML_FILE.write_text(html_template(), encoding="utf-8")
    return history


def run_once():
    print("1/3 Construyendo state...")
    state = construir_state()
    print("2/3 Guardando archivos...")
    history = write_dashboard_assets(state)
    print("3/3 Terminado.")
    return state, history


def main():
    if not HTML_FILE.exists():
        HTML_FILE.write_text(html_template(), encoding="utf-8")

    server = None

    if SERVE_LOCAL:
        try:
            server = start_server()
            local_ip = get_local_ip()
            local_url = f"http://127.0.0.1:{PORT}/qqq-spread-dashboard.html"
            lan_url = f"http://{local_ip}:{PORT}/qqq-spread-dashboard.html"
            print(f"Dashboard local: {local_url}")
            print(f"Dashboard móvil/tablet: {lan_url}")
            if OPEN_BROWSER:
                try:
                    webbrowser.open(local_url)
                except Exception:
                    pass
        except Exception as e:
            print(f"No se pudo iniciar el servidor local: {e}")

    if not RUN_FOREVER:
        state, history = run_once()
        print(f"[{state['updatedAt']}] {state['decisionLabel']} | Score {state['score']} | Precio {state['precio']} | Histórico {len(history)}")
        return

    print("Iniciando bucle de actualización...")
    print(f"Frecuencia: cada {AUTO_REFRESH_SECONDS} segundos")
    print("Pulsa Ctrl+C para detener.")

    try:
        while True:
            try:
                state, history = run_once()
                print(f"[{state['updatedAt']}] {state['decisionLabel']} | Score {state['score']} | Precio {state['precio']} | Histórico {len(history)}")
            except Exception as e:
                print(f"Error en actualización: {e}")
            time.sleep(AUTO_REFRESH_SECONDS)
    except KeyboardInterrupt:
        print("Detenido por usuario.")
        if server:
            server.shutdown()


if __name__ == "__main__":
    main()
