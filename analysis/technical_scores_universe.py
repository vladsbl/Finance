#!/usr/bin/env python3
"""Compute the technical score (RSI + MA50/MA200 trend) for the full ticker
universe, not just the 10 core-pipeline tickers.

Background
----------
analysis/combined_score.py computes technical_score for a ticker but sources
current_price/ma_50/ma_200 from the `stocks` table, which is only ever
populated by ingestion/fetch_prices.py for the 10 pilot tickers -- that is the
sole reason the technical score has been limited to 10 tickers so far (no
hardcoded ticker list in combined_score.py itself, just a narrow source
table). price_history, by contrast, already covers the ~1900-ticker universe
(populated by ingestion/ingest_universe_prices.py), so no new data collection
is needed here: current_price/ma_50/ma_200 are derived directly from
price_history.close instead of from `stocks`.

This script reuses the EXACT SAME indicator functions as combined_score.py
(compute_rsi, proxy_rsi, rsi_points, price_direction, trend_points, the
normalisation helper and TECH_MIN/TECH_MAX) -- nothing about the calculation
itself is reimplemented, only the ticker scope and the price/MA source.

Rows are appended to the existing `final_scores` table (symbol,
technical_score, confidence), matching the append + "latest by MAX(id)" read
convention already used by every consumer (analysis/combined_score.py's own
inserts, reasoning/opportunity_scoring.py's reads). Because that convention
means the newest row for a symbol always wins, this script CARRIES FORWARD
that symbol's latest known price_valuation_score (if any -- e.g. for the 10
pilot tickers already scored by combined_score.py) into the new row instead of
writing NULL, so a universe-wide technical pass never shadows/loses an
existing price/valuation score. volatility_score, volume_score and final_score
are left NULL (not computed here) -- opportunity_scoring.py only ever reads
price_valuation_score/technical_score from this table, so that's safe; the legacy
"Vue d'ensemble" dashboard page still only shows the 10 pilot tickers because
it INNER JOINs against `stocks`, unaffected by the extra rows added here for
tickers absent from `stocks`.

Tickers with fewer than MIN_HISTORY_DAYS closes in price_history are skipped
(logged, never crash) -- that threshold matches compute_rsi's own minimum
(RSI_PERIOD + 1), so every ticker that clears it gets a REAL RSI-14, not the
proxy. ma_50/ma_200 gracefully average whatever is available when fewer than
50/200 points exist, same degrade-gracefully pattern already used by
ingestion/fetch_prices.py for the pilot tickers.

Usage:
    python analysis/technical_scores_universe.py --priorite haute
    python analysis/technical_scores_universe.py --priorite haute --limit 50
    python analysis/technical_scores_universe.py --priorite toutes
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

from analysis.combined_score import (  # noqa: E402
    RSI_PERIOD, TECH_MAX, TECH_MIN, _norm, compute_rsi, price_direction,
    proxy_rsi, rsi_points, trend_points,
)

DB_PATH = os.path.join(REPO_ROOT, "data", "marketdb.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("technical_scores_universe")

# Minimum closes required to compute anything at all. Equal to compute_rsi's
# own cutoff (RSI_PERIOD + 1), so every included ticker gets a real RSI-14.
MIN_HISTORY_DAYS = RSI_PERIOD + 1

INSERT_SQL = """
INSERT INTO final_scores
    (symbol, price_valuation_score, technical_score, volatility_score,
     volume_score, final_score, confidence)
VALUES
    (:symbol, :price_valuation_score, :technical_score, NULL, NULL, NULL, :confidence);
"""


# --- Data access ---------------------------------------------------------------

def load_tickers(conn, priorite, limit, explicit_tickers=None):
    """Same --priorite/--limit contract as ingestion/ingest_universe_prices.py,
    plus an optional --tickers override for ad-hoc / targeted re-runs (e.g.
    after universe/fix_ticker_mapping.py corrects a handful of tickers)."""
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


def tickers_with_complete_score(conn, tickers):
    """Tickers whose LATEST final_scores row already has a non-null
    final_score -- i.e. a complete row produced by analysis/combined_score.py
    (the 10 pilot tickers). This script must never insert a new row for those:
    since consumers read the newest row per symbol, a technical-only row
    would shadow volatility_score/volume_score/final_score that combined_score
    already computed, breaking the legacy dashboard pages that expect a
    complete row for the pilots. Extending coverage, not replacing it."""
    if not tickers:
        return set()
    placeholders = ",".join("?" for _ in tickers)
    rows = conn.execute(
        f"SELECT symbol FROM final_scores f WHERE symbol IN ({placeholders}) "
        f"AND final_score IS NOT NULL "
        f"AND id = (SELECT MAX(id) FROM final_scores WHERE symbol = f.symbol)",
        list(tickers),
    ).fetchall()
    return {r[0] for r in rows}


def load_latest_price_valuations(conn, tickers):
    """Return {symbol: price_valuation_score} using each ticker's latest
    non-null price_valuation_score already in final_scores, if any (e.g. the
    10 pilot tickers scored by analysis/combined_score.py). Carried forward
    so this technical-only pass never shadows an existing price/valuation
    score."""
    if not tickers:
        return {}
    placeholders = ",".join("?" for _ in tickers)
    rows = conn.execute(
        f"SELECT symbol, price_valuation_score FROM final_scores f "
        f"WHERE symbol IN ({placeholders}) AND price_valuation_score IS NOT NULL "
        f"AND id = (SELECT MAX(id) FROM final_scores "
        f"          WHERE symbol = f.symbol AND price_valuation_score IS NOT NULL)",
        list(tickers),
    ).fetchall()
    return dict(rows)


def load_price_series(conn, tickers):
    """Return {ticker: [close, ...]} oldest-first, for this batch of tickers."""
    if not tickers:
        return {}
    placeholders = ",".join("?" for _ in tickers)
    rows = conn.execute(
        f"SELECT ticker, close FROM price_history "
        f"WHERE ticker IN ({placeholders}) AND close IS NOT NULL "
        f"ORDER BY ticker, date",
        list(tickers),
    ).fetchall()
    series = {}
    for ticker, close in rows:
        series.setdefault(ticker, []).append(close)
    return series


# --- Scoring (reuses combined_score.py's indicator functions verbatim) ---------

def score_from_price_series(closes):
    """Compute (technical_score, confidence) from a close-price series, or
    None if there isn't enough history. Mirrors combined_score.py's
    _analyse_row/_finalise technical-component logic exactly."""
    n_points = len(closes)
    if n_points < MIN_HISTORY_DAYS:
        return None

    price = closes[-1]
    ma_50 = sum(closes[-50:]) / len(closes[-50:]) if n_points >= 50 else sum(closes) / n_points
    ma_200 = sum(closes[-200:]) / len(closes[-200:]) if n_points >= 200 else sum(closes) / n_points

    rsi = compute_rsi(closes)
    rsi_is_real = rsi is not None
    if not rsi_is_real:
        rsi = proxy_rsi(price, ma_50, ma_200)

    direction = price_direction(closes)
    technical_raw = rsi_points(rsi) + trend_points(price, ma_50, direction)
    technical_score = round(_norm(technical_raw, TECH_MIN, TECH_MAX), 2)

    rsi_reliability = min(1.0, max(0, n_points - 1) / RSI_PERIOD)
    confidence = round(100.0 * rsi_reliability, 1)

    return technical_score, confidence


# --- Orchestration ---------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Compute technical scores (RSI + MA trend) for the ticker universe.")
    p.add_argument("--priorite", default="toutes",
                   choices=["haute", "moyenne", "basse", "toutes"])
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--tickers", type=str, default=None,
                   help="Comma-separated explicit ticker list, overrides "
                        "--priorite/--limit.")
    p.add_argument("--batch-size", type=int, default=40)
    p.add_argument("--pause", type=float, default=0.5)
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    explicit_tickers = None
    if args.tickers:
        explicit_tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]

    if not os.path.exists(DB_PATH):
        logger.error("Database not found at %s.", DB_PATH)
        return 1

    conn = sqlite3.connect(DB_PATH)

    tickers = load_tickers(conn, args.priorite, args.limit, explicit_tickers)
    if not tickers:
        logger.warning("No tickers for priorite=%s. Nothing to do.", args.priorite)
        conn.close()
        return 1

    batches = list(_chunks(tickers, args.batch_size))
    logger.info("Priorite=%s | %d tickers | %d lot(s) de %d | pause=%ss",
                args.priorite, len(tickers), len(batches), args.batch_size,
                args.pause)

    start = time.time()
    n_scored, n_excluded, n_skipped_complete = 0, 0, 0

    for i, batch in enumerate(batches, start=1):
        already_complete = tickers_with_complete_score(conn, batch)
        series_by_ticker = load_price_series(conn, batch)
        price_valuations = load_latest_price_valuations(conn, batch)
        rows_to_insert = []
        for ticker in batch:
            if ticker in already_complete:
                n_skipped_complete += 1
                continue
            closes = series_by_ticker.get(ticker, [])
            result = score_from_price_series(closes)
            if result is None:
                n_excluded += 1
                logger.debug("%s: historique insuffisant (%d jours < %d) - exclu.",
                            ticker, len(closes), MIN_HISTORY_DAYS)
                continue
            technical_score, confidence = result
            rows_to_insert.append({
                "symbol": ticker,
                "technical_score": technical_score,
                "confidence": confidence,
                "price_valuation_score": price_valuations.get(ticker),
            })

        if rows_to_insert:
            try:
                conn.executemany(INSERT_SQL, rows_to_insert)
                conn.commit()
            except sqlite3.Error as exc:
                conn.rollback()
                logger.error("Lot %d/%d: echec insertion (%s)", i, len(batches), exc)
                continue
            n_scored += len(rows_to_insert)

        logger.info("Lot %d/%d traite : %d scores, %d exclus (historique insuffisant), "
                    "%d deja couverts par combined_score.py.",
                    i, len(batches), len(rows_to_insert),
                    len(batch) - len(rows_to_insert) - sum(1 for t in batch if t in already_complete),
                    sum(1 for t in batch if t in already_complete))

        if i < len(batches):
            time.sleep(args.pause)

    elapsed = time.time() - start
    conn.close()

    logger.info("=" * 60)
    logger.info("Termine en %.1fs. Tickers scores: %d | exclus (historique "
                "insuffisant ou absent): %d | deja couverts par "
                "combined_score.py (inchanges): %d | total traite: %d.",
                elapsed, n_scored, n_excluded, n_skipped_complete, len(tickers))
    return 0 if (n_scored or n_skipped_complete) else 1


if __name__ == "__main__":
    sys.exit(main())
