"""Unit tests for reasoning/analyze_news.py's divergence-detection helpers
-- pure functions, no DB/Groq dependency."""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from reasoning.analyze_news import _build_divergence_note  # noqa: E402
from reasoning.direction_probability import (  # noqa: E402
    compute_direction_probabilities,
    dominant_direction as _dominant_direction,
)


def test_dominant_direction_picks_the_largest_bucket():
    assert _dominant_direction({"hausse": 70, "stagnation": 20, "baisse": 10}) == "hausse"
    assert _dominant_direction({"hausse": 10, "stagnation": 20, "baisse": 70}) == "baisse"
    assert _dominant_direction({"hausse": 20, "stagnation": 60, "baisse": 20}) == "stagnation"


def test_no_divergence_note_when_either_direction_missing():
    some_direction = compute_direction_probabilities(score_technique=70, score_prix_valorisation=60)
    assert _build_divergence_note(None, some_direction) is None
    assert _build_divergence_note(some_direction, None) is None
    assert _build_divergence_note(None, None) is None


def test_no_divergence_note_when_both_agree():
    # Same bullish scores with and without a mildly-positive news --
    # both should still lean "hausse" overall, no divergence to flag.
    general = compute_direction_probabilities(score_technique=90, score_prix_valorisation=85)
    with_news = compute_direction_probabilities(
        score_technique=90, score_prix_valorisation=85,
        news_tonalite="positive", news_importance=3,
    )
    assert _dominant_direction(general) == _dominant_direction(with_news)
    assert _build_divergence_note(general, with_news) is None


def test_divergence_note_produced_when_news_flips_the_dominant_direction():
    # Bullish structural scores (JPM-style: decent technical/valuation
    # read), but a strongly negative, important news -- exactly the
    # scenario that used to silently contradict the narrative text.
    general = compute_direction_probabilities(score_technique=75, score_prix_valorisation=70)
    with_bad_news = compute_direction_probabilities(
        score_technique=75, score_prix_valorisation=70,
        news_tonalite="negative", news_importance=10,
    )
    assert _dominant_direction(general) != _dominant_direction(with_bad_news)
    note = _build_divergence_note(general, with_bad_news)
    assert note is not None
    assert "Divergence detectee" in note
    assert str(general["hausse"]) in note
    assert str(with_bad_news["baisse"]) in note
