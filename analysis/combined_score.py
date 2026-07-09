#!/usr/bin/env python3
"""Combine fundamental, technical, volatility and volume signals into a final
weighted score per stock.

Reads the latest snapshot of every symbol from ``stocks`` and the latest
fundamental score from ``fundamental_scores`` (both in data/marketdb.db),
computes simple technical indicators (RSI + MA50 trend), then blends
everything into a 0-100 ``final_score`` with a ``confidence`` level, stored in
``final_scores``.

Data note
---------
The ``stocks`` table stores one snapshot (price + moving averages) per
ingestion run, not a raw price time series. A textbook 14-day RSI needs ~15
successive closes, so:
  * if >= 15 snapshots exist for a symbol, a real RSI-14 is computed (numpy);
  * otherwise a documented *proxy* RSI is derived from price vs ma_50/ma_200.
The ``confidence`` column reflects this (few snapshots => low RSI reliability).

Run directly:
    python analysis/combined_score.py
"""

import logging
import os
import sqlite3
import sys

import numpy as np

# --- Configuration ---------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(REPO_ROOT, "data", "marketdb.db")

RSI_PERIOD = 14

# Weights of each component in the final score (must sum to 1.0).
W_FUNDAMENTAL = 0.40
W_TECHNICAL = 0.30
W_VOLATILITY = 0.20
W_VOLUME = 0.10

# Normalisation ranges. Fundamental total_score theoretically spans
# [-75, +70] (see analysis/fundamental/score.py). Technical raw spans
# [-25, +25] (RSI points in {-15,0,15} + trend points in {-10,0,10}).
FUND_MIN, FUND_MAX = -75.0, 70.0
TECH_MIN, TECH_MAX = -25.0, 25.0
# Annualised volatility: <= VOL_LOW is ideal (100), >= VOL_HIGH is worst (0).
VOL_LOW, VOL_HIGH = 0.10, 0.60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("combined_score")


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS final_scores (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol            TEXT,
    fundamental_score REAL,
    technical_score   REAL,
    volatility_score  REAL,
    volume_score      REAL,
    final_score       REAL,
    confidence        REAL,
    timestamp         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

INSERT_SQL = """
INSERT INTO final_scores
    (symbol, fundamental_score, technical_score, volatility_score,
     volume_score, final_score, confidence)
VALUES
    (:symbol, :fundamental_score, :technical_score, :volatility_score,
     :volume_score, :final_score, :confidence);
"""

# Latest snapshot per symbol from stocks (MAX(id) == most recent insert).
LATEST_STOCKS_SQL = """
SELECT s.symbol, s.current_price, s.ma_50, s.ma_200, s.volume, s.volatility
FROM stocks s
JOIN (SELECT symbol, MAX(id) AS max_id FROM stocks GROUP BY symbol) l
  ON s.id = l.max_id
ORDER BY s.symbol;
"""

# Full price history per symbol, oldest first (for RSI / trend when available).
PRICE_HISTORY_SQL = """
SELECT symbol, current_price
FROM stocks
WHERE current_price IS NOT NULL
ORDER BY symbol, id;
"""

# Latest fundamental total_score per symbol.
LATEST_FUNDAMENTAL_SQL = """
SELECT f.symbol, f.total_score
FROM fundamental_scores f
JOIN (SELECT symbol, MAX(id) AS max_id FROM fundamental_scores GROUP BY symbol) l
  ON f.id = l.max_id;
"""


# --- Small numeric helpers -------------------------------------------------

def _clamp01(x):
    return max(0.0, min(1.0, x))


def _norm(value, lo, hi):
    """Linear map [lo, hi] -> [0, 100], clamped."""
    return _clamp01((value - lo) / (hi - lo)) * 100.0


def _norm_inverse(value, lo, hi):
    """Linear map where lo -> 100 and hi -> 0, clamped (lower is better)."""
    return _clamp01((hi - value) / (hi - lo)) * 100.0


# --- Technical indicators --------------------------------------------------

def compute_rsi(prices, period=RSI_PERIOD):
    """Real RSI over ``period`` using numpy, or None if not enough history."""
    prices = np.asarray(prices, dtype=float)
    if prices.size < period + 1:
        return None

    deltas = np.diff(prices)
    gains = np.where(deltas > 0.0, deltas, 0.0)
    losses = np.where(deltas < 0.0, -deltas, 0.0)

    avg_gain = gains[-period:].mean()
    avg_loss = losses[-period:].mean()

    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def proxy_rsi(price, ma_50, ma_200):
    """Simplified RSI proxy when the price history is too short.

    Centres on 50 (neutral) and shifts with the price's deviation from its
    50- and 200-day averages: consistently above the averages -> overbought
    (>50), below -> oversold (<50). Uses numpy for the clamp.
    """
    deviation = 0.0
    if ma_50:
        deviation += price / ma_50 - 1.0
    if ma_200:
        deviation += price / ma_200 - 1.0
    # Scale so a ~25% combined deviation approaches the 70/30 bands.
    rsi = 50.0 + deviation * 80.0
    return float(np.clip(rsi, 0.0, 100.0))


def rsi_points(rsi):
    """Overbought (-15), oversold (+15), else neutral (0)."""
    if rsi > 70.0:
        return -15
    if rsi < 30.0:
        return 15
    return 0


def price_direction(history):
    """+1 rising, -1 falling, 0 flat/unknown, from the last two snapshots."""
    if len(history) < 2:
        return 0
    diff = history[-1] - history[-2]
    if diff > 0:
        return 1
    if diff < 0:
        return -1
    return 0


def trend_points(price, ma_50, direction):
    """price > ma_50 AND rising -> +10; price < ma_50 AND falling -> -10.

    When there is no real direction (single snapshot), fall back to the sign
    of (price - ma_50) as a proxy for the recent trajectory.
    """
    if direction == 0:
        direction = 1 if price > ma_50 else (-1 if price < ma_50 else 0)

    if price > ma_50 and direction > 0:
        return 10
    if price < ma_50 and direction < 0:
        return -10
    return 0


# --- Data loading ----------------------------------------------------------

def load_price_history(conn):
    history = {}
    for symbol, price in conn.execute(PRICE_HISTORY_SQL):
        history.setdefault(symbol, []).append(price)
    return history


def load_fundamentals(conn):
    return {symbol: total for symbol, total in conn.execute(LATEST_FUNDAMENTAL_SQL)}


# --- Orchestration ---------------------------------------------------------

def main():
    if not os.path.exists(DB_PATH):
        logger.error("Database not found at %s. Run the ingestion script first.", DB_PATH)
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
        logger.error("Could not create/verify 'final_scores' table: %s", exc)
        conn.close()
        return 1

    try:
        latest = conn.execute(LATEST_STOCKS_SQL).fetchall()
        history = load_price_history(conn)
        fundamentals = load_fundamentals(conn)
    except sqlite3.Error as exc:
        logger.error("Could not read source tables: %s", exc)
        conn.close()
        return 1

    if not latest:
        logger.warning("No data in 'stocks'. Run ingestion/fetch_prices.py first.")
        conn.close()
        return 1

    # Pass 1: compute raw component values and collect volumes for scaling.
    prelim = []
    volumes = []
    for row in latest:
        item = _analyse_row(row, history, fundamentals)
        if item is None:
            continue
        prelim.append(item)
        if item["volume"] is not None:
            volumes.append(item["volume"])

    if not prelim:
        logger.warning("No stock had enough data to score.")
        conn.close()
        return 1

    vmin = min(volumes) if volumes else None
    vmax = max(volumes) if volumes else None

    # Pass 2: normalise, weight, persist.
    results = []
    for item in prelim:
        result = _finalise(item, vmin, vmax)
        try:
            conn.execute(INSERT_SQL, result)
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            logger.error("%s: failed to insert final score (%s)", result["symbol"], exc)
            continue
        results.append(result)

    conn.close()

    _print_ranking(results)
    return 0 if results else 1


def _analyse_row(row, history, fundamentals):
    """Compute raw (un-normalised) component values for one symbol."""
    symbol = row["symbol"]
    price = row["current_price"]
    ma_50 = row["ma_50"]
    ma_200 = row["ma_200"]
    volume = row["volume"]
    volatility = row["volatility"]

    if price is None or ma_50 is None or ma_200 is None:
        logger.warning("%s: missing price/MA data - skipped", symbol)
        return None

    series = history.get(symbol, [])
    n_points = len(series)

    rsi = compute_rsi(series)
    rsi_is_real = rsi is not None
    if not rsi_is_real:
        rsi = proxy_rsi(price, ma_50, ma_200)

    direction = price_direction(series)
    technical_raw = rsi_points(rsi) + trend_points(price, ma_50, direction)

    fundamental_raw = fundamentals.get(symbol)  # may be None

    # Confidence: data completeness (50%), fundamental availability (30%),
    # RSI reliability from history length (20%).
    fields = [price, ma_50, ma_200, volume, volatility]
    fields_ok = sum(1 for f in fields if f is not None) / len(fields)
    has_fund = 1.0 if fundamental_raw is not None else 0.0
    rsi_reliability = min(1.0, max(0, n_points - 1) / RSI_PERIOD)
    confidence = 100.0 * (0.5 * fields_ok + 0.3 * has_fund + 0.2 * rsi_reliability)

    return {
        "symbol": symbol,
        "price": price,
        "ma_50": ma_50,
        "volume": volume,
        "volatility": volatility,
        "rsi": rsi,
        "rsi_is_real": rsi_is_real,
        "technical_raw": technical_raw,
        "fundamental_raw": fundamental_raw,
        "confidence": round(confidence, 1),
    }


def _finalise(item, vmin, vmax):
    """Normalise components to 0-100 and compute the weighted final score."""
    # Fundamental: neutral 50 when unavailable.
    if item["fundamental_raw"] is None:
        fundamental_score = 50.0
    else:
        fundamental_score = _norm(item["fundamental_raw"], FUND_MIN, FUND_MAX)

    technical_score = _norm(item["technical_raw"], TECH_MIN, TECH_MAX)

    if item["volatility"] is None:
        volatility_score = 50.0
    else:
        volatility_score = _norm_inverse(item["volatility"], VOL_LOW, VOL_HIGH)

    # Volume scored relative to the current batch (liquidity ranking).
    if item["volume"] is None or vmin is None or vmax is None or vmax == vmin:
        volume_score = 50.0
    else:
        volume_score = _norm(item["volume"], vmin, vmax)

    final_score = (
        W_FUNDAMENTAL * fundamental_score
        + W_TECHNICAL * technical_score
        + W_VOLATILITY * volatility_score
        + W_VOLUME * volume_score
    )

    return {
        "symbol": item["symbol"],
        "fundamental_score": round(fundamental_score, 2),
        "technical_score": round(technical_score, 2),
        "volatility_score": round(volatility_score, 2),
        "volume_score": round(volume_score, 2),
        "final_score": round(final_score, 2),
        "confidence": item["confidence"],
        "rsi": round(item["rsi"], 1),
        "rsi_is_real": item["rsi_is_real"],
    }


def _print_ranking(results):
    if not results:
        logger.warning("Nothing to rank.")
        return

    ranked = sorted(results, key=lambda r: r["final_score"], reverse=True)[:10]

    print("\n" + "=" * 92)
    print("FINAL COMBINED SCORES - TOP 10")
    print("=" * 92)
    header = (f"{'#':>2}  {'Symbol':<7}{'Fund':>8}{'Tech':>8}{'Vol.ty':>8}"
              f"{'Volume':>8}{'RSI':>7}{'FINAL':>9}{'Conf%':>8}")
    print(header)
    print("-" * 92)
    for rank, r in enumerate(ranked, start=1):
        rsi_flag = "" if r["rsi_is_real"] else "~"  # ~ marks a proxy RSI
        print(f"{rank:>2}  {r['symbol']:<7}"
              f"{r['fundamental_score']:>8.1f}{r['technical_score']:>8.1f}"
              f"{r['volatility_score']:>8.1f}{r['volume_score']:>8.1f}"
              f"{rsi_flag + str(r['rsi']):>7}{r['final_score']:>9.2f}"
              f"{r['confidence']:>8.1f}")
    print("=" * 92)
    if any(not r["rsi_is_real"] for r in ranked):
        print("~ RSI is a proxy (insufficient price history); reflected in Conf%.")
    print()
    logger.info("Scored and ranked %d stock(s).", len(results))


if __name__ == "__main__":
    sys.exit(main())
