import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
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


def clean_cell(v):
    if v is None:
        return None
    if isinstance(v, (list, tuple, dict, set)):
        return None
    s = str(v).strip()
    if s in {"", "-", "--", "N/A", "n/a", "nan", "None", "[]"}:
        return None
    return s


def to_num(v):
    s = clean_cell(v)
    if s is None:
        return None
    s = s.replace(",", "").replace("$", "").replace("%", "")
    x = pd.to_numeric(s, errors="coerce")
    return None if pd.isna(x) else float(x)


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
        "2026-06-19": "Juneteenth",
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
            "source": "Nasdaq holiday calendar",
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


def normalize_columns(df):
    out = df.copy()
    out.columns = [str(c).strip().lower() for c in out.columns]
    return out


def find_col(cols, names):
    for c in cols:
        for name in names:
            if name in c:
                return c
    return None


def choose_best_option_row(df, spot):
    cols = list(df.columns)

    strike_col = find_col(cols, ["strike"])
    bid_col = find_col(cols, ["bid"])
    ask_col = find_col(cols, ["ask"])
    vol_col = find_col(cols, ["volume", "vol"])
    oi_col = find_col(cols, ["open interest", "openinterest", "oi"])
    delta_col = find_col(cols, ["delta"])

    if not strike_col:
        return None

    work = df.copy()
    work["_strike"] = work[strike_col].map(to_num)
    work["_bid"] = work[bid_col].map(to_num) if bid_col else None
    work["_ask"] = work[ask_col].map(to_num) if ask_col else None
    work["_vol"] = work[vol_col].map(to_num) if vol_col else None
    work["_oi"] = work[oi_col].map(to_num) if oi_col else None
    work["_delta"] = work[delta_col].map(to_num) if delta_col else None

    work = work[work["_strike"].notna()].copy()
    if work.empty:
        return None

    otm = work[work["_strike"] >= spot].copy() if spot is not None else work.copy()
    if not otm.empty:
        work = otm

    if "_vol" not in work:
        work["_vol"] = None
    if "_oi" not in work:
        work["_oi"] = None

    work["_activity"] = work[["_vol", "_oi"]].fillna(0).sum(axis=1)
    work["_distance"] = (work["_strike"] - spot).abs() if spot is not None else 999999

    work = work.sort_values(["_activity", "_distance", "_strike"], ascending=[False, True, True])
    if work.empty:
        return None

    return work.iloc[0]


def parse_tables_from_html(html):
    try:
        tables = pd.read_html(html)
    except Exception:
        return []

    candidates = []
    for t in tables:
        if t is None or t.empty:
            continue
        df = normalize_columns(t)
        cols = list(df.columns)
        has_strike = any("strike" in c for c in cols)
        has_bid = any("bid" in c for c in cols)
        has_ask = any("ask" in c for c in cols)
        has_oi = any("open interest" in c or "openinterest" in c or c == "oi" for c in cols)
        has_vol = any("volume" in c or c == "vol" for c in cols)

        if has_strike and (has_bid or has_ask or has_oi or has_vol):
            candidates.append(df)

    return candidates


def build_option_snapshot_from_row(row, source_name, source_url, expiration="0DTE_or_nearest"):
    short_strike = round2(row.get("_strike"))
    long_strike = round2(short_strike + SPREAD_WIDTH) if short_strike is not None else None
    short_bid = round2(row.get("_bid"))
    short_ask = round2(row.get("_ask"))
    short_vol = round2(row.get("_vol"))
    short_oi = round2(row.get("_oi"))
    short_delta = round2(row.get("_delta"))

    quotes_usable = short_bid is not None and short_ask is not None and short_ask > short_bid
    liquidity_ok = (short_oi or 0) >= 50 or (short_vol or 0) >= 10
    delta_ok = short_delta is not None and 0.10 <= abs(short_delta) <= 0.35

    net_credit = None
    breakeven = None
    if quotes_usable:
        mid = (short_bid + short_ask) / 2.0
        net_credit = round2(max(mid, 0.01))
        breakeven = round2(short_strike + net_credit) if short_strike is not None else None

    return {
        "expiration": expiration,
        "shortCallBid": short_bid,
        "shortCallAsk": short_ask,
        "longCallBid": None,
        "longCallAsk": None,
        "shortCallDelta": short_delta,
        "longCallDelta": None,
        "shortCallOI": short_oi,
        "longCallOI": None,
        "shortCallVolume": short_vol,
        "longCallVolume": None,
        "quotesUsable": quotes_usable,
        "liquidityOk": liquidity_ok,
        "deltaOk": delta_ok,
        "notes": f"Snapshot orientativo desde {source_name}",
        "issues": [],
        "meta": {
            "used": True,
            "source": source_name,
            "sourceUrl": source_url,
            "updatedAt": now_utc().isoformat(),
            "shortStrike": short_strike,
            "longStrike": long_strike,
            "netCredit": net_credit,
            "breakeven": breakeven,
        },
    }


def get_nasdaq_options_snapshot(symbol, spot):
    urls = [
        f"https://www.nasdaq.com/market-activity/etf/{symbol.lower()}/option-chain",
        f"https://www.nasdaq.com/market-activity/stocks/{symbol.lower()}/option-chain",
    ]

    last_error = None

    for url in urls:
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            r.raise_for_status()

            candidates = parse_tables_from_html(r.text)
            if not candidates:
                last_error = "No se encontraron tablas de chain en Nasdaq"
                continue

            for df in candidates:
                row = choose_best_option_row(df, spot)
                if row is None:
                    continue

                cols = list(df.columns)
                exp_col = find_col(cols, ["expiry", "expiration", "date"])
                expiration = "0DTE_or_nearest"
                if exp_col and exp_col in df.columns and not df.empty:
                    exp_value = clean_cell(df.iloc[0][exp_col])
                    if exp_value:
                        expiration = exp_value

                return build_option_snapshot_from_row(
                    row=row,
                    source_name="nasdaq_option_chain",
                    source_url=url,
                    expiration=expiration,
                )

            last_error = "Nasdaq devolvió tablas pero no fila útil"

        except Exception as e:
            last_error = str(e)

    return {
        "expiration": "0DTE_or_nearest",
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
        "notes": "Nasdaq no devolvió snapshot usable en esta ejecución",
        "issues": [f"nasdaq_unavailable_or_unparseable: {last_error}"] if last_error else ["nasdaq_unavailable_or_unparseable"],
        "meta": {
            "used": False,
            "source": "nasdaq_option_chain",
            "sourceUrl": None,
            "updatedAt": now_utc().isoformat(),
            "shortStrike": None,
            "longStrike": None,
            "netCredit": None,
            "breakeven": None,
        },
    }


def get_yahoo_options_snapshot(symbol, spot):
    url = f"https://finance.yahoo.com/quote/{symbol}/options/"

    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        r.raise_for_status()

        candidates = parse_tables_from_html(r.text)
        if not candidates:
            return {
                "expiration": "0DTE_or_nearest",
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
                "notes": "Yahoo no devolvió snapshot usable en esta ejecución",
                "issues": ["yahoo_unavailable_or_unparseable"],
                "meta": {
                    "used": False,
                    "source": "yahoo_option_chain",
                    "sourceUrl": url,
                    "updatedAt": now_utc().isoformat(),
                    "shortStrike": None,
                    "longStrike": None,
                    "netCredit": None,
                    "breakeven": None,
                },
            }

        for df in candidates:
            row = choose_best_option_row(df, spot)
            if row is not None:
                return build_option_snapshot_from_row(
                    row=row,
                    source_name="yahoo_option_chain",
                    source_url=url,
                    expiration="0DTE_or_nearest",
                )

        return {
            "expiration": "0DTE_or_nearest",
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
            "notes": "Yahoo devolvió tablas pero no fila útil",
            "issues": ["yahoo_no_usable_row"],
            "meta": {
                "used": False,
                "source": "yahoo_option_chain",
                "sourceUrl": url,
                "updatedAt": now_utc().isoformat(),
                "shortStrike": None,
                "longStrike": None,
                "netCredit": None,
                "breakeven": None,
            },
        }

    except Exception as e:
        return {
            "expiration": "0DTE_or_nearest",
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
            "notes": "Yahoo no devolvió snapshot usable en esta ejecución",
            "issues": [f"yahoo_unavailable_or_unparseable: {e}"],
            "meta": {
                "used": False,
                "source": "yahoo_option_chain",
                "sourceUrl": url,
                "updatedAt": now_utc().isoformat(),
                "shortStrike": None,
                "longStrike": None,
                "netCredit": None,
                "breakeven": None,
            },
        }


def get_options_snapshot(symbol, spot, market_holiday):
    if market_holiday["isHoliday"]:
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

    nasdaq = get_nasdaq_options_snapshot(symbol, spot)
    if nasdaq["meta"]["used"]:
        return nasdaq

    yahoo = get_yahoo_options_snapshot(symbol, spot)
    if yahoo["meta"]["used"]:
        yahoo["issues"] = nasdaq.get("issues", []) + yahoo.get("issues", [])
        yahoo["notes"] = "Snapshot orientativo desde yahoo_option_chain (fallback tras Nasdaq)"
        return yahoo

    return {
        "expiration": "0DTE_or_nearest",
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
        "notes": "Ni Nasdaq ni Yahoo devolvieron snapshot usable en esta ejecución",
        "issues": nasdaq.get("issues", []) + yahoo.get("issues", []),
        "meta": {
            "used": False,
            "source": "no_options_source_available",
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
        reasons.append("No hay sesión operable de opciones")
        return 0, "no entrar", "red", "no entrar", "Riesgo alto", reasons

    if price_data["changePct"] is not None and price_data["changePct"] < 0:
        reasons.append("Sesgo bajista favorable")

    if session["code"] != "regular":
        reasons.append("Sesión extendida")
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

    score = max(0, min(100, int(round(score))))

    if macro["next"]["isVeto"]:
        return score, "no entrar", "red", "no entrar", "Riesgo alto", reasons
    if score >= 70:
        return score, "entraría", "green", "entraría", "Riesgo controlado", reasons
    return score, "esperar confirmación", "yellow", "esperar confirmación", "Riesgo medio", reasons


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
        "optionsSource": state["optionsMeta"]["source"],
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

    options = get_options_snapshot(TICKER, price_data["price"], market_holiday)
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
