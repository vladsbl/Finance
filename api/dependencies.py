"""Shared FastAPI dependencies -- kept deliberately tiny.

This module owns NO business logic: every route handler imports and calls
functions already written in reasoning/*.py, graph/*.py, etc. (the exact
same functions dashboard/app.py already calls). The only thing this API
layer adds is (a) a per-request sqlite3 connection and (b) JSON shaping --
see the module docstring of api/main.py.
"""

import sqlite3
from datetime import date

from fastapi import HTTPException

from graph.build_graph import build_graph, load_relations
from reasoning.daily_summary import (
    DB_PATH,
    TICKER_ANALYSIS_DAILY_LIMIT,
    USAGE_TABLE_TICKER_ANALYSIS,
    add_argued_texts,
    build_signal,
    load_cached_argument,
    load_opportunite_for_ticker,
)


def get_db():
    """One sqlite3 connection per request, closed when the request ends --
    same open-per-call/close-in-finally pattern every reasoning/*.py and
    dashboard/app.py loader already uses (no connection pool anywhere in
    this codebase to stay consistent with).

    check_same_thread=False: this generator dependency and the route
    handler that consumes its connection are each dispatched to FastAPI's
    threadpool independently (starlette's run_in_threadpool), so the two
    can land on different worker threads even within one request -- sqlite3
    otherwise raises "SQLite objects created in a thread can only be used
    in that same thread" as soon as two requests are in flight
    concurrently. Still safe here: the connection is never shared between
    requests, only (potentially) handed from one worker thread to another
    within its own single request's lifecycle."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
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


def get_or_generate_argued_text(conn, ticker):
    """Shared by /api/daily-summary/{ticker}/argued-text and
    /api/stock/{ticker}/argued-text -- both pages expose the exact same
    on-demand AI analysis (same quota pool, same cache table), so this is
    the one place the cache-then-generate flow is written.

    Returns (found, result) where found=False means `ticker` has no
    opportunites row at all (caller should 404); otherwise result is
    {"ticker", "texte_argumente", "source"} with source one of
    "cache" | "generated" | "unavailable" (quota exhausted, no API key, or
    a network error -- add_argued_texts() never raises for those)."""
    opp_row = load_opportunite_for_ticker(conn, ticker)
    if opp_row is None:
        return False, None

    relations = load_relations(conn)
    graph = build_graph(relations)
    signal = build_signal(conn, opp_row, graph, relations)

    today = date.today().isoformat()
    cached = load_cached_argument(conn, today, ticker)
    if cached:
        return True, {"ticker": ticker, "texte_argumente": cached, "source": "cache"}

    add_argued_texts(
        conn, [signal],
        usage_table=USAGE_TABLE_TICKER_ANALYSIS,
        call_limit=TICKER_ANALYSIS_DAILY_LIMIT,
    )
    texte = signal.get("texte_argumente")
    return True, {
        "ticker": ticker,
        "texte_argumente": texte,
        "source": "generated" if texte else "unavailable",
    }
