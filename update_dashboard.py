import math
import json
import time
import socket
import threading
import webbrowser
from pathlib import Path
from datetime import datetime, date
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler


# =========================
# QQQ BEAR CALL SPREAD - V2
# + HTML local responsive
# + Auto refresh
# + Histórico JSON
# + Últimas señales
# + Servidor local opcional
# =========================

# -------- CONFIG --------
TICKER = "QQQ"
LOOKBACK_DAYS = 20
SPREAD_WIDTH = 1.0
NET_CREDIT = 0.10

TARGET_CREDIT_MIN = 0.08
TARGET_CREDIT_MAX = 0.12

TAKE_PROFIT_PCT = 0.60
STOP_MULTIPLIER = 2.0
WAIT_MINUTES_AFTER_OPEN = 5
OPENING_RANGE_MINUTES = 15
POWER_HOUR_STRICT = True

AUTO_REFRESH_SECONDS = 60
MAX_HISTORY_ITEMS = 200
SERVE_LOCAL = False
PORT = 8000
OPEN_BROWSER = False

MAG7 = {
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "NVDA": "Nvidia",
    "AMZN": "Amazon",
    "META": "Meta",
    "GOOGL": "Alphabet",
    "TSLA": "Tesla",
}

NY_TZ = ZoneInfo("America/New_York")

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

BASE_DIR = Path.cwd() / "qqq_dashboard_v2"
BASE_DIR.mkdir(exist_ok=True)

HTML_FILE = BASE_DIR / "qqq-spread-dashboard.html"
STATE_FILE = BASE_DIR / "state.json"
HISTORY_FILE = BASE_DIR / "history.json"


# -------- HELPERS --------
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


def normalizar_fecha(x):
    if x is None:
        return None
    try:
        ts = pd.to_datetime(x)
        if pd.isna(ts):
            return None
        try:
            ts = ts.tz_localize(None)
        except Exception:
            pass
        return ts.date()
    except Exception:
        return None


def dias_restantes(fecha_obj):
    if fecha_obj is None:
        return None
    return (fecha_obj - date.today()).days


def get_now_ny():
    return datetime.now(NY_TZ)


def traducir_momento(texto):
    if texto is None:
        return "Hora no especificada"
    t = str(texto).strip().lower()
    if "before market open" in t or "before open" in t:
        return "Antes de la apertura"
    if "after market close" in t or "after close" in t:
        return "Después del cierre"
    if "time not supplied" in t:
        return "Hora no especificada"
    if t in ["tas", "during market hours", "during market", "market hours"]:
        return "Durante la sesión"
    return str(texto)


def traducir_fuente_precio(source):
    mapping = {
        "postMarketPrice": "Después del cierre",
        "preMarketPrice": "Antes de la apertura",
        "regularMarketPrice": "Durante la sesión",
        "fast_info.lastPrice": "Último precio disponible",
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


# -------- DATOS --------
def get_price_and_source(ticker_symbol):
    t = yf.Ticker(ticker_symbol)
    info = t.info

    current_price = None
    price_source = None

    if es_valor_numerico_real(info.get("postMarketPrice")):
        current_price = info.get("postMarketPrice")
        price_source = "postMarketPrice"
    elif es_valor_numerico_real(info.get("preMarketPrice")):
        current_price = info.get("preMarketPrice")
        price_source = "preMarketPrice"
    elif es_valor_numerico_real(info.get("regularMarketPrice")):
        current_price = info.get("regularMarketPrice")
        price_source = "regularMarketPrice"

    if not es_valor_numerico_real(current_price):
        try:
            lp = t.fast_info["lastPrice"]
            if es_valor_numerico_real(lp):
                current_price = lp
                price_source = "fast_info.lastPrice"
        except Exception:
            current_price = None

    if not es_valor_numerico_real(current_price):
        raise ValueError(f"No se pudo obtener el precio actual de {ticker_symbol}.")

    return float(current_price), price_source, t


def descargar_historico_base(ticker_symbol):
    hist = yf.download(ticker_symbol, period="3mo", interval="1d", auto_adjust=True, progress=False)
    if hist is None or hist.empty:
        raise ValueError("No se pudo descargar histórico.")
    if isinstance(hist.columns, pd.MultiIndex):
        hist.columns = hist.columns.get_level_values(0)
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
    try:
        df = yf.download(
            ticker_symbol,
            period="1d",
            interval="1m",
            auto_adjust=True,
            progress=False,
            prepost=True
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


# -------- EARNINGS --------
def obtener_proximo_earnings(ticker_symbol):
    try:
        t = yf.Ticker(ticker_symbol)
        df = t.get_earnings_dates(limit=8)
        if df is None or df.empty:
            return None

        df = df.copy()
        if not isinstance(df.index, pd.RangeIndex):
            df = df.reset_index()

        cols_lower = {str(c).lower(): c for c in df.columns}
        date_col = None
        time_col = None

        for candidate in ["earnings date", "date", "index"]:
            if candidate in cols_lower:
                date_col = cols_lower[candidate]
                break
        for candidate in ["earnings call time", "earnings time", "time"]:
            if candidate in cols_lower:
                time_col = cols_lower[candidate]
                break

        if date_col is None and len(df.columns) > 0:
            date_col = df.columns[0]
        if date_col is None:
            return None

        df["_fecha"] = pd.to_datetime(df[date_col], errors="coerce")
        df = df[df["_fecha"].notna()].copy()
        try:
            df["_fecha"] = df["_fecha"].dt.tz_localize(None)
        except Exception:
            pass

        hoy = pd.Timestamp.today().normalize()
        df = df[df["_fecha"] >= hoy].sort_values("_fecha")
        if df.empty:
            return None

        row = df.iloc[0]
        fecha = normalizar_fecha(row["_fecha"])
        momento_raw = row[time_col] if time_col in df.columns else None

        return {
            "ticker": ticker_symbol,
            "fecha": fecha,
            "dias": dias_restantes(fecha),
            "momento": traducir_momento(momento_raw)
        }
    except Exception:
        return None


def obtener_earnings_mag7(mag7_map):
    earnings_list = []
    for tk, nombre in mag7_map.items():
        data = obtener_proximo_earnings(tk)
        if data:
            earnings_list.append({
                "empresa": nombre,
                "ticker": tk,
                "dias": data["dias"],
                "fecha": data["fecha"],
                "momento": data["momento"]
            })
    return sorted(earnings_list, key=lambda x: (9999 if x["dias"] is None else x["dias"]))


# -------- MACRO --------
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


# -------- VWAP --------
def calcular_vwap_intradia(ticker_symbol, interval="5m", include_prepost=True):
    try:
        df = yf.download(
            ticker_symbol,
            period="1d",
            interval=interval,
            auto_adjust=True,
            progress=False,
            prepost=include_prepost
        )
        if df is None or df.empty:
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df = df[["High", "Low", "Close", "Volume"]].dropna().copy()
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
            "last_intraday_close": float(df["Close"].iloc[-1]),
            "bars": int(len(df))
        }
    except Exception:
        return None


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
    vwap_ext_data = calcular_vwap_intradia(ticker_symbol, interval="5m", include_prepost=True)

    ctx = {
        "vwap_rth": None,
        "vwap_ext": None,
        "dist_to_vwap_rth_pct": None,
        "dist_to_vwap_ext_pct": None,
        "vwap_rth_bias": "VWAP regular no disponible",
        "vwap_ext_bias": "VWAP extended no disponible",
        "vwap_gap_pct": None,
    }

    if vwap_rth_data is not None:
        ctx["vwap_rth"] = vwap_rth_data["vwap"]
        diff = current_price - ctx["vwap_rth"]
        if ctx["vwap_rth"] != 0:
            ctx["dist_to_vwap_rth_pct"] = (diff / ctx["vwap_rth"]) * 100
            ctx["vwap_rth_bias"] = clasificar_bias_vwap(ctx["dist_to_vwap_rth_pct"])

    if vwap_ext_data is not None:
        ctx["vwap_ext"] = vwap_ext_data["vwap"]
        diff = current_price - ctx["vwap_ext"]
        if ctx["vwap_ext"] != 0:
            ctx["dist_to_vwap_ext_pct"] = (diff / ctx["vwap_ext"]) * 100
            ctx["vwap_ext_bias"] = clasificar_bias_vwap(ctx["dist_to_vwap_ext_pct"])

    if es_valor_numerico_real(ctx["vwap_rth"]) and es_valor_numerico_real(ctx["vwap_ext"]) and ctx["vwap_rth"] != 0:
        ctx["vwap_gap_pct"] = ((ctx["vwap_ext"] - ctx["vwap_rth"]) / ctx["vwap_rth"]) * 100

    return ctx


# -------- OPCIONES --------
def obtener_expiracion_cercana(ticker_obj):
    try:
        expirations = ticker_obj.options
        if not expirations:
            return None
        return expirations[0]
    except Exception:
        return None


def obtener_option_chain_snapshot(ticker_symbol, expiration):
    try:
        t = yf.Ticker(ticker_symbol)
        chain = t.option_chain(expiration)
        return {"calls": chain.calls.copy(), "puts": chain.puts.copy()}
    except Exception:
        return None


def buscar_strike_mas_cercano(df, strike_target):
    if df is None or df.empty or "strike" not in df.columns:
        return None
    tmp = df.copy()
    tmp["strike_diff"] = (tmp["strike"] - strike_target).abs()
    tmp = tmp.sort_values("strike_diff")
    if tmp.empty:
        return None
    return tmp.iloc[0].to_dict()


def evaluar_liquidez_call_spread(calls_df, short_strike, long_strike):
    resultado = {
        "liquidez_ok": None,
        "quotes_validas": None,
        "motivos": []
    }

    short_leg = buscar_strike_mas_cercano(calls_df, short_strike)
    long_leg = buscar_strike_mas_cercano(calls_df, long_strike)

    if short_leg is None or long_leg is None:
        resultado["liquidez_ok"] = False
        resultado["quotes_validas"] = False
        resultado["motivos"].append("No se pudieron localizar las patas del spread.")
        return resultado

    def get_num(d, key):
        v = d.get(key, np.nan)
        return None if pd.isna(v) else float(v)

    short_bid = get_num(short_leg, "bid")
    short_ask = get_num(short_leg, "ask")
    long_bid = get_num(long_leg, "bid")
    long_ask = get_num(long_leg, "ask")

    quotes_validas = all(v is not None for v in [short_bid, short_ask, long_bid, long_ask])
    resultado["quotes_validas"] = quotes_validas

    if not quotes_validas:
        resultado["liquidez_ok"] = None
        resultado["motivos"].append("Quotes no válidas o no disponibles.")
        return resultado

    def bid_ask_pct(bid, ask):
        mid = (bid + ask) / 2
        if mid <= 0:
            return None
        return ((ask - bid) / mid) * 100

    short_ba = bid_ask_pct(short_bid, short_ask)
    long_ba = bid_ask_pct(long_bid, long_ask)

    short_oi = short_leg.get("openInterest", 0)
    long_oi = long_leg.get("openInterest", 0)
    short_oi = 0 if pd.isna(short_oi) else int(short_oi)
    long_oi = 0 if pd.isna(long_oi) else int(long_oi)

    liquidez_ok = True
    if short_ba is None or short_ba > 12:
        liquidez_ok = False
        resultado["motivos"].append("Bid-ask amplio en la short call.")
    if long_ba is None or long_ba > 18:
        liquidez_ok = False
        resultado["motivos"].append("Bid-ask amplio en la long call.")
    if short_oi < 100:
        liquidez_ok = False
        resultado["motivos"].append("Open interest bajo en la short call.")
    if long_oi < 50:
        liquidez_ok = False
        resultado["motivos"].append("Open interest bajo en la long call.")

    resultado["liquidez_ok"] = liquidez_ok
    return resultado


# -------- NIVELES / RÉGIMEN --------
def clasificar_regimen_dia(current_price, premarket_high, premarket_low, opening_range_high, opening_range_low):
    if es_valor_numerico_real(opening_range_high) and current_price > opening_range_high:
        return "trend_alcista"
    if es_valor_numerico_real(opening_range_low) and current_price < opening_range_low:
        return "trend_bajista"
    if es_valor_numerico_real(premarket_high) and es_valor_numerico_real(premarket_low):
        if premarket_low <= current_price <= premarket_high:
            return "rango"
    return "indefinido"


def construir_contexto_intradia(ticker_symbol, ticker_obj, short_strike, long_strike, current_price):
    now_ny = get_now_ny()
    tramo = clasificar_tramo_horario_ny(now_ny)

    expiration = obtener_expiracion_cercana(ticker_obj)
    liquidity = None
    if expiration is not None:
        chain_snapshot = obtener_option_chain_snapshot(ticker_symbol, expiration)
        if chain_snapshot is not None:
            liquidity = evaluar_liquidez_call_spread(chain_snapshot["calls"], short_strike, long_strike)

    intraday_1m = descargar_intradia_1m(ticker_symbol)
    premarket_high = None
    premarket_low = None
    opening_range_high = None
    opening_range_low = None
    opening_range_ready = False

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
            if minutos_desde_apertura(now_ny) >= OPENING_RANGE_MINUTES:
                opening_range_ready = opening_range_high is not None and opening_range_low is not None

    regime = clasificar_regimen_dia(
        current_price, premarket_high, premarket_low, opening_range_high, opening_range_low
    )

    return {
        "now_ny": now_ny,
        "tramo_horario": tramo,
        "expiration": expiration,
        "liquidity": liquidity,
        "premarket_high": premarket_high,
        "premarket_low": premarket_low,
        "opening_range_high": opening_range_high,
        "opening_range_low": opening_range_low,
        "opening_range_ready": opening_range_ready,
        "regime": regime,
    }


# -------- SETUP --------
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


# -------- SCORE --------
def calcular_score_operativo(price_source, current_price, trade_setup, vwap_ctx, earnings_list, macro_events, intraday_ctx):
    score = 100.0
    motivos = []
    alertas = []
    bloqueo = False

    if price_source in ["preMarketPrice", "postMarketPrice"]:
        score -= 3
        motivos.append("Precio fuera de horario regular (-3)")

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

    liq = intraday_ctx.get("liquidity")
    if liq is None:
        score -= 5
        motivos.append("Sin validación de liquidez (-5)")
        alertas.append("No hay validación de liquidez")
    else:
        if liq.get("quotes_validas") is False:
            if tramo == "premarket":
                motivos.append("Quotes aún no válidas en premarket")
            else:
                score -= 6
                motivos.append("Quotes no válidas (-6)")
                alertas.append("Bid/ask no utilizable")
        elif liq.get("liquidez_ok") is False:
            score -= 10
            motivos.append("Liquidez insuficiente (-10)")
            alertas.extend(liq.get("motivos", []))
        elif liq.get("liquidez_ok") is True:
            score += 2
            motivos.append("Liquidez aceptable (+2)")

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

    for e in earnings_list:
        if e["dias"] is None:
            continue
        if e["dias"] == 0 and e["momento"] == "Durante la sesión":
            score -= 35
            motivos.append(f"Resultados hoy durante sesión de {e['empresa']} (-35)")
            alertas.append(f"Resultados hoy de {e['empresa']}")
            bloqueo = True
        elif e["dias"] == 1:
            score -= 10
            motivos.append(f"Resultados mañana de {e['empresa']} (-10)")
            alertas.append(f"Resultados mañana de {e['empresa']}")
        elif e["dias"] == 2:
            score -= 5
            motivos.append(f"Resultados en 2 días de {e['empresa']} (-5)")

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
        elif momento == "Antes de la apertura":
            if th <= 12:
                score -= 12
                motivos.append(f"{ev['evento']} antes de apertura en <12h (-12)")
                alertas.append(f"{ev['evento']} antes de apertura")
        elif momento == "Después del cierre":
            if th <= 12:
                score -= 8
                motivos.append(f"{ev['evento']} después del cierre en <12h (-8)")
                alertas.append(f"{ev['evento']} después del cierre")

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


def ajustar_decision_final(trade_setup, intraday_ctx, score_data, price_source):
    decision = trade_setup["decision_base"]
    motivos = []

    tramo = intraday_ctx["tramo_horario"]
    liq = intraday_ctx.get("liquidity")
    quotes_validas = None if liq is None else liq.get("quotes_validas")
    liquidez_ok = None if liq is None else liq.get("liquidez_ok")

    if score_data["bloqueo_operativa"]:
        return "no entraría", ["Bloqueo operativo activado"]

    if tramo == "premarket":
        if quotes_validas is False:
            return "esperar confirmación", ["Premarket sin quotes válidas"]
        if price_source in ["preMarketPrice", "postMarketPrice"]:
            return "esperar confirmación", ["Precio fuera de horario regular"]

    if tramo == "apertura":
        mins = minutos_desde_apertura(intraday_ctx["now_ny"])
        if 0 <= mins < WAIT_MINUTES_AFTER_OPEN:
            return "esperar confirmación", [f"Primeros {WAIT_MINUTES_AFTER_OPEN} minutos"]

    if quotes_validas is False:
        return "esperar confirmación", ["Sin quotes utilizables"]

    if liquidez_ok is False:
        return "no entraría", ["Liquidez insuficiente"]

    if score_data["score"] < 50:
        return "no entraría", ["Score insuficiente"]

    if 50 <= score_data["score"] < 75 and decision == "entraría":
        return "esperar confirmación", ["Score medio"]

    return decision, motivos


# -------- HISTÓRICO --------
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


def build_recent_signals(history, limit=8):
    recent = history[-limit:]
    recent = list(reversed(recent))
    rows = []
    for item in recent:
        rows.append([
            item.get("timestamp", ""),
            item.get("decisionLabel", item.get("decision", "")),
            str(item.get("score", "")),
            fmt_price(item.get("precio")),
            item.get("tramo", "N/D"),
        ])
    return rows


# -------- STATE --------
def construir_state():
    current_price, price_source, ticker_obj = get_price_and_source(TICKER)
    hist = descargar_historico_base(TICKER)
    buffers = calcular_buffers(hist, LOOKBACK_DAYS)
    buffer_pct = buffers["p75_up_move"]

    trade_setup = construir_setup_trade(current_price, buffer_pct)
    vwap_ctx = construir_contexto_vwap(TICKER, current_price)
    earnings_list = obtener_earnings_mag7(MAG7)
    macro_events = preparar_eventos_macro(MACRO_EVENTS)
    intraday_ctx = construir_contexto_intradia(
        TICKER, ticker_obj, trade_setup["short_strike"], trade_setup["long_strike"], current_price
    )
    score_data = calcular_score_operativo(
        price_source, current_price, trade_setup, vwap_ctx, earnings_list, macro_events, intraday_ctx
    )
    decision_final, decision_notes = ajustar_decision_final(trade_setup, intraday_ctx, score_data, price_source)
    tone, decision_label = tone_from_decision(decision_final)

    reasons = []
    base_reasons = score_data["motivos_score"][:4]
    for txt in base_reasons:
        rtone = "ok"
        low = txt.lower()
        if "-" in txt or "riesgo" in low or "bloqueo" in low or "insuficiente" in low:
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
        if "bloqueo" in low or "hoy" in low or "muy cerca" in low:
            tone_a = "danger"
        alerts.append({"tone": tone_a, "title": a, "text": a})

    if not alerts:
        alerts.append({"tone": "ok", "title": "Sin alertas cercanas", "text": "No hay alertas inmediatas por macro o resultados."})

    prox_macro = macro_events[0] if macro_events else None
    prox_earn = earnings_list[0] if earnings_list else None

    context_rows = [
        ["Tramo", intraday_ctx["tramo_horario"].replace("_", " ").title()],
        ["Régimen", intraday_ctx["regime"].replace("_", " ").title()],
        ["Premarket high", fmt_price(intraday_ctx["premarket_high"])],
        ["Premarket low", fmt_price(intraday_ctx["premarket_low"])],
    ]

    events_rows = [
        ["Próximo macro", prox_macro["evento"] if prox_macro else "N/D"],
        ["Impacto", prox_macro["impacto"].title() if prox_macro else "N/D"],
        ["Mag 7 cercano", prox_earn["empresa"] if prox_earn else "N/D"],
        ["Días", str(prox_earn["dias"]) if prox_earn and prox_earn["dias"] is not None else "N/D"],
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


# -------- HTML --------
def html_template():
    return """<!doctype html>
<html lang="es" data-theme="light">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>qqq-spread-dashboard-v2</title>
  <style>
    :root, [data-theme="light"] {
      --font-body: Inter, system-ui, sans-serif;
      --text-xs: clamp(0.75rem, 0.7rem + 0.25vw, 0.875rem);
      --text-sm: clamp(0.875rem, 0.8rem + 0.35vw, 1rem);
      --text-base: clamp(1rem, 0.95rem + 0.25vw, 1.125rem);
      --text-lg: clamp(1.125rem, 1rem + 0.75vw, 1.5rem);
      --text-xl: clamp(1.45rem, 1.1rem + 1.3vw, 2rem);
      --space-1:.25rem; --space-2:.5rem; --space-3:.75rem; --space-4:1rem; --space-5:1.25rem; --space-6:1.5rem; --space-8:2rem;
      --color-bg:#f7f6f2; --color-surface:#f9f8f5; --color-surface-2:#fbfbf9; --color-border:#d4d1ca;
      --color-text:#28251d; --color-text-muted:#6d6b66; --color-text-faint:#9d9b95;
      --color-primary:#01696f; --color-primary-highlight:#dbe9e6; --color-success:#437a22; --color-success-highlight:#dfe9d7;
      --color-warning:#a86b12; --color-warning-highlight:#f0e3c8; --color-error:#a13544; --color-error-highlight:#efd8dc;
      --radius-md:.85rem; --radius-lg:1.15rem; --radius-full:9999px;
      --shadow-sm:0 1px 2px rgba(27,24,18,.05), 0 10px 30px rgba(27,24,18,.04);
      --shadow-md:0 3px 10px rgba(27,24,18,.06), 0 16px 44px rgba(27,24,18,.08);
    }
    [data-theme="dark"] {
      --color-bg:#171614; --color-surface:#1c1b19; --color-surface-2:#22211f; --color-border:#393836;
      --color-text:#ebe8e1; --color-text-muted:#b4b0a7; --color-text-faint:#848077;
      --color-primary:#4f98a3; --color-primary-highlight:#25373a; --color-success:#7db35c; --color-success-highlight:#263022;
      --color-warning:#efb347; --color-warning-highlight:#3d3220; --color-error:#df6d7a; --color-error-highlight:#3d252a;
      --shadow-sm:0 1px 2px rgba(0,0,0,.18), 0 10px 30px rgba(0,0,0,.18);
      --shadow-md:0 3px 10px rgba(0,0,0,.25), 0 16px 44px rgba(0,0,0,.24);
    }
    *{box-sizing:border-box;margin:0;padding:0}
    body{min-height:100dvh;font-family:var(--font-body);font-size:var(--text-base);line-height:1.5;color:var(--color-text);background:var(--color-bg)}
    button{font:inherit;color:inherit}
    .shell{max-width:1120px;margin:0 auto;padding:var(--space-4)}
    .topbar{display:flex;align-items:center;justify-content:space-between;gap:var(--space-4);padding:var(--space-4) 0 var(--space-6)}
    .brand{display:flex;align-items:center;gap:.85rem}
    .logo{width:2.4rem;height:2.4rem;border-radius:.8rem;background:var(--color-primary-highlight);display:grid;place-items:center;color:var(--color-primary);box-shadow:var(--shadow-sm)}
    .brand h1{font-size:var(--text-lg);font-weight:800;letter-spacing:-.03em}
    .brand p{color:var(--color-text-muted);font-size:var(--text-sm)}
    .theme-toggle{min-width:44px;min-height:44px;border:1px solid rgba(0,0,0,.08);background:var(--color-surface);border-radius:var(--radius-full);display:grid;place-items:center;box-shadow:var(--shadow-sm)}
    .hero,.card{background:var(--color-surface);border:1px solid rgba(0,0,0,.08);border-radius:var(--radius-lg)}
    .hero{display:grid;gap:var(--space-4);padding:clamp(1.1rem,3vw,1.8rem);box-shadow:var(--shadow-md)}
    .eyebrow{font-size:var(--text-xs);text-transform:uppercase;letter-spacing:.14em;color:var(--color-text-muted)}
    .hero-main h2{font-size:var(--text-xl);line-height:1.05;letter-spacing:-.04em}
    .hero-main p{color:var(--color-text-muted);max-width:62ch;margin-top:.5rem}
    .decision-row{display:grid;grid-template-columns:1fr;gap:var(--space-3)}
    .decision-pill{display:inline-flex;align-items:center;gap:.6rem;width:fit-content;padding:.7rem 1rem;border-radius:var(--radius-full);font-weight:700;font-size:var(--text-sm)}
    .decision-pill.green{background:var(--color-success-highlight);color:var(--color-success)}
    .decision-pill.yellow{background:var(--color-warning-highlight);color:var(--color-warning)}
    .decision-pill.red{background:var(--color-error-highlight);color:var(--color-error)}
    .grid{display:grid;gap:var(--space-4);margin-top:var(--space-4);grid-template-columns:repeat(12,minmax(0,1fr))}
    .card{grid-column:span 12;padding:var(--space-4);box-shadow:var(--shadow-sm)}
    .card h3{font-size:var(--text-sm);text-transform:uppercase;letter-spacing:.12em;color:var(--color-text-muted);margin-bottom:var(--space-3)}
    .kpis{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:var(--space-3)}
    .kpi{background:var(--color-surface-2);border-radius:var(--radius-md);padding:var(--space-3);border:1px solid rgba(0,0,0,.06)}
    .kpi-label{font-size:var(--text-xs);color:var(--color-text-muted);text-transform:uppercase;letter-spacing:.08em}
    .kpi-value{font-size:clamp(1.15rem,2vw,1.6rem);font-weight:800;margin-top:.2rem;letter-spacing:-.03em}
    .kpi-note{font-size:var(--text-xs);color:var(--color-text-faint);margin-top:.25rem}
    .summary-list,.alerts-list{display:grid;gap:.7rem}
    .summary-item,.alerts-item{display:flex;gap:.75rem;align-items:flex-start;padding:.85rem .95rem;border-radius:var(--radius-md);background:var(--color-surface-2);border:1px solid rgba(0,0,0,.06)}
    .dot{width:.65rem;height:.65rem;border-radius:999px;flex:0 0 auto;margin-top:.4rem;background:var(--color-primary)}
    .dot.warn{background:var(--color-warning)} .dot.danger{background:var(--color-error)} .dot.ok{background:var(--color-success)}
    .summary-item strong,.alerts-item strong{display:block;font-size:var(--text-sm)}
    .summary-item span,.alerts-item span{color:var(--color-text-muted);font-size:var(--text-sm)}
    .mini-table{display:grid;gap:.65rem}
    .mini-row{display:flex;justify-content:space-between;gap:1rem;padding-bottom:.65rem;border-bottom:1px solid rgba(0,0,0,.06)}
    .mini-row:last-child{border-bottom:0;padding-bottom:0}
    .mini-row dt{color:var(--color-text-muted);font-size:var(--text-sm)}
    .mini-row dd{font-weight:700;text-align:right}
    .footer-note{color:var(--color-text-faint);font-size:var(--text-xs);margin:var(--space-6) 0 var(--space-4)}
    table{width:100%;border-collapse:collapse;font-size:var(--text-sm)}
    th,td{padding:.7rem .55rem;border-bottom:1px solid rgba(0,0,0,.08);text-align:left}
    th{color:var(--color-text-muted);font-size:var(--text-xs);text-transform:uppercase;letter-spacing:.08em}
    .status-line{display:flex;flex-wrap:wrap;gap:.75rem;color:var(--color-text-muted);font-size:var(--text-sm)}
    @media (min-width:700px){
      .decision-row{grid-template-columns:1.2fr .8fr}
      .card.span-6{grid-column:span 6}
      .card.span-4{grid-column:span 4}
      .kpis{grid-template-columns:repeat(4,minmax(0,1fr))}
    }
  </style>
</head>
<body>
  <div class="shell">
    <header class="topbar">
      <div class="brand">
        <div class="logo">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9">
            <path d="M4 16 9 11l3 3 8-8"></path>
            <path d="M17 6h3v3"></path>
          </svg>
        </div>
        <div>
          <h1>QQQ Spread Dashboard V2</h1>
          <p>Resumen móvil con histórico y autoactualización</p>
        </div>
      </div>
      <button class="theme-toggle" data-theme-toggle aria-label="Cambiar tema"></button>
    </header>

    <main>
      <section class="hero">
        <div class="eyebrow">Modelo operativo resumido</div>
        <div class="hero-main">
          <h2 id="resumen-title">Cargando...</h2>
          <p>Vista compacta para revisar la decisión, niveles clave y alertas desde cualquier dispositivo de tu red local.</p>
        </div>

        <div class="decision-row">
          <div>
            <div id="decision-pill" class="decision-pill yellow">Cargando</div>
          </div>
          <div class="status-line">
            <div><strong>Actualizado:</strong> <span id="updated-at">-</span></div>
            <div><strong>NY:</strong> <span id="hora-ny">-</span></div>
          </div>
        </div>
      </section>

      <section class="grid">
        <article class="card">
          <h3>Niveles clave</h3>
          <div class="kpis">
            <div class="kpi">
              <div class="kpi-label">Score</div>
              <div class="kpi-value" id="score">-</div>
              <div class="kpi-note" id="semaforo">-</div>
            </div>
            <div class="kpi">
              <div class="kpi-label">Precio</div>
              <div class="kpi-value" id="precio">-</div>
              <div class="kpi-note" id="precio-fuente">-</div>
            </div>
            <div class="kpi">
              <div class="kpi-label">Strikes</div>
              <div class="kpi-value" id="strikes">-</div>
              <div class="kpi-note" id="break-even">-</div>
            </div>
            <div class="kpi">
              <div class="kpi-label">VWAP / salida</div>
              <div class="kpi-value" id="vwap">-</div>
              <div class="kpi-note" id="vwap-bias">-</div>
              <div class="kpi-note" id="salida">-</div>
            </div>
          </div>
        </article>

        <article class="card span-6">
          <h3>Motivos resumidos</h3>
          <div id="reasons-list" class="summary-list"></div>
        </article>

        <article class="card span-6">
          <h3>Alertas</h3>
          <div id="alerts-list" class="alerts-list"></div>
        </article>

        <article class="card span-4">
          <h3>Contexto</h3>
          <dl id="context-table" class="mini-table"></dl>
        </article>

        <article class="card span-4">
          <h3>Eventos</h3>
          <dl id="events-table" class="mini-table"></dl>
        </article>

        <article class="card span-4">
          <h3>Riesgo</h3>
          <dl class="mini-table">
            <div class="mini-row"><dt>Riesgo máximo</dt><dd id="riesgo-max">-</dd></div>
            <div class="mini-row"><dt>Take profit / stop</dt><dd id="salida-riesgo">-</dd></div>
            <div class="mini-row"><dt>Ticker</dt><dd id="ticker-name">QQQ</dd></div>
          </dl>
        </article>

        <article class="card">
          <h3>Últimas señales</h3>
          <table>
            <thead>
              <tr>
                <th>Hora</th>
                <th>Señal</th>
                <th>Score</th>
                <th>Precio</th>
                <th>Tramo</th>
              </tr>
            </thead>
            <tbody id="history-body">
              <tr><td colspan="5">Cargando histórico...</td></tr>
            </tbody>
          </table>
        </article>
      </section>

      <p class="footer-note">La página se refresca sola leyendo state.json e history.json. Para verla en móvil, usa la URL local que imprime el script.</p>
    </main>
  </div>

  <script>
    function safe(v, fallback='N/D') {
      return v === null || v === undefined || v === '' ? fallback : v;
    }

    function renderList(targetId, items) {
      const target = document.getElementById(targetId);
      target.innerHTML = items.map(item => `
        <div class="${targetId === 'alerts-list' ? 'alerts-item' : 'summary-item'}">
          <div class="dot ${item.tone}"></div>
          <div>
            <strong>${safe(item.title)}</strong>
            <span>${safe(item.text)}</span>
          </div>
        </div>
      `).join('');
    }

    function renderTable(targetId, rows) {
      const target = document.getElementById(targetId);
      target.innerHTML = rows.map(([k, v]) => `
        <div class="mini-row"><dt>${safe(k)}</dt><dd>${safe(v)}</dd></div>
      `).join('');
    }

    function renderHistory(history) {
      const body = document.getElementById('history-body');
      if (!history || !history.length) {
        body.innerHTML = '<tr><td colspan="5">Sin histórico todavía.</td></tr>';
        return;
      }
      body.innerHTML = history.map(item => `
        <tr>
          <td>${safe(item[0])}</td>
          <td>${safe(item[1])}</td>
          <td>${safe(item[2])}</td>
          <td>${safe(item[3])}</td>
          <td>${safe(item[4])}</td>
        </tr>
      `).join('');
    }

    function renderState(state) {
      document.getElementById('decision-pill').className = `decision-pill ${state.decisionTone}`;
      document.getElementById('decision-pill').textContent = state.decisionLabel;
      document.getElementById('resumen-title').textContent = `${state.decisionLabel.replace(/^.[ ]*/, '')} antes de vender el bear call spread.`;
      document.getElementById('updated-at').textContent = safe(state.updatedAt);
      document.getElementById('hora-ny').textContent = safe(state.horaNy);
      document.getElementById('score').textContent = `${safe(state.score)} / 100`;
      document.getElementById('semaforo').textContent = safe(state.semaforo);
      document.getElementById('precio').textContent = state.precio !== null && state.precio !== undefined ? `$${Number(state.precio).toFixed(2)}` : 'N/D';
      document.getElementById('precio-fuente').textContent = safe(state.precioFuente);
      document.getElementById('strikes').textContent = `${safe(state.shortStrike)} / ${safe(state.longStrike)}`;
      document.getElementById('break-even').textContent = state.breakeven !== null && state.breakeven !== undefined ? `Break-even ${Number(state.breakeven).toFixed(2)}` : 'Break-even N/D';
      document.getElementById('vwap').textContent = state.vwap !== null && state.vwap !== undefined ? Number(state.vwap).toFixed(2) : 'N/D';
      document.getElementById('vwap-bias').textContent = safe(state.vwapBias);
      const salidaTxt = `${Number(state.tp).toFixed(2)} / ${Number(state.stop).toFixed(2)}`;
      document.getElementById('salida').textContent = salidaTxt;
      document.getElementById('salida-riesgo').textContent = salidaTxt;
      document.getElementById('riesgo-max').textContent = state.riesgoMax !== null && state.riesgoMax !== undefined ? `$${Number(state.riesgoMax).toFixed(2)}` : 'N/D';
      document.getElementById('ticker-name').textContent = safe(state.ticker);
      renderList('reasons-list', state.reasons || []);
      renderList('alerts-list', state.alerts || []);
      renderTable('context-table', state.context || []);
      renderTable('events-table', state.events || []);
    }

    async function loadAll() {
      try {
        const stateRes = await fetch('./state.json?_=' + Date.now(), { cache: 'no-store' });
        const state = await stateRes.json();
        renderState(state);
      } catch (e) {
        console.error('Error cargando state.json', e);
      }

      try {
        const histRes = await fetch('./history.json?_=' + Date.now(), { cache: 'no-store' });
        const historyRaw = await histRes.json();
        const recent = historyRaw.slice(-8).reverse().map(item => [
          item.timestamp || '',
          item.decisionLabel || item.decision || '',
          String(item.score ?? ''),
          item.precio != null ? `$${Number(item.precio).toFixed(2)}` : 'N/D',
          item.tramo || 'N/D'
        ]);
        renderHistory(recent);
      } catch (e) {
        console.error('Error cargando history.json', e);
      }
    }

    (function () {
      const root = document.documentElement;
      const toggle = document.querySelector('[data-theme-toggle]');
      let theme = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
      root.setAttribute('data-theme', theme);

      function paint() {
        toggle.innerHTML = theme === 'dark'
          ? '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"></circle><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"></path></svg>'
          : '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path></svg>';
      }

      paint();
      toggle.addEventListener('click', () => {
        theme = theme === 'dark' ? 'light' : 'dark';
        root.setAttribute('data-theme', theme);
        paint();
      });
    })();

    loadAll();
    setInterval(loadAll, 15000);
  </script>
</body>
</html>
"""


# -------- SERVER --------
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
    os_cwd = Path.cwd()
    if os_cwd != BASE_DIR:
        import os
        os.chdir(BASE_DIR)

    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), SilentHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


# -------- OUTPUT --------
def write_dashboard_assets(state):
    save_json_file(STATE_FILE, state)
    history = append_history(state)
    save_json_file(HISTORY_FILE, history)
    if not HTML_FILE.exists():
        HTML_FILE.write_text(html_template(), encoding="utf-8")
    return history


def run_once():
    state = construir_state()
    history = write_dashboard_assets(state)
    return state, history


def main():
    if not HTML_FILE.exists():
        HTML_FILE.write_text(html_template(), encoding="utf-8")

    server = None
    if SERVE_LOCAL:
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

    print("Iniciando bucle de actualización...")
    print(f"Frecuencia: cada {AUTO_REFRESH_SECONDS} segundos")
    print("Pulsa Ctrl+C para detener.")

    try:
        while True:
            try:
                state, history = run_once()
                print(
                    f"[{state['updatedAt']}] {state['decisionLabel']} | "
                    f"Score {state['score']} | Precio {state['precio']} | "
                    f"Histórico {len(history)}"
                )
            except Exception as e:
                print(f"Error en actualización: {e}")
            time.sleep(AUTO_REFRESH_SECONDS)
    except KeyboardInterrupt:
        print("Detenido por usuario.")
        if server:
            server.shutdown()


if __name__ == "__main__":
    main()
