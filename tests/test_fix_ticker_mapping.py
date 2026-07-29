"""Tests for universe/fix_ticker_mapping.py's validate_ticker -- never a real
yfinance call, always mocked, per the project's Groq/network test discipline.

validate_ticker used to accept ANY non-empty 5-day history, which let a thin
or near-delisted cross-listing through (A4XA.F, a Frankfurt listing for
American Homes 4 Rent, cleared that bar despite being effectively
untradeable in one observed run). It now requires at least MIN_TRADING_DAYS
distinct non-null-Close rows over a full month, a stronger liquidity bar
that a genuinely thin listing won't clear."""

from unittest.mock import MagicMock, patch

import pandas as pd

from universe.fix_ticker_mapping import MIN_TRADING_DAYS, validate_ticker


def _history_with_closes(n_rows, n_non_null):
    """A DataFrame shaped like yfinance's Ticker.history() output, with
    `n_non_null` of its `n_rows` Close values populated and the rest NaN --
    mimics a listing with sparse/stale trading days rather than a
    continuously-liquid one."""
    index = pd.date_range("2026-06-29", periods=n_rows, freq="D")
    closes = [100.0 + i if i < n_non_null else float("nan") for i in range(n_rows)]
    return pd.DataFrame({
        "Open": closes, "High": closes, "Low": closes, "Close": closes,
        "Volume": [1000] * n_rows,
    }, index=index)


def _mock_ticker(history_return=None, history_side_effect=None):
    ticker = MagicMock()
    if history_side_effect is not None:
        ticker.history.side_effect = history_side_effect
    else:
        ticker.history.return_value = history_return
    return ticker


@patch("universe.fix_ticker_mapping.yf.Ticker")
def test_validate_ticker_accepts_liquid_ticker(mock_ticker_cls):
    mock_ticker_cls.return_value = _mock_ticker(_history_with_closes(22, 22))
    assert validate_ticker("REAL.PA") is True


@patch("universe.fix_ticker_mapping.yf.Ticker")
def test_validate_ticker_accepts_ticker_at_exact_threshold(mock_ticker_cls):
    mock_ticker_cls.return_value = _mock_ticker(_history_with_closes(22, MIN_TRADING_DAYS))
    assert validate_ticker("BORDERLINE.PA") is True


@patch("universe.fix_ticker_mapping.yf.Ticker")
def test_validate_ticker_rejects_thin_ticker_just_under_threshold(mock_ticker_cls):
    mock_ticker_cls.return_value = _mock_ticker(_history_with_closes(22, MIN_TRADING_DAYS - 1))
    assert validate_ticker("THIN.PA") is False


@patch("universe.fix_ticker_mapping.yf.Ticker")
def test_validate_ticker_rejects_stale_ticker_like_a4xa(mock_ticker_cls):
    """The A4XA.F scenario that motivated this hardening: a handful of
    trading days over the last month, nowhere near enough real liquidity to
    trust for the daily pipeline, but enough that the OLD 5-day/non-empty
    check would have accepted it."""
    mock_ticker_cls.return_value = _mock_ticker(_history_with_closes(22, 3))
    assert validate_ticker("A4XA.F") is False


@patch("universe.fix_ticker_mapping.yf.Ticker")
def test_validate_ticker_rejects_empty_history(mock_ticker_cls):
    mock_ticker_cls.return_value = _mock_ticker(pd.DataFrame())
    assert validate_ticker("GHOST.PA") is False


@patch("universe.fix_ticker_mapping.yf.Ticker")
def test_validate_ticker_returns_false_never_raises_on_network_error(mock_ticker_cls):
    mock_ticker_cls.return_value = _mock_ticker(
        history_side_effect=ConnectionError("boom"))
    assert validate_ticker("ERROR.PA") is False
