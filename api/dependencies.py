"""Shared FastAPI dependencies -- kept deliberately tiny.

This module owns NO business logic: every route handler imports and calls
functions already written in reasoning/*.py, graph/*.py, etc. (the exact
same functions dashboard/app.py already calls). The only thing this API
layer adds is (a) a per-request sqlite3 connection and (b) JSON shaping --
see the module docstring of api/main.py.
"""

import sqlite3

from fastapi import HTTPException

from reasoning.daily_summary import DB_PATH


def get_db():
    """One sqlite3 connection per request, closed when the request ends --
    same open-per-call/close-in-finally pattern every reasoning/*.py and
    dashboard/app.py loader already uses (no connection pool anywhere in
    this codebase to stay consistent with)."""
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


def normalise_ticker(ticker: str) -> str:
    """Tickers are stored upper-case in `universe` (see
    universe/build_universe.py) -- trims/upper-cases a path param so
    "/api/daily-summary/aapl/argued-text" and ".../AAPL/..." both resolve
    to the same row instead of the lower-case variant silently matching
    nothing."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        raise HTTPException(status_code=422, detail="Ticker manquant.")
    return ticker
