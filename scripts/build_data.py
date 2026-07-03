import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
STATE_FILE = DATA_DIR / "state.json"
HISTORY_FILE = DATA_DIR / "history.json"

TICKER = "QQQ"
HISTORY_LIMIT = 200
SPREAD_WIDTH = 1.0
NY_ZONE = ZoneInfo("America/New_York")
UTC_ZONE = timezone.utc

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")
FINNHUB_BASE = "https://finnhub.io/api/v1"

try:
    import requests
except Exception as e:
    raise RuntimeError("Falta instalar requests en requirements.txt") from e


HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}


def now_utc():
    return datetime.now(UTC_ZONE)


def now_ny():
    return now_utc().astimezone(NY_ZONE)


def round2(v):
    return None if v is None else round(float(v), 2)


def safe_float(v, default=None):
    try:
        if v is None:
            return default
        if isinstance(v, (list, tuple, dict, set)):
            return default
        x = float(v)
        if math.isnan(x):
            return default
        return x
    except Exception:
        return default


def json_safe(obj):
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [json_safe(x) for x in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def fmt_countdown(delta_seconds):
    if delta_seconds <= 0:
        return "ya ocurrido"
    h = int(delta_seconds // 3600)
    m = int((delta_seconds % 3600) // 60)
    return f"{h}h {m}m" if h > 0 else f"{m}m"


def get_session_label(ny_dt, market_closed=False):
    if market_closed:
        return {"code": "closed", "label": "Market closed"}

    mins = ny_dt.hour * 60 + ny_dt.minute
    if mins < 9 * 60 + 30:
        return {"code": "premarket", "label": "Premarket"}
    if mins < 16 * 60:
        return {"code": "regular", "label": "Regular"}
    return {"code": "afterhours", "label": "After hours"}


def is_us_market_holiday(ny_dt):
    date_str = ny_dt.strftime("%Y-%m-%d")

    holidays_2026 = {
        "2026-01-01": "New Year's Day",
        "2026-01-19": "Martin Luther King, Jr. Day",
        "2026-02-16": "Presidents Day",
        "2026-04-03": "Good Friday",
        "2026-05-25": "Memorial Day",
        "2026-07-03": "Independence Day (Observed)",
        "2026-09-07": "Labor Day",
        "2026-11-26": "Thanksgiving Day",
        "2026-12-25": "Christmas Day",
    }

    if date_str in holidays_2026:
        return {
            "isHoliday": True,
            "name": holidays_2026[date_str],
            "date": date_str,
            "source": "Nasdaq 2026 holiday schedule",
        }

    if ny_dt.weekday() >= 5:
        return {
            "isHoliday": True,
            "name": "Weekend",
            "date": date_str,
            "source": "Weekend",
        }

    return {
        "isHoliday": False,
        "name": None,
        "date": date_str,
        "source": None,
    }


def finnhub_get(path, params=None, raise_http=True):
    if not FINNHUB_API_KEY:
        raise RuntimeError("FINNHUB_API_KEY no configurada")
    params = params or {}
    params["token"] = FINNHUB_API_KEY
    r = requests.get(f"{FINNHUB_BASE}{path}", params=params, timeout=20)
    if raise_http:
        r.raise_for_status()
    return r


def get_finnhub_quote(symbol):
    r = finnhub_get("/quote", {"symbol": symbol}, raise_http=True)
    data = r.json()
    return {
        "price": safe_float(data.get("c")),
        "prevClose": safe_float(data.get("pc")),
        "change": safe_float(data.get("d")),
        "changePct": safe_float(data.get("dp")),
        "timestamp": data.get("t"),
    }


def get_price_data(symbol):
    quote = get_finnhub_quote(symbol)

    price = round2(quote["price"])
    prev_close = round2(quote["prevClose"])
    change = round2(quote["change"])
    change_pct = round2(quote["changePct"])
    tone = "up" if (change or 0) > 0 else "down" if (change or 0) < 0 else "flat"

    last_bar_at = None
    if quote.get("timestamp"):
        try:
            last_bar_at = datetime.fromtimestamp(int(quote["timestamp"]), tz=UTC_ZONE).isoformat()
        except Exception:
            last_bar_at = None

    return {
        "price": price,
        "prevClose": prev_close,
        "change": change,
        "changePct": change_pct,
        "tone": tone,
        "source": "finnhub_quote",
        "lastBarAt": last_bar_at,
        "vwap": {
            "value": None,
            "distPct": None,
            "zScore": None,
            "sigma": None,
            "bias": "No usado",
        },
    }


def get_options_snapshot_market_closed():
    return {
        "expiration": "market_closed",
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
        "liquidityOk": False,
        "deltaOk": False,
        "notes": "Mercado cerrado en EE. UU.; options chain operativa desactivada",
        "issues": ["market_closed_us_holiday"],
        "meta": {
            "used": False,
            "source": "market_closed_guard",
            "sourceUrl": None,
            "updatedAt": now_utc().isoformat(),
            "shortStrike": None,
            "longStrike": None,
            "netCredit": None,
            "breakeven": None,
        },
    }


def compute_expected_move(price, change_pct):
    if price is None:
        return {
            "method": "unavailable",
            "dailyVolPct": None,
            "move": None,
            "movePct": None,
            "upper": None,
            "lower": None,
            "status": "No disponible",
        }

    move_pct = abs(change_pct) if change_pct is not None else 1.50
    move = price * (move_pct / 100.0)

    return {
        "method": "historical_vol_proxy",
        "dailyVolPct": round2(move_pct),
        "move": round2(move),
        "movePct": round2(move_pct),
        "upper": round2(price + move),
        "lower": round2(price - move),
        "status": "OK",
    }


def compute_buffer(expected_move_pct, pm_range_pct, vwap_dist_pct_abs, session_code, macro_veto, market_closed):
    em_component = min(max((expected_move_pct or 0) * 0.56, 0), 0.85)
    pm_component = min(max((pm_range_pct or 0) * 0.20, 0), 0.40)
    vwap_component = min(max((vwap_dist_pct_abs or 0) * 0.18, 0), 0.25)
    session_boost = 0.22 if session_code not in {"regular", "closed"} else 0.0
    macro_boost = 0.18 if macro_veto else 0.0
    closed_boost = 0.20 if market_closed else 0.0

    raw = em_component + pm_component + vwap_component + session_boost + macro_boost + closed_boost
    final = min(max(raw, 0.75), 2.50)

    return {
        "bufferBasePct": None,
        "bufferDynamicPct": round2(final),
        "bufferReason": f"exp move +{round2(em_component):.2f}% · pm range +{round2(pm_component):.2f}% · vwap dist +{round2(vwap_component):.2f}% · sesión +{round2(session_boost):.2f}% · macro +{round2(macro_boost):.2f}% · market closed +{round2(closed_boost):.2f}%",
        "bufferDebug": {
            "expectedMovePct": round2(expected_move_pct),
            "premarketRangePct": round2(pm_range_pct),
            "vwapDistPctAbs": round2(vwap_dist_pct_abs),
            "emComponent": round2(em_component),
            "pmComponent": round2(pm_component),
            "vwapComponent": round2(vwap_component),
            "sessionBoost": round2(session_boost),
            "macroBoost": round2(macro_boost),
            "marketClosedBoost": round2(closed_boost),
            "rawBeforeClamp": round2(raw),
            "final": round2(final),
        },
    }


def get_macro_block():
    event_ny = datetime(2026, 7, 3, 8, 30, tzinfo=NY_ZONE)
    nowu = now_utc()
    event_utc = event_ny.astimezone(UTC_ZONE)
    delta_seconds = (event_utc - nowu).total_seconds()

    item = {
        "evento": "Nóminas no agrícolas (NFP)",
        "impacto": "alto",
        "datetimeNY": event_ny.strftime("%Y-%m-%d %H:%M ET"),
        "datetimeUTC": event_utc.strftime("%Y-%m-%d %H:%M UTC"),
        "dateNY": event_ny.strftime("%Y-%m-%d"),
        "timeNY": event_ny.strftime("%H:%M"),
        "dias": max(0, int(delta_seconds // 86400)) if delta_seconds > 0 else 0,
        "horas": max(0, int(delta_seconds // 3600)) if delta_seconds > 0 else 0,
        "totalHoras": round2(delta_seconds / 3600.0),
        "countdown": fmt_countdown(delta_seconds),
        "momento": "Antes de la apertura",
        "status": "upcoming" if delta_seconds > 0 else "recent",
        "isVeto": delta_seconds > 0 and delta_seconds <= 24 * 3600,
    }

    return {"status": "OK", "next": item, "items": [item], "all": [item]}


def get_earnings_block():
    next_item = {
        "empresa": "Tesla",
        "ticker": "TSLA",
        "fecha": "2026-07-22",
        "dias": 20,
        "momento": "Hora no especificada",
        "status": "OK",
    }
    return {"status": "OK", "next": next_item, "items": [next_item]}


def compute_freshness(last_bar_at):
    if not last_bar_at:
        return {
            "ageMinutes": None,
            "thresholdMinutes": 3,
            "isStale": True,
            "status": "Sin timestamp",
        }

    try:
        last_dt = datetime.fromisoformat(last_bar_at)
        age = (now_utc() - last_dt.astimezone(UTC_ZONE)).total_seconds() / 60.0
        return {
            "ageMinutes": round2(age),
            "thresholdMinutes": 3,
            "isStale": False,
            "status": "Quote timestamp",
        }
    except Exception:
        return {
            "ageMinutes": None,
            "thresholdMinutes": 3,
            "isStale": True,
            "status": "Error",
        }


def compute_score_and_reasons(price_data, options, trade, macro, session, market_holiday):
    reasons = []
    score = 50

    if market_holiday["isHoliday"]:
        reasons.append(f"Mercado cerrado: {market_holiday['name']}")
        score = 0

    if price_data["changePct"] is not None and price_data["changePct"] < 0:
        reasons.append("Sesgo bajista favorable")

    if session["code"] != "regular":
        reasons.append("Sesión no regular")
        score -= 10

    if not options["quotesUsable"]:
        reasons.append("Quotes no operables")
        score -= 15

    if not options["liquidityOk"]:
        reasons.append("Liquidez insuficiente")
        score -= 10

    if not options["deltaOk"]:
        reasons.append("Delta fuera de rango")
        score -= 5

    if not trade["creditOk"]:
        reasons.append("Crédito bajo")
        score -= 5

    if macro["next"]["isVeto"]:
        reasons.append("Macro cercana: Nóminas no agrícolas (NFP)")
        score -= 20

    if market_holiday["isHoliday"]:
        decision = "no entrar"
        tone = "red"
        label = "no entrar"
        risk = "Riesgo alto"
        return 0, decision, tone, label, risk, reasons

    score = max(0, min(100, int(round(score))))

    if macro["next"]["isVeto"]:
        decision = "no entrar"
        tone = "red"
        label = "no entrar"
        risk = "Riesgo alto"
    elif score >= 70:
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


def append_history(state):
    entry = {
        "updatedAt": state["updatedAt"],
        "updatedAtNY": state["updatedAtNY"],
        "price": state["price"],
        "score": state["score"],
        "decision": state["decisionLabel"],
        "session": state["session"]["label"],
        "shortStrike": state["trade"]["shortStrike"],
        "longStrike": state["trade"]["longStrike"],
        "notes": state["options"]["notes"],
        "source": state["source"],
        "marketClosed": state["market"]["isHoliday"],
        "marketHolidayName": state["market"]["name"],
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

    HISTORY_FILE.write_text(
        json.dumps(json_safe(history), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    market_holiday = is_us_market_holiday(now_ny())
    price_data = get_price_data(TICKER)
    macro = get_macro_block()
    earnings = get_earnings_block()
    session = get_session_label(now_ny(), market_closed=market_holiday["isHoliday"])
    expected_move = compute_expected_move(price_data["price"], price_data["changePct"])

    pm_range_pct = 0.0
    vwap_dist_pct_abs = abs(price_data["vwap"]["distPct"]) if price_data["vwap"]["distPct"] is not None else 0.0

    buffer_part = compute_buffer(
        expected_move_pct=expected_move["movePct"],
        pm_range_pct=pm_range_pct,
        vwap_dist_pct_abs=vwap_dist_pct_abs,
        session_code=session["code"],
        macro_veto=macro["next"]["isVeto"],
        market_closed=market_holiday["isHoliday"],
    )

    if market_holiday["isHoliday"]:
        options = get_options_snapshot_market_closed()
    else:
        options = get_options_snapshot_market_closed()

    meta = options.pop("meta")

    trade = {
        "bufferBasePct": buffer_part["bufferBasePct"],
        "bufferDynamicPct": buffer_part["bufferDynamicPct"],
        "bufferReason": buffer_part["bufferReason"],
        "shortStrike": meta["shortStrike"],
        "longStrike": meta["longStrike"],
        "breakeven": meta["breakeven"],
        "spreadWidth": round2(SPREAD_WIDTH),
        "netCredit": meta["netCredit"],
        "creditOk": meta["netCredit"] is not None and meta["netCredit"] >= 0.03,
        "minCreditRequired": 0.03,
        "distToShort": round2(meta["shortStrike"] - price_data["price"]) if meta["shortStrike"] is not None and price_data["price"] is not None else None,
    }

    freshness = compute_freshness(price_data["lastBarAt"])
    score, decision, tone, label, risk, reasons = compute_score_and_reasons(
        price_data, options, trade, macro, session, market_holiday
    )

    state = {
        "ticker": TICKER,
        "price": price_data["price"],
        "prevClose": price_data["prevClose"],
        "change": price_data["change"],
        "changePct": price_data["changePct"],
        "tone": price_data["tone"],
        "source": price_data["source"],
        "lastBarAt": price_data["lastBarAt"],
        "updatedAt": now_utc().isoformat(),
        "updatedAtText": now_utc().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "updatedAtNY": now_ny().strftime("%Y-%m-%d %H:%M:%S ET"),
        "session": session,
        "market": market_holiday,
        "summary": f"buffer {buffer_part['bufferDynamicPct']:.2f}% · short {('--' if trade['shortStrike'] is None else trade['shortStrike'])} · crédito {('--' if trade['netCredit'] is None else trade['netCredit'])} · macro {macro['next']['countdown']} · tramo {session['code']}",
        "trade": trade,
        "bufferDebug": buffer_part["bufferDebug"],
        "options": options,
        "vwap": price_data["vwap"],
        "openingRange": {
            "available": False,
            "status": "No aplica" if market_holiday["isHoliday"] else "Pendiente",
            "message": "Mercado cerrado" if market_holiday["isHoliday"] else "Opening Range disponible a partir de 09:35 ET",
        },
        "premarketRange": {
            "available": False,
            "status": "No aplica" if market_holiday["isHoliday"] else "No disponible",
            "message": "Mercado cerrado" if market_holiday["isHoliday"] else "Premarket Range no disponible",
        },
        "expectedMove": expected_move,
        "freshness": freshness,
        "score": score,
        "decision": decision,
        "decisionTone": tone,
        "decisionLabel": label,
        "riskLabel": risk,
        "reasons": reasons,
        "alerts": [
            {
                "level": "warning",
                "title": "Mercado cerrado",
                "text": f"EE. UU. cerrado por {market_holiday['name']}"
            }
        ] if market_holiday["isHoliday"] else [],
        "macro": macro,
        "earnings": earnings,
        "optionsMeta": meta,
    }

    STATE_FILE.write_text(
        json.dumps(json_safe(state), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    append_history(state)


if __name__ == "__main__":
    main()
