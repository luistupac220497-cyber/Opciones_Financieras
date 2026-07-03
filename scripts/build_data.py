import json
import math
import os
from datetime import datetime, timezone, timedelta
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


def get_session_label(ny_dt):
    mins = ny_dt.hour * 60 + ny_dt.minute
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


def get_finnhub_candles(symbol):
    now_ts = int(now_utc().timestamp())
    start_ts = int((now_utc() - timedelta(days=5)).timestamp())
    data = finnhub_get(
        "/stock/candle",
        {
            "symbol": symbol,
            "resolution": "5",
            "from": start_ts,
            "to": now_ts,
        },
    )
    if data.get("s") != "ok":
        return None
    return data


def get_price_data(symbol):
    quote = get_finnhub_quote(symbol)
    candles = get_finnhub_candles(symbol)

    price = round2(quote["price"])
    prev_close = round2(quote["prevClose"])
    change = round2(quote["change"])
    change_pct = round2(quote["changePct"])
    source = "finnhub_quote"
    last_bar_at = None
    vwap_value = None
    sigma = None
    z_score = None
    vwap_dist_pct = None
    bias = "No usado"

    if candles and candles.get("c") and candles.get("t"):
        closes = [safe_float(x) for x in candles["c"]]
        highs = [safe_float(x) for x in candles.get("h", [])]
        lows = [safe_float(x) for x in candles.get("l", [])]
        opens = [safe_float(x) for x in candles.get("o", [])]
        vols = [safe_float(x, 0) for x in candles.get("v", [])]
        times = candles["t"]

        rows = []
        for i in range(min(len(closes), len(times), len(highs), len(lows), len(opens), len(vols))):
            if None in (closes[i], highs[i], lows[i], opens[i]):
                continue
            rows.append(
                {
                    "close": closes[i],
                    "high": highs[i],
                    "low": lows[i],
                    "open": opens[i],
                    "volume": vols[i] or 0,
                    "ts": times[i],
                }
            )

        if rows:
            df = pd.DataFrame(rows)
            df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True)
            df["date_ny"] = df["dt"].dt.tz_convert(NY_ZONE).dt.date
            latest_day = df["date_ny"].max()
            day_df = df[df["date_ny"] == latest_day].copy()

            if not day_df.empty:
                source = "intraday_5m"
                last_dt = day_df["dt"].max()
                last_bar_at = last_dt.isoformat()

                typical = (day_df["high"] + day_df["low"] + day_df["close"]) / 3.0
                vol = day_df["volume"].replace(0, 1)
                cum_pv = (typical * vol).cumsum()
                cum_v = vol.cumsum().replace(0, 1)
                day_df["vwap"] = cum_pv / cum_v

                latest = day_df.iloc[-1]
                vwap_value = round2(latest["vwap"])

                if price is not None and vwap_value is not None and vwap_value != 0:
                    vwap_dist_pct = round2(((price - vwap_value) / vwap_value) * 100.0)

                close_std = safe_float(day_df["close"].std(ddof=0))
                sigma = round2(close_std)
                if price is not None and vwap_value is not None and close_std not in (None, 0):
                    z_score = round2((price - vwap_value) / close_std)

                if vwap_dist_pct is not None:
                    if vwap_dist_pct <= -0.8:
                        bias = "Muy por debajo"
                    elif vwap_dist_pct < 0:
                        bias = "Por debajo"
                    elif vwap_dist_pct >= 0.8:
                        bias = "Muy por encima"
                    else:
                        bias = "Cerca"

    tone = "up" if (change or 0) > 0 else "down" if (change or 0) < 0 else "flat"

    return {
        "price": price,
        "prevClose": prev_close,
        "change": change,
        "changePct": change_pct,
        "tone": tone,
        "source": source,
        "lastBarAt": last_bar_at,
        "vwap": {
            "value": vwap_value,
            "distPct": vwap_dist_pct,
            "zScore": z_score,
            "sigma": sigma,
            "bias": bias,
        },
    }


def clean_cell(v):
    if v is None:
        return None
    if isinstance(v, (list, tuple, dict, set)):
        return None
    s = str(v).strip()
    if s in {"", "-", "--", "N/A", "nan", "None", "[]"}:
        return None
    return s


def to_num(v):
    s = clean_cell(v)
    if s is None:
        return None
    s = s.replace(",", "").replace("$", "").replace("%", "")
    x = pd.to_numeric(s, errors="coerce")
    return None if pd.isna(x) else float(x)


def normalize_columns(df):
    out = df.copy()
    out.columns = [str(c).strip().lower() for c in out.columns]
    return out


def first_col(cols, candidates):
    for c in cols:
        for cand in candidates:
            if cand in c:
                return c
    return None


def get_options_snapshot_fallback(symbol):
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
            if not isinstance(tables, list) or not tables:
                continue

            candidate = None
            for t in tables:
                df = normalize_columns(t)
                cols = list(df.columns)
                has_strike = any("strike" in c for c in cols)
                has_bid = any("bid" in c for c in cols)
                has_ask = any("ask" in c for c in cols)
                if has_strike and (has_bid or has_ask):
                    candidate = df
                    break

            if candidate is None or candidate.empty:
                continue

            cols = list(candidate.columns)
            strike_col = first_col(cols, ["strike"])
            bid_col = first_col(cols, ["bid"])
            ask_col = first_col(cols, ["ask"])
            vol_col = first_col(cols, ["volume", "vol"])
            oi_col = first_col(cols, ["open interest", "oi"])
            delta_col = first_col(cols, ["delta"])

            if not strike_col:
                continue

            candidate["_strike"] = candidate[strike_col].map(to_num)
            candidate["_bid"] = candidate[bid_col].map(to_num) if bid_col else None
            candidate["_ask"] = candidate[ask_col].map(to_num) if ask_col else None
            candidate["_vol"] = candidate[vol_col].map(to_num) if vol_col else None
            candidate["_oi"] = candidate[oi_col].map(to_num) if oi_col else None
            candidate["_delta"] = candidate[delta_col].map(to_num) if delta_col else None

            candidate = candidate[candidate["_strike"].notna()].copy()
            if candidate.empty:
                continue

            if "_vol" not in candidate:
                candidate["_vol"] = None
            if "_oi" not in candidate:
                candidate["_oi"] = None

            candidate["_activity"] = candidate[["_vol", "_oi"]].fillna(0).sum(axis=1)
            candidate = candidate.sort_values(["_activity", "_strike"], ascending=[False, True])

            row = candidate.iloc[0]

            short_strike = round2(row["_strike"])
            long_strike = round2(short_strike + SPREAD_WIDTH) if short_strike is not None else None
            short_bid = round2(row["_bid"])
            short_ask = round2(row["_ask"])
            short_oi = round2(row["_oi"])
            short_vol = round2(row["_vol"])
            short_delta = round2(row["_delta"])

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
                "expiration": "0DTE_or_nearest",
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
                "notes": "Snapshot orientativo desde Finviz experimental",
                "issues": [],
                "meta": {
                    "used": True,
                    "source": "finviz_experimental",
                    "sourceUrl": url,
                    "updatedAt": now_utc().isoformat(),
                    "shortStrike": short_strike,
                    "longStrike": long_strike,
                    "netCredit": net_credit,
                    "breakeven": breakeven,
                },
            }

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
        "notes": "Sin snapshot de opciones disponible en esta ejecución",
        "issues": [f"finviz_unavailable_or_unparseable: {last_error}"] if last_error else ["finviz_unavailable_or_unparseable"],
        "meta": {
            "used": False,
            "source": "finviz_experimental",
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


def compute_buffer(expected_move_pct, pm_range_pct, vwap_dist_pct_abs, session_code, macro_veto):
    em_component = min(max((expected_move_pct or 0) * 0.56, 0), 0.85)
    pm_component = min(max((pm_range_pct or 0) * 0.20, 0), 0.40)
    vwap_component = min(max((vwap_dist_pct_abs or 0) * 0.18, 0), 0.25)
    session_boost = 0.22 if session_code != "regular" else 0.0
    macro_boost = 0.18 if macro_veto else 0.0

    raw = em_component + pm_component + vwap_component + session_boost + macro_boost
    final = min(max(raw, 0.75), 2.50)

    return {
        "bufferBasePct": None,
        "bufferDynamicPct": round2(final),
        "bufferReason": f"exp move +{round2(em_component):.2f}% · pm range +{round2(pm_component):.2f}% · vwap dist +{round2(vwap_component):.2f}% · sesión +{round2(session_boost):.2f}% · macro +{round2(macro_boost):.2f}%",
        "bufferDebug": {
            "expectedMovePct": round2(expected_move_pct),
            "premarketRangePct": round2(pm_range_pct),
            "vwapDistPctAbs": round2(vwap_dist_pct_abs),
            "emComponent": round2(em_component),
            "pmComponent": round2(pm_component),
            "vwapComponent": round2(vwap_component),
            "sessionBoost": round2(session_boost),
            "macroBoost": round2(macro_boost),
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

    return {
        "status": "OK",
        "next": item,
        "items": [item],
        "all": [item],
    }


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
            "thresholdMinutes": 20,
            "isStale": True,
            "status": "Sin timestamp",
        }

    try:
        last_dt = datetime.fromisoformat(last_bar_at)
        age = (now_utc() - last_dt.astimezone(UTC_ZONE)).total_seconds() / 60.0
        is_prev_session = age > 390
        return {
            "ageMinutes": round2(age),
            "thresholdMinutes": 3,
            "isStale": False if is_prev_session else age > 3,
            "status": "Prev session close" if is_prev_session else ("Stale" if age > 3 else "OK"),
        }
    except Exception:
        return {
            "ageMinutes": None,
            "thresholdMinutes": 20,
            "isStale": True,
            "status": "Error",
        }


def compute_score_and_reasons(price_data, options, trade, macro, session):
    reasons = []
    score = 50

    vwap_dist = price_data["vwap"]["distPct"]

    if vwap_dist is not None and vwap_dist < 0:
        reasons.append("Precio por debajo de VWAP")
        score += 5
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
    if price_data["changePct"] is not None and price_data["changePct"] < 0:
        reasons.insert(0, "Sesgo bajista favorable")

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

    price_data = get_price_data(TICKER)
    macro = get_macro_block()
    earnings = get_earnings_block()
    session = get_session_label(now_ny())
    expected_move = compute_expected_move(price_data["price"], price_data["changePct"])

    pm_range_pct = 0.0
    vwap_dist_pct_abs = abs(price_data["vwap"]["distPct"]) if price_data["vwap"]["distPct"] is not None else 0.0

    buffer_part = compute_buffer(
        expected_move_pct=expected_move["movePct"],
        pm_range_pct=pm_range_pct,
        vwap_dist_pct_abs=vwap_dist_pct_abs,
        session_code=session["code"],
        macro_veto=macro["next"]["isVeto"],
    )

    options = get_options_snapshot_fallback(TICKER)
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
    score, decision, tone, label, risk, reasons = compute_score_and_reasons(price_data, options, trade, macro, session)

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
        "summary": f"buffer {buffer_part['bufferDynamicPct']:.2f}% · short {('--' if trade['shortStrike'] is None else trade['shortStrike'])} · crédito {('--' if trade['netCredit'] is None else trade['netCredit'])} · macro {macro['next']['countdown']} · tramo {session['code']}",
        "trade": trade,
        "bufferDebug": buffer_part["bufferDebug"],
        "options": options,
        "vwap": price_data["vwap"],
        "openingRange": {
            "available": False,
            "status": "Pendiente",
            "message": "Opening Range disponible a partir de 09:35 ET",
        },
        "premarketRange": {
            "available": False,
            "status": "No disponible",
            "message": "Premarket Range no disponible",
        },
        "expectedMove": expected_move,
        "freshness": freshness,
        "score": score,
        "decision": decision,
        "decisionTone": tone,
        "decisionLabel": label,
        "riskLabel": risk,
        "reasons": reasons,
        "alerts": [],
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
