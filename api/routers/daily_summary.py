"""GET /api/daily-summary/* -- JSON view of reasoning/daily_summary.py.

Every route here is a thin wrapper: it calls the exact same functions
dashboard/app.py's "Resume du jour" page and "Analyse d'une action" AI
section already call (build_daily_summary, build_signal, add_argued_texts,
staleness_summary, load_opportunite_for_ticker), and returns their results
as JSON. No scoring, staleness, or Groq-prompting logic is reimplemented
here -- if that logic ever needs to change, it changes in
reasoning/daily_summary.py once, for the CLI, the Streamlit dashboard, and
this API all at once.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies import get_db, normalise_ticker
from graph.build_graph import build_graph, load_relations
from reasoning.daily_summary import (
    TICKER_ANALYSIS_DAILY_LIMIT,
    USAGE_TABLE_TICKER_ANALYSIS,
    add_argued_texts,
    build_daily_summary,
    build_signal,
    load_cached_argument,
    load_opportunite_for_ticker,
    staleness_summary,
)

router = APIRouter(prefix="/api/daily-summary", tags=["daily-summary"])


@router.get("")
def get_daily_summary(conn=Depends(get_db)):
    """Today's top signals -- same data as reasoning/daily_summary.py's CLI
    output and dashboard/app.py's "Resume du jour" page. `dates_by_priority`
    is per-tier (haute/moyenne/basse), never a single flattened date -- see
    build_daily_summary's own docstring for why a single MAX(date_calcul)
    can silently hide a stale tier."""
    signals, dates_by_priority, n_candidates = build_daily_summary(conn)
    return {
        "signals": signals,
        "dates_by_priority": dates_by_priority,
        "n_candidates": n_candidates,
        "staleness": staleness_summary(dates_by_priority),
    }


@router.get("/{ticker}/argued-text")
def get_argued_text(ticker: str, conn=Depends(get_db)):
    """The Groq-written 3-paragraph argued text for `ticker`, reusing
    add_argued_texts() exactly as dashboard/app.py's "Analyse d'une action"
    page does: today's cached text (daily_summary_arguments) is returned
    immediately if present; otherwise this call itself triggers ONE
    generation attempt against the ticker-analysis quota pool
    (USAGE_TABLE_TICKER_ANALYSIS, 10/day) -- unlike the dashboard's
    button-gated UX, a GET here is what actually produces the text, since a
    frontend page load is the natural point to ask for it.

    Never a 5xx for a normal degraded state (quota exhausted, no API key,
    network error): add_argued_texts() itself never raises for those, and
    this route mirrors that -- "source": "unavailable" with
    texte_argumente=None distinguishes it from a real generation.
    404 only when the ticker has no opportunites row at all (never scored,
    or every component failed), matching the dashboard's own "Pas de
    donnees d'opportunite" message."""
    ticker = normalise_ticker(ticker)

    opp_row = load_opportunite_for_ticker(conn, ticker)
    if opp_row is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Aucune donnee d'opportunite pour {ticker}. Lance "
                "`python reasoning/opportunity_scoring.py --priorite toutes` "
                "pour l'inclure."
            ),
        )

    relations = load_relations(conn)
    graph = build_graph(relations)
    signal = build_signal(conn, opp_row, graph, relations)

    today = date.today().isoformat()
    cached = load_cached_argument(conn, today, ticker)
    if cached:
        return {"ticker": ticker, "texte_argumente": cached, "source": "cache"}

    add_argued_texts(
        conn, [signal],
        usage_table=USAGE_TABLE_TICKER_ANALYSIS,
        call_limit=TICKER_ANALYSIS_DAILY_LIMIT,
    )
    texte = signal.get("texte_argumente")
    return {
        "ticker": ticker,
        "texte_argumente": texte,
        "source": "generated" if texte else "unavailable",
    }
