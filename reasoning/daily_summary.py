#!/usr/bin/env python3
"""Daily Summary -- the strongest investment signals detected TODAY.

This is an explicit, opinionated advisory output (not a neutral alert feed):
the project is a personal financial-advisory tool, so each signal is
presented with its supporting arguments, an explicit risk level, and a "today
only" horizon -- this is about what to look at right now, not a forecast for
next week or next month.

Selection logic
----------------
Candidates come from today's rows in `opportunites` (date_calcul = today).
Ranking uses an ADJUSTED score, not the raw score_global, because a high raw
score built on a single, unverified signal should never outrank a lower but
well-supported one:

    score_ajuste = score_global * (confiance / 100)

Rationale: `confiance` (see reasoning/opportunity_scoring.py) already measures
exactly "how much of this score can we trust" -- it is 100 when all three
components (fundamental, technical, fresh news) are present, and lower when
some are missing or stale. Multiplying makes confiance a direct, transparent
discount on the raw score: a ticker at 80 with 33% confiance scores 26.6,
well below a ticker at 73 with 83% confiance (60.8) -- exactly the ordering a
person reading a daily "top picks" list would expect and trust. It is a single
line, easy to retune later (e.g. sqrt(confiance/100) to soften the penalty) if
the weighting ever needs adjusting.

Tickers below MIN_CONFIDENCE are excluded outright regardless of score --
quality over quantity, per the project's own guidance: showing 0, 1 or 2
signals is preferable to forcing a 3rd pick nobody should act on.

Risk level is derived (not scored by an LLM) from three signals:
  * annualised volatility (same bands as analysis/fundamental/score.py's
    score_volatility: >40% high, <20% low)
  * confiance itself (a signal that isn't fully backed carries more risk)
  * coherence between the fundamental and technical components -- if one is
    clearly strong and the other clearly weak, that contradiction raises risk
    (a stock the fundamentals like but the technicals are selling off, or vice
    versa, is a genuinely less clear-cut situation)

"Companies to watch" queries the Knowledge Graph (graph/build_graph.py,
networkx) for each retained ticker's direct relations (competitor/supplier/
client/partner); a ticker absent from the graph simply gets no such section,
never an error.

Usage:
    python reasoning/daily_summary.py
"""

import argparse
import logging
import os
import sqlite3
import sys
from datetime import date

# The report uses check/cross marks (✓/✗/•) inherited from opportunity_scoring's
# explication text; Windows consoles often default to cp1252, which can't
# encode them. Force UTF-8 stdout so the CLI report never crashes on print().
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from analysis.fundamental_scores_universe import compute_volatility  # noqa: E402
from graph.build_graph import build_graph, direct_relations, load_relations  # noqa: E402

DB_PATH = os.path.join(REPO_ROOT, "data", "marketdb.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("daily_summary")

# --- Configuration -----------------------------------------------------------

TOP_N = 3
# Quality gate: a ticker below this confiance is never shown, however high
# its raw score_global -- fewer, trustworthy signals beat a forced 3rd pick.
MIN_CONFIDENCE = 50.0

# Same thresholds as reasoning/opportunity_scoring.py's own component labels.
THRESH_SOLID = 60.0
THRESH_FAIBLE = 40.0

# Same volatility bands as analysis/fundamental/score.py's score_volatility().
VOL_HIGH = 0.40
VOL_LOW = 0.20

HORIZON_LABEL = ("Signal du jour - a surveiller aujourd'hui, "
                "pas une prevision a moyen/long terme.")


# --- Scoring / risk ------------------------------------------------------------

def compute_adjusted_score(score_global, confiance):
    """score_ajuste = score_global * (confiance/100). See module docstring."""
    return round(score_global * (confiance / 100.0), 2)


def _classify(score):
    """haute / neutre / basse against the same bands opportunity_scoring.py
    uses for its own ✓/•/✗ explanation labels, or None if unavailable."""
    if score is None:
        return None
    if score >= THRESH_SOLID:
        return "haute"
    if score < THRESH_FAIBLE:
        return "basse"
    return "neutre"


def has_conflict(score_fondamental, score_technique):
    """True when fundamental and technical clearly disagree (one solid, the
    other weak) -- a genuinely less clear-cut situation, not just a small gap."""
    f, t = _classify(score_fondamental), _classify(score_technique)
    if f is None or t is None:
        return False
    return {f, t} == {"haute", "basse"}


def compute_risk(volatility, confiance, conflict):
    """Faible / Modere / Eleve from a simple, transparent point system."""
    points = 0
    if volatility is not None:
        if volatility > VOL_HIGH:
            points += 2
        elif volatility > VOL_LOW:
            points += 0  # normal range, no penalty
        else:
            points += 0
    if confiance < 70.0:
        points += 1
    if conflict:
        points += 2

    if points <= 1:
        return "Faible"
    if points <= 3:
        return "Modere"
    return "Eleve"


# --- Data access -----------------------------------------------------------

def load_today_opportunites(conn, today):
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM opportunites WHERE date_calcul = ? AND score_global IS NOT NULL",
        (today,),
    ).fetchall()


def load_price_series(conn, ticker):
    rows = conn.execute(
        "SELECT close FROM price_history WHERE ticker = ? AND close IS NOT NULL "
        "ORDER BY date",
        (ticker,),
    ).fetchall()
    return [r[0] for r in rows]


def companies_to_watch(graph, relations, ticker):
    """Direct relations grouped by type, or None if the ticker isn't in the
    Knowledge Graph at all (never an error in that case)."""
    if not graph.has_node(ticker):
        return None
    grouped = direct_relations(relations, ticker)
    return grouped or None


# --- Orchestration ---------------------------------------------------------

def build_daily_summary(conn, today=None):
    """Return (signals, today, n_candidates). ``signals`` has at most TOP_N
    entries, fewer if not enough tickers clear MIN_CONFIDENCE."""
    today = today or date.today().isoformat()

    rows = load_today_opportunites(conn, today)
    eligible = [r for r in rows if r["confiance"] is not None and r["confiance"] >= MIN_CONFIDENCE]

    ranked = sorted(
        eligible,
        key=lambda r: compute_adjusted_score(r["score_global"], r["confiance"]),
        reverse=True,
    )
    top = ranked[:TOP_N]

    relations = load_relations(conn)
    graph = build_graph(relations)

    signals = []
    for r in top:
        closes = load_price_series(conn, r["ticker"])
        volatility = compute_volatility(closes) if closes else None
        conflict = has_conflict(r["score_fondamental"], r["score_technique"])
        risk = compute_risk(volatility, r["confiance"], conflict)
        watch = companies_to_watch(graph, relations, r["ticker"])

        signals.append({
            "ticker": r["ticker"],
            "score_global": r["score_global"],
            "confiance": r["confiance"],
            "score_ajuste": compute_adjusted_score(r["score_global"], r["confiance"]),
            "score_fondamental": r["score_fondamental"],
            "score_technique": r["score_technique"],
            "score_news": r["score_news"],
            "explication": r["explication"],
            "risque": risk,
            "conflit_composantes": conflict,
            "volatilite": volatility,
            "horizon": HORIZON_LABEL,
            "entreprises_a_surveiller": watch,
        })

    return signals, today, len(eligible)


# --- CLI ---------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(description="Build today's daily investment summary.")
    p.add_argument("--date", default=None, help="Override date_calcul (YYYY-MM-DD), for testing.")
    return p.parse_args(argv)


def _fmt_pct(value):
    return f"{value:.0%}" if value is not None else "n/a"


def print_summary(signals, today, n_candidates):
    print("\n" + "=" * 78)
    print(f"RESUME DU JOUR - {today}")
    print("=" * 78)
    if not signals:
        print(f"Aucun signal ne depasse le seuil de confiance minimal "
              f"({MIN_CONFIDENCE:.0f}%) parmi {n_candidates} candidat(s) eligible(s).")
        print("=" * 78 + "\n")
        return

    print(f"{len(signals)} signal(aux) retenu(s) sur {n_candidates} candidat(s) eligibles "
          f"(confiance >= {MIN_CONFIDENCE:.0f}%).\n")

    for rank, s in enumerate(signals, start=1):
        print(f"#{rank} {s['ticker']} - score ajuste {s['score_ajuste']:.1f} "
              f"(brut {s['score_global']:.1f} x confiance {s['confiance']:.0f}%)")
        print(f"    Risque: {s['risque']}" +
              (" (fondamental/technique en contradiction)" if s["conflit_composantes"] else "") +
              (f" - volatilite annualisee {_fmt_pct(s['volatilite'])}" if s["volatilite"] else ""))
        print(f"    Horizon: {s['horizon']}")
        print(f"    Arguments: {s['explication']}")
        if s["entreprises_a_surveiller"]:
            parts = [f"{rtype}: {', '.join(names)}"
                    for rtype, names in s["entreprises_a_surveiller"].items()]
            print(f"    Entreprises a surveiller: {' | '.join(parts)}")
        print()
    print("=" * 78 + "\n")


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])

    if not os.path.exists(DB_PATH):
        logger.error("Database not found at %s.", DB_PATH)
        return 1

    conn = sqlite3.connect(DB_PATH)
    signals, today, n_candidates = build_daily_summary(conn, today=args.date)
    conn.close()

    print_summary(signals, today, n_candidates)
    logger.info("Resume genere pour %s : %d signal(aux) (sur %d candidats eligibles).",
                today, len(signals), n_candidates)
    return 0


if __name__ == "__main__":
    sys.exit(main())
