#!/usr/bin/env python3
"""Module 9 v1 - Opportunity detection: pure SQLite aggregation, no LLM calls.

Combines scores ALREADY present in the database (price/valuation, technical,
news) into a single 0-100 "score_global" per ticker, with a transparent,
human-readable explanation and a confidence level based on data availability
and freshness. Spirit of reasoning/prioritize_news.py: fast, free, local.

Naming note: "price/valuation" here is what used to be loosely called
"fundamental" -- it is a momentum/volatility score, not real company
fundamentals (growth, margins, debt); see analysis/fundamental_real/ for that.

Sources (real column names, verified against the live schema before writing
this script -- see the schema inspection in the session, not assumed):
  * final_scores.price_valuation_score -- already normalised 0-100 by
    analysis/combined_score.py (price_valuation_scores.total_score, [-75,+70],
    is the RAW version; we reuse the already-normalised column to avoid
    duplicating that normalisation logic).
  * final_scores.technical_score    -- already normalised 0-100 (RSI +
    price-vs-MA50/MA200 trend, see analysis/combined_score.py).
  * news_analysis.importance (1-10) + news_analysis.tonalite
    (positive/negative/neutre), joined via news_raw.id/ticker/published_at.

Both final_scores and news_analysis currently only cover the original 10
core-pipeline tickers (AAPL, MSFT, ...), NOT the full ~1900-ticker universe:
the wider ingestion (price_history, universe) has been run, but
analysis/combined_score.py and reasoning/analyze_news.py have not (yet) been
run at that scale. Missing components are handled explicitly -- never
crashes, always yields a (possibly partial) result with a matching
confidence.

Usage:
    python reasoning/opportunity_scoring.py --priorite haute
    python reasoning/opportunity_scoring.py --priorite haute --limit 20
    python reasoning/opportunity_scoring.py --priorite toutes
"""

import argparse
import logging
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timezone
from statistics import mean

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

DB_PATH = os.path.join(REPO_ROOT, "data", "marketdb.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("opportunity_scoring")

# --- Weights: easy to adjust, must sum to 1.0 -------------------------------

POIDS = {"prix_valorisation": 0.4, "technique": 0.3, "news": 0.3}
assert abs(sum(POIDS.values()) - 1.0) < 1e-9, "POIDS must sum to 1.0"

# News scoring / freshness windows.
NEWS_LOOKBACK_DAYS = 30    # news older than this are ignored entirely
NEWS_FRESHNESS_DAYS = 7    # news within this window count as "fresh" for confidence

# Explanation thresholds (0-100 scale).
THRESH_SOLID = 60.0
THRESH_FAIBLE = 40.0


# --- Schema ------------------------------------------------------------------

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS opportunites (
    ticker                  TEXT NOT NULL,
    date_calcul             TEXT NOT NULL,
    score_global            REAL,
    score_prix_valorisation REAL,
    score_technique         REAL,
    score_news              REAL,
    explication             TEXT,
    confiance               REAL,
    PRIMARY KEY (ticker, date_calcul)
);
"""

UPSERT_SQL = """
INSERT INTO opportunites
    (ticker, date_calcul, score_global, score_prix_valorisation, score_technique,
     score_news, explication, confiance)
VALUES
    (:ticker, :date_calcul, :score_global, :score_prix_valorisation, :score_technique,
     :score_news, :explication, :confiance)
ON CONFLICT(ticker, date_calcul) DO UPDATE SET
    score_global            = excluded.score_global,
    score_prix_valorisation = excluded.score_prix_valorisation,
    score_technique         = excluded.score_technique,
    score_news              = excluded.score_news,
    explication             = excluded.explication,
    confiance               = excluded.confiance;
"""


# --- Data access ---------------------------------------------------------------

def load_tickers(conn, priorite, limit):
    """Return [(ticker, priorite), ...] from universe. Same --priorite/--limit
    contract as ingestion/ingest_universe_prices.py and ingestion/fetch_news.py."""
    if priorite == "toutes":
        sql = "SELECT ticker, priorite FROM universe ORDER BY priorite, ticker"
        params = ()
    else:
        sql = "SELECT ticker, priorite FROM universe WHERE priorite = ? ORDER BY ticker"
        params = (priorite,)
    rows = conn.execute(sql, params).fetchall()
    if limit is not None:
        rows = rows[:limit]
    return rows


def get_price_valuation_technical(conn, ticker):
    """Latest (price_valuation_score, technical_score) from final_scores, or
    (None, None) if this ticker has no row there yet."""
    row = conn.execute(
        "SELECT price_valuation_score, technical_score FROM final_scores "
        "WHERE symbol = ? ORDER BY id DESC LIMIT 1",
        (ticker,),
    ).fetchone()
    if not row:
        return None, None
    return row[0], row[1]


def get_news_rows(conn, ticker):
    """All (importance, tonalite, published_at) for a ticker's analysed news,
    most recent first."""
    return conn.execute(
        "SELECT a.importance, a.tonalite, r.published_at "
        "FROM news_analysis a JOIN news_raw r ON r.id = a.news_id "
        "WHERE r.ticker = ? ORDER BY r.published_at DESC",
        (ticker,),
    ).fetchall()


def _parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --- Scoring -------------------------------------------------------------------

def _clamp(value, lo=0.0, hi=100.0):
    return max(lo, min(hi, value))


def news_signal(conn, ticker, now=None):
    """Return (score_0_100_or_None, has_fresh_news_bool).

    Only news within NEWS_LOOKBACK_DAYS contribute to the score. Each news
    item contributes its signed importance (positive tonalite -> +importance,
    negative -> -importance, neutre -> 0); the average is linearly mapped
    from [-10, +10] to [0, 100] (0 avg -> 50, "no directional signal").
    has_fresh_news is True iff at least one qualifying news is within
    NEWS_FRESHNESS_DAYS (used for the confidence calculation, not the score).
    """
    now = now or datetime.now(timezone.utc)
    rows = get_news_rows(conn, ticker)

    qualifying = []
    has_fresh = False
    for importance, tonalite, published_at in rows:
        dt = _parse_dt(published_at)
        if dt is None:
            continue
        age_days = (now - dt).total_seconds() / 86400.0
        if age_days > NEWS_LOOKBACK_DAYS:
            continue
        qualifying.append((importance, tonalite))
        if age_days <= NEWS_FRESHNESS_DAYS:
            has_fresh = True

    if not qualifying:
        return None, False

    signed = []
    for importance, tonalite in qualifying:
        imp = importance if importance is not None else 5
        t = (tonalite or "").strip().lower()
        if t.startswith("pos"):
            signed.append(imp)
        elif t.startswith("neg"):
            signed.append(-imp)
        else:
            signed.append(0)

    avg = mean(signed)
    score = _clamp(50.0 + avg * 5.0)
    return round(score, 1), has_fresh


def compute_score_global(f_score, t_score, n_score):
    """Weighted average over whatever components are available, weights
    renormalised over the available subset. None (nothing available) if no
    component has data."""
    available = {}
    if f_score is not None:
        available["prix_valorisation"] = f_score
    if t_score is not None:
        available["technique"] = t_score
    if n_score is not None:
        available["news"] = n_score
    if not available:
        return None
    total_weight = sum(POIDS[k] for k in available)
    weighted = sum(POIDS[k] * v for k, v in available.items())
    return round(weighted / total_weight, 1)


def compute_confidence(f_score, t_score, n_score, has_fresh_news):
    """0-100. Each of the 3 components present contributes up to 1 point;
    the news point is halved if data exists but is stale (>7 days)."""
    points = 0.0
    if f_score is not None:
        points += 1.0
    if t_score is not None:
        points += 1.0
    if n_score is not None:
        points += 1.0 if has_fresh_news else 0.5
    return round((points / 3.0) * 100.0, 1)


def _component_line(label, score, pos_word, neg_word, neutral_word):
    if score is None:
        return f"○ {label} : donnee indisponible"
    if score >= THRESH_SOLID:
        return f"✓ {label} {pos_word} ({score:.0f}/100)"
    if score < THRESH_FAIBLE:
        return f"✗ {label} {neg_word} ({score:.0f}/100)"
    return f"• {label} {neutral_word} ({score:.0f}/100)"


def build_explanation(f_score, t_score, n_score, has_fresh_news):
    """Human-readable, transparent breakdown of the three components."""
    lines = [
        _component_line("Prix/Valorisation", f_score, "solide", "faible", "neutre"),
        _component_line("Momentum technique", t_score, "positif", "faible", "neutre"),
    ]
    if n_score is None:
        lines.append("○ News : aucune donnee recente disponible")
    else:
        stale_note = "" if has_fresh_news else " (donnees news perimees, >7j)"
        lines.append(
            _component_line("News recentes", n_score, "positives", "negatives", "neutres")
            + stale_note
        )
    return " | ".join(lines)


def score_ticker(conn, ticker, now=None):
    """Compute the full opportunity record for one ticker. Never raises."""
    try:
        f_score, t_score = get_price_valuation_technical(conn, ticker)
    except sqlite3.Error:
        f_score, t_score = None, None
    try:
        n_score, has_fresh_news = news_signal(conn, ticker, now)
    except sqlite3.Error:
        n_score, has_fresh_news = None, False

    score_global = compute_score_global(f_score, t_score, n_score)
    confiance = compute_confidence(f_score, t_score, n_score, has_fresh_news)
    explication = build_explanation(f_score, t_score, n_score, has_fresh_news)

    return {
        "ticker": ticker,
        "score_global": score_global,
        "score_prix_valorisation": f_score,
        "score_technique": t_score,
        "score_news": n_score,
        "explication": explication,
        "confiance": confiance,
    }


# --- Orchestration ---------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(description="Compute opportunity scores (no LLM).")
    p.add_argument("--priorite", default="toutes",
                   choices=["haute", "moyenne", "basse", "toutes"])
    p.add_argument("--limit", type=int, default=None)
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])

    if not os.path.exists(DB_PATH):
        logger.error("Database not found at %s.", DB_PATH)
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.execute(CREATE_TABLE_SQL)
    conn.commit()

    tickers = load_tickers(conn, args.priorite, args.limit)
    if not tickers:
        logger.warning("No tickers for priorite=%s. Nothing to do.", args.priorite)
        conn.close()
        return 1

    today = date.today().isoformat()
    now = datetime.now(timezone.utc)

    results = []
    for ticker, _priorite in tickers:
        record = score_ticker(conn, ticker, now)
        record["date_calcul"] = today
        results.append(record)

    try:
        conn.executemany(UPSERT_SQL, results)
        conn.commit()
    except sqlite3.Error as exc:
        logger.error("Database error while upserting opportunites: %s", exc)
        conn.close()
        return 1

    n_with_data = sum(1 for r in results if r["score_global"] is not None)
    logger.info("Priorite=%s | %d tickers traites | %d avec au moins une donnee "
                "(%d sans aucune donnee).", args.priorite, len(results),
                n_with_data, len(results) - n_with_data)

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
