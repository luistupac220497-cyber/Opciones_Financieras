import json
import math
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

DATA_DIR = "data"
STATE_PATH = os.path.join(DATA_DIR, "state.json")

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

def parse_event(dt_str, label, impact="alto", kind="macro"):
    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M").replace(tzinfo=NY)
    return {
        "label": label,
        "impact": impact,
        "kind": kind,
        "dt": dt,
    }

def upcoming_macro_events():
    return [
        parse_event("2026-07-08 14:00", "Actas del FOMC", "alto"),
        parse_event("2026-07-09 08:30", "Peticiones de desempleo", "medio"),
        parse_event("2026-07-14 08:30", "IPC (CPI)", "alto"),
        parse_event("2026-07-14 08:30", "IPC subyacente", "alto"),
        parse_event("2026-07-15 08:30", "IPP (PPI)", "medio"),
        parse_event("2026-07-16 08:30", "Ventas minoristas", "alto"),
        parse_event("2026-07-24 09:45", "PMI manufacturero", "medio"),
        parse_event("2026-07-24 09:45", "PMI servicios", "medio"),
        parse_event("2026-07-29 14:00", "Decisión de tipos FOMC", "alto"),
        parse_event("2026-07-29 14:30", "Rueda de prensa FOMC", "alto"),
        parse_event("2026-08-07 08:30", "Nóminas no agrícolas (NFP)", "alto"),
    ]

def build_macro_block(current_dt):
    events = [e for e in upcoming_macro_events() if e["dt"] >= current_dt - timedelta(hours=4)]
    events.sort(key=lambda x: x["dt"])

    high_impact_today = []
    critical_window = []
    next_big = None

    for e in events:
        if e["impact"] == "alto" and e["dt"].date() == current_dt.date():
            high_impact_today.append(e)

        minutes_to = (e["dt"] - current_dt).total_seconds() / 60
        if e["impact"] == "alto" and -60 <= minutes_to <= 90:
            critical_window.append(e)

    high_impact_future = [e for e in events if e["impact"] == "alto" and e["dt"] >= current_dt]
    if high_impact_future:
        next_big = high_impact_future[0]

    macro_today_high_impact = len(high_impact_today) > 0
    macro_window_critical = len(critical_window) > 0

    if macro_window_critical:
        headline = critical_window[0]["label"]
        summary = f"Ventana crítica · {headline}"
    elif macro_today_high_impact:
        labels = ", ".join(e["label"] for e in high_impact_today[:2])
        summary = f"Macro hoy · {labels}"
    else:
        summary = "Sin macro alta hoy"

    macro = {
        "todayHighImpact": macro_today_high_impact,
        "windowCritical": macro_window_critical,
        "todayList": [
            {
                "label": e["label"],
                "impact": e["impact"],
                "datetimeNY": fmt_dt(e["dt"]),
                "countdown": fmt_countdown(e["dt"], current_dt),
            }
            for e in high_impact_today
        ],
        "windowList": [
            {
                "label": e["label"],
                "impact": e["impact"],
                "datetimeNY": fmt_dt(e["dt"]),
                "countdown": fmt_countdown(e["dt"], current_dt),
            }
            for e in critical_window
        ],
        "nextBig": None if not next_big else {
            "label": next_big["label"],
            "impact": next_big["impact"],
            "datetimeNY": fmt_dt(next_big["dt"]),
            "dateNY": fmt_date(next_big["dt"]),
            "countdown": fmt_countdown(next_big["dt"], current_dt),
        },
        "summary": summary,
    }

    return macro

def build_state():
    current_dt = now_ny()
    macro = build_macro_block(current_dt)

    decision_label = "no entrar"
    decision_tone = "yellow"
    score = 0
    reasons = [
        "Sesión extendida",
        "Quotes no operables",
        "Liquidez insuficiente",
    ]

    if macro["windowCritical"]:
        reasons.insert(0, "Ventana macro crítica")

    state = {
        "updatedAtNY": fmt_dt(current_dt),
        "updatedAtText": fmt_dt(current_dt),
        "decisionLabel": decision_label,
        "decisionTone": decision_tone,
        "score": score,
        "price": 712.60,
        "change": -12.57,
        "changePct": -1.73,
        "prevClose": 725.17,
        "source": "finnhub_quote",
        "riskLabel": "Riesgo alto",
        "session": {
            "code": "premarket",
            "label": "Premarket",
        },
        "vwap": {
            "value": None,
            "distPct": None,
        },
        "expectedMove": {
            "move": 12.33,
            "movePct": 1.73,
        },
        "trade": {
            "bufferDynamicPct": 1.07,
            "shortStrike": None,
            "breakeven": None,
            "distToShort": None,
            "netCredit": None,
        },
        "options": {
            "expiration": "0dte_or_nearest",
            "quotesUsable": False,
            "liquidityOk": False,
            "shortCallDelta": None,
            "notes": "Sin cadena operable en esta ejecución",
        },
        "optionsMeta": {
            "source": "no_options_source_available",
        },
        "macro": macro,
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
        "alerts": [],
        "reasons": reasons,
    }

    if macro["windowCritical"]:
        state["alerts"].append({
            "title": "Macro crítica",
            "text": macro["summary"],
        })

    return state

def main():
    ensure_dirs()
    state = build_state()
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
