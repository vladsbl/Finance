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


def test_opportunities_priority_filter_changes_row_count():
    """Regression test: the 'Priorite univers' filter must offer every real
    universe.priorite value (not just a subset seen in already-computed
    opportunites rows) and must actually narrow the displayed table when a
    tier is selected, rather than always showing every row."""
    at = AppTest.from_function(_run_page_opportunities, default_timeout=60).run()
    assert not at.exception, f"page_opportunities raised: {list(at.exception)}"

    sb = at.selectbox(key="opp_priorite")
    options = set(sb.options)
    assert {"haute", "moyenne", "basse"}.issubset(options), (
        f"Expected haute/moyenne/basse all offered, got: {options}"
    )

    counts = {}
    for choice in sb.options:
        run = AppTest.from_function(_run_page_opportunities, default_timeout=60).run()
        run.selectbox(key="opp_priorite").set_value(choice).run()
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
        "Breakout", "Priorite",
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
