#!/usr/bin/env python3
"""Compute the PRICE/VALUATION score (valuation/momentum/volatility) for the
full ticker universe, not just the 10 core-pipeline tickers.

Background -- naming note
--------------------------
Despite the historical name "fundamental" still used for the underlying module
path (analysis/fundamental/score.py, kept as-is since it's documented in
README.md's pipeline table), this is NOT built from real fundamental data
(revenue growth, margins, debt, cash-flow, P/E, etc.) -- see
analysis/fundamental_real/ for that. It is a *price-based* score: valuation
(price vs MA50/MA200), short/long momentum (price vs MA50/MA200) and
volatility, all sourced from the `stocks` table -- which, exactly like the
technical score before it, is only ever populated by ingestion/fetch_prices.py
for the 10 pilot tickers. That narrow source table is the sole reason it has
been limited to 10 tickers; there is no hardcoded ticker list in the scoring
logic itself. The data it produces is named honestly: table
`price_valuation_scores`, column `final_scores.price_valuation_score`.

Consequence: extending it to the ~1900-ticker universe needs ZERO new API
calls and no rate limits to respect -- price_history (already populated
universe-wide by ingestion/ingest_universe_prices.py) has everything required
(close prices for MA/valuation/momentum, and to derive volatility).

This script reuses, verbatim, the exact scoring functions from
analysis/fundamental/score.py (score_valuation, score_momentum_short,
score_momentum_long, score_volatility) and analysis/combined_score.py's
normalisation (_norm, PRICE_VAL_MIN, PRICE_VAL_MAX). Only the price/MA/
volatility source changes (price_history instead of stocks).

Volatility is computed with the exact same formula as
ingestion/fetch_prices.py's compute_metrics(): annualised std-dev of the last
30 daily log returns (sqrt(252) annualisation, sample std ddof=1).

Precautions (same spirit as analysis/technical_scores_universe.py)
--------------------------------------------------------------------
* Never touches the 10 pilot tickers: they are detected via
  final_scores.final_score IS NOT NULL (a complete row only
  analysis/combined_score.py ever produces) and skipped entirely.
* Resumable: a ticker already present in `price_valuation_scores` (from the
  pilot script OR a previous, possibly interrupted, run of this script) is
  skipped too, so re-running after a crash does not recompute from scratch.
* Writes to BOTH `price_valuation_scores` (raw sub-scores, same schema/table
  as the pilot script) and `final_scores` (normalised price_valuation_score,
  which is what reasoning/opportunity_scoring.py actually reads), carrying
  forward any existing technical_score so this pass never shadows it.
* Tickers with insufficient/unusable price history are skipped, logged, never
  crash the run.

Usage:
    python analysis/price_valuation_scores_universe.py --priorite haute --limit 20
    python analysis/price_valuation_scores_universe.py --priorite haute
    python analysis/price_valuation_scores_universe.py --priorite toutes
"""

import argparse
import logging
import os
import sqlite3
import sys
import time

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from analysis.combined_score import PRICE_VAL_MAX, PRICE_VAL_MIN, _norm  # noqa: E402
from analysis.fundamental.score import (  # noqa: E402
    CREATE_TABLE_SQL as PRICE_VAL_CREATE_TABLE_SQL,
    INSERT_SQL as PRICE_VAL_INSERT_SQL,
    score_momentum_long, score_momentum_short, score_valuation, score_volatility,
)

DB_PATH = os.path.join(REPO_ROOT, "data", "marketdb.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("price_valuation_scores_universe")

# Same "enough history to be meaningful" bar as analysis/technical_scores_universe.py.
MIN_HISTORY_DAYS = 15
# Matches ingestion/fetch_prices.py's own volatility window exactly.
VOLATILITY_WINDOW = 30

FINAL_SCORES_INSERT_SQL = """
INSERT INTO final_scores
    (symbol, price_valuation_score, technical_score, volatility_score,
     volume_score, final_score, confidence)
VALUES
    (:symbol, :price_valuation_score, :technical_score, NULL, NULL, NULL, :confidence);
"""


# --- Data access -----------------------------------------------------------

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


def tickers_with_complete_score(conn, tickers):
    """The 10 pilot tickers: latest final_scores row has a non-null
    final_score (only analysis/combined_score.py ever sets one). Never
    touched by this script."""
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


def tickers_already_processed(conn, tickers):
    """Tickers that already have ANY row in price_valuation_scores -- the 10
    pilots (from analysis/fundamental/score.py) plus any ticker this script
    already scored in a previous (possibly interrupted) run. Skipping them
    is what makes the script resumable without a separate checkpoint file:
    the database itself is the checkpoint."""
    if not tickers:
        return set()
    placeholders = ",".join("?" for _ in tickers)
    rows = conn.execute(
        f"SELECT DISTINCT symbol FROM price_valuation_scores WHERE symbol IN ({placeholders})",
        list(tickers),
    ).fetchall()
    return {r[0] for r in rows}


def load_latest_technical(conn, tickers):
    """{symbol: technical_score} from each ticker's latest final_scores row
    with a non-null technical_score, if any -- carried forward so this
    price/valuation-only pass never shadows an existing technical score."""
    if not tickers:
        return {}
    placeholders = ",".join("?" for _ in tickers)
    rows = conn.execute(
        f"SELECT symbol, technical_score FROM final_scores f "
        f"WHERE symbol IN ({placeholders}) AND technical_score IS NOT NULL "
        f"AND id = (SELECT MAX(id) FROM final_scores "
        f"          WHERE symbol = f.symbol AND technical_score IS NOT NULL)",
        list(tickers),
    ).fetchall()
    return dict(rows)


def load_price_series(conn, tickers):
    """{ticker: [close, ...]} oldest-first, for this batch of tickers."""
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


# --- Scoring (reuses fundamental/score.py + combined_score.py verbatim) ----

def compute_volatility(closes):
    """Annualised volatility from up to the last 30 daily log returns.

    Exact same formula as ingestion/fetch_prices.py's compute_metrics():
    std-dev (sample, ddof=1) of log returns over a 30-day window, annualised
    by sqrt(252). None if fewer than 2 returns are available.
    """
    arr = np.asarray(closes, dtype=float)
    if arr.size < 2:
        return None
    log_returns = np.diff(np.log(arr))
    window = log_returns[-VOLATILITY_WINDOW:]
    if window.size < 2:
        return None
    return float(np.std(window, ddof=1) * np.sqrt(252))


def score_from_price_series(closes):
    """Compute the price/valuation sub-scores from a close-price series, or
    None if there isn't enough usable history. Mirrors
    analysis/fundamental/score.py's score_stock() logic exactly, just fed
    from price_history-derived values instead of the `stocks` table."""
    n_points = len(closes)
    if n_points < MIN_HISTORY_DAYS:
        return None

    price = closes[-1]
    ma_50 = sum(closes[-50:]) / len(closes[-50:]) if n_points >= 50 else sum(closes) / n_points
    ma_200 = sum(closes[-200:]) / len(closes[-200:]) if n_points >= 200 else sum(closes) / n_points
    volatility = compute_volatility(closes)

    if volatility is None:
        return None  # same gate as score_stock(): all 4 inputs required

    valuation = score_valuation(price, ma_50, ma_200)
    momentum_short = score_momentum_short(price, ma_50)
    momentum_long = score_momentum_long(price, ma_200)
    vol_score = score_volatility(volatility)
    total_score = valuation + momentum_short + momentum_long + vol_score

    rsi_like_reliability = min(1.0, max(0, n_points - 1) / MIN_HISTORY_DAYS)
    confidence = round(100.0 * rsi_like_reliability, 1)

    return {
        "valuation_score": valuation,
        "momentum_short_score": momentum_short,
        "momentum_long_score": momentum_long,
        "volatility_score": vol_score,
        "total_score": total_score,
        "confidence": confidence,
    }


# --- Orchestration ---------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Compute price/valuation scores (valuation/momentum/volatility) "
                    "for the ticker universe.")
    p.add_argument("--priorite", default="toutes",
                   choices=["haute", "moyenne", "basse", "toutes"])
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--batch-size", type=int, default=40)
    p.add_argument("--pause", type=float, default=0.5)
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])

    if not os.path.exists(DB_PATH):
        logger.error("Database not found at %s.", DB_PATH)
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.execute(PRICE_VAL_CREATE_TABLE_SQL)
    conn.commit()

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
    n_scored, n_excluded, n_skipped_pilot, n_skipped_resume = 0, 0, 0, 0

    for i, batch in enumerate(batches, start=1):
        already_complete = tickers_with_complete_score(conn, batch)
        already_processed = tickers_already_processed(conn, batch)
        series_by_ticker = load_price_series(conn, batch)
        technical = load_latest_technical(conn, batch)

        fund_rows, final_rows = [], []
        batch_pilot = batch_resume = batch_excluded = 0
        for ticker in batch:
            if ticker in already_complete:
                n_skipped_pilot += 1
                batch_pilot += 1
                continue
            if ticker in already_processed:
                n_skipped_resume += 1
                batch_resume += 1
                continue

            closes = series_by_ticker.get(ticker, [])
            result = score_from_price_series(closes)
            if result is None:
                n_excluded += 1
                batch_excluded += 1
                logger.debug("%s: historique insuffisant/inutilisable (%d jours) - exclu.",
                            ticker, len(closes))
                continue

            fund_rows.append({
                "symbol": ticker,
                "valuation_score": result["valuation_score"],
                "momentum_short_score": result["momentum_short_score"],
                "momentum_long_score": result["momentum_long_score"],
                "volatility_score": result["volatility_score"],
                "total_score": result["total_score"],
            })
            price_valuation_score = round(_norm(result["total_score"], PRICE_VAL_MIN, PRICE_VAL_MAX), 2)
            final_rows.append({
                "symbol": ticker,
                "price_valuation_score": price_valuation_score,
                "technical_score": technical.get(ticker),
                "confidence": result["confidence"],
            })

        if fund_rows:
            try:
                conn.executemany(PRICE_VAL_INSERT_SQL, fund_rows)
                conn.executemany(FINAL_SCORES_INSERT_SQL, final_rows)
                conn.commit()
            except sqlite3.Error as exc:
                conn.rollback()
                logger.error("Lot %d/%d: echec insertion (%s)", i, len(batches), exc)
                continue
            n_scored += len(fund_rows)

        logger.info(
            "Lot %d/%d traite : %d scores, %d exclus (historique), "
            "%d pilotes ignores, %d deja traites (reprise).",
            i, len(batches), len(fund_rows), batch_excluded, batch_pilot, batch_resume,
        )

        if i < len(batches):
            time.sleep(args.pause)

    elapsed = time.time() - start
    conn.close()

    logger.info("=" * 60)
    logger.info(
        "Termine en %.1fs. Scores: %d | exclus (historique insuffisant): %d | "
        "pilotes ignores (inchanges): %d | deja traites (reprise): %d | total: %d.",
        elapsed, n_scored, n_excluded, n_skipped_pilot, n_skipped_resume, len(tickers),
    )
    return 0 if (n_scored or n_skipped_pilot or n_skipped_resume) else 1


if __name__ == "__main__":
    sys.exit(main())
