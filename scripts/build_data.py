import json
import os
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import requests

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

DATA_DIR = "data"
STATE_PATH = os.path.join(DATA_DIR, "state.json")


# ------------------------
# Utilidades generales
# ------------------------

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


# ------------------------
# Macro
# ------------------------

def parse_event(dt_str, label, impact="alto", kind="macro", veto=False):
    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M").replace(tzinfo=NY)
    return {
        "label": label,
        "impact": impact,
        "kind": kind,
        "veto": veto,
        "dt": dt,
    }


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

    macro_today_high = len(today_high) > 0
    macro_window_critical = len(window_critical) > 0

    if macro_window_critical:
        macro_summary = f"Ventana crítica · {window_critical[0]['label']}"
        macro_score = -40
    elif macro_today_high:
        macro_summary = f"Macro hoy · {', '.join(e['label'] for e in today_high[:2])}"
        macro_score = -15
    elif next_big:
        macro_summary = f"Próximo gran evento · {next_big['label']}"
        macro_score = -5
    else:
        macro_summary = "Sin macro alta hoy"
        macro_score = 0

    return {
        "todayHighImpact": macro_today_high,
        "windowCritical": macro_window_critical,
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
        "nextBig": None if not next_big else {
            "label": next_big["label"],
            "impact": next_big["impact"],
            "datetimeNY": fmt_dt(next_big["dt"]),
            "dateNY": fmt_date(next_big["dt"]),
            "countdown": fmt_countdown(next_big["dt"], current_dt),
            "veto": next_big["veto"],
        },
        "summary": macro_summary,
    }


# ------------------------
# OPEX flags
# ------------------------

def get_opex_flags(current_dt):
    monthly_opex = {
        "2026-07-17",
        "2026-08-21",
        "2026-09-18",
        "2026-10-16",
        "2026-11-20",
        "2026-12-18",
    }
    quarterly_opex = {
        "2026-09-18",
        "2026-12-18",
    }

    today = fmt_date(current_dt)
    return {
        "opexDay": today in monthly_opex,
        "opexQuarterly": today in quarterly_opex,
    }


# ------------------------
# Finnhub – precio y sesión
# ------------------------

def get_finnhub_api_key():
    api_key = os.getenv("FINNHUB_API_KEY")
    if not api_key:
        raise RuntimeError("FINNHUB_API_KEY no está configurada en el entorno")
    return api_key


def fetch_quote_finnhub(symbol: str):
    api_key = get_finnhub_api_key()
    url = "https://finnhub.io/api/v1/quote"
    params = {"symbol": symbol, "token": api_key}
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    price = float(data.get("c") or 0.0)   # current price
    change = float(data.get("d") or 0.0)  # change
    change_pct = float(data.get("dp") or 0.0)  # percent change
    prev_close = float(data.get("pc") or 0.0)  # previous close
    ts = data.get("t") or 0

    if ts:
        updated_dt = datetime.fromtimestamp(ts, tz=NY)
    else:
        updated_dt = now_ny()

    return {
        "price": price,
        "change": change,
        "changePct": change_pct,
        "prevClose": prev_close,
        "updatedAt": updated_dt,
        "source": "finnhub_quote",
    }


def infer_session_from_time(current_dt: datetime):
    # Horario regular US equities: 9:30–16:00 ET [web:415][web:45]
    t = current_dt.time()
    pre_start = time(4, 0)
    regular_start = time(9, 30)
    regular_end = time(16, 0)
    after_end = time(20, 0)

    if t < pre_start or t >= after_end:
        return {"code": "closed", "label": "Mercado cerrado"}
    if pre_start <= t < regular_start:
        return {"code": "premarket", "label": "Premarket"}
    if regular_start <= t < regular_end:
        return {"code": "regular", "label": "Sesión regular"}
    # 16:00–20:00
    return {"code": "afterhours", "label": "After hours"}


# ------------------------
# Ventana temporal intradía (tu regla 1h)
# ------------------------

def build_execution_block(current_dt, session_code):
    # 10:30 ET = ~15:30 Canarias ⇒ 1h tras apertura 9:30 ET [web:415]
    entry_start = datetime.combine(current_dt.date(), time(10, 30), tzinfo=NY)
    entry_cutoff = datetime.combine(current_dt.date(), time(13, 30), tzinfo=NY)
    hard_exit = datetime.combine(current_dt.date(), time(15, 15), tzinfo=NY)
    max_hold_minutes = 60

    minutes_to_entry_start = int((entry_start - current_dt).total_seconds() / 60)
    minutes_to_cutoff = int((entry_cutoff - current_dt).total_seconds() / 60)
    minutes_to_hard_exit = int((hard_exit - current_dt).total_seconds() / 60)

    entry_window_open = (
        session_code == "regular"
        and minutes_to_entry_start <= 0
        and minutes_to_cutoff > 0
    )

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


# ------------------------
# Trade quality (de momento mock)
# ------------------------

def mock_trade_quality():
    return {
        "targetDelta": 0.20,
        "creditPerRisk": 0.25,
        "minOpenInterest": 8000,
        "shortStrikeOI": 9200,
        "longStrikeOI": 8700,
        "width": 5,
        "bidAskWidth": 0.07,
        "spreadWidthPct": 0.28,
        "strikeSpacingOk": True,
    }


def score_trade_quality(q):
    score = 0
    reasons = []

    d = q["targetDelta"]
    if 0.15 <= d <= 0.25:
        score += 10
    elif 0.10 <= d < 0.30:
        score += 5
        reasons.append("Delta fuera de rango óptimo")
    else:
        score -= 10
        reasons.append("Delta demasiado agresivo")

    cr = q["creditPerRisk"]
    if 0.20 <= cr <= 0.35:
        score += 10
    elif 0.15 <= cr < 0.20:
        reasons.append("Crédito algo justo")
    else:
        score -= 10
        reasons.append("Crédito/riesgo pobre")

    oi = q["minOpenInterest"]
    if oi >= 5000:
        score += 10
    elif 2000 <= oi < 5000:
        reasons.append("OI moderado")
    else:
        score -= 10
        reasons.append("OI bajo")

    spread_width_pct = q["spreadWidthPct"]
    if spread_width_pct <= 0.10:
        score += 10
    elif spread_width_pct <= 0.20:
        score += 4
        reasons.append("Spread algo ancho")
    else:
        score -= 10
        reasons.append("Spread ancho")

    if q["strikeSpacingOk"]:
        score += 5
    else:
        score -= 5
        reasons.append("Spacing de strikes no ideal")

    return score, reasons


# ------------------------
# Decisión final
# ------------------------

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
        alerts.append({
            "title": "Macro crítica",
            "text": base_state["macro"]["summary"],
        })

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

    tq_score, tq_reasons = score_trade_quality(base_state["tradeQuality"])
    score += tq_score
    reasons.extend(tq_reasons)

    if base_state["macro"]["windowCritical"]:
        decision_label = "no entrar"
        decision_tone = "red"
        risk_label = "Riesgo alto"
    elif execution["timeStopTriggered"]:
        decision_label = "cerrar o no abrir"
        decision_tone = "red"
        risk_label = "Riesgo alto"
    elif session_code != "regular":
        decision_label = "esperar apertura"
        decision_tone = "yellow"
        risk_label = "Riesgo medio"
    elif execution["minutesToEntryStart"] > 0:
        decision_label = "esperar primera hora"
        decision_tone = "yellow"
        risk_label = "Riesgo controlado"
    elif not execution["entryWindowOpen"]:
        decision_label = "fuera de ventana"
        decision_tone = "yellow"
        risk_label = "Riesgo medio"
    else:
        if score <= -40:
            decision_label = "no entrar"
            decision_tone = "red"
            risk_label = "Riesgo alto"
        elif score <= -15:
            decision_label = "esperar confirmación"
            decision_tone = "yellow"
            risk_label = "Riesgo medio"
        else:
            decision_label = "entrar sólo si setup perfecto"
            decision_tone = "green"
            risk_label = "Riesgo controlado"

    return {
        "decisionLabel": decision_label,
        "decisionTone": decision_tone,
        "score": score,
        "riskLabel": risk_label,
        "reasons": reasons,
        "alerts": alerts,
    }


# ------------------------
# Estado principal
# ------------------------

def build_state():
    current_dt = now_ny()

    # 1) Finnhub para QQQ
    quote = fetch_quote_finnhub("QQQ")
    price = quote["price"]
    change = quote["change"]
    change_pct = quote["changePct"]
    prev_close = quote["prevClose"]
    updated_at = quote["updatedAt"]
    source = quote["source"]

    # 2) Sesión real basada en hora NY
    session_info = infer_session_from_time(current_dt)
    session_code = session_info["code"]
    session_label = session_info["label"]

    # 3) Flags, macro, ejecución temporal
    flags = get_opex_flags(current_dt)
    macro = build_macro_block(current_dt)
    execution = build_execution_block(current_dt, session_code)
    trade_quality = mock_trade_quality()

    state = {
        "updatedAtNY": fmt_dt(updated_at),
        "updatedAtText": fmt_dt(updated_at),
        "price": price,
        "change": change,
        "changePct": change_pct,
        "prevClose": prev_close,
        "source": source,
        "session": {
            "code": session_code,
            "label": session_label,
        },
        "vwap": {
            "value": None,
            "distPct": None,
        },
        "expectedMove": {
            "move": None,
            "movePct": None,
        },
        "trade": {
            "bufferDynamicPct": 1.07,
            "shortStrike": None,
            "breakeven": None,
            "distToShort": None,
            "netCredit": None,
        },
        "tradeQuality": trade_quality,
        "execution": execution,
        "options": {
            "expiration": "0dte_or_nearest",
            "quotesUsable": False,
            "liquidityOk": False,
            "shortCallDelta": trade_quality["targetDelta"],
            "notes": "Opciones en modo mock; validación real pendiente de conectar cadena",
        },
        "optionsMeta": {
            "source": "no_options_source_available",
        },
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
            "isHoliday": False,  # en fases posteriores puedes cruzar con calendario Nasdaq [web:43]
            "name": "Sesión normal",
            "date": fmt_date(current_dt),
            "source": "--",
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
