import json
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


def decide_trade(base_state):
    reasons = []
    alerts = []
    score = 0
    decision_label = "vigilar"
    decision_tone = "yellow"

    if base_state["session"]["code"] != "regular":
        reasons.append("Sesión extendida")
        score -= 12

    if not base_state["options"]["quotesUsable"]:
        reasons.append("Quotes no operables")
        score -= 20

    if base_state["options"]["liquidityOk"] is False:
        reasons.append("Liquidez insuficiente")
        score -= 15

    if base_state["macro"]["windowCritical"]:
        reasons.insert(0, "Ventana macro crítica")
        alerts.append({
            "title": "Macro crítica",
            "text": base_state["macro"]["summary"],
        })
        score -= 40

    if base_state["flags"]["opexQuarterly"]:
        reasons.append("OPEX trimestral")
        score -= 10
    elif base_state["flags"]["opexDay"]:
        reasons.append("OPEX mensual")
        score -= 5

    score += base_state["macro"]["score"]

    if score <= -40:
        decision_label = "no entrar"
        decision_tone = "red"
        risk_label = "Riesgo alto"
    elif score <= -15:
        decision_label = "esperar confirmación"
        decision_tone = "yellow"
        risk_label = "Riesgo medio"
    else:
        decision_label = "vigilar"
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


def build_state():
    current_dt = now_ny()
    flags = get_opex_flags(current_dt)
    macro = build_macro_block(current_dt)

    state = {
        "updatedAtNY": fmt_dt(current_dt),
        "updatedAtText": fmt_dt(current_dt),
        "price": 712.60,
        "change": -12.57,
        "changePct": -1.73,
        "prevClose": 725.17,
        "source": "finnhub_quote",
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
