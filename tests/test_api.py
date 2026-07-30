"""Tests for api/main.py + api/routers/daily_summary.py.

Same discipline as tests/test_dashboard_pages.py: reads hit the REAL
project database (data/marketdb.db) exactly like the Streamlit dashboard
already does for its own "loads without crashing" tests -- these routes
are thin wrappers around reasoning/daily_summary.py, so there is nothing
useful to mock for the read path. The one route that could reach Groq
(argued-text) never actually does during a test run: add_argued_texts()
checks os.environ["PYTEST_CURRENT_TEST"] (set automatically by pytest) and
no-ops before any network call -- see that function's own docstring in
reasoning/daily_summary.py.
"""

from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


def test_health_endpoint():
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db_found"] is True


def test_daily_summary_returns_expected_top_level_shape():
    resp = client.get("/api/daily-summary")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"signals", "dates_by_priority", "n_candidates", "staleness"}
    assert isinstance(body["signals"], list)
    assert isinstance(body["dates_by_priority"], dict)
    assert isinstance(body["n_candidates"], int)


def test_daily_summary_signal_shape_matches_build_signal():
    """Every field build_signal() produces (reasoning/daily_summary.py) must
    survive the JSON round-trip untouched -- this route must never
    reshape/rename/drop a field, since that's exactly the kind of drift
    this API is designed to avoid (see api/main.py's module docstring)."""
    resp = client.get("/api/daily-summary")
    assert resp.status_code == 200
    signals = resp.json()["signals"]
    if not signals:
        return  # nothing scored today in this environment -- not a failure
    expected_keys = {
        "ticker", "nom_affiche", "score_global", "confiance", "score_ajuste",
        "score_prix_valorisation", "score_technique", "score_news",
        "score_fondamental_reel", "explication", "risque",
        "conflit_composantes", "volatilite", "horizon",
        "entreprises_a_surveiller", "prix",
    }
    assert set(signals[0].keys()) == expected_keys


def test_argued_text_unknown_ticker_returns_404():
    resp = client.get("/api/daily-summary/NOTATICKER123/argued-text")
    assert resp.status_code == 404
    assert "NOTATICKER123" in resp.json()["detail"]


def test_argued_text_known_ticker_never_calls_groq_during_tests():
    """KEY has a real opportunites row in the project DB (used throughout
    this session's manual testing). Regardless of whether today's text is
    already cached from earlier manual curl testing or not, this call must
    never reach Groq during pytest -- source is always "cache" or
    "unavailable", texte_argumente is either a real cached string or None,
    but the route itself must always return 200 with a well-shaped body."""
    resp = client.get("/api/daily-summary/KEY/argued-text")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ticker"] == "KEY"
    assert body["source"] in {"cache", "unavailable"}
    if body["source"] == "unavailable":
        assert body["texte_argumente"] is None
    else:
        assert isinstance(body["texte_argumente"], str) and body["texte_argumente"]


def test_argued_text_lowercase_ticker_is_normalised():
    resp = client.get("/api/daily-summary/key/argued-text")
    assert resp.status_code == 200
    assert resp.json()["ticker"] == "KEY"


def test_cors_headers_present_for_dev_origin():
    """The future React dev server (localhost:3000 or :5173) must be able
    to call this API without a CORS block -- see api/main.py's DEV_ORIGINS."""
    resp = client.get(
        "/api/health",
        headers={"Origin": "http://localhost:5173"},
    )
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"
