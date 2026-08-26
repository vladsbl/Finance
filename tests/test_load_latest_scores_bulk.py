"""Unit tests for reasoning/daily_summary.py's load_latest_scores_bulk() --
the bulk (not N+1) score lookup added for list views (News & Analyse IA,
Raisonnement causal) that need technical/valuation/fondamental-reel scores
for many tickers at once."""

import os
import sqlite3
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from reasoning.daily_summary import load_latest_scores_bulk  # noqa: E402


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE final_scores (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, "
        "price_valuation_score REAL, technical_score REAL, volatility_score REAL, "
        "volume_score REAL, final_score REAL, confidence REAL)"
    )
    conn.execute(
        "CREATE TABLE fundamental_real_scores (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "symbol TEXT, score_global REAL)"
    )
    return conn


def test_empty_ticker_list_returns_empty_dict():
    conn = _make_conn()
    assert load_latest_scores_bulk(conn, []) == {}


def test_missing_ticker_gets_all_none_scores():
    conn = _make_conn()
    result = load_latest_scores_bulk(conn, ["ZZZZ"])
    assert result == {
        "ZZZZ": {"technical_score": None, "price_valuation_score": None, "score_fondamental_reel": None}
    }


def test_fetches_scores_for_a_known_ticker():
    conn = _make_conn()
    conn.execute(
        "INSERT INTO final_scores (symbol, price_valuation_score, technical_score) VALUES (?, ?, ?)",
        ("AAPL", 62.0, 70.0),
    )
    conn.execute(
        "INSERT INTO fundamental_real_scores (symbol, score_global) VALUES (?, ?)",
        ("AAPL", 55.0),
    )
    result = load_latest_scores_bulk(conn, ["AAPL"])
    assert result["AAPL"] == {
        "technical_score": 70.0, "price_valuation_score": 62.0, "score_fondamental_reel": 55.0,
    }


def test_keeps_only_the_most_recent_row_per_symbol():
    conn = _make_conn()
    conn.execute(
        "INSERT INTO final_scores (symbol, price_valuation_score, technical_score) VALUES (?, ?, ?)",
        ("AAPL", 40.0, 40.0),
    )
    conn.execute(
        "INSERT INTO final_scores (symbol, price_valuation_score, technical_score) VALUES (?, ?, ?)",
        ("AAPL", 62.0, 70.0),
    )
    result = load_latest_scores_bulk(conn, ["AAPL"])
    assert result["AAPL"]["technical_score"] == 70.0
    assert result["AAPL"]["price_valuation_score"] == 62.0


def test_handles_many_tickers_in_one_call_not_n_plus_one():
    conn = _make_conn()
    tickers = [f"T{i}" for i in range(50)]
    for i, t in enumerate(tickers):
        conn.execute(
            "INSERT INTO final_scores (symbol, price_valuation_score, technical_score) VALUES (?, ?, ?)",
            (t, float(i), float(i * 2)),
        )
    result = load_latest_scores_bulk(conn, tickers)
    assert len(result) == 50
    assert result["T10"]["price_valuation_score"] == 10.0
    assert result["T10"]["technical_score"] == 20.0


def test_deduplicates_repeated_tickers():
    conn = _make_conn()
    conn.execute(
        "INSERT INTO final_scores (symbol, price_valuation_score, technical_score) VALUES (?, ?, ?)",
        ("AAPL", 62.0, 70.0),
    )
    result = load_latest_scores_bulk(conn, ["AAPL", "AAPL", "AAPL"])
    assert list(result.keys()) == ["AAPL"]
