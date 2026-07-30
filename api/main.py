#!/usr/bin/env python3
"""FastAPI JSON API -- common backend for the future React frontend (web,
then a Windows app via Tauri, then an iPhone PWA), built entirely on top of
this project's EXISTING reasoning/graph/analysis modules.

This app owns NO business logic of its own: every route (see
api/routers/*.py) imports and calls functions already written for the
Streamlit dashboard (dashboard/app.py) and the CLI scripts -- the exact
same build_daily_summary(), add_argued_texts(), build_graph(), etc. If a
scoring rule, a quota, or a prompt ever needs to change, it changes once in
reasoning/*.py or graph/*.py, and every consumer (CLI, Streamlit, this API)
picks it up automatically. This module and api/routers/*.py add nothing
but: routing, a per-request DB connection (api/dependencies.py), CORS, and
shaping return values as JSON.

Streamlit (dashboard/app.py) keeps running unmodified and untouched during
the whole migration -- this is an ADDITIONAL, parallel entry point onto the
same data/marketdb.db, not a replacement. Nothing here writes to
dashboard/app.py or changes its behaviour.

Migration order (routers/*.py are pre-created as empty skeletons for all
of these, one gets filled in per migration step):
    1. Resume du jour       (api/routers/daily_summary.py)  -- DONE
    2. Opportunites du jour (api/routers/opportunities.py)
    3. Analyse d'une action (api/routers/stock.py)
    4. Knowledge Graph      (api/routers/graph.py)
    5. Correlations         (api/routers/correlations.py)
    6. Raisonnement causal  (api/routers/causal_reasoning.py)
    7. News & Analyse IA    (api/routers/news.py)

Run locally (from the repo root, with the project's venv active):
    uvicorn api.main:app --reload --port 8000

Runs happily alongside Streamlit (`streamlit run dashboard/app.py`, default
port 8501) -- different port, same sqlite file, both just open/close their
own short-lived connections per request/page-load like every script in
this project already does (no shared connection, no lock contention beyond
what sqlite itself already handles for the daily pipeline + dashboard
today).

Interactive API docs once running: http://localhost:8000/docs
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from api.routers import (  # noqa: E402
    causal_reasoning,
    correlations,
    daily_summary,
    graph,
    news,
    opportunities,
    stock,
)

app = FastAPI(
    title="Finance API",
    description=(
        "JSON API over the Finance project's existing analysis pipeline -- "
        "see this module's docstring for the no-duplicated-logic design "
        "and the page-by-page migration plan."
    ),
    version="0.1.0",
)

# Dev-only origins: the future React app's local dev servers (Create React
# App defaults to :3000, Vite to :5173). Tighten this list (or read it from
# an env var) before any non-local deployment -- allow_credentials=True
# together with a wildcard "*" origin is rejected by browsers anyway, so an
# explicit list is required as soon as credentials/cookies are involved.
DEV_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=DEV_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(daily_summary.router)
app.include_router(opportunities.router)
app.include_router(stock.router)
app.include_router(graph.router)
app.include_router(correlations.router)
app.include_router(causal_reasoning.router)
app.include_router(news.router)


@app.get("/api/health")
def health():
    """Liveness check -- confirms the API process is up and can see
    data/marketdb.db, without depending on any specific table existing yet
    (a fresh clone with no pipeline run at all should still report ok)."""
    db_exists = os.path.exists(os.path.join(REPO_ROOT, "data", "marketdb.db"))
    return {"status": "ok", "db_found": db_exists}
