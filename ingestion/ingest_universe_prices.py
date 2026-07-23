#!/usr/bin/env python3
"""Batch price ingestion for the large ticker universe.

Reads tickers from the ``universe`` table (optionally filtered by priority),
downloads their daily OHLCV in BATCHES via ``yf.download`` (not one ticker at a
time), pauses between batches, and retries with exponential backoff on
errors / rate limits. Rows are upserted into the same ``price_history`` table
used by the core pipeline (idempotent on (ticker, date)).

This is built for scale but is meant to be run on SUBSETS first (via
--priorite / --limit) before ever unleashing it on all ~1900 tickers.

Usage:
    python ingestion/ingest_universe_prices.py --priorite haute --limit 50
    python ingestion/ingest_universe_prices.py --priorite moyenne --limit 50
    python ingestion/ingest_universe_prices.py --priorite toutes   # full run

Options:
    --priorite   haute | moyenne | basse | toutes   (default: toutes)
    --limit N    cap the number of tickers processed (default: no cap)
    --batch-size N   tickers per yf.download call (default: 50)
    --pause S    seconds to sleep between batches (default: 3.0)
    --period P   yfinance history period (default: 1y)
    --retries N  max attempts per batch on failure (default: 3)
"""

import argparse
import logging
import os
import sqlite3
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

DB_PATH = os.path.join(REPO_ROOT, "data", "marketdb.db")
DATA_DIR = os.path.dirname(DB_PATH)

# CA bundle before importing yfinance (see ssl_utils).
try:
    from ingestion.ssl_utils import configure_ca_bundle
except ImportError:
    from ssl_utils import configure_ca_bundle

configure_ca_bundle(DATA_DIR)

import numpy as np  # noqa: E402
import yfinance as yf  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ingest_universe")

VALID_PRIORITIES = {"haute", "moyenne", "basse"}

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

UPSERT_SQL = """
INSERT INTO price_history (ticker, date, open, high, low, close, volume)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(ticker, date) DO UPDATE SET
    open = excluded.open, high = excluded.high, low = excluded.low,
    close = excluded.close, volume = excluded.volume;
"""


def _to_float(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(v) else v


def _to_int(value):
    f = _to_float(value)
    return int(f) if f is not None else None


def load_tickers(conn, priorite, limit, explicit_tickers=None):
    """--priorite/--limit contract, plus an optional --tickers override for
    ad-hoc / targeted re-runs (e.g. after universe/fix_ticker_mapping.py
    corrects a handful of tickers)."""
    if explicit_tickers:
        return explicit_tickers
    if priorite == "toutes":
        sql = "SELECT ticker FROM universe ORDER BY priorite, ticker"
        params = ()
    else:
        sql = "SELECT ticker FROM universe WHERE priorite = ? ORDER BY ticker"
        params = (priorite,)
    tickers = [r[0] for r in conn.execute(sql, params)]
    if limit is not None:
        tickers = tickers[:limit]
    return tickers


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def download_batch(tickers, period, retries):
    """Download one batch with retry + exponential backoff. Returns a DataFrame
    (possibly multi-indexed by ticker) or None on repeated failure."""
    for attempt in range(retries):
        try:
            data = yf.download(
                tickers, period=period, interval="1d",
                group_by="ticker", auto_adjust=False,
                threads=True, progress=False,
            )
            if data is not None and not data.empty:
                return data
            logger.warning("Batch returned empty (attempt %d/%d).",
                           attempt + 1, retries)
        except Exception as exc:  # noqa: BLE001 - yfinance raises varied errors
            logger.warning("Batch error (attempt %d/%d): %s",
                           attempt + 1, retries, exc)
        if attempt < retries - 1:
            wait = 2.0 * (2 ** attempt)  # 2, 4, 8, ...
            logger.info("Backing off %.0fs before retry...", wait)
            time.sleep(wait)
    return None


def _ticker_frame(data, ticker, single):
    """Extract a single ticker's OHLCV frame from a yf.download result."""
    if single:
        return data
    try:
        if ticker in data.columns.get_level_values(0):
            return data[ticker]
    except (KeyError, AttributeError):
        return None
    return None


def store_batch(conn, data, tickers):
    """Upsert every ticker's rows from a batch. Returns (rows, tickers_ok)."""
    single = len(tickers) == 1
    total_rows, ok = 0, 0
    for ticker in tickers:
        frame = _ticker_frame(data, ticker, single)
        if frame is None or frame.empty:
            continue
        rows = []
        for idx, r in frame.dropna(how="all").iterrows():
            date_str = idx.date().isoformat() if hasattr(idx, "date") else str(idx)
            rows.append((
                ticker, date_str,
                _to_float(r.get("Open")), _to_float(r.get("High")),
                _to_float(r.get("Low")), _to_float(r.get("Close")),
                _to_int(r.get("Volume")),
            ))
        if not rows:
            continue
        conn.executemany(UPSERT_SQL, rows)
        conn.commit()
        total_rows += len(rows)
        ok += 1
    return total_rows, ok


def parse_args(argv):
    p = argparse.ArgumentParser(description="Batch universe price ingestion.")
    p.add_argument("--priorite", default="toutes",
                   choices=["haute", "moyenne", "basse", "toutes"])
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--tickers", type=str, default=None,
                   help="Comma-separated explicit ticker list, overrides "
                        "--priorite/--limit.")
    p.add_argument("--batch-size", type=int, default=50)
    p.add_argument("--pause", type=float, default=3.0)
    p.add_argument("--period", default="1y")
    p.add_argument("--retries", type=int, default=3)
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    explicit_tickers = None
    if args.tickers:
        explicit_tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(CREATE_TABLE_SQL)
        conn.commit()
    except sqlite3.Error as exc:
        logger.error("Database error: %s", exc)
        return 1

    try:
        tickers = load_tickers(conn, args.priorite, args.limit, explicit_tickers)
    except sqlite3.Error as exc:
        logger.error("Could not read universe (run universe/build_universe.py): %s", exc)
        conn.close()
        return 1

    if not tickers:
        logger.warning("No tickers for priorite=%s. Nothing to do.", args.priorite)
        conn.close()
        return 1

    batches = list(_chunks(tickers, args.batch_size))
    logger.info("Priorite=%s | %d tickers | %d batch(es) of %d | period=%s | pause=%ss",
                args.priorite, len(tickers), len(batches), args.batch_size,
                args.period, args.pause)

    start = time.time()
    total_rows, total_ok, failed_batches = 0, 0, 0

    for i, batch in enumerate(batches, start=1):
        b_start = time.time()
        data = download_batch(batch, args.period, args.retries)
        if data is None:
            logger.error("Batch %d/%d failed after retries (%d tickers).",
                         i, len(batches), len(batch))
            failed_batches += 1
        else:
            rows, ok = store_batch(conn, data, batch)
            total_rows += rows
            total_ok += ok
            logger.info("Batch %d/%d: %d/%d tickers, %d rows, %.1fs",
                        i, len(batches), ok, len(batch), rows,
                        time.time() - b_start)
        if i < len(batches):
            time.sleep(args.pause)

    elapsed = time.time() - start
    conn.close()

    logger.info("=" * 52)
    logger.info("Done in %.1fs (%.2fs/ticker). Tickers OK: %d/%d. Rows: %d. "
                "Failed batches: %d.", elapsed,
                elapsed / len(tickers) if tickers else 0.0,
                total_ok, len(tickers), total_rows, failed_batches)
    return 0 if total_ok else 1


if __name__ == "__main__":
    sys.exit(main())
