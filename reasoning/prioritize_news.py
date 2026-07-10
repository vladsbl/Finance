#!/usr/bin/env python3
"""Score not-yet-analysed news by importance, WITHOUT calling any LLM.

The Groq free tier (~1000 calls/day) is far smaller than the volume of news
collected once ingestion runs across the full ~1900-ticker universe (tens of
thousands of raw rows). This module computes a cheap, local 0-100 priority
score for every unanalysed row in ``news_raw`` so ``reasoning/analyze_news.py``
can spend its daily quota on the news that matter most instead of an arbitrary
(ticker, id) order.

Score components (weighted sum, see WEIGHTS):
  * ticker priority (haute/moyenne/basse from `universe`) -- strongest weight
  * recent price move (% change over the last few trading days, price_history)
  * recent volume anomaly (latest day's volume vs its trailing average)
  * high-impact keywords in the title (FR + EN, since sources are mixed)
  * freshness (last 24h favoured over older news)
  * a flat bonus if the same story appears on both Yahoo RSS and Finnhub
    (matched by normalised title, per ticker)

Usage:
    python reasoning/prioritize_news.py --dry-run            # show top 50
    python reasoning/prioritize_news.py --dry-run --top 20   # show top 20
    python reasoning/prioritize_news.py --dry-run --priorite haute
"""

import argparse
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
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
logger = logging.getLogger("prioritize_news")

# --- Scoring configuration ---------------------------------------------------

# Weights must sum to 1.0 -- they combine into the 0-100 base score, to which
# the cross-source bonus is added afterwards (capped at 100 total).
WEIGHTS = {
    "priority": 0.35,
    "price_move": 0.20,
    "volume": 0.15,
    "keywords": 0.15,
    "freshness": 0.15,
}

PRIORITY_SCORES = {"haute": 100.0, "moyenne": 60.0, "basse": 30.0}
PRIORITY_DEFAULT = 40.0  # ticker not found in universe (shouldn't normally happen)

PRICE_MOVE_SATURATION_PCT = 8.0   # |% change| >= this maps to 100 points
PRICE_MOVE_LOOKBACK_DAYS = 3       # compare latest close to N trading days back

VOLUME_RATIO_SATURATION = 3.0     # today's volume >= 3x trailing average -> 100
VOLUME_TRAILING_WINDOW = 20        # trailing average window (trading days)

CROSS_SOURCE_BONUS = 15.0

# Freshness brackets: (max_hours, score). Checked in order; first match wins.
FRESHNESS_BRACKETS = [
    (24, 100.0),
    (48, 75.0),
    (72, 50.0),
    (24 * 7, 25.0),
]
FRESHNESS_FLOOR = 10.0
FRESHNESS_UNKNOWN = 30.0  # published_at missing/unparsable

# High-impact keywords, French + English (mixed sources). Lowercase, matched
# as substrings against the lowercased title. Kept deliberately simple
# (no NLP/stemming) so the scoring stays fast and auditable.
HIGH_IMPACT_KEYWORDS = sorted(set([
    # FR
    "rachat", "acquisition", "fusion", "resultats", "résultats",
    "benefice", "bénéfice", "benefices", "bénéfices",
    "faillite", "sanction", "amende", "demission", "démission",
    "rupture", "proces", "procès", "rappel", "licenciement",
    "greve", "grève", "scandale", "fraude", "enquete", "enquête",
    "chute", "effondrement", "record", "avertissement",
    # EN
    "merger", "buyout", "takeover", "earnings", "profit", "bankruptcy",
    "fine", "penalty", "ceo", "resign", "resignation", "layoff", "layoffs",
    "lawsuit", "recall", "strike", "scandal", "fraud", "investigation",
    "plunge", "surge", "crash", "downgrade", "upgrade", "guidance",
    "warning", "probe", "settlement", "indictment",
]))

KEYWORD_POINTS_PER_HIT = 35.0  # capped at 100 (i.e. 3+ distinct hits saturate)


# --- SQL ---------------------------------------------------------------------

UNANALYSED_NEWS_SQL = """
SELECT r.id, r.ticker, r.source, r.title, r.published_at, r.summary_brut
FROM news_raw r
LEFT JOIN news_analysis a ON a.news_id = r.id
WHERE a.news_id IS NULL
ORDER BY r.ticker, r.id;
"""


# --- Component scores ---------------------------------------------------------

def priority_score(priorite):
    return PRIORITY_SCORES.get(priorite, PRIORITY_DEFAULT)


def price_move_score(pct_change):
    """None (no data) -> neutral 30. Otherwise scales with |% change|."""
    if pct_change is None:
        return 30.0
    magnitude = abs(pct_change)
    return min(100.0, (magnitude / PRICE_MOVE_SATURATION_PCT) * 100.0)


def volume_anomaly_score(vol_ratio):
    """None -> neutral-low 20. Ratio <=1 -> 0. Scales up to the saturation point."""
    if vol_ratio is None:
        return 20.0
    if vol_ratio <= 1.0:
        return 0.0
    span = VOLUME_RATIO_SATURATION - 1.0
    return min(100.0, ((vol_ratio - 1.0) / span) * 100.0)


def keyword_score(title):
    """Return (score, sorted list of matched keywords)."""
    text = (title or "").lower()
    hits = sorted(kw for kw in HIGH_IMPACT_KEYWORDS if kw in text)
    return min(100.0, len(hits) * KEYWORD_POINTS_PER_HIT), hits


def freshness_score(published_at, now=None):
    now = now or datetime.now(timezone.utc)
    dt = _parse_dt(published_at)
    if dt is None:
        return FRESHNESS_UNKNOWN
    hours_old = max(0.0, (now - dt).total_seconds() / 3600.0)
    for max_hours, score in FRESHNESS_BRACKETS:
        if hours_old <= max_hours:
            return score
    return FRESHNESS_FLOOR


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


# --- Data loading --------------------------------------------------------------

def load_universe_priorities(conn):
    return dict(conn.execute("SELECT ticker, priorite FROM universe"))


def _normalise_title(title):
    """Lowercase, strip punctuation, collapse whitespace (for near-dup match)."""
    t = re.sub(r"[^\w\s]", " ", (title or "").lower())
    return re.sub(r"\s+", " ", t).strip()


def build_cross_source_index(conn):
    """{ticker: {source: {normalised_title, ...}}} from ALL of news_raw, so a
    story is credited even if the matching item on the other source was
    already analysed earlier."""
    idx = {}
    for ticker, source, title in conn.execute("SELECT ticker, source, title FROM news_raw"):
        idx.setdefault(ticker, {}).setdefault(source, set()).add(_normalise_title(title))
    return idx


def has_cross_source_match(idx, ticker, source, title):
    per_source = idx.get(ticker)
    if not per_source:
        return False
    norm = _normalise_title(title)
    for other_source, titles in per_source.items():
        if other_source != source and norm in titles:
            return True
    return False


def compute_price_signals(conn, tickers):
    """Return {ticker: {'pct_move': float|None, 'vol_ratio': float|None}}."""
    if not tickers:
        return {}
    placeholders = ",".join("?" for _ in tickers)
    rows = conn.execute(
        f"SELECT ticker, date, close, volume FROM price_history "
        f"WHERE ticker IN ({placeholders}) ORDER BY ticker, date",
        list(tickers),
    ).fetchall()

    by_ticker = {}
    for ticker, date, close, volume in rows:
        by_ticker.setdefault(ticker, []).append((date, close, volume))

    signals = {}
    for ticker, series in by_ticker.items():
        closes = [c for _, c, _ in series if c is not None]
        volumes = [v for _, _, v in series if v is not None]

        pct_move = None
        if len(closes) > PRICE_MOVE_LOOKBACK_DAYS:
            base = closes[-1 - PRICE_MOVE_LOOKBACK_DAYS]
            if base:
                pct_move = (closes[-1] - base) / base * 100.0
        elif len(closes) >= 2 and closes[0]:
            pct_move = (closes[-1] - closes[0]) / closes[0] * 100.0

        vol_ratio = None
        if len(volumes) >= 2:
            trailing = volumes[-(VOLUME_TRAILING_WINDOW + 1):-1] or volumes[:-1]
            trailing_mean = mean(trailing) if trailing else None
            if trailing_mean:
                vol_ratio = volumes[-1] / trailing_mean

        signals[ticker] = {"pct_move": pct_move, "vol_ratio": vol_ratio}
    return signals


# --- Main scoring API ----------------------------------------------------------

def compute_scores(conn, priorite_filter=None):
    """Return unanalysed news scored 0-100, sorted descending.

    Each result dict has: news_id, ticker, source, title, published_at,
    priorite, score, components (per-signal breakdown for transparency).
    """
    rows = conn.execute(UNANALYSED_NEWS_SQL).fetchall()
    if not rows:
        return []

    priorities = load_universe_priorities(conn)
    if priorite_filter:
        rows = [r for r in rows if priorities.get(r[1]) == priorite_filter]
    if not rows:
        return []

    tickers = sorted({r[1] for r in rows})
    price_signals = compute_price_signals(conn, tickers)
    cross_idx = build_cross_source_index(conn)
    now = datetime.now(timezone.utc)

    results = []
    for news_id, ticker, source, title, published_at, _summary in rows:
        prio = priorities.get(ticker)
        p_score = priority_score(prio)

        sig = price_signals.get(ticker, {})
        pm_score = price_move_score(sig.get("pct_move"))
        vol_score = volume_anomaly_score(sig.get("vol_ratio"))

        kw_score, kw_hits = keyword_score(title)
        fresh_score = freshness_score(published_at, now)
        bonus = CROSS_SOURCE_BONUS if has_cross_source_match(cross_idx, ticker, source, title) else 0.0

        base = (
            WEIGHTS["priority"] * p_score
            + WEIGHTS["price_move"] * pm_score
            + WEIGHTS["volume"] * vol_score
            + WEIGHTS["keywords"] * kw_score
            + WEIGHTS["freshness"] * fresh_score
        )
        total = min(100.0, base + bonus)

        results.append({
            "news_id": news_id,
            "ticker": ticker,
            "source": source,
            "title": title,
            "published_at": published_at,
            "priorite": prio,
            "score": round(total, 1),
            "components": {
                "priority": round(p_score, 1),
                "price_move": round(pm_score, 1),
                "pct_move_raw": sig.get("pct_move"),
                "volume": round(vol_score, 1),
                "vol_ratio_raw": sig.get("vol_ratio"),
                "keywords": round(kw_score, 1),
                "keyword_hits": kw_hits,
                "freshness": round(fresh_score, 1),
                "cross_source_bonus": bonus,
            },
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    return results


# --- CLI -----------------------------------------------------------------------

def _fmt_pct(value):
    return f"{value:+.1f}%" if value is not None else "n/a"


def _fmt_ratio(value):
    return f"{value:.1f}x" if value is not None else "n/a"


def print_top(results, top_n):
    top = results[:top_n]
    print("\n" + "=" * 100)
    print(f"TOP {len(top)} NEWS BY PRIORITY SCORE (of {len(results)} unanalysed)")
    print("=" * 100)
    for rank, r in enumerate(top, start=1):
        c = r["components"]
        print(f"\n#{rank:<3d} score={r['score']:5.1f}  [{r['ticker']:<8s} "
              f"prio={r['priorite'] or '?':<8s} src={r['source']}]")
        print(f"     {r['title'][:90]}")
        print(f"     priority={c['priority']:.1f}  "
              f"price_move={c['price_move']:.1f} ({_fmt_pct(c['pct_move_raw'])})  "
              f"volume={c['volume']:.1f} ({_fmt_ratio(c['vol_ratio_raw'])})  "
              f"keywords={c['keywords']:.1f} {c['keyword_hits'] or ''}  "
              f"freshness={c['freshness']:.1f}  "
              f"cross_source_bonus={c['cross_source_bonus']:.0f}")
    print("\n" + "=" * 100 + "\n")


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Score unanalysed news by importance (no LLM calls).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the top-N scored news with signal breakdown; "
                        "makes no changes anywhere.")
    p.add_argument("--top", type=int, default=50,
                   help="How many news to show with --dry-run (default: 50).")
    p.add_argument("--priorite", default=None,
                   choices=["haute", "moyenne", "basse"],
                   help="Restrict scoring to one ticker priority tier.")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])

    if not os.path.exists(DB_PATH):
        logger.error("Database not found at %s.", DB_PATH)
        return 1

    conn = sqlite3.connect(DB_PATH)
    results = compute_scores(conn, priorite_filter=args.priorite)
    conn.close()

    if not results:
        logger.info("No unanalysed news to score.")
        return 0

    if args.dry_run:
        print_top(results, args.top)
    else:
        logger.info("%d unanalysed news scored. Top 5:", len(results))
        for r in results[:5]:
            logger.info("  %5.1f  %-8s %s", r["score"], r["ticker"], r["title"][:70])
        logger.info("Run with --dry-run for the full breakdown.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
