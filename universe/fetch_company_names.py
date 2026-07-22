#!/usr/bin/env python3
"""Fetch and store each universe ticker's real company name via yfinance.

Adds/populates universe.nom_entreprise: yfinance `.info["longName"]`, falling
back to `shortName`, falling back to the ticker itself -- so the column is
NEVER null and downstream consumers never need special-casing: when no real
name is found anywhere, nom_entreprise just equals the ticker, so the ticker
is shown alone (exactly the "graceful, never crash" behaviour asked for).

Note: `universe.nom` already exists and is 100% populated (scraped from the
index constituent pages -- Wikipedia / topforeignstocks -- during
universe/build_universe.py, e.g. AAPL -> "Apple Inc.", 7203.T -> "Toyota Motor
Corp."). This script is a SEPARATE, distinctly-named column
(nom_entreprise) sourced specifically from yfinance as instructed, not a
replacement for `nom` -- kept distinct rather than overwritten so both
sources remain inspectable/comparable.

Rate limits: `.info` was already called for every one of the ~1900 tickers in
analysis/fundamental_real/score.py's full-universe run with no rate-limit
issues at that scale (only genuine "quote not found" 404s for ~105 tickers
with known bad/delisted symbols) -- same batch/pause/retry defaults are reused
here for consistency and as a safety margin, not because a new limit was
observed.

Usage:
    python universe/fetch_company_names.py --priorite haute --limit 20
    python universe/fetch_company_names.py --priorite haute
    python universe/fetch_company_names.py --priorite toutes
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

# CA bundle before importing yfinance (see ingestion/ssl_utils.py).
from ingestion.ssl_utils import configure_ca_bundle  # noqa: E402

configure_ca_bundle(DATA_DIR)

import yfinance as yf  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fetch_company_names")

MAX_RETRIES = 5
BACKOFF_BASE = 2.0  # seconds: 2, 4, 8, 16, 32 -- same pattern used elsewhere


# --- Rate limit handling (same pattern as analysis/fundamental_real/score.py) -

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


# --- Fetch -------------------------------------------------------------------

def fetch_company_name(ticker):
    """longName -> shortName -> ticker itself. Never raises, never returns
    None/empty (the ticker is the ultimate fallback)."""
    try:
        info = _with_retry(lambda: yf.Ticker(ticker).info) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s: .info fetch failed (%s)", ticker, exc)
        info = {}

    long_name = (info.get("longName") or "").strip()
    if long_name:
        return long_name
    short_name = (info.get("shortName") or "").strip()
    if short_name:
        return short_name
    return ticker


# --- Data access -----------------------------------------------------------

def ensure_column(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(universe)")}
    if "nom_entreprise" not in cols:
        conn.execute("ALTER TABLE universe ADD COLUMN nom_entreprise TEXT")
        conn.commit()


def load_tickers(conn, priorite, limit):
    """Same --priorite/--limit contract as the other universe-scale scripts."""
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


# --- Orchestration ---------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Fetch real company names for the ticker universe via yfinance.")
    p.add_argument("--priorite", default="toutes",
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
    ensure_column(conn)

    tickers = load_tickers(conn, args.priorite, args.limit)
    if not tickers:
        logger.warning("No tickers for priorite=%s. Nothing to do.", args.priorite)
        conn.close()
        return 1

    batches = list(_chunks(tickers, args.batch_size))
    logger.info("Priorite=%s | %d tickers | %d lot(s) de %d | pause=%ss",
                args.priorite, len(tickers), len(batches), args.batch_size,
                args.pause)

    start = time.time()
    n_real, n_fallback_ticker = 0, 0

    for i, batch in enumerate(batches, start=1):
        batch_real = 0
        for ticker in batch:
            name = fetch_company_name(ticker)
            is_real = name != ticker
            try:
                conn.execute(
                    "UPDATE universe SET nom_entreprise = ? WHERE ticker = ?",
                    (name, ticker),
                )
                conn.commit()
            except sqlite3.Error as exc:
                conn.rollback()
                logger.error("%s: update failed (%s)", ticker, exc)
                continue
            if is_real:
                n_real += 1
                batch_real += 1
            else:
                n_fallback_ticker += 1

        logger.info("Lot %d/%d traite : %d/%d avec un nom reel trouve.",
                    i, len(batches), batch_real, len(batch))

        if i < len(batches):
            time.sleep(args.pause)

    elapsed = time.time() - start
    conn.close()

    logger.info("=" * 60)
    logger.info(
        "Termine en %.1fs (%.2fs/ticker). Nom reel trouve: %d | "
        "repli sur le ticker seul: %d | total: %d.",
        elapsed, elapsed / len(tickers), n_real, n_fallback_ticker, len(tickers),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
