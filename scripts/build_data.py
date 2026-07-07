import json
import math
import os
import time as time_module
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import requests

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

DATA_DIR = "data"
STATE_PATH = os.path.join(DATA_DIR, "state.json")

QQQ_OPTIONS_OPEN_ET = time(9, 30)
QQQ_OPTIONS_CLOSE_ET = time(16, 0)

EARNINGS_WATCHLIST = ["QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "SPCX"]
PRIMARY_EARNINGS_WATCHLIST = {"QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA"}
SECONDARY_EARNINGS_WATCHLIST = {"SPCX"}

MACRO_KEYWORDS = {
    "fomc": {"label": "FOMC", "impact": "alto", "veto": True},
    "fed": {"label": "Fed", "impact": "alto", "veto": True},
    "cpi": {"label": "IPC (CPI)", "impact": "alto", "veto": True},
    "consumer price index": {"label": "IPC (CPI)", "impact": "alto", "veto": True},
    "ppi": {"label": "PPI", "impact": "medio", "veto": False},
    "producer price index": {"label": "PPI", "impact": "medio", "veto": False},
    "nonfarm payrolls": {"label": "Nóminas no agrícolas (NFP)", "impact": "alto", "veto": True},
    "nfp": {"label": "Nóminas no agrícolas (NFP)", "impact": "alto", "veto": True},
    "payroll": {"label": "Nóminas no agrícolas (NFP)", "impact": "alto", "veto": True},
    "retail sales": {"label": "Ventas minoristas", "impact": "medio", "veto": False},
    "pmi": {"label": "PMI", "impact": "medio", "veto": False},
    "ism": {"label": "ISM", "impact": "medio", "veto": False},
    "jobless claims": {"label": "Peticiones de desempleo", "impact": "medio", "veto": False},
    "unemployment rate": {"label": "Tasa de desempleo", "impact": "alto", "veto": True},
    "interest rate": {"label": "Decisión de tipos", "impact": "alto", "veto": True},
    "minutes": {"label": "Actas del FOMC", "impact": "alto", "veto": False},
}


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)


def now_ny():
    return datetime.now(UTC).astimezone(NY)


def fmt_dt(dt):
    if not dt:
        return None
    return dt.astimezone(NY).strftime("%Y-%m-%d %H:%M ET")


def fmt_date(dt):
    if not dt:
        return None
    return dt.astimezone(NY).strftime("%Y-%m-%d")


def fmt_countdown(target, current):
    if not target:
        return None
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


def safe_float(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None


def clamp(value, min_value, max_value):
    return max(min_value, min(value, max_value))


def std_norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def approx_call_delta(spot, strike, iv_annual, t_years):
    if not spot or not strike or not iv_annual or iv_annual <= 0 or not t_years or t_years <= 0:
        return None
    try:
        sigma_sqrt_t = iv_annual * math.sqrt(t_years)
        if sigma_sqrt_t <= 0:
            return None
        d1 = (math.log(spot / strike) + 0.5 * (iv_annual ** 2) * t_years) / sigma_sqrt_t
        return std_norm_cdf(d1)
    except Exception:
        return None


def get_finnhub_api_key():
    api_key = os.getenv("FINNHUB_API_KEY")
    if not api_key:
        raise RuntimeError("FINNHUB_API_KEY no está configurada")
    return api_key


def load_previous_state():
    if not os.path.exists(STATE_PATH):
        return None
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def fallback_quote_from_previous(previous_state, reason_code, reason_text):
    quote = {
        "price": None,
        "change": None,
        "changePct": None,
        "prevClose": None,
        "updatedAt": None,
        "source": "finnhub_quote",
        "degraded": True,
        "degradedReasonCode": reason_code,
        "degradedReason": reason_text,
        "staleFromPreviousState": False,
    }
    if previous_state:
        quote["price"] = previous_state.get("price")
        quote["change"] = previous_state.get("change")
        quote["changePct"] = previous_state.get("changePct")
        quote["prevClose"] = previous_state.get("prevClose")
        quote["updatedAt"] = previous_state.get("updatedAt")
        quote["staleFromPreviousState"] = True
    return quote


def fetch_quote_finnhub(symbol, retries=3, sleep_seconds=2):
    previous_state = load_previous_state()
    api_key = get_finnhub_api_key()
    url = "https://finnhub.io/api/v1/quote"
    params = {"symbol": symbol, "token": api_key}

    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, params=params, timeout=15)
            if r.status_code == 429:
                if attempt < retries:
                    time_module.sleep(sleep_seconds)
                    continue
                return fallback_quote_from_previous(previous_state, "finnhub_rate_limited", "Finnhub rate limited")
            if 500 <= r.status_code <= 599:
                if attempt < retries:
                    time_module.sleep(sleep_seconds)
                    continue
                return fallback_quote_from_previous(previous_state, f"finnhub_http_{r.status_code}", f"Finnhub HTTP {r.status_code}")

            r.raise_for_status()
            data = r.json()

            updated_at = None
            if data.get("t"):
                updated_at = datetime.fromtimestamp(data["t"], tz=UTC).astimezone(NY)

            return {
                "price": safe_float(data.get("c")),
                "change": safe_float(data.get("d")),
                "changePct": safe_float(data.get("dp")),
                "prevClose": safe_float(data.get("pc")),
                "updatedAt": fmt_dt(updated_at),
                "source": "finnhub_quote",
                "degraded": False,
                "degradedReasonCode": None,
                "degradedReason": None,
                "staleFromPreviousState": False,
            }

        except requests.Timeout:
            if attempt < retries:
                time_module.sleep(sleep_seconds)
                continue
            return fallback_quote_from_previous(previous_state, "finnhub_timeout", "Timeout consultando Finnhub")
        except requests.RequestException as e:
            if attempt < retries:
                time_module.sleep(sleep_seconds)
                continue
            return fallback_quote_from_previous(previous_state, "finnhub_request_error", str(e))
        except Exception as e:
            return fallback_quote_from_previous(previous_state, "finnhub_unexpected_error", str(e))

    return fallback_quote_from_previous(previous_state, "finnhub_unknown", "Error desconocido")


def fetch_intraday_vwap(symbol, current_dt):
    try:
        api_key = get_finnhub_api_key()
        start_ny = current_dt.replace(hour=4, minute=0, second=0, microsecond=0)
        frm = int(start_ny.astimezone(UTC).timestamp())
        to = int(current_dt.astimezone(UTC).timestamp())

        url = "https://finnhub.io/api/v1/stock/candle"
        params = {
            "symbol": symbol,
            "resolution": "5",
            "from": frm,
            "to": to,
            "token": api_key,
        }
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()

        if data.get("s") != "ok":
            return {"vwap": None, "source": "finnhub_candles", "status": data.get("s", "no_data")}

        closes = data.get("c", [])
        highs = data.get("h", [])
        lows = data.get("l", [])
        volumes = data.get("v", [])

        pv_sum = 0.0
        v_sum = 0.0
        for c, h, l, v in zip(closes, highs, lows, volumes):
            if v is None or v <= 0:
                continue
            typical = (float(h) + float(l) + float(c)) / 3.0
            pv_sum += typical * float(v)
            v_sum += float(v)

        if v_sum <= 0:
            return {"vwap": None, "source": "finnhub_candles", "status": "no_volume"}

        return {"vwap": round(pv_sum / v_sum, 2), "source": "finnhub_candles", "status": "ok"}

    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        return {"vwap": None, "source": "finnhub_candles", "status": f"http_{status}"}
    except Exception:
        return {"vwap": None, "source": "finnhub_candles", "status": "error"}


def infer_session_from_time(current_dt):
    weekday = current_dt.weekday()
    if weekday >= 5:
        return {"code": "closed", "label": "Mercado cerrado"}

    hm = current_dt.time()
    if hm < time(4, 0):
        return {"code": "closed", "label": "Mercado cerrado"}
    if time(4, 0) <= hm < time(9, 30):
        return {"code": "premarket", "label": "Pre-market"}
    if time(9, 30) <= hm < time(16, 0):
        return {"code": "regular", "label": "Sesión regular"}
    if time(16, 0) <= hm < time(20, 0):
        return {"code": "afterhours", "label": "After hours"}
    return {"code": "closed", "label": "Mercado cerrado"}


def build_execution_block(current_dt, session_code):
    entry_start = current_dt.replace(hour=10, minute=30, second=0, microsecond=0)
    entry_cutoff = current_dt.replace(hour=13, minute=30, second=0, microsecond=0)
    hard_exit = current_dt.replace(hour=15, minute=15, second=0, microsecond=0)

    mins_to_start = int((entry_start - current_dt).total_seconds() // 60)
    mins_to_cutoff = int((entry_cutoff - current_dt).total_seconds() // 60)
    entry_window_open = session_code == "regular" and entry_start <= current_dt <= entry_cutoff
    time_stop_triggered = current_dt >= hard_exit
    minutes_since_start = int((current_dt - entry_start).total_seconds() // 60)

    if session_code != "regular":
        phase = "Fuera de sesión regular"
    elif current_dt < entry_start:
        phase = "Esperando primera hora"
    elif entry_window_open:
        phase = "Ventana de entrada abierta"
    elif current_dt > entry_cutoff and not time_stop_triggered:
        phase = "Fuera de ventana"
    else:
        phase = "Salida dura / no abrir"

    return {
        "phase": phase,
        "entryStartEt": "10:30 ET",
        "minsToEntryStart": mins_to_start,
        "minutesSinceEntryStart": minutes_since_start,
        "entryCutoffEt": "13:30 ET",
        "minsToCutoff": mins_to_cutoff,
        "hardExitEt": "15:15 ET",
        "timeStopTriggered": time_stop_triggered,
        "entryWindowOpen": entry_window_open,
        "maxHoldMinutes": 60,
    }


def normalize_macro_event_name(raw_name):
    if not raw_name:
        return None, None, None
    name = raw_name.strip()
    lower = name.lower()

    for key, meta in MACRO_KEYWORDS.items():
        if key in lower:
            return meta["label"], meta["impact"], meta["veto"]

    return name, "bajo", False


def fetch_macro_calendar(current_dt):
    api_key = get_finnhub_api_key()
    frm = fmt_date(current_dt - timedelta(days=1))
    to = fmt_date(current_dt + timedelta(days=21))

    try:
        url = "https://finnhub.io/api/v1/calendar/economic"
        params = {"from": frm, "to": to, "token": api_key}
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        rows = data.get("economicCalendar", []) or []

        items = []
        seen = set()

        for row in rows:
            country = (row.get("country") or "").upper()
            if country not in ("US", "USA", "UNITED STATES"):
                continue

            raw_event = row.get("event") or row.get("name") or row.get("title")
            label, impact, veto = normalize_macro_event_name(raw_event)
            if not label or impact == "bajo":
                continue

            raw_time = row.get("time")
            dt = None

            if raw_time:
                try:
                    dt = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00")).astimezone(NY)
                except Exception:
                    dt = None

            if dt is None:
                date_str = row.get("date")
                if not date_str:
                    continue
                try:
                    hhmm = str(row.get("hour") or "00:00").split(":")
                    hh = int(hhmm[0])
                    mm = int(hhmm[1]) if len(hhmm) > 1 else 0
                    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(
                        hour=hh, minute=mm, second=0, microsecond=0, tzinfo=NY
                    )
                except Exception:
                    continue

            key = (label, dt.isoformat())
            if key in seen:
                continue
            seen.add(key)

            items.append({
                "label": label,
                "impact": impact,
                "kind": "macro",
                "dateEt": fmt_dt(dt),
                "countdown": fmt_countdown(dt, current_dt),
                "veto": veto,
                "_dt": dt,
            })

        items.sort(key=lambda x: x["_dt"])

        today_high = [x for x in items if x["_dt"].date() == current_dt.date() and x["impact"] == "alto"]
        next_big = next((x for x in items if x["_dt"] > current_dt and x["impact"] == "alto"), None)

        window_critical = False
        for ev in items:
            mins = (ev["_dt"] - current_dt).total_seconds() / 60
            if ev["veto"] and -60 <= mins <= 90:
                window_critical = True
                break

        score = 0
        summary = "Sin macro alta hoy"

        if window_critical:
            score -= 25
            summary = "Ventana crítica por macro de alto impacto"
        elif today_high:
            score -= 12
            summary = f"Macro alta hoy · {today_high[0]['label']}"
        elif next_big:
            hours_to_next = (next_big["_dt"] - current_dt).total_seconds() / 3600
            if hours_to_next <= 24:
                score -= 6
            elif hours_to_next <= 72:
                score -= 2
            summary = f"Sin macro alta hoy; próximo gran evento · {next_big['label']}"

        clean_items = []
        for x in items[:12]:
            y = dict(x)
            y.pop("_dt", None)
            clean_items.append(y)

        clean_next_big = None
        if next_big:
            clean_next_big = dict(next_big)
            clean_next_big.pop("_dt", None)

        return {
            "todayHighImpact": bool(today_high),
            "windowCritical": window_critical,
            "score": score,
            "items": clean_items,
            "nextBig": clean_next_big,
            "summary": summary,
            "source": "finnhub_economic_calendar",
        }

    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status == 403:
            return {
                "todayHighImpact": False,
                "windowCritical": False,
                "score": 0,
                "items": [],
                "nextBig": None,
                "summary": "Macro no disponible por acceso/API",
                "source": "macro_unavailable",
            }
        return {
            "todayHighImpact": False,
            "windowCritical": False,
            "score": 0,
            "items": [],
            "nextBig": None,
            "summary": f"Macro no disponible (HTTP {status})",
            "source": "macro_unavailable",
        }
    except Exception:
        return {
            "todayHighImpact": False,
            "windowCritical": False,
            "score": 0,
            "items": [],
            "nextBig": None,
            "summary": "Macro no disponible",
            "source": "macro_unavailable",
        }


def get_opex_flags(current_dt):
    d = current_dt.date()
    weekday = current_dt.weekday()

    def third_friday(year, month):
        first = datetime(year, month, 1).date()
        first_friday_offset = (4 - first.weekday()) % 7
        first_friday = first + timedelta(days=first_friday_offset)
        return first_friday + timedelta(weeks=2)

    monthly = weekday == 4 and d == third_friday(d.year, d.month)
    quarterly = monthly and d.month in (3, 6, 9, 12)
    return {"opexDay": monthly, "opexQuarterly": quarterly}


def yahoo_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
    })
    return s


def fetch_yahoo_options_chain(symbol):
    s = yahoo_session()
    url = f"https://query2.finance.yahoo.com/v7/finance/options/{symbol}"
    r = s.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()
    return data["optionChain"]["result"][0]


def choose_expiration(result, current_dt):
    expirations = result.get("expirationDates", []) or []
    if not expirations:
        return None, None

    target = None
    for ts in expirations:
        dt = datetime.fromtimestamp(ts, tz=UTC).astimezone(NY)
        if dt.date() >= current_dt.date():
            target = ts
            break
    if target is None:
        target = expirations[0]

    target_dt = datetime.fromtimestamp(target, tz=UTC).astimezone(NY)
    return target, target_dt


def extract_option_rows(result, expiration_ts):
    current = None
    if result.get("expirationDates") and expiration_ts == result.get("expirationDates", [None])[0]:
        opt_list = result.get("options", [])
        if opt_list:
            current = opt_list[0]

    if current is None or current.get("expirationDate") != expiration_ts:
        s = yahoo_session()
        symbol = result["quote"]["symbol"]
        url = f"https://query2.finance.yahoo.com/v7/finance/options/{symbol}?date={expiration_ts}"
        r = s.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()
        current = data["optionChain"]["result"][0]["options"][0]
    return current


def compute_expected_move_from_chain(spot, iv, current_dt, expiration_dt):
    if not spot or not iv or not expiration_dt:
        return None, None
    end_dt = expiration_dt.replace(hour=16, minute=0, second=0, microsecond=0)
    t_days = max((end_dt - current_dt).total_seconds() / 86400.0, 0)
    t_years = t_days / 365.0
    if t_years <= 0:
        return None, None
    em = spot * iv * math.sqrt(t_years)
    return round(em, 2), round((em / spot) * 100.0, 2)


def estimate_expected_move_pct_fallback(current_dt):
    mins_to_close = max(
        (current_dt.replace(hour=16, minute=0, second=0, microsecond=0) - current_dt).total_seconds() / 60.0,
        60.0
    )
    day_fraction = mins_to_close / 390.0

    if day_fraction >= 0.85:
        return 1.10
    if day_fraction >= 0.60:
        return 0.95
    if day_fraction >= 0.35:
        return 0.80
    return 0.65


def compute_dynamic_buffer_pct(session, macro, earnings, trade, current_dt):
    expected_move_pct = trade.get("expectedMovePct")
    if expected_move_pct is None:
        expected_move_pct = estimate_expected_move_pct_fallback(current_dt)

    base_buffer = expected_move_pct * 0.55

    if session.get("code") == "premarket":
        base_buffer *= 1.10

    if macro.get("todayHighImpact"):
        base_buffer *= 1.20

    if macro.get("windowCritical"):
        base_buffer *= 1.35

    if earnings.get("qqqNext"):
        base_buffer *= 1.25
    elif earnings.get("watchlist"):
        base_buffer *= 1.12
    elif earnings.get("secondaryWatchlist"):
        base_buffer *= 1.05

    return round(clamp(base_buffer, 0.45, 2.20), 2)


def compute_dynamic_short_strike(spot_price, buffer_pct, step=1.0):
    if spot_price is None or buffer_pct is None:
        return None
    raw_strike = spot_price * (1 + buffer_pct / 100.0)
    return math.ceil(raw_strike / step) * step


def fetch_options_source(symbol, spot_price, current_dt, session_code):
    in_options_hours = session_code == "regular" and QQQ_OPTIONS_OPEN_ET <= current_dt.time() <= QQQ_OPTIONS_CLOSE_ET

    if session_code != "regular":
        return {
            "options": {
                "expiration": "market_closed",
                "expirationLabel": "Mercado cerrado",
                "quotesUsable": False,
                "liquidityOk": None,
                "liquidityLabel": "No evaluable",
                "deltaShort": None,
                "deltaTarget": 0.20,
                "bidAskSpread": None,
                "spreadPct": None,
                "openInterestShort": None,
                "openInterestLong": None,
                "spacingOk": None,
                "status": "unavailable",
                "notes": "Fuera de horario regular de opciones; no se evalúan liquidez, OI, spread ni crédito",
                "impliedVolatility": None,
            },
            "trade": {
                "bufferPct": None,
                "shortStrike": None,
                "shortStrikeMode": "estimated",
                "netCredit": None,
                "breakeven": None,
                "distanceToShort": None,
                "expectedMove": None,
                "expectedMovePct": None,
            },
            "optionsMeta": {
                "source": "yahoo_options",
                "snapshot": "no_live_chain",
                "expirationDate": None,
            }
        }

    try:
        result = fetch_yahoo_options_chain(symbol)
        expiration_ts, expiration_dt = choose_expiration(result, current_dt)
        if not expiration_ts:
            raise RuntimeError("No hay expiraciones disponibles")

        current = extract_option_rows(result, expiration_ts)
        calls = current.get("calls", []) or []
        if not calls:
            raise RuntimeError("No hay calls disponibles")

        enriched = []
        for row in calls:
            strike = safe_float(row.get("strike"))
            bid = safe_float(row.get("bid"))
            ask = safe_float(row.get("ask"))
            oi = row.get("openInterest")
            iv = safe_float(row.get("impliedVolatility"))
            last_price = safe_float(row.get("lastPrice"))

            mid = None
            if bid is not None and ask is not None and ask >= bid:
                mid = round((bid + ask) / 2.0, 2)
            elif last_price is not None:
                mid = last_price

            spread = None
            spread_pct = None
            if bid is not None and ask is not None and ask >= bid:
                spread = round(ask - bid, 2)
                if mid and mid > 0:
                    spread_pct = round(spread / mid, 4)

            t_days = max((expiration_dt.replace(hour=16, minute=0, second=0, microsecond=0) - current_dt).total_seconds() / 86400.0, 0.001)
            t_years = t_days / 365.0
            delta = approx_call_delta(spot_price, strike, iv, t_years) if (spot_price and strike and iv) else None

            enriched.append({
                "strike": strike,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "spread": spread,
                "spreadPct": spread_pct,
                "oi": oi,
                "iv": iv,
                "delta": delta,
                "symbol": row.get("contractSymbol"),
            })

        otm_calls = [x for x in enriched if x["strike"] is not None and spot_price is not None and x["strike"] >= spot_price]
        if not otm_calls:
            otm_calls = enriched

        def ranking(x):
            delta = x["delta"] if x["delta"] is not None else 9
            delta_dist = abs(delta - 0.20) if delta != 9 else 9
            oi = x["oi"] if isinstance(x["oi"], (int, float)) else -1
            spread_pct = x["spreadPct"] if x["spreadPct"] is not None else 9
            return (delta_dist, spread_pct, -oi)

        short_leg = sorted(otm_calls, key=ranking)[0]

        expected_move, expected_move_pct = compute_expected_move_from_chain(
            spot_price, short_leg.get("iv"), current_dt, expiration_dt
        )
        if expected_move is None and spot_price is not None:
            expected_move_pct = estimate_expected_move_pct_fallback(current_dt)
            expected_move = round(spot_price * expected_move_pct / 100.0, 2)

        short_strike = short_leg.get("strike")
        distance_to_short = None if spot_price is None or short_strike is None else round(short_strike - spot_price, 2)
        net_credit = short_leg.get("mid")
        breakeven = None if short_strike is None or net_credit is None else round(short_strike + net_credit, 2)

        buffer_pct = None
        if spot_price is not None and short_strike is not None:
            buffer_pct = round(((short_strike / spot_price) - 1) * 100.0, 2)

        spread_pct_value = short_leg.get("spreadPct")
        liquidity_ok = False
        if (
            short_leg.get("bid") is not None
            and short_leg.get("ask") is not None
            and short_leg.get("oi") is not None
            and short_leg.get("oi") >= 100
            and spread_pct_value is not None
            and spread_pct_value <= 0.20
        ):
            liquidity_ok = True

        quotes_usable = net_credit is not None and short_leg.get("bid") is not None and short_leg.get("ask") is not None

        return {
            "options": {
                "expiration": "nearest",
                "expirationLabel": "0DTE o vencimiento más cercano",
                "quotesUsable": quotes_usable,
                "liquidityOk": liquidity_ok,
                "liquidityLabel": "Aceptable" if liquidity_ok else "Débil",
                "deltaShort": round(short_leg["delta"], 2) if short_leg.get("delta") is not None else None,
                "deltaTarget": 0.20,
                "bidAskSpread": short_leg.get("spread"),
                "spreadPct": round(short_leg["spreadPct"] * 100.0, 2) if short_leg.get("spreadPct") is not None else None,
                "openInterestShort": short_leg.get("oi"),
                "openInterestLong": None,
                "spacingOk": True,
                "status": "live" if quotes_usable else "theoretical",
                "notes": f"Cadena obtenida desde Yahoo Finance; short seleccionado {short_leg.get('symbol') or 'N/A'}",
                "impliedVolatility": short_leg.get("iv"),
            },
            "trade": {
                "bufferPct": buffer_pct,
                "shortStrike": short_strike,
                "shortStrikeMode": "live" if quotes_usable else "estimated",
                "netCredit": net_credit if quotes_usable else None,
                "breakeven": breakeven if quotes_usable else None,
                "distanceToShort": distance_to_short,
                "expectedMove": expected_move,
                "expectedMovePct": expected_move_pct,
            },
            "optionsMeta": {
                "source": "yahoo_options",
                "snapshot": "live_chain" if quotes_usable else "partial_chain",
                "expirationDate": fmt_date(expiration_dt),
            }
        }

    except Exception as e:
        expected_move_pct = estimate_expected_move_pct_fallback(current_dt) if spot_price else None
        expected_move = round(spot_price * expected_move_pct / 100.0, 2) if spot_price and expected_move_pct else None

        return {
            "options": {
                "expiration": "nearest" if in_options_hours else "market_closed",
                "expirationLabel": "0DTE o vencimiento más cercano" if in_options_hours else "Mercado cerrado",
                "quotesUsable": False,
                "liquidityOk": False if in_options_hours else None,
                "liquidityLabel": "Débil" if in_options_hours else "No evaluable",
                "deltaShort": None,
                "deltaTarget": 0.20,
                "bidAskSpread": None,
                "spreadPct": None,
                "openInterestShort": None,
                "openInterestLong": None,
                "spacingOk": True if in_options_hours else None,
                "status": "theoretical" if in_options_hours else "unavailable",
                "notes": f"No se pudo usar cadena real ({str(e)[:140]}). Se muestra setup teórico.",
                "impliedVolatility": None,
            },
            "trade": {
                "bufferPct": None,
                "shortStrike": None,
                "shortStrikeMode": "estimated",
                "netCredit": None,
                "breakeven": None,
                "distanceToShort": None,
                "expectedMove": expected_move,
                "expectedMovePct": expected_move_pct,
            },
            "optionsMeta": {
                "source": "yahoo_options",
                "snapshot": "fallback_theoretical",
                "expirationDate": None,
            }
        }


def fetch_earnings_calendar(symbols, current_dt):
    api_key = get_finnhub_api_key()
    start_date = fmt_date(current_dt)
    end_date = fmt_date(current_dt + timedelta(days=21))

    items = []
    by_symbol = {}

    for sym in symbols:
        try:
            url = "https://finnhub.io/api/v1/calendar/earnings"
            params = {
                "from": start_date,
                "to": end_date,
                "symbol": sym,
                "token": api_key,
            }
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            data = r.json()
            earnings = data.get("earningsCalendar", []) or []

            future_rows = []
            for row in earnings:
                date_str = row.get("date")
                if not date_str:
                    continue

                dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=NY, hour=16, minute=0)
                if dt < current_dt - timedelta(days=1):
                    continue

                symbol = row.get("symbol") or sym
                weight = "primary" if symbol in PRIMARY_EARNINGS_WATCHLIST else "secondary"

                future_rows.append({
                    "symbol": symbol,
                    "date": date_str,
                    "dateEt": fmt_dt(dt),
                    "countdown": fmt_countdown(dt, current_dt),
                    "epsEstimate": row.get("epsEstimate"),
                    "hour": row.get("hour"),
                    "quarter": row.get("quarter"),
                    "year": row.get("year"),
                    "revenueEstimate": row.get("revenueEstimate"),
                    "weight": weight,
                })

            future_rows.sort(key=lambda x: x["date"])
            if future_rows:
                by_symbol[sym] = future_rows[0]
                items.extend(future_rows[:2])

        except Exception:
            continue

    items.sort(key=lambda x: (x["date"], 0 if x["weight"] == "primary" else 1))

    next_any = items[0] if items else None
    qqq_next = by_symbol.get("QQQ")
    primary_watch = [x for x in items if x["symbol"] != "QQQ" and x["weight"] == "primary"][:6]
    secondary_watch = [x for x in items if x["symbol"] != "QQQ" and x["weight"] == "secondary"][:6]
    today = [x for x in items if x["date"] == fmt_date(current_dt)]

    score = 0
    summary = "Sin earnings relevantes cercanos"

    if qqq_next:
        qqq_dt = datetime.strptime(qqq_next["date"], "%Y-%m-%d").replace(tzinfo=NY, hour=16, minute=0)
        days_to_qqq = (qqq_dt.date() - current_dt.date()).days
        if days_to_qqq <= 1:
            score -= 18
            summary = f"Earnings propios muy próximos · {qqq_next['symbol']}"
        elif days_to_qqq <= 3:
            score -= 10
            summary = f"Earnings propios próximos · {qqqNext['symbol']}"

    if not qqq_next and primary_watch:
        hw_dt = datetime.strptime(primary_watch[0]["date"], "%Y-%m-%d").replace(tzinfo=NY, hour=16, minute=0)
        days_to_hw = (hw_dt.date() - current_dt.date()).days
        if days_to_hw <= 1:
            score -= 8
            summary = f"Mega-cap earnings muy próximos · {primary_watch[0]['symbol']}"
        elif days_to_hw <= 3:
            score -= 4
            summary = f"Mega-cap earnings próximos · {primary_watch[0]['symbol']}"

    if not qqq_next and not primary_watch and secondary_watch:
        sec_dt = datetime.strptime(secondary_watch[0]["date"], "%Y-%m-%d").replace(tzinfo=NY, hour=16, minute=0)
        days_to_sec = (sec_dt.date() - current_dt.date()).days
        if days_to_sec <= 1:
            score -= 2
            summary = f"Catalyst secundario próximo · {secondary_watch[0]['symbol']}"
        elif days_to_sec <= 3:
            score -= 1
            summary = f"Catalyst secundario cercano · {secondary_watch[0]['symbol']}"

    return {
        "today": today[:10],
        "next": next_any,
        "qqqNext": qqq_next,
        "watchlist": primary_watch,
        "secondaryWatchlist": secondary_watch,
        "score": score,
        "summary": summary,
        "source": "finnhub_earnings_calendar",
    }


def score_trade_quality(state):
    options = state["options"]
    session_code = state["session"]["code"]

    if session_code != "regular" or options.get("status") == "unavailable":
        return {
            "score": 0,
            "reasons": ["Fuera de horario de opciones; métricas no evaluables aún"],
            "label": "Pendiente de apertura",
        }

    score = 0
    reasons = []

    delta_short = options.get("deltaShort")
    if delta_short is not None:
        delta_dist = abs(delta_short - 0.20)
        if delta_dist <= 0.03:
            score += 4
            reasons.append("Delta cerca del objetivo 0.20")
        elif delta_dist <= 0.07:
            score += 1
            reasons.append("Delta razonable")
        else:
            score -= 3
            reasons.append("Delta lejos del objetivo")
    else:
        score -= 3
        reasons.append("Delta real no disponible")

    credit = state["trade"].get("netCredit")
    if credit is None:
        score -= 4
        reasons.append("Crédito no disponible")
    elif credit >= 0.6:
        score += 4
        reasons.append("Crédito atractivo")
    elif credit >= 0.3:
        score += 2
        reasons.append("Crédito aceptable")
    else:
        score -= 3
        reasons.append("Crédito bajo")

    oi_short = options.get("openInterestShort")
    if oi_short is None:
        score -= 4
        reasons.append("OI no disponible")
    elif oi_short >= 500:
        score += 4
        reasons.append("OI sólido")
    elif oi_short >= 100:
        score += 2
        reasons.append("OI aceptable")
    else:
        score -= 4
        reasons.append("OI débil")

    spread_pct = options.get("spreadPct")
    if spread_pct is None:
        score -= 4
        reasons.append("Spread no disponible")
    elif spread_pct <= 10:
        score += 4
        reasons.append("Spread limpio")
    elif spread_pct <= 20:
        score += 1
        reasons.append("Spread tolerable")
    else:
        score -= 5
        reasons.append("Spread amplio")

    if options.get("quotesUsable") is False:
        score -= 6
        reasons.append("Quotes no operables")

    if options.get("liquidityOk") is False:
        score -= 4
        reasons.append("Liquidez insuficiente")
    elif options.get("liquidityOk") is True:
        score += 2
        reasons.append("Liquidez aceptable")

    if options.get("spacingOk") is True:
        score += 1
        reasons.append("Spacing correcto")

    label = "Operable" if options.get("status") == "live" and options.get("quotesUsable") else "Teórico"
    return {
        "score": score,
        "reasons": reasons,
        "label": label,
    }


def decide_trade(state):
    score = 0
    reasons = []
    alerts = []

    macro = state["macro"]
    earnings = state["earnings"]
    execution = state["execution"]
    flags = state["flags"]
    data_health = state["dataHealth"]
    tq = state["tradeQuality"]
    session_code = state["session"]["code"]

    score += macro["score"]
    score += earnings["score"]
    score += tq["score"]

    if macro["windowCritical"]:
        reasons.append("Ventana crítica por macro")
        alerts.append("Macro crítica en ventana operativa")
    elif macro["todayHighImpact"]:
        reasons.append("Hay macro alta hoy")
    elif macro["source"] == "macro_unavailable":
        reasons.append("Macro no disponible por API/plan")

    if earnings.get("qqqNext"):
        reasons.append(f"Earnings próximos propios · {earnings['qqqNext']['symbol']}")
        alerts.append(f"Earnings propios cercanos · {earnings['qqqNext']['symbol']} {earnings['qqqNext']['date']}")
    elif earnings.get("watchlist"):
        reasons.append(f"Earnings próximos mega-cap · {earnings['watchlist'][0]['symbol']}")
    elif earnings.get("secondaryWatchlist"):
        reasons.append(f"Catalyst secundario próximo · {earnings['secondaryWatchlist'][0]['symbol']}")

    if session_code == "closed":
        score -= 4
        reasons.append("Mercado cerrado")
    elif session_code == "premarket":
        score -= 3
        reasons.append("Pre-market; esperar apertura")
    elif session_code == "afterhours":
        score -= 4
        reasons.append("After hours; no abrir nuevas posiciones")

    if session_code == "regular" and not execution["entryWindowOpen"] and execution["minsToEntryStart"] > 0:
        score -= 4
        reasons.append("Aún no ha empezado la ventana de entrada")

    if execution["timeStopTriggered"]:
        score -= 12
        reasons.append("Time stop activado")

    if flags["opexQuarterly"]:
        score -= 5
        reasons.append("OPEX trimestral")
    elif flags["opexDay"]:
        score -= 2
        reasons.append("OPEX mensual")

    if data_health["spotDegraded"]:
        score -= 8
        reasons.append("Spot degradado")
        alerts.append("Dato spot degradado; lectura provisional")

    next_big = macro.get("nextBig")
    if next_big and macro["score"] < 0 and not macro["todayHighImpact"]:
        reasons.append(f"Próximo gran evento · {next_big['label']}")

    for r in tq["reasons"]:
        if r not in reasons:
            reasons.append(r)

    if session_code != "regular":
        decision_label = "esperar apertura"
        decision_tone = "blue"
        risk_label = "Riesgo medio"
    elif score <= -20:
        decision_label = "no entrar"
        decision_tone = "red"
        risk_label = "Riesgo alto"
    elif score <= -8:
        decision_label = "esperar confirmación"
        decision_tone = "yellow"
        risk_label = "Riesgo medio"
    elif score <= 8:
        decision_label = "entrar solo si setup perfecto"
        decision_tone = "blue"
        risk_label = "Riesgo medio"
    else:
        decision_label = "setup favorable"
        decision_tone = "green"
        risk_label = "Riesgo controlado"

    return {
        "score": score,
        "decisionLabel": decision_label,
        "decisionTone": decision_tone,
        "riskLabel": risk_label,
        "reasons": reasons,
        "alerts": alerts,
    }


def build_state():
    current_dt = now_ny()
    quote = fetch_quote_finnhub("QQQ")
    session = infer_session_from_time(current_dt)
    flags = get_opex_flags(current_dt)
    macro = fetch_macro_calendar(current_dt)
    earnings = fetch_earnings_calendar(EARNINGS_WATCHLIST, current_dt)
    execution = build_execution_block(current_dt, session["code"])
    vwap_block = fetch_intraday_vwap("QQQ", current_dt)
    options_bundle = fetch_options_source("QQQ", quote["price"], current_dt, session["code"])

    dynamic_buffer_pct = compute_dynamic_buffer_pct(session, macro, earnings, options_bundle["trade"], current_dt)
    options_bundle["trade"]["bufferPct"] = dynamic_buffer_pct

    if session["code"] != "regular" or not options_bundle["options"].get("quotesUsable"):
        options_bundle["trade"]["shortStrikeMode"] = "estimated"

    if options_bundle["trade"].get("shortStrike") is None and quote["price"] is not None:
        proposed_short = compute_dynamic_short_strike(quote["price"], dynamic_buffer_pct, step=1.0)
        options_bundle["trade"]["shortStrike"] = proposed_short
        options_bundle["trade"]["shortStrikeMode"] = "estimated"
        options_bundle["trade"]["distanceToShort"] = round(proposed_short - quote["price"], 2) if proposed_short is not None else None
    elif quote["price"] is not None and options_bundle["trade"].get("shortStrike") is not None:
        options_bundle["trade"]["distanceToShort"] = round(options_bundle["trade"]["shortStrike"] - quote["price"], 2)

    vwap = vwap_block.get("vwap")
    vwap_dist = None
    if quote["price"] is not None and vwap is not None:
        vwap_dist = round(quote["price"] - vwap, 2)

    state = {
        "symbol": "QQQ",
        "updatedAt": fmt_dt(current_dt),
        "generatedAtUnix": int(current_dt.timestamp()),
        "price": quote["price"],
        "change": quote["change"],
        "changePct": quote["changePct"],
        "prevClose": quote["prevClose"],
        "session": session,
        "vwap": vwap,
        "vwapDist": vwap_dist,
        "expectedMove": options_bundle["trade"].get("expectedMove"),
        "expectedMovePct": options_bundle["trade"].get("expectedMovePct"),
        "trade": options_bundle["trade"],
        "execution": execution,
        "options": options_bundle["options"],
        "optionsMeta": options_bundle["optionsMeta"],
        "macro": macro,
        "earnings": earnings,
        "flags": flags,
        "market": {
            "isHoliday": False,
            "name": None,
            "date": fmt_date(current_dt),
        },
        "dataHealth": {
            "spotSource": quote["source"],
            "optionsSource": options_bundle["optionsMeta"]["source"],
            "vwapSource": vwap_block["source"],
            "macroSource": macro["source"],
            "earningsSource": earnings["source"],
            "spotDegraded": quote["degraded"],
            "spotDegradedReason": quote["degradedReason"],
            "spotStaleFromPreviousState": quote["staleFromPreviousState"],
            "freshnessLabel": "Dato degradado" if quote["degraded"] else "Dato reciente",
            "snapshotLabel": "Snapshot previo reciclado" if quote["staleFromPreviousState"] else "Snapshot actual",
            "optionsDataStatus": options_bundle["options"]["status"],
        }
    }

    state["tradeQuality"] = score_trade_quality(state)
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
