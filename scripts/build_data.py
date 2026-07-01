import json
import math
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf


TICKER = "QQQ"
MAX_HISTORY_ITEMS = 200
SPREAD_WIDTH = 1.0
NET_CREDIT = 0.10
MODEL_BUFFER_PCT = 0.0185

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
STATE_FILE = DATA_DIR / "state.json"
HISTORY_FILE = DATA_DIR / "history.json"


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


def get_price_snapshot():
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

    if df is not None and not df.empty and "Close" in df.columns:
        closes = df["Close"].dropna()
        if not closes.empty:
            last_price = float(closes.iloc[-1])

            daily_df = yf.download(
                tickers=TICKER,
                period="5d",
                interval="1d",
                auto_adjust=True,
                progress=False,
                prepost=False,
                threads=False,
            )
            daily_df = flatten_columns(daily_df)

            prev_close = None
            if daily_df is not None and not daily_df.empty and "Close" in daily_df.columns:
                daily_closes = daily_df["Close"].dropna()
                if len(daily_closes) >= 2:
                    prev_close = float(daily_closes.iloc[-2])
                elif len(daily_closes) == 1:
                    prev_close = float(daily_closes.iloc[-1])

            return {
                "price": last_price,
                "prev_close": prev_close,
                "source": "intraday_5m"
            }

    daily_df = yf.download(
        tickers=TICKER,
        period="5d",
        interval="1d",
        auto_adjust=True,
        progress=False,
        prepost=False,
        threads=False,
    )
    daily_df = flatten_columns(daily_df)

    if daily_df is not None and not daily_df.empty and "Close" in daily_df.columns:
        daily_closes = daily_df["Close"].dropna()
        if not daily_closes.empty:
            last_price = float(daily_closes.iloc[-1])
            prev_close = float(daily_closes.iloc[-2]) if len(daily_closes) >= 2 else None
            return {
                "price": last_price,
                "prev_close": prev_close,
                "source": "daily_close"
            }

    raise RuntimeError("No se pudo obtener el precio de QQQ.")


def compute_change(price, prev_close):
    if prev_close is None or prev_close == 0:
        return None, None
    chg = price - prev_close
    chg_pct = (chg / prev_close) * 100
    return chg, chg_pct


def build_trade_levels(price):
    projected_upside_price = price * (1 + MODEL_BUFFER_PCT)
    short_strike = math.ceil(projected_upside_price)
    long_strike = short_strike + SPREAD_WIDTH
    breakeven = short_strike + NET_CREDIT

    return {
        "bufferPct": round(MODEL_BUFFER_PCT * 100, 2),
        "shortStrike": short_strike,
        "longStrike": long_strike,
        "breakeven": round(breakeven, 2),
        "spreadWidth": SPREAD_WIDTH,
        "netCredit": NET_CREDIT
    }


def get_option_snapshot(price):
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
                "notes": "Sin calls disponibles"
            }

        levels = build_trade_levels(price)
        short_strike = levels["shortStrike"]
        long_strike = levels["longStrike"]

        calls["strike_diff_short"] = (calls["strike"] - short_strike).abs()
        calls["strike_diff_long"] = (calls["strike"] - long_strike).abs()

        short_row = calls.sort_values("strike_diff_short").iloc[0]
        long_row = calls.sort_values("strike_diff_long").iloc[0]

        return {
            "expiration": expiry,
            "shortCallBid": safe_float(short_row.get("bid")),
            "shortCallAsk": safe_float(short_row.get("ask")),
            "longCallBid": safe_float(long_row.get("bid")),
            "longCallAsk": safe_float(long_row.get("ask")),
            "notes": "OK"
        }
    except Exception as e:
        return {
            "expiration": None,
            "shortCallBid": None,
            "shortCallAsk": None,
            "longCallBid": None,
            "longCallAsk": None,
            "notes": f"Options no disponibles: {str(e)}"
        }


def build_state():
    snapshot = get_price_snapshot()
    price = snapshot["price"]
    prev_close = snapshot["prev_close"]
    source = snapshot["source"]

    change, change_pct = compute_change(price, prev_close)
    levels = build_trade_levels(price)
    option_snapshot = get_option_snapshot(price)

    now_utc = datetime.now(timezone.utc)

    tone = "neutral"
    if change is not None:
        tone = "up" if change > 0 else "down" if change < 0 else "flat"

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
        "trade": levels,
        "options": option_snapshot
    }


def update_history(state):
    history = load_json(HISTORY_FILE, [])

    row = {
        "updatedAt": state["updatedAt"],
        "updatedAtText": state["updatedAtText"],
        "ticker": state["ticker"],
        "price": state["price"],
        "change": state["change"],
        "changePct": state["changePct"],
        "tone": state["tone"],
        "source": state["source"]
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
        f"changePct={state['changePct']} | "
        f"short={state['trade']['shortStrike']} long={state['trade']['longStrike']} | "
        f"history={len(history)}"
    )


if __name__ == "__main__":
    main()
