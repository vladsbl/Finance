"""GET /api/opportunities -- JSON view of dashboard/app.py's "Opportunites
du jour" page.

Reuses reasoning/daily_summary.py's resolve_data_dates_by_priority() (the
per-priorite-tier latest date_calcul resolution -- see that function's own
docstring for why a single flat MAX(date_calcul) silently hides a stale
tier) and load_opportunites_multi() for the actual rows. No scoring or
freshness logic is reimplemented here: if either ever changes, it changes
once in reasoning/daily_summary.py for the CLI, Streamlit, and this API at
once.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from api.dependencies import get_db
from reasoning.daily_summary import (
    load_opportunites_multi,
    resolve_data_dates_by_priority,
    staleness_summary,
)

router = APIRouter(prefix="/api/opportunities", tags=["opportunities"])

VALID_PRIORITES = {"toutes", "haute", "moyenne", "basse"}


def _row_to_dict(row):
    return {
        "ticker": row["ticker"],
        "nom_affiche": row["nom_affiche"],
        "priorite": row["priorite"],
        "score_global": row["score_global"],
        "score_prix_valorisation": row["score_prix_valorisation"],
        "score_technique": row["score_technique"],
        "score_news": row["score_news"],
        "score_fondamental_reel": row["score_fondamental_reel"],
        "confiance": row["confiance"],
        "explication": row["explication"],
        "date_calcul": row["date_calcul"],
    }


DEFAULT_LIMIT = 50
MAX_LIMIT = 500


@router.get("")
def get_opportunities(
    priorite: str = Query("toutes", description="toutes | haute | moyenne | basse"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Lignes par page"),
    offset: int = Query(0, ge=0, description="Nombre de lignes a sauter"),
    conn=Depends(get_db),
):
    """All scored tickers (any priorite tier by default, or one specific
    tier), each tier resolved to its OWN latest date_calcul, sorted by
    score_global descending -- same row set and same sort as
    dashboard/app.py's OPPORTUNITES_SQL, just optionally pre-filtered to
    one tier server-side instead of client-side (dashboard/app.py loads
    every tier then filters the dataframe in Streamlit; filtering the
    `dates_by_priority` dict before querying here avoids fetching rows the
    caller didn't ask for).

    `limit`/`offset` paginate the FULL, already-sorted result set -- sorting
    happens once, up front, over every matching row, so page N always
    reflects the true global rank (never re-sorted per-page, which could
    otherwise put the same ticker on two different pages if scores tie).
    `n_total` is always the COMPLETE count across every page (not just this
    page's length), so the client can compute how many pages exist."""
    priorite = (priorite or "toutes").strip().lower()
    if priorite not in VALID_PRIORITES:
        raise HTTPException(
            status_code=422,
            detail=f"priorite invalide : {priorite!r} (attendu : {sorted(VALID_PRIORITES)})",
        )

    dates_by_priority = resolve_data_dates_by_priority(conn)
    if priorite != "toutes":
        dates_by_priority = {
            k: v for k, v in dates_by_priority.items() if k == priorite
        }

    rows = load_opportunites_multi(conn, dates_by_priority)
    opportunites = [_row_to_dict(r) for r in rows]
    opportunites.sort(
        key=lambda o: (o["score_global"] is None, -(o["score_global"] or 0))
    )

    n_total = len(opportunites)
    page = opportunites[offset:offset + limit]

    return {
        "opportunites": page,
        "dates_by_priority": dates_by_priority,
        "staleness": staleness_summary(dates_by_priority),
        "n_total": n_total,
        "limit": limit,
        "offset": offset,
    }
