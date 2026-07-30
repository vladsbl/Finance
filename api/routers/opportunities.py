"""GET /api/opportunities/* -- NOT YET IMPLEMENTED.

Planned to wrap reasoning/opportunity_scoring.py + dashboard/app.py's
OPPORTUNITES_SQL/load_opportunites() (the "Opportunites du jour" page:
full-universe table, filterable by universe.priorite). Next page to
migrate after "Resume du jour" -- see api/main.py's module docstring for
the migration order.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/opportunities", tags=["opportunities"])
