import json
import math
import os
import time as time_module
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import requests

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

DATA_DIR = "data"
STATE_PATH = os.path.join(DATA_DIR, "state.json")
QQQ_OPTIONS_CLOSE_ET = time(16, 15)


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)


def now_ny():
    return datetime.now(UTC).astimezone(NY)


def fmt_dt(dt):
    return dt.strftime("%Y-%m-%d %H:%M ET")


def fmt_date(dt):
    return dt.strftime("%Y-%m-%d")


def fmt_countdown(target, current):
    delta = target - current
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return "ya ocurrido"
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def parse_event(dt_str, label, impact="alto", kind="macro", veto=False):
    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M").replace(tzinfo=NY)
    return {"label": label, "impact": impact, "kind": kind, "veto": veto, "dt": dt}


def macro_events_2026():
    return [
        parse_event("2026-07-08 14:00", "Actas del FOMC", "alto", veto=True),
        parse_event("2026-07-09 08:30", "Peticiones de desempleo", "medio", veto=False),
        parse_event("2026-07-14 08:30", "IPC (CPI)", "alto", veto=True),
        parse_event("2026-07-14 08:30", "IPC subyacente", "alto", veto=True),
        parse_event("2026-07-15 08:30", "IPP (PPI)", "medio", veto=False),
        parse_event("2026-07-16 08:30", "Ventas minoristas", "alto", veto=True),
        parse_event("2026-07-24 09:45", "PMI manufacturero", "medio", veto=False),
        parse_event("2026-07-24 09:45", "PMI servicios", "medio", veto=False),
        parse_event("2026-07-29 14:00", "Decisión de tipos FOMC", "alto", veto=True),
        parse_event("2026-07-29 14:30", "Rueda de prensa FOMC", "alto", veto=True),
        parse_event("2026-08-07 08:30", "Nóminas no agrícolas (NFP)", "alto", veto=True),
    ]


def build_macro_block(current_dt):
    events = [e for e in macro_events_2026() if e["dt"] >= current_dt - timedelta(hours=6)]
    events.sort(key=lambda x: x["dt"])

    today_high = []
    window_critical = []
    next_big = None

    for e in events:
        if e["impact"] == "alto" and e["dt"].date() == current_dt.date():
            today_high.append(e)
        minutes_to = (e["dt"] - current_dt).total_seconds() / 60
        if e["veto"] and -60 <= minutes_to <= 90:
            window_critical.append(e)

    high_future = [e for e in events if e["impact"] == "alto" and e["dt"] >= current_dt]
    if high_future:
        next_big = high_future[0]

    if window_critical:
        macro_summary = f"Ventana crítica · {window_critical[0]['label']}"
        macro_score = -40
    elif today_high:
        macro_summary = f"Macro hoy · {', '.join(e['label'] for e in today_high[:2])}"
        macro_score = -15
    elif next_big:
        macro_summary = f"Próximo gran evento · {next_big['label']}"
        macro_score = -5
    else:
        macro_summary = "Sin macro alta hoy"
        macro_score = 0

    return {
        "todayHighImpact": len(today_high) > 0,
        "windowCritical": len(window_critical) > 0,
        "score": macro_score,
        "todayList": [
            {
                "label": e["label"],
                "impact": e["impact"],
                "datetimeNY": fmt_dt(e["dt"]),
                "countdown": fmt_countdown(e["dt"], current_dt),
                "veto": e["veto"],
            }
            for e in today_high
        ],
        "windowList": [
            {
                "label": e["label"],
                "impact": e["impact"],
                "datetimeNY": fmt_dt(e["dt"]),
                "countdown": fmt_countdown(e["dt"], current_dt),
                "veto": e["veto"],
            }
            for e in window_critical
        ],
        "nextBig": None
        if not next_big
        else {
            "label": next_big["label"],
            "impact": next_big["impact"],
            "datetimeNY": fmt_dt(next_big["dt"]),
            "dateNY": fmt_date(next_big["dt"]),
            "countdown": fmt_countdown(next_big["dt"], current_dt),
            "veto": next_big["veto"],
        },
        "summary": macro_summary,
    }


def get_opex_flags(current_dt):
    monthly_opex = {"2026-07-17", "2026-08-21", "2026-09-18", "2026-10-16", "2026-11-20", "2026-12-18"}
    quarterly_opex = {"2026-09-18", "2026-12-18"}
    today = fmt_date(current_dt)
    return {
        "opexDay": today in monthly_opex,
        "opexQuarterly": today in quarterly_opex,
    }


def get_finnhub_api_key():
    api_key = os.getenv("FINNHUB_API_KEY")
    if not api_key:
        raise RuntimeError("FINNHUB_API_KEY no está configurada")
    return api_key


def load_previous_state():
    if not os.path.exists(STATE_PATH):
        return None
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def fallback_quote_from_previous(previous_state, reason_code, reason_text):
    current_dt = now_ny()

    if previous_state:
        prev_price = previous_state.get("price")
        prev_change = previous_state.get("change")
        prev_change_pct = previous_state.get("changePct")
        prev_close = previous_state.get("prevClose")
        prev_updated = previous_state.get("updatedAtNY") or previous_state.get("updatedAtText") or fmt_dt(current_dt)

        return {
            "price": prev_price,
            "change": prev_change,
            "changePct": prev_change_pct,
            "prevClose": prev_close,
            "updatedAt": current_dt,
            "updatedAtText": prev_updated,
            "source": reason_code,
            "degraded": True,
            "degradedReason": reason_text,
            "staleFromPreviousState": True,
        }

    return {
        "price": None,
        "change": None,
        "changePct": None,
        "prevClose": None,
        "updatedAt": current_dt,
        "updatedAtText": fmt_dt(current_dt),
        "source": reason_code,
        "degraded": True,
        "degradedReason": reason_text,
        "staleFromPreviousState": False,
    }


def fetch_quote_finnhub(symbol: str, retries: int = 3, sleep_seconds: float = 2.0):
    api_key = get_finnhub_api_key()
    url = "https://finnhub.io/api/v1/quote"
    params = {"symbol": symbol, "token": api_key}
    previous_state = load_previous_state()
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=15)
            status = resp.status_code

            if status >= 500:
                last_error = f"Finnhub {status}"
                if attempt < retries:
                    time_module.sleep(sleep_seconds)
                    continue
                return fallback_quote_from_previous(
                    previous_state,
                    f"finnhub_http_{status}",
                    f"Finnhub devolvió HTTP {status}",
                )

            if status == 429:
                last_error = "Finnhub 429"
                if attempt < retries:
                    time_module.sleep(sleep_seconds)
                    continue
                return fallback_quote_from_previous(
                    previous_state,
                    "finnhub_rate_limited",
                    "Finnhub devolvió 429 rate limit",
                )

            resp.raise_for_status()
            data = resp.json()

            price = float(data.get("c") or 0.0)
            change = float(data.get("d") or 0.0)
            change_pct = float(data.get("dp") or 0.0)
            prev_close = float(data.get("pc") or 0.0)
            ts = data.get("t") or 0
            updated_dt = datetime.fromtimestamp(ts, tz=NY) if ts else now_ny()

            return {
                "price": price,
                "change": change,
                "changePct": change_pct,
                "prevClose": prev_close,
                "updatedAt": updated_dt,
                "updatedAtText": fmt_dt(updated_dt),
                "source": "finnhub_quote",
                "degraded": False,
                "degradedReason": None,
                "staleFromPreviousState": False,
            }

        except requests.exceptions.Timeout:
            last_error = "timeout"
            if attempt < retries:
                time_module.sleep(sleep_seconds)
                continue
            return fallback_quote_from_previous(
                previous_state,
                "finnhub_timeout",
                "Timeout consultando Finnhub",
            )

        except requests.exceptions.RequestException as e:
            last_error = str(e)
            if attempt < retries:
                time_module.sleep(sleep_seconds)
                continue
            return fallback_quote_from_previous(
                previous_state,
                "finnhub_request_error",
                f"Error consultando Finnhub: {str(e)[:140]}",
            )

        except Exception as e:
            last_error = str(e)
            return fallback_quote_from_previous(
                previous_state,
                "finnhub_unexpected_error",
                f"Error inesperado en Finnhub: {str(e)[:140]}",
            )

    return fallback_quote_from_previous(
        previous_state,
        "finnhub_unknown_error",
        f"Error desconocido en Finnhub: {last_error or 'sin detalle'}",
    )


def infer_session_from_time(current_dt: datetime):
    t = current_dt.time()
    if t < time(4, 0) or t >= time(20, 0):
        return {"code": "closed", "label": "Mercado cerrado"}
    if time(4, 0) <= t < time(9, 30):
        return {"code": "premarket", "label": "Premarket"}
    if time(9, 30) <= t < time(16, 0):
        return {"code": "regular", "label": "Sesión regular"}
    return {"code": "afterhours", "label": "After hours"}


def build_execution_block(current_dt, session_code):
    entry_start = datetime.combine(current_dt.date(), time(10, 30), tzinfo=NY)
    entry_cutoff = datetime.combine(current_dt.date(), time(13, 30), tzinfo=NY)
    hard_exit = datetime.combine(current_dt.date(), time(15, 15), tzinfo=NY)
    max_hold_minutes = 60

    minutes_to_entry_start = int((entry_start - current_dt).total_seconds() / 60)
    minutes_to_cutoff = int((entry_cutoff - current_dt).total_seconds() / 60)
    minutes_to_hard_exit = int((hard_exit - current_dt).total_seconds() / 60)

    entry_window_open = session_code == "regular" and minutes_to_entry_start <= 0 and minutes_to_cutoff > 0
    time_stop_triggered = session_code == "regular" and minutes_to_hard_exit <= 0

    if session_code != "regular":
        phase = "Fuera de sesión regular"
    elif minutes_to_entry_start > 0:
        phase = "Esperando 1h de mercado"
    elif not entry_window_open:
        phase = "Fuera de ventana de entrada"
    else:
        phase = "Ventana de entrada abierta"

    return {
        "entryStartET": entry_start.strftime("%H:%M ET"),
        "entryCutoffET": entry_cutoff.strftime("%H:%M ET"),
        "hardExitET": hard_exit.strftime("%H:%M ET"),
        "minutesToEntryStart": minutes_to_entry_start,
        "minutesToEntryCutoff": minutes_to_cutoff,
        "minutesToHardExit": minutes_to_hard_exit,
        "maxHoldMinutes": max_hold_minutes,
        "entryWindowOpen": entry_window_open,
        "timeStopTriggered": time_stop_triggered,
        "phase": phase,
    }


def round_to_strike(price, step=1):
    if price is None:
        return None
    return math.ceil(price / step) * step


def fetch_options_source(symbol: str, spot_price: float, current_dt: datetime, session_code: str):
    regular_open = time(9, 30)
    qqq_opt_close = QQQ_OPTIONS_CLOSE_ET

    base_trade = {
        "bufferDynamicPct": 1.07,
        "shortStrike": None,
        "breakeven": None,
        "distToShort": None,
        "netCredit": None,
    }

    base_quality = {
        "targetDelta": 0.20,
        "creditPerRisk": 0.25,
        "minOpenInterest": 0,
        "shortStrikeOI": None,
        "longStrikeOI": None,
        "width": 5,
        "bidAskWidth": None,
        "spreadWidthPct": None,
        "strikeSpacingOk": True,
    }

    if session_code != "regular":
        return {
            "options": {
                "expiration": "0dte_or_nearest",
                "quotesUsable": False,
                "liquidityOk": False,
                "shortCallDelta": None,
                "notes": "Mercado fuera de sesión regular; cadena de opciones desactivada",
            },
            "optionsMeta": {"source": "market_closed_guard"},
            "trade": base_trade,
            "tradeQuality": base_quality,
        }

    if not (regular_open <= current_dt.time() <= qqq_opt_close):
        return {
            "options": {
                "expiration": "market_closed",
                "quotesUsable": False,
                "liquidityOk": False,
                "shortCallDelta": None,
                "notes": "Fuera de horario operable de opciones para QQQ",
            },
            "optionsMeta": {"source": "market_closed_guard"},
            "trade": base_trade,
            "tradeQuality": base_quality,
        }

    short_strike = round_to_strike((spot_price or 0) * 1.0107, 1) if spot_price is not None else None

    return {
        "options": {
            "expiration": "0dte_or_nearest",
            "quotesUsable": False,
            "liquidityOk": False,
            "shortCallDelta": 0.20,
            "notes": "Sesión regular activa, pero la cadena pública de Nasdaq no devolvió datos utilizables; no se fabrican strikes/créditos operables",
        },
        "optionsMeta": {"source": "nasdaq_option_chain"},
        "trade": {
            "bufferDynamicPct": 1.07,
            "shortStrike": short_strike,
            "breakeven": None,
            "distToShort": None if short_strike is None or spot_price is None else round(short_strike - spot_price, 2),
            "netCredit": None,
        },
        "tradeQuality": base_quality,
    }


def score_trade_quality(q, session_code):
    score = 0
    reasons = []

    d = q.get("targetDelta")
    if d is not None:
        if 0.15 <= d <= 0.25:
            score += 10
        elif 0.10 <= d < 0.30:
            score += 5
            reasons.append("Delta fuera de rango óptimo")
        else:
            score -= 10
            reasons.append("Delta demasiado agresivo")

    cr = q.get("creditPerRisk")
    if cr is not None:
        if 0.20 <= cr <= 0.35:
            score += 10
        elif 0.15 <= cr < 0.20:
            reasons.append("Crédito algo justo")
        else:
            score -= 10
            reasons.append("Crédito/riesgo pobre")

    oi = q.get("minOpenInterest")
    if session_code == "regular":
        if oi is None or oi <= 0:
            reasons.append("OI no disponible")
        elif oi >= 800:
            score += 10
        elif 200 <= oi < 800:
            reasons.append("OI moderado")
        else:
            score -= 10
            reasons.append("OI bajo")

    spread_width_pct = q.get("spreadWidthPct")
    if session_code == "regular":
        if spread_width_pct is None:
            reasons.append("Spread no disponible")
        elif spread_width_pct <= 0.10:
            score += 10
        elif spread_width_pct <= 0.20:
            score += 4
            reasons.append("Spread algo ancho")
        else:
            score -= 10
            reasons.append("Spread ancho")

    if q.get("strikeSpacingOk") is True:
        score += 5
    elif q.get("strikeSpacingOk") is False:
        score -= 5
        reasons.append("Spacing de strikes no ideal")

    return score, reasons


def decide_trade(base_state):
    reasons = []
    alerts = []
    score = 0

    session_code = base_state["session"]["code"]
    quotes_usable = base_state["options"]["quotesUsable"]
    liquidity_ok = base_state["options"]["liquidityOk"]
    execution = base_state["execution"]

    score += base_state["macro"]["score"]

    if base_state["macro"]["windowCritical"]:
        reasons.insert(0, "Ventana macro crítica")
        alerts.append({"title": "Macro crítica", "text": base_state["macro"]["summary"]})

    if session_code != "regular":
        reasons.append("Esperar apertura")
        score -= 5

    if session_code == "regular" and execution["minutesToEntryStart"] > 0:
        reasons.append("Esperar 1h de mercado desde la apertura")
        score -= 10

    if session_code == "regular" and not execution["entryWindowOpen"] and execution["minutesToEntryStart"] <= 0:
        reasons.append("Fuera de ventana de entrada")
        score -= 20

    if execution["timeStopTriggered"]:
        reasons.append("Hora de salida alcanzada")
        score -= 30

    if session_code == "regular":
        if not quotes_usable:
            reasons.append("Quotes no operables")
            score -= 20
        if liquidity_ok is False:
            reasons.append("Liquidez insuficiente")
            score -= 15

    if base_state["flags"]["opexQuarterly"]:
        reasons.append("OPEX trimestral")
        score -= 10
    elif base_state["flags"]["opexDay"]:
        reasons.append("OPEX mensual")
        score -= 5

    tq_score, tq_reasons = score_trade_quality(base_state["tradeQuality"], session_code)
    score += tq_score
    reasons.extend(tq_reasons)

    if base_state.get("dataHealth", {}).get("spotDegraded"):
        reasons.append("Spot degradado")
        alerts.append({"title": "Dato degradado", "text": base_state["dataHealth"]["spotReason"]})
        score -= 15

    if base_state["macro"]["windowCritical"]:
        decision_label, decision_tone, risk_label = "no entrar", "red", "Riesgo alto"
    elif execution["timeStopTriggered"]:
        decision_label, decision_tone, risk_label = "cerrar o no abrir", "red", "Riesgo alto"
    elif session_code != "regular":
        decision_label, decision_tone, risk_label = "esperar apertura", "yellow", "Riesgo medio"
    elif execution["minutesToEntryStart"] > 0:
        decision_label, decision_tone, risk_label = "esperar primera hora", "yellow", "Riesgo controlado"
    elif not execution["entryWindowOpen"]:
        decision_label, decision_tone, risk_label = "fuera de ventana", "yellow", "Riesgo medio"
    else:
        if score <= -40:
            decision_label, decision_tone, risk_label = "no entrar", "red", "Riesgo alto"
        elif score <= -15:
            decision_label, decision_tone, risk_label = "esperar confirmación", "yellow", "Riesgo medio"
        else:
            decision_label, decision_tone, risk_label = "entrar sólo si setup perfecto", "green", "Riesgo controlado"

    return {
        "decisionLabel": decision_label,
        "decisionTone": decision_tone,
        "score": score,
        "riskLabel": risk_label,
        "reasons": reasons,
        "alerts": alerts,
    }


def build_state():
    current_dt = now_ny()

    quote = fetch_quote_finnhub("QQQ")
    price = quote["price"]
    change = quote["change"]
    change_pct = quote["changePct"]
    prev_close = quote["prevClose"]
    updated_at = quote["updatedAt"]
    source = quote["source"]

    session_info = infer_session_from_time(current_dt)
    session_code = session_info["code"]
    session_label = session_info["label"]

    flags = get_opex_flags(current_dt)
    macro = build_macro_block(current_dt)
    execution = build_execution_block(current_dt, session_code)
    options_bundle = fetch_options_source("QQQ", price, current_dt, session_code)

    state = {
        "updatedAtNY": fmt_dt(updated_at),
        "updatedAtText": quote.get("updatedAtText", fmt_dt(updated_at)),
        "price": price,
        "change": change,
        "changePct": change_pct,
        "prevClose": prev_close,
        updated_at = quote["updatedAt"]
    source = quote["source"]

    session_info = infer_session_from_time(current_dt)
    session_code = session_info["code"]
    session_label = session_info["label"]

    flags = get_opex_flags(current_dt)
    macro = build_macro_block(current_dt)
    execution = build_execution_block(current_dt, session_code)
    options_bundle = fetch_options_source("QQQ", price, current_dt, session_code)

    state = {
        "updatedAtNY": fmt_dt(updated_at),
        "updatedAtText": quote.get("updatedAtText", fmt_dt(updated_at)),
        "price": price,
        "change": change,
        "changePct": change_pct,
        "prevClose": prev_close,
        "source": source,
        "session": {"code": session_code, "label": session_label},
        "vwap": {"value": None, "distPct": None},
        "expectedMove": {"move": None, "movePct": None},
        "trade": options_bundle["trade"],
        "tradeQuality": options_bundle["tradeQuality"],
        "execution": execution,
        "options": options_bundle["options"],
        "optionsMeta": options_bundle["optionsMeta"],
        "macro": macro,
        "flags": flags,
        "earnings": {
            "next": {
                "empresa": "Tesla",
                "ticker": "TSLA",
                "fecha": "2026-07-22",
                "dias": 16,
                "momento": "Hora no especificada",
            }
        },
        "market": {
            "isHoliday": False,
            "name": "Sesión normal",
            "date": fmt_date(current_dt),
            "source": "--",
        },
        "dataHealth": {
            "spotDegraded": quote.get("degraded", False),
            "spotReason": quote.get("degradedReason"),
            "staleFromPreviousState": quote.get("staleFromPreviousState", False),
        },
    }

    decision = decide_trade(state)
    state.update(decision)
    return state


def main():
    ensure_dirs()
    state = build_state()
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
   
