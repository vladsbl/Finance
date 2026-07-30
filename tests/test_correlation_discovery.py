"""Tests for reasoning/correlation_discovery.py's load_pairs_from_relations_generated.

This function used to filter only on resolved=1 (target_ticker matched),
completely ignoring the human-review statut column -- so a pair whose
relation had been explicitly REJECTED by a human reviewer (statut='rejete'),
or one still awaiting review (statut='a_valider'), was fed into the
statistical correlation test exactly like a validated one. That directly
contradicts this module's own docstring, which describes every tested pair
as having "a real, human-inspectable reason to suspect a relationship" --
an unreviewed or rejected proposal is not that. The fix restricts the query
to statut='valide' only."""

import sqlite3

from reasoning.correlation_discovery import (
    classify_market, is_same_market, load_pairs_from_relations_generated,
)

CREATE_SQL = """
CREATE TABLE relations_generated (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_ticker   TEXT NOT NULL,
    relation_type   TEXT NOT NULL,
    target_name     TEXT NOT NULL,
    target_ticker   TEXT,
    resolved        INTEGER NOT NULL DEFAULT 0,
    justification   TEXT,
    confiance       REAL,
    statut          TEXT NOT NULL DEFAULT 'a_valider'
);
"""


def _seed(conn, rows):
    conn.execute(CREATE_SQL)
    conn.executemany(
        "INSERT INTO relations_generated "
        "(source_ticker, relation_type, target_name, target_ticker, resolved, statut) "
        "VALUES (?, ?, ?, ?, 1, ?)",
        rows,
    )
    conn.commit()


def test_excludes_rejected_pairs():
    conn = sqlite3.connect(":memory:")
    _seed(conn, [
        ("AAPL", "concurrent", "Samsung", "005930.KS", "valide"),
        ("AAPL", "fournisseur", "SomeBadMatch", "XXXX", "rejete"),
    ])
    pairs = load_pairs_from_relations_generated(conn)
    assert pairs == [("AAPL", "concurrent", "005930.KS")]


def test_excludes_pending_pairs():
    conn = sqlite3.connect(":memory:")
    _seed(conn, [
        ("MSFT", "client", "NVDA", "NVDA", "valide"),
        ("MSFT", "concurrent", "GOOGL", "GOOGL", "a_valider"),
    ])
    pairs = load_pairs_from_relations_generated(conn)
    assert pairs == [("MSFT", "client", "NVDA")]


def test_includes_only_validated_pairs_when_mixed():
    conn = sqlite3.connect(":memory:")
    _seed(conn, [
        ("JPM", "concurrent", "BAC", "BAC", "valide"),
        ("JPM", "concurrent", "WFC", "WFC", "valide"),
        ("JPM", "concurrent", "C", "C", "rejete"),
        ("JPM", "concurrent", "MS", "MS", "a_valider"),
    ])
    pairs = set(load_pairs_from_relations_generated(conn))
    assert pairs == {("JPM", "concurrent", "BAC"), ("JPM", "concurrent", "WFC")}


def test_returns_empty_when_nothing_validated():
    conn = sqlite3.connect(":memory:")
    _seed(conn, [
        ("TSLA", "concurrent", "RIVN", "RIVN", "rejete"),
        ("TSLA", "concurrent", "GM", "GM", "a_valider"),
    ])
    assert load_pairs_from_relations_generated(conn) == []


# --- classify_market / is_same_market -----------------------------------------
#
# Motivated by a real finding: 14 of 31 lag!=0 correlations discovered on the
# expanded Knowledge Graph turned out to pair a US ticker against a foreign
# one (e.g. ALB -> QYM.MU), 13 of them at exactly lag=+1 -- the signature of
# a time-zone/closing-time artifact (the foreign market absorbing the prior
# US session's overnight information), not a genuine delayed economic
# reaction specific to that pair.

def test_classify_market_plain_ticker_is_us():
    assert classify_market("AAPL") == "US (NYSE/NASDAQ)"


def test_classify_market_share_class_ticker_is_still_us():
    # BRK-B has a hyphen but no "." suffix -- still a plain NYSE/NASDAQ ticker.
    assert classify_market("BRK-B") == "US (NYSE/NASDAQ)"


def test_classify_market_known_suffixes():
    assert classify_market("QYM.MU") == "Allemagne (Munich)"
    assert classify_market("7203.T") == "Japon (Tokyo)"
    assert classify_market("INFY.NS") == "Inde (NSE)"
    assert classify_market("005850.KS") == "Coree du Sud (KOSPI)"


def test_classify_market_unknown_suffix_gets_its_own_distinct_label():
    # Two DIFFERENT unmapped suffixes must not collapse into the same label
    # (which would wrongly mark them as "same market").
    assert classify_market("XXX.ZZ") != classify_market("XXX.QQ")


def test_is_same_market_true_for_two_us_tickers():
    assert is_same_market("AMD", "MSFT") is True


def test_is_same_market_false_for_us_vs_foreign():
    assert is_same_market("ALB", "QYM.MU") is False


def test_is_same_market_true_for_two_tickers_on_same_foreign_exchange():
    assert is_same_market("7203.T", "6301.T") is True
