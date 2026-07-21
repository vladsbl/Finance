#!/usr/bin/env python3
"""Compute a PRICE/VALUATION score for each stock in data/marketdb.db.

IMPORTANT naming note: despite this module's path/history, it does NOT use
real company fundamentals (revenue growth, margins, debt, cash-flow -- see
analysis/fundamental_real/ for that). It scores price vs moving averages and
volatility only. The module path is kept as-is (documented in README.md's
pipeline table) to avoid an unrelated file-move; the DATA it produces has been
renamed to be honest about what it is: the table is ``price_valuation_scores``
and the column downstream (in ``final_scores``) is ``price_valuation_score``.

Reads the latest snapshot of every symbol from the ``stocks`` table, scores it
on four criteria, stores the result in ``price_valuation_scores`` and prints a
report plus a top-5 ranking.

Run directly:
    python analysis/fundamental/score.py
"""

import logging
import os
import sqlite3
import sys

# --- Configuration ---------------------------------------------------------

# data/marketdb.db lives at the repo root: analysis/fundamental/score.py -> up 3.
REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
DB_PATH = os.path.join(REPO_ROOT, "data", "marketdb.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("price_valuation_score")


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS price_valuation_scores (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol               TEXT,
    valuation_score      INTEGER,
    momentum_short_score INTEGER,
    momentum_long_score  INTEGER,
    volatility_score     INTEGER,
    total_score          INTEGER,
    timestamp            TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

INSERT_SQL = """
INSERT INTO price_valuation_scores
    (symbol, valuation_score, momentum_short_score,
     momentum_long_score, volatility_score, total_score)
VALUES
    (:symbol, :valuation_score, :momentum_short_score,
     :momentum_long_score, :volatility_score, :total_score);
"""

# Latest row per symbol. id is AUTOINCREMENT, so MAX(id) == most recent insert.
LATEST_ROWS_SQL = """
SELECT s.symbol, s.current_price, s.ma_50, s.ma_200, s.volatility
FROM stocks s
JOIN (
    SELECT symbol, MAX(id) AS max_id
    FROM stocks
    GROUP BY symbol
) latest ON s.id = latest.max_id
ORDER BY s.symbol;
"""


# --- Scoring ---------------------------------------------------------------

def score_valuation(price, ma_50, ma_200):
    """Undervalued (+25) if price < ma_50 < ma_200, overvalued (-25) if the
    reverse, otherwise neutral (0)."""
    if price < ma_50 < ma_200:
        return 25
    if price > ma_50 > ma_200:
        return -25
    return 0


def score_momentum_short(price, ma_50):
    if price > ma_50:
        return 20
    if price < ma_50:
        return -20
    return 0


def score_momentum_long(price, ma_200):
    if price > ma_200:
        return 20
    if price < ma_200:
        return -20
    return 0


def score_volatility(volatility):
    if volatility > 0.40:
        return -10
    if volatility < 0.20:
        return 5
    return 0


def score_stock(row):
    """Compute all four sub-scores and the total for one stock row.

    ``row`` is a sqlite3.Row with symbol, current_price, ma_50, ma_200,
    volatility. Returns a dict, or None if any required value is missing.
    """
    price = row["current_price"]
    ma_50 = row["ma_50"]
    ma_200 = row["ma_200"]
    volatility = row["volatility"]

    if None in (price, ma_50, ma_200, volatility):
        logger.warning(
            "%s: missing data (price=%s ma_50=%s ma_200=%s volatility=%s) - skipped",
            row["symbol"], price, ma_50, ma_200, volatility,
        )
        return None

    valuation = score_valuation(price, ma_50, ma_200)
    momentum_short = score_momentum_short(price, ma_50)
    momentum_long = score_momentum_long(price, ma_200)
    vol = score_volatility(volatility)

    return {
        "symbol": row["symbol"],
        "valuation_score": valuation,
        "momentum_short_score": momentum_short,
        "momentum_long_score": momentum_long,
        "volatility_score": vol,
        "total_score": valuation + momentum_short + momentum_long + vol,
    }


# --- Orchestration ---------------------------------------------------------

def main():
    if not os.path.exists(DB_PATH):
        logger.error("Database not found at %s. Run ingestion/fetch_prices.py first.", DB_PATH)
        return 1

    logger.info("Opening SQLite database at %s ...", DB_PATH)
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        logger.error("Database connection failed: %s", exc)
        return 1

    try:
        conn.execute(CREATE_TABLE_SQL)
        conn.commit()
    except sqlite3.Error as exc:
        logger.error("Could not create/verify 'price_valuation_scores' table: %s", exc)
        conn.close()
        return 1

    try:
        rows = conn.execute(LATEST_ROWS_SQL).fetchall()
    except sqlite3.Error as exc:
        logger.error("Could not read from 'stocks' table: %s", exc)
        conn.close()
        return 1

    if not rows:
        logger.warning("No stock data found in 'stocks'. Run ingestion/fetch_prices.py first.")
        conn.close()
        return 1

    scored = []
    for row in rows:
        result = score_stock(row)
        if result is None:
            continue

        try:
            conn.execute(INSERT_SQL, result)
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            logger.error("%s: failed to insert score (%s)", result["symbol"], exc)
            continue

        scored.append(result)

    conn.close()

    _print_report(scored)
    return 0 if scored else 1


def _print_report(scored):
    """Print a per-stock breakdown and a top-5 ranking."""
    if not scored:
        logger.warning("No stock could be scored.")
        return

    print("\n" + "=" * 68)
    print("PRICE/VALUATION SCORES")
    print("=" * 68)
    print(f"{'Symbol':<8}{'Valuation':>11}{'Mom.Short':>11}"
          f"{'Mom.Long':>11}{'Volatility':>12}{'Total':>8}")
    print("-" * 68)
    for s in sorted(scored, key=lambda r: r["total_score"], reverse=True):
        print(f"{s['symbol']:<8}{s['valuation_score']:>11}"
              f"{s['momentum_short_score']:>11}{s['momentum_long_score']:>11}"
              f"{s['volatility_score']:>12}{s['total_score']:>8}")

    print("\n" + "=" * 40)
    print("TOP 5 BY SCORE")
    print("=" * 40)
    top5 = sorted(scored, key=lambda r: r["total_score"], reverse=True)[:5]
    for rank, s in enumerate(top5, start=1):
        print(f"{rank}. {s['symbol']:<8} {s['total_score']:>4} pts")
    print("=" * 40 + "\n")

    logger.info("Scored %d stock(s).", len(scored))


if __name__ == "__main__":
    sys.exit(main())
