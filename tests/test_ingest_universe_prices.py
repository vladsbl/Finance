"""Tests for ingestion/ingest_universe_prices.py's yf.download column
unwrapping -- never a real network call, always a hand-built DataFrame
mirroring yfinance's actual shape.

yf.download(..., group_by="ticker") returns MultiIndex columns (ticker,
field) REGARDLESS of how many tickers were requested -- a single-ticker call
is still MultiIndex-shaped, not flat. _ticker_frame used to assume otherwise
(``if single: return data``), which for exactly one ticker returned the
frame with its MultiIndex columns untouched: every ``r.get("Close")`` in
store_batch then looked up a plain string key that didn't exist (the real
key was the tuple ``(ticker, "Close")``), silently returning None -- so
every OHLCV value for a single-ticker run was stored as NULL, with no error
or warning anywhere in the pipeline. This is exactly what happened to A4XA.F
when it was re-ingested alone."""

import sqlite3

import pandas as pd

from ingestion.ingest_universe_prices import (
    CREATE_TABLE_SQL, _ticker_frame, store_batch,
)


def _multiindex_download(tickers, n_days=3):
    """A DataFrame shaped exactly like a real yf.download(..., group_by=
    "ticker") result -- MultiIndex columns (ticker, field), whether `tickers`
    has one entry or several."""
    columns = pd.MultiIndex.from_product(
        [tickers, ["Open", "High", "Low", "Close", "Volume"]],
        names=["Ticker", "Price"])
    index = pd.date_range("2026-01-01", periods=n_days, freq="D")
    rows = []
    for day in range(n_days):
        row = []
        for _ in tickers:
            base = 100.0 + day
            row.extend([base, base + 1, base - 1, base + 0.5, 1000 + day])
        rows.append(row)
    return pd.DataFrame(rows, index=index, columns=columns)


def _memory_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(CREATE_TABLE_SQL)
    conn.commit()
    return conn


def test_ticker_frame_unwraps_multiindex_for_single_ticker():
    """The exact regression case: yf.download(['AAPL'], group_by='ticker')
    still returns MultiIndex columns for that one ticker."""
    data = _multiindex_download(["AAPL"])
    frame = _ticker_frame(data, "AAPL")
    assert not isinstance(frame.columns, pd.MultiIndex)
    assert list(frame["Close"]) == [100.5, 101.5, 102.5]


def test_ticker_frame_unwraps_multiindex_for_multi_ticker_batch():
    data = _multiindex_download(["AAPL", "MSFT"])
    frame = _ticker_frame(data, "MSFT")
    assert not isinstance(frame.columns, pd.MultiIndex)
    assert list(frame["Close"]) == [100.5, 101.5, 102.5]


def test_store_batch_writes_real_close_values_for_single_ticker():
    """End-to-end: a single-ticker download must produce non-NULL OHLCV rows
    in price_history, not silently-NULL ones."""
    conn = _memory_conn()
    data = _multiindex_download(["AAPL"])
    rows, ok = store_batch(conn, data, ["AAPL"])
    assert ok == 1
    assert rows == 3
    stored = conn.execute(
        "SELECT date, open, close, volume FROM price_history "
        "WHERE ticker = 'AAPL' ORDER BY date"
    ).fetchall()
    assert all(r[1] is not None and r[2] is not None for r in stored), (
        f"Expected real (non-NULL) OHLCV values, got: {stored}"
    )
    assert [r[2] for r in stored] == [100.5, 101.5, 102.5]


def test_store_batch_writes_real_close_values_for_multi_ticker_batch():
    conn = _memory_conn()
    data = _multiindex_download(["AAPL", "MSFT"])
    rows, ok = store_batch(conn, data, ["AAPL", "MSFT"])
    assert ok == 2
    assert rows == 6
    stored = conn.execute(
        "SELECT ticker, close FROM price_history ORDER BY ticker, date"
    ).fetchall()
    assert all(close is not None for _, close in stored), (
        f"Expected real (non-NULL) close values, got: {stored}"
    )
