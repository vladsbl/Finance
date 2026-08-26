"""Unit tests for reasoning/direction_probability.py -- pure functions, no
DB/Groq dependency for compute_direction_probabilities itself (only
load_causal_effect_for_ticker touches sqlite3, tested separately with an
in-memory DB)."""

import os
import sqlite3
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from reasoning.direction_probability import (  # noqa: E402
    DISCLAIMER,
    HORIZON_BASE,
    HORIZON_NEWS,
    compute_direction_probabilities,
    dominant_direction,
    load_causal_effect_for_ticker,
    load_causal_effects_bulk,
)


def test_none_when_no_scores_available():
    assert compute_direction_probabilities() is None
    assert compute_direction_probabilities(score_fondamental_reel=70) is None


def test_percentages_always_sum_to_100():
    cases = [
        dict(score_technique=80, score_prix_valorisation=75, score_fondamental_reel=70),
        dict(score_technique=20, score_prix_valorisation=25),
        dict(score_technique=50, score_prix_valorisation=50, score_fondamental_reel=50),
        dict(score_technique=90, score_prix_valorisation=10, score_fondamental_reel=90),
        dict(score_technique=None, score_prix_valorisation=65),
        dict(score_technique=65, score_prix_valorisation=None, causal_effect="positif", causal_confidence=80),
    ]
    for kwargs in cases:
        result = compute_direction_probabilities(**kwargs)
        assert result is not None
        total = result["hausse"] + result["stagnation"] + result["baisse"]
        assert total == 100, f"{kwargs} -> {result} sums to {total}"


def test_bullish_scores_lean_hausse():
    result = compute_direction_probabilities(score_technique=90, score_prix_valorisation=85)
    assert result["hausse"] > result["baisse"]
    assert result["hausse"] > result["stagnation"]


def test_bearish_scores_lean_baisse():
    result = compute_direction_probabilities(score_technique=15, score_prix_valorisation=10)
    assert result["baisse"] > result["hausse"]
    assert result["baisse"] > result["stagnation"]


def test_neutral_scores_lean_stagnation():
    result = compute_direction_probabilities(score_technique=50, score_prix_valorisation=50)
    assert result["stagnation"] >= result["hausse"]
    assert result["stagnation"] >= result["baisse"]
    # abs(lean) == 0 -> stagnation pinned at its max band.
    assert result["stagnation"] == 55


def test_conflict_dampens_toward_stagnation():
    # Same bullish technical score, but the fondamental-reel component
    # clashes with it (has_conflict True) vs. agrees with it (False) --
    # the conflicting case must show more stagnation, less conviction.
    with_conflict = compute_direction_probabilities(
        score_technique=90, score_prix_valorisation=90, score_fondamental_reel=10,
    )
    without_conflict = compute_direction_probabilities(
        score_technique=90, score_prix_valorisation=90, score_fondamental_reel=90,
    )
    assert with_conflict["stagnation"] > without_conflict["stagnation"]


def test_causal_effect_shifts_the_result():
    base = compute_direction_probabilities(score_technique=55, score_prix_valorisation=55)
    boosted = compute_direction_probabilities(
        score_technique=55, score_prix_valorisation=55,
        causal_effect="positif", causal_confidence=100,
    )
    assert boosted["hausse"] > base["hausse"]


def test_explication_mentions_every_available_component():
    result = compute_direction_probabilities(
        score_technique=70, score_prix_valorisation=60, score_fondamental_reel=65,
        causal_effect="negatif", causal_confidence=90,
    )
    assert "Momentum technique" in result["explication"]
    assert "Prix/valorisation" in result["explication"]
    assert "causal" in result["explication"]


def test_disclaimer_always_present_and_honest():
    result = compute_direction_probabilities(score_technique=70, score_prix_valorisation=60)
    assert result["disclaimer"] == DISCLAIMER
    assert "PAS" in DISCLAIMER or "pas" in DISCLAIMER.lower()


def test_horizon_is_base_without_news_context():
    result = compute_direction_probabilities(score_technique=70, score_prix_valorisation=60)
    assert result["horizon"] == HORIZON_BASE


def test_horizon_switches_to_news_when_news_context_used():
    result = compute_direction_probabilities(
        score_technique=70, score_prix_valorisation=60,
        news_tonalite="negative", news_importance=8,
    )
    assert result["horizon"] == HORIZON_NEWS


def test_news_context_is_ignored_when_not_provided():
    """Every pre-existing caller (Resume du jour, StockPage, Correlations,
    ChainCard) never passes news_tonalite -- this must produce EXACTLY the
    same result as before the news-context feature existed, since the
    weight redistribution only kicks in when a news component is actually
    present."""
    without_news = compute_direction_probabilities(
        score_technique=70, score_prix_valorisation=60, causal_effect="positif", causal_confidence=80,
    )
    with_neutral_but_unset_importance = compute_direction_probabilities(
        score_technique=70, score_prix_valorisation=60, causal_effect="positif", causal_confidence=80,
        news_tonalite=None, news_importance=None,
    )
    assert without_news == with_neutral_but_unset_importance


def test_negative_news_pulls_result_toward_baisse():
    # Same bullish structural scores, but a strongly negative, important
    # news pulls the news-aware result down relative to the general one --
    # this is exactly the JPM-style scenario the news-context feature
    # exists to fix (a fresh negative news must move the number).
    general = compute_direction_probabilities(score_technique=70, score_prix_valorisation=65)
    with_bad_news = compute_direction_probabilities(
        score_technique=70, score_prix_valorisation=65,
        news_tonalite="negative", news_importance=10,
    )
    assert with_bad_news["hausse"] < general["hausse"]
    assert with_bad_news["baisse"] > general["baisse"]


def test_neutral_news_still_contributes_and_is_traceable():
    # A neutral-but-important news should genuinely dilute conviction
    # (pull toward stagnation), not be silently treated as "no news at
    # all" -- see _news_lean's own docstring for why lean=0.0 still counts
    # as a real component.
    general = compute_direction_probabilities(score_technique=80, score_prix_valorisation=80)
    with_neutral_news = compute_direction_probabilities(
        score_technique=80, score_prix_valorisation=80,
        news_tonalite="neutre", news_importance=9,
    )
    assert with_neutral_news["stagnation"] > general["stagnation"]
    assert "Cette news" in with_neutral_news["explication"]


def test_news_importance_scales_the_pull():
    low_importance = compute_direction_probabilities(
        score_technique=50, score_prix_valorisation=50,
        news_tonalite="positive", news_importance=2,
    )
    high_importance = compute_direction_probabilities(
        score_technique=50, score_prix_valorisation=50,
        news_tonalite="positive", news_importance=10,
    )
    assert high_importance["hausse"] > low_importance["hausse"]


def test_load_causal_effect_for_ticker_finds_impacted_company():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE causal_chains (id INTEGER PRIMARY KEY, entreprises_impactees TEXT, "
        "confiance REAL, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO causal_chains (entreprises_impactees, confiance, created_at) VALUES (?, ?, ?)",
        ('[{"entreprise": "Caterpillar Inc.", "ticker": "CAT", "effet": "positif"}]', 85.0, "2026-08-24 10:00:00"),
    )
    result = load_causal_effect_for_ticker(conn, "CAT")
    assert result is not None
    assert result["effet"] == "positif"
    assert result["confiance"] == 85.0


def test_load_causal_effect_for_ticker_returns_none_when_absent():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE causal_chains (id INTEGER PRIMARY KEY, entreprises_impactees TEXT, "
        "confiance REAL, created_at TEXT)"
    )
    assert load_causal_effect_for_ticker(conn, "ZZZZ") is None


def test_dominant_direction_picks_the_largest_bucket():
    assert dominant_direction({"hausse": 70, "stagnation": 20, "baisse": 10}) == "hausse"
    assert dominant_direction({"hausse": 10, "stagnation": 20, "baisse": 70}) == "baisse"
    assert dominant_direction({"hausse": 20, "stagnation": 60, "baisse": 20}) == "stagnation"


def test_dominant_direction_ties_break_toward_stagnation():
    assert dominant_direction({"hausse": 40, "stagnation": 40, "baisse": 20}) == "stagnation"
    assert dominant_direction({"hausse": 20, "stagnation": 40, "baisse": 40}) == "stagnation"


def _make_causal_chains_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE causal_chains (id INTEGER PRIMARY KEY, entreprises_impactees TEXT, "
        "confiance REAL, created_at TEXT)"
    )
    return conn


def test_load_causal_effects_bulk_builds_one_entry_per_ticker():
    conn = _make_causal_chains_conn()
    conn.execute(
        "INSERT INTO causal_chains (entreprises_impactees, confiance, created_at) VALUES (?, ?, ?)",
        (
            '[{"entreprise": "Caterpillar Inc.", "ticker": "CAT", "effet": "positif"}, '
            '{"entreprise": "Rio Tinto", "ticker": "RIO.L", "effet": "negatif"}]',
            85.0, "2026-08-24 10:00:00",
        ),
    )
    bulk = load_causal_effects_bulk(conn)
    assert bulk["CAT"]["effet"] == "positif"
    assert bulk["RIO.L"]["effet"] == "negatif"
    assert len(bulk) == 2


def test_load_causal_effects_bulk_keeps_most_recent_per_ticker():
    conn = _make_causal_chains_conn()
    # Newest first (matches the real ORDER BY created_at DESC query) --
    # the OLDER entry for CAT must never overwrite the newer one.
    conn.execute(
        "INSERT INTO causal_chains (entreprises_impactees, confiance, created_at) VALUES (?, ?, ?)",
        ('[{"entreprise": "Caterpillar Inc.", "ticker": "CAT", "effet": "positif"}]', 90.0, "2026-08-25 10:00:00"),
    )
    conn.execute(
        "INSERT INTO causal_chains (entreprises_impactees, confiance, created_at) VALUES (?, ?, ?)",
        ('[{"entreprise": "Caterpillar Inc.", "ticker": "CAT", "effet": "negatif"}]', 50.0, "2026-08-20 10:00:00"),
    )
    bulk = load_causal_effects_bulk(conn)
    assert bulk["CAT"]["effet"] == "positif"
    assert bulk["CAT"]["confiance"] == 90.0


def test_load_causal_effects_bulk_matches_single_ticker_lookup():
    conn = _make_causal_chains_conn()
    conn.execute(
        "INSERT INTO causal_chains (entreprises_impactees, confiance, created_at) VALUES (?, ?, ?)",
        ('[{"entreprise": "Caterpillar Inc.", "ticker": "CAT", "effet": "positif"}]', 85.0, "2026-08-24 10:00:00"),
    )
    bulk = load_causal_effects_bulk(conn)
    single = load_causal_effect_for_ticker(conn, "CAT")
    assert bulk["CAT"] == single
