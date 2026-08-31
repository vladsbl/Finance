"""Unit tests for reasoning/analyze_news.py's zone_geographique field --
added to the existing news-analysis JSON extraction (no new Groq call), and
its migration for a news_analysis table created before the field existed.
In-memory sqlite fixtures, same convention as tests/test_macro_context.py."""

import os
import sqlite3
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from reasoning.analyze_news import (  # noqa: E402
    _ensure_zone_geographique_column,
    load_news,
)


def _make_legacy_conn():
    """A news_raw/news_analysis pair matching the schema BEFORE
    zone_geographique existed -- exactly the shape this project's own real
    dev DB was in (1700+ already-analysed rows) before this feature."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE news_raw (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, "
        "source TEXT, title TEXT, url TEXT, published_at TEXT, summary_brut TEXT, "
        "dedup_key TEXT)"
    )
    conn.execute(
        "CREATE TABLE news_analysis (id INTEGER PRIMARY KEY AUTOINCREMENT, news_id INTEGER, "
        "company TEXT, sector TEXT, importance INTEGER, tonalite TEXT, impact TEXT, "
        "horizon TEXT, confidence REAL, model TEXT, "
        "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    cur = conn.execute(
        "INSERT INTO news_raw (ticker, source, title, url, published_at, summary_brut) "
        "VALUES ('AAPL', 'yahoo_rss', 'Apple reports quarterly earnings', "
        "'http://example/x', '2026-08-20T10:00:00', '')"
    )
    news_id = cur.lastrowid
    conn.execute(
        "INSERT INTO news_analysis (news_id, company, sector, importance, tonalite, impact, "
        "horizon, confidence) VALUES (?, 'Apple', 'Technologie', 7, 'positive', "
        "'impact test', 'court terme', 60)",
        (news_id,),
    )
    conn.commit()
    return conn


def test_ensure_zone_geographique_column_migrates_legacy_table():
    conn = _make_legacy_conn()
    columns_before = {row[1] for row in conn.execute("PRAGMA table_info(news_analysis)")}
    assert "zone_geographique" not in columns_before

    _ensure_zone_geographique_column(conn)

    columns_after = {row[1] for row in conn.execute("PRAGMA table_info(news_analysis)")}
    assert "zone_geographique" in columns_after


def test_ensure_zone_geographique_column_is_idempotent():
    conn = _make_legacy_conn()
    _ensure_zone_geographique_column(conn)
    _ensure_zone_geographique_column(conn)  # must not raise "duplicate column"
    columns = {row[1] for row in conn.execute("PRAGMA table_info(news_analysis)")}
    assert "zone_geographique" in columns


def test_load_news_self_heals_legacy_table_and_returns_none_for_missing_zone():
    """load_news() must work directly against a pre-migration table (it
    calls _ensure_zone_geographique_column itself -- this is the API's
    actual read path into news_analysis, see its own docstring) and must
    surface the pre-existing row's missing zone as None, never crash."""
    conn = _make_legacy_conn()
    rows = load_news(conn)
    assert len(rows) == 1
    assert rows[0]["sector"] == "Technologie"
    assert rows[0]["zone_geographique"] is None


def test_load_news_returns_real_zone_when_present():
    conn = _make_legacy_conn()
    _ensure_zone_geographique_column(conn)
    conn.execute("UPDATE news_analysis SET zone_geographique = 'Etats-Unis'")
    conn.commit()
    rows = load_news(conn)
    assert rows[0]["zone_geographique"] == "Etats-Unis"


def test_load_news_by_ticker_also_includes_zone_geographique():
    conn = _make_legacy_conn()
    _ensure_zone_geographique_column(conn)
    conn.execute("UPDATE news_analysis SET zone_geographique = 'Etats-Unis'")
    conn.commit()
    rows = load_news(conn, ticker="AAPL")
    assert len(rows) == 1
    assert rows[0]["zone_geographique"] == "Etats-Unis"
