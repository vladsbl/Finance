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


# --- /api/opportunities --------------------------------------------------------

def test_opportunities_returns_expected_top_level_shape():
    resp = client.get("/api/opportunities")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {
        "opportunites", "dates_by_priority", "staleness", "n_total", "limit", "offset",
    }
    assert isinstance(body["opportunites"], list)
    assert body["n_total"] >= len(body["opportunites"])


def test_opportunities_row_shape():
    resp = client.get("/api/opportunities")
    assert resp.status_code == 200
    rows = resp.json()["opportunites"]
    if not rows:
        return  # nothing scored in this environment -- not a failure
    expected_keys = {
        "ticker", "nom_affiche", "priorite", "score_global",
        "score_prix_valorisation", "score_technique", "score_news",
        "score_fondamental_reel", "confiance", "explication", "date_calcul",
    }
    assert set(rows[0].keys()) == expected_keys


def test_opportunities_sorted_by_score_global_descending():
    resp = client.get("/api/opportunities", params={"limit": 500})
    rows = resp.json()["opportunites"]
    scores = [r["score_global"] for r in rows if r["score_global"] is not None]
    assert scores == sorted(scores, reverse=True)


def test_opportunities_priorite_filter_scopes_to_one_tier():
    resp = client.get("/api/opportunities", params={"priorite": "haute"})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["dates_by_priority"].keys()) <= {"haute"}
    assert all(r["priorite"] == "haute" for r in body["opportunites"])


# --- pagination (limit/offset) --------------------------------------------------

def test_opportunities_default_limit_is_50():
    resp = client.get("/api/opportunities")
    body = resp.json()
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert len(body["opportunites"]) <= 50


def test_opportunities_custom_limit_is_respected():
    resp = client.get("/api/opportunities", params={"limit": 10})
    body = resp.json()
    assert body["limit"] == 10
    assert len(body["opportunites"]) <= 10


def test_opportunities_offset_returns_a_different_page():
    page1 = client.get("/api/opportunities", params={"limit": 20, "offset": 0}).json()
    page2 = client.get("/api/opportunities", params={"limit": 20, "offset": 20}).json()
    tickers1 = [o["ticker"] for o in page1["opportunites"]]
    tickers2 = [o["ticker"] for o in page2["opportunites"]]
    assert page1["n_total"] == page2["n_total"]
    if tickers1 and tickers2:
        assert set(tickers1).isdisjoint(tickers2)


def test_opportunities_n_total_reflects_full_count_not_just_page():
    small_page = client.get("/api/opportunities", params={"limit": 1}).json()
    full = client.get("/api/opportunities", params={"limit": 500}).json()
    assert small_page["n_total"] == full["n_total"]
    assert len(small_page["opportunites"]) <= 1


def test_opportunities_limit_beyond_max_returns_422():
    resp = client.get("/api/opportunities", params={"limit": 10000})
    assert resp.status_code == 422


def test_opportunities_negative_offset_returns_422():
    resp = client.get("/api/opportunities", params={"offset": -1})
    assert resp.status_code == 422


def test_opportunities_invalid_priorite_returns_422():
    resp = client.get("/api/opportunities", params={"priorite": "inexistante"})
    assert resp.status_code == 422
    assert "inexistante" in resp.json()["detail"]


# --- /api/tickers + /api/stock/{ticker}* ----------------------------------------
#
# AAPL is used as a "full data" ticker (has final_scores, fundamental_real_scores,
# 200+ days of price_history, and an opportunites row -- confirmed via manual
# curl testing against the real project DB). 1COV.DE is used as a "partial
# data" ticker: it's in `universe` but has no final_scores row and under 50
# days of price_history, so every score/MA/RSI field must degrade to null
# rather than error.

def test_tickers_returns_full_universe_list():
    resp = client.get("/api/tickers")
    assert resp.status_code == 200
    body = resp.json()
    tickers = body["tickers"]
    assert len(tickers) > 1000  # full universe, not a truncated sample
    assert all({"ticker", "nom_affiche"} == set(t.keys()) for t in tickers[:5])
    assert "AAPL" in {t["ticker"] for t in tickers}


def test_stock_detail_full_data_ticker():
    resp = client.get("/api/stock/AAPL")
    assert resp.status_code == 200
    body = resp.json()
    expected_keys = {
        "ticker", "nom_affiche", "priorite", "devise", "current_price",
        "prix_eur", "variations", "ma_50", "ma_200", "volume", "volatility",
        "rsi", "rsi_is_real", "price_valuation_score", "technical_score",
        "volatility_score", "volume_score", "final_score", "confidence",
        "score_fondamental_reel", "sector", "industry",
    }
    assert set(body.keys()) == expected_keys
    assert body["ticker"] == "AAPL"
    assert body["current_price"] is not None
    assert body["ma_50"] is not None and body["ma_200"] is not None


def test_stock_detail_partial_data_ticker_degrades_gracefully():
    resp = client.get("/api/stock/1COV.DE")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ticker"] == "1COV.DE"
    # Not enough price_history for a 50/200-day rolling average yet.
    assert body["ma_50"] is None
    assert body["ma_200"] is None
    # Never scored by final_scores -- these pillars are null, not a 500.
    assert body["price_valuation_score"] is None
    assert body["technical_score"] is None
    assert body["final_score"] is None


def test_stock_detail_unknown_ticker_returns_404():
    resp = client.get("/api/stock/NOTATICKER123")
    assert resp.status_code == 404
    assert "NOTATICKER123" in resp.json()["detail"]


def test_stock_detail_lowercase_ticker_is_normalised():
    resp = client.get("/api/stock/aapl")
    assert resp.status_code == 200
    assert resp.json()["ticker"] == "AAPL"


def test_stock_chart_full_data_ticker():
    resp = client.get("/api/stock/AAPL/chart")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ticker"] == "AAPL"
    assert body["devise_affichee"] == "EUR"
    assert len(body["points"]) > 200
    last = body["points"][-1]
    assert {"date", "close", "ma_50", "ma_200"} == set(last.keys())
    assert last["ma_50"] is not None and last["ma_200"] is not None


def test_stock_chart_partial_data_ticker_has_null_moving_averages():
    resp = client.get("/api/stock/1COV.DE/chart")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["points"], list)
    assert all(p["ma_50"] is None for p in body["points"])
    assert all(p["ma_200"] is None for p in body["points"])


def test_stock_chart_unknown_ticker_returns_404():
    resp = client.get("/api/stock/NOTATICKER123/chart")
    assert resp.status_code == 404


def test_stock_argued_text_unknown_ticker_returns_404():
    resp = client.get("/api/stock/NOTATICKER123/argued-text")
    assert resp.status_code == 404
    assert "NOTATICKER123" in resp.json()["detail"]


def test_stock_argued_text_never_calls_groq_during_tests():
    """Same PYTEST_CURRENT_TEST guard as the daily-summary equivalent
    (test_argued_text_known_ticker_never_calls_groq_during_tests) -- shared
    via api/dependencies.py's get_or_generate_argued_text, so this must
    behave identically here."""
    resp = client.get("/api/stock/KEY/argued-text")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ticker"] == "KEY"
    assert body["source"] in {"cache", "unavailable"}
    if body["source"] == "unavailable":
        assert body["texte_argumente"] is None
    else:
        assert isinstance(body["texte_argumente"], str) and body["texte_argumente"]
