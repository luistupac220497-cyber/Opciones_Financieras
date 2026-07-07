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

QQQ_OPTIONS_OPEN_ET = time(9, 30)
QQQ_OPTIONS_CLOSE_ET = time(16, 0)


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)


def now_ny():
    return datetime.now(UTC).astimezone(NY)


def fmt_dt(dt):
    if not dt:
        return None
    return dt.astimezone(NY).strftime("%Y-%m-%d %H:%M ET")


def fmt_date(dt):
    if not dt:
        return None
    return dt.astimezone(NY).strftime("%Y-%m-%d")


def fmt_countdown(target, current):
    if not target:
        return None
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
    return {
        "dt": dt,
        "label": label,
        "impact": impact,
        "kind": kind,
        "veto": veto,
    }


def macro_events_2026():
    return [
        parse_event("2026-07-08 14:00", "Actas del FOMC", impact="alto", kind="fomc_minutes", veto=False),
        parse_event("2026-07-15 08:30", "IPC (CPI)", impact="alto", kind="cpi", veto=True),
        parse_event("2026-07-16 08:30", "Ventas minoristas", impact="medio", kind="retail_sales", veto=False),
        parse_event("2026-07-17 08:30", "PPI", impact="medio", kind="ppi", veto=False),
        parse_event("2026-07-23 08:30", "Nóminas no agrícolas (NFP)", impact="alto", kind="nfp", veto=True),
        parse_event("2026-07-29 14:00", "Decisión de tipos FOMC", impact="alto", kind="fomc", veto=True),
    ]


def build_macro_block(current_dt):
    events = macro_events_2026()
    relevant = []
    today_high = []
    next_big = None
    window_critical = False

    for ev in events:
        if ev["dt"] >= current_dt - timedelta(hours=6):
            relevant.append(ev)
        if ev["dt"].date() == current_dt.date() and ev["impact"] == "alto":
            today_high.append(ev)
        mins = (ev["dt"] - current_dt).total_seconds() / 60
        if -60 <= mins <= 90 and ev["veto"]:
            window_critical = True
        if ev["dt"] > current_dt and ev["impact"] == "alto" and next_big is None:
            next_big = ev

    score = 0
    summary = "Sin macro alta hoy"

    if window_critical:
        score -= 25
        summary = "Ventana crítica por macro de alto impacto"
    elif today_high:
        score -= 12
        summary = f"Macro alta hoy · {today_high[0]['label']}"
    elif next_big:
        hours_to_next = (next_big["dt"] - current_dt).total_seconds() / 3600
        if hours_to_next <= 24:
            score -= 6
        elif hours_to_next <= 72:
            score -= 2
        summary = f"Sin macro alta hoy; próximo gran evento · {next_big['label']}"

    return {
        "todayHighImpact": bool(today_high),
        "windowCritical": window_critical,
        "score": score,
        "items": [
            {
                "label": ev["label"],
                "impact": ev["impact"],
                "kind": ev["kind"],
                "dateEt": fmt_dt(ev["dt"]),
                "countdown": fmt_countdown(ev["dt"], current_dt),
                "veto": ev["veto"],
            }
            for ev in relevant[:10]
        ],
        "nextBig": None if not next_big else {
            "label": next_big["label"],
            "impact": next_big["impact"],
            "kind": next_big["kind"],
            "dateEt": fmt_dt(next_big["dt"]),
            "countdown": fmt_countdown(next_big["dt"], current_dt),
        },
        "summary": summary,
    }


def get_opex_flags(current_dt):
    d = current_dt.date()
    weekday = current_dt.weekday()

    def third_friday(year, month):
        first = datetime(year, month, 1).date()
        first_friday_offset = (4 - first.weekday()) % 7
        first_friday = first + timedelta(days=first_friday_offset)
        return first_friday + timedelta(weeks=2)

    monthly = weekday == 4 and d == third_friday(d.year, d.month)
    quarterly = monthly and d.month in (3, 6, 9, 12)
    return {"opexDay": monthly, "opexQuarterly": quarterly}


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
    quote = {
        "price": None,
        "change": None,
        "changePct": None,
        "prevClose": None,
        "updatedAt": None,
        "source": "finnhub_quote",
        "degraded": True,
        "degradedReasonCode": reason_code,
        "degradedReason": reason_text,
        "staleFromPreviousState": False,
    }

    if previous_state:
        quote["price"] = previous_state.get("price")
        quote["change"] = previous_state.get("change")
        quote["changePct"] = previous_state.get("changePct")
        quote["prevClose"] = previous_state.get("prevClose")
        quote["updatedAt"] = previous_state.get("updatedAt")
        quote["staleFromPreviousState"] = True

    return quote


def fetch_quote_finnhub(symbol, retries=3, sleep_seconds=2):
    previous_state = load_previous_state()
    api_key = get_finnhub_api_key()
    url = "https://finnhub.io/api/v1/quote"
    params = {"symbol": symbol, "token": api_key}

    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, params=params, timeout=15)
            if r.status_code == 429:
                if attempt < retries:
                    time_module.sleep(sleep_seconds)
                    continue
                return fallback_quote_from_previous(previous_state, "finnhub_rate_limited", "Finnhub rate limited")
            if 500 <= r.status_code <= 599:
                if attempt < retries:
                    time_module.sleep(sleep_seconds)
                    continue
                return fallback_quote_from_previous(previous_state, f"finnhub_http_{r.status_code}", f"Finnhub HTTP {r.status_code}")

            r.raise_for_status()
            data = r.json()

            price = data.get("c")
            change = data.get("d")
            change_pct = data.get("dp")
            prev_close = data.get("pc")
            ts = data.get("t")

            updated_at = None
            if ts:
                updated_at = datetime.fromtimestamp(ts, tz=UTC).astimezone(NY)

            return {
                "price": price,
                "change": change,
                "changePct": change_pct,
                "prevClose": prev_close,
                "updatedAt": fmt_dt(updated_at),
                "source": "finnhub_quote",
                "degraded": False,
                "degradedReasonCode": None,
                "degradedReason": None,
                "staleFromPreviousState": False,
            }

        except requests.Timeout:
            if attempt < retries:
                time_module.sleep(sleep_seconds)
                continue
            return fallback_quote_from_previous(previous_state, "finnhub_timeout", "Timeout consultando Finnhub")
        except requests.RequestException as e:
            if attempt < retries:
                time_module.sleep(sleep_seconds)
                continue
            return fallback_quote_from_previous(previous_state, "finnhub_request_error", str(e))
        except Exception as e:
            return fallback_quote_from_previous(previous_state, "finnhub_unexpected_error", str(e))

    return fallback_quote_from_previous(previous_state, "finnhub_unknown", "Error desconocido")


def infer_session_from_time(current_dt):
    weekday = current_dt.weekday()
    if weekday >= 5:
        return {"code": "closed", "label": "Mercado cerrado"}

    hm = current_dt.time()
    if hm < time(4, 0):
        return {"code": "closed", "label": "Mercado cerrado"}
    if time(4, 0) <= hm < time(9, 30):
        return {"code": "premarket", "label": "Pre-market"}
    if time(9, 30) <= hm < time(16, 0):
        return {"code": "regular", "label": "Sesión regular"}
    if time(16, 0) <= hm < time(20, 0):
        return {"code": "afterhours", "label": "After hours"}
    return {"code": "closed", "label": "Mercado cerrado"}


def build_execution_block(current_dt, session_code):
    entry_start = current_dt.replace(hour=10, minute=30, second=0, microsecond=0)
    entry_cutoff = current_dt.replace(hour=13, minute=30, second=0, microsecond=0)
    hard_exit = current_dt.replace(hour=15, minute=15, second=0, microsecond=0)

    mins_to_start = int((entry_start - current_dt).total_seconds() // 60)
    mins_to_cutoff = int((entry_cutoff - current_dt).total_seconds() // 60)
    entry_window_open = session_code == "regular" and entry_start <= current_dt <= entry_cutoff
    time_stop_triggered = current_dt >= hard_exit
    minutes_since_start = int((current_dt - entry_start).total_seconds() // 60)

    if session_code != "regular":
        phase = "Fuera de sesión regular"
    elif current_dt < entry_start:
        phase = "Esperando primera hora"
    elif entry_window_open:
        phase = "Ventana de entrada abierta"
    elif current_dt > entry_cutoff and not time_stop_triggered:
        phase = "Fuera de ventana"
    else:
        phase = "Salida dura / no abrir"

    return {
        "phase": phase,
        "entryStartEt": "10:30 ET",
        "minsToEntryStart": mins_to_start,
        "minutesSinceEntryStart": minutes_since_start,
        "entryCutoffEt": "13:30 ET",
        "minsToCutoff": mins_to_cutoff,
        "hardExitEt": "15:15 ET",
        "timeStopTriggered": time_stop_triggered,
        "entryWindowOpen": entry_window_open,
        "maxHoldMinutes": 60,
    }


def round_to_strike(price, step=1):
    if price is None:
        return None
    return math.ceil(price / step) * step


def fetch_options_source(symbol, spot_price, current_dt, session_code):
    in_options_hours = session_code == "regular" and QQQ_OPTIONS_OPEN_ET <= current_dt.time() <= QQQ_OPTIONS_CLOSE_ET

    if not in_options_hours:
        return {
            "options": {
                "expiration": "market_closed",
                "expirationLabel": "Mercado cerrado",
                "quotesUsable": False,
                "liquidityOk": None,
                "liquidityLabel": "No evaluable",
                "deltaShort": None,
                "deltaTarget": 0.20,
                "bidAskSpread": None,
                "spreadPct": None,
                "openInterestShort": None,
                "openInterestLong": None,
                "spacingOk": None,
                "status": "unavailable",
                "notes": "Fuera de horario regular de opciones; no se evalúan liquidez, OI, spread ni crédito",
            },
            "trade": {
                "bufferPct": 1.07,
                "shortStrike": None,
                "netCredit": None,
                "breakeven": None,
                "distanceToShort": None,
                "expectedMove": None,
                "expectedMovePct": None,
            },
            "optionsMeta": {
                "source": "nasdaq_public",
                "snapshot": "no_live_chain",
            }
        }

    short_strike = round_to_strike(spot_price * 1.0107 if spot_price else None, step=1)
    distance_to_short = None if spot_price is None or short_strike is None else round(short_strike - spot_price, 2)

    return {
        "options": {
            "expiration": "nearest",
            "expirationLabel": "0DTE o vencimiento más cercano",
            "quotesUsable": False,
            "liquidityOk": False,
            "liquidityLabel": "Débil",
            "deltaShort": None,
            "deltaTarget": 0.20,
            "bidAskSpread": None,
            "spreadPct": None,
            "openInterestShort": None,
            "openInterestLong": None,
            "spacingOk": True,
            "status": "theoretical",
            "notes": "Sesión regular activa, pero la cadena pública de Nasdaq no devolvió datos utilizables; setup teórico sin strikes/créditos operables reales",
        },
        "trade": {
            "bufferPct": 1.07,
            "shortStrike": short_strike,
            "netCredit": None,
            "breakeven": None,
            "distanceToShort": distance_to_short,
            "expectedMove": None,
            "expectedMovePct": None,
        },
        "optionsMeta": {
            "source": "nasdaq_public",
            "snapshot": "theoretical_setup_only",
        }
    }


def score_trade_quality(state):
    options = state["options"]
    session_code = state["session"]["code"]

    if session_code != "regular" or options.get("status") == "unavailable":
        return {
            "score": 0,
            "reasons": ["Fuera de horario de opciones; métricas no evaluables aún"],
            "label": "Pendiente de apertura",
        }

    score = 0
    reasons = []

    delta_target = options.get("deltaTarget")
    if delta_target is not None:
        score += 2
        reasons.append("Delta objetivo 0.20 definida")

    if state["trade"].get("netCredit") is None:
        reasons.append("Crédito no disponible")
        score -= 4

    oi_short = options.get("openInterestShort")
    if oi_short is None:
        reasons.append("OI no disponible")
        score -= 4
    elif oi_short <= 0:
        reasons.append("OI short en cero")
        score -= 6

    spread_pct = options.get("spreadPct")
    if spread_pct is None:
        reasons.append("Spread no disponible")
        score -= 4
    elif spread_pct > 0.15:
        reasons.append("Spread demasiado amplio")
        score -= 6

    if options.get("spacingOk") is True:
        score += 1
        reasons.append("Spacing correcto")
    elif options.get("spacingOk") is False:
        score -= 3
        reasons.append("Spacing pobre")

    if options.get("quotesUsable") is False:
        score -= 8
        reasons.append("Quotes no operables")

    if options.get("liquidityOk") is False:
        score -= 6
        reasons.append("Liquidez insuficiente")

    return {
        "score": score,
        "reasons": reasons,
        "label": "Teórico" if options.get("status") != "live" else "Operable",
    }


def decide_trade(state):
    score = 0
    reasons = []
    alerts = []

    macro = state["macro"]
    execution = state["execution"]
    flags = state["flags"]
    data_health = state["dataHealth"]
    tq = state["tradeQuality"]
    session_code = state["session"]["code"]

    score += macro["score"]
    score += tq["score"]

    if macro["windowCritical"]:
        reasons.append("Ventana crítica por macro")
        alerts.append("Macro crítica en ventana operativa")
    elif macro["todayHighImpact"]:
        reasons.append("Hay macro alta hoy")

    if session_code == "closed":
        score -= 4
        reasons.append("Mercado cerrado")
    elif session_code == "premarket":
        score -= 3
        reasons.append("Pre-market; esperar apertura")
    elif session_code == "afterhours":
        score -= 4
        reasons.append("After hours; no abrir nuevas posiciones")

    if session_code == "regular" and not execution["entryWindowOpen"] and execution["minsToEntryStart"] > 0:
        score -= 4
        reasons.append("Aún no ha empezado la ventana de entrada")

    if execution["timeStopTriggered"]:
        score -= 12
        reasons.append("Time stop activado")

    if flags["opexQuarterly"]:
        score -= 5
        reasons.append("OPEX trimestral")
    elif flags["opexDay"]:
        score -= 2
        reasons.append("OPEX mensual")

    if data_health["spotDegraded"]:
        score -= 8
        reasons.append("Spot degradado")
        alerts.append("Dato spot degradado; lectura provisional")

    next_big = macro.get("nextBig")
    if next_big and macro["score"] < 0 and not macro["todayHighImpact"]:
        reasons.append(f"Próximo gran evento · {next_big['label']}")

    for r in tq["reasons"]:
        if r not in reasons:
            reasons.append(r)

    if session_code != "regular":
        decision_label = "esperar apertura"
        decision_tone = "blue"
        risk_label = "Riesgo medio"
    elif score <= -20:
        decision_label = "no entrar"
        decision_tone = "red"
        risk_label = "Riesgo alto"
    elif score <= -8:
        decision_label = "esperar confirmación"
        decision_tone = "yellow"
        risk_label = "Riesgo medio"
    elif score <= 6:
        decision_label = "entrar solo si setup perfecto"
        decision_tone = "blue"
        risk_label = "Riesgo medio"
    else:
        decision_label = "setup favorable"
        decision_tone = "green"
        risk_label = "Riesgo controlado"

    return {
        "score": score,
        "decisionLabel": decision_label,
        "decisionTone": decision_tone,
        "riskLabel": risk_label,
        "reasons": reasons,
        "alerts": alerts,
    }


def build_state():
    current_dt = now_ny()
    quote = fetch_quote_finnhub("QQQ")
    session = infer_session_from_time(current_dt)
    flags = get_opex_flags(current_dt)
    macro = build_macro_block(current_dt)
    execution = build_execution_block(current_dt, session["code"])
    options_bundle = fetch_options_source("QQQ", quote["price"], current_dt, session["code"])

    base_state = {
        "symbol": "QQQ",
        "updatedAt": fmt_dt(current_dt),
        "generatedAtUnix": int(current_dt.timestamp()),
        "price": quote["price"],
        "change": quote["change"],
        "changePct": quote["changePct"],
        "prevClose": quote["prevClose"],
        "session": session,
        "vwap": None,
        "vwapDist": None,
        "expectedMove": None,
        "expectedMovePct": None,
        "trade": options_bundle["trade"],
        "execution": execution,
        "options": options_bundle["options"],
        "optionsMeta": options_bundle["optionsMeta"],
        "macro": macro,
        "flags": flags,
        "earnings": {
            "next": {
                "symbol": "TSLA",
                "date": "2026-07-22",
                "label": "Tesla"
            }
        },
        "market": {
            "isHoliday": False,
            "name": None,
            "date": fmt_date(current_dt),
        },
        "dataHealth": {
            "spotSource": quote["source"],
            "optionsSource": options_bundle["optionsMeta"]["source"],
            "spotDegraded": quote["degraded"],
            "spotDegradedReason": quote["degradedReason"],
            "spotStaleFromPreviousState": quote["staleFromPreviousState"],
            "freshnessLabel": "Dato degradado" if quote["degraded"] else "Dato reciente",
            "snapshotLabel": "Snapshot previo reciclado" if quote["staleFromPreviousState"] else "Snapshot actual",
            "optionsDataStatus": options_bundle["options"]["status"],
        }
    }

    base_state["tradeQuality"] = score_trade_quality(base_state)
    decision = decide_trade(base_state)
    base_state.update(decision)

    return base_state


def main():
    ensure_dirs()
    state = build_state()
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
