#!/usr/bin/env python3
"""Ingest 2 years of daily OHLCV history for every tracked ticker.

For each ticker, downloads the last 2 years of daily bars from yfinance and
upserts them into the ``price_history`` table of data/marketdb.db. The
(ticker, date) primary key guarantees no duplicates: re-running the script
refreshes existing rows and appends new trading days.

Run directly (see README for the cron setup):
    python ingestion/ingest_prices.py
"""

import logging
import os
import sqlite3
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

DB_PATH = os.path.join(REPO_ROOT, "data", "marketdb.db")
DATA_DIR = os.path.dirname(DB_PATH)

# Configure the CA bundle before importing yfinance so curl_cffi can verify
# TLS on networks that intercept it (see ingestion/ssl_utils.py). Works whether
# run as a script or imported as ingestion.ingest_prices.
try:
    from ingestion.ssl_utils import configure_ca_bundle
except ImportError:
    from ssl_utils import configure_ca_bundle

configure_ca_bundle(DATA_DIR)

import numpy as np  # noqa: E402
import yfinance as yf  # noqa: E402

# Reuse the single source of truth for the tracked tickers.
from ingestion.fetch_prices import SYMBOLS  # noqa: E402

# Two years of daily bars is enough for a 200-day moving average with margin.
HISTORY_PERIOD = "2y"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ingest_prices")


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS price_history (
    ticker TEXT    NOT NULL,
    date   TEXT    NOT NULL,
    open   REAL,
    high   REAL,
    low    REAL,
    close  REAL,
    volume INTEGER,
    PRIMARY KEY (ticker, date)
);
"""

# Upsert: no duplicates on (ticker, date); existing rows are refreshed.
UPSERT_SQL = """
INSERT INTO price_history (ticker, date, open, high, low, close, volume)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(ticker, date) DO UPDATE SET
    open   = excluded.open,
    high   = excluded.high,
    low    = excluded.low,
    close  = excluded.close,
    volume = excluded.volume;
"""


def _to_float(value):
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(value) else value


def _to_int(value):
    f = _to_float(value)
    return int(f) if f is not None else None


def get_connection():
    os.makedirs(DATA_DIR, exist_ok=True)
    return sqlite3.connect(DB_PATH)


def fetch_history_rows(ticker):
    """Download 2y of daily bars and return rows ready for the upsert.

    Returns a list of (ticker, date, open, high, low, close, volume) tuples,
    or None if no data could be fetched.
    """
    history = yf.Ticker(ticker).history(period=HISTORY_PERIOD, auto_adjust=False)
    if history is None or history.empty:
        logger.warning("%s: no data returned by yfinance", ticker)
        return None

    history = history.reset_index()
    rows = []
    for record in history.itertuples(index=False):
        # record.Date is a pandas Timestamp; keep the calendar date only.
        date_str = record.Date.date().isoformat()
        rows.append((
            ticker,
            date_str,
            _to_float(getattr(record, "Open", None)),
            _to_float(getattr(record, "High", None)),
            _to_float(getattr(record, "Low", None)),
            _to_float(getattr(record, "Close", None)),
            _to_int(getattr(record, "Volume", None)),
        ))
    return rows


def main():
    logger.info("Opening SQLite database at %s ...", DB_PATH)
    try:
        conn = get_connection()
    except sqlite3.Error as exc:
        logger.error("Database connection failed: %s", exc)
        return 1

    try:
        conn.execute(CREATE_TABLE_SQL)
        conn.commit()
    except sqlite3.Error as exc:
        logger.error("Could not create/verify 'price_history' table: %s", exc)
        conn.close()
        return 1

    ingested = []
    failed = []
    total_rows = 0

    for ticker in SYMBOLS:
        try:
            rows = fetch_history_rows(ticker)
        except Exception as exc:  # yfinance can raise many error types
            logger.error("%s: failed to fetch history (%s)", ticker, exc)
            failed.append(ticker)
            continue

        if not rows:
            failed.append(ticker)
            continue

        try:
            conn.executemany(UPSERT_SQL, rows)
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            logger.error("%s: failed to upsert history (%s)", ticker, exc)
            failed.append(ticker)
            continue

        ingested.append(ticker)
        total_rows += len(rows)
        logger.info("%-6s upserted %d rows (%s -> %s)",
                    ticker, len(rows), rows[0][1], rows[-1][1])

    conn.close()

    logger.info("Done. %d/%d tickers, %d rows total: %s",
                len(ingested), len(SYMBOLS), total_rows,
                ", ".join(ingested) if ingested else "none")
    if failed:
        logger.warning("Failed: %s", ", ".join(failed))

    return 0 if ingested else 1


if __name__ == "__main__":
    sys.exit(main())
