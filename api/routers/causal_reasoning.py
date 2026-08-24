"""GET/POST /api/causal-reasoning/* -- JSON view of dashboard/app.py's
"Raisonnement causal" page.

Every route here is a thin wrapper around reasoning/causal_reasoning.py
(load_causal_chains, parse_entreprises_impactees, causal_reasoning_status,
run_causal_reasoning) and reasoning/daily_summary.py's staleness_note -- no
selection, quota, or Groq-prompting logic is reimplemented here.
run_causal_reasoning is the exact same function dashboard/app.py's
"Recalculer maintenant" button calls, so a click here and a click on the
Streamlit page can never double-count or disagree about what "already
processed" means (same USAGE_TABLE_CAUSAL quota table, same
load_eligible_news selection).
"""

from fastapi import APIRouter, Depends

from api.dependencies import get_db
from reasoning.causal_reasoning import (
    CAUSAL_CHAIN_DISPLAY_LIMIT,
    causal_reasoning_status,
    load_causal_chains,
    parse_entreprises_impactees,
    run_causal_reasoning,
)
from reasoning.daily_summary import staleness_note

router = APIRouter(prefix="/api/causal-reasoning", tags=["causal-reasoning"])


def _chain_to_dict(chain):
    return {
        "id": chain["id"],
        "news_id": chain["news_id"],
        "news_title": chain["news_title"],
        "ticker_source": chain["ticker_source"],
        "chaine_raisonnement": chain["chaine_raisonnement"],
        "entreprises_impactees": parse_entreprises_impactees(chain["entreprises_impactees"]),
        "confiance": chain["confiance"],
        "model": chain["model"],
        "created_at": chain["created_at"],
    }


@router.get("")
def get_causal_chains(limit: int = CAUSAL_CHAIN_DISPLAY_LIMIT, conn=Depends(get_db)):
    """Stored causal chains, most recently generated first -- never scoped
    to "today" (see load_causal_chains's own docstring: this module runs on
    its own tightly-capped daily quota, so a chain from a few days ago is
    still the right thing to show). `entreprises_impactees` comes back as a
    structured list (parsed from its JSON column), not a raw string, so
    React can render each entry's `effet` (positif/negatif/neutre) as a
    real field instead of re-parsing JSON client-side.

    `staleness` mirrors /api/opportunities' own field: a human-readable
    note when the most recent chain is more than a few days old, null when
    fresh or when there are no chains at all."""
    chains = load_causal_chains(conn, limit)
    latest_date = (chains[0]["created_at"] or "")[:10] if chains else None
    return {
        "chains": [_chain_to_dict(c) for c in chains],
        "n_total": len(chains),
        "staleness": staleness_note(latest_date) if latest_date else None,
    }


@router.get("/status")
def get_causal_reasoning_status(conn=Depends(get_db)):
    """(n_pending, quota_used, quota_limit, quota_remaining) for the
    "Recalculer maintenant" button -- recomputed fresh on every call (never
    cached), same as the Streamlit page, so the numbers are accurate right
    before a caller decides whether to offer/enable the button."""
    n_pending, quota_used, quota_limit, quota_remaining = causal_reasoning_status(conn)
    return {
        "n_pending": n_pending,
        "quota_used": quota_used,
        "quota_limit": quota_limit,
        "quota_remaining": quota_remaining,
    }


@router.post("/run")
def post_causal_reasoning_run(conn=Depends(get_db)):
    """Runs run_causal_reasoning(conn) synchronously and returns its stats
    dict as-is -- already JSON-shaped (see that function's own docstring).
    Quota-capped at CAUSAL_REASONING_DAILY_LIMIT (5/day), so unlike
    /api/pipeline/run this never takes more than a handful of Groq calls
    and needs no background job: the Streamlit button calls the exact same
    function the exact same way, synchronously.

    Never a 5xx for a normal degraded state (quota exhausted, no eligible
    news, no API key, a Groq failure) -- stats["error"] carries a non-None
    message only for a genuine setup failure (missing GROQ_API_KEY, client
    unavailable), matching run_causal_reasoning's own "never raises"
    contract; the route mirrors that by always returning 200."""
    return run_causal_reasoning(conn)
