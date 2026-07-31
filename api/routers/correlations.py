"""GET /api/correlations -- JSON view of dashboard/app.py's "Correlations
decouvertes" page.

This route is a thin wrapper around reasoning/correlation_discovery.py's
load_correlations, dedupe_mirror_correlations, classify_correlation_badge
and format_lag_direction -- no query, dedup, or classification logic is
reimplemented here. classify_correlation_badge returns a structured
{"type", "severity", "message"} dict (or None) rather than a pre-formatted
string specifically so this route can hand React a real field to badge on,
instead of parsing an emoji out of a sentence.
"""

from fastapi import APIRouter, Depends, Query

from api.dependencies import get_db
from reasoning.correlation_discovery import (
    classify_correlation_badge,
    dedupe_mirror_correlations,
    format_lag_direction,
    load_correlations,
)

router = APIRouter(prefix="/api/correlations", tags=["correlations"])

DEFAULT_LIMIT = 50
MAX_LIMIT = 500


def _row_to_dict(row):
    return {
        "id": row["id"],
        "ticker_source": row["ticker_source"],
        "nom_source": row["nom_source"],
        "ticker_target": row["ticker_target"],
        "nom_target": row["nom_target"],
        "relation_type": row["relation_type"],
        "source_table": row["source_table"],
        "lag": row["lag"],
        "lag_direction": row["lag_direction"],
        "lag_label": format_lag_direction(row),
        "coefficient": row["coefficient"],
        "p_value": row["p_value"],
        "p_value_corrigee": row["p_value_corrigee"],
        "n_observations": row["n_observations"],
        "methode": row["methode"],
        "correction": row["correction"],
        "meme_marche": bool(row["meme_marche"]),
        "badge": classify_correlation_badge(row),
        "created_at": row["created_at"],
    }


@router.get("")
def get_correlations(
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Lignes par page"),
    offset: int = Query(0, ge=0, description="Nombre de lignes a sauter"),
    conn=Depends(get_db),
):
    """Every stored correlation, strongest first (ABS(coefficient) DESC,
    same order load_correlations's own SQL already produces -- dedup
    preserves that order, so this never re-sorts), with KG-symmetric
    mirror pairs collapsed into one row each (dedupe_mirror_correlations)
    exactly like the Streamlit page.

    `n_before_dedup` is the raw stored row count (before collapsing mirror
    pairs) -- the Streamlit page shows both numbers ("N retenues... M
    affichees") so this route does too, letting the frontend reproduce
    that same caption. `limit`/`offset` paginate the already-deduped,
    already-sorted list, same convention as /api/opportunities."""
    rows = load_correlations(conn)
    n_before_dedup = len(rows)
    deduped = dedupe_mirror_correlations(rows)

    n_total = len(deduped)
    page = deduped[offset:offset + limit]

    return {
        "correlations": [_row_to_dict(r) for r in page],
        "n_before_dedup": n_before_dedup,
        "n_total": n_total,
        "limit": limit,
        "offset": offset,
    }
