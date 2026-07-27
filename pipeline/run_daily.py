#!/usr/bin/env python3
"""Module 11 -- daily pipeline orchestration.

Chains every ingestion/scoring/reasoning step needed for a fresh daily
snapshot, in order:
    a. ingestion/ingest_universe_prices.py    -- price history, full universe
    b. analysis/price_valuation_scores_universe.py,
       analysis/technical_scores_universe.py,
       analysis/fundamental_real/score.py     -- scores for tickers still
                                                  missing them (each script's
                                                  own resume/skip logic --
                                                  see their docstrings; this
                                                  is a deliberate backfill-
                                                  style behaviour, not a
                                                  daily full recompute)
    c. ingestion/fetch_news.py,
       reasoning/analyze_news.py              -- news collection + Groq
                                                  analysis (own daily quota,
                                                  llm_usage, unaffected by
                                                  this script)
    d. reasoning/opportunity_scoring.py        -- regenerate `opportunites`
    e. reasoning/daily_summary.py              -- today's top-3 summary
                                                  (own quota, llm_usage_summary)
    f. reasoning/notifications.py              -- Telegram alert for
                                                  opportunities >= threshold
                                                  (no Groq quota of its own)

Every step runs in-process (each target script's own main(argv) is called
directly, not via subprocess) so a Python exception in ANY step is caught
individually and logged -- the pipeline always continues to the next step.
A step's own internal retry/backoff/quota logic is untouched; this script
only adds an outer safety net so one broken step (e.g. a network outage
during price ingestion) can never silently take the news/summary/
notification steps down with it.

Usage:
    python pipeline/run_daily.py

Windows Task Scheduler: see pipeline/run_daily.bat and the module docstring
of that file for the exact setup procedure.
"""

import logging
import os
import sqlite3
import sys
import time
import traceback

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

DB_PATH = os.path.join(REPO_ROOT, "data", "marketdb.db")

LOG_DIR = os.path.join(REPO_ROOT, "data", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "run_daily.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger("run_daily")


def run_step(name, fn, *args, **kwargs):
    """Run one pipeline step, catching ANY exception so a failure never
    blocks subsequent steps -- exactly the "un echec sur une etape ne doit
    jamais bloquer les suivantes" requirement. Returns a small result dict
    used for the final summary report."""
    logger.info("=" * 78)
    logger.info("ETAPE : %s", name)
    logger.info("=" * 78)
    start = time.time()
    try:
        result = fn(*args, **kwargs)
        elapsed = time.time() - start
        logger.info("[OK] %s termine en %.1fs.", name, elapsed)
        return {"name": name, "ok": True, "elapsed": elapsed, "error": None}
    except Exception as exc:  # noqa: BLE001
        elapsed = time.time() - start
        logger.error("[ECHEC] %s a echoue apres %.1fs : %s", name, elapsed, exc)
        logger.debug("Traceback complet :\n%s", traceback.format_exc())
        return {"name": name, "ok": False, "elapsed": elapsed, "error": str(exc)}


def _step_ingest_prices():
    import ingestion.ingest_universe_prices as mod
    return mod.main(["--priorite", "toutes"])


def _step_price_valuation_scores():
    import analysis.price_valuation_scores_universe as mod
    return mod.main(["--priorite", "toutes"])


def _step_technical_scores():
    import analysis.technical_scores_universe as mod
    return mod.main(["--priorite", "toutes"])


def _step_fundamental_scores():
    import analysis.fundamental_real.score as mod
    return mod.main(["--priorite", "toutes"])


def _step_fetch_news():
    import ingestion.fetch_news as mod
    return mod.main(["--priorite", "toutes"])


def _step_analyze_news():
    import reasoning.analyze_news as mod
    return mod.main([])


def _step_opportunity_scoring():
    import reasoning.opportunity_scoring as mod
    return mod.main(["--priorite", "toutes"])


def _step_daily_summary():
    import reasoning.daily_summary as mod
    conn = sqlite3.connect(DB_PATH)
    try:
        signals, data_date, n_candidates = mod.build_daily_summary(conn)
        mod.add_argued_texts(conn, signals)
        mod.print_summary(signals, data_date, n_candidates)
        return {"data_date": data_date, "n_signals": len(signals),
                "n_candidates": n_candidates}
    finally:
        conn.close()


def _step_notifications():
    import reasoning.notifications as mod
    from dotenv import load_dotenv
    load_dotenv()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        sent, message, n_notable = mod.run_notifications(conn)
        return {"sent": sent, "n_notable": n_notable}
    finally:
        conn.close()


PIPELINE_STEPS = [
    ("Ingestion des prix (univers complet)", _step_ingest_prices),
    ("Scores prix/valorisation (tickers manquants)", _step_price_valuation_scores),
    ("Scores techniques (tickers manquants)", _step_technical_scores),
    ("Scores fondamentaux reels (tickers manquants)", _step_fundamental_scores),
    ("Collecte des news (univers)", _step_fetch_news),
    ("Analyse des news (Groq, quota quotidien)", _step_analyze_news),
    ("Regeneration de opportunites", _step_opportunity_scoring),
    ("Resume du jour", _step_daily_summary),
    ("Notification Telegram", _step_notifications),
]


def main(argv=None):
    if not os.path.exists(DB_PATH):
        logger.error("Database not found at %s.", DB_PATH)
        return 1

    run_start = time.time()
    logger.info("DEBUT DU PIPELINE QUOTIDIEN (%d etapes)", len(PIPELINE_STEPS))

    results = [run_step(name, fn) for name, fn in PIPELINE_STEPS]

    total_elapsed = time.time() - run_start
    n_ok = sum(1 for r in results if r["ok"])
    n_failed = len(results) - n_ok

    logger.info("=" * 78)
    logger.info("BILAN DU PIPELINE QUOTIDIEN")
    logger.info("=" * 78)
    for r in results:
        status = "OK" if r["ok"] else "ECHEC"
        detail = f" -- {r['error']}" if r["error"] else ""
        logger.info("  [%-5s] %-45s %6.1fs%s", status, r["name"], r["elapsed"], detail)
    logger.info("-" * 78)
    logger.info("Total : %d/%d etapes reussies, duree globale %.1fs (%.1f min).",
                n_ok, len(results), total_elapsed, total_elapsed / 60.0)
    logger.info("Log complet : %s", LOG_FILE)

    return 0 if n_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
