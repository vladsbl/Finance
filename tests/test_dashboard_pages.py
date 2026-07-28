"""Headless tests for the Streamlit dashboard pages (streamlit.testing.AppTest).

Each page registered via st.Page(...) in dashboard/app.py is a plain Python
function, so AppTest.from_function can run it directly without needing the
full multipage navigation shell (AppTest.switch_page only supports file-based
pages, not the callable-based ones used here).

Note: AppTest.from_function re-executes the *source* of the given function in
isolation -- it does not carry over closures, so each test below is a
standalone top-level function with no captured variables (a closure over a
loop/parametrize variable would silently fail with a NameError).

Run:
    pytest tests/test_dashboard_pages.py -v
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from streamlit.testing.v1 import AppTest


def _run_page_overview():
    import dashboard.app as app
    app.page_overview()


def _run_page_stock():
    import dashboard.app as app
    app.page_stock()


def _run_page_news():
    import dashboard.app as app
    app.page_news()


def _run_page_graph():
    import dashboard.app as app
    app.page_graph()


def _run_page_opportunities():
    import dashboard.app as app
    app.page_opportunities()


def _run_page_daily_summary():
    import dashboard.app as app
    app.page_daily_summary()


def _run_page_daily_summary_zero_signals():
    # Force the 0-signal path through the real page code: build_daily_summary
    # looks up MIN_CONFIDENCE as a module-global at call time, so patching it
    # on reasoning.daily_summary (not on dashboard.app's already-imported copy,
    # which is display-only) genuinely makes every candidate ineligible.
    # load_daily_summary's @st.cache_data cache can persist across separate
    # AppTest runs within the same pytest process, so it must be cleared here
    # too, or a previous test's cached (non-empty) result would be reused.
    import dashboard.app as app
    import reasoning.daily_summary as ds
    ds.MIN_CONFIDENCE = 101.0  # impossible threshold -> guarantees 0 signals
    app.load_daily_summary.clear()
    app.page_daily_summary()


def test_page_daily_summary_loads_without_error():
    """'Resume du jour' (new default homepage) must render cleanly."""
    at = AppTest.from_function(_run_page_daily_summary, default_timeout=60).run()
    assert not at.exception, f"page_daily_summary raised: {list(at.exception)}"
    subheaders = [s.value for s in at.subheader]
    assert "Resume du jour" in subheaders


def test_page_daily_summary_handles_zero_signals_without_crash():
    """When no ticker clears the confidence threshold, the page must show a
    clear message instead of crashing (quality-over-quantity is a feature)."""
    at = AppTest.from_function(_run_page_daily_summary_zero_signals, default_timeout=60).run()
    assert not at.exception, f"page_daily_summary raised: {list(at.exception)}"
    warnings = [w.value for w in at.warning]
    assert any("Aucun signal" in w for w in warnings), (
        f"Expected a 'no signal' warning, got warnings: {warnings}"
    )


def test_page_opportunities_loads_without_error():
    """The new 'Opportunites du jour' page (module 9 v1) must render cleanly."""
    at = AppTest.from_function(_run_page_opportunities, default_timeout=60).run()
    assert not at.exception, f"page_opportunities raised: {list(at.exception)}"
    subheaders = [s.value for s in at.subheader]
    assert "Opportunites du jour" in subheaders


def _run_page_causal_reasoning():
    import dashboard.app as app
    app.load_causal_chains.clear()
    app.page_causal_reasoning()


def _run_page_causal_reasoning_zero_chains():
    # No domain threshold to tweak here (unlike MIN_CONFIDENCE for daily
    # summary) -- causal_chains simply has 0 rows in that scenario, so the
    # loader itself is monkeypatched to return an empty result, exercising
    # the real render_causal_reasoning_page() empty-state branch. The stub
    # keeps a no-op .clear() so it stays a safe drop-in replacement for the
    # real @st.cache_data function everywhere the app calls .clear() on it
    # -- module state (dashboard.app) is shared across the whole pytest
    # process, so a plain lambda here would permanently break .clear() for
    # every test that runs afterwards, not just this one.
    import dashboard.app as app

    def _stub(limit=app.CAUSAL_CHAIN_DISPLAY_LIMIT):
        return [], None
    _stub.clear = lambda: None
    app.load_causal_chains = _stub
    app.page_causal_reasoning()


def test_page_causal_reasoning_loads_without_error():
    """'Raisonnement causal' (module 7 dashboard page) must render cleanly."""
    at = AppTest.from_function(_run_page_causal_reasoning, default_timeout=60).run()
    assert not at.exception, f"page_causal_reasoning raised: {list(at.exception)}"
    subheaders = [s.value for s in at.subheader]
    assert "Raisonnement causal" in subheaders


def test_page_causal_reasoning_handles_zero_chains_without_crash():
    """When no causal chain has been generated yet (a likely state given the
    dedicated Groq quota), the page must show a clear info message instead
    of an empty page."""
    at = AppTest.from_function(_run_page_causal_reasoning_zero_chains, default_timeout=60).run()
    assert not at.exception, f"page_causal_reasoning raised: {list(at.exception)}"
    infos = [i.value for i in at.info]
    assert any("Aucune chaine" in i for i in infos), (
        f"Expected a 'no chain' info message, got: {infos}"
    )


def _run_page_causal_reasoning_recalc_available():
    # Deterministic pending/quota numbers (real ones change day to day, and
    # today's real quota may already be exhausted from manual testing) --
    # never touches Groq, only the button's own status display. See
    # _run_page_causal_reasoning_zero_chains for why the stub needs its own
    # no-op .clear().
    import dashboard.app as app

    def _stub(limit=app.CAUSAL_CHAIN_DISPLAY_LIMIT):
        return [], None
    _stub.clear = lambda: None
    app.load_causal_chains = _stub
    app._causal_reasoning_status = lambda conn: (3, 2, 5, 3)
    app.page_causal_reasoning()


def _run_page_causal_reasoning_recalc_click():
    # run_causal_reasoning itself is replaced with a canned result -- this
    # is the module-level name dashboard.app imported from
    # reasoning.causal_reasoning, so reassigning it here intercepts the
    # button handler's call without ever reaching Groq. AppTest reruns this
    # ENTIRE function's source on every .run() (including the rerun
    # triggered by .click()), so the monkeypatches below apply consistently
    # across both runs, not just the first. The button handler itself calls
    # load_causal_chains.clear() after a successful run, so the stub needs
    # a real (no-op) .clear() too -- see _run_page_causal_reasoning_zero_chains.
    import dashboard.app as app

    def _stub(limit=app.CAUSAL_CHAIN_DISPLAY_LIMIT):
        return [], None
    _stub.clear = lambda: None
    app.load_causal_chains = _stub
    app._causal_reasoning_status = lambda conn: (3, 2, 5, 3)
    app.run_causal_reasoning = lambda conn: {
        "n_candidates": 3, "processed": 1, "failed": 0,
        "skipped_no_relations": 0, "quota_used": 3, "quota_limit": 5,
        "quota_exhausted": False, "error": None,
    }
    app.page_causal_reasoning()


def _run_page_causal_reasoning_recalc_quota_exhausted():
    # See _run_page_causal_reasoning_zero_chains for why the stub needs its
    # own no-op .clear().
    import dashboard.app as app

    def _stub(limit=app.CAUSAL_CHAIN_DISPLAY_LIMIT):
        return [], None
    _stub.clear = lambda: None
    app.load_causal_chains = _stub
    app._causal_reasoning_status = lambda conn: (12, 5, 5, 0)
    app.page_causal_reasoning()


def test_page_causal_reasoning_recalc_button_shows_pending_and_quota():
    """The 'Recalculer maintenant' button's status line must show the
    pending-news count and today's quota BEFORE any click, so the user
    knows what to expect."""
    at = AppTest.from_function(_run_page_causal_reasoning_recalc_available, default_timeout=60).run()
    assert not at.exception, f"page_causal_reasoning raised: {list(at.exception)}"
    captions = [c.value for c in at.caption]
    assert any("3 news eligible" in c and "2/5" in c and "3 restant" in c for c in captions), (
        f"Expected the pending/quota status line, got: {captions}"
    )
    buttons = [b for b in at.button if b.label == "Recalculer maintenant"]
    assert buttons and not buttons[0].disabled


def test_page_causal_reasoning_recalc_button_click_generates_chain_mocked():
    """Clicking 'Recalculer maintenant' must call run_causal_reasoning (here
    mocked -- never a real Groq call in tests) and show a success message
    reflecting the returned stats, with the chain list refreshed
    automatically (no manual reload needed)."""
    at = AppTest.from_function(_run_page_causal_reasoning_recalc_click, default_timeout=60).run()
    assert not at.exception, f"page_causal_reasoning raised: {list(at.exception)}"
    buttons = [b for b in at.button if b.label == "Recalculer maintenant"]
    assert buttons, "Expected an enabled 'Recalculer maintenant' button"

    at = buttons[0].click().run()
    assert not at.exception, f"page_causal_reasoning raised after click: {list(at.exception)}"
    successes = [s.value for s in at.success]
    assert any("1 nouvelle" in s for s in successes), (
        f"Expected a success message mentioning the 1 generated chain, got: {successes}"
    )


def test_page_causal_reasoning_recalc_button_disabled_when_quota_exhausted():
    """When today's causal-reasoning quota is already used up, the button
    must be disabled and show the exact 'reessayez demain' message instead
    of a button that would fail silently or crash if clicked."""
    at = AppTest.from_function(
        _run_page_causal_reasoning_recalc_quota_exhausted, default_timeout=60).run()
    assert not at.exception, f"page_causal_reasoning raised: {list(at.exception)}"
    buttons = [b for b in at.button if b.label == "Recalculer maintenant"]
    assert buttons and buttons[0].disabled
    infos = [i.value for i in at.info]
    assert any("Quota atteint pour aujourd'hui, reessayez demain" in i for i in infos), (
        f"Expected the quota-exhausted message, got: {infos}"
    )


def _run_page_correlations():
    import dashboard.app as app
    app.load_correlations.clear()
    app.page_correlations()


def _run_page_correlations_zero_rows():
    # Same discipline as the causal-reasoning zero-state test: no domain
    # threshold to tweak, so the loader itself is monkeypatched to return an
    # empty result, exercising the real render_correlations_page() empty-
    # state branch without touching the real correlations_discovered table.
    import dashboard.app as app
    app.load_correlations = lambda: ([], None)
    app.page_correlations()


def test_page_correlations_loads_without_error():
    """'Correlations decouvertes' (module 8 dashboard page) must render
    cleanly and must never claim causation -- only a correlation/causation
    caveat is expected in its info banner."""
    at = AppTest.from_function(_run_page_correlations, default_timeout=60).run()
    assert not at.exception, f"page_correlations raised: {list(at.exception)}"
    subheaders = [s.value for s in at.subheader]
    assert "Correlations decouvertes" in subheaders
    infos = [i.value for i in at.info]
    assert any("preuve de causalite" in i for i in infos), (
        f"Expected the correlation-is-not-causation caveat, got: {infos}"
    )


def test_page_correlations_handles_zero_rows_without_crash():
    """When no correlation has been discovered/stored yet, the page must
    show a clear info message (with the command to run) instead of an
    empty page."""
    at = AppTest.from_function(_run_page_correlations_zero_rows, default_timeout=60).run()
    assert not at.exception, f"page_correlations raised: {list(at.exception)}"
    infos = [i.value for i in at.info]
    assert any("Aucune correlation" in i for i in infos), (
        f"Expected a 'no correlation' info message, got: {infos}"
    )


def test_opportunities_priority_filter_changes_row_count():
    """Regression test: the 'Priorite univers' filter must offer every real
    universe.priorite value (not just a subset seen in already-computed
    opportunites rows) and must actually narrow the displayed table when a
    tier is selected, rather than always showing every row.

    Uses st.pills (not st.selectbox): a fixed 4-option list is a click
    choice, not something to search for, so a non-searchable widget avoids
    the false "you can type here" affordance a searchable dropdown gives."""
    at = AppTest.from_function(_run_page_opportunities, default_timeout=60).run()
    assert not at.exception, f"page_opportunities raised: {list(at.exception)}"

    sb = at.pills(key="opp_priorite")
    options = set(sb.options)
    assert {"haute", "moyenne", "basse"}.issubset(options), (
        f"Expected haute/moyenne/basse all offered, got: {options}"
    )

    counts = {}
    for choice in sb.options:
        run = AppTest.from_function(_run_page_opportunities, default_timeout=60).run()
        run.pills(key="opp_priorite").set_value(choice).run()
        assert not run.exception, f"[{choice}] raised: {list(run.exception)}"
        counts[choice] = len(run.dataframe[0].value) if run.dataframe else 0

    total = counts.get("toutes")
    others = [n for k, n in counts.items() if k != "toutes"]
    assert total == sum(others), (
        f"'toutes' ({total}) should equal the sum of every tier ({others}): {counts}"
    )
    assert len(set(counts.values())) > 1, (
        f"Selecting a priority had no effect on the row count: {counts}"
    )


def test_page_overview_loads_without_error():
    at = AppTest.from_function(_run_page_overview, default_timeout=60).run()
    assert not at.exception, f"page_overview raised: {list(at.exception)}"


def test_page_stock_loads_without_error():
    at = AppTest.from_function(_run_page_stock, default_timeout=60).run()
    assert not at.exception, f"page_stock raised: {list(at.exception)}"


def test_page_news_loads_without_error():
    at = AppTest.from_function(_run_page_news, default_timeout=60).run()
    assert not at.exception, f"page_news raised: {list(at.exception)}"


def test_page_graph_loads_without_error():
    at = AppTest.from_function(_run_page_graph, default_timeout=60).run()
    assert not at.exception, f"page_graph raised: {list(at.exception)}"


def test_glossaire_loads_with_expected_terms():
    """The glossary dict backing the dashboard's tooltips must load and
    cover the key terms named when it was introduced (RSI, momentum,
    moving average, technical/price-valuation/fundamental scores,
    confidence, volatility, priority)."""
    from dashboard.glossaire import GLOSSAIRE
    assert len(GLOSSAIRE) > 0
    expected = {
        "RSI", "Momentum technique", "Moyenne mobile", "Score technique",
        "Prix/Valorisation", "Fondamental reel", "Confiance", "Volatilite",
        "Breakout", "Priorite", "Correlation", "P-value", "Lag",
        "Significativite statistique",
    }
    missing = expected - set(GLOSSAIRE)
    assert not missing, f"Glossary missing expected terms: {missing}"
    for term, explanation in GLOSSAIRE.items():
        assert explanation.strip(), f"Empty explanation for term {term!r}"


def test_highlight_terms_wraps_known_terms_in_tooltip_spans():
    """highlight_terms() must wrap recognised terms in a span carrying the
    explanation as its title (native browser tooltip), and must escape the
    source text (defence against LLM-generated / externally-scraped
    content being rendered as raw HTML)."""
    from dashboard.glossaire import highlight_terms
    out = highlight_terms("Prix/Valorisation solide (62/100) | Confiance 87%")
    assert "<span" in out and "title=" in out
    assert "Prix/Valorisation" in out

    escaped = highlight_terms("<script>alert(1)</script> Confiance 50%")
    assert "<script>" not in escaped
