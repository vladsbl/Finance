"""GET /api/stock/* -- NOT YET IMPLEMENTED.

Planned to wrap dashboard/app.py's "Analyse d'une action" page: per-ticker
detail (load_ticker_detail), price/MA chart data (load_price_series), and
the on-demand AI analysis section -- the latter already has its
generation/caching logic exposed via
GET /api/daily-summary/{ticker}/argued-text (shared with "Resume du jour"),
so this router only needs to add the detail/chart data routes.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/stock", tags=["stock"])
