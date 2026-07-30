"""GET /api/correlations/* -- NOT YET IMPLEMENTED.

Planned to wrap dashboard/app.py's CORRELATIONS_SQL query plus the
dedup/exclusion/badge logic already extracted into standalone functions
there (_dedupe_mirror_correlations, _filter_suspect_relations,
_MEAN_REVERSION_PAIRS, classify_market/is_same_market from
reasoning/correlation_discovery.py) -- all pure functions already, so this
router only needs to call them and shape the result as JSON.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/correlations", tags=["correlations"])
