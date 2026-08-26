"""Unit tests for reasoning/company_description.py -- the cache/usage
plumbing is pure sqlite3, tested against an in-memory DB (no Groq/network)."""

import os
import sqlite3
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from reasoning.company_description import (  # noqa: E402
    build_company_description_prompt,
    bump_usage,
    get_or_generate_company_description,
    get_usage,
    load_cached_description,
    save_description,
)


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE universe (ticker TEXT PRIMARY KEY, nom TEXT, nom_entreprise TEXT, devise TEXT, priorite TEXT)")
    conn.execute("CREATE TABLE ticker_sector_cache (ticker TEXT PRIMARY KEY, sector TEXT, industry TEXT)")
    return conn


def test_build_prompt_includes_known_sector_and_industry():
    prompt = build_company_description_prompt("Apple Inc.", "AAPL", "Technology", "Consumer Electronics")
    assert "Apple Inc." in prompt
    assert "AAPL" in prompt
    assert "Technology" in prompt
    assert "Consumer Electronics" in prompt


def test_build_prompt_handles_missing_sector_gracefully():
    prompt = build_company_description_prompt("Some Corp", "XYZ", None, None)
    assert "non precise" in prompt
    assert "non precisee" in prompt


def test_cache_roundtrip():
    conn = _make_conn()
    conn.execute(
        "CREATE TABLE company_descriptions (ticker TEXT PRIMARY KEY, description TEXT NOT NULL, "
        "model TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    assert load_cached_description(conn, "AAPL") is None
    save_description(conn, "AAPL", "Une description.", "test-model")
    assert load_cached_description(conn, "AAPL") == "Une description."


def test_usage_counter_increments():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE llm_usage_company_description (day TEXT PRIMARY KEY, calls INTEGER NOT NULL DEFAULT 0)")
    today = "2026-08-25"
    assert get_usage(conn, today) == 0
    bump_usage(conn, today)
    bump_usage(conn, today)
    assert get_usage(conn, today) == 2


def test_unknown_ticker_returns_not_found():
    conn = _make_conn()
    found, result = get_or_generate_company_description(conn, "NOPE")
    assert found is False
    assert result is None


def test_known_ticker_never_calls_groq_during_pytest():
    """PYTEST_CURRENT_TEST is set automatically by pytest -- the function
    must short-circuit to "unavailable" rather than attempt a real Groq
    call, exactly like daily_summary.py's add_argued_texts."""
    conn = _make_conn()
    conn.execute(
        "INSERT INTO universe (ticker, nom, nom_entreprise, devise, priorite) "
        "VALUES ('AAPL', 'Apple', 'Apple Inc.', 'USD', 'haute')"
    )
    found, result = get_or_generate_company_description(conn, "AAPL")
    assert found is True
    assert result["source"] == "unavailable"
    assert result["description"] is None


def test_cached_description_returned_without_regenerating():
    conn = _make_conn()
    conn.execute(
        "INSERT INTO universe (ticker, nom, nom_entreprise, devise, priorite) "
        "VALUES ('AAPL', 'Apple', 'Apple Inc.', 'USD', 'haute')"
    )
    conn.execute(
        "CREATE TABLE company_descriptions (ticker TEXT PRIMARY KEY, description TEXT NOT NULL, "
        "model TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    save_description(conn, "AAPL", "Description en cache.", "test-model")

    found, result = get_or_generate_company_description(conn, "AAPL")
    assert found is True
    assert result == {"ticker": "AAPL", "description": "Description en cache.", "source": "cache"}
