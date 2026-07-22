#!/usr/bin/env python3
"""Compute a REAL fundamental score from actual company financial data.

Unlike analysis/fundamental/score.py (a price/valuation score -- momentum vs
moving averages + volatility, despite its module path), this uses genuine
company fundamentals from yfinance:
  * revenue growth      (`.info["revenueGrowth"]`)
  * net profit margin   (`.info["profitMargins"]`)
  * debt/equity ratio   (`.info["debtToEquity"]`)
  * free cash flow YoY evolution (derived from `.cashflow`'s "Free Cash Flow"
    row, comparing the two most recent fiscal years)

Field reliability (verified empirically on the 10 pilot tickers before
writing this scoring logic -- see the session's exploration, not assumed):
  * revenueGrowth / profitMargins: present for all 10 pilots.
  * debtToEquity: present for 9/10 (missing for JPM -- a bank; the metric is
    less meaningful for financials anyway, whose leverage model differs).
  * `.info`'s own `freeCashflow`/`operatingCashflow`/`ebitda` are missing for
    JPM and JNJ. However, the full `.cashflow` STATEMENT (a separate call)
    has a populated "Free Cash Flow" row for every single pilot, including
    JPM/JNJ -- so FCF evolution is derived from the statement, not `.info`,
    for much better coverage. This costs a second API call per ticker
    (~0.2-0.5s), roughly doubling the time budget vs a single `.info` call.

Rate limits (verified empirically, not assumed): 88 consecutive `.info` calls
with zero delay produced 0 HTTP 429s (~0.22s/ticker) -- contrary to the
assumption that this endpoint is stricter than price history. Retry with
exponential backoff on 429 is still implemented as a safety margin for a much
larger, longer-running batch (same pattern as ingestion/fetch_news.py's
Finnhub retry), and a conservative batch/pause default is used regardless.

Missing/unusable fields are handled explicitly per component -- a component
with no data is simply excluded from score_global (which is a weighted
average of whatever IS available, renormalised over the available subset,
same design as reasoning/opportunity_scoring.py); confiance reflects how many
of the 4 components were actually available. Never crashes on a ticker.

This is a STANDALONE module for now: it writes to its own
`fundamental_real_scores` table, not yet wired into final_scores/
opportunity_scoring.py/daily_summary.py -- that integration is a deliberate
follow-up decision once the test results (this run) have been reviewed.

Usage:
    python analysis/fundamental_real/score.py --tickers AAPL,MSFT,GOOGL
    python analysis/fundamental_real/score.py --priorite haute --limit 20
    python analysis/fundamental_real/score.py --priorite haute
"""

import argparse
import logging
import os
import sqlite3
import sys

# The report uses check/cross marks (✓/✗/•); Windows consoles often default to
# cp1252, which can't encode them. Force UTF-8 stdout so the CLI report never
# crashes on print() (same fix as reasoning/daily_summary.py).
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
logger = logging.getLogger("fundamental_real_score")

# --- Weights: easy to adjust, must sum to 1.0 -------------------------------
# Growth and margin weighted slightly higher: they are steadier, more
# universally comparable "quality" signals. Debt and (especially) FCF YoY
# evolution can swing much more sharply quarter to quarter (observed FCF
# growth ranging from -252% to +74% across the 10 pilots), so they carry
# less weight individually.
W_GROWTH = 0.30
W_MARGIN = 0.30
W_DEBT = 0.20
W_FCF = 0.20

# Normalisation bounds: linear map to [0,100], clamped at both ends.
# Chosen from the empirically observed pilot distribution, documented per
# component rather than picked arbitrarily.
GROWTH_MIN, GROWTH_MAX = -0.10, 0.30      # revenueGrowth: -10% to +30%
MARGIN_MIN, MARGIN_MAX = -0.10, 0.40      # profitMargins: -10% to +40%
DEBT_LOW, DEBT_HIGH = 20.0, 150.0         # debtToEquity: lower is better (inverse)
FCF_GROWTH_MIN, FCF_GROWTH_MAX = -0.30, 0.30  # FCF YoY: -30% to +30%

THRESH_SOLID = 60.0
THRESH_FAIBLE = 40.0

MAX_RETRIES = 5
BACKOFF_BASE = 2.0  # seconds: 2, 4, 8, 16, 32 -- same pattern as fetch_news.py


# --- Schema ------------------------------------------------------------------

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS fundamental_real_scores (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol         TEXT NOT NULL,
    revenue_growth REAL,
    profit_margin  REAL,
    debt_to_equity REAL,
    fcf_growth     REAL,
    score_growth   REAL,
    score_margin   REAL,
    score_debt     REAL,
    score_fcf      REAL,
    score_global   REAL,
    explication    TEXT,
    confiance      REAL,
    timestamp      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

INSERT_SQL = """
INSERT INTO fundamental_real_scores
    (symbol, revenue_growth, profit_margin, debt_to_equity, fcf_growth,
     score_growth, score_margin, score_debt, score_fcf, score_global,
     explication, confiance)
VALUES
    (:symbol, :revenue_growth, :profit_margin, :debt_to_equity, :fcf_growth,
     :score_growth, :score_margin, :score_debt, :score_fcf, :score_global,
     :explication, :confiance);
"""


# --- Normalisation helpers ---------------------------------------------------

def _norm(value, lo, hi):
    """Linear map [lo, hi] -> [0, 100], clamped. None stays None."""
    if value is None:
        return None
    return max(0.0, min(1.0, (value - lo) / (hi - lo))) * 100.0


def _norm_inverse(value, lo, hi):
    """Linear map where lo -> 100 and hi -> 0, clamped (lower is better)."""
    if value is None:
        return None
    return max(0.0, min(1.0, (hi - value) / (hi - lo))) * 100.0


# --- Rate limit handling -----------------------------------------------------

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


# --- Data fetch --------------------------------------------------------------

def fetch_fundamentals(ticker):
    """Return {revenue_growth, profit_margin, debt_to_equity, fcf_growth},
    each possibly None. Never raises -- logs and degrades gracefully."""
    result = {
        "revenue_growth": None,
        "profit_margin": None,
        "debt_to_equity": None,
        "fcf_growth": None,
    }

    try:
        info = _with_retry(lambda: yf.Ticker(ticker).info)
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s: .info fetch failed (%s)", ticker, exc)
        info = {}
    info = info or {}

    result["revenue_growth"] = info.get("revenueGrowth")
    result["profit_margin"] = info.get("profitMargins")
    result["debt_to_equity"] = info.get("debtToEquity")

    try:
        cashflow = _with_retry(lambda: yf.Ticker(ticker).cashflow)
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s: .cashflow fetch failed (%s)", ticker, exc)
        cashflow = None

    if cashflow is not None and not cashflow.empty and "Free Cash Flow" in cashflow.index:
        row = cashflow.loc["Free Cash Flow"].dropna()
        if len(row) >= 2 and row.iloc[1]:
            result["fcf_growth"] = float((row.iloc[0] - row.iloc[1]) / abs(row.iloc[1]))

    return result


# --- Scoring -------------------------------------------------------------------

def _component_line(label, raw, score, pos_word, neg_word, as_pct=True):
    if score is None:
        return f"○ {label} : donnee indisponible"
    icon = "✓" if score >= THRESH_SOLID else ("✗" if score < THRESH_FAIBLE else "•")
    qualif = pos_word if score >= THRESH_SOLID else (neg_word if score < THRESH_FAIBLE else "neutre")
    if raw is None:
        raw_str = "n/a"
    elif as_pct:
        raw_str = f"{raw:+.1%}"
    else:
        raw_str = f"{raw:.1f}"
    return f"{icon} {label} {qualif} ({raw_str}, score {score:.0f}/100)"


def score_ticker(ticker):
    """Compute the full real-fundamental record for one ticker. Never raises."""
    raw = fetch_fundamentals(ticker)

    score_growth = _norm(raw["revenue_growth"], GROWTH_MIN, GROWTH_MAX)
    score_margin = _norm(raw["profit_margin"], MARGIN_MIN, MARGIN_MAX)
    score_debt = _norm_inverse(raw["debt_to_equity"], DEBT_LOW, DEBT_HIGH)
    score_fcf = _norm(raw["fcf_growth"], FCF_GROWTH_MIN, FCF_GROWTH_MAX)

    weights = {"growth": W_GROWTH, "margin": W_MARGIN, "debt": W_DEBT, "fcf": W_FCF}
    available = {}
    if score_growth is not None:
        available["growth"] = score_growth
    if score_margin is not None:
        available["margin"] = score_margin
    if score_debt is not None:
        available["debt"] = score_debt
    if score_fcf is not None:
        available["fcf"] = score_fcf

    if available:
        total_weight = sum(weights[k] for k in available)
        score_global = round(sum(weights[k] * v for k, v in available.items()) / total_weight, 1)
    else:
        score_global = None

    confiance = round(100.0 * len(available) / 4.0, 1)

    lines = [
        _component_line("Croissance CA", raw["revenue_growth"], score_growth, "forte", "faible"),
        _component_line("Marge nette", raw["profit_margin"], score_margin, "solide", "faible"),
        _component_line("Endettement", raw["debt_to_equity"], score_debt,
                        "maitrise", "eleve", as_pct=False),
        _component_line("Evolution FCF", raw["fcf_growth"], score_fcf, "positive", "negative"),
    ]
    explication = " | ".join(lines)

    return {
        "symbol": ticker,
        "revenue_growth": raw["revenue_growth"],
        "profit_margin": raw["profit_margin"],
        "debt_to_equity": raw["debt_to_equity"],
        "fcf_growth": raw["fcf_growth"],
        "score_growth": round(score_growth, 1) if score_growth is not None else None,
        "score_margin": round(score_margin, 1) if score_margin is not None else None,
        "score_debt": round(score_debt, 1) if score_debt is not None else None,
        "score_fcf": round(score_fcf, 1) if score_fcf is not None else None,
        "score_global": score_global,
        "explication": explication,
        "confiance": confiance,
    }


# --- Data access -----------------------------------------------------------

def load_tickers(conn, priorite, limit, explicit_tickers):
    """Same --priorite/--limit contract as the other universe-scale scripts,
    plus an optional --tickers override for ad-hoc testing."""
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


# --- Orchestration ---------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Compute REAL fundamental scores (growth/margin/debt/FCF) via yfinance.")
    p.add_argument("--priorite", default="toutes",
                   choices=["haute", "moyenne", "basse", "toutes"])
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--tickers", type=str, default=None,
                   help="Comma-separated explicit ticker list, overrides --priorite/--limit.")
    p.add_argument("--batch-size", type=int, default=20)
    p.add_argument("--pause", type=float, default=2.0)
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
    conn.execute(CREATE_TABLE_SQL)
    conn.commit()

    tickers = load_tickers(conn, args.priorite, args.limit, explicit_tickers)
    if not tickers:
        logger.warning("No tickers to process.")
        conn.close()
        return 1

    batches = list(_chunks(tickers, args.batch_size))
    logger.info("%d tickers | %d lot(s) de %d | pause=%ss",
                len(tickers), len(batches), args.batch_size, args.pause)

    start = time.time()
    n_ok, n_no_data = 0, 0
    results = []

    for i, batch in enumerate(batches, start=1):
        batch_ok = 0
        for ticker in batch:
            record = score_ticker(ticker)
            try:
                conn.execute(INSERT_SQL, record)
                conn.commit()
            except sqlite3.Error as exc:
                conn.rollback()
                logger.error("%s: insert failed (%s)", ticker, exc)
                continue
            results.append(record)
            if record["score_global"] is not None:
                n_ok += 1
                batch_ok += 1
            else:
                n_no_data += 1

        logger.info("Lot %d/%d traite : %d/%d avec au moins une donnee.",
                    i, len(batches), batch_ok, len(batch))

        if i < len(batches):
            time.sleep(args.pause)

    elapsed = time.time() - start
    conn.close()

    logger.info("=" * 60)
    logger.info("Termine en %.1fs (%.2fs/ticker). Avec donnees: %d | sans "
                "aucune donnee: %d | total: %d.",
                elapsed, elapsed / len(tickers), n_ok, n_no_data, len(tickers))

    _print_report(results)
    return 0 if n_ok else 1


def _print_report(results):
    if not results:
        return
    scored = [r for r in results if r["score_global"] is not None]
    print("\n" + "=" * 100)
    print("REAL FUNDAMENTAL SCORES")
    print("=" * 100)
    for r in sorted(scored, key=lambda r: r["score_global"], reverse=True):
        print(f"{r['symbol']:<8} score_global={r['score_global']:>5.1f}  "
              f"confiance={r['confiance']:>5.1f}%")
        print(f"    {r['explication']}")
    missing = [r["symbol"] for r in results if r["score_global"] is None]
    if missing:
        print(f"\nAucune donnee du tout pour: {', '.join(missing)}")
    print("=" * 100 + "\n")


if __name__ == "__main__":
    sys.exit(main())
