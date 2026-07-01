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


def calculate_score(change_pct, trade_levels, vwap_dist_pct, option_snapshot, session_code):
    score = 100
    reasons = []

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

    score = max(0, min(100, round(score, 2)))
    return score, reasons[:6]


def make_decision(score, trade_levels, session_code):
    dist_to_short = trade_levels["distToShort"]

    if dist_to_short <= 0:
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
    score, reasons = calculate_score(change_pct, levels, vwap_dist_pct, option_snapshot, session_code)
    decision, decision_tone, decision_label, risk_label = make_decision(score, levels, session_code)

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
        "reasons": reasons
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
        f"session={state['session']['label']} | score={state['score']} | "
        f"decision={state['decision']} | history={len(history)}"
    )


if __name__ == "__main__":
    main()
