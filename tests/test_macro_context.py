"""Unit tests for reasoning/macro_context.py -- Phase 4 V1 ("Contexte
geopolitique et economique mondial"). In-memory sqlite fixtures, same
convention as tests/test_load_latest_scores_bulk.py -- these must never
depend on network access or burn real Groq quota."""

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import json

from reasoning.macro_context import (  # noqa: E402
    MACRO_KEYWORD_PATTERN,
    MACRO_LOOKBACK_FALLBACK_HOURS,
    MACRO_LOOKBACK_HOURS,
    _ensure_two_tier_columns,
    _parse_macro_completion,
    build_macro_context_prompt,
    gather_macro_sources,
    get_or_generate_macro_context,
    load_macro_relevant_company_news,
    load_recent_macro_news,
    sources_for_display,
)


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE macro_news (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, "
        "title TEXT, url TEXT, published_at TEXT, content_raw TEXT, dedup_key TEXT)"
    )
    conn.execute(
        "CREATE TABLE news_raw (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, "
        "source TEXT, title TEXT, url TEXT, published_at TEXT, summary_brut TEXT, "
        "dedup_key TEXT)"
    )
    conn.execute(
        "CREATE TABLE news_analysis (id INTEGER PRIMARY KEY AUTOINCREMENT, news_id INTEGER, "
        "company TEXT, sector TEXT, importance INTEGER, tonalite TEXT, impact TEXT, "
        "horizon TEXT, confidence REAL)"
    )
    return conn


def _iso(hours_ago):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


# --- MACRO_KEYWORD_PATTERN ---------------------------------------------------

def test_keyword_pattern_matches_central_bank_terms():
    assert MACRO_KEYWORD_PATTERN.search("Fed signals rate cut in September")
    assert MACRO_KEYWORD_PATTERN.search("La BCE maintient ses taux directeurs")
    assert MACRO_KEYWORD_PATTERN.search("OPEC agrees to cut oil output")
    assert MACRO_KEYWORD_PATTERN.search("Tensions geopolitiques en mer Rouge")


def test_keyword_pattern_does_not_match_routine_company_news():
    assert not MACRO_KEYWORD_PATTERN.search("Apple unveils new iPhone lineup")
    assert not MACRO_KEYWORD_PATTERN.search("Nvidia beats earnings estimates")


# --- load_recent_macro_news ---------------------------------------------------

def test_load_recent_macro_news_filters_by_window():
    conn = _make_conn()
    conn.execute(
        "INSERT INTO macro_news (source, title, url, published_at, content_raw) "
        "VALUES (?, ?, ?, ?, ?)",
        ("fed", "Fresh Fed release", "http://fed.example/1", _iso(2), "..."),
    )
    conn.execute(
        "INSERT INTO macro_news (source, title, url, published_at, content_raw) "
        "VALUES (?, ?, ?, ?, ?)",
        ("ecb", "Old ECB release", "http://ecb.example/1", _iso(500), "..."),
    )
    since = _iso(MACRO_LOOKBACK_HOURS)
    items = load_recent_macro_news(conn, since)
    assert len(items) == 1
    assert items[0]["title"] == "Fresh Fed release"


def test_load_recent_macro_news_missing_table_returns_empty():
    conn = sqlite3.connect(":memory:")  # no macro_news table at all
    assert load_recent_macro_news(conn, _iso(48)) == []


# --- load_macro_relevant_company_news -----------------------------------------

def _insert_news(conn, ticker, title, published_at, importance, sector="Technologie"):
    cur = conn.execute(
        "INSERT INTO news_raw (ticker, source, title, url, published_at, summary_brut) "
        "VALUES (?, 'yahoo_rss', ?, 'http://example/x', ?, '')",
        (ticker, title, published_at),
    )
    news_id = cur.lastrowid
    conn.execute(
        "INSERT INTO news_analysis (news_id, company, sector, importance, tonalite, impact) "
        "VALUES (?, ?, ?, ?, 'neutre', 'impact test')",
        (news_id, ticker, sector, importance),
    )
    conn.commit()
    return news_id


def test_company_news_macro_filter_keeps_only_keyword_matches():
    conn = _make_conn()
    _insert_news(conn, "OXY", "Oil prices drop after OPEC decision", _iso(2), importance=8)
    _insert_news(conn, "AAPL", "Apple launches new product line", _iso(2), importance=8)
    items = load_macro_relevant_company_news(conn, _iso(MACRO_LOOKBACK_HOURS))
    tickers = {it["ticker"] for it in items}
    assert tickers == {"OXY"}


def test_company_news_macro_filter_respects_importance_threshold():
    conn = _make_conn()
    _insert_news(conn, "OXY", "Oil prices drop after OPEC decision", _iso(2), importance=2)
    items = load_macro_relevant_company_news(conn, _iso(MACRO_LOOKBACK_HOURS))
    assert items == []


def test_company_news_macro_filter_respects_time_window():
    conn = _make_conn()
    _insert_news(conn, "OXY", "Oil prices drop after OPEC decision", _iso(500), importance=9)
    items = load_macro_relevant_company_news(conn, _iso(MACRO_LOOKBACK_HOURS))
    assert items == []


# --- gather_macro_sources: fallback window --------------------------------

def test_gather_macro_sources_uses_primary_window_when_populated():
    conn = _make_conn()
    conn.execute(
        "INSERT INTO macro_news (source, title, url, published_at, content_raw) "
        "VALUES ('fed', 'Recent Fed release', 'http://fed.example/1', ?, '...')",
        (_iso(2),),
    )
    macro_items, company_items, window_hours = gather_macro_sources(conn)
    assert window_hours == MACRO_LOOKBACK_HOURS
    assert len(macro_items) == 1


def test_gather_macro_sources_falls_back_when_primary_window_empty():
    conn = _make_conn()
    conn.execute(
        "INSERT INTO macro_news (source, title, url, published_at, content_raw) "
        "VALUES ('fed', 'Older Fed release', 'http://fed.example/1', ?, '...')",
        (_iso(96),),  # older than the 48h primary window, within the 7-day fallback
    )
    macro_items, company_items, window_hours = gather_macro_sources(conn)
    assert window_hours == MACRO_LOOKBACK_FALLBACK_HOURS
    assert len(macro_items) == 1


def test_gather_macro_sources_stays_empty_beyond_fallback_window():
    conn = _make_conn()
    conn.execute(
        "INSERT INTO macro_news (source, title, url, published_at, content_raw) "
        "VALUES ('fed', 'Ancient Fed release', 'http://fed.example/1', ?, '...')",
        (_iso(MACRO_LOOKBACK_FALLBACK_HOURS + 24),),
    )
    macro_items, company_items, window_hours = gather_macro_sources(conn)
    assert macro_items == []
    assert company_items == []
    assert window_hours == MACRO_LOOKBACK_HOURS  # never widened for nothing


# --- sources_for_display -------------------------------------------------

def test_sources_for_display_merges_and_sorts_by_recency():
    macro_items = [{"source": "fed", "title": "Older", "url": "u1", "published_at": _iso(50)}]
    company_items = [{"ticker": "OXY", "title": "Newer", "url": "u2", "published_at": _iso(1)}]
    display = sources_for_display(macro_items, company_items)
    assert [d["title"] for d in display] == ["Newer", "Older"]
    assert display[0]["source"] == "OXY"
    assert display[1]["source"] == "fed"


# --- build_macro_context_prompt -------------------------------------------

def test_prompt_notes_fallback_window_widening():
    prompt = build_macro_context_prompt([], [], window_hours=MACRO_LOOKBACK_FALLBACK_HOURS)
    assert "fenetre plus large" in prompt


def test_prompt_omits_fallback_note_for_primary_window():
    prompt = build_macro_context_prompt([], [], window_hours=MACRO_LOOKBACK_HOURS)
    assert "fenetre plus large" not in prompt


def test_prompt_handles_no_sources_without_inventing_content():
    prompt = build_macro_context_prompt([], [])
    assert "n'invente aucune declaration" in prompt
    assert "Aucune news macro" in prompt


# --- get_or_generate_macro_context: never calls Groq under pytest ----------

def test_get_or_generate_never_calls_groq_under_pytest():
    # PYTEST_CURRENT_TEST is set automatically by pytest for the duration of
    # this test -- get_or_generate_macro_context must short-circuit before
    # any network/Groq call, same guard as add_argued_texts/
    # get_or_generate_news_narrative.
    assert os.environ.get("PYTEST_CURRENT_TEST")
    conn = _make_conn()
    conn.execute(
        "INSERT INTO macro_news (source, title, url, published_at, content_raw) "
        "VALUES ('fed', 'Fresh Fed release', 'http://fed.example/1', ?, '...')",
        (_iso(2),),
    )
    result = get_or_generate_macro_context(conn)
    assert result["source"] == "unavailable"
    assert result["texte_court"] is None
    assert result["texte_detaille"] is None
    assert result["secteurs_a_surveiller"] == []
    assert result["n_sources"] == 1
    assert result["sources"][0]["title"] == "Fresh Fed release"


def test_get_or_generate_reuses_cached_text_without_regenerating():
    conn = _make_conn()
    conn.execute(
        "CREATE TABLE macro_context_daily (day TEXT PRIMARY KEY, texte TEXT NOT NULL, "
        "texte_detaille TEXT, secteurs_json TEXT, n_sources INTEGER NOT NULL, model TEXT, "
        "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    from datetime import date
    conn.execute(
        "INSERT INTO macro_context_daily (day, texte, texte_detaille, secteurs_json, n_sources) "
        "VALUES (?, ?, ?, ?, ?)",
        (date.today().isoformat(), "Synthese courte en cache.", "Synthese detaillee en cache.",
         json.dumps([{"secteur": "Energie", "raison": "Sanctions petrolieres."}]), 3),
    )
    result = get_or_generate_macro_context(conn)
    assert result["source"] == "cache"
    assert result["texte_court"] == "Synthese courte en cache."
    assert result["texte_detaille"] == "Synthese detaillee en cache."
    assert result["secteurs_a_surveiller"] == [{"secteur": "Energie", "raison": "Sanctions petrolieres."}]
    assert result["n_sources"] == 3


def test_get_or_generate_treats_legacy_row_without_detailed_text_as_cache_miss():
    """A macro_context_daily row from before the two-tier synthesis existed
    (texte_detaille NULL) must NOT be served as a valid cache hit -- see
    load_cached_macro_context's own docstring. Under pytest this then falls
    through to the PYTEST_CURRENT_TEST guard, i.e. "unavailable", never a
    crash and never a half-formed "cache" response."""
    conn = _make_conn()
    conn.execute(
        "CREATE TABLE macro_context_daily (day TEXT PRIMARY KEY, texte TEXT NOT NULL, "
        "texte_detaille TEXT, secteurs_json TEXT, n_sources INTEGER NOT NULL, model TEXT, "
        "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    from datetime import date
    conn.execute(
        "INSERT INTO macro_context_daily (day, texte, n_sources) VALUES (?, ?, ?)",
        (date.today().isoformat(), "Ancienne synthese (V1 mono-version).", 3),
    )
    result = get_or_generate_macro_context(conn)
    assert result["source"] == "unavailable"
    assert result["texte_court"] is None


def test_ensure_two_tier_columns_migrates_legacy_table():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE macro_context_daily (day TEXT PRIMARY KEY, texte TEXT NOT NULL, "
        "n_sources INTEGER NOT NULL, model TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    _ensure_two_tier_columns(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(macro_context_daily)")}
    assert {"texte_detaille", "secteurs_json"} <= columns


# --- _parse_macro_completion -------------------------------------------------

def test_parse_macro_completion_extracts_both_versions_and_sectors():
    content = json.dumps({
        "synthese_courte": "Version courte.",
        "synthese_detaillee": "Version detaillee, plus pedagogique.",
        "secteurs_a_surveiller": [
            {"secteur": "Energie", "raison": "Sanctions sur le petrole iranien."},
            {"secteur": "Technologie", "raison": "Perspectives de taux affectant les valorisations."},
        ],
    })
    result = _parse_macro_completion(content)
    assert result["texte_court"] == "Version courte."
    assert result["texte_detaille"] == "Version detaillee, plus pedagogique."
    assert len(result["secteurs"]) == 2
    assert result["secteurs"][0] == {"secteur": "Energie", "raison": "Sanctions sur le petrole iranien."}


def test_parse_macro_completion_returns_none_when_short_text_missing():
    content = json.dumps({"synthese_courte": "", "synthese_detaillee": "Detaillee.", "secteurs_a_surveiller": []})
    assert _parse_macro_completion(content) is None


def test_parse_macro_completion_returns_none_when_detailed_text_missing():
    content = json.dumps({"synthese_courte": "Courte.", "synthese_detaillee": "", "secteurs_a_surveiller": []})
    assert _parse_macro_completion(content) is None


def test_parse_macro_completion_drops_malformed_sector_entries():
    content = json.dumps({
        "synthese_courte": "Courte.",
        "synthese_detaillee": "Detaillee.",
        "secteurs_a_surveiller": [
            {"secteur": "Energie", "raison": "Raison valide."},
            {"secteur": "", "raison": "Secteur vide, doit etre ignore."},
            "pas un dict",
            {"secteur": "Sans raison"},
        ],
    })
    result = _parse_macro_completion(content)
    assert result["secteurs"] == [{"secteur": "Energie", "raison": "Raison valide."}]


def test_parse_macro_completion_caps_sectors_at_max():
    many_sectors = [{"secteur": f"Secteur{i}", "raison": f"Raison {i}."} for i in range(10)]
    content = json.dumps({
        "synthese_courte": "Courte.",
        "synthese_detaillee": "Detaillee.",
        "secteurs_a_surveiller": many_sectors,
    })
    result = _parse_macro_completion(content)
    assert len(result["secteurs"]) == 5


def test_parse_macro_completion_defaults_missing_sectors_to_empty_list():
    content = json.dumps({"synthese_courte": "Courte.", "synthese_detaillee": "Detaillee."})
    result = _parse_macro_completion(content)
    assert result["secteurs"] == []
