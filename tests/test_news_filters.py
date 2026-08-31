"""Unit tests for reasoning/analyze_news.py's News & Analyse IA filter
helpers (filter_news_by_search/_sector/_zone, _normalize_zone,
load_news_facets) -- pure functions over plain dicts / an in-memory
sqlite DB, same convention as tests/test_analyze_news_zone.py."""

import os
import sqlite3
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from reasoning.analyze_news import (  # noqa: E402
    _normalize_zone,
    filter_news_by_search,
    filter_news_by_sector,
    filter_news_by_zone,
    load_news_facets,
)


def _row(**overrides):
    base = {
        "news_id": 1, "ticker": "AAPL", "title": "Apple reports record earnings",
        "company": "Apple Inc.", "sector": "Technologie", "zone_geographique": "Etats-Unis",
    }
    base.update(overrides)
    return base


# --- filter_news_by_search ---------------------------------------------------

def test_search_matches_title():
    rows = [_row(title="Apple beats earnings estimates")]
    assert filter_news_by_search(rows, "beats") == rows


def test_search_matches_company():
    rows = [_row(company="Samsung Electronics")]
    assert filter_news_by_search(rows, "samsung") == rows


def test_search_matches_sector():
    rows = [_row(sector="Biotechnologie")]
    assert filter_news_by_search(rows, "biotech") == rows


def test_search_matches_zone():
    rows = [_row(zone_geographique="Moyen-Orient")]
    assert filter_news_by_search(rows, "moyen-orient") == rows


def test_search_matches_ticker():
    rows = [_row(ticker="TSM")]
    assert filter_news_by_search(rows, "tsm") == rows


def test_search_is_case_insensitive_and_partial():
    rows = [_row(title="Nvidia Unveils New AI Chip")]
    assert filter_news_by_search(rows, "NVIDIA") == rows
    assert filter_news_by_search(rows, "ai chip") == rows


def test_search_excludes_non_matching_rows():
    rows = [_row(title="Apple reports record earnings")]
    assert filter_news_by_search(rows, "tesla") == []


def test_search_handles_none_fields_without_crashing():
    rows = [_row(sector=None, zone_geographique=None, company=None)]
    assert filter_news_by_search(rows, "apple") == rows  # still matches on title
    assert filter_news_by_search(rows, "nonexistent") == []


def test_search_empty_or_whitespace_returns_all_rows_unchanged():
    rows = [_row(), _row(news_id=2, title="Something else entirely")]
    assert filter_news_by_search(rows, None) == rows
    assert filter_news_by_search(rows, "") == rows
    assert filter_news_by_search(rows, "   ") == rows


# --- filter_news_by_sector ---------------------------------------------------

def test_sector_filter_exact_match_case_insensitive():
    rows = [_row(sector="Technologie")]
    assert filter_news_by_sector(rows, "technologie") == rows
    assert filter_news_by_sector(rows, "TECHNOLOGIE") == rows


def test_sector_filter_no_partial_match():
    rows = [_row(sector="Technologie")]
    assert filter_news_by_sector(rows, "Techno") == []


def test_sector_filter_excludes_rows_with_no_sector():
    rows = [_row(sector=None), _row(news_id=2, sector="")]
    assert filter_news_by_sector(rows, "Technologie") == []


def test_sector_filter_empty_returns_all_rows_unchanged():
    rows = [_row(), _row(news_id=2, sector=None)]
    assert filter_news_by_sector(rows, None) == rows
    assert filter_news_by_sector(rows, "") == rows


# --- _normalize_zone / filter_news_by_zone -----------------------------------

def test_normalize_zone_strips_accents_and_casefolds():
    assert _normalize_zone("États-Unis") == _normalize_zone("Etats-Unis")
    assert _normalize_zone("ÉTATS-UNIS") == _normalize_zone("etats-unis")


def test_zone_filter_matches_across_accent_variants():
    rows = [_row(zone_geographique="États-Unis"), _row(news_id=2, zone_geographique="Etats-Unis")]
    result = filter_news_by_zone(rows, "Etats-Unis")
    assert len(result) == 2
    result2 = filter_news_by_zone(rows, "États-Unis")
    assert len(result2) == 2


def test_zone_filter_excludes_rows_with_no_zone():
    rows = [_row(zone_geographique=None), _row(news_id=2, zone_geographique="")]
    assert filter_news_by_zone(rows, "Etats-Unis") == []


def test_zone_filter_empty_returns_all_rows_unchanged():
    rows = [_row(), _row(news_id=2, zone_geographique=None)]
    assert filter_news_by_zone(rows, None) == rows
    assert filter_news_by_zone(rows, "") == rows


def test_zone_filter_excludes_non_matching_zone():
    rows = [_row(zone_geographique="Asie")]
    assert filter_news_by_zone(rows, "Etats-Unis") == []


# --- load_news_facets ---------------------------------------------------------

def _make_conn_with_analysis():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE news_analysis (id INTEGER PRIMARY KEY AUTOINCREMENT, news_id INTEGER, "
        "company TEXT, sector TEXT, zone_geographique TEXT, importance INTEGER, "
        "tonalite TEXT, impact TEXT, horizon TEXT, confidence REAL)"
    )
    return conn


def _insert_analysis(conn, sector, zone):
    conn.execute(
        "INSERT INTO news_analysis (news_id, sector, zone_geographique) VALUES "
        "((SELECT COALESCE(MAX(news_id), 0) + 1 FROM news_analysis), ?, ?)",
        (sector, zone),
    )
    conn.commit()


def test_load_news_facets_counts_and_orders_by_frequency():
    conn = _make_conn_with_analysis()
    for _ in range(3):
        _insert_analysis(conn, "Technologie", "Etats-Unis")
    _insert_analysis(conn, "Energie", "Asie")
    facets = load_news_facets(conn)
    assert facets["sectors"][0] == {"value": "Technologie", "count": 3}
    assert facets["sectors"][1] == {"value": "Energie", "count": 1}


def test_load_news_facets_merges_zone_accent_variants_into_one_option():
    conn = _make_conn_with_analysis()
    _insert_analysis(conn, "Technologie", "États-Unis")
    _insert_analysis(conn, "Technologie", "États-Unis")
    _insert_analysis(conn, "Technologie", "Etats-Unis")
    facets = load_news_facets(conn)
    assert len(facets["zones"]) == 1
    assert facets["zones"][0] == {"value": "États-Unis", "count": 3}  # most frequent spelling wins


def test_load_news_facets_excludes_null_and_empty_values():
    conn = _make_conn_with_analysis()
    _insert_analysis(conn, None, None)
    _insert_analysis(conn, "", "")
    _insert_analysis(conn, "Technologie", "Asie")
    facets = load_news_facets(conn)
    assert facets["sectors"] == [{"value": "Technologie", "count": 1}]
    assert facets["zones"] == [{"value": "Asie", "count": 1}]


def test_load_news_facets_empty_table_returns_empty_lists():
    conn = _make_conn_with_analysis()
    facets = load_news_facets(conn)
    assert facets == {"sectors": [], "zones": []}
