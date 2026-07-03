import json
import math
import os
from datetime import datetime, timezone, date
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}


def now_utc():
    return datetime.now(UTC_ZONE)


def now_ny():
    return now_utc().astimezone(NY_ZONE)


def safe_float(v, default=None):
    try:
        if v is None:
            return default
        if isinstance(v, (list, tuple, dict, set)):
            return default
        if v == "":
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


def to_num(v):
    if v is None:
        return None
    if isinstance(v, (list, tuple, dict, set)):
        return None
    s = str(v).strip().replace(",", "").replace("$", "").replace("%", "")
    if s in {"", "-", "--", "N/A", "nan", "None", "[]"}:
        return None
    try:
        x = pd.to_numeric(s, errors="coerce")
        if pd.isna(x):
            return None
        return float(x)
    except Exception:
        return None


def normalize_columns(df):
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df


def find_candidate_table(tables):
    for df in tables:
        temp = normalize_columns(df)
        cols = list(temp.columns)
        has_strike = any("strike" in c for c in cols)
        has_bid = any("bid" in c for c in cols)
        has_ask = any("ask" in c for c in cols)
        if has_strike and (has_bid or has_ask):
            return temp
    return None


def first_col(cols, keywords):
    for c in cols:
        for kw in keywords:
            if kw in c:
                return c
    return None


def get_finviz_options_experimental(symbol):
    urls = [
        f"https://finviz.com/quote.ashx?t={symbol}&p=d&ty=oc",
        f"https://finviz.com/quote.ashx?t={symbol}&p=d",
    ]

    last_error = None

    for url in urls:
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()

            tables = pd.read_html(r.text)
            if not tables:
                continue

            df = find_candidate_table(tables)
            if df is None or df.empty:
                continue

            cols = list(df.columns)
            strike_col = first_col(cols, ["strike"])
            bid_col = first_col(cols, ["bid"])
            ask_col = first_col(cols, ["ask"])
            vol_col = first_col(cols, ["volume", "vol"])
            oi_col = first_col(cols, ["open interest", "oi"])

            if not strike_col:
                continue

            df["_strike"] = df[strike_col].map(to_num)
            df["_bid"] = df[bid_col].map(to_num) if bid_col else None
            df["_ask"] = df[ask_col].map(to_num) if ask_col else None
            df["_vol"] = df[vol_col].map(to_num) if vol_col else None
            df["_oi"] = df[oi_col].map(to_num) if oi_col else None

            df = df[df["_strike"].notna()].copy()
            if df.empty:
                continue

            if "_vol" not in df:
                df["_vol"] = None
            if "_oi" not in df:
                df["_oi"] = None

            df["_activity"] = df[["_vol", "_oi"]].fillna(0).sum(axis=1)
            df = df.sort_values(["_activity", "_strike"], ascending=[False, True])

            row = df.iloc[0]

            short_strike = round2(row["_strike"])
            long_strike = round2(short_strike + SPREAD_WIDTH) if short_strike is not None else None
            short_bid = round2(row["_bid"])
            short_ask = round2(row["_ask"])
            short_vol = round2(row["_vol"])
            short_oi = round2(row["_oi"])

            quotes_usable = short_bid is not None and short_ask is not None and short_ask > short_bid
            liquidity_ok = (short_vol or 0) >= 10 or (short_oi or 0) >= 50

            net_credit = None
            breakeven = None
            if quotes_usable:
                mid = (short_bid + short_ask) / 2
                synthetic_long_mid = max(mid - 0.12, 0.01)
                net_credit = round2(mid - synthetic_long_mid)
                breakeven = round2(short_strike + net_credit) if short_strike is not None else None

            return {
                "used": True,
                "source": "finviz_experimental",
                "sourceUrl": url,
                "updatedAt": now_utc().isoformat(),
                "shortStrike": short_strike,
                "longStrike": long_strike,
                "shortBid": short_bid,
                "shortAsk": short_ask,
                "shortVolume": short_vol,
                "shortOI": short_oi,
                "quotesUsable": quotes_usable,
                "liquidityOk": liquidity_ok,
                "netCredit": net_credit,
                "breakeven": breakeven,
                "notes": "Datos orientativos extraídos experimentalmente de Finviz",
                "issues": [],
            }

        except Exception as e:
            last_error = str(e)

    return {
        "used": False,
        "source": "finviz_experimental",
        "sourceUrl": None,
        "updatedAt": now_utc().isoformat(),
        "shortStrike": None,
        "longStrike": None,
        "shortBid": None,
        "shortAsk": None,
        "shortVolume": None,
        "shortOI": None,
        "quotesUsable": False,
        "liquidityOk": False,
        "netCredit": None,
        "breakeven": None,
        "notes": "Finviz no disponible en esta ejecución",
        "issues": [last_error] if last_error else ["No se pudo extraer tabla de opciones"],
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
    score = 50
    reasons = []

    if state["price"] is not None:
        score += 8
        reasons.append("Precio disponible vía Finnhub")

    if state["finviz"]["used"]:
        score += 8
        reasons.append("Finviz experimental devolvió referencia de opciones")
    else:
        score -= 10
        reasons.append("Finviz no devolvió opciones en esta ejecución")

    if state["options"]["liquidityOk"]:
        score += 8
        reasons.append("Liquidez orientativa aceptable")
    else:
        score -= 10
        reasons.append("Liquidez orientativa débil")

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
        "finviz": {
            "used": False,
            "source": "finviz_experimental",
            "sourceUrl": None,
            "updatedAt": None,
            "shortStrike": None,
            "longStrike": None,
            "shortBid": None,
            "shortAsk": None,
            "shortVolume": None,
            "shortOI": None,
            "quotesUsable": False,
            "liquidityOk": False,
            "netCredit": None,
            "breakeven": None,
            "notes": msg,
            "issues": [msg],
        },
        "vwap": {"value": None, "distPct": None, "zScore": None, "sigma": None, "bias": "No usado"},
        "openingRange": {"available": False, "status": "No usado", "message": "No usado"},
        "premarketRange": {"available": False, "status": "No usado", "message": "No usado"},
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
        "finvizUsed": state.get("finviz", {}).get("used"),
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
        encoding="utf-8"
    )


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
        finviz = get_finviz_options_experimental(TICKER)
        freshness = compute_freshness(px.get("lastBarAt"), nowu.isoformat())

        trade = {
            "bufferBasePct": BASE_BUFFER_PCT,
            "bufferDynamicPct": BASE_BUFFER_PCT,
            "bufferReason": "Buffer base con opciones orientativas desde Finviz experimental",
            "shortStrike": finviz["shortStrike"],
            "longStrike": finviz["longStrike"],
            "breakeven": finviz["breakeven"],
            "spreadWidth": round2(SPREAD_WIDTH),
            "netCredit": finviz["netCredit"],
            "creditOk": finviz["netCredit"] is not None and finviz["netCredit"] >= 0.03,
            "minCreditRequired": 0.03,
            "distToShort": round2(finviz["shortStrike"] - px["price"]) if finviz["shortStrike"] is not None and px["price"] is not None else None,
        }

        options = {
            "expiration": "0DTE_or_nearest",
            "quotesUsable": finviz["quotesUsable"],
            "liquidityOk": finviz["liquidityOk"],
            "deltaOk": None,
            "notes": finviz["notes"],
            "issues": finviz["issues"],
        }

        state = {
            "ticker": TICKER,
            **px,
            "updatedAt": nowu.isoformat(),
            "updatedAtText": nowu.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "updatedAtNY": ny.strftime("%Y-%m-%d %H:%M:%S ET"),
            "session": session,
            "summary": "",
            "trade": trade,
            "options": options,
            "finviz": finviz,
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
            f"Finviz {'OK' if state['finviz']['used'] else 'sin datos'} · "
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

    STATE_FILE.write_text(
        json.dumps(json_safe(state), ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    append_history(state)


if __name__ == "__main__":
    main()
