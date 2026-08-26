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
from reasoning.direction_probability import (
    compute_direction_probabilities,
    dominant_direction,
    load_causal_effects_bulk,
)

router = APIRouter(prefix="/api/opportunities", tags=["opportunities"])

VALID_PRIORITES = {"toutes", "haute", "moyenne", "basse"}
VALID_DIRECTIONS = {"toutes", "hausse", "stagnation", "baisse"}


def _row_to_dict(row, causal_effects):
    """`causal_effects` is the WHOLE {ticker: {...}} dict from
    load_causal_effects_bulk() -- computed ONCE per request (see
    get_opportunities below), not once per row: `opportunites` already
    carries score_technique/score_prix_valorisation/score_fondamental_reel
    as denormalized columns (opportunity_scoring.py writes them at scoring
    time), so the only per-row work compute_direction_probabilities needs
    that ISN'T already sitting in `row` is this bulk causal-effects lookup
    -- a pure dict .get(), not a query."""
    causal = causal_effects.get(row["ticker"])
    direction = compute_direction_probabilities(
        score_technique=row["score_technique"],
        score_prix_valorisation=row["score_prix_valorisation"],
        score_fondamental_reel=row["score_fondamental_reel"],
        causal_effect=causal["effet"] if causal else None,
        causal_confidence=causal["confiance"] if causal else None,
    )
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
        "direction_probabilities": direction,
    }


DEFAULT_LIMIT = 50
MAX_LIMIT = 500


@router.get("")
def get_opportunities(
    priorite: str = Query("toutes", description="toutes | haute | moyenne | basse"),
    direction: str = Query("toutes", description="toutes | hausse | stagnation | baisse"),
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

    `direction` filters on each row's DOMINANT hausse/stagnation/baisse
    scenario -- applied server-side, BEFORE pagination, because this list
    genuinely paginates (up to ~2000 rows across many pages): a client-side
    filter would only ever narrow down whatever page happens to be loaded,
    silently hiding matches sitting on other pages. `direction_probabilities`
    is computed for every row from data already present in it (see
    _row_to_dict) plus ONE bulk causal-effects lookup for the whole
    request, never a per-row query -- this stays a pure, Groq-free
    computation just like everywhere else it's used.

    `limit`/`offset` paginate the FULL, already-sorted AND already-filtered
    result set -- sorting happens once, up front, over every matching row,
    so page N always reflects the true global rank (never re-sorted
    per-page, which could otherwise put the same ticker on two different
    pages if scores tie). `n_total` is always the COMPLETE count across
    every page (not just this page's length), so the client can compute
    how many pages exist."""
    priorite = (priorite or "toutes").strip().lower()
    if priorite not in VALID_PRIORITES:
        raise HTTPException(
            status_code=422,
            detail=f"priorite invalide : {priorite!r} (attendu : {sorted(VALID_PRIORITES)})",
        )
    direction = (direction or "toutes").strip().lower()
    if direction not in VALID_DIRECTIONS:
        raise HTTPException(
            status_code=422,
            detail=f"direction invalide : {direction!r} (attendu : {sorted(VALID_DIRECTIONS)})",
        )

    dates_by_priority = resolve_data_dates_by_priority(conn)
    if priorite != "toutes":
        dates_by_priority = {
            k: v for k, v in dates_by_priority.items() if k == priorite
        }

    rows = load_opportunites_multi(conn, dates_by_priority)
    causal_effects = load_causal_effects_bulk(conn)
    opportunites = [_row_to_dict(r, causal_effects) for r in rows]
    if direction != "toutes":
        opportunites = [
            o for o in opportunites
            if o["direction_probabilities"] is not None
            and dominant_direction(o["direction_probabilities"]) == direction
        ]
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
