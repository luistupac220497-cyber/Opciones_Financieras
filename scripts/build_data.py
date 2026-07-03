import json
import math
import os
from datetime import datetime, timezone, date
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
STATE_FILE = DATA_DIR / "state.json"
HISTORY_FILE = DATA_DIR / "history.json"

TICKER = "QQQ"
HISTORY_LIMIT = 150
SPREAD_WIDTH = 1.0
NY_ZONE = ZoneInfo("America/New_York")
UTC_ZONE = timezone.utc

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")
FINNHUB_BASE = "https://finnhub.io/api/v1"

BASE_BUFFER_PCT = 2.20

# =========================================================
# OPTIONSTRAT MANUAL / SEMI-MANUAL LAYER
# Rellena estos campos mirando OptionStrat delayed (15m)
# =========================================================
OPTIONSTRAT_INPUT = {
    "enabled": True,
    "updatedAt": "2026-07-03 10:40 WEST",
    "shortStrike": 737,
    "longStrike": 738,
    "shortVolume": 420,
    "longVolume": 165,
    "shortOI": 980,
    "longOI": 420,
    "volumeBias": "short_active",
    "liquidityNote": "Volumen aceptable en short strike; long menor pero usable",
    "strikeConfirmed": True,
    "quotesUsable": True
}


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


def json_safe(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
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


def finnhub_get(path, params=None):
    if not FINNHUB_API_KEY:
        raise RuntimeError("FINNHUB_API_KEY no configurada")
    params = params or {}
    params["token"] = FINNHUB_API_KEY
    r = requests.get(f"{FINNHUB_BASE}{path}", params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def get_finnhub_quote(symbol):
    data = finnhub_get("/quote", {"symbol": symbol})
    return {
        "price": safe_float(data.get("c")),
        "prevClose": safe_float(data.get("pc")),
        "change": safe_float(data.get("d")),
        "changePct": safe_float(data.get("dp")),
        "timestamp": data.get("t"),
        "high": safe_float(data.get("h")),
        "low": safe_float(data.get("l")),
        "open": safe_float(data.get("o")),
    }


def get_price_data(ticker):
    quote = get_finnhub_quote(ticker)

    price = safe_float(quote.get("price"))
    prev_close = safe_float(quote.get("prevClose"))
    change = safe_float(quote.get("change"))
    change_pct = safe_float(quote.get("changePct"))
    quote_ts = quote.get("timestamp")

    last_bar_at = None
    if quote_ts:
      try:
          last_bar_at = datetime.fromtimestamp(int(quote_ts), tz=UTC_ZONE).isoformat()
      except Exception:
          last_bar_at = None

    tone = "up" if (change or 0) > 0 else "down" if (change or 0) < 0 else "flat"

    return {
        "price": round2(price),
        "prevClose": round2(prev_close),
        "change": round2(change),
        "changePct": round2(change_pct),
        "tone": tone,
        "source": "finnhub_quote",
        "lastBarAt": last_bar_at,
    }


def compute_expected_move_simple(price):
    if price is None:
        return {
            "method": "unavailable",
            "move": None,
            "movePct": None,
            "upper": None,
            "lower": None,
            "status": "No disponible",
        }
    move_pct = BASE_BUFFER_PCT
    move = price * (move_pct / 100.0)
    return {
        "method": "buffer_proxy",
        "move": round2(move),
        "movePct": round2(move_pct),
        "upper": round2(price + move),
        "lower": round2(price - move),
        "status": "OK",
    }


def get_macro_block():
    event_ny = datetime(2026, 7, 3, 8, 30, tzinfo=NY_ZONE)
    nowu = now_utc()
    event_utc = event_ny.astimezone(UTC_ZONE)
    delta_seconds = (event_utc - nowu).total_seconds()

    next_event = {
        "evento": "Nóminas no agrícolas (NFP)",
        "impacto": "alto",
        "datetimeNY": event_ny.strftime("%Y-%m-%d %H:%M ET"),
        "datetimeUTC": event_utc.strftime("%Y-%m-%d %H:%M UTC"),
        "dateNY": event_ny.strftime("%Y-%m-%d"),
        "timeNY": event_ny.strftime("%H:%M"),
        "dias": max(0, int(delta_seconds // 86400)) if delta_seconds > 0 else 0,
        "horas": max(0, int(delta_seconds // 3600)) if delta_seconds > 0 else 0,
        "totalHoras": round(delta_seconds / 3600, 2),
        "countdown": fmt_countdown(delta_seconds),
        "momento": "Antes de la apertura",
        "status": "upcoming" if delta_seconds > 0 else "recent",
        "isVeto": delta_seconds > 0 and delta_seconds <= 24 * 3600,
    }

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


def build_optionstrat_block(price):
    os_in = OPTIONSTRAT_INPUT if OPTIONSTRAT_INPUT.get("enabled") else {}

    short_strike = safe_float(os_in.get("shortStrike"))
    long_strike = safe_float(os_in.get("longStrike"))
    short_volume = safe_float(os_in.get("shortVolume"), 0)
    long_volume = safe_float(os_in.get("longVolume"), 0)
    short_oi = safe_float(os_in.get("shortOI"), 0)
    long_oi = safe_float(os_in.get("longOI"), 0)
    strike_confirmed = bool(os_in.get("strikeConfirmed"))
    quotes_usable = bool(os_in.get("quotesUsable"))

    net_credit = None
    breakeven = None
    if short_strike is not None and long_strike is not None:
        net_credit = 0.12
        breakeven = short_strike + net_credit

    liquidity_ok = short_volume >= 100 or short_oi >= 300
    volume_bias = os_in.get("volumeBias") or "neutral"

    return {
        "trade": {
            "bufferBasePct": BASE_BUFFER_PCT,
            "bufferDynamicPct": BASE_BUFFER_PCT,
            "bufferReason": "Buffer base fijo mientras la lógica delayed se estabiliza",
            "shortStrike": round2(short_strike),
            "longStrike": round2(long_strike),
            "breakeven": round2(breakeven),
            "spreadWidth": round2(SPREAD_WIDTH),
            "netCredit": round2(net_credit),
            "creditOk": net_credit is not None and net_credit >= 0.03,
            "minCreditRequired": 0.03,
            "distToShort": round2(short_strike - price) if short_strike is not None and price is not None else None,
        },
        "options": {
            "expiration": "0DTE",
            "quotesUsable": quotes_usable,
            "liquidityOk": liquidity_ok,
            "deltaOk": None,
            "notes": os_in.get("liquidityNote") or "Sin nota",
            "issues": [],
        },
        "optionStrat": {
            "used": True,
            "updatedAt": os_in.get("updatedAt"),
            "shortStrike": round2(short_strike),
            "longStrike": round2(long_strike),
            "shortVolume": round2(short_volume),
            "longVolume": round2(long_volume),
            "shortOI": round2(short_oi),
            "longOI": round2(long_oi),
            "volumeBias": volume_bias,
            "strikeConfirmed": strike_confirmed,
            "liquidityNote": os_in.get("liquidityNote"),
        },
    }


def compute_freshness(last_bar_at, updated_at_iso):
    if not last_bar_at:
        return {
            "ageMinutes": None,
            "thresholdMinutes": 20,
            "isStale": True,
            "status": "Sin timestamp",
        }

    try:
        last_dt = datetime.fromisoformat(last_bar_at)
        now_dt = datetime.fromisoformat(updated_at_iso)
        age = (now_dt - last_dt).total_seconds() / 60.0
        return {
            "ageMinutes": round2(age),
            "thresholdMinutes": 20,
            "isStale": age > 20,
            "status": "Stale" if age > 20 else "OK",
        }
    except Exception:
        return {
            "ageMinutes": None,
            "thresholdMinutes": 20,
            "isStale": True,
            "status": "Error",
        }


def compute_score(state):
    score = 55
    reasons = []

    if state["price"] is not None:
        score += 5
        reasons.append("Precio disponible")

    if state["optionStrat"]["strikeConfirmed"]:
        score += 10
        reasons.append("Strike confirmado con OptionStrat")

    if state["options"]["liquidityOk"]:
        score += 8
        reasons.append("Liquidez aceptable en strike objetivo")
    else:
        score -= 12
        reasons.append("Liquidez floja en strike objetivo")

    if state["macro"]["next"]["isVeto"]:
        score -= 15
        reasons.append("Evento macro cercano")

    if state["session"]["code"] != "regular":
        score -= 8
        reasons.append("Sesión fuera de regular hours")

    score = max(0, min(100, int(round(score))))

    if state["macro"]["next"]["isVeto"]:
        decision = "no entrar"
        tone = "red"
        label = "no entrar"
        risk = "Riesgo alto"
    elif score >= 72:
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
        "lastBarAt": None,
        "updatedAt": now_utc().isoformat(),
        "updatedAtText": now_utc().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "updatedAtNY": ny.strftime("%Y-%m-%d %H:%M:%S ET"),
        "session": {"code": "error", "label": "Error"},
        "summary": f"Error generando estado: {msg}",
        "trade": {
            "bufferBasePct": BASE_BUFFER_PCT,
            "bufferDynamicPct": BASE_BUFFER_PCT,
            "bufferReason": "error",
            "shortStrike": None,
            "longStrike": None,
            "breakeven": None,
            "spreadWidth": round2(SPREAD_WIDTH),
            "netCredit": None,
            "creditOk": False,
            "minCreditRequired": 0.03,
            "distToShort": None,
        },
        "options": {
            "expiration": None,
            "quotesUsable": False,
            "liquidityOk": False,
            "deltaOk": None,
            "notes": msg,
            "issues": [msg],
        },
        "optionStrat": {
            "used": False,
            "updatedAt": None,
            "shortStrike": None,
            "longStrike": None,
            "shortVolume": None,
            "longVolume": None,
            "shortOI": None,
            "longOI": None,
            "volumeBias": None,
            "strikeConfirmed": False,
            "liquidityNote": msg,
        },
        "vwap": {"value": None, "distPct": None, "zScore": None, "sigma": None, "bias": "No disponible"},
        "openingRange": {"available": False, "status": "No disponible", "message": "No usado"},
        "premarketRange": {"available": False, "status": "No disponible", "message": "No usado"},
        "expectedMove": compute_expected_move_simple(None),
        "freshness": {"ageMinutes": None, "thresholdMinutes": 20, "isStale": True, "status": "Error"},
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


def append_history(state):
    entry = {
        "updatedAt": state.get("updatedAt"),
        "updatedAtNY": state.get("updatedAtNY"),
        "price": state.get("price"),
        "score": state.get("score"),
        "decision": state.get("decisionLabel"),
        "session": state.get("session", {}).get("label"),
        "shortStrike": state.get("trade", {}).get("shortStrike"),
        "longStrike": state.get("trade", {}).get("longStrike"),
        "optionStratConfirmed": state.get("optionStrat", {}).get("strikeConfirmed"),
    }

    history = []
    if HISTORY_FILE.exists():
        try:
            history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            if not isinstance(history, list):
                history = []
        except Exception:
            history = []

    history.append(entry)
    history = history[-HISTORY_LIMIT:]
    HISTORY_FILE.write_text(json.dumps(json_safe(history), ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    try:
        ny = now_ny()
        nowu = now_utc()

        px = get_price_data(TICKER)
        session = get_session_label(ny)
        macro = get_macro_block()
        earnings = get_earnings_block()
        expected_move = compute_expected_move_simple(px["price"])
        option_block = build_optionstrat_block(px["price"])
        freshness = compute_freshness(px.get("lastBarAt"), nowu.isoformat())

        state = {
            "ticker": TICKER,
            **px,
            "updatedAt": nowu.isoformat(),
            "updatedAtText": nowu.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "updatedAtNY": ny.strftime("%Y-%m-%d %H:%M:%S ET"),
            "session": session,
            "summary": "",
            "trade": option_block["trade"],
            "options": option_block["options"],
            "optionStrat": option_block["optionStrat"],
            "vwap": {"value": None, "distPct": None, "zScore": None, "sigma": None, "bias": "No usado"},
            "openingRange": {"available": False, "status": "No usado", "message": "No usado"},
            "premarketRange": {"available": False, "status": "No usado", "message": "No usado"},
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

        short_txt = f"${state['trade']['shortStrike']:.2f}" if state["trade"]["shortStrike"] is not None else "--"
        long_txt = f"${state['trade']['longStrike']:.2f}" if state["trade"]["longStrike"] is not None else "--"
        macro_txt = state["macro"]["next"]["countdown"] if state["macro"].get("next") else "--"

        state["summary"] = (
            f"precio {('$' + str(state['price'])) if state['price'] is not None else '--'} · "
            f"short {short_txt} · long {long_txt} · "
            f"OptionStrat {'confirmado' if state['optionStrat']['strikeConfirmed'] else 'sin confirmar'} · "
            f"macro {macro_txt}"
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

    STATE_FILE.write_text(json.dumps(json_safe(state), ensure_ascii=False, indent=2), encoding="utf-8")
    append_history(state)


if __name__ == "__main__":
    main()
