#!/usr/bin/env python3
"""Cache each universe ticker's real GICS sector/industry via yfinance.

`universe` has no sector column today (only pays/devise/priorite) -- grouping
tickers by sector for graph/generate_relations.py's diverse-sample selection
needs real data, not a guess. This mirrors universe/fetch_company_names.py's
established pattern exactly (same retry/backoff, same batch/pause defaults,
already proven at 1900-ticker scale) but writes to its own small cache table
(ticker_sector_cache) instead of adding a column to `universe` -- sector is a
generation-time input for one specific downstream task, not a core universe
attribute the rest of the app depends on, so it stays separate.

Usage:
    python universe/fetch_sector_info.py --priorite haute --limit 20
    python universe/fetch_sector_info.py --priorite haute
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

from ingestion.ssl_utils import configure_ca_bundle  # noqa: E402

configure_ca_bundle(DATA_DIR)

import yfinance as yf  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fetch_sector_info")

MAX_RETRIES = 5
BACKOFF_BASE = 2.0  # seconds: 2, 4, 8, 16, 32 -- same pattern as fetch_company_names.py

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ticker_sector_cache (
    ticker     TEXT PRIMARY KEY,
    sector     TEXT,
    industry   TEXT,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def _is_rate_limit(exc):
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status == 429 or "429" in str(exc) or "rate limit" in str(exc).lower()


def _with_retry(fn, *args, **kwargs):
    for attempt in range(MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            if _is_rate_limit(exc) and attempt < MAX_RETRIES - 1:
                wait = BACKOFF_BASE * (2 ** attempt)
                logger.warning("Rate limit (429). Backoff %.0fs (try %d/%d)...",
                               wait, attempt + 1, MAX_RETRIES)
                time.sleep(wait)
                continue
            raise
    return None


def fetch_sector_industry(ticker):
    """(sector, industry), each None if unavailable/lookup failed -- never
    raises, never guesses."""
    try:
        info = _with_retry(lambda: yf.Ticker(ticker).info) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s: .info fetch failed (%s)", ticker, exc)
        return None, None
    sector = (info.get("sector") or "").strip() or None
    industry = (info.get("industry") or "").strip() or None
    return sector, industry


def load_tickers_needing_fetch(conn, priorite, limit):
    if priorite == "toutes":
        sql = ("SELECT u.ticker FROM universe u "
               "LEFT JOIN ticker_sector_cache c ON c.ticker = u.ticker "
               "WHERE c.ticker IS NULL ORDER BY u.priorite, u.ticker")
        params = ()
    else:
        sql = ("SELECT u.ticker FROM universe u "
               "LEFT JOIN ticker_sector_cache c ON c.ticker = u.ticker "
               "WHERE u.priorite = ? AND c.ticker IS NULL ORDER BY u.ticker")
        params = (priorite,)
    tickers = [r[0] for r in conn.execute(sql, params)]
    if limit is not None:
        tickers = tickers[:limit]
    return tickers


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Cache real sector/industry for universe tickers via yfinance.")
    p.add_argument("--priorite", default="haute",
                   choices=["haute", "moyenne", "basse", "toutes"])
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--batch-size", type=int, default=20)
    p.add_argument("--pause", type=float, default=2.0)
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])

    if not os.path.exists(DB_PATH):
        logger.error("Database not found at %s.", DB_PATH)
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.execute(CREATE_TABLE_SQL)
    conn.commit()

    tickers = load_tickers_needing_fetch(conn, args.priorite, args.limit)
    if not tickers:
        logger.info("Rien a faire : deja en cache pour priorite=%s.", args.priorite)
        conn.close()
        return 0

    batches = list(_chunks(tickers, args.batch_size))
    logger.info("Priorite=%s | %d tickers a mettre en cache | %d lot(s) de %d",
                args.priorite, len(tickers), len(batches), args.batch_size)

    n_found = 0
    for i, batch in enumerate(batches, start=1):
        for ticker in batch:
            sector, industry = fetch_sector_industry(ticker)
            conn.execute(
                "INSERT INTO ticker_sector_cache (ticker, sector, industry) "
                "VALUES (?, ?, ?) ON CONFLICT(ticker) DO UPDATE SET "
                "sector = excluded.sector, industry = excluded.industry, "
                "fetched_at = CURRENT_TIMESTAMP",
                (ticker, sector, industry),
            )
            conn.commit()
            if sector:
                n_found += 1
        logger.info("Lot %d/%d traite.", i, len(batches))
        if i < len(batches):
            time.sleep(args.pause)

    conn.close()
    logger.info("Termine. Secteur trouve pour %d/%d tickers.", n_found, len(tickers))
    return 0


if __name__ == "__main__":
    sys.exit(main())
