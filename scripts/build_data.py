import json
import math
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf


TICKER = "QQQ"
MAX_HISTORY_ITEMS = 200
SPREAD_WIDTH = 1.0
NET_CREDIT = 0.10
MODEL_BUFFER_PCT = 0.0185

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
STATE_FILE = DATA_DIR / "state.json"
HISTORY_FILE = DATA_DIR / "history.json"

NY_TZ = ZoneInfo("America/New_York")

MAG7 = {
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "NVDA": "Nvidia",
    "AMZN": "Amazon",
    "META": "Meta",
    "GOOGL": "Alphabet",
    "TSLA": "Tesla",
}

MACRO_EVENTS = [
    {"evento": "ISM Manufacturero", "datetime_ny": "2026-07-01 10:00", "impacto": "alto"},
    {"evento": "Nóminas no agrícolas (NFP)", "datetime_ny": "2026-07-03 08:30", "impacto": "alto"},
    {"evento": "Tasa de desempleo", "datetime_ny": "2026-07-03 08:30", "impacto": "alto"},
    {"evento": "IPC (CPI)", "datetime_ny": "2026-07-15 08:30", "impacto": "alto"},
    {"evento": "IPP (PPI)", "datetime_ny": "2026-07-16 08:30", "impacto": "alto"},
    {"evento": "Ventas minoristas", "datetime_ny": "2026-07-16 08:30", "impacto": "medio"},
    {"evento": "PIB", "datetime_ny": "2026-07-30 08:30", "impacto": "alto"},
    {"evento": "PCE subyacente", "datetime_ny": "2026-07-31 08:30", "impacto": "alto"},
    {"evento": "Decisión FOMC / tipos", "datetime_ny": "2026-07-29 14:00", "impacto": "alto"},
]


def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_json(path, default):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def flatten_columns(df):
    if df is None or df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def safe_float(value, default=None):
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def normalize_date(value):
    if value is None:
        return None
    try:
        ts = pd.to_datetime(value)
        if pd.isna(ts):
            return None
        try:
            ts = ts.tz_localize(None)
        except Exception:
            pass
        return ts.date()
    except Exception:
        return None


def days_until(d, base_date=None):
    if d is None:
        return None
    base = base_date or datetime.now(NY_TZ).date()
    return (d - base).days


def translate_earnings_moment(text):
    if text is None:
        return "Hora no especificada"
    t = str(text).strip().lower()
    if "before market open" in t or "before open" in t:
        return "Antes de la apertura"
    if "after market close" in t or "after close" in t:
        return "Después del cierre"
    if "during market" in t or "market hours" in t or t == "tas":
        return "Durante la sesión"
    if "time not supplied" in t:
        return "Hora no especificada"
    return str(text)


def classify_session(now_ny):
    minutes = now_ny.hour * 60 + now_ny.minute
    open_m = 9 * 60 + 30
    noon_m = 12 * 60
    power_m = 15 * 60
    close_m = 16 * 60

    if minutes < 4 * 60:
        return "overnight", "Overnight"
    if 4 * 60 <= minutes < open_m:
        return "premarket", "Premarket"
    if open_m <= minutes < noon_m:
        return "apertura", "Apertura"
    if noon_m <= minutes < power_m:
        return "media_sesion", "Media sesión"
    if power_m <= minutes <= close_m:
        return "power_hour", "Power hour"
    if close_m < minutes <= 20 * 60:
        return "after_hours", "After hours"
    return "overnight", "Overnight"


def classify_macro_moment(dt_ny):
    mins = dt_ny.hour * 60 + dt_ny.minute
    open_m = 9 * 60 + 30
    close_m = 16 * 60
    if mins < open_m:
        return "Antes de la apertura"
    if mins <= close_m:
        return "Durante la sesión"
    return "Después del cierre"


def get_intraday_df():
    df = yf.download(
        tickers=TICKER,
        period="2d",
        interval="5m",
        auto_adjust=True,
        progress=False,
        prepost=True,
        threads=False,
    )
    df = flatten_columns(df)
    if df is None or df.empty:
        return None
    return df


def get_daily_df():
    df = yf.download(
        tickers=TICKER,
        period="5d",
        interval="1d",
        auto_adjust=True,
        progress=False,
        prepost=False,
        threads=False,
    )
    df = flatten_columns(df)
    if df is None or df.empty:
        return None
    return df


def get_price_snapshot():
    intraday = get_intraday_df()
    if intraday is not None and "Close" in intraday.columns:
        closes = intraday["Close"].dropna()
        if not closes.empty:
            last_price = float(closes.iloc[-1])

            daily_df = get_daily_df()
            prev_close = None
            if daily_df is not None and "Close" in daily_df.columns:
                daily_closes = daily_df["Close"].dropna()
                if len(daily_closes) >= 2:
                    prev_close = float(daily_closes.iloc[-2])
                elif len(daily_closes) == 1:
                    prev_close = float(daily_closes.iloc[-1])

            return {
                "price": last_price,
                "prev_close": prev_close,
                "source": "intraday_5m",
                "intraday_df": intraday
            }

    daily_df = get_daily_df()
    if daily_df is not None and "Close" in daily_df.columns:
        daily_closes = daily_df["Close"].dropna()
        if not daily_closes.empty:
            last_price = float(daily_closes.iloc[-1])
            prev_close = float(daily_closes.iloc[-2]) if len(daily_closes) >= 2 else None
            return {
                "price": last_price,
                "prev_close": prev_close,
                "source": "daily_close",
                "intraday_df": None
            }

    raise RuntimeError("No se pudo obtener el precio de QQQ.")


def compute_change(price, prev_close):
    if prev_close is None or prev_close == 0:
        return None, None
    chg = price - prev_close
    chg_pct = (chg / prev_close) * 100
    return chg, chg_pct


def compute_vwap(intraday_df):
    if intraday_df is None or intraday_df.empty:
        return None, None, "VWAP no disponible"

    needed = {"High", "Low", "Close", "Volume"}
    if not needed.issubset(set(intraday_df.columns)):
        return None, None, "VWAP no disponible"

    df = intraday_df.copy().dropna(subset=["High", "Low", "Close", "Volume"])
    if df.empty:
        return None, None, "VWAP no disponible"

    vol = df["Volume"].astype(float)
    if vol.sum() <= 0:
        return None, None, "VWAP no disponible"

    typical_price = (df["High"] + df["Low"] + df["Close"]) / 3.0
    df["tpv"] = typical_price * vol
    df["cum_tpv"] = df["tpv"].cumsum()
    df["cum_vol"] = vol.cumsum()
    df["vwap"] = df["cum_tpv"] / df["cum_vol"]

    vwap_value = float(df["vwap"].iloc[-1])
    current_price = float(df["Close"].iloc[-1])
    dist_pct = ((current_price - vwap_value) / vwap_value) * 100 if vwap_value else None

    if dist_pct is None:
        bias = "VWAP no disponible"
    elif dist_pct >= 0.75:
        bias = "Muy por encima"
    elif dist_pct >= 0.30:
        bias = "Por encima"
    elif dist_pct <= -0.75:
        bias = "Muy por debajo"
    elif dist_pct <= -0.30:
        bias = "Por debajo"
    else:
        bias = "Cerca del VWAP"

    return round(vwap_value, 2), round(dist_pct, 2) if dist_pct is not None else None, bias


def build_trade_levels(price):
    projected_upside_price = price * (1 + MODEL_BUFFER_PCT)
    short_strike = math.ceil(projected_upside_price)
    long_strike = short_strike + SPREAD_WIDTH
    breakeven = short_strike + NET_CREDIT
    dist_to_short = short_strike - price

    return {
        "bufferPct": round(MODEL_BUFFER_PCT * 100, 2),
        "shortStrike": short_strike,
        "longStrike": long_strike,
        "breakeven": round(breakeven, 2),
        "spreadWidth": SPREAD_WIDTH,
        "netCredit": NET_CREDIT,
        "distToShort": round(dist_to_short, 2)
    }


def get_option_snapshot(price):
    try:
        ticker = yf.Ticker(TICKER)
        expirations = list(ticker.options)
        if not expirations:
            return {
                "expiration": None,
                "shortCallBid": None,
                "shortCallAsk": None,
                "longCallBid": None,
                "longCallAsk": None,
                "notes": "Sin expiraciones disponibles"
            }

        expiry = expirations[0]
        chain = ticker.option_chain(expiry)
        calls = chain.calls.copy()

        if calls.empty or "strike" not in calls.columns:
            return {
                "expiration": expiry,
                "shortCallBid": None,
                "shortCallAsk": None,
                "longCallBid": None,
                "longCallAsk": None,
                "notes": "Sin calls disponibles"
            }

        levels = build_trade_levels(price)
        short_strike = levels["shortStrike"]
        long_strike = levels["longStrike"]

        calls["strike_diff_short"] = (calls["strike"] - short_strike).abs()
        calls["strike_diff_long"] = (calls["strike"] - long_strike).abs()

        short_row = calls.sort_values("strike_diff_short").iloc[0]
        long_row = calls.sort_values("strike_diff_long").iloc[0]

        return {
            "expiration": expiry,
            "shortCallBid": safe_float(short_row.get("bid")),
            "shortCallAsk": safe_float(short_row.get("ask")),
            "longCallBid": safe_float(long_row.get("bid")),
            "longCallAsk": safe_float(long_row.get("ask")),
            "notes": "OK"
        }
    except Exception as e:
        return {
            "expiration": None,
            "shortCallBid": None,
            "shortCallAsk": None,
            "longCallBid": None,
            "longCallAsk": None,
            "notes": f"Options no disponibles: {str(e)}"
        }


def get_next_earnings_for_ticker(ticker_symbol):
    try:
        t = yf.Ticker(ticker_symbol)
        df = t.get_earnings_dates(limit=8)

        if df is None or df.empty:
            return {
                "ticker": ticker_symbol,
                "fecha": None,
                "dias": None,
                "momento": "No disponible",
                "status": "Vacío en Yahoo"
            }

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

        if date_col is None:
            return {
                "ticker": ticker_symbol,
                "fecha": None,
                "dias": None,
                "momento": "No disponible",
                "status": "Columna de fecha no encontrada"
            }

        df["_fecha"] = pd.to_datetime(df[date_col], errors="coerce")
        df = df[df["_fecha"].notna()].copy()

        try:
            df["_fecha"] = df["_fecha"].dt.tz_localize(None)
        except Exception:
            pass

        today = datetime.now(NY_TZ).date()
        df["_fecha_date"] = df["_fecha"].dt.date
        df = df[df["_fecha_date"] >= today].sort_values("_fecha")

        if df.empty:
            return {
                "ticker": ticker_symbol,
                "fecha": None,
                "dias": None,
                "momento": "No disponible",
                "status": "Sin fechas futuras"
            }

        row = df.iloc[0]
        fecha = normalize_date(row["_fecha"])
        momento_raw = row[time_col] if time_col in df.columns else None

        return {
            "ticker": ticker_symbol,
            "fecha": fecha.isoformat() if fecha else None,
            "dias": days_until(fecha, today),
            "momento": translate_earnings_moment(momento_raw),
            "status": "OK"
        }
    except Exception as e:
        return {
            "ticker": ticker_symbol,
            "fecha": None,
            "dias": None,
            "momento": "No disponible",
            "status": f"Error Yahoo: {str(e)}"
        }


def get_mag7_earnings():
    rows = []
    ok_count = 0

    for tk, nombre in MAG7.items():
        item = get_next_earnings_for_ticker(tk)
        row = {
            "empresa": nombre,
            "ticker": tk,
            "fecha": item.get("fecha"),
            "dias": item.get("dias"),
            "momento": item.get("momento"),
            "status": item.get("status", "No disponible")
        }
        if row["status"] == "OK":
            ok_count += 1
        rows.append(row)

    rows.sort(key=lambda x: (
        0 if x["dias"] is not None else 1,
        9999 if x["dias"] is None else x["dias"]
    ))

    next_item = next((r for r in rows if r["status"] == "OK"), None)

    status = "OK" if ok_count > 0 else "Yahoo no devolvió earnings"
    return {
        "status": status,
        "next": next_item,
        "items": rows[:7]
    }


def get_upcoming_macro():
    now_ny = datetime.now(NY_TZ)
    rows = []

    for ev in MACRO_EVENTS:
        try:
            dt_ny = datetime.strptime(ev["datetime_ny"], "%Y-%m-%d %H:%M").replace(tzinfo=NY_TZ)
            delta_h = (dt_ny - now_ny).total_seconds() / 3600

            rows.append({
                "evento": ev["evento"],
                "impacto": ev["impacto"],
                "datetimeNY": dt_ny.strftime("%Y-%m-%d %H:%M ET"),
                "dateNY": dt_ny.strftime("%Y-%m-%d"),
                "timeNY": dt_ny.strftime("%H:%M"),
                "dias": int(delta_h // 24) if delta_h >= 0 else int(delta_h // 24),
                "horas": int(abs(delta_h) % 24),
                "totalHoras": round(delta_h, 2),
                "momento": classify_macro_moment(dt_ny),
                "status": "upcoming" if delta_h >= 0 else "past"
            })
        except Exception:
            continue

    rows.sort(key=lambda x: x["totalHoras"])

    upcoming = [r for r in rows if r["totalHoras"] >= 0]
    past = [r for r in rows if r["totalHoras"] < 0]

    recent = None
    recent_today = [r for r in past if r["dateNY"] == now_ny.strftime("%Y-%m-%d")]
    if recent_today:
        recent = sorted(recent_today, key=lambda x: x["totalHoras"], reverse=True)[0]
    elif past:
        recent = sorted(past, key=lambda x: x["totalHoras"], reverse=True)[0]

    next_item = upcoming[0] if upcoming else None

    if next_item and recent:
        status = "OK | reciente + próximo"
    elif next_item:
        status = "OK | solo próximo"
    elif recent:
        status = "OK | solo reciente"
    else:
        status = "Sin macro en la lista manual"

    return {
        "status": status,
        "next": next_item,
        "recent": recent,
        "items": upcoming[:5],
        "all": rows[:12]
    }


def calculate_score(change_pct, trade_levels, vwap_dist_pct, option_snapshot, session_code, macro_block, earnings_block):
    score = 100
    reasons = []
    alerts = []

    dist_to_short = trade_levels["distToShort"]
    if dist_to_short <= 0:
        score -= 45
        reasons.append("Precio en o sobre short strike")
    elif dist_to_short <= 1:
        score -= 30
        reasons.append("Short strike muy cerca")
    elif dist_to_short <= 2:
        score -= 18
        reasons.append("Short strike algo cerca")
    elif dist_to_short <= 3:
        score -= 8
        reasons.append("Margen limitado al short")
    else:
        reasons.append("Distancia razonable al short")

    if change_pct is not None:
        if change_pct >= 1.5:
            score -= 15
            reasons.append("Sesgo alcista fuerte hoy")
        elif change_pct >= 0.75:
            score -= 8
            reasons.append("Sesgo alcista moderado")
        elif change_pct <= -0.75:
            score += 4
            reasons.append("Sesgo bajista favorable")
        else:
            reasons.append("Cambio diario contenido")

    if vwap_dist_pct is not None:
        if vwap_dist_pct >= 1.0:
            score -= 12
            reasons.append("Muy por encima del VWAP")
        elif vwap_dist_pct >= 0.5:
            score -= 7
            reasons.append("Por encima del VWAP")
        elif vwap_dist_pct <= -0.5:
            score += 3
            reasons.append("Por debajo del VWAP")
        else:
            reasons.append("Cerca del VWAP")

    bid = option_snapshot.get("shortCallBid")
    ask = option_snapshot.get("shortCallAsk")
    if bid is None or ask is None:
        score -= 6
        reasons.append("Sin quote válida en short call")
    else:
        mid = (bid + ask) / 2 if bid is not None and ask is not None else None
        spread_pct = ((ask - bid) / mid) * 100 if mid and mid > 0 else None
        if spread_pct is not None and spread_pct > 20:
            score -= 10
            reasons.append("Bid/ask muy amplio")
        elif spread_pct is not None and spread_pct > 12:
            score -= 5
            reasons.append("Bid/ask algo amplio")
        else:
            reasons.append("Bid/ask razonable")

    if session_code == "premarket":
        score -= 8
        reasons.append("Premarket: menor liquidez")
    elif session_code == "apertura":
        score -= 6
        reasons.append("Apertura: más volatilidad")
    elif session_code == "media_sesion":
        score += 2
        reasons.append("Media sesión más estable")
    elif session_code == "power_hour":
        score -= 8
        reasons.append("Power hour: más agresiva")
    elif session_code == "after_hours":
        score -= 12
        reasons.append("After hours: spreads peores")
    elif session_code == "overnight":
        score -= 15
        reasons.append("Overnight: sin mercado regular")

    for ev in macro_block.get("items", [])[:3]:
        if ev["impacto"] != "alto":
            continue
        if ev["momento"] == "Durante la sesión" and ev["totalHoras"] <= 24:
            score -= 25
            reasons.append(f"Macro alta en <24h: {ev['evento']}")
            alerts.append(f"{ev['evento']} en {ev['dias']}d {ev['horas']}h")
        elif ev["momento"] == "Antes de la apertura" and ev["totalHoras"] <= 12:
            score -= 12
            reasons.append(f"Macro antes de apertura: {ev['evento']}")
            alerts.append(f"{ev['evento']} antes de la apertura")
        elif ev["totalHoras"] <= 48:
            score -= 6
            reasons.append(f"Macro próxima: {ev['evento']}")

    ok_earnings = [e for e in earnings_block.get("items", []) if e.get("status") == "OK"]

    for er in ok_earnings[:4]:
        if er["dias"] is None:
            continue
        if er["dias"] == 0 and er["momento"] == "Durante la sesión":
            score -= 30
            reasons.append(f"Resultados hoy en sesión: {er['empresa']}")
            alerts.append(f"Resultados hoy de {er['empresa']}")
        elif er["dias"] == 1:
            score -= 10
            reasons.append(f"Resultados mañana: {er['empresa']}")
            alerts.append(f"Resultados mañana de {er['empresa']}")
        elif er["dias"] == 2:
            score -= 5
            reasons.append(f"Resultados en 2 días: {er['empresa']}")

    score = max(0, min(100, round(score, 2)))
    return score, reasons[:8], alerts[:6]


def make_decision(score, trade_levels, session_code, alerts):
    dist_to_short = trade_levels["distToShort"]

    if dist_to_short <= 0:
        return "no entraría", "red", "🔴 No entraría", "🔴 Riesgo alto"

    if any("Resultados hoy" in a for a in alerts):
        return "no entraría", "red", "🔴 No entraría", "🔴 Riesgo alto"

    if session_code in ["after_hours", "overnight"]:
        if score >= 75:
            return "esperar confirmación", "yellow", "🟡 Esperar confirmación", "🟡 Riesgo medio"
        return "no entraría", "red", "🔴 No entraría", "🔴 Riesgo alto"

    if session_code == "premarket" and score >= 75:
        return "esperar confirmación", "yellow", "🟡 Esperar confirmación", "🟡 Riesgo medio"

    if score >= 75:
        return "entraría", "green", "🟢 Entraría", "🟢 Riesgo controlado"

    if score >= 50:
        return "esperar confirmación", "yellow", "🟡 Esperar confirmación", "🟡 Riesgo medio"

    return "no entraría", "red", "🔴 No entraría", "🔴 Riesgo alto"


def build_state():
    now_utc = datetime.now(timezone.utc)
    now_ny = now_utc.astimezone(NY_TZ)
    session_code, session_label = classify_session(now_ny)

    snapshot = get_price_snapshot()
    price = snapshot["price"]
    prev_close = snapshot["prev_close"]
    source = snapshot["source"]
    intraday_df = snapshot["intraday_df"]

    change, change_pct = compute_change(price, prev_close)
    levels = build_trade_levels(price)
    option_snapshot = get_option_snapshot(price)
    vwap_value, vwap_dist_pct, vwap_bias = compute_vwap(intraday_df)

    macro_block = get_upcoming_macro()
    earnings_block = get_mag7_earnings()

    score, reasons, alerts = calculate_score(
        change_pct, levels, vwap_dist_pct, option_snapshot, session_code, macro_block, earnings_block
    )
    decision, decision_tone, decision_label, risk_label = make_decision(score, levels, session_code, alerts)

    tone = "flat"
    if change is not None:
        tone = "up" if change > 0 else "down" if change < 0 else "flat"

    return {
        "ticker": TICKER,
        "price": round(price, 2),
        "prevClose": round(prev_close, 2) if prev_close is not None else None,
        "change": round(change, 2) if change is not None else None,
        "changePct": round(change_pct, 2) if change_pct is not None else None,
        "tone": tone,
        "source": source,
        "updatedAt": now_utc.isoformat(),
        "updatedAtText": now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "updatedAtNY": now_ny.strftime("%Y-%m-%d %H:%M:%S ET"),
        "session": {
            "code": session_code,
            "label": session_label
        },
        "trade": levels,
        "options": option_snapshot,
        "vwap": {
            "value": vwap_value,
            "distPct": vwap_dist_pct,
            "bias": vwap_bias
        },
        "score": score,
        "decision": decision,
        "decisionTone": decision_tone,
        "decisionLabel": decision_label,
        "riskLabel": risk_label,
        "reasons": reasons,
        "alerts": alerts,
        "macro": macro_block,
        "earnings": earnings_block
    }


def update_history(state):
    history = load_json(HISTORY_FILE, [])

    row = {
        "updatedAt": state["updatedAt"],
        "updatedAtText": state["updatedAtText"],
        "updatedAtNY": state["updatedAtNY"],
        "ticker": state["ticker"],
        "price": state["price"],
        "change": state["change"],
        "changePct": state["changePct"],
        "tone": state["tone"],
        "source": state["source"],
        "score": state["score"],
        "decisionLabel": state["decisionLabel"],
        "sessionLabel": state["session"]["label"]
    }

    if history and history[-1].get("updatedAt") == row["updatedAt"]:
        history[-1] = row
    else:
        history.append(row)

    history = history[-MAX_HISTORY_ITEMS:]
    save_json(HISTORY_FILE, history)
    return history


def main():
    ensure_dirs()
    state = build_state()
    save_json(STATE_FILE, state)
    history = update_history(state)

    print(
        f"OK | {state['ticker']} | price={state['price']} | "
        f"macro_status={state['macro']['status']} | "
        f"earnings_status={state['earnings']['status']} | "
        f"score={state['score']} | history={len(history)}"
    )


if __name__ == "__main__":
    main()
