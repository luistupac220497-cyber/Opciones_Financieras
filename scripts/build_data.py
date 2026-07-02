import os
import json
import math
from datetime import datetime, timedelta, timezone, date

import pandas as pd
import yfinance as yf

STATE_FILE = "state.json"
HISTORY_FILE = "history.json"

SYMBOL = "QQQ"
BASE_BUFFER = 1.85
SHORT_DELTA_TARGET = 0.15
STRIKE_STEP = 1.0
DATA_STALE_MINUTES = 3
MAG7 = ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA"]
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


def ceil_step(x, step=1.0):
    if x is None:
        return None
    return math.ceil(x / step) * step


def market_phase(ny_dt):
    mins = ny_dt.hour * 60 + ny_dt.minute
    if mins < 4 * 60:
        return "overnight"
    if mins < 9 * 60 + 30:
        return "premarket"
    if mins < 10 * 60 + 30:
        return "opening_range"
    if mins < 16 * 60:
        return "regular"
    return "afterhours"


def session_name(phase):
    return {
        "overnight": "Overnight",
        "premarket": "Premarket",
        "opening_range": "Opening range",
        "regular": "Regular",
        "afterhours": "After hours",
    }.get(phase, phase)


def get_quote(symbol):
    t = yf.Ticker(symbol)
    hist_1m = None
    hist_1d = None
    try:
        hist_1m = t.history(period="1d", interval="1m", auto_adjust=False, prepost=True)
    except Exception:
        pass
    try:
        hist_1d = t.history(period="5d", interval="1d", auto_adjust=False, prepost=True)
    except Exception:
        pass

    price = None
    prev_close = None
    open_price = None
    day_high = None
    day_low = None
    volume = None

    try:
        fi = t.fast_info
        price = safe_float(getattr(fi, "last_price", None), price)
        prev_close = safe_float(getattr(fi, "previous_close", None), prev_close)
        open_price = safe_float(getattr(fi, "open", None), open_price)
        day_high = safe_float(getattr(fi, "day_high", None), day_high)
        day_low = safe_float(getattr(fi, "day_low", None), day_low)
        volume = safe_float(getattr(fi, "last_volume", None), volume)
    except Exception:
        pass

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
    tp = (hist_1m["High"] + hist_1m["Low"] + hist_1m["Close"]) / 3.0
    vol = hist_1m["Volume"].fillna(0)
    if vol.sum() == 0:
        return None
    return float((tp * vol).sum() / vol.sum())


def compute_ranges(hist_1m):
    out = {"premarket_range": None, "opening_range": None}
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
            out["premarket_range"] = {"status": "pending", "message": "Premarket aún no disponible."}
    except Exception:
        out["premarket_range"] = {"status": "pending", "message": "Premarket range no disponible."}

    try:
        reg = hist_1m.between_time("09:30", "10:30")
        phase = market_phase(now_ny())
        if phase in ("overnight", "premarket"):
            out["opening_range"] = {"status": "pending", "message": "Opening range pendiente hasta la apertura regular."}
        elif phase == "opening_range":
            if reg.empty:
                out["opening_range"] = {"status": "forming", "message": "Opening range formándose."}
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
                out["opening_range"] = {"status": "missing", "message": "Opening range no disponible."}
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
        out["opening_range"] = {"status": "missing", "message": "Opening range no disponible."}

    return out


def compute_expected_move(ticker, spot):
    for period in ["3mo", "1mo"]:
        try:
            hist = ticker.history(period=period, interval="1d", auto_adjust=False)
            if hist is not None and len(hist) >= 10:
                rets = hist["Close"].pct_change().dropna()
                hv_daily = float(rets.std())
                if hv_daily > 0 and spot:
                    em_dollar = spot * hv_daily
                    return {
                        "method": f"historical_vol_{period}",
                        "daily_vol_pct": hv_daily * 100,
                        "expected_move_dollar": em_dollar,
                        "upper": spot + em_dollar,
                        "lower": spot - em_dollar,
                    }
        except Exception:
            pass

    try:
        hist = ticker.history(period="5d", interval="5m", auto_adjust=False, prepost=True)
        if hist is not None and len(hist) >= 30 and spot:
            rets = hist["Close"].pct_change().dropna()
            hv_5m = float(rets.std())
            hv_daily = hv_5m * math.sqrt(78)
            em_dollar = spot * hv_daily
            return {
                "method": "intraday_fallback_5m",
                "daily_vol_pct": hv_daily * 100,
                "expected_move_dollar": em_dollar,
                "upper": spot + em_dollar,
                "lower": spot - em_dollar,
            }
    except Exception:
        pass

    return {"method": "unavailable", "daily_vol_pct": None, "expected_move_dollar": None, "upper": None, "lower": None}


def get_macro_events():
    today = now_ny().date()
    return [
        {"time_et": "08:30", "title": "Macro placeholder", "impact": "medium", "date": str(today)}
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
        if -15 <= diff <= 120:
            if soon is None or abs(diff) < abs(soon["mins"]):
                soon = {"mins": diff, "impact": ev.get("impact", "low"), "title": ev.get("title", "")}
    return soon


def normalize_earnings_date(value):
    try:
        if value is None:
            return None
        if isinstance(value, pd.Timestamp):
            return value.date()
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return pd.to_datetime(value).date()
    except Exception:
        return None


def classify_earnings_status(d):
    if d is None:
        return "No cercano"
    today = now_ny().date()
    diff = (d - today).days
    if diff < 0:
        return "Pasado"
    if diff == 0:
        return "Today"
    if diff == 1:
        return "Tomorrow"
    if diff <= 7:
        return "This week"
    return "No cercano"


def get_symbol_earnings_date(symbol):
    try:
        t = yf.Ticker(symbol)
        df = t.get_earnings_dates(limit=12)
        if df is not None and not df.empty:
            future_dates = []
            for idx in df.index:
                d = normalize_earnings_date(idx)
                if d and d >= now_ny().date():
                    future_dates.append(d)
            if future_dates:
                return min(future_dates), "ticker_get_earnings_dates"
    except Exception:
        pass
    return None, None


def get_mag7_earnings():
    found = {}
    for sym in MAG7:
        d, source = get_symbol_earnings_date(sym)
        if d is not None:
            found[sym] = (d, source)

    missing = [sym for sym in MAG7 if sym not in found]
    if missing:
        try:
            cal = yf.Calendars(start=now_ny().date(), end=now_ny().date() + timedelta(days=30))
            df = cal.get_earnings_calendar(limit=100, force=True)
            if df is not None and not df.empty:
                symbol_col = None
                date_col = None
                for c in df.columns:
                    lc = str(c).lower()
                    if symbol_col is None and ("symbol" in lc or "ticker" in lc):
                        symbol_col = c
                    if date_col is None and ("date" in lc or "earn" in lc or "startdatetime" in lc):
                        date_col = c

                if symbol_col is not None:
                    for _, row in df.iterrows():
                        sym = str(row.get(symbol_col, "")).upper().strip()
                        if sym in missing and sym not in found:
                            d = normalize_earnings_date(row.get(date_col)) if date_col is not None else None
                            if d is not None:
                                found[sym] = (d, "calendars_get_earnings_calendar")
        except Exception:
            pass

    out = []
    for sym in MAG7:
        d, source = found.get(sym, (None, None))
        out.append({"symbol": sym, "date": None if d is None else str(d), "status": classify_earnings_status(d), "source": source})
    return out


def earnings_veto(earnings):
    return any((e.get("status") or "").lower() in ("today", "tomorrow", "this week") for e in earnings)


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

    if "delta" not in calls.columns:
        calls["delta"] = None

    calls["delta_abs_diff"] = calls["delta"].apply(lambda x: abs(abs(x) - SHORT_DELTA_TARGET) if x is not None and pd.notna(x) else 999)
    target_short = ceil_step(spot + dynamic_buffer, STRIKE_STEP)
    calls = calls[calls["strike"] >= spot].copy()
    if calls.empty:
        return fallback

    calls["target_diff"] = (calls["strike"] - target_short).abs()
    calls = calls.sort_values(["target_diff", "delta_abs_diff"]).reset_index(drop=True)
    short = calls.iloc[0].to_dict()

    short_strike = safe_float(short.get("strike"))
    long_strike = short_strike + STRIKE_STEP if short_strike is not None else None

    long_row = calls[calls["strike"] == long_strike]
    if long_row.empty:
        all_calls = chain.calls.copy()
        long_row = all_calls[all_calls["strike"] == long_strike]

    long_call = long_row.iloc[0].to_dict() if not long_row.empty else None

    bid = safe_float(short.get("bid"), 0)
    ask = safe_float(short.get("ask"), 0)
    mid_short = (bid + ask) / 2 if bid is not None and ask is not None else None

    long_mid = None
    if long_call:
        lb = safe_float(long_call.get("bid"), 0)
        la = safe_float(long_call.get("ask"), 0)
        long_mid = (lb + la) / 2 if lb is not None and la is not None else None

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

    quotes_reliable = phase in ("opening_range", "regular") and not (safe_float(short.get("bid"), 0) == 0 and safe_float(short.get("ask"), 0) == 0)

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


def build_buffer_logic(price, expected_move, opening_range, premarket_range, phase, vwap, macro_risk):
    base = BASE_BUFFER
    em = safe_float((expected_move or {}).get("expected_move_dollar"))
    or_width = safe_float((opening_range or {}).get("width"))
    pm_width = safe_float((premarket_range or {}).get("width"))

    em_component = em * 0.14 if em is not None else 0
    or_component = or_width * 0.33 if or_width is not None else 0
    pm_component = pm_width * 0.18 if pm_width is not None else 0

    raw = max(base, em_component, or_component, pm_component)
    reasons = [f"base {base:.2f}"]

    if em_component > base:
        reasons.append(f"expected move factor {em_component:.2f}")
    if or_component > base:
        reasons.append(f"opening range factor {or_component:.2f}")
    if pm_component > base:
        reasons.append(f"premarket range factor {pm_component:.2f}")

    mult = 1.0
    if phase == "premarket":
        mult *= 1.08
        reasons.append("premarket multiplier x1.08")
    elif phase == "opening_range":
        mult *= 1.10
        reasons.append("opening range multiplier x1.10")

    if macro_risk and macro_risk.get("impact") == "high":
        mult *= 1.10
        reasons.append("high impact macro x1.10")
    elif macro_risk and macro_risk.get("impact") == "medium":
        mult *= 1.05
        reasons.append("medium impact macro x1.05")

    if price is not None and vwap is not None and price > vwap:
        mult *= 1.05
        reasons.append("price above VWAP x1.05")

    dynamic = round(raw * mult, 2)
    return dynamic, " | ".join(reasons)


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
        return score, "No entraría", "Alto", notes
    if score < 65:
        return score, "Esperar confirmación", "Medio", notes
    return score, "Entraría", "Controlado", notes


def format_trade_setup(price, dynamic_buffer, option_setup):
    sc = option_setup.get("short_call") if option_setup else None
    lc = option_setup.get("long_call") if option_setup else None
    short_strike = safe_float(sc.get("strike")) if sc else None
    long_strike = safe_float(lc.get("strike")) if lc else None
    short_delta = safe_float(sc.get("delta")) if sc else None
    short_oi = safe_float(sc.get("openInterest")) if sc else None
    short_vol = safe_float(sc.get("volume")) if sc else None
    net_credit = safe_float(option_setup.get("net_credit_mid")) if option_setup else None
    spread_width = safe_float(option_setup.get("spread_width")) if option_setup else None
    liq = safe_float(option_setup.get("liquidity_score")) if option_setup else None
    breakeven = short_strike + net_credit if short_strike is not None and net_credit is not None else None
    distance = short_strike - price if short_strike is not None and price is not None else None
    return {
        "buffer_dynamic": dynamic_buffer,
        "short_strike_selected": short_strike,
        "long_strike_selected": long_strike,
        "distance_to_short_strike": distance,
        "short_delta": short_delta,
        "short_open_interest": short_oi,
        "short_volume": short_vol,
        "spread_width": spread_width,
        "estimated_credit_mid": net_credit,
        "breakeven": breakeven,
        "liquidity_score": liq,
    }


def build_summary(decision, score, phase, fresh_mins, option_setup, trade_setup):
    pieces = [f"{decision} ({score}/100).", f"Sesión: {session_name(phase)}."]
    if fresh_mins is not None:
        pieces.append(f"Frescura: {fresh_mins:.1f} min.")
    if trade_setup.get("short_strike_selected") is not None and trade_setup.get("distance_to_short_strike") is not None:
        pieces.append(f"Short {trade_setup['short_strike_selected']:.2f}, distancia {trade_setup['distance_to_short_strike']:.2f}.")
    if option_setup and not option_setup.get("quotes_reliable", True):
        pieces.append("Quotes de opciones aún no fiables.")
    return " ".join(pieces)


def main():
    try:
        phase = market_phase(now_ny())
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

        dynamic_buffer, buffer_reason = build_buffer_logic(
            price=price,
            expected_move=expected_move,
            opening_range=ranges.get("opening_range"),
            premarket_range=ranges.get("premarket_range"),
            phase=phase,
            vwap=vwap,
            macro_risk=macro_risk,
        )

        option_setup = choose_option_setup(ticker, price, dynamic_buffer, phase)
        option_setup["buffer_reason"] = buffer_reason

        short_strike = safe_float(option_setup.get("short_call", {}).get("strike")) if option_setup.get("short_call") else None
        freshness = 0.0

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
            freshness_min=freshness,
        )

        trade_setup = format_trade_setup(price, dynamic_buffer, option_setup)
        summary = build_summary(decision, score, phase, freshness, option_setup, trade_setup)

        state = {
            "symbol": SYMBOL,
            "timestamp_utc": iso(now_utc()),
            "timestamp_ny": now_ny().strftime("%Y-%m-%d %H:%M:%S ET"),
            "session": session_name(phase),
            "phase": phase,
            "freshness_minutes": freshness,
            "freshness_alert": freshness > DATA_STALE_MINUTES,
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
            "buffer": {"base": BASE_BUFFER, "dynamic": dynamic_buffer, "reason_short": buffer_reason},
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
            "macro": {"next_events": macro_events, "risk_context": macro_risk},
            "earnings": {"mag7": earnings, "earnings_veto": earnings_block},
        }

        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

        history = [{
            "timestamp_utc": state["timestamp_utc"],
            "price": state["price"],
            "score": state["score"],
            "decision": state["decision"],
            "buffer_dynamic": dynamic_buffer,
            "vwap": state["vwap"],
        }]
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        fallback = {
            "symbol": SYMBOL,
            "timestamp_utc": iso(now_utc()),
            "timestamp_ny": now_ny().strftime("%Y-%m-%d %H:%M:%S ET"),
            "session": "Error",
            "phase": "error",
            "freshness_minutes": None,
            "freshness_alert": True,
            "price": None,
            "change": None,
            "change_pct": None,
            "open": None,
            "day_high": None,
            "day_low": None,
            "volume": None,
            "vwap": None,
            "expected_move": {"method": "unavailable", "daily_vol_pct": None, "expected_move_dollar": None, "upper": None, "lower": None},
            "premarket_range": None,
            "opening_range": None,
            "score": 0,
            "decision": "No entraría",
            "risk": "Alto",
            "summary": f"Error generando estado: {e}",
            "notes": [str(e)],
            "buffer": {"base": BASE_BUFFER, "dynamic": BASE_BUFFER, "reason_short": "error"},
            "trade_setup": {"buffer_dynamic": BASE_BUFFER},
            "options_snapshot": {"status": "error", "message": "Error generando snapshot", "quotes_reliable": False},
            "macro": {"next_events": get_macro_events(), "risk_context": None},
            "earnings": {"mag7": [{"symbol": s, "date": None, "status": "No cercano", "source": None} for s in MAG7], "earnings_veto": False},
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(fallback, f, ensure_ascii=False, indent=2)
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump([{"timestamp_utc": fallback["timestamp_utc"], "price": None, "score": 0, "decision": "No entraría", "buffer_dynamic": BASE_BUFFER, "vwap": None}], f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
