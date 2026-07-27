#!/usr/bin/env python3
"""Module 8 -- correlation discovery, anchored on ALREADY KNOWN relations.

Deliberately NOT a blind screen of every ticker pair in the universe: with
~500+ haute tickers that would be ~125,000 pairs, and testing that many
pairs (x several lags each) makes finding a handful of "statistically
significant" correlations by pure chance a near-certainty, not a discovery
-- exactly the multiple-comparisons trap this module is designed to avoid.
Instead, every pair tested here already has a real, human-inspectable
reason to suspect a relationship: an edge in `relations` (the 40
hand-curated pilot relations) or `relations_generated` (Groq-proposed,
graph/generate_relations.py, statut='a_valider'). This turns an
astronomically large hypothesis space into a small, justified one -- and
still applies a multiple-testing correction across whatever IS tested,
because even a "small" set of pairs x lags adds up fast (see LAGS below).

Method
------
For each (source_ticker, target_ticker) pair from the chosen relation
table:
  1. Build the two tickers' DAILY RETURNS (pct change of close-to-close),
     not raw prices -- correlating price LEVELS would pick up a shared
     market-wide uptrend/downtrend as a spurious "relationship" between
     any two stocks, returns strip that out.
  2. Align on the dates BOTH tickers actually have a valid (non-null,
     positive) close for -- price_history coverage differs a lot between
     the 10 pilots (~502 trading days) and everything added later via the
     universe pipeline (~251 trading days), so a naive index-based join
     would silently misalign the two series.
  3. Test the SIMULTANEOUS correlation (lag 0) plus, for each lag in LAGS,
     BOTH directions -- source leading target (e.g. "the supplier reacts
     N days after the client") and target leading source -- since a
     relation_type like "concurrent" has no obvious a-priori direction, and
     even "fournisseur"/"client" only suggests a plausible direction, it
     does not prove one.
  4. Spearman's rank correlation (scipy.stats.spearmanr), not Pearson:
     daily stock returns are fat-tailed and a single earnings-day move can
     dominate a Pearson coefficient; rank correlation is far less sensitive
     to that kind of one-day outlier while still capturing a real
     co-movement or lagged-reaction pattern.
  5. Every p-value produced in the run is corrected together via
     Benjamini-Hochberg FDR (scipy.stats.false_discovery_control) -- ALL
     tests from this run form the correction "family", not just the ones
     that look promising before correction (correcting only the
     already-filtered subset would defeat the point).
  6. A result is RETAINED only if BOTH the corrected p-value < ALPHA and
     the pair had at least MIN_OBSERVATIONS common trading days for that
     specific lag -- a "significant" correlation built on 20 overlapping
     days is not trustworthy regardless of its p-value.

Every stored row keeps the full transparency trail (both tickers, the
originating relation_type, the lag and its direction, the coefficient, the
raw AND corrected p-value, and the observation count) -- a correlation
without this context is never stored.

This module NEVER touches the Knowledge Graph (`relations`), reasoning/
causal_reasoning.py, or the dashboard -- purely a discovery/storage stage
for human review, exactly like graph/generate_relations.py's
relations_generated table.

Usage:
    python reasoning/correlation_discovery.py --source relations --dry-run
    python reasoning/correlation_discovery.py --source relations
    python reasoning/correlation_discovery.py --source relations_generated
"""

import argparse
import logging
import os
import sqlite3
import sys
from datetime import date

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

DB_PATH = os.path.join(REPO_ROOT, "data", "marketdb.db")

import numpy as np  # noqa: E402
from scipy import stats  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("correlation_discovery")

# --- Configuration -----------------------------------------------------------

# Trading-day lags tested in ADDITION to the simultaneous (lag 0) case, both
# directions each (source-leads-target and target-leads-source).
LAGS = [1, 3, 5, 10]

MIN_OBSERVATIONS = 60   # per-lag common-trading-day count floor
ALPHA = 0.05            # on the FDR-corrected p-value

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS correlations_discovered (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker_source      TEXT NOT NULL,
    ticker_target      TEXT NOT NULL,
    relation_type      TEXT NOT NULL,
    source_table       TEXT NOT NULL,
    lag                INTEGER NOT NULL,
    lag_direction      TEXT NOT NULL,
    coefficient        REAL NOT NULL,
    p_value            REAL NOT NULL,
    p_value_corrigee   REAL NOT NULL,
    n_observations     INTEGER NOT NULL,
    methode            TEXT NOT NULL,
    correction         TEXT NOT NULL,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (ticker_source, ticker_target, relation_type, source_table, lag)
);
"""

INSERT_SQL = """
INSERT INTO correlations_discovered
    (ticker_source, ticker_target, relation_type, source_table, lag,
     lag_direction, coefficient, p_value, p_value_corrigee, n_observations,
     methode, correction)
VALUES
    (:ticker_source, :ticker_target, :relation_type, :source_table, :lag,
     :lag_direction, :coefficient, :p_value, :p_value_corrigee, :n_observations,
     :methode, :correction)
ON CONFLICT(ticker_source, ticker_target, relation_type, source_table, lag)
DO UPDATE SET
    coefficient      = excluded.coefficient,
    p_value          = excluded.p_value,
    p_value_corrigee = excluded.p_value_corrigee,
    n_observations   = excluded.n_observations,
    created_at       = CURRENT_TIMESTAMP;
"""


# --- Pair loading --------------------------------------------------------------

def load_pairs_from_relations(conn):
    """(source_ticker, relation_type, target_ticker) from the active,
    hand-curated Knowledge Graph -- only rows with a real target_ticker
    (external entities with no ticker can't be correlated)."""
    rows = conn.execute(
        "SELECT DISTINCT source_ticker, relation_type, target_ticker "
        "FROM relations WHERE target_ticker IS NOT NULL AND TRIM(target_ticker) != ''"
    ).fetchall()
    return [(r[0].strip(), r[1], r[2].strip()) for r in rows]


def load_pairs_from_relations_generated(conn, only_resolved=True):
    """Same shape, from the Groq-proposed (still 'a_valider') relations.
    only_resolved=True restricts to rows whose target already matched an
    existing universe ticker (resolved=1) -- an unresolved row has no
    target_ticker to correlate against at all."""
    sql = ("SELECT DISTINCT source_ticker, relation_type, target_ticker "
           "FROM relations_generated WHERE target_ticker IS NOT NULL")
    if only_resolved:
        sql += " AND resolved = 1"
    rows = conn.execute(sql).fetchall()
    return [(r[0].strip(), r[1], r[2].strip()) for r in rows]


# --- Returns construction --------------------------------------------------

def load_close_by_date(conn, ticker):
    rows = conn.execute(
        "SELECT date, close FROM price_history WHERE ticker = ? AND close IS NOT NULL",
        (ticker,),
    ).fetchall()
    return {d: c for d, c in rows if c is not None}


def build_common_returns(conn, ticker_a, ticker_b):
    """Daily returns for both tickers, aligned on the dates BOTH have a
    valid positive close for. Returns (returns_a, returns_b, n_common_dates)
    -- both lists have length n_common_dates - 1 (returns need a prior
    close). Empty lists if fewer than 2 common dates."""
    closes_a = load_close_by_date(conn, ticker_a)
    closes_b = load_close_by_date(conn, ticker_b)

    common_dates = sorted(
        d for d in (set(closes_a) & set(closes_b))
        if closes_a[d] > 0 and closes_b[d] > 0
    )
    if len(common_dates) < 2:
        return [], [], len(common_dates)

    series_a = [closes_a[d] for d in common_dates]
    series_b = [closes_b[d] for d in common_dates]

    returns_a = [(b - a) / a for a, b in zip(series_a, series_a[1:])]
    returns_b = [(b - a) / a for a, b in zip(series_b, series_b[1:])]
    return returns_a, returns_b, len(common_dates)


def lagged_series(returns_source, returns_target, lag):
    """(x, y) aligned pairs for a given signed lag. lag=0: simultaneous.
    lag>0: source leads target (source[i] paired with target[i+lag]).
    lag<0: target leads source (target[i] paired with source[i-lag])."""
    if lag == 0:
        return returns_source, returns_target
    if lag > 0:
        return returns_source[:-lag], returns_target[lag:]
    n = -lag
    return returns_source[n:], returns_target[:-n]


# --- Statistical test -------------------------------------------------------

def test_pair(returns_source, returns_target, lag):
    """Returns (coefficient, p_value, n_observations) for one (pair, lag).
    None values if fewer than 2 usable observations (spearmanr needs >=2,
    but MIN_OBSERVATIONS filters far more strictly downstream -- this is
    just to avoid a crash on a degenerate pair)."""
    x, y = lagged_series(returns_source, returns_target, lag)
    n = min(len(x), len(y))
    if n < 2:
        return None, None, n
    rho, p_value = stats.spearmanr(x[:n], y[:n])
    if np.isnan(rho):
        return None, None, n
    return float(rho), float(p_value), n


# --- Orchestration ------------------------------------------------------------

def run_discovery(conn, pairs, source_table, min_observations=MIN_OBSERVATIONS,
                   alpha=ALPHA):
    """Test every (source, relation_type, target) pair at lag 0 and each of
    LAGS in both directions. Returns a list of ALL result dicts (whether or
    not they end up retained) -- every dict has 'significatif' set once
    p-values are corrected. Skips a pair entirely (logged) if it has fewer
    than min_observations common trading days even at lag 0."""
    all_results = []
    n_pairs_skipped = 0

    for source_ticker, relation_type, target_ticker in pairs:
        returns_source, returns_target, n_common_dates = build_common_returns(
            conn, source_ticker, target_ticker)
        if len(returns_source) < min_observations:
            n_pairs_skipped += 1
            logger.info(
                "%s -> %s (%s) : ignore, seulement %d jour(s) de trading "
                "commun(s) (< %d requis).",
                source_ticker, target_ticker, relation_type,
                n_common_dates, min_observations,
            )
            continue

        tests = [(0, "simultane")]
        for lag in LAGS:
            tests.append((lag, "source_precede_target"))
            tests.append((-lag, "target_precede_source"))

        for lag, direction in tests:
            coefficient, p_value, n_obs = test_pair(returns_source, returns_target, lag)
            if coefficient is None or n_obs < min_observations:
                continue
            all_results.append({
                "ticker_source": source_ticker,
                "ticker_target": target_ticker,
                "relation_type": relation_type,
                "source_table": source_table,
                "lag": lag,
                "lag_direction": direction,
                "coefficient": coefficient,
                "p_value": p_value,
                "n_observations": n_obs,
            })

    if not all_results:
        logger.warning("Aucun test exploitable (%d paire(s) ignoree(s) pour "
                       "historique commun insuffisant).", n_pairs_skipped)
        return []

    raw_p_values = np.array([r["p_value"] for r in all_results])
    corrected = stats.false_discovery_control(raw_p_values, method="bh")
    for r, p_corr in zip(all_results, corrected):
        r["p_value_corrigee"] = float(p_corr)
        r["significatif"] = bool(p_corr < alpha and r["n_observations"] >= min_observations)

    logger.info(
        "%d test(s) effectue(s) sur %d paire(s) (%d paire(s) ignoree(s) "
        "pour historique insuffisant). Correction FDR (Benjamini-Hochberg) "
        "appliquee sur l'ensemble des %d tests.",
        len(all_results), len(pairs) - n_pairs_skipped, n_pairs_skipped,
        len(all_results),
    )
    return all_results


def store_significant(conn, results):
    conn.execute(CREATE_TABLE_SQL)
    conn.commit()
    to_store = [r for r in results if r["significatif"]]
    for r in to_store:
        conn.execute(INSERT_SQL, {
            **r,
            "methode": "spearman",
            "correction": "fdr_bh",
        })
    conn.commit()
    return len(to_store)


def print_report(results, alpha=ALPHA):
    if not results:
        print("\nAucun resultat exploitable.\n")
        return

    results_sorted = sorted(results, key=lambda r: r["p_value_corrigee"])
    n_sig = sum(1 for r in results if r["significatif"])

    print("\n" + "=" * 110)
    print(f"CORRELATION DISCOVERY -- {len(results)} test(s), {n_sig} retenu(s) "
          f"(p corrigee < {alpha}, Spearman, correction FDR Benjamini-Hochberg)")
    print("=" * 110)
    header = (f"{'source':<8} {'target':<8} {'relation':<12} {'lag':>5} "
              f"{'direction':<24} {'rho':>7} {'p brute':>10} {'p corrigee':>12} "
              f"{'n':>5}  retenu")
    print(header)
    print("-" * 110)
    for r in results_sorted:
        flag = "OUI" if r["significatif"] else "non"
        print(f"{r['ticker_source']:<8} {r['ticker_target']:<8} "
              f"{r['relation_type']:<12} {r['lag']:>5} {r['lag_direction']:<24} "
              f"{r['coefficient']:>7.3f} {r['p_value']:>10.4f} "
              f"{r['p_value_corrigee']:>12.4f} {r['n_observations']:>5}  {flag}")
    print("=" * 110 + "\n")


# --- CLI -----------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Test correlations on ticker pairs from known relations only.")
    p.add_argument("--source", choices=["relations", "relations_generated"],
                   default="relations",
                   help="Which relation table to draw pairs from (default: relations, "
                        "the 40 validated pilot relations).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the full report; never writes to correlations_discovered.")
    p.add_argument("--min-observations", type=int, default=MIN_OBSERVATIONS)
    p.add_argument("--alpha", type=float, default=ALPHA)
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])

    if not os.path.exists(DB_PATH):
        logger.error("Database not found at %s.", DB_PATH)
        return 1

    conn = sqlite3.connect(DB_PATH)

    if args.source == "relations":
        pairs = load_pairs_from_relations(conn)
    else:
        pairs = load_pairs_from_relations_generated(conn)

    logger.info("Source=%s : %d paire(s) (source_ticker, relation_type, target_ticker) a tester.",
                args.source, len(pairs))

    results = run_discovery(conn, pairs, args.source,
                            min_observations=args.min_observations, alpha=args.alpha)
    print_report(results, alpha=args.alpha)

    if not args.dry_run and results:
        n_stored = store_significant(conn, results)
        logger.info("%d correlation(s) significative(s) stockee(s) dans "
                    "correlations_discovered.", n_stored)
    elif args.dry_run:
        logger.info("[DRY-RUN] Rien stocke dans correlations_discovered.")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
