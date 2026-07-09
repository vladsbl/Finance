#!/usr/bin/env python3
"""Fetch historical prices for a set of stocks and store metrics in PostgreSQL.

Run directly:
    python ingestion/fetch_prices.py
"""

import logging
import os
import sqlite3
import sys

import numpy as np
import yfinance as yf

# --- Configuration ---------------------------------------------------------

SYMBOLS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA",
    "TSLA", "META", "JPM", "JNJ", "V",
]

# Number of trading days of history to download. 200-day MA needs at least
# 200 sessions, plus a margin for holidays/weekends.
HISTORY_PERIOD = "1y"

# Local SQLite database lives under data/marketdb.db, relative to the repo
# root (the parent of this file's directory).
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DB_PATH = os.path.join(DATA_DIR, "marketdb.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fetch_prices")


# --- Database --------------------------------------------------------------

def get_connection():
    """Open a connection to the local SQLite database.

    Creates the data/ directory on first use.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    return sqlite3.connect(DB_PATH)


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stocks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT,
    current_price REAL,
    ma_50         REAL,
    ma_200        REAL,
    volume        INTEGER,
    volatility    REAL,
    timestamp     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

INSERT_SQL = """
INSERT INTO stocks
    (symbol, current_price, ma_50, ma_200, volume, volatility)
VALUES
    (:symbol, :current_price, :ma_50, :ma_200, :volume, :volatility);
"""


def ensure_table(conn):
    conn.execute(CREATE_TABLE_SQL)
    conn.commit()


# --- Market data -----------------------------------------------------------

def _to_float(value):
    """Return a plain float or None for NaN/empty values."""
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(value) else value


def compute_metrics(symbol):
    """Download history for one symbol and compute the metrics.

    Returns a dict ready to insert, or None if data could not be fetched.
    """
    ticker = yf.Ticker(symbol)
    history = ticker.history(period=HISTORY_PERIOD, auto_adjust=False)

    if history is None or history.empty:
        logger.warning("%s: no data returned by yfinance", symbol)
        return None

    close = history["Close"].dropna()
    if close.empty:
        logger.warning("%s: no close prices available", symbol)
        return None

    current_price = close.iloc[-1]
    ma_50 = close.tail(50).mean() if len(close) >= 50 else close.mean()
    ma_200 = close.tail(200).mean() if len(close) >= 200 else close.mean()

    # Annualised volatility from the last 30 daily log returns.
    returns = np.log(close / close.shift(1)).dropna()
    window = returns.tail(30)
    volatility = window.std() * np.sqrt(252) if len(window) >= 2 else None

    volume_series = history["Volume"].dropna()
    volume = int(volume_series.iloc[-1]) if not volume_series.empty else None

    return {
        "symbol": symbol,
        "current_price": _to_float(current_price),
        "ma_50": _to_float(ma_50),
        "ma_200": _to_float(ma_200),
        "volume": volume,
        "volatility": _to_float(volatility),
    }


# --- Orchestration ---------------------------------------------------------

def main():
    logger.info("Opening SQLite database at %s ...", DB_PATH)
    try:
        conn = get_connection()
    except sqlite3.Error as exc:
        logger.error("Database connection failed: %s", exc)
        return 1

    try:
        ensure_table(conn)
    except sqlite3.Error as exc:
        logger.error("Could not create/verify 'stocks' table: %s", exc)
        conn.close()
        return 1

    fetched = []
    failed = []

    for symbol in SYMBOLS:
        try:
            metrics = compute_metrics(symbol)
        except Exception as exc:  # yfinance can raise a variety of errors
            logger.error("%s: failed to fetch data (%s)", symbol, exc)
            failed.append(symbol)
            continue

        if metrics is None:
            failed.append(symbol)
            continue

        try:
            conn.execute(INSERT_SQL, metrics)
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            logger.error("%s: failed to insert into database (%s)", symbol, exc)
            failed.append(symbol)
            continue

        fetched.append(symbol)
        logger.info(
            "%-6s price=%.2f  ma50=%.2f  ma200=%.2f  vol=%s  volatility=%.4f",
            symbol,
            metrics["current_price"] if metrics["current_price"] is not None else float("nan"),
            metrics["ma_50"] if metrics["ma_50"] is not None else float("nan"),
            metrics["ma_200"] if metrics["ma_200"] is not None else float("nan"),
            metrics["volume"],
            metrics["volatility"] if metrics["volatility"] is not None else float("nan"),
        )

    conn.close()

    logger.info("Done. Fetched %d/%d: %s", len(fetched), len(SYMBOLS),
                ", ".join(fetched) if fetched else "none")
    if failed:
        logger.warning("Failed: %s", ", ".join(failed))

    return 0 if fetched else 1


if __name__ == "__main__":
    sys.exit(main())
