import json
import math
from datetime import datetime, timezone, date
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
STATE_FILE = DATA_DIR / "state.json"

TICKER = "QQQ"
BASE_BUFFER_PCT = 1.85
SPREAD_WIDTH = 1.0
MIN_NET_CREDIT = 0.05
MIN_CREDIT_PCT_WIDTH = 0.08
QUOTE_MAX_SPREAD_PCT_REGULAR = 0.35
QUOTE_MAX_SPREAD_PCT_EXTENDED = 0.25
STALE_THRESHOLD_MINUTES = 3

NY_ZONE = ZoneInfo("America/New_York")
UTC_ZONE = timezone.utc


def now_utc():
    return datetime.now(UTC_ZONE)


def now_ny():
    return now_utc().astimezone(NY_ZONE)


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


def fmt_countdown(delta_seconds):
    if delta_seconds is None:
        return "--"
    if delta_seconds <= 0:
        return "ya ocurrido"
    h = int(delta_seconds // 3600)
    m = int((delta_seconds % 3600) // 60)
    return f"{h}h {m}m" if h > 0 else f"{m}m"


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
    last_bar_utc = None

    if hist_5m is not None and not hist_5m.empty:
        price = safe_float(hist_5m.iloc[-1].get("Close"))
        idx = hist_5m.index[-1]
        if isinstance(idx, pd.Timestamp):
            last_bar_utc = idx.tz_convert("UTC") if idx.tzinfo else idx.tz_localize("UTC")

    if hist_1d is not None and len(hist_1d) >= 2:
        prev_close = safe_float(hist_1d.iloc[-2].get("Close"))

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
        "source": source,
        "lastBarAt": last_bar_utc.isoformat() if last_bar_utc is not None else None,
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
        "bias": bias,
    }


def compute_opening_range(hist):
    if hist is None or hist.empty:
        return {"available": False, "status": "Pendiente", "message": "Opening Range no disponible"}

    hist_local = hist.copy()
    if hist_local.index.tz is None:
        hist_local.index = hist_local.index.tz_localize("UTC").tz_convert(NY_ZONE)
    else:
        hist_local.index = hist_local.index.tz_convert(NY_ZONE)

    ny = now_ny()
    day = ny.date()
    day_hist = hist_local[hist_local.index.date == day]
    if ny.hour < 9 or (ny.hour == 9 and ny.minute < 35):
        return {"available": False, "status": "Pendiente", "message": "Opening Range disponible a partir de 09:35 ET"}

    or_bars = day_hist.between_time("09:30", "09:34")
    if or_bars is None or or_bars.empty:
        return {"available": False, "status": "Pendiente", "message": "Opening Range pendiente de datos suficientes"}

    high = safe_float(or_bars["High"].max())
    low = safe_float(or_bars["Low"].min())
    size = None if high is None or low is None else high - low
    return {
        "available": True,
        "status": "OK",
        "message": "Opening Range calculado",
        "high": round2(high),
        "low": round2(low),
        "size": round2(size),
    }


def compute_premarket_range(hist):
    if hist is None or hist.empty:
        return {"available": False, "status": "No disponible", "message": "Premarket Range no disponible"}

    hist_local = hist.copy()
    if hist_local.index.tz is None:
        hist_local.index = hist_local.index.tz_localize("UTC").tz_convert(NY_ZONE)
    else:
        hist_local.index = hist_local.index.tz_convert(NY_ZONE)
    day = now_ny().date()
    day_hist = hist_local[hist_local.index.date == day]
    pm = day_hist.between_time("04:00", "09:29")
    if pm is None or pm.empty:
        return {"available": False, "status": "No disponible", "message": "Premarket Range no disponible"}

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
        "sizePct": round2(size_pct),
    }


def compute_expected_move(tk, spot):
    try:
        hist = tk.history(period="3mo", interval="1d", auto_adjust=False)
        if hist is not None and len(hist) > 10:
            rets = hist["Close"].pct_change().dropna()
            daily_vol_pct = float(rets.std()) * 100
            move = spot * (daily_vol_pct / 100.0) if spot is not None else None
            return {
                "method": "historical_vol_3mo",
                "dailyVolPct": round2(daily_vol_pct),
                "move": round2(move),
                "movePct": round2(daily_vol_pct),
                "upper": round2(spot + move) if spot is not None and move is not None else None,
                "lower": round2(spot - move) if spot is not None and move is not None else None,
                "status": "OK",
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
        "status": "No disponible",
    }


def build_macro_event(name, impact, event_ny, moment):
    nowu = now_utc()
    event_utc = event_ny.astimezone(UTC_ZONE)
    delta_seconds = (event_utc - nowu).total_seconds()
    return {
        "evento": name,
        "impacto": impact,
        "datetimeNY": event_ny.strftime("%Y-%m-%d %H:%M ET"),
        "datetimeUTC": event_utc.strftime("%Y-%m-%d %H:%M UTC"),
        "dateNY": event_ny.strftime("%Y-%m-%d"),
        "timeNY": event_ny.strftime("%H:%M"),
        "dias": max(0, int(delta_seconds // 86400)) if delta_seconds > 0 else 0,
        "horas": max(0, int(delta_seconds // 3600)) if delta_seconds > 0 else 0,
        "totalHoras": round(delta_seconds / 3600, 2),
        "countdown": fmt_countdown(delta_seconds),
        "momento": moment,
        "status": "upcoming" if delta_seconds > 0 else "recent",
        "isVeto": impact == "alto" and 0 <= delta_seconds <= 24 * 3600,
    }


def get_macro_block():
    event_ny = datetime(2026, 7, 3, 8, 30, tzinfo=NY_ZONE)
    next_event = build_macro_event("Nóminas no agrícolas (NFP)", "alto", event_ny, "Antes de la apertura")
    return {
        "status": "OK",
        "next": next_event,
        "items": [next_event],
        "all": [next_event],
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
            "status": "OK",
        },
        "items": [
            {
                "empresa": "Tesla",
                "ticker": "TSLA",
                "fecha": "2026-07-22",
                "dias": 20,
                "momento": "Hora no especificada",
                "status": "OK",
            }
        ],
    }


def compute_dynamic_buffer_pct(expected_move, pm_range, session_code, macro):
    base = BASE_BUFFER_PCT
    move_pct = safe_float(expected_move.get("movePct"), 0) if expected_move.get("status") == "OK" else 0
    pm_pct = safe_float(pm_range.get("sizePct"), 0) if pm_range.get("available") else 0

    em_component = move_pct * 0.18
    pm_component = pm_pct * 0.22
    session_boost = 0.18 if session_code in ("premarket", "overnight", "afterhours") else 0.0

    macro_boost = 0.0
    nxt = macro.get("next")
    total_hours = safe_float(nxt.get("totalHoras")) if nxt else None
    if nxt and total_hours is not None and 0 <= total_hours <= 24 and nxt.get("impacto") == "alto":
        macro_boost = 0.12

    raw = base + em_component + pm_component + session_boost + macro_boost
    final = round2(raw)
    reason_parts = [f"base {base:.2f}%", f"exp move +{em_component:.2f}%", f"pm range +{pm_component:.2f}%"]
    if session_boost:
        reason_parts.append(f"sesión extendida +{session_boost:.2f}%")
    if macro_boost:
        reason_parts.append(f"macro +{macro_boost:.2f}%")

    debug = {
        "base": round2(base),
        "expectedMovePct": round2(move_pct),
        "premarketRangePct": round2(pm_pct),
        "emComponent": round2(em_component),
        "pmComponent": round2(pm_component),
        "sessionBoost": round2(session_boost),
        "macroBoost": round2(macro_boost),
        "rawBeforeRound": round2(raw),
        "final": final,
    }
    return final, " · ".join(reason_parts), debug


def evaluate_quotes(short_bid, short_ask, long_bid, long_ask, session_code):
    notes = []
    valid = True
    for label, bid, ask in [("short", short_bid, short_ask), ("long", long_bid, long_ask)]:
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            valid = False
            notes.append(f"{label}: bid/ask vacío")
            continue
        if ask <= bid:
            valid = False
            notes.append(f"{label}: ask <= bid")
            continue
        mid = (bid + ask) / 2
        spread_pct = ((ask - bid) / mid) if mid > 0 else None
        limit = QUOTE_MAX_SPREAD_PCT_EXTENDED if session_code in ("premarket", "overnight", "afterhours") else QUOTE_MAX_SPREAD_PCT_REGULAR
        if spread_pct is None or spread_pct > limit:
            valid = False
            notes.append(f"{label}: spread amplio")
    return valid, notes


def get_options_trade(tk, price, buffer_pct, session_code):
    short_strike = long_strike = breakeven = net_credit = dist_to_short = None
    expiration = None
    short_bid = short_ask = long_bid = long_ask = None
    short_delta = long_delta = None
    short_oi = long_oi = short_vol = long_vol = None
    quotes_usable = False
    credit_ok = False
    notes = []

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
                    short_bid = safe_float(sc.get("bid"))
                    short_ask = safe_float(sc.get("ask"))
                    short_oi = safe_float(sc.get("openInterest"), 0)
                    short_vol = safe_float(sc.get("volume"), 0)
                    short_delta = safe_float(sc.get("delta"))
                if not lc.empty:
                    lc = lc.iloc[0]
                    long_bid = safe_float(lc.get("bid"))
                    long_ask = safe_float(lc.get("ask"))
                    long_oi = safe_float(lc.get("openInterest"), 0)
                    long_vol = safe_float(lc.get("volume"), 0)
                    long_delta = safe_float(lc.get("delta"))

                quote_valid, quote_notes = evaluate_quotes(short_bid, short_ask, long_bid, long_ask, session_code)
                notes.extend(quote_notes)

                if short_bid is not None and short_ask is not None and long_bid is not None and long_ask is not None:
                    short_mid = (short_bid + short_ask) / 2
                    long_mid = (long_bid + long_ask) / 2
                    net_credit = short_mid - long_mid
                    breakeven = short_strike + net_credit if short_strike is not None and net_credit is not None else None

                quotes_usable = quote_valid
                min_credit_by_width = SPREAD_WIDTH * MIN_CREDIT_PCT_WIDTH
                credit_ok = net_credit is not None and net_credit >= max(MIN_NET_CREDIT, min_credit_by_width)
                if not credit_ok:
                    notes.append("crédito insuficiente")
                if quotes_usable and credit_ok and not notes:
                    notes.append("Quotes operables")
        else:
            notes.append("Sin expiraciones disponibles")
    except Exception as e:
        notes = [f"Error options: {str(e)}"]

    if not notes:
        notes = ["Cadena de opciones no disponible"]

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
            "creditOk": credit_ok,
            "minCreditRequired": round2(max(MIN_NET_CREDIT, SPREAD_WIDTH * MIN_CREDIT_PCT_WIDTH)),
            "distToShort": round2(dist_to_short),
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
            "notes": " · ".join(notes),
            "issues": notes,
        },
    }
    return json_safe(payload)


def compute_freshness(last_bar_at):
    nowu = now_utc()
    if not last_bar_at:
        return {"ageMinutes": None, "thresholdMinutes": STALE_THRESHOLD_MINUTES, "isStale": True, "status": "Sin timestamp"}
    try:
        ts = pd.Timestamp(last_bar_at)
        ts = ts.tz_convert("UTC") if ts.tzinfo else ts.tz_localize("UTC")
        age = (nowu - ts.to_pydatetime()).total_seconds() / 60
        return {
            "ageMinutes": round2(age),
            "thresholdMinutes": STALE_THRESHOLD_MINUTES,
            "isStale": age > STALE_THRESHOLD_MINUTES,
            "status": "Stale" if age > STALE_THRESHOLD_MINUTES else "OK",
        }
    except Exception:
        return {"ageMinutes": None, "thresholdMinutes": STALE_THRESHOLD_MINUTES, "isStale": True, "status": "Error timestamp"}


def compute_score(state):
    score = 55
    reasons = []

    if (state["change"] or 0) < 0:
        score += 8
        reasons.append("Sesgo bajista favorable")
    if safe_float(state["vwap"].get("distPct")) is not None and state["vwap"]["distPct"] < 0:
        score += 7
        reasons.append("Precio por debajo de VWAP")
    if state["session"]["code"] in ("overnight", "premarket", "afterhours"):
        score -= 10
        reasons.append("Sesión extendida")
    if state["freshness"]["isStale"]:
        score -= 18
        reasons.append("Dato no fresco")
    if not state["options"]["quotesUsable"]:
        score -= 15
        reasons.append("Quotes no operables")
    if not state["trade"].get("creditOk"):
        score -= 16
        reasons.append("Crédito insuficiente")
    nxt = state["macro"].get("next")
    if nxt and nxt.get("isVeto"):
        score -= 12
        reasons.append(f"Macro cercana: {nxt['evento']}")

    score = max(0, min(100, int(round(score))))

    if state["freshness"]["isStale"] or (nxt and nxt.get("isVeto")) or (not state["options"]["quotesUsable"] and not state["trade"].get("creditOk")):
        decision = "no entrar"
        tone = "red"
        label = "no entrar"
        risk = "Riesgo alto"
    elif score >= 70 and state["options"]["quotesUsable"] and state["trade"].get("creditOk"):
        decision = "entraría"
        tone = "green"
        label = "entraría"
        risk = "Riesgo controlado"
    else:
        decision = "esperar confirmación"
        tone = "yellow"
        label = "esperar confirmación"
        risk = "Riesgo medio"

    return score, decision, tone, label, risk, reasons


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
        "trade": {"bufferBasePct": round2(BASE_BUFFER_PCT), "bufferDynamicPct": round2(BASE_BUFFER_PCT), "bufferReason": "error", "shortStrike": None, "longStrike": None, "breakeven": None, "spreadWidth": round2(SPREAD_WIDTH), "netCredit": None, "creditOk": False, "minCreditRequired": round2(max(MIN_NET_CREDIT, SPREAD_WIDTH * MIN_CREDIT_PCT_WIDTH)), "distToShort": None},
        "bufferDebug": {},
        "options": {"expiration": None, "shortCallBid": None, "shortCallAsk": None, "longCallBid": None, "longCallAsk": None, "shortCallDelta": None, "longCallDelta": None, "shortCallOI": None, "longCallOI": None, "shortCallVolume": None, "longCallVolume": None, "quotesUsable": False, "notes": msg, "issues": [msg]},
        "vwap": {"value": None, "distPct": None, "zScore": None, "sigma": None, "bias": "No disponible"},
        "openingRange": {"available": False, "status": "Error", "message": "Opening Range no disponible"},
        "premarketRange": {"available": False, "status": "Error", "message": "Premarket Range no disponible"},
        "expectedMove": {"method": "unavailable", "dailyVolPct": None, "move": None, "movePct": None, "upper": None, "lower": None, "status": "No disponible"},
        "freshness": {"ageMinutes": None, "thresholdMinutes": STALE_THRESHOLD_MINUTES, "isStale": True, "status": "Error"},
        "score": 0,
        "decision": "no entrar",
        "decisionTone": "red",
        "decisionLabel": "no entrar",
        "riskLabel": "Riesgo alto",
        "reasons": [msg],
        "alerts": [],
        "macro": get_macro_block(),
        "earnings": get_earnings_block(),
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
        dyn_buffer_pct, buffer_reason, buffer_debug = compute_dynamic_buffer_pct(expected_move, pm_range, session["code"], macro)
        opt = get_options_trade(tk, px["price"], dyn_buffer_pct, session["code"])
        opt["trade"]["bufferReason"] = buffer_reason
        freshness = compute_freshness(px.get("lastBarAt"))

        state = {
            "ticker": TICKER,
            **px,
            "updatedAt": now_utc().isoformat(),
            "updatedAtText": now_utc().strftime("%Y-%m-%d %H:%M:%S UTC"),
            "updatedAtNY": ny.strftime("%Y-%m-%d %H:%M:%S ET"),
            "session": session,
            "summary": "",
            "trade": opt["trade"],
            "bufferDebug": buffer_debug,
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
            "earnings": earnings,
        }

        short_txt = f"${state['trade']['shortStrike']:.2f}" if state['trade']['shortStrike'] is not None else "--"
        macro_txt = state['macro']['next']['countdown'] if state['macro'].get('next') else '--'
        credit_txt = f"${state['trade']['netCredit']:.2f}" if state['trade']['netCredit'] is not None else '--'
        state["summary"] = f"buffer {state['trade']['bufferDynamicPct']:.2f}% · short {short_txt} · crédito {credit_txt} · macro {macro_txt} · tramo {state['session']['code']}"

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
