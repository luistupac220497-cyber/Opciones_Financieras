import json
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf


TICKER = "QQQ"
MAX_HISTORY_ITEMS = 200

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


def get_qqq_price():
    df = yf.download(
        tickers=TICKER,
        period="1d",
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
            return float(closes.iloc[-1]), "intraday_5m"

    df = yf.download(
        tickers=TICKER,
        period="5d",
        interval="1d",
        auto_adjust=True,
        progress=False,
        prepost=False,
        threads=False,
    )

    df = flatten_columns(df)
    if df is not None and not df.empty and "Close" in df.columns:
        closes = df["Close"].dropna()
        if not closes.empty:
            return float(closes.iloc[-1]), "daily_close"

    raise RuntimeError("No se pudo obtener el precio de QQQ.")


def build_state():
    price, source = get_qqq_price()
    now_utc = datetime.now(timezone.utc)

    return {
        "ticker": TICKER,
        "price": round(price, 2),
        "source": source,
        "updatedAt": now_utc.isoformat(),
        "updatedAtText": now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
    }


def update_history(state):
    history = load_json(HISTORY_FILE, [])

    row = {
        "updatedAt": state["updatedAt"],
        "updatedAtText": state["updatedAtText"],
        "ticker": state["ticker"],
        "price": state["price"],
        "source": state["source"],
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

    print(f"OK | {state['ticker']} | {state['price']} | {state['updatedAtText']} | history={len(history)}")


if __name__ == "__main__":
    main()
