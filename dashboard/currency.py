"""Display-only EUR currency conversion for the dashboard.

Every raw price shown in the app is converted to EUR AT DISPLAY TIME ONLY --
nothing is ever re-stored in the database in a different currency, and the
historical price series (price_history, stocks) stays exactly as ingested.
This module just answers "what is this native-currency amount worth in
EUR right now" using a rate cached once per calendar day (exchange_rates
table), so a page load never triggers a fresh network call.

Currencies actually present in `universe.devise` (verified against the
real data, not assumed from ticker suffixes -- see graph/build_graph.py-era
mistakes about guessing from formatting): USD, CNY, EUR, JPY, KRW, GBP,
BRL, HKD, INR, CHF, SEK, NOK, DKK, PLN.
"""

import os
import sqlite3
from datetime import date

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(REPO_ROOT, "data", "marketdb.db")

# Native-currency symbol/code used ONLY when no EUR rate is available for
# that day (fallback display -- never a fake EUR label on an unconverted
# amount). Deliberately covers exactly the currencies seen in
# universe.devise, not a generic worldwide list.
CURRENCY_SYMBOLS = {
    "EUR": "€", "USD": "$", "GBP": "£", "JPY": "¥", "CNY": "¥",
    "KRW": "₩", "BRL": "R$", "HKD": "HK$", "INR": "₹", "CHF": "CHF",
    "SEK": "kr", "NOK": "kr", "DKK": "kr", "PLN": "zl",
}

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS exchange_rates (
    currency    TEXT NOT NULL,
    day         TEXT NOT NULL,
    rate_to_eur REAL NOT NULL,
    fetched_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (currency, day)
);
"""


def ensure_table(conn):
    conn.execute(CREATE_TABLE_SQL)
    conn.commit()


def _fetch_rate_to_eur(currency):
    """Real-time rate (1 unit of `currency` in EUR), via yfinance's
    {CURRENCY}EUR=X FX pair (e.g. "USDEUR=X", "JPYEUR=X"). Returns None on
    any failure -- missing pair, network error, empty history -- callers
    must fall back to the native currency, never fabricate a rate."""
    if currency == "EUR":
        return 1.0
    try:
        from ingestion.ssl_utils import configure_ca_bundle
        configure_ca_bundle(os.path.dirname(DB_PATH))
        import yfinance as yf
        hist = yf.Ticker(f"{currency}EUR=X").history(period="5d")
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception:  # noqa: BLE001
        return None


def get_rate_to_eur(conn, currency, today=None):
    """EUR conversion rate for `currency`, cached once per calendar day in
    `exchange_rates` (traceable: each row keeps the day it was fetched).
    Returns None if no rate could be obtained -- never guesses."""
    if not currency:
        return None
    today = today or date.today().isoformat()
    ensure_table(conn)
    row = conn.execute(
        "SELECT rate_to_eur FROM exchange_rates WHERE currency = ? AND day = ?",
        (currency, today),
    ).fetchone()
    if row is not None:
        return row[0]

    rate = _fetch_rate_to_eur(currency)
    if rate is None:
        return None
    conn.execute(
        "INSERT INTO exchange_rates (currency, day, rate_to_eur) VALUES (?, ?, ?) "
        "ON CONFLICT(currency, day) DO UPDATE SET rate_to_eur = excluded.rate_to_eur",
        (currency, today, rate),
    )
    conn.commit()
    return rate


def format_amount(amount, currency, rate):
    """'12.34 €' if `rate` is available (not None), else the native
    currency with its own symbol (e.g. '315.08 $') -- never a fake EUR
    label on an unconverted amount. `amount` may be None/NaN -> 'N/A'."""
    if amount is None:
        return "N/A"
    try:
        if amount != amount:  # NaN, without importing pandas/math here
            return "N/A"
    except TypeError:
        pass
    if currency == "EUR":
        return f"{amount:,.2f} €"
    if rate is None:
        symbol = CURRENCY_SYMBOLS.get(currency, currency)
        return f"{amount:,.2f} {symbol}"
    return f"{amount * rate:,.2f} €"
