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
