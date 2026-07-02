import os
import json
import math
from datetime import datetime, timedelta, timezone

import requests
import yfinance as yf
import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
HISTORY_FILE = os.path.join(BASE_DIR, "history.json")

SYMBOL = "QQQ"
BASE_BUFFER = 1.85
SHORT_DELTA_TARGET = 0.15
STRIKE_STEP = 1.0
DATA_STALE_MINUTES = 3
HISTORY_LIMIT = 300

NY_TZ = timezone(timedelta(hours=-4))


def now_utc():
    return datetime.now(timezone.utc)


def now_ny():
    return now_utc().astimezone(NY_TZ)


def iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def safe_float(v, default=None):
    try:
        if v is None:
            return default
        if isinstance(v, str) and not v.strip():
            return default
        x = float(v)
        if math.isnan(x):
            return default
        return x
    except Exception:
        return default


def round_step(x, step=1.0):
    if x is None:
        return None
    return round(x / step) * step


def floor_step(x, step=1.0):
    if x is None:
        return None
    return math.floor(x / step) * step


def ceil_step(x, step=1.0):
    if x is None:
        return None
    return math.ceil(x / step) * step


def json_safe(obj):
    if obj is None:
        return None

    if isinstance(obj, (str, int, float, bool)):
        return obj

    if isinstance(obj, (datetime, pd.Timestamp)):
        return obj.isoformat()

    if isinstance(obj, pd.Series):
        return {str(k): json_safe(v) for k, v in obj.to_dict().items()}

    if isinstance(obj, pd.DataFrame):
        return [json_safe(row) for row in obj.to_dict(orient="records")]

    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}

    if isinstance(obj, (list, tuple, set)):
        return [json_safe(x) for x in obj]

    if hasattr(obj, "item"):
        try:
            return obj.item()
        except Exception:
            pass

    return str(obj)


def market_phase(ny_dt):
    h = ny_dt.hour
    m = ny_dt.minute
    mins = h * 60 + m
    pm_start = 4 * 60
    open_start = 9 * 60 + 30
    or_end = 10 * 60 + 30
    close_time = 16 * 60

    if mins < pm_start:
        return "overnight"
    if pm_start <= mins < open_start:
        return "premarket"
    if open_start <= mins < or_end:
        return "opening_range"
    if or_end <= mins < close_time:
        return "regular"
    return "afterhours"


def session_name(phase):
    mapping = {
        "overnight": "Overnight",
        "premarket": "Premarket",
        "opening_range": "Opening range",
        "regular": "Regular",
        "afterhours": "After hours",
    }
    return mapping.get(phase, phase)


def get_quote(symbol):
    t = yf.Ticker(symbol)
    fast = None
    try:
        fast = t.fast_info
    except Exception:
        fast = None

    hist_1d = None
    try:
        hist_1d = t.history(period="5d", interval="1d", auto_adjust=False, prepost=True)
    except Exception:
        hist_1d = None

    hist_1m = None
    try:
        hist_1m = t.history(period="1d", interval="1m", auto_adjust=False, prepost=True)
    except Exception:
        hist_1m = None

    price = None
    prev_close = None
    open_price = None
    day_high = None
    day_low = None
    volume = None

    if fast:
        price = safe_float(getattr(fast, "last_price", None), price)
        prev_close = safe_float(getattr(fast, "previous_close", None), prev_close)
        open_price = safe_float(getattr(fast, "open", None), open_price)
        day_high = safe_float(getattr(fast, "day_high", None), day_high)
        day_low = safe_float(getattr(fast, "day_low", None), day_low)
        volume = safe_float(getattr(fast, "last_volume", None), volume)

    if hist_1m is not None and not hist_1m.empty:
        last = hist_1m.iloc[-1]
        price = safe_float(last.get("Close"), price)
        day_high = safe_float(hist_1m["High"].max(), day_high)
        day_low = safe_float(hist_1m["Low"].min(), day_low)
        volume = safe_float(hist_1m["Volume"].sum(), volume)
        try:
            reg = hist_1m.between_time("09:30", "16:00")
            if not reg.empty:
                open_price = safe_float(reg.iloc[0]["Open"], open_price)
        except Exception:
            pass

    if hist_1d is not None and len(hist_1d) >= 2:
        prev_close = safe_float(hist_1d.iloc[-2]["Close"], prev_close)

    chg = None
    chg_pct = None
    if price is not None and prev_close not in (None, 0):
        chg = price - prev_close
        chg_pct = (chg / prev_close) * 100

    return {
        "price": price,
        "previous_close": prev_close,
        "open": open_price,
        "day_high": day_high,
        "day_low": day_low,
        "volume": volume,
        "change": chg,
        "change_pct": chg_pct,
        "hist_1m": hist_1m,
        "ticker": t,
    }


def compute_vwap(hist_1m):
    if hist_1m is None or hist_1m.empty:
        return None
    try:
        df = hist_1m.copy()
        tp = (df["High"] + df["Low"] + df["Close"]) / 3.0
        vol = df["Volume"].fillna(0)
        if vol.sum() == 0:
            return None
        return float((tp * vol).sum() / vol.sum())
    except Exception:
        return None


def compute_ranges(hist_1m):
    out = {
        "premarket_range": None,
        "opening_range": None,
    }
    if hist_1m is None or hist_1m.empty:
        return out

    try:
        pm = hist_1m.between_time("04:00", "09:29")
        if not pm.empty:
            pm_high = safe_float(pm["High"].max())
            pm_low = safe_float(pm["Low"].min())
            out["premarket_range"] = {
                "status": "available",
                "high": pm_high,
                "low": pm_low,
                "width": None if pm_high is None or pm_low is None else pm_high - pm_low,
                "message": "Rango del premarket calculado.",
            }
        else:
            out["premarket_range"] = {
                "status": "pending",
                "message": "Premarket aún no disponible.",
            }
    except Exception:
        out["premarket_range"] = {
            "status": "pending",
            "message": "Premarket range no disponible.",
        }

    try:
        reg = hist_1m.between_time("09:30", "10:30")
        ny = now_ny()
        phase = market_phase(ny)
        if phase in ("overnight", "premarket"):
            out["opening_range"] = {
                "status": "pending",
                "message": "Opening range pendiente hasta la apertura regular.",
            }
        elif phase == "opening_range":
            if reg.empty:
                out["opening_range"] = {
                    "status": "forming",
                    "message": "Opening range formándose.",
                }
            else:
                r_high = safe_float(reg["High"].max())
                r_low = safe_float(reg["Low"].min())
                out["opening_range"] = {
                    "status": "forming",
                    "high": r_high,
                    "low": r_low,
                    "width": None if r_high is None or r_low is None else r_high - r_low,
                    "message": "Opening range en formación.",
                }
        else:
            if reg.empty:
                out["opening_range"] = {
                    "status": "missing",
                    "message": "Opening range no disponible.",
                }
            else:
                r_high = safe_float(reg["High"].max())
                r_low = safe_float(reg["Low"].min())
                out["opening_range"] = {
                    "status": "available",
                    "high": r_high,
                    "low": r_low,
                    "width": None if r_high is None or r_low is None else r_high - r_low,
                    "message": "Opening range completado.",
                }
    except Exception:
        out["opening_range"] = {
            "status": "missing",
            "message": "Opening range no disponible.",
        }

    return out


def compute_expected_move(ticker, spot):
    methods = [
        ("3mo", "3mo"),
        ("1mo", "1mo"),
    ]
    for label, period in methods:
        try:
            hist = ticker.history(period=period, interval="1d", auto_adjust=False)
            if hist is not None and len(hist) >= 10:
                rets = hist["Close"].pct_change().dropna()
                hv_daily = float(rets.std())
                if hv_daily and hv_daily > 0:
                    em_pct = hv_daily * 100
                    em_dollar = spot * hv_daily if spot else None
                    return {
                        "method": f"historical_vol_{label}",
                        "daily_vol_pct": em_pct,
                        "expected_move_dollar": em_dollar,
                        "upper": None if em_dollar is None or spot is None else spot + em_dollar,
                        "lower": None if em_dollar is None or spot is None else spot - em_dollar,
                    }
        except Exception:
            pass

    try:
        hist = ticker.history(period="5d", interval="5m", auto_adjust=False, prepost=True)
        if hist is not None and len(hist) >= 30:
            rets = hist["Close"].pct_change().dropna()
            hv_5m = float(rets.std())
            bars = 78
            hv_daily = hv_5m * math.sqrt(bars)
            em_pct = hv_daily * 100
            em_dollar = spot * hv_daily if spot else None
            return {
                "method": "intraday_fallback_5m",
                "daily_vol_pct": em_pct,
                "expected_move_dollar": em_dollar,
                "upper": None if em_dollar is None or spot is None else spot + em_dollar,
                "lower": None if em_dollar is None or spot is None else spot - em_dollar,
            }
    except Exception:
        pass

    return {
        "method": "unavailable",
        "daily_vol_pct": None,
        "expected_move_dollar": None,
        "upper": None,
        "lower": None,
    }


def get_macro_events():
    today = now_utc().date()
    return [
        {
            "time_et": "08:30",
            "title": "Macro placeholder",
            "impact": "medium",
            "date": str(today),
        }
    ]


def get_mag7_earnings():
    return [
        {"symbol": "AAPL", "date": None, "status": "No cercano"},
        {"symbol": "MSFT", "date": None, "status": "No cercano"},
        {"symbol": "NVDA", "date": None, "status": "No cercano"},
        {"symbol": "AMZN", "date": None, "status": "No cercano"},
        {"symbol": "META", "date": None, "status": "No cercano"},
        {"symbol": "GOOGL", "date": None, "status": "No cercano"},
        {"symbol": "TSLA", "date": None, "status": "No cercano"},
    ]


def nearest_macro_risk(macro_events):
    ny = now_ny()
    today = str(ny.date())
    soon = None
    for ev in macro_events:
        if ev.get("date") != today:
            continue
        t = ev.get("time_et")
        if not t:
            continue
        hh, mm = map(int, t.split(":"))
        dt = ny.replace(hour=hh, minute=mm, second=0, microsecond=0)
        diff = (dt - ny).total_seconds() / 60
        if -15 <= diff <= 90:
            if soon is None or abs(diff) < abs(soon["mins"]):
                soon = {"mins": diff, "impact": ev.get("impact", "low"), "title": ev.get("title", "")}
    return soon


def earnings_veto(earnings):
    for e in earnings:
        status = (e.get("status") or "").lower()
        if "today" in status or "mañana" in status or "tomorrow" in status:
            return True
    return False


def choose_expiration(ticker):
    try:
        exps = ticker.options
        if exps:
            return exps[0]
    except Exception:
        pass
    return None


def choose_option_setup(ticker, spot, dynamic_buffer, phase):
    expiration = choose_expiration(ticker)
    fallback = {
        "expiration": expiration,
        "status": "no_chain",
        "message": "Cadena de opciones no disponible.",
        "short_call": None,
        "long_call": None,
        "spread_width": 1.0,
        "net_credit_mid": None,
        "liquidity_score": 0,
        "quotes_reliable": phase in ("opening_range", "regular"),
        "short_strike_target": ceil_step((spot or 0) + dynamic_buffer, STRIKE_STEP) if spot else None,
    }
    if not expiration:
        return fallback

    try:
        chain = ticker.option_chain(expiration)
        calls = chain.calls.copy()
    except Exception:
        return fallback

    if calls is None or calls.empty or spot is None:
        return fallback

    calls["delta_abs_diff"] = SHORT_DELTA_TARGET
    if "delta" in calls.columns:
        calls["delta_abs_diff"] = (calls["delta"].abs() - SHORT_DELTA_TARGET).abs()
    else:
        calls["delta"] = None

    calls["distance_from_spot"] = calls["strike"] - spot
    calls = calls[calls["strike"] >= spot]
    if calls.empty:
        return fallback

    target_short = ceil_step(spot + dynamic_buffer, STRIKE_STEP)
    calls["target_diff"] = (calls["strike"] - target_short).abs()

    calls = calls.sort_values(["target_diff", "delta_abs_diff"]).reset_index(drop=True)
    short = json_safe(calls.iloc[0].to_dict())

    short_strike = safe_float(short.get("strike"))
    long_strike = short_strike + STRIKE_STEP if short_strike is not None else None

    long_row = calls[calls["strike"] == long_strike]
    if long_row.empty:
      try:
        all_calls = chain.calls.copy()
        long_row = all_calls[all_calls["strike"] == long_strike]
      except Exception:
        long_row = pd.DataFrame()

    long_call = json_safe(long_row.iloc[0].to_dict()) if not long_row.empty else None

    bid = safe_float(short.get("bid"), 0)
    ask = safe_float(short.get("ask"), 0)
    mid_short = (bid + ask) / 2 if (bid is not None and ask is not None) else None

    long_mid = None
    if long_call:
        lb = safe_float(long_call.get("bid"), 0)
        la = safe_float(long_call.get("ask"), 0)
        long_mid = (lb + la) / 2 if (lb is not None and la is not None) else None

    net_credit = None
    if mid_short is not None and long_mid is not None:
        net_credit = mid_short - long_mid

    oi = safe_float(short.get("openInterest"), 0)
    vol = safe_float(short.get("volume"), 0)
    spread = None if bid is None or ask is None else ask - bid

    liquidity_score = 0
    if oi >= 1000:
        liquidity_score += 40
    elif oi >= 500:
        liquidity_score += 25
    elif oi >= 100:
        liquidity_score += 10

    if vol >= 500:
        liquidity_score += 30
    elif vol >= 100:
        liquidity_score += 15
    elif vol >= 25:
        liquidity_score += 8

    if spread is not None:
        if spread <= 0.03:
            liquidity_score += 30
        elif spread <= 0.06:
            liquidity_score += 20
        elif spread <= 0.10:
            liquidity_score += 10

    quotes_reliable = phase in ("opening_range", "regular") and not (
        safe_float(short.get("bid"), 0) == 0 and safe_float(short.get("ask"), 0) == 0
    )

    return {
        "expiration": expiration,
        "status": "ok",
        "message": "Opciones cargadas." if quotes_reliable else "Premarket: quotes de opciones aún no fiables.",
        "short_call": short,
        "long_call": long_call,
        "spread_width": STRIKE_STEP,
        "net_credit_mid": net_credit,
        "liquidity_score": clamp(liquidity_score, 0, 100),
        "quotes_reliable": quotes_reliable,
        "short_strike_target": target_short,
    }


def compute_dynamic_buffer(price, expected_move, opening_range, phase, vwap, macro_risk):
    base = BASE_BUFFER
    em = safe_float((expected_move or {}).get("expected_move_dollar"))
    or_width = safe_float((opening_range or {}).get("width"))
    em_component = em * 0.14 if em is not None else 0
    or_component = or_width * 0.33 if or_width is not None else 0

    buffer_value = max(base, em_component, or_component)

    reasons = [f"base {base:.2f}"]
    if em_component > base:
        reasons.append(f"expected move {em_component:.2f}")
    if or_component > base:
        reasons.append(f"opening range {or_component:.2f}")

    if macro_risk and macro_risk["impact"] in ("high", "medium"):
        mult = 1.18 if macro_risk["impact"] == "high" else 1.10
        buffer_value *= mult
        reasons.append(f"macro x{mult:.2f}")

    if phase == "opening_range":
        buffer_value *= 1.10
        reasons.append("opening range x1.10")
    elif phase == "premarket":
        buffer_value *= 1.06
        reasons.append("premarket x1.06")

    if price is not None and vwap is not None and price > vwap:
        buffer_value *= 1.05
        reasons.append("price>VWAP x1.05")

    return round(buffer_value, 2), ", ".join(reasons)


def score_trade(price, vwap, dynamic_buffer, short_strike, expected_move, opening_range, macro_risk, earnings_block, option_setup, phase, freshness_min):
    score = 70
    notes = []

    if earnings_block:
        score -= 45
        notes.append("Veto por earnings cercanos.")

    if macro_risk:
        if macro_risk["impact"] == "high":
            score -= 22
            notes.append("Macro de alto impacto cercana.")
        elif macro_risk["impact"] == "medium":
            score -= 10
            notes.append("Macro de impacto medio cercana.")

    if freshness_min is not None and freshness_min > DATA_STALE_MINUTES:
        score -= 12
        notes.append("Datos con frescura insuficiente.")

    if price is not None and vwap is not None:
        if price > vwap:
            score -= 8
            notes.append("Precio por encima de VWAP.")
        else:
            score += 6
            notes.append("Precio por debajo de VWAP.")

    em = safe_float((expected_move or {}).get("expected_move_dollar"))
    if short_strike is not None and price is not None:
        dist = short_strike - price
        if dist >= dynamic_buffer:
            score += 10
            notes.append("Distancia al short strike aceptable.")
        else:
            score -= 16
            notes.append("Short strike demasiado cerca.")
        if em is not None:
            if dist < em * 0.10:
                score -= 14
                notes.append("Strike muy cerca para el expected move.")
            elif dist > em * 0.18:
                score += 5
                notes.append("Colchón razonable frente al expected move.")

    or_width = safe_float((opening_range or {}).get("width"))
    if or_width is not None:
        if or_width > 4.0:
            score -= 8
            notes.append("Opening range amplio.")
        elif or_width < 2.0:
            score += 4
            notes.append("Opening range contenido.")

    if option_setup:
        liq = safe_float(option_setup.get("liquidity_score"), 0)
        if liq >= 70:
            score += 8
            notes.append("Liquidez de opciones buena.")
        elif liq < 30:
            score -= 10
            notes.append("Liquidez floja.")
        if not option_setup.get("quotes_reliable", False):
            score -= 8
            notes.append("Quotes aún no fiables.")

    if phase == "opening_range":
        score -= 4
        notes.append("Primer tramo de sesión más inestable.")
    elif phase == "regular":
        score += 3
        notes.append("Tramo regular más favorable.")
    elif phase == "premarket":
        score -= 12
        notes.append("Premarket: contexto menos fiable.")

    score = clamp(int(round(score)), 0, 100)

    if earnings_block or score < 45:
        decision = "No entraría"
        risk = "Alto"
    elif score < 65:
        decision = "Esperar confirmación"
        risk = "Medio"
    else:
        decision = "Entraría"
        risk = "Controlado"

    return score, decision, risk, notes


def format_trade_setup(price, dynamic_buffer, option_setup):
    short_strike = None
    short_delta = None
    short_oi = None
    short_vol = None
    if option_setup and option_setup.get("short_call"):
        sc = option_setup["short_call"]
        short_strike = safe_float(sc.get("strike"))
        short_delta = safe_float(sc.get("delta"))
        short_oi = safe_float(sc.get("openInterest"))
        short_vol = safe_float(sc.get("volume"))

    distance = None if price is None or short_strike is None else short_strike - price

    return {
        "buffer_base": BASE_BUFFER,
        "buffer_dynamic": dynamic_buffer,
        "buffer_reason": option_setup.get("buffer_reason") if option_setup else None,
        "short_strike_target": option_setup.get("short_strike_target") if option_setup else None,
        "short_strike_selected": short_strike,
        "distance_to_short_strike": distance,
        "short_delta": short_delta,
        "short_open_interest": short_oi,
        "short_volume": short_vol,
        "spread_width": option_setup.get("spread_width") if option_setup else None,
        "estimated_credit_mid": option_setup.get("net_credit_mid") if option_setup else None,
    }


def build_summary(decision, score, phase, fresh_mins, option_setup, trade_setup):
    pieces = [f"{decision} ({score}/100).", f"Sesión: {session_name(phase)}."]
    if fresh_mins is not None:
        pieces.append(f"Frescura: {fresh_mins:.1f} min.")
    if trade_setup.get("short_strike_selected") is not None and trade_setup.get("distance_to_short_strike") is not None:
        pieces.append(
            f"Short {trade_setup['short_strike_selected']:.2f}, distancia {trade_setup['distance_to_short_strike']:.2f}."
        )
    if option_setup and not option_setup.get("quotes_reliable", True):
        pieces.append("Quotes de opciones aún no fiables.")
    return " ".join(pieces)


def load_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(json_safe(data), f, ensure_ascii=False, indent=2, default=str)


def main():
    ny = now_ny()
    phase = market_phase(ny)

    quote = get_quote(SYMBOL)
    price = quote["price"]
    ticker = quote["ticker"]
    hist_1m = quote["hist_1m"]
    vwap = compute_vwap(hist_1m)
    ranges = compute_ranges(hist_1m)

    expected_move = compute_expected_move(ticker, price)
    macro_events = get_macro_events()
    earnings = get_mag7_earnings()
    macro_risk = nearest_macro_risk(macro_events)
    earnings_block = earnings_veto(earnings)

    dynamic_buffer, buffer_reason = compute_dynamic_buffer(
        price=price,
        expected_move=expected_move,
        opening_range=ranges.get("opening_range"),
        phase=phase,
        vwap=vwap,
        macro_risk=macro_risk,
    )

    option_setup = choose_option_setup(ticker, price, dynamic_buffer, phase)
    option_setup["buffer_reason"] = buffer_reason

    short_strike = None
    if option_setup.get("short_call"):
        short_strike = safe_float(option_setup["short_call"].get("strike"))

    fresh_mins = 0.0

    score, decision, risk, notes = score_trade(
        price=price,
        vwap=vwap,
        dynamic_buffer=dynamic_buffer,
        short_strike=short_strike,
        expected_move=expected_move,
        opening_range=ranges.get("opening_range"),
        macro_risk=macro_risk,
        earnings_block=earnings_block,
        option_setup=option_setup,
        phase=phase,
        freshness_min=fresh_mins,
    )

    trade_setup = format_trade_setup(price, dynamic_buffer, option_setup)
    summary = build_summary(decision, score, phase, fresh_mins, option_setup, trade_setup)

    state = {
        "symbol": SYMBOL,
        "timestamp_utc": iso(now_utc()),
        "timestamp_ny": now_ny().strftime("%Y-%m-%d %H:%M:%S ET"),
        "session": session_name(phase),
        "phase": phase,
        "freshness_minutes": fresh_mins,
        "freshness_alert": fresh_mins > DATA_STALE_MINUTES,

        "price": price,
        "change": quote["change"],
        "change_pct": quote["change_pct"],
        "open": quote["open"],
        "day_high": quote["day_high"],
        "day_low": quote["day_low"],
        "volume": quote["volume"],
        "vwap": vwap,

        "expected_move": expected_move,
        "premarket_range": ranges.get("premarket_range"),
        "opening_range": ranges.get("opening_range"),

        "score": score,
        "decision": decision,
        "risk": risk,
        "summary": summary,
        "notes": notes,

        "buffer": {
            "base": BASE_BUFFER,
            "dynamic": dynamic_buffer,
            "reason": buffer_reason,
        },

        "trade_setup": trade_setup,

        "options_snapshot": {
            "status": option_setup.get("status"),
            "message": option_setup.get("message"),
            "quotes_reliable": option_setup.get("quotes_reliable"),
            "liquidity_score": option_setup.get("liquidity_score"),
            "expiration": option_setup.get("expiration"),
            "short_call": option_setup.get("short_call"),
            "long_call": option_setup.get("long_call"),
            "net_credit_mid": option_setup.get("net_credit_mid"),
            "spread_width": option_setup.get("spread_width"),
        },

        "macro": {
            "next_events": macro_events,
            "risk_context": macro_risk,
        },

        "earnings": {
            "mag7": earnings,
            "earnings_veto": earnings_block,
        },
    }

    history = load_history()
    history.append({
        "timestamp_utc": state["timestamp_utc"],
        "price": state["price"],
        "score": state["score"],
        "decision": state["decision"],
        "buffer_dynamic": dynamic_buffer,
        "vwap": state["vwap"],
    })
    history = history[-HISTORY_LIMIT:]

    save_json(STATE_FILE, state)
    save_json(HISTORY_FILE, history)


if __name__ == "__main__":
    main()
