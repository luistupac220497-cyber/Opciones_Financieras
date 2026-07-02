import json
import math
from datetime import datetime, timezone, timedelta

STATE_PATH = "data/state.json"
HISTORY_PATH = "data/history.json"

BASE_BUFFER_PCT = 1.85


def safe_num(x, default=None):
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def round2(x):
    if x is None:
        return None
    return round(float(x), 2)


def ceil_strike(x, step=1.0):
    if x is None:
        return None
    return math.ceil(x / step) * step


def now_utc_iso():
    return datetime.now(timezone.utc).isoformat()


def now_utc_text():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def now_ny_text():
    ny = timezone(timedelta(hours=-4))
    return datetime.now(timezone.utc).astimezone(ny).strftime("%Y-%m-%d %H:%M:%S ET")


def compute_dynamic_buffer_pct(price, expected_move, premarket_range, opening_range, session_code, vwap, macro_next):
    """
    Devuelve:
    - buffer_base_pct
    - buffer_dynamic_pct
    - buffer_reason
    """

    base = BASE_BUFFER_PCT
    factors = [base]
    reasons = [f"base {base:.2f}%"]

    em_move_pct = safe_num(expected_move.get("movePct") if expected_move else None)
    if em_move_pct is not None:
        em_factor = em_move_pct * 0.85
        factors.append(em_factor)
        reasons.append(f"expected move factor {em_factor:.2f}%")

    pm_size_pct = safe_num(premarket_range.get("sizePct") if premarket_range else None)
    if pm_size_pct is not None:
        pm_factor = pm_size_pct * 1.10
        factors.append(pm_factor)
        reasons.append(f"premarket range factor {pm_factor:.2f}%")

    if opening_range and opening_range.get("available"):
        or_size_pct = safe_num(opening_range.get("sizePct"))
        if or_size_pct is not None:
            or_factor = or_size_pct * 1.15
            factors.append(or_factor)
            reasons.append(f"opening range factor {or_factor:.2f}%")

    dynamic = max(factors)

    if session_code == "premarket":
        dynamic *= 1.08
        reasons.append("premarket multiplier x1.08")
    elif session_code == "opening":
        dynamic *= 1.12
        reasons.append("opening multiplier x1.12")

    if price is not None and vwap and safe_num(vwap.get("value")) is not None:
        vwap_value = safe_num(vwap["value"])
        if price > vwap_value:
            dynamic *= 1.05
            reasons.append("price above VWAP x1.05")

    if macro_next:
        impacto = (macro_next.get("impacto") or "").lower()
        total_horas = safe_num(macro_next.get("totalHoras"))
        if impacto == "alto" and total_horas is not None and total_horas <= 36:
            dynamic *= 1.10
            reasons.append("high impact macro <=36h x1.10")
        elif impacto == "medio" and total_horas is not None and total_horas <= 24:
            dynamic *= 1.05
            reasons.append("medium impact macro <=24h x1.05")

    dynamic = round2(dynamic)
    return round2(base), dynamic, " | ".join(reasons)


def build_trade_block(price, buffer_dynamic_pct, spread_width=1.0, net_credit=0.10):
    if price is None or buffer_dynamic_pct is None:
        return {
            "bufferBasePct": BASE_BUFFER_PCT,
            "bufferDynamicPct": None,
            "bufferReason": "No disponible",
            "shortStrike": None,
            "longStrike": None,
            "breakeven": None,
            "spreadWidth": spread_width,
            "netCredit": net_credit,
            "distToShort": None
        }

    dist_dollars = price * (buffer_dynamic_pct / 100.0)
    short_strike = ceil_strike(price + dist_dollars, 1.0)
    long_strike = round2(short_strike + spread_width)
    breakeven = round2(short_strike + net_credit)
    dist_to_short = round2(short_strike - price)

    return {
        "bufferBasePct": round2(BASE_BUFFER_PCT),
        "bufferDynamicPct": round2(buffer_dynamic_pct),
        "bufferReason": "",
        "shortStrike": round2(short_strike),
        "longStrike": round2(long_strike),
        "breakeven": round2(breakeven),
        "spreadWidth": round2(spread_width),
        "netCredit": round2(net_credit),
        "distToShort": round2(dist_to_short)
    }


def main():
    # =========================
    # EJEMPLO DE DATOS BASE
    # Sustituye estos por tu lógica real de extracción
    # =========================
    price = 720.50
    prev_close = 736.40
    change = round2(price - prev_close)
    change_pct = round2((change / prev_close) * 100)
    session = {"code": "premarket", "label": "Premarket"}

    options = {
        "expiration": "2026-07-02",
        "shortCallBid": 0.0,
        "shortCallAsk": 0.0,
        "longCallBid": 0.0,
        "longCallAsk": 0.0,
        "shortCallDelta": None,
        "longCallDelta": None,
        "shortCallOI": 0.0,
        "longCallOI": 0.0,
        "shortCallVolume": 14567.0,
        "longCallVolume": 41432.0,
        "quotesUsable": False,
        "notes": "Premarket: quotes de opciones aún no fiables"
    }

    vwap = {
        "value": 728.01,
        "distPct": -1.03,
        "zScore": -3.63,
        "sigma": 2.07,
        "bias": "Muy por debajo"
    }

    opening_range = {
        "available": False,
        "status": "Pendiente",
        "message": "Opening Range disponible a partir de 09:30 ET"
    }

    premarket_range = {
        "available": True,
        "status": "OK",
        "message": "Premarket Range calculado",
        "high": 728.04,
        "low": 719.46,
        "close": 720.50,
        "size": 8.58,
        "sizePct": 1.19
    }

    expected_move = {
        "method": "historical_vol_3mo",
        "dailyVolPct": 2.10,
        "move": 15.15,
        "movePct": 2.10,
        "upper": 735.65,
        "lower": 705.35,
        "status": "OK"
    }

    freshness = {
        "ageMinutes": 0,
        "thresholdMinutes": 3,
        "isStale": False,
        "status": "OK"
    }

    macro = {
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

    earnings = {
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

    # =========================
    # NUEVA LÓGICA DE BUFFER DINÁMICO
    # =========================
    buffer_base_pct, buffer_dynamic_pct, buffer_reason = compute_dynamic_buffer_pct(
        price=price,
        expected_move=expected_move,
        premarket_range=premarket_range,
        opening_range=opening_range,
        session_code=session["code"],
        vwap=vwap,
        macro_next=macro.get("next")
    )

    trade = build_trade_block(
        price=price,
        buffer_dynamic_pct=buffer_dynamic_pct,
        spread_width=1.0,
        net_credit=0.10
    )
    trade["bufferReason"] = buffer_reason

    # Aquí ya no usamos buffer fijo para strike:
    score = 55
    decision = "esperar confirmación"
    decision_tone = "yellow"
    decision_label = "🟡 Esperar confirmación"
    risk_label = "🟡 Riesgo medio"

    reasons = [
        f"Buffer base: {buffer_base_pct:.2f}%",
        f"Buffer dinámico aplicado: {buffer_dynamic_pct:.2f}%",
        "Sesgo bajista favorable",
        "Por debajo del VWAP",
        "Premarket: quotes de opciones no fiables",
        "Macro próxima: Nóminas no agrícolas (NFP)"
    ]

    summary = (
        f"buffer dinámico {buffer_dynamic_pct:.2f}% "
        f"(base {buffer_base_pct:.2f}%) · "
        f"short strike {int(trade['shortStrike']) if trade['shortStrike'] else '--'} · "
        f"quotes de opciones no fiables todavía · "
        f"próximo earnings relevante: Tesla en 20d · "
        f"tramo actual: {session['code']}"
    )

    state = {
        "ticker": "QQQ",
        "price": price,
        "prevClose": prev_close,
        "change": change,
        "changePct": change_pct,
        "tone": "down" if change < 0 else "up",
        "source": "intraday_5m",
        "updatedAt": now_utc_iso(),
        "updatedAtText": now_utc_text(),
        "updatedAtNY": now_ny_text(),
        "session": session,
        "summary": summary,
        "trade": trade,
        "options": options,
        "vwap": vwap,
        "openingRange": opening_range,
        "premarketRange": premarket_range,
        "expectedMove": expected_move,
        "freshness": freshness,
        "score": score,
        "decision": decision,
        "decisionTone": decision_tone,
        "decisionLabel": decision_label,
        "riskLabel": risk_label,
        "reasons": reasons,
        "alerts": [],
        "macro": macro,
        "earnings": earnings
    }

    history = []
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            history = json.load(f)
            if not isinstance(history, list):
                history = []
    except Exception:
        history = []

    history.append({
        "updatedAt": state["updatedAt"],
        "price": state["price"],
        "score": state["score"],
        "decision": state["decision"],
        "bufferDynamicPct": trade["bufferDynamicPct"],
        "shortStrike": trade["shortStrike"]
    })

    history = history[-200:]

    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

    print("state.json y history.json actualizados correctamente.")


if __name__ == "__main__":
    main()
