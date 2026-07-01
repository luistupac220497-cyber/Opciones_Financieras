import json
import math
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf


TICKER = "QQQ"
MAX_HISTORY_ITEMS = 200
SPREAD_WIDTH = 1.0
NET_CREDIT = 0.10
MODEL_BUFFER_PCT = 0.0185
STALE_MINUTES = 3

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
STATE_FILE = DATA_DIR / "state.json"
HISTORY_FILE = DATA_DIR / "history.json"

NY_TZ = ZoneInfo("America/New_York")

MAG7 = {
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "NVDA": "Nvidia",
    "AMZN": "Amazon",
    "META": "Meta",
    "GOOGL": "Alphabet",
    "TSLA": "Tesla",
}

MACRO_EVENTS = [
    {"evento": "ISM Manufacturero", "datetime_ny": "2026-07-01 10:00", "impacto": "alto"},
    {"evento": "Nóminas no agrícolas (NFP)", "datetime_ny": "2026-07-03 08:30", "impacto": "alto"},
    {"evento": "Tasa de desempleo", "datetime_ny": "2026-07-03 08:30", "impacto": "alto"},
    {"evento": "IPC (CPI)", "datetime_ny": "2026-07-15 08:30", "impacto": "alto"},
    {"evento": "IPP (PPI)", "datetime_ny": "2026-07-16 08:30", "impacto": "alto"},
    {"evento": "Ventas minoristas", "datetime_ny": "2026-07-16 08:30", "impacto": "medio"},
    {"evento": "PIB", "datetime_ny": "2026-07-30 08:30", "impacto": "alto"},
    {"evento": "PCE subyacente", "datetime_ny": "2026-07-31 08:30", "impacto": "alto"},
    {"evento": "Decisión FOMC / tipos", "datetime_ny": "2026-07-29 14:00", "impacto": "alto"},
]


def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_json(path, default):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def flatten_columns(df):
    if df is None or df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def safe_float(value, default=None):
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def normalize_date(value):
    if value is None:
        return None
    try:
        ts = pd.to_datetime(value)
        if pd.isna(ts):
            return None
        try:
            ts = ts.tz_localize(None)
        except Exception:
            pass
        return ts.date()
    except Exception:
        return None


def days_until(d, base_date=None):
    if d is None:
        return None
    base = base_date or datetime.now(NY_TZ).date()
    return (d - base).days


def translate_earnings_moment(text):
    if text is None:
        return "Hora no especificada"
    t = str(text).strip().lower()
    if "before market open" in t or "before open" in t:
        return "Antes de la apertura"
    if "after market close" in t or "after close" in t:
        return "Después del cierre"
    if "during market" in t or "market hours" in t or t == "tas":
        return "Durante la sesión"
    if "time not supplied" in t:
        return "Hora no especificada"
    return str(text)


def classify_session(now_ny):
    minutes = now_ny.hour * 60 + now_ny.minute
    open_m = 9 * 60 + 30
    noon_m = 12 * 60
    power_m = 15 * 60
    close_m = 16 * 60

    if minutes < 4 * 60:
        return "overnight", "Overnight"
    if 4 * 60 <= minutes < open_m:
        return "premarket", "Premarket"
    if open_m <= minutes < noon_m:
        return "apertura", "Apertura"
    if noon_m <= minutes < power_m:
        return "media_sesion", "Media sesión"
    if power_m <= minutes <= close_m:
        return "power_hour", "Power hour"
    if close_m < minutes <= 20 * 60:
        return "after_hours", "After hours"
    return "overnight", "Overnight"


def classify_macro_moment(dt_ny):
    mins = dt_ny.hour * 60 + dt_ny.minute
    open_m = 9 * 60 + 30
    close_m = 16 * 60
    if mins < open_m:
        return "Antes de la apertura"
    if mins <= close_m:
        return "Durante la sesión"
    return "Después del cierre"


def get_intraday_df():
    df = yf.download(
        tickers=TICKER,
        period="2d",
        interval="5m",
        auto_adjust=True,
        progress=False,
        prepost=True,
        threads=False,
    )
    df = flatten_columns(df)
    if df is None or df.empty:
        return None
    return df


def get_daily_df(period="3mo"):
    df = yf.download(
        tickers=TICKER,
        period=period,
        interval="1d",
        auto_adjust=True,
        progress=False,
        prepost=False,
        threads=False,
    )
    df = flatten_columns(df)
    if df is None or df.empty:
        return None
    return df


def get_price_snapshot():
    intraday = get_intraday_df()
    if intraday is not None and "Close" in intraday.columns:
        closes = intraday["Close"].dropna()
        if not closes.empty:
            last_price = float(closes.iloc[-1])

            daily_df = get_daily_df(period="5d")
            prev_close = None
            if daily_df is not None and "Close" in daily_df.columns:
                daily_closes = daily_df["Close"].dropna()
                if len(daily_closes) >= 2:
                    prev_close = float(daily_closes.iloc[-2])
                elif len(daily_closes) == 1:
                    prev_close = float(daily_closes.iloc[-1])

            return {
                "price": last_price,
                "prev_close": prev_close,
                "source": "intraday_5m",
                "intraday_df": intraday
            }

    daily_df = get_daily_df(period="5d")
    if daily_df is not None and "Close" in daily_df.columns:
        daily_closes = daily_df["Close"].dropna()
        if not daily_closes.empty:
            last_price = float(daily_closes.iloc[-1])
            prev_close = float(daily_closes.iloc[-2]) if len(daily_closes) >= 2 else None
            return {
                "price": last_price,
                "prev_close": prev_close,
                "source": "daily_close",
                "intraday_df": None
            }

    raise RuntimeError("No se pudo obtener el precio de QQQ.")


def compute_change(price, prev_close):
    if prev_close is None or prev_close == 0:
        return None, None
    chg = price - prev_close
    chg_pct = (chg / prev_close) * 100
    return chg, chg_pct


def compute_vwap(intraday_df):
    if intraday_df is None or intraday_df.empty:
        return None, None, None, None, "VWAP no disponible"

    needed = {"High", "Low", "Close", "Volume"}
    if not needed.issubset(set(intraday_df.columns)):
        return None, None, None, None, "VWAP no disponible"

    df = intraday_df.copy().dropna(subset=["High", "Low", "Close", "Volume"])
    if df.empty:
        return None, None, None, None, "VWAP no disponible"

    vol = df["Volume"].astype(float)
    if vol.sum() <= 0:
        return None, None, None, None, "VWAP no disponible"

    typical_price = (df["High"] + df["Low"] + df["Close"]) / 3.0
    df["tpv"] = typical_price * vol
    df["cum_tpv"] = df["tpv"].cumsum()
    df["cum_vol"] = vol.cumsum()
    df["vwap"] = df["cum_tpv"] / df["cum_vol"]

    vwap_value = float(df["vwap"].iloc[-1])
    current_price = float(df["Close"].iloc[-1])
    dist_pct = ((current_price - vwap_value) / vwap_value) * 100 if vwap_value else None

    residuals = df["Close"] - df["vwap"]
    sigma = float(residuals.std(ddof=0)) if len(residuals) > 1 else None
    zscore = ((current_price - vwap_value) / sigma) if sigma and sigma > 0 else None

    if dist_pct is None:
        bias = "VWAP no disponible"
    elif dist_pct >= 0.75:
        bias = "Muy por encima"
    elif dist_pct >= 0.30:
        bias = "Por encima"
    elif dist_pct <= -0.75:
        bias = "Muy por debajo"
    elif dist_pct <= -0.30:
        bias = "Por debajo"
    else:
        bias = "Cerca del VWAP"

    return (
        round(vwap_value, 2),
        round(dist_pct, 2) if dist_pct is not None else None,
        round(zscore, 2) if zscore is not None else None,
        round(sigma, 2) if sigma is not None else None,
        bias
    )


def compute_opening_range(intraday_df, now_ny, minutes=5):
    if intraday_df is None or intraday_df.empty:
        return {
            "available": False,
            "status": "Sin datos intradía",
            "message": "No hay datos para calcular Opening Range"
        }

    session_open_dt = datetime.combine(now_ny.date(), datetime.strptime("09:30", "%H:%M").time()).replace(tzinfo=NY_TZ)
    session_end_dt = session_open_dt + pd.Timedelta(minutes=minutes)

    if now_ny < session_open_dt:
        return {
            "available": False,
            "status": "Pendiente",
            "message": f"Opening Range disponible a partir de {session_open_dt.strftime('%H:%M ET')}"
        }

    if now_ny < session_end_dt:
        return {
            "available": False,
            "status": "En formación",
            "message": f"Opening Range en formación hasta {session_end_dt.strftime('%H:%M ET')}"
        }

    needed = {"Open", "High", "Low", "Close", "Volume"}
    if not needed.issubset(set(intraday_df.columns)):
        return {
            "available": False,
            "status": "No disponible",
            "message": "Columnas insuficientes para OR"
        }

    df = intraday_df.copy().dropna(subset=["Open", "High", "Low", "Close"])
    if df.empty:
        return {
            "available": False,
            "status": "No disponible",
            "message": "Sin velas válidas para OR"
        }

    try:
        if getattr(df.index, "tz", None) is not None:
            df = df.tz_convert(NY_TZ)
    except Exception:
        pass

    try:
        df = df[df.index.date == now_ny.date()]
    except Exception:
        return {
            "available": False,
            "status": "No disponible",
            "message": "No se pudo filtrar OR del día"
        }

    if df.empty:
        return {
            "available": False,
            "status": "No disponible",
            "message": "No hay velas del día para OR"
        }

    mask = (df.index >= pd.Timestamp(session_open_dt)) & (df.index < pd.Timestamp(session_end_dt))
    or_df = df.loc[mask]
    if or_df.empty:
        return {
            "available": False,
            "status": "No disponible",
            "message": "No hay velas de 09:30 para OR"
        }

    or_high = float(or_df["High"].max())
    or_low = float(or_df["Low"].min())
    or_open = float(or_df["Open"].iloc[0])
    or_close = float(or_df["Close"].iloc[-1])
    or_size = or_high - or_low
    or_mid = (or_high + or_low) / 2

    ratio = (or_size / or_open) if or_open else None
    if ratio is None:
        state = "No disponible"
    elif ratio <= 0.003:
        state = "tight"
    elif ratio <= 0.006:
        state = "normal"
    else:
        state = "wide"

    return {
        "available": True,
        "status": "OK",
        "message": "Opening Range calculado",
        "minutes": minutes,
        "open": round(or_open, 2),
        "high": round(or_high, 2),
        "low": round(or_low, 2),
        "close": round(or_close, 2),
        "mid": round(or_mid, 2),
        "size": round(or_size, 2),
        "sizePct": round(ratio * 100, 2) if ratio is not None else None,
        "state": state
    }


def compute_premarket_range(intraday_df, now_ny):
    if intraday_df is None or intraday_df.empty:
        return {
            "available": False,
            "status": "Sin datos intradía",
            "message": "No hay datos para Premarket Range"
        }

    needed = {"High", "Low", "Close"}
    if not needed.issubset(set(intraday_df.columns)):
        return {
            "available": False,
            "status": "No disponible",
            "message": "Columnas insuficientes para Premarket Range"
        }

    df = intraday_df.copy().dropna(subset=["High", "Low", "Close"])
    if df.empty:
        return {
            "available": False,
            "status": "No disponible",
            "message": "Sin velas válidas para Premarket Range"
        }

    try:
        if getattr(df.index, "tz", None) is not None:
            df = df.tz_convert(NY_TZ)
    except Exception:
        pass

    try:
        df = df[df.index.date == now_ny.date()]
    except Exception:
        return {
            "available": False,
            "status": "No disponible",
            "message": "No se pudo filtrar Premarket Range"
        }

    if df.empty:
        return {
            "available": False,
            "status": "No disponible",
            "message": "Sin velas del día"
        }

    start_dt = datetime.combine(now_ny.date(), datetime.strptime("04:00", "%H:%M").time()).replace(tzinfo=NY_TZ)
    end_dt = datetime.combine(now_ny.date(), datetime.strptime("09:30", "%H:%M").time()).replace(tzinfo=NY_TZ)

    pm_end = min(now_ny, end_dt)
    if pm_end <= start_dt:
        return {
            "available": False,
            "status": "Pendiente",
            "message": "Premarket Range disponible desde 04:00 ET"
        }

    mask = (df.index >= pd.Timestamp(start_dt)) & (df.index < pd.Timestamp(pm_end))
    pm_df = df.loc[mask]
    if pm_df.empty:
        return {
            "available": False,
            "status": "No disponible",
            "message": "Sin velas suficientes en premarket"
        }

    high = float(pm_df["High"].max())
    low = float(pm_df["Low"].min())
    close = float(pm_df["Close"].iloc[-1])
    size = high - low
    size_pct = (size / close) * 100 if close else None

    return {
        "available": True,
        "status": "OK",
        "message": "Premarket Range calculado",
        "high": round(high, 2),
        "low": round(low, 2),
        "close": round(close, 2),
        "size": round(size, 2),
        "sizePct": round(size_pct, 2) if size_pct is not None else None
    }


def compute_expected_move(price, intraday_df=None):
    def build_result_from_returns(returns, label):
        returns = returns.dropna()
        if len(returns) < 10:
            return None

        vol_daily = float(returns.tail(20).std(ddof=0))
        if vol_daily <= 0:
            return None

        move = price * vol_daily
        return {
            "method": label,
            "dailyVolPct": round(vol_daily * 100, 2),
            "move": round(move, 2),
            "movePct": round(vol_daily * 100, 2),
            "upper": round(price + move, 2),
            "lower": round(price - move, 2),
            "status": "OK"
        }

    for period in ["3mo", "1mo"]:
        try:
            daily_df = get_daily_df(period=period)
            if daily_df is not None and not daily_df.empty and "Close" in daily_df.columns:
                closes = daily_df["Close"].dropna()
                if len(closes) >= 10:
                    returns = closes.pct_change()
                    result = build_result_from_returns(returns, f"historical_vol_{period}")
                    if result:
                        return result
        except Exception:
            pass

    try:
        if intraday_df is not None and not intraday_df.empty and "Close" in intraday_df.columns:
            closes = intraday_df["Close"].dropna()
            if len(closes) >= 10:
                returns = closes.pct_change()
                result = build_result_from_returns(returns, "intraday_fallback")
                if result:
                    return result
    except Exception:
        pass

    return {
        "method": "fallback_failed",
        "dailyVolPct": None,
        "move": None,
        "movePct": None,
        "upper": None,
        "lower": None,
        "status": "No disponible"
    }


def build_trade_levels(price):
    projected_upside_price = price * (1 + MODEL_BUFFER_PCT)
    short_strike = math.ceil(projected_upside_price)
    long_strike = short_strike + SPREAD_WIDTH
    breakeven = short_strike + NET_CREDIT
    dist_to_short = short_strike - price

    return {
        "bufferPct": round(MODEL_BUFFER_PCT * 100, 2),
        "shortStrike": short_strike,
        "longStrike": long_strike,
        "breakeven": round(breakeven, 2),
        "spreadWidth": SPREAD_WIDTH,
        "netCredit": NET_CREDIT,
        "distToShort": round(dist_to_short, 2)
    }


def get_option_snapshot(price, session_code):
    try:
        ticker = yf.Ticker(TICKER)
        expirations = list(ticker.options)
        if not expirations:
            return {
                "expiration": None,
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
                "notes": "Sin expiraciones disponibles"
            }

        expiry = expirations[0]
        chain = ticker.option_chain(expiry)
        calls = chain.calls.copy()

        if calls.empty or "strike" not in calls.columns:
            return {
                "expiration": expiry,
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
                "notes": "Sin calls disponibles"
            }

        levels = build_trade_levels(price)
        short_strike = levels["shortStrike"]
        long_strike = levels["longStrike"]

        calls["strike_diff_short"] = (calls["strike"] - short_strike).abs()
        calls["strike_diff_long"] = (calls["strike"] - long_strike).abs()

        short_row = calls.sort_values("strike_diff_short").iloc[0]
        long_row = calls.sort_values("strike_diff_long").iloc[0]

        short_bid = safe_float(short_row.get("bid"))
        short_ask = safe_float(short_row.get("ask"))
        long_bid = safe_float(long_row.get("bid"))
        long_ask = safe_float(long_row.get("ask"))

        short_delta = safe_float(short_row.get("delta"))
        long_delta = safe_float(long_row.get("delta"))
        short_oi = safe_float(short_row.get("openInterest"))
        long_oi = safe_float(long_row.get("openInterest"))
        short_vol = safe_float(short_row.get("volume"))
        long_vol = safe_float(long_row.get("volume"))

        zero_quotes = (
            (short_bid in [0, 0.0, None] and short_ask in [0, 0.0, None]) or
            (long_bid in [0, 0.0, None] and long_ask in [0, 0.0, None])
        )

        quotes_usable = not zero_quotes

        if zero_quotes and session_code == "premarket":
            notes = "Premarket: quotes de opciones aún no fiables"
        elif zero_quotes:
            notes = "Quotes no útiles en este momento"
        else:
            notes = "OK"

        return {
            "expiration": expiry,
            "shortCallBid": short_bid,
            "shortCallAsk": short_ask,
            "longCallBid": long_bid,
            "longCallAsk": long_ask,
            "shortCallDelta": short_delta,
            "longCallDelta": long_delta,
            "shortCallOI": short_oi,
            "longCallOI": long_oi,
            "shortCallVolume": short_vol,
            "longCallVolume": long_vol,
            "quotesUsable": quotes_usable,
            "notes": notes
        }
    except Exception as e:
        return {
            "expiration": None,
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
            "notes": f"Options no disponibles: {str(e)}"
        }


def get_next_earnings_for_ticker(ticker_symbol):
    try:
        t = yf.Ticker(ticker_symbol)
        df = t.get_earnings_dates(limit=8)

        if df is None or df.empty:
            return {
                "ticker": ticker_symbol,
                "fecha": None,
                "dias": None,
                "momento": "No disponible",
                "status": "Vacío en Yahoo"
            }

        df = df.copy()
        if not isinstance(df.index, pd.RangeIndex):
            df = df.reset_index()

        cols_lower = {str(c).lower(): c for c in df.columns}
        date_col = None
        time_col = None

        for candidate in ["earnings date", "date", "index"]:
            if candidate in cols_lower:
                date_col = cols_lower[candidate]
                break

        for candidate in ["earnings call time", "earnings time", "time"]:
            if candidate in cols_lower:
                time_col = cols_lower[candidate]
                break

        if date_col is None:
            return {
                "ticker": ticker_symbol,
                "fecha": None,
                "dias": None,
                "momento": "No disponible",
                "status": "Columna de fecha no encontrada"
            }

        df["_fecha"] = pd.to_datetime(df[date_col], errors="coerce")
        df = df[df["_fecha"].notna()].copy()

        try:
            df["_fecha"] = df["_fecha"].dt.tz_localize(None)
        except Exception:
            pass

        today = datetime.now(NY_TZ).date()
        df["_fecha_date"] = df["_fecha"].dt.date
        df = df[df["_fecha_date"] >= today].sort_values("_fecha")

        if df.empty:
            return {
                "ticker": ticker_symbol,
                "fecha": None,
                "dias": None,
                "momento": "No disponible",
                "status": "Sin fechas futuras"
            }

        row = df.iloc[0]
        fecha = normalize_date(row["_fecha"])
        momento_raw = row[time_col] if time_col in df.columns else None

        return {
            "ticker": ticker_symbol,
            "fecha": fecha.isoformat() if fecha else None,
            "dias": days_until(fecha, today),
            "momento": translate_earnings_moment(momento_raw),
            "status": "OK"
        }
    except Exception as e:
        return {
            "ticker": ticker_symbol,
            "fecha": None,
            "dias": None,
            "momento": "No disponible",
            "status": f"Error Yahoo: {str(e)}"
        }


def get_mag7_earnings():
    rows = []
    ok_count = 0

    for tk, nombre in MAG7.items():
        item = get_next_earnings_for_ticker(tk)
        row = {
            "empresa": nombre,
            "ticker": tk,
            "fecha": item.get("fecha"),
            "dias": item.get("dias"),
            "momento": item.get("momento"),
            "status": item.get("status", "No disponible")
        }
        if row["status"] == "OK":
            ok_count += 1
        rows.append(row)

    rows.sort(key=lambda x: (
        0 if x["dias"] is not None else 1,
        9999 if x["dias"] is None else x["dias"]
    ))

    next_item = next((r for r in rows if r["status"] == "OK"), None)

    status = "OK" if ok_count > 0 else "Yahoo no devolvió earnings"
    return {
        "status": status,
        "next": next_item,
        "items": rows[:7]
    }


def get_upcoming_macro():
    now_ny = datetime.now(NY_TZ)
    rows = []

    for ev in MACRO_EVENTS:
        try:
            dt_ny = datetime.strptime(ev["datetime_ny"], "%Y-%m-%d %H:%M").replace(tzinfo=NY_TZ)
            delta_h = (dt_ny - now_ny).total_seconds() / 3600

            rows.append({
                "evento": ev["evento"],
                "impacto": ev["impacto"],
                "datetimeNY": dt_ny.strftime("%Y-%m-%d %H:%M ET"),
                "dateNY": dt_ny.strftime("%Y-%m-%d"),
                "timeNY": dt_ny.strftime("%H:%M"),
                "dias": int(delta_h // 24) if delta_h >= 0 else int(delta_h // 24),
                "horas": int(abs(delta_h) % 24),
                "totalHoras": round(delta_h, 2),
                "momento": classify_macro_moment(dt_ny),
                "status": "upcoming" if delta_h >= 0 else "past"
            })
        except Exception:
            continue

    rows.sort(key=lambda x: x["totalHoras"])

    upcoming = [r for r in rows if r["totalHoras"] >= 0]
    past = [r for r in rows if r["totalHoras"] < 0]

    recent = None
    recent_today = [r for r in past if r["dateNY"] == now_ny.strftime("%Y-%m-%d")]
    if recent_today:
        recent = sorted(recent_today, key=lambda x: x["totalHoras"], reverse=True)[0]
    elif past:
        recent = sorted(past, key=lambda x: x["totalHoras"], reverse=True)[0]

    next_item = upcoming[0] if upcoming else None

    if next_item and recent:
        status = "OK | reciente + próximo"
    elif next_item:
        status = "OK | solo próximo"
    elif recent:
        status = "OK | solo reciente"
    else:
        status = "Sin macro en la lista manual"

    return {
        "status": status,
        "next": next_item,
        "recent": recent,
        "items": upcoming[:5],
        "all": rows[:12]
    }


def build_summary(price, trade_levels, expected_move, macro_block, earnings_block, session_label, freshness, option_snapshot):
    parts = []

    if freshness.get("isStale"):
        parts.append(f"datos con {freshness.get('ageMinutes')} min de retraso")

    next_macro = macro_block.get("next")
    if next_macro:
        parts.append(f"macro {next_macro['evento']} en {next_macro['dias']}d {next_macro['horas']}h")

    em = expected_move.get("move")
    if em is not None:
        short_strike = trade_levels.get("shortStrike")
        upper = expected_move.get("upper")
        if short_strike is not None and upper is not None:
            if short_strike > upper:
                parts.append("short strike por encima del expected move")
            else:
                parts.append("short strike dentro o cerca del expected move")

    if option_snapshot.get("quotesUsable") is False:
        parts.append("quotes de opciones no fiables todavía")

    next_er = earnings_block.get("next")
    if next_er and next_er.get("dias") is not None:
        parts.append(f"próximo earnings relevante: {next_er['empresa']} en {next_er['dias']}d")

    parts.append(f"tramo actual: {session_label.lower()}")
    return " · ".join(parts[:5])


def calculate_score(change_pct, trade_levels, vwap_dist_pct, vwap_zscore, opening_range, option_snapshot, expected_move, freshness, session_code, macro_block, earnings_block):
    score = 100
    reasons = []
    alerts = []

    dist_to_short = trade_levels["distToShort"]
    if dist_to_short <= 0:
        score -= 45
        reasons.append("Precio en o sobre short strike")
    elif dist_to_short <= 1:
        score -= 30
        reasons.append("Short strike muy cerca")
    elif dist_to_short <= 2:
        score -= 18
        reasons.append("Short strike algo cerca")
    elif dist_to_short <= 3:
        score -= 8
        reasons.append("Margen limitado al short")
    else:
        reasons.append("Distancia razonable al short")

    if change_pct is not None:
        if change_pct >= 1.5:
            score -= 15
            reasons.append("Sesgo alcista fuerte hoy")
        elif change_pct >= 0.75:
            score -= 8
            reasons.append("Sesgo alcista moderado")
        elif change_pct <= -0.75:
            score += 4
            reasons.append("Sesgo bajista favorable")
        else:
            reasons.append("Cambio diario contenido")

    if vwap_dist_pct is not None:
        if vwap_dist_pct >= 1.0:
            score -= 12
            reasons.append("Muy por encima del VWAP")
        elif vwap_dist_pct >= 0.5:
            score -= 7
            reasons.append("Por encima del VWAP")
        elif vwap_dist_pct <= -0.5:
            score += 3
            reasons.append("Por debajo del VWAP")
        else:
            reasons.append("Cerca del VWAP")

    if vwap_zscore is not None:
        if abs(vwap_zscore) >= 2.0:
            score -= 10
            reasons.append("VWAP estirado a más de 2 sigma")
        elif abs(vwap_zscore) >= 1.0:
            score -= 5
            reasons.append("VWAP algo estirado")
        else:
            reasons.append("VWAP dentro de rango razonable")

    if opening_range and opening_range.get("available"):
        or_pct = opening_range.get("sizePct")
        if or_pct is not None:
            if or_pct >= 0.60:
                score -= 8
                reasons.append("Opening range amplio")
            elif or_pct <= 0.25:
                score += 3
                reasons.append("Opening range estrecho")
            else:
                reasons.append("Opening range normal")

    em_move = expected_move.get("move")
    em_upper = expected_move.get("upper")
    short_strike = trade_levels.get("shortStrike")
    if em_move is not None and em_upper is not None and short_strike is not None:
        if short_strike > em_upper:
            score += 6
            reasons.append("Short strike fuera del expected move")
        else:
            score -= 8
            reasons.append("Short strike dentro o cerca del expected move")

    bid = option_snapshot.get("shortCallBid")
    ask = option_snapshot.get("shortCallAsk")
    notes = option_snapshot.get("notes", "")
    short_delta = option_snapshot.get("shortCallDelta")
    short_oi = option_snapshot.get("shortCallOI")
    long_oi = option_snapshot.get("longCallOI")
    short_vol = option_snapshot.get("shortCallVolume")

    if option_snapshot.get("quotesUsable") is False:
        if session_code == "premarket":
            score -= 8
            reasons.append("Premarket: quotes de opciones no fiables")
        else:
            score -= 10
            reasons.append("Sin liquidez útil en options")
    elif bid is None or ask is None:
        score -= 6
        reasons.append("Sin quote válida en short call")
    else:
        mid = (bid + ask) / 2 if bid is not None and ask is not None else None
        spread_pct = ((ask - bid) / mid) * 100 if mid and mid > 0 else None
        if spread_pct is not None and spread_pct > 20:
            score -= 10
            reasons.append("Bid/ask muy amplio")
        elif spread_pct is not None and spread_pct > 12:
            score -= 5
            reasons.append("Bid/ask algo amplio")
        else:
            reasons.append("Bid/ask razonable")

    if short_delta is not None:
        abs_delta = abs(short_delta)
        if abs_delta >= 0.30:
            score -= 12
            reasons.append("Delta demasiado alta en short call")
        elif abs_delta >= 0.20:
            score -= 6
            reasons.append("Delta moderada en short call")
        else:
            score += 2
            reasons.append("Delta contenida en short call")

    if short_oi is not None and long_oi is not None:
        if short_oi >= 800 and long_oi >= 800:
            score += 4
            reasons.append("Open interest sólido en strikes")
        else:
            score -= 6
            reasons.append("Open interest flojo en strikes")

    if short_vol is not None:
        if short_vol >= 500:
            reasons.append("Volumen decente en short call")
        elif short_vol < 100:
            score -= 4
            reasons.append("Volumen bajo en short call")

    if freshness.get("isStale"):
        score -= 15
        reasons.append(f"Datos con más de {STALE_MINUTES} min")
        alerts.append(f"Datos antiguos: {freshness.get('ageMinutes')} min")

    if session_code == "premarket":
        score -= 8
        reasons.append("Premarket: menor liquidez")
    elif session_code == "apertura":
        score -= 6
        reasons.append("Apertura: más volatilidad")
    elif session_code == "media_sesion":
        score += 2
        reasons.append("Media sesión más estable")
    elif session_code == "power_hour":
        score -= 8
        reasons.append("Power hour: más agresiva")
    elif session_code == "after_hours":
        score -= 12
        reasons.append("After hours: spreads peores")
    elif session_code == "overnight":
        score -= 15
        reasons.append("Overnight: sin mercado regular")

    for ev in macro_block.get("items", [])[:3]:
        if ev["impacto"] != "alto":
            continue
        if ev["momento"] == "Durante la sesión" and ev["totalHoras"] <= 24:
            score -= 25
            reasons.append(f"Macro alta en <24h: {ev['evento']}")
            alerts.append(f"{ev['evento']} en {ev['dias']}d {ev['horas']}h")
        elif ev["momento"] == "Antes de la apertura" and ev["totalHoras"] <= 12:
            score -= 12
            reasons.append(f"Macro antes de apertura: {ev['evento']}")
            alerts.append(f"{ev['evento']} antes de la apertura")
        elif ev["totalHoras"] <= 48:
            score -= 6
            reasons.append(f"Macro próxima: {ev['evento']}")

    ok_earnings = [e for e in earnings_block.get("items", []) if e.get("status") == "OK"]

    for er in ok_earnings[:4]:
        if er["dias"] is None:
            continue
        if er["dias"] == 0:
            score -= 40
            reasons.append(f"Earnings hoy: {er['empresa']}")
            alerts.append(f"Earnings hoy de {er['empresa']}")
        elif er["dias"] == 1:
            score -= 10
            reasons.append(f"Resultados mañana: {er['empresa']}")
            alerts.append(f"Resultados mañana de {er['empresa']}")
        elif er["dias"] == 2:
            score -= 5
            reasons.append(f"Resultados en 2 días: {er['empresa']}")

    score = max(0, min(100, round(score, 2)))
    return score, reasons[:12], alerts[:8]


def make_decision(score, trade_levels, session_code, alerts, earnings_block, freshness):
    dist_to_short = trade_levels["distToShort"]

    if dist_to_short <= 0:
        return "no entraría", "red", "🔴 No entraría", "🔴 Riesgo alto"

    if freshness.get("isStale"):
        return "esperar actualización", "yellow", "🟡 Esperar actualización", "🟡 Riesgo medio"

    if any("Earnings hoy" in a for a in alerts):
        return "no entraría", "red", "🔴 No entraría", "🔴 Riesgo alto"

    next_er = earnings_block.get("next")
    if next_er and next_er.get("dias") == 0:
        return "no entraría", "red", "🔴 No entraría", "🔴 Riesgo alto"

    if session_code in ["after_hours", "overnight"]:
        if score >= 75:
            return "esperar confirmación", "yellow", "🟡 Esperar confirmación", "🟡 Riesgo medio"
        return "no entraría", "red", "🔴 No entraría", "🔴 Riesgo alto"

    if session_code == "premarket" and score >= 75:
        return "esperar confirmación", "yellow", "🟡 Esperar confirmación", "🟡 Riesgo medio"

    if score >= 78:
        return "entraría", "green", "🟢 Entraría", "🟢 Riesgo controlado"

    if score >= 52:
        return "esperar confirmación", "yellow", "🟡 Esperar confirmación", "🟡 Riesgo medio"

    return "no entraría", "red", "🔴 No entraría", "🔴 Riesgo alto"


def build_freshness(now_utc):
    age_min = 0
    return {
        "ageMinutes": age_min,
        "thresholdMinutes": STALE_MINUTES,
        "isStale": age_min > STALE_MINUTES,
        "status": "OK"
    }


def build_state():
    now_utc = datetime.now(timezone.utc)
    now_ny = now_utc.astimezone(NY_TZ)
    session_code, session_label = classify_session(now_ny)

    snapshot = get_price_snapshot()
    price = snapshot["price"]
    prev_close = snapshot["prev_close"]
    source = snapshot["source"]
    intraday_df = snapshot["intraday_df"]

    change, change_pct = compute_change(price, prev_close)
    levels = build_trade_levels(price)
    option_snapshot = get_option_snapshot(price, session_code)

    vwap_value, vwap_dist_pct, vwap_zscore, vwap_sigma, vwap_bias = compute_vwap(intraday_df)
    opening_range = compute_opening_range(intraday_df, now_ny, minutes=5)
    premarket_range = compute_premarket_range(intraday_df, now_ny)
    expected_move = compute_expected_move(price, intraday_df)

    macro_block = get_upcoming_macro()
    earnings_block = get_mag7_earnings()
    freshness = build_freshness(now_utc)

    score, reasons, alerts = calculate_score(
        change_pct,
        levels,
        vwap_dist_pct,
        vwap_zscore,
        opening_range,
        option_snapshot,
        expected_move,
        freshness,
        session_code,
        macro_block,
        earnings_block
    )
    decision, decision_tone, decision_label, risk_label = make_decision(
        score, levels, session_code, alerts, earnings_block, freshness
    )

    tone = "flat"
    if change is not None:
        tone = "up" if change > 0 else "down" if change < 0 else "flat"

    summary = build_summary(
        price,
        levels,
        expected_move,
        macro_block,
        earnings_block,
        session_label,
        freshness,
        option_snapshot
    )

    return {
        "ticker": TICKER,
        "price": round(price, 2),
        "prevClose": round(prev_close, 2) if prev_close is not None else None,
        "change": round(change, 2) if change is not None else None,
        "changePct": round(change_pct, 2) if change_pct is not None else None,
        "tone": tone,
        "source": source,
        "updatedAt": now_utc.isoformat(),
        "updatedAtText": now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "updatedAtNY": now_ny.strftime("%Y-%m-%d %H:%M:%S ET"),
        "session": {
            "code": session_code,
            "label": session_label
        },
        "summary": summary,
        "trade": levels,
        "options": option_snapshot,
        "vwap": {
            "value": vwap_value,
            "distPct": vwap_dist_pct,
            "zScore": vwap_zscore,
            "sigma": vwap_sigma,
            "bias": vwap_bias
        },
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
        "alerts": alerts,
        "macro": macro_block,
        "earnings": earnings_block
    }


def update_history(state):
    history = load_json(HISTORY_FILE, [])

    row = {
        "updatedAt": state["updatedAt"],
        "updatedAtText": state["updatedAtText"],
        "updatedAtNY": state["updatedAtNY"],
        "ticker": state["ticker"],
        "price": state["price"],
        "change": state["change"],
        "changePct": state["changePct"],
        "tone": state["tone"],
        "source": state["source"],
        "score": state["score"],
        "decisionLabel": state["decisionLabel"],
        "sessionLabel": state["session"]["label"]
    }

    if history and history[-1].get("updatedAt") == row["updatedAt"]:
        history[-1] = row
    else:
        history.append(row)

    history = history[-MAX_HISTORY_ITEMS:]
    save_json(HISTORY_FILE, history)
    return history


def main():
    ensure_dirs()
    state = build_state()
    save_json(STATE_FILE, state)
    history = update_history(state)

    print(
        f"OK | {state['ticker']} | price={state['price']} | "
        f"macro_status={state['macro']['status']} | "
        f"earnings_status={state['earnings']['status']} | "
        f"em_status={state['expectedMove']['status']} | "
        f"or_status={state['openingRange']['status']} | "
        f"score={state['score']} | history={len(history)}"
    )


if __name__ == "__main__":
    main()
