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
        "entreprises_a_surveiller", "prix", "direction_probabilities",
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
        "direction_probabilities",
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


# --- direction filter -------------------------------------------------------

def test_opportunities_invalid_direction_returns_422():
    resp = client.get("/api/opportunities", params={"direction": "inexistante"})
    assert resp.status_code == 422
    assert "inexistante" in resp.json()["detail"]


def test_opportunities_direction_filter_matches_only_that_dominant_scenario():
    from reasoning.direction_probability import dominant_direction

    for direction in ("hausse", "stagnation", "baisse"):
        resp = client.get("/api/opportunities", params={"direction": direction, "limit": 500})
        assert resp.status_code == 200
        rows = resp.json()["opportunites"]
        for row in rows:
            dp = row["direction_probabilities"]
            assert dp is not None
            assert dominant_direction(dp) == direction


def test_opportunities_direction_filter_reduces_or_equals_unfiltered_total():
    unfiltered = client.get("/api/opportunities", params={"limit": 1}).json()["n_total"]
    hausse = client.get("/api/opportunities", params={"direction": "hausse", "limit": 1}).json()["n_total"]
    stagnation = client.get("/api/opportunities", params={"direction": "stagnation", "limit": 1}).json()["n_total"]
    baisse = client.get("/api/opportunities", params={"direction": "baisse", "limit": 1}).json()["n_total"]
    assert hausse <= unfiltered
    assert stagnation <= unfiltered
    assert baisse <= unfiltered
    # every row with a computable direction falls into exactly one bucket
    assert hausse + stagnation + baisse <= unfiltered


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
        "score_fondamental_reel", "sector", "industry", "direction_probabilities",
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


def test_company_description_unknown_ticker_returns_404():
    resp = client.get("/api/stock/NOTATICKER123/description")
    assert resp.status_code == 404
    assert "NOTATICKER123" in resp.json()["detail"]


def test_company_description_never_calls_groq_during_tests():
    """Same PYTEST_CURRENT_TEST guard as the argued-text/narrative
    equivalents -- get_or_generate_company_description must never reach
    Groq during a pytest run."""
    resp = client.get("/api/stock/AAPL/description")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"ticker", "description", "source"}
    assert body["ticker"] == "AAPL"
    assert body["source"] in {"cache", "unavailable"}
    if body["source"] == "unavailable":
        assert body["description"] is None
    else:
        assert isinstance(body["description"], str) and body["description"]


def test_company_description_is_cached_across_repeated_calls():
    """Unlike argued-text/narrative, this cache has NO day component -- two
    calls in the same test run must return the exact same result (either
    both "cache" with identical text, or both "unavailable" -- never a
    fresh "generated" the second time)."""
    first = client.get("/api/stock/AAPL/description").json()
    second = client.get("/api/stock/AAPL/description").json()
    assert first == second


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


# --- /api/graph -------------------------------------------------------------
#
# AAPL is used as a "has real relations" ticker (part of the original
# hand-curated pilot seed, confirmed via manual curl testing to have 6
# direct outbound relations). 1COV.DE is used as a "no relations yet"
# ticker (confirmed empty via the same manual testing) to check the
# ticker-centered route degrades to an empty graph rather than erroring.
#
# Every test that creates a manual relation deletes it again at the end
# (real project DB, not an in-memory fixture -- these tests must leave no
# trace, both so the suite is re-runnable and so it never pollutes the
# actual Knowledge Graph the dashboard/API serve).

def test_graph_default_returns_expected_shape():
    resp = client.get("/api/graph")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "top_opportunities"
    assert isinstance(body["top_tickers"], list)
    assert len(body["top_tickers"]) <= 10
    assert isinstance(body["nodes"], list)
    assert isinstance(body["edges"], list)
    assert body["n_primary"] + body["n_external"] == len(body["nodes"])


def test_graph_node_and_edge_shape():
    resp = client.get("/api/graph")
    body = resp.json()
    if not body["nodes"]:
        return  # no relations at all in this environment -- not a failure
    node = body["nodes"][0]
    assert {"id", "kind", "ticker", "label", "display_name"} == set(node.keys())
    assert node["kind"] in {"primary", "external"}
    if body["edges"]:
        edge = body["edges"][0]
        assert {"source", "target", "relation_type", "notes"} == set(edge.keys())


def test_graph_ticker_centered_known_ticker():
    resp = client.get("/api/graph", params={"ticker": "AAPL"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "ticker"
    assert body["ticker"] == "AAPL"
    assert len(body["nodes"]) > 0
    assert all(e["source"] == "AAPL" for e in body["edges"])


def test_graph_ticker_centered_lowercase_is_normalised():
    resp = client.get("/api/graph", params={"ticker": "aapl"})
    assert resp.status_code == 200
    assert resp.json()["ticker"] == "AAPL"


def test_graph_ticker_centered_no_relations_returns_empty():
    resp = client.get("/api/graph", params={"ticker": "1COV.DE"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["nodes"] == []
    assert body["edges"] == []


def test_graph_ticker_centered_unknown_ticker_returns_404():
    resp = client.get("/api/graph", params={"ticker": "NOTATICKER123"})
    assert resp.status_code == 404
    assert "NOTATICKER123" in resp.json()["detail"]


def test_graph_manual_relations_add_list_delete_round_trip():
    """The full CRUD cycle: add -> appears in the manual list -> delete ->
    gone from the manual list -> a second delete of the same id 404s."""
    payload = {
        "source_ticker": "AAPL",
        "relation_type": "partenaire",
        "target_name": "Pytest Test Partner Inc.",
        "target_ticker": "ZZPYTEST",
        "notes": "cree par tests/test_api.py, doit etre supprime par le meme test",
    }
    created = None
    try:
        resp = client.post("/api/graph/relations", json=payload)
        assert resp.status_code == 201
        created = resp.json()
        assert created["source_ticker"] == "AAPL"
        assert created["target_ticker"] == "ZZPYTEST"
        relation_id = created["id"]

        list_resp = client.get("/api/graph/relations/manual")
        assert list_resp.status_code == 200
        list_body = list_resp.json()
        assert any(r["id"] == relation_id for r in list_body["relations"])
        assert set(list_body["relation_types"]) == {
            "concurrent", "fournisseur", "client", "partenaire", "dependance",
        }

        del_resp = client.delete(f"/api/graph/relations/{relation_id}")
        assert del_resp.status_code == 200
        assert del_resp.json() == {"deleted": True, "id": relation_id}
        created = None  # already cleaned up

        list_resp2 = client.get("/api/graph/relations/manual")
        assert not any(r["id"] == relation_id for r in list_resp2.json()["relations"])

        del_again = client.delete(f"/api/graph/relations/{relation_id}")
        assert del_again.status_code == 404
    finally:
        if created is not None:
            client.delete(f"/api/graph/relations/{created['id']}")


def test_graph_manual_relation_duplicate_returns_409():
    payload = {
        "source_ticker": "AAPL",
        "relation_type": "partenaire",
        "target_name": "Pytest Dup Test Inc.",
        "target_ticker": "ZZPYDUP",
    }
    created = None
    try:
        first = client.post("/api/graph/relations", json=payload)
        assert first.status_code == 201
        created = first.json()

        second = client.post("/api/graph/relations", json=payload)
        assert second.status_code == 409
    finally:
        if created is not None:
            client.delete(f"/api/graph/relations/{created['id']}")


def test_graph_manual_relation_invalid_type_returns_422():
    resp = client.post("/api/graph/relations", json={
        "source_ticker": "AAPL",
        "relation_type": "invalidtype",
        "target_name": "X",
    })
    assert resp.status_code == 422


def test_graph_manual_relation_missing_target_name_returns_422():
    resp = client.post("/api/graph/relations", json={
        "source_ticker": "AAPL",
        "relation_type": "concurrent",
        "target_name": "",
    })
    assert resp.status_code == 422


def test_graph_manual_relation_target_equals_source_returns_422():
    resp = client.post("/api/graph/relations", json={
        "source_ticker": "AAPL",
        "relation_type": "concurrent",
        "target_name": "X",
        "target_ticker": "AAPL",
    })
    assert resp.status_code == 422


def test_graph_manual_relation_unknown_source_returns_404():
    resp = client.post("/api/graph/relations", json={
        "source_ticker": "NOTATICKER123",
        "relation_type": "concurrent",
        "target_name": "X",
    })
    assert resp.status_code == 404


def test_graph_delete_unknown_relation_returns_404():
    resp = client.delete("/api/graph/relations/99999999")
    assert resp.status_code == 404


# --- /api/correlations -------------------------------------------------------
#
# Known badge examples confirmed against the real project DB via a direct
# reasoning.correlation_discovery call (not hardcoded blindly): ESS/AVB and
# ESS/EQR are the two curated MEAN_REVERSION_PAIRS (mean_reversion badge,
# lag!=0 row only -- their own lag=0 row has no badge); ALB/QYM.MU is an
# inter_market_lag example (different exchanges); MDLZ/GIS is a same-market
# lag!=0 example (lag_caution). Searched by ticker pair rather than by
# position, since a future correlation_discovery.py run could reorder rows.

def _fetch_all_correlations():
    """Every correlation across both pages needed to reach the known badge
    examples above (n_total was 551 at last count, i.e. 2 pages of 500)."""
    page1 = client.get("/api/correlations", params={"limit": 500, "offset": 0}).json()
    page2 = client.get("/api/correlations", params={"limit": 500, "offset": 500}).json()
    return page1["correlations"] + page2["correlations"], page1


def _find_pair(rows, ticker_a, ticker_b, lag_nonzero=None):
    pair = {ticker_a, ticker_b}
    matches = [
        r for r in rows
        if {r["ticker_source"], r["ticker_target"]} == pair
        and (lag_nonzero is None or (r["lag"] != 0) == lag_nonzero)
    ]
    return matches[0] if matches else None


def test_correlations_returns_expected_top_level_shape():
    resp = client.get("/api/correlations")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {
        "correlations", "n_before_dedup", "n_total", "search", "limit", "offset",
    }
    assert body["search"] is None  # no ?search= given -> no filter applied
    assert isinstance(body["correlations"], list)
    assert body["n_before_dedup"] >= body["n_total"]  # dedup only ever collapses, never adds rows


def test_correlations_row_shape():
    resp = client.get("/api/correlations", params={"limit": 1})
    body = resp.json()
    if not body["correlations"]:
        return  # nothing discovered yet in this environment -- not a failure
    row = body["correlations"][0]
    expected_keys = {
        "id", "ticker_source", "nom_source", "ticker_target", "nom_target",
        "relation_type", "source_table", "lag", "lag_direction", "lag_label",
        "coefficient", "p_value", "p_value_corrigee", "n_observations",
        "methode", "correction", "meme_marche", "badge", "created_at",
    }
    assert set(row.keys()) == expected_keys
    assert isinstance(row["meme_marche"], bool)


def test_correlations_sorted_by_abs_coefficient_descending():
    resp = client.get("/api/correlations", params={"limit": 500})
    rows = resp.json()["correlations"]
    coeffs = [abs(r["coefficient"]) for r in rows]
    assert coeffs == sorted(coeffs, reverse=True)


def test_correlations_badge_none_for_strong_simultaneous_row():
    rows, _ = _fetch_all_correlations()
    lag_zero_rows = [r for r in rows if r["lag"] == 0]
    if not lag_zero_rows:
        return
    assert lag_zero_rows[0]["badge"] is None
    assert lag_zero_rows[0]["lag_label"] == "Simultanee (meme jour de bourse)"


def test_correlations_badge_mean_reversion_for_known_pair():
    rows, _ = _fetch_all_correlations()
    row = _find_pair(rows, "ESS", "AVB", lag_nonzero=True) or _find_pair(rows, "ESS", "EQR", lag_nonzero=True)
    if row is None:
        return  # curated pair not present in this environment's data -- not a failure
    assert row["badge"] is not None
    assert row["badge"]["type"] == "mean_reversion"
    assert row["badge"]["severity"] == "warning"
    assert "retour a la moyenne" in row["badge"]["message"]


def test_correlations_badge_inter_market_lag_for_known_pair():
    rows, _ = _fetch_all_correlations()
    row = _find_pair(rows, "ALB", "QYM.MU")
    if row is None:
        return
    assert row["meme_marche"] is False
    assert row["badge"]["type"] == "inter_market_lag"
    assert row["badge"]["severity"] == "warning"


def test_correlations_badge_lag_caution_for_known_pair():
    rows, _ = _fetch_all_correlations()
    row = _find_pair(rows, "MDLZ", "GIS", lag_nonzero=True)
    if row is None:
        return
    assert row["meme_marche"] is True
    assert row["badge"]["type"] == "lag_caution"
    assert row["badge"]["severity"] == "info"


def test_correlations_default_limit_is_50():
    resp = client.get("/api/correlations")
    body = resp.json()
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert len(body["correlations"]) <= 50


def test_correlations_custom_limit_is_respected():
    resp = client.get("/api/correlations", params={"limit": 10})
    body = resp.json()
    assert body["limit"] == 10
    assert len(body["correlations"]) <= 10


def test_correlations_offset_returns_a_different_page():
    page1 = client.get("/api/correlations", params={"limit": 20, "offset": 0}).json()
    page2 = client.get("/api/correlations", params={"limit": 20, "offset": 20}).json()
    ids1 = [r["id"] for r in page1["correlations"]]
    ids2 = [r["id"] for r in page2["correlations"]]
    assert page1["n_total"] == page2["n_total"]
    if ids1 and ids2:
        assert set(ids1).isdisjoint(ids2)


def test_correlations_limit_beyond_max_returns_422():
    resp = client.get("/api/correlations", params={"limit": 10000})
    assert resp.status_code == 422


def test_correlations_negative_offset_returns_422():
    resp = client.get("/api/correlations", params={"offset": -1})
    assert resp.status_code == 422


# --- /api/correlations?search= -----------------------------------------------
#
# "Apple" is used as the reference query: AAPL is part of the hand-curated
# pilot seed and has several stored correlations, confirmed against the real
# DB. Assertions check the FILTER'S CONTRACT (every returned row really has
# the term in one of its two names, counts shrink, n_before_dedup stays
# global) rather than an exact match count, which a future
# correlation_discovery.py run could legitimately change.

def _names_of(row):
    return (row["nom_source"].casefold(), row["nom_target"].casefold())


def test_correlations_search_filters_by_company_name():
    resp = client.get("/api/correlations", params={"search": "Apple", "limit": 500})
    assert resp.status_code == 200
    body = resp.json()
    assert body["search"] == "Apple"
    assert body["n_total"] > 0, "AAPL should have at least one stored correlation"
    for row in body["correlations"]:
        assert any("apple" in n for n in _names_of(row)), row


def test_correlations_search_matches_source_or_target_side():
    """The filter must be an OR across the two sides, not just the source --
    AAPL appears as ticker_source in some stored pairs and as ticker_target
    in others."""
    body = client.get("/api/correlations", params={"search": "Apple", "limit": 500}).json()
    rows = body["correlations"]
    if len(rows) < 2:
        return  # too little data in this environment to show both sides
    matched_on_source = any("apple" in r["nom_source"].casefold() for r in rows)
    matched_on_target = any("apple" in r["nom_target"].casefold() for r in rows)
    assert matched_on_source and matched_on_target


def test_correlations_search_is_case_insensitive():
    variants = ["Apple", "apple", "APPLE", "aPpLe"]
    totals = {
        v: client.get("/api/correlations", params={"search": v, "limit": 500}).json()["n_total"]
        for v in variants
    }
    assert len(set(totals.values())) == 1, totals


def test_correlations_search_matches_partial_name():
    """A prefix of the real name must match -- the point of the search box is
    not having to type the exact registered company name."""
    partial = client.get("/api/correlations", params={"search": "Appl", "limit": 500}).json()
    full = client.get("/api/correlations", params={"search": "Apple", "limit": 500}).json()
    assert partial["n_total"] >= full["n_total"] > 0


def test_correlations_search_is_trimmed_and_echoed():
    body = client.get("/api/correlations", params={"search": "  Apple  "}).json()
    assert body["search"] == "Apple"


def test_correlations_blank_search_behaves_like_no_search():
    no_search = client.get("/api/correlations", params={"limit": 1}).json()
    blank = client.get("/api/correlations", params={"search": "   ", "limit": 1}).json()
    assert blank["search"] is None
    assert blank["n_total"] == no_search["n_total"]


def test_correlations_search_narrows_the_result_set():
    unfiltered = client.get("/api/correlations", params={"limit": 1}).json()
    filtered = client.get("/api/correlations", params={"search": "Apple", "limit": 1}).json()
    assert 0 < filtered["n_total"] < unfiltered["n_total"]


def test_correlations_search_leaves_n_before_dedup_global():
    """n_before_dedup describes the whole dataset, not the filtered view --
    the frontend pairs it with n_total to say "N matches out of M stored"."""
    unfiltered = client.get("/api/correlations", params={"limit": 1}).json()
    filtered = client.get("/api/correlations", params={"search": "Apple", "limit": 1}).json()
    assert filtered["n_before_dedup"] == unfiltered["n_before_dedup"]


def test_correlations_search_with_no_match_returns_empty_not_error():
    resp = client.get("/api/correlations", params={"search": "zzzz-aucune-entreprise-zzzz"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["correlations"] == []
    assert body["n_total"] == 0
    assert body["search"] == "zzzz-aucune-entreprise-zzzz"


def test_correlations_search_still_sorted_by_abs_coefficient():
    body = client.get("/api/correlations", params={"search": "Energy", "limit": 500}).json()
    coeffs = [abs(r["coefficient"]) for r in body["correlations"]]
    assert coeffs == sorted(coeffs, reverse=True)


def test_correlations_search_paginates_the_filtered_set():
    """limit/offset must page through the MATCHES, not through the full list
    then filter -- otherwise page 2 of a search could come back empty while
    n_total claims there are more."""
    term = "Energy"
    full = client.get("/api/correlations", params={"search": term, "limit": 500}).json()
    if full["n_total"] <= 5:
        return  # not enough matches to paginate meaningfully
    page1 = client.get("/api/correlations", params={"search": term, "limit": 5, "offset": 0}).json()
    page2 = client.get("/api/correlations", params={"search": term, "limit": 5, "offset": 5}).json()
    assert page1["n_total"] == page2["n_total"] == full["n_total"]
    assert len(page1["correlations"]) == 5
    ids1 = [r["id"] for r in page1["correlations"]]
    ids2 = [r["id"] for r in page2["correlations"]]
    assert set(ids1).isdisjoint(ids2)
    assert ids1 + ids2 == [r["id"] for r in full["correlations"][:len(ids1) + len(ids2)]]


# --- /api/pipeline -----------------------------------------------------------
#
# The real pipeline is NEVER launched here. api/routers/pipeline.py funnels
# every launch through a single _spawn() seam precisely so these tests can
# replace it with a fake process object: nothing in this section ever calls
# subprocess, touches data/logs/run_daily.log, or writes to the database.

class _FakeProcess:
    """Stand-in for subprocess.Popen. `returncode_to_report` is what poll()
    answers: None means still running, an int means the child has exited --
    flip it mid-test to simulate the pipeline finishing."""

    def __init__(self, returncode_to_report=None):
        self.returncode_to_report = returncode_to_report

    def poll(self):
        return self.returncode_to_report


def _reset_pipeline_state():
    """Bring the router's module-level run state back to a fresh boot, so
    tests never inherit a previous test's 'running' flag."""
    import api.routers.pipeline as pl
    pl._run.clear()
    pl._run.update(pl._IDLE_STATE)
    pl._process = None
    return pl


def test_pipeline_status_is_idle_before_any_run():
    _reset_pipeline_state()
    resp = client.get("/api/pipeline/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "idle"
    assert body["task_id"] is None
    assert set(body.keys()) == {
        "task_id", "status", "started_at", "finished_at", "returncode",
        "error", "last_run", "log_file",
    }


def test_pipeline_run_returns_202_immediately_with_task_id(monkeypatch):
    pl = _reset_pipeline_state()
    monkeypatch.setattr(pl, "_spawn", lambda: _FakeProcess())

    resp = client.post("/api/pipeline/run")
    assert resp.status_code == 202  # accepted, NOT waited on
    body = resp.json()
    assert body["status"] == "running"
    assert body["task_id"]
    assert body["started_at"]
    assert body["finished_at"] is None


def test_pipeline_second_run_while_running_returns_409(monkeypatch):
    """Two pipelines writing to the same SQLite file at once is exactly what
    this guard exists to prevent -- a double click must not start a second
    one."""
    pl = _reset_pipeline_state()
    monkeypatch.setattr(pl, "_spawn", lambda: _FakeProcess())

    assert client.post("/api/pipeline/run").status_code == 202
    second = client.post("/api/pipeline/run")
    assert second.status_code == 409
    assert "deja en cours" in second.json()["detail"]


def test_pipeline_status_stays_running_while_process_alive(monkeypatch):
    pl = _reset_pipeline_state()
    monkeypatch.setattr(pl, "_spawn", lambda: _FakeProcess())
    client.post("/api/pipeline/run")

    body = client.get("/api/pipeline/status").json()
    assert body["status"] == "running"
    assert body["returncode"] is None


def test_pipeline_status_flips_to_success_when_process_exits_zero(monkeypatch):
    pl = _reset_pipeline_state()
    fake = _FakeProcess()
    monkeypatch.setattr(pl, "_spawn", lambda: fake)
    client.post("/api/pipeline/run")

    fake.returncode_to_report = 0  # child finished, every step passed
    body = client.get("/api/pipeline/status").json()
    assert body["status"] == "success"
    assert body["returncode"] == 0
    assert body["finished_at"]
    assert body["error"] is None


def test_pipeline_status_flips_to_failed_when_process_exits_nonzero(monkeypatch):
    """run_daily.py exits 1 when ANY step failed -- the route reports failure
    but points at the per-step detail rather than claiming the run crashed."""
    pl = _reset_pipeline_state()
    fake = _FakeProcess()
    monkeypatch.setattr(pl, "_spawn", lambda: fake)
    client.post("/api/pipeline/run")

    fake.returncode_to_report = 1
    body = client.get("/api/pipeline/status").json()
    assert body["status"] == "failed"
    assert body["returncode"] == 1
    assert "1" in body["error"]
    assert body["finished_at"]


def test_pipeline_can_run_again_after_previous_run_finished(monkeypatch):
    pl = _reset_pipeline_state()
    first = _FakeProcess()
    monkeypatch.setattr(pl, "_spawn", lambda: first)
    client.post("/api/pipeline/run")
    first.returncode_to_report = 0
    assert client.get("/api/pipeline/status").json()["status"] == "success"

    monkeypatch.setattr(pl, "_spawn", lambda: _FakeProcess())
    again = client.post("/api/pipeline/run")
    assert again.status_code == 202
    assert again.json()["status"] == "running"


def test_pipeline_run_reports_500_when_spawn_fails(monkeypatch):
    pl = _reset_pipeline_state()

    def _boom():
        raise OSError("interpreteur introuvable")

    monkeypatch.setattr(pl, "_spawn", _boom)
    resp = client.post("/api/pipeline/run")
    assert resp.status_code == 500
    assert "interpreteur introuvable" in resp.json()["detail"]
    # A failed launch must not leave the guard stuck on "running".
    assert client.get("/api/pipeline/status").json()["status"] == "idle"


# --- pipeline/run_log.py's parser --------------------------------------------
#
# The status route's per-step detail is only as good as this parser, and it
# reads a format owned by another module -- so it is pinned against the exact
# line shapes run_daily.py's logger produces.

_LOG_FINISHED = [
    "10:22:15 [INFO] DEBUT DU PIPELINE QUOTIDIEN (9 etapes)",
    "10:22:15 [INFO] ETAPE : Ingestion des prix (univers complet)",
    "10:25:03 [INFO] [OK] Ingestion des prix (univers complet) termine en 168.0s.",
    "10:25:03 [INFO] ETAPE : Collecte des news (univers)",
    "10:25:10 [ERROR] [ECHEC] Collecte des news (univers) a echoue apres 6.5s : HTTPError 503",
    "10:30:00 [INFO] BILAN DU PIPELINE QUOTIDIEN",
    "10:30:00 [INFO]   [OK   ] Ingestion des prix (univers complet)          168.0s",
    "10:30:00 [INFO]   [ECHEC] Collecte des news (univers)                     6.5s -- HTTPError 503",
    "10:30:00 [INFO] Total : 8/9 etapes reussies, duree globale 465.0s (7.8 min).",
]


def _write_log(tmp_path, lines):
    path = tmp_path / "run_daily.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def test_parse_last_run_returns_none_when_no_log(tmp_path):
    from pipeline.run_log import parse_last_run
    assert parse_last_run(str(tmp_path / "absent.log")) is None


def test_parse_last_run_returns_none_when_log_has_no_run_marker(tmp_path):
    from pipeline.run_log import parse_last_run
    assert parse_last_run(_write_log(tmp_path, ["12:00:00 [INFO] rien a voir"])) is None


def test_parse_last_run_reads_steps_and_totals(tmp_path):
    from pipeline.run_log import parse_last_run
    r = parse_last_run(_write_log(tmp_path, _LOG_FINISHED))
    assert r["completed"] is True
    assert r["steps_total"] == 9
    assert r["n_ok"] == 8 and r["n_failed"] == 1
    assert r["duree_secondes"] == 465.0
    assert r["current_step"] is None
    assert [s["status"] for s in r["steps"]] == ["ok", "failed"]
    assert r["steps"][1]["error"] == "HTTPError 503"


def test_parse_last_run_does_not_double_count_the_bilan_recap(tmp_path):
    """The BILAN section repeats every step as "  [OK   ] name ..." -- close
    enough to a real step line to be counted twice if the regexes were not
    anchored right after the log level."""
    from pipeline.run_log import parse_last_run
    r = parse_last_run(_write_log(tmp_path, _LOG_FINISHED))
    assert r["steps_done"] == 2, r["steps"]


def test_parse_last_run_reports_the_step_in_progress(tmp_path):
    from pipeline.run_log import parse_last_run
    lines = [
        "11:00:00 [INFO] DEBUT DU PIPELINE QUOTIDIEN (9 etapes)",
        "11:00:00 [INFO] ETAPE : Ingestion des prix (univers complet)",
        "11:02:00 [INFO] [OK] Ingestion des prix (univers complet) termine en 120.0s.",
        "11:02:00 [INFO] ETAPE : Scores techniques (tickers manquants)",
    ]
    r = parse_last_run(_write_log(tmp_path, lines))
    assert r["completed"] is False
    assert r["current_step"] == "Scores techniques (tickers manquants)"
    assert r["steps_done"] == 1
    assert r["log_time_end"] is None


def test_parse_last_run_only_reads_the_most_recent_run(tmp_path):
    """The log is appended to across days -- an older run's steps must not
    leak into the reported one."""
    from pipeline.run_log import parse_last_run
    older = [
        "09:00:00 [INFO] DEBUT DU PIPELINE QUOTIDIEN (9 etapes)",
        "09:00:00 [INFO] ETAPE : Vieille etape",
        "09:00:05 [INFO] [OK] Vieille etape termine en 5.0s.",
        "09:00:05 [INFO] Total : 9/9 etapes reussies, duree globale 5.0s (0.1 min).",
    ]
    r = parse_last_run(_write_log(tmp_path, older + _LOG_FINISHED))
    assert r["log_time_start"] == "10:22:15"
    assert all("Vieille" not in s["name"] for s in r["steps"])


# --- /api/causal-reasoning ----------------------------------------------------
#
# GET routes hit the REAL database (read-only, same discipline as the rest
# of this file). POST /run is ALWAYS tested against a monkeypatched
# run_causal_reasoning -- api/routers/causal_reasoning.py imports it as a
# bare name specifically so tests can replace it here, exactly like
# api/routers/pipeline.py's _spawn seam. The real function is never
# guarded by PYTEST_CURRENT_TEST (unlike add_argued_texts), so calling it
# unmocked from a test would reach real Groq -- never done here.

def test_causal_reasoning_returns_expected_top_level_shape():
    resp = client.get("/api/causal-reasoning")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"chains", "n_total", "staleness"}
    assert isinstance(body["chains"], list)
    assert body["n_total"] == len(body["chains"])


def test_causal_reasoning_chain_shape():
    resp = client.get("/api/causal-reasoning")
    chains = resp.json()["chains"]
    if not chains:
        return  # nothing generated yet in this environment -- not a failure
    chain = chains[0]
    expected_keys = {
        "id", "news_id", "news_title", "ticker_source", "chaine_raisonnement",
        "entreprises_impactees", "confiance", "model", "created_at",
        "direction_probabilities",
    }
    assert set(chain.keys()) == expected_keys
    assert isinstance(chain["entreprises_impactees"], list)
    for entry in chain["entreprises_impactees"]:
        assert "entreprise" in entry
        assert "effet" in entry


def test_causal_reasoning_sorted_by_date_descending():
    resp = client.get("/api/causal-reasoning", params={"limit": 500})
    dates = [c["created_at"] for c in resp.json()["chains"]]
    assert dates == sorted(dates, reverse=True)


def test_causal_reasoning_limit_is_respected():
    resp = client.get("/api/causal-reasoning", params={"limit": 2})
    assert len(resp.json()["chains"]) <= 2


def test_causal_reasoning_status_returns_expected_shape():
    resp = client.get("/api/causal-reasoning/status")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"n_pending", "quota_used", "quota_limit", "quota_remaining"}
    assert body["quota_limit"] == 5
    assert body["quota_used"] + body["quota_remaining"] == body["quota_limit"]
    assert body["n_pending"] >= 0


def test_causal_reasoning_run_never_calls_real_groq(monkeypatch):
    """The route must delegate to run_causal_reasoning exactly once and
    return its stats untouched -- proves the route itself does no Groq
    call, prompting, or reshaping of its own."""
    import api.routers.causal_reasoning as cr_router

    calls = []

    def _fake_run(conn):
        calls.append(conn)
        return {
            "n_candidates": 10, "processed": 3, "failed": 1,
            "skipped_no_relations": 2, "quota_used": 3, "quota_limit": 5,
            "quota_exhausted": False, "error": None,
        }

    monkeypatch.setattr(cr_router, "run_causal_reasoning", _fake_run)
    resp = client.post("/api/causal-reasoning/run")
    assert resp.status_code == 200
    assert resp.json() == {
        "n_candidates": 10, "processed": 3, "failed": 1,
        "skipped_no_relations": 2, "quota_used": 3, "quota_limit": 5,
        "quota_exhausted": False, "error": None,
    }
    assert len(calls) == 1


def test_causal_reasoning_run_reports_quota_exhausted_without_error(monkeypatch):
    import api.routers.causal_reasoning as cr_router

    monkeypatch.setattr(cr_router, "run_causal_reasoning", lambda conn: {
        "n_candidates": 50, "processed": 0, "failed": 0,
        "skipped_no_relations": 0, "quota_used": 5, "quota_limit": 5,
        "quota_exhausted": True, "error": None,
    })
    resp = client.post("/api/causal-reasoning/run")
    assert resp.status_code == 200  # never a 5xx for a normal degraded state
    body = resp.json()
    assert body["quota_exhausted"] is True
    assert body["error"] is None


def test_causal_reasoning_run_surfaces_setup_error_without_5xx(monkeypatch):
    """A genuine setup failure (missing GROQ_API_KEY, client unavailable)
    is still a 200 with stats["error"] set -- run_causal_reasoning's own
    'never raises' contract, mirrored by the route."""
    import api.routers.causal_reasoning as cr_router

    monkeypatch.setattr(cr_router, "run_causal_reasoning", lambda conn: {
        "n_candidates": 0, "processed": 0, "failed": 0,
        "skipped_no_relations": 0, "quota_used": 0, "quota_limit": 5,
        "quota_exhausted": False, "error": "GROQ_API_KEY absent. Ajoutez-la a votre .env.",
    })
    resp = client.post("/api/causal-reasoning/run")
    assert resp.status_code == 200
    assert resp.json()["error"] == "GROQ_API_KEY absent. Ajoutez-la a votre .env."


# --- /api/news ----------------------------------------------------------------
#
# Real DB reads only (same discipline as the rest of this file) -- no Groq
# call anywhere on this route's path (news_analysis is already populated by
# an earlier reasoning/analyze_news.py run, this route only reads it).
# AAPL is used as the reference ticker: confirmed via manual curl testing to
# have real news_analysis rows with a full price-before/after (real
# variation, not "insufficient data").

def test_news_returns_expected_top_level_shape():
    resp = client.get("/api/news")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"news", "n_total", "ticker", "limit", "offset"}
    assert isinstance(body["news"], list)
    assert body["ticker"] is None
    assert body["n_total"] >= len(body["news"])


def test_news_item_shape():
    resp = client.get("/api/news", params={"limit": 1})
    items = resp.json()["news"]
    if not items:
        return  # nothing analysed yet in this environment -- not a failure
    item = items[0]
    expected_keys = {
        "news_id", "ticker", "title", "url", "published_at", "source", "company",
        "sector", "importance", "tonalite", "impact", "horizon", "confidence",
        "summary_paragraph", "price_context", "direction_probabilities",
    }
    assert set(item.keys()) == expected_keys
    assert isinstance(item["summary_paragraph"], str) and item["summary_paragraph"]
    price_ctx_keys = {
        "devise", "date_before", "price_before", "price_before_eur",
        "date_after", "price_after", "price_after_eur", "variation_pct",
        "insufficient_data", "insufficient_reason",
    }
    assert set(item["price_context"].keys()) == price_ctx_keys


def test_news_sorted_by_published_at_descending():
    resp = client.get("/api/news", params={"limit": 500})
    dates = [n["published_at"] for n in resp.json()["news"]]
    assert dates == sorted(dates, reverse=True)


def test_news_ticker_filter_scopes_results():
    resp = client.get("/api/news", params={"ticker": "AAPL"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ticker"] == "AAPL"
    assert all(n["ticker"] == "AAPL" for n in body["news"])


def test_news_ticker_filter_is_case_insensitive_and_trimmed():
    resp = client.get("/api/news", params={"ticker": "  aapl  "})
    assert resp.json()["ticker"] == "AAPL"


def test_news_price_context_has_real_variation_for_known_ticker():
    """AAPL has price_history both before and well after its stored news --
    confirmed via manual curl testing -- so insufficient_data must be False
    with a real numeric variation, not a null/placeholder."""
    resp = client.get("/api/news", params={"ticker": "AAPL", "limit": 1})
    items = resp.json()["news"]
    if not items:
        return
    ctx = items[0]["price_context"]
    assert ctx["insufficient_data"] is False
    assert ctx["variation_pct"] is not None
    assert ctx["price_before"] is not None and ctx["price_after"] is not None
    assert ctx["price_before_eur"] is not None  # AAPL trades in USD, EUR rate always resolvable


def test_news_unknown_ticker_returns_empty_not_error():
    resp = client.get("/api/news", params={"ticker": "NOTATICKER123"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["news"] == []
    assert body["n_total"] == 0
    assert body["ticker"] == "NOTATICKER123"


def test_news_default_limit_is_50():
    resp = client.get("/api/news")
    body = resp.json()
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert len(body["news"]) <= 50


def test_news_custom_limit_is_respected():
    resp = client.get("/api/news", params={"limit": 5})
    body = resp.json()
    assert body["limit"] == 5
    assert len(body["news"]) <= 5


def test_news_offset_returns_a_different_page():
    page1 = client.get("/api/news", params={"limit": 10, "offset": 0}).json()
    page2 = client.get("/api/news", params={"limit": 10, "offset": 10}).json()
    assert page1["n_total"] == page2["n_total"]
    urls1 = {n["url"] for n in page1["news"]}
    urls2 = {n["url"] for n in page2["news"]}
    if urls1 and urls2:
        assert urls1.isdisjoint(urls2)


def test_news_n_total_reflects_full_count_not_just_page():
    small = client.get("/api/news", params={"limit": 1}).json()
    full = client.get("/api/news", params={"limit": 500}).json()
    assert small["n_total"] == full["n_total"]


def test_news_limit_beyond_max_returns_422():
    resp = client.get("/api/news", params={"limit": 10000})
    assert resp.status_code == 422


def test_news_negative_offset_returns_422():
    resp = client.get("/api/news", params={"offset": -1})
    assert resp.status_code == 422


# --- direction filter -------------------------------------------------------

def test_news_invalid_direction_returns_422():
    resp = client.get("/api/news", params={"direction": "inexistante"})
    assert resp.status_code == 422
    assert "inexistante" in resp.json()["detail"]


def test_news_direction_filter_matches_only_that_dominant_scenario():
    from reasoning.direction_probability import dominant_direction

    for direction in ("hausse", "stagnation", "baisse"):
        resp = client.get("/api/news", params={"direction": direction, "limit": 500})
        assert resp.status_code == 200
        items = resp.json()["news"]
        for item in items:
            dp = item["direction_probabilities"]
            assert dp is not None
            assert dominant_direction(dp) == direction


def test_news_direction_filter_reduces_or_equals_unfiltered_total():
    unfiltered = client.get("/api/news", params={"limit": 1}).json()["n_total"]
    hausse = client.get("/api/news", params={"direction": "hausse", "limit": 1}).json()["n_total"]
    stagnation = client.get("/api/news", params={"direction": "stagnation", "limit": 1}).json()["n_total"]
    baisse = client.get("/api/news", params={"direction": "baisse", "limit": 1}).json()["n_total"]
    assert hausse <= unfiltered
    assert stagnation <= unfiltered
    assert baisse <= unfiltered
    assert hausse + stagnation + baisse <= unfiltered


def test_news_narrative_unknown_id_returns_404():
    resp = client.get("/api/news/999999999/narrative")
    assert resp.status_code == 404


def test_news_narrative_known_id_never_calls_groq_during_tests():
    """Mirrors test_argued_text_known_ticker_never_calls_groq_during_tests:
    get_or_generate_news_narrative has the same PYTEST_CURRENT_TEST guard as
    add_argued_texts, so a real news_id must come back "cache" (already
    generated during manual testing) or "unavailable" -- never "generated"
    during a pytest run."""
    items = client.get("/api/news", params={"limit": 1}).json()["news"]
    if not items:
        return  # nothing analysed yet in this environment -- not a failure
    news_id = items[0]["news_id"]

    resp = client.get(f"/api/news/{news_id}/narrative")
    assert resp.status_code == 200
    body = resp.json()
    assert body["news_id"] == news_id
    assert body["source"] in ("cache", "unavailable")
    assert set(body.keys()) == {"news_id", "texte", "direction_probabilities", "source"}
