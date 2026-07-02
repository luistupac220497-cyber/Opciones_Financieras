import json
import math
from datetime import datetime, timedelta, timezone, date
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
STATE_FILE = DATA_DIR / "state.json"

TICKER = "QQQ"
BASE_BUFFER_PCT = 1.85
SPREAD_WIDTH = 1.0
TARGET_SHORT_DELTA = 0.15

NY_TZ = timezone(timedelta(hours=-4))


def now_utc():
    return datetime.now(timezone.utc)


def now_ny():
    return now_utc().astimezone(NY_TZ)


def safe_float(v, default=None):
    try:
        if v is None:
            return default
        x = float(v)
        if math.isnan(x):
            return default
        return x
    except Exception:
        return default


def round2(v):
    return None if v is None else round(float(v), 2)


def ceil_strike(x, step=1.0):
    if x is None:
        return None
    return math.ceil(x / step) * step


def json_safe(obj):
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [json_safe(x) for x in obj]
    if isinstance(obj, tuple):
        return [json_safe(x) for x in obj]
    return obj


def get_session_label(ny_dt):
    mins = ny_dt.hour * 60 + ny_dt.minute
    if mins < 4 * 60:
        return {"code": "overnight", "label": "Overnight"}
    if mins < 9 * 60 + 30:
        return {"code": "premarket", "label": "Premarket"}
    if mins < 16 * 60:
        return {"code": "regular", "label": "Regular"}
    return {"code": "afterhours", "label": "After hours"}


def get_price_data(ticker):
    tk = yf.Ticker(ticker)

    hist_5m = None
    hist_1d = None

    try:
        hist_5m = tk.history(period="1d", interval="5m", auto_adjust=False, prepost=True)
    except Exception:
        pass

    try:
        hist_1d = tk.history(period="5d", interval="1d", auto_adjust=False, prepost=True)
    except Exception:
        pass

    price = prev_close = change = change_pct = None
    source = "intraday_5m"

    if hist_5m is not None and not hist_5m.empty:
        price = safe_float(hist_5m.iloc[-1]["Close"])

    if hist_1d is not None and len(hist_1d) >= 2:
        prev_close = safe_float(hist_1d.iloc[-2]["Close"])

    if price is not None and prev_close not in (None, 0):
        change = price - prev_close
        change_pct = (change / prev_close) * 100

    tone = "up" if (change or 0) > 0 else "down" if (change or 0) < 0 else "flat"

    return tk, hist_5m, {
        "price": round2(price),
        "prevClose": round2(prev_close),
        "change": round2(change),
        "changePct": round2(change_pct),
        "tone": tone,
        "source": source
    }


def compute_vwap(hist):
    if hist is None or hist.empty:
        return {"value": None, "distPct": None, "zScore": None, "sigma": None, "bias": "No disponible"}

    tp = (hist["High"] + hist["Low"] + hist["Close"]) / 3.0
    vol = hist["Volume"].fillna(0)
    if vol.sum() == 0:
        return {"value": None, "distPct": None, "zScore": None, "sigma": None, "bias": "No disponible"}

    vwap = float((tp * vol).sum() / vol.sum())
    closes = hist["Close"].dropna()
    sigma = float(closes.std()) if len(closes) > 5 else None
    price = safe_float(hist.iloc[-1]["Close"])
    dist_pct = ((price - vwap) / vwap) * 100 if price is not None and vwap else None
    z_score = ((price - vwap) / sigma) if price is not None and vwap is not None and sigma not in (None, 0) else None

    if dist_pct is None:
        bias = "No disponible"
    elif dist_pct < -0.8:
        bias = "Muy por debajo"
    elif dist_pct < -0.2:
        bias = "Por debajo"
    elif dist_pct > 0.8:
        bias = "Muy por encima"
    elif dist_pct > 0.2:
        bias = "Por encima"
    else:
        bias = "Cerca"

    return {
        "value": round2(vwap),
        "distPct": round2(dist_pct),
        "zScore": round2(z_score),
        "sigma": round2(sigma),
        "bias": bias
    }


def compute_opening_range(hist):
    if hist is None or hist.empty:
        return {
            "available": False,
            "status": "Pendiente",
            "message": "Opening Range no disponible"
        }

    ny = now_ny()
    if ny.hour < 9 or (ny.hour == 9 and ny.minute < 30):
        return {
            "available": False,
            "status": "Pendiente",
            "message": "Opening Range disponible a partir de 09:30 ET"
        }

    return {
        "available": False,
        "status": "Pendiente",
        "message": "Opening Range pendiente de cálculo en esta versión"
    }


def compute_premarket_range(hist):
    if hist is None or hist.empty:
        return {
            "available": False,
            "status": "No disponible",
            "message": "Premarket Range no disponible"
        }

    try:
        pm = hist.between_time("04:00", "09:29")
    except Exception:
        pm = hist

    if pm is None or pm.empty:
        return {
            "available": False,
            "status": "No disponible",
            "message": "Premarket Range no disponible"
        }

    high = safe_float(pm["High"].max())
    low = safe_float(pm["Low"].min())
    close = safe_float(pm.iloc[-1]["Close"])
    size = None if high is None or low is None else high - low
    size_pct = None if close in (None, 0) or size is None else (size / close) * 100

    return {
        "available": True,
        "status": "OK",
        "message": "Premarket Range calculado",
        "high": round2(high),
        "low": round2(low),
        "close": round2(close),
        "size": round2(size),
        "sizePct": round2(size_pct)
    }


def compute_expected_move(tk, spot):
    try:
        hist = tk.history(period="3mo", interval="1d", auto_adjust=False)
        if hist is not None and len(hist) > 10:
            rets = hist["Close"].pct_change().dropna()
            daily_vol = float(rets.std()) * 100
            move = spot * (daily_vol / 100.0) if spot is not None else None
            return {
                "method": "historical_vol_3mo",
                "dailyVolPct": round2(daily_vol),
                "move": round2(move),
                "movePct": round2(daily_vol),
                "upper": round2(spot + move) if spot is not None and move is not None else None,
                "lower": round2(spot - move) if spot is not None and move is not None else None,
                "status": "OK"
            }
    except Exception:
        pass

    return {
        "method": "unavailable",
        "dailyVolPct": None,
        "move": None,
        "movePct": None,
        "upper": None,
        "lower": None,
        "status": "No disponible"
    }


def get_macro_block():
    return {
        "status": "OK | reciente + próximo",
        "next": {
            "evento": "Nóminas no agrícolas (NFP)",
            "impacto": "alto",
            "datetimeNY": "2026-07-03 08:30 ET",
            "dateNY": "2026-07-03",
            "timeNY": "08:30",
            "dias": 1,
            "horas": 4,
            "totalHoras": 28.32,
            "momento": "Antes de la apertura",
            "status": "upcoming"
        },
        "items": [
            {
                "evento": "Nóminas no agrícolas (NFP)",
                "impacto": "alto",
                "datetimeNY": "2026-07-03 08:30 ET",
                "dateNY": "2026-07-03",
                "timeNY": "08:30",
                "dias": 1,
                "horas": 4,
                "totalHoras": 28.32,
                "momento": "Antes de la apertura",
                "status": "upcoming"
            }
        ],
        "all": []
    }


def get_earnings_block():
    return {
        "status": "OK",
        "next": {
            "empresa": "Tesla",
            "ticker": "TSLA",
            "fecha": "2026-07-22",
            "dias": 20,
            "momento": "Hora no especificada",
            "status": "OK"
        },
        "items": [
            {
                "empresa": "Tesla",
                "ticker": "TSLA",
                "fecha": "2026-07-22",
                "dias": 20,
                "momento": "Hora no especificada",
                "status": "OK"
            }
        ]
    }


def compute_dynamic_buffer_pct(expected_move, pm_range, session_code, macro):
    base = BASE_BUFFER_PCT
    em_factor = safe_float(expected_move.get("movePct"), 0) * 0.85 if expected_move.get("status") == "OK" else 0
    pm_factor = safe_float(pm_range.get("sizePct"), 0) * 1.10 if pm_range.get("available") else 0

    dyn = max(base, em_factor, pm_factor)
    reason_parts = [f"base {base:.2f}%"]

    if em_factor:
        reason_parts.append(f"expected move factor {em_factor:.2f}%")
    if pm_factor:
        reason_parts.append(f"premarket range factor {pm_factor:.2f}%")

    if session_code == "premarket":
        dyn *= 1.08
        reason_parts.append("premarket multiplier x1.08")

    nxt = macro.get("next")
    if nxt and safe_float(nxt.get("totalHoras")) is not None and nxt["totalHoras"] <= 36 and nxt.get("impacto") == "alto":
        dyn *= 1.10
        reason_parts.append("high impact macro <=36h x1.10")

    return round2(dyn), " | ".join(reason_parts)


def get_options_trade(tk, price, buffer_pct):
    short_strike = long_strike = breakeven = net_credit = dist_to_short = None
    expiration = None
    short_bid = short_ask = long_bid = long_ask = None
    short_delta = long_delta = None
    short_oi = long_oi = short_vol = long_vol = None
    quotes_usable = False
    notes = "Cadena de opciones no disponible"

    try:
        exps = tk.options
        if exps:
            expiration = exps[0]
            chain = tk.option_chain(expiration)
            calls = chain.calls.copy()

            if calls is not None and not calls.empty and price is not None:
                short_target = ceil_strike(price * (1 + buffer_pct / 100.0), 1.0)
                short_strike = short_target
                long_strike = short_target + SPREAD_WIDTH
                dist_to_short = short_strike - price

                sc = calls[calls["strike"] == short_strike]
                lc = calls[calls["strike"] == long_strike]

                if not sc.empty:
                    sc = sc.iloc[0]
                    short_bid = safe_float(sc.get("bid"), 0)
                    short_ask = safe_float(sc.get("ask"), 0)
                    short_oi = safe_float(sc.get("openInterest"), 0)
                    short_vol = safe_float(sc.get("volume"), 0)
                    short_delta = safe_float(sc.get("delta"))

                if not lc.empty:
                    lc = lc.iloc[0]
                    long_bid = safe_float(lc.get("bid"), 0)
                    long_ask = safe_float(lc.get("ask"), 0)
                    long_oi = safe_float(lc.get("openInterest"), 0)
                    long_vol = safe_float(lc.get("volume"), 0)
                    long_delta = safe_float(lc.get("delta"))

                short_mid = ((short_bid or 0) + (short_ask or 0)) / 2
                long_mid = ((long_bid or 0) + (long_ask or 0)) / 2
                net_credit = short_mid - long_mid
                breakeven = short_strike + net_credit if short_strike is not None and net_credit is not None else None

                quotes_usable = any([
                    (short_bid or 0) > 0,
                    (short_ask or 0) > 0,
                    (long_bid or 0) > 0,
                    (long_ask or 0) > 0
                ])

                notes = "Premarket: quotes de opciones aún no fiables" if not quotes_usable else "Quotes de opciones cargadas"
    except Exception as e:
        notes = f"Error options: {str(e)}"

    payload = {
        "trade": {
            "bufferBasePct": round2(BASE_BUFFER_PCT),
            "bufferDynamicPct": round2(buffer_pct),
            "bufferReason": None,
            "shortStrike": round2(short_strike),
            "longStrike": round2(long_strike),
            "breakeven": round2(breakeven),
            "spreadWidth": round2(SPREAD_WIDTH),
            "netCredit": round2(net_credit),
            "distToShort": round2(dist_to_short)
        },
        "options": {
            "expiration": expiration,
            "shortCallBid": round2(short_bid),
            "shortCallAsk": round2(short_ask),
            "longCallBid": round2(long_bid),
            "longCallAsk": round2(long_ask),
            "shortCallDelta": round2(short_delta),
            "longCallDelta": round2(long_delta),
            "shortCallOI": round2(short_oi),
            "longCallOI": round2(long_oi),
            "shortCallVolume": round2(short_vol),
            "longCallVolume": round2(long_vol),
            "quotesUsable": quotes_usable,
            "notes": notes
        }
    }

    return json_safe(payload)


def compute_score(state):
    score = 55
    reasons = [
        f"Buffer base: {state['trade']['bufferBasePct']:.2f}%",
        f"Buffer dinámico aplicado: {state['trade']['bufferDynamicPct']:.2f}%"
    ]

    if (state["change"] or 0) < 0:
        reasons.append("Sesgo bajista favorable")

    if safe_float(state["vwap"]["distPct"]) is not None and state["vwap"]["distPct"] < 0:
        reasons.append("Por debajo del VWAP")

    if not state["options"]["quotesUsable"]:
        reasons.append("Premarket: quotes de opciones no fiables")

    nxt = state["macro"].get("next")
    if nxt:
        reasons.append(f"Macro próxima: {nxt['evento']}")

    decision = "esperar confirmación"
    decision_tone = "yellow"
    decision_label = "esperar confirmación"
    risk_label = "Riesgo medio"

    return score, decision, decision_tone, decision_label, risk_label, reasons


def build_error_state(msg):
    ny = now_ny()
    return {
        "ticker": TICKER,
        "price": None,
        "prevClose": None,
        "change": None,
        "changePct": None,
        "tone": "flat",
        "source": "error",
        "updatedAt": now_utc().isoformat(),
        "updatedAtText": now_utc().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "updatedAtNY": ny.strftime("%Y-%m-%d %H:%M:%S ET"),
        "session": {"code": "error", "label": "Error"},
        "summary": f"Error generando estado: {msg}",
        "trade": {
            "bufferBasePct": round2(BASE_BUFFER_PCT),
            "bufferDynamicPct": round2(BASE_BUFFER_PCT),
            "bufferReason": "error",
            "shortStrike": None,
            "longStrike": None,
            "breakeven": None,
            "spreadWidth": round2(SPREAD_WIDTH),
            "netCredit": None,
            "distToShort": None
        },
        "options": {
            "expiration": None,
            "shortCallBid": None,
            "shortCallAsk": None,
            "longCallBid": None,
            "longCallAsk": None,
            "shortCallDelta": None,
            "longCallDelta": None,
            "shortCallOI": None,
            "longCallOI": None,
            "shortCallVolume": None,
            "longCallVolume": None,
            "quotesUsable": False,
            "notes": msg
        },
        "vwap": {"value": None, "distPct": None, "zScore": None, "sigma": None, "bias": "No disponible"},
        "openingRange": {"available": False, "status": "Error", "message": "Opening Range no disponible"},
        "premarketRange": {"available": False, "status": "Error", "message": "Premarket Range no disponible"},
        "expectedMove": {
            "method": "unavailable",
            "dailyVolPct": None,
            "move": None,
            "movePct": None,
            "upper": None,
            "lower": None,
            "status": "No disponible"
        },
        "freshness": {
            "ageMinutes": None,
            "thresholdMinutes": 3,
            "isStale": True,
            "status": "Error"
        },
        "score": 0,
        "decision": "no operar",
        "decisionTone": "red",
        "decisionLabel": "no operar",
        "riskLabel": "Riesgo alto",
        "reasons": [msg],
        "alerts": [],
        "macro": get_macro_block(),
        "earnings": get_earnings_block()
    }


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    try:
        ny = now_ny()
        tk, hist_5m, px = get_price_data(TICKER)
        session = get_session_label(ny)

        vwap = compute_vwap(hist_5m)
        opening_range = compute_opening_range(hist_5m)
        pm_range = compute_premarket_range(hist_5m)
        expected_move = compute_expected_move(tk, px["price"])
        macro = get_macro_block()
        earnings = get_earnings_block()

        dyn_buffer_pct, buffer_reason = compute_dynamic_buffer_pct(
            expected_move,
            pm_range,
            session["code"],
            macro
        )

        opt = get_options_trade(tk, px["price"], dyn_buffer_pct)
        opt["trade"]["bufferReason"] = buffer_reason

        freshness = {
            "ageMinutes": 0,
            "thresholdMinutes": 3,
            "isStale": False,
            "status": "OK"
        }

        state = {
            "ticker": TICKER,
            **px,
            "updatedAt": now_utc().isoformat(),
            "updatedAtText": now_utc().strftime("%Y-%m-%d %H:%M:%S UTC"),
            "updatedAtNY": ny.strftime("%Y-%m-%d %H:%M:%S ET"),
            "session": session,
            "summary": "",
            "trade": opt["trade"],
            "options": opt["options"],
            "vwap": vwap,
            "openingRange": opening_range,
            "premarketRange": pm_range,
            "expectedMove": expected_move,
            "freshness": freshness,
            "score": 0,
            "decision": "",
            "decisionTone": "",
            "decisionLabel": "",
            "riskLabel": "",
            "reasons": [],
            "alerts": [],
            "macro": macro,
            "earnings": earnings
        }

        short_strike_txt = f"{int(state['trade']['shortStrike'])}" if state["trade"]["shortStrike"] is not None else "--"
        earnings_txt = (
            f"{state['earnings']['next']['empresa']} en {state['earnings']['next']['dias']}d"
            if state["earnings"].get("next") else "sin earnings cercanos"
        )

        state["summary"] = (
            f"buffer dinámico {state['trade']['bufferDynamicPct']:.2f}% "
            f"(base {state['trade']['bufferBasePct']:.2f}%) · "
            f"short strike {short_strike_txt} · "
            f"{state['options']['notes'].lower()} · "
            f"próximo earnings relevante: {earnings_txt} · "
            f"tramo actual: {state['session']['code']}"
        )

        score, decision, decision_tone, decision_label, risk_label, reasons = compute_score(state)
        state["score"] = score
        state["decision"] = decision
        state["decisionTone"] = decision_tone
        state["decisionLabel"] = decision_label
        state["riskLabel"] = risk_label
        state["reasons"] = reasons

    except Exception as e:
        state = build_error_state(str(e))

    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(json_safe(state), f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
