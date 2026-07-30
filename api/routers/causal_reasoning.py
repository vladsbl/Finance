"""GET/POST /api/causal-reasoning/* -- NOT YET IMPLEMENTED.

Planned to wrap reasoning/causal_reasoning.py: GET for the stored
causal_chains list, POST for the "Recalculer maintenant" action
(run_causal_reasoning(conn), the same function dashboard/app.py's recalc
button calls -- quota-aware, never raises, returns a stats dict already
JSON-shaped as-is).
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/causal-reasoning", tags=["causal-reasoning"])
