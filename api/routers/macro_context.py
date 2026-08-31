"""GET /api/macro-context -- JSON view of reasoning/macro_context.py's
daily "Contexte geopolitique et economique mondial" briefing (Phase 4 V1).

Thin wrapper, same convention as every other router in this package: no
source-gathering, filtering, caching or Groq-prompting logic reimplemented
here, only routing + JSON shaping. See reasoning/macro_context.py's module
docstring for the full design (official Fed/ECB RSS + a keyword filter over
already-analysed company news, one Groq call/day, cached per calendar day).
"""

from fastapi import APIRouter, Depends

from api.dependencies import get_db
from reasoning.macro_context import get_or_generate_macro_context

router = APIRouter(prefix="/api/macro-context", tags=["macro-context"])


@router.get("")
def get_macro_context(conn=Depends(get_db)):
    """Today's macro/geopolitical briefing. Never a 5xx for a normal
    degraded state (no sources collected yet, no API key, quota exhausted,
    network error): get_or_generate_macro_context never raises for those --
    "source": "unavailable" with texte=None distinguishes it from a real
    generation, same convention as /api/daily-summary/{ticker}/argued-text
    and /api/news/{news_id}/narrative. No 404 case here (unlike those two
    ticker/news-scoped routes): this route is never scoped to a missing
    entity, so "no sources today" is itself just one more `source` state,
    not an error."""
    return get_or_generate_macro_context(conn)
