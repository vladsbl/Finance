#!/usr/bin/env python3
"""Build the tracked-universe candidate list from major world index constituents.

This step ONLY defines the universe of tickers (stored in the ``universe``
table). It does NOT fetch prices or news -- ingestion at this scale is a
separate, later decision.

Sources (index -> page scraped). Wikipedia constituent tables are generally
well maintained; where the English page has no data table, a specialised list
site is used instead. Each source is documented next to its fetcher below.

Tickers are normalised to the yfinance convention (exchange suffixes:
.PA .DE .L .AS .SW .ST .MI .MC ... for Europe, .T Japan, .KS Korea, .HK Hong
Kong, .SA Brazil, .SS/.SZ China, .NS India; US tickers use '-' not '.').

Run:
    python universe/build_universe.py
"""

import io
import logging
import os
import re
import sqlite3
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

DB_PATH = os.path.join(REPO_ROOT, "data", "marketdb.db")
DATA_DIR = os.path.dirname(DB_PATH)

# Reuse the shared TLS fix so requests works behind intercepting proxies.
from ingestion.ssl_utils import configure_ca_bundle  # noqa: E402

configure_ca_bundle(DATA_DIR)

import pandas as pd  # noqa: E402
import requests  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("build_universe")

USER_AGENT = "Mozilla/5.0 (Finance universe builder; research)"
TIMEOUT = 30

# European country -> (yfinance suffix, currency). Used for STOXX Europe 600,
# whose constituents span many exchanges.
EUROPE = {
    "Switzerland": (".SW", "CHF"),
    "Germany": (".DE", "EUR"),
    "France": (".PA", "EUR"),
    "United Kingdom": (".L", "GBP"),
    "Netherlands": (".AS", "EUR"),
    "Sweden": (".ST", "SEK"),
    "Denmark": (".CO", "DKK"),
    "Norway": (".OL", "NOK"),
    "Finland": (".HE", "EUR"),
    "Italy": (".MI", "EUR"),
    "Spain": (".MC", "EUR"),
    "Belgium": (".BR", "EUR"),
    "Ireland": (".IR", "EUR"),
    "Austria": (".VI", "EUR"),
    "Portugal": (".LS", "EUR"),
    "Luxembourg": (".LU", "EUR"),
    "Poland": (".WA", "PLN"),
    "Czech Republic": (".PR", "CZK"),
    "Greece": (".AT", "EUR"),
    "Jersey": (".L", "GBP"),
    "Guernsey": (".L", "GBP"),
    "Isle of Man": (".L", "GBP"),
}


# --- HTTP / table helpers --------------------------------------------------

def _session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def _read_tables(session, url):
    resp = session.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    return pd.read_html(io.StringIO(resp.text))


def _norm_col(col):
    """Flatten/strip a column label (handles tuples and [footnote] markers)."""
    if isinstance(col, tuple):
        col = col[-1]
    return re.sub(r"\[.*?\]", "", str(col)).strip().lower()


def pick_table(tables, required, min_rows=20):
    """Return the first table whose columns cover all `required` substrings."""
    req = [r.lower() for r in required]
    for t in tables:
        cols = [_norm_col(c) for c in t.columns]
        if len(t) >= min_rows and all(any(r in c for c in cols) for r in req):
            return t
    return None


def _col(df, substr):
    """Return the actual column whose normalised name contains `substr`."""
    for c in df.columns:
        if substr.lower() in _norm_col(c):
            return c
    raise KeyError(substr)


def _digits(value):
    m = re.search(r"\d+", str(value))
    return m.group(0) if m else ""


# --- Per-index fetchers ----------------------------------------------------
# Each returns a list of dicts: ticker, nom, pays, indice_source, devise.

def fetch_sp500(session):
    """S&P 500 (US). Source: Wikipedia 'List of S&P 500 companies'."""
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    t = pick_table(_read_tables(session, url), ["symbol", "security"])
    if t is None:
        raise ValueError("S&P 500 table not found")
    sym, name = _col(t, "symbol"), _col(t, "security")
    out = []
    for _, r in t.iterrows():
        ticker = str(r[sym]).strip().upper().replace(".", "-")
        if ticker and ticker.lower() != "nan":
            out.append(_row(ticker, r[name], "United States", "S&P 500", "USD"))
    return out


def fetch_stoxx600(session):
    """STOXX Europe 600. Source: Wikipedia 'STOXX Europe 600' (Ticker+Country)."""
    url = "https://en.wikipedia.org/wiki/STOXX_Europe_600"
    t = pick_table(_read_tables(session, url), ["ticker", "company", "country"])
    if t is None:
        raise ValueError("STOXX 600 table not found")
    tk, name, country = _col(t, "ticker"), _col(t, "company"), _col(t, "country")
    out, skipped = [], 0
    for _, r in t.iterrows():
        ctry = str(r[country]).strip()
        base = str(r[tk]).strip().upper()
        if not base or base.lower() == "nan":
            continue
        mapping = EUROPE.get(ctry)
        if not mapping:  # unknown exchange -> skip rather than emit bad ticker
            skipped += 1
            continue
        suffix, ccy = mapping
        out.append(_row(base + suffix, r[name], ctry, "STOXX Europe 600", ccy))
    if skipped:
        logger.info("STOXX 600: %d constituents skipped (unmapped country).", skipped)
    return out


def fetch_nikkei225(session):
    """Nikkei 225 (Japan). Source: topforeignstocks.com (English Wikipedia has
    no constituent table). 'Code' column is already yfinance-formatted (.T)."""
    url = "https://topforeignstocks.com/indices/the-components-of-the-nikkei-225-index/"
    t = pick_table(_read_tables(session, url), ["company", "code"])
    if t is None:
        raise ValueError("Nikkei 225 table not found")
    code, name = _col(t, "code"), _col(t, "company")
    out = []
    for _, r in t.iterrows():
        raw = str(r[code]).strip().upper()
        num = _digits(raw)
        if not num:
            continue
        out.append(_row(f"{num}.T", r[name], "Japan", "Nikkei 225", "JPY"))
    return out


def fetch_kospi200(session):
    """KOSPI 200 (South Korea). Source: Wikipedia 'KOSPI 200'. 6-digit code -> .KS."""
    url = "https://en.wikipedia.org/wiki/KOSPI_200"
    t = pick_table(_read_tables(session, url), ["company", "symbol"])
    if t is None:
        raise ValueError("KOSPI 200 table not found")
    sym, name = _col(t, "symbol"), _col(t, "company")
    out = []
    for _, r in t.iterrows():
        num = _digits(r[sym])
        if not num:
            continue
        out.append(_row(f"{num.zfill(6)}.KS", r[name], "South Korea",
                        "KOSPI 200", "KRW"))
    return out


def fetch_hangseng(session):
    """Hang Seng Index (Hong Kong). Source: Wikipedia 'Hang Seng Index'.
    Ticker like 'SEHK: 5' -> zero-padded 4 digits + .HK."""
    url = "https://en.wikipedia.org/wiki/Hang_Seng_Index"
    t = pick_table(_read_tables(session, url), ["ticker", "name"], min_rows=30)
    if t is None:
        raise ValueError("Hang Seng table not found")
    tk, name = _col(t, "ticker"), _col(t, "name")
    out = []
    for _, r in t.iterrows():
        num = _digits(r[tk])
        if not num:
            continue
        out.append(_row(f"{num.zfill(4)}.HK", r[name], "Hong Kong",
                        "Hang Seng", "HKD"))
    return out


def fetch_ibovespa(session):
    """Brazil. Source: Wikipedia 'List of companies listed on B3' (the English
    Ibovespa page has no data table). Ticker like 'ALPA4' -> .SA."""
    url = "https://en.wikipedia.org/wiki/List_of_companies_listed_on_B3"
    t = pick_table(_read_tables(session, url), ["company", "ticker"])
    if t is None:
        raise ValueError("B3 / Ibovespa table not found")
    tk, name = _col(t, "ticker"), _col(t, "company")
    out = []
    for _, r in t.iterrows():
        base = str(r[tk]).strip().upper()
        if not base or base.lower() == "nan":
            continue
        out.append(_row(f"{base}.SA", r[name], "Brazil", "B3 (Brazil)", "BRL"))
    return out


def fetch_csi300(session):
    """CSI 300 (mainland China A-shares). Source: Wikipedia 'CSI 300 Index'.
    Ticker 'SSE: 600519' + Exchange column -> .SS (Shanghai) / .SZ (Shenzhen)."""
    url = "https://en.wikipedia.org/wiki/CSI_300_Index"
    t = pick_table(_read_tables(session, url), ["ticker", "company", "exchange"])
    if t is None:
        raise ValueError("CSI 300 table not found")
    tk, name, exch = _col(t, "ticker"), _col(t, "company"), _col(t, "exchange")
    out = []
    for _, r in t.iterrows():
        num = _digits(r[tk])
        if not num:
            continue
        raw = f"{r[tk]} {r[exch]}".lower()
        suffix = ".SS" if ("shanghai" in raw or "sse" in raw) else ".SZ"
        out.append(_row(f"{num.zfill(6)}{suffix}", r[name], "China",
                        "CSI 300", "CNY"))
    return out


def fetch_nifty50(session):
    """Nifty 50 (India). Source: Wikipedia 'NIFTY 50'. Symbol -> .NS."""
    url = "https://en.wikipedia.org/wiki/NIFTY_50"
    t = pick_table(_read_tables(session, url), ["company", "symbol"], min_rows=40)
    if t is None:
        raise ValueError("Nifty 50 table not found")
    sym, name = _col(t, "symbol"), _col(t, "company")
    out = []
    for _, r in t.iterrows():
        base = str(r[sym]).strip().upper()
        if not base or base.lower() == "nan":
            continue
        out.append(_row(f"{base}.NS", r[name], "India", "Nifty 50", "INR"))
    return out


FETCHERS = [
    ("S&P 500", fetch_sp500),
    ("STOXX Europe 600", fetch_stoxx600),
    ("Nikkei 225", fetch_nikkei225),
    ("KOSPI 200", fetch_kospi200),
    ("Hang Seng", fetch_hangseng),
    ("B3 (Brazil)", fetch_ibovespa),
    ("CSI 300", fetch_csi300),
    ("Nifty 50", fetch_nifty50),
]


# Ingestion priority per index, driven by data-source coverage:
#   haute   = US, full coverage (yfinance + Finnhub + Yahoo RSS)
#   moyenne = developed intl (yfinance + RSS, no Finnhub free coverage)
#   basse   = China / India (mostly yfinance only)
PRIORITY = {
    "S&P 500": "haute",
    "STOXX Europe 600": "moyenne",
    "Nikkei 225": "moyenne",
    "KOSPI 200": "moyenne",
    "Hang Seng": "moyenne",
    "B3 (Brazil)": "moyenne",
    "CSI 300": "basse",
    "Nifty 50": "basse",
}


def _row(ticker, name, pays, indice, devise):
    return {
        "ticker": ticker.strip(),
        "nom": re.sub(r"\s+", " ", str(name)).strip(),
        "pays": pays,
        "indice_source": indice,
        "devise": devise,
        "priorite": PRIORITY.get(indice, "moyenne"),
    }


# --- Persistence -----------------------------------------------------------

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS universe (
    ticker        TEXT PRIMARY KEY,
    nom           TEXT,
    pays          TEXT,
    indice_source TEXT,
    devise        TEXT,
    priorite      TEXT
);
"""

UPSERT_SQL = """
INSERT INTO universe (ticker, nom, pays, indice_source, devise, priorite)
VALUES (:ticker, :nom, :pays, :indice_source, :devise, :priorite)
ON CONFLICT(ticker) DO UPDATE SET
    nom = excluded.nom, pays = excluded.pays,
    indice_source = excluded.indice_source, devise = excluded.devise,
    priorite = excluded.priorite;
"""


def ensure_schema(conn):
    """Create the table and add the priorite column if an older DB lacks it."""
    conn.execute(CREATE_TABLE_SQL)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(universe)")}
    if "priorite" not in cols:
        conn.execute("ALTER TABLE universe ADD COLUMN priorite TEXT")
    conn.commit()


def dedup(rows):
    """Keep the first occurrence of each ticker across all indices."""
    seen, out = set(), []
    for r in rows:
        if not r["ticker"] or r["ticker"] in seen:
            continue
        seen.add(r["ticker"])
        out.append(r)
    return out


def main():
    session = _session()
    all_rows = []
    for label, fetcher in FETCHERS:
        try:
            rows = fetcher(session)
            logger.info("%-18s %4d constituents", label, len(rows))
            all_rows.extend(rows)
        except Exception as exc:  # noqa: BLE001
            logger.error("%-18s FAILED: %s", label, exc)

    if not all_rows:
        logger.error("No constituents fetched. Aborting.")
        return 1

    unique = dedup(all_rows)

    try:
        conn = sqlite3.connect(DB_PATH)
        ensure_schema(conn)
        conn.executemany(UPSERT_SQL, unique)
        conn.commit()
    except sqlite3.Error as exc:
        logger.error("Database error: %s", exc)
        return 1

    _summary(conn, unique, len(all_rows))
    conn.close()
    return 0


def _summary(conn, unique, raw_count):
    logger.info("=" * 52)
    logger.info("Total constituents fetched (with overlaps): %d", raw_count)
    logger.info("Unique tickers after de-duplication:        %d", len(unique))
    logger.info("Rows now in `universe` table:               %d",
                conn.execute("SELECT COUNT(*) FROM universe").fetchone()[0])

    logger.info("--- By country ---")
    for pays, n in conn.execute(
            "SELECT pays, COUNT(*) FROM universe GROUP BY pays "
            "ORDER BY COUNT(*) DESC"):
        logger.info("  %-18s %4d", pays, n)

    logger.info("--- By index source ---")
    for idx, n in conn.execute(
            "SELECT indice_source, COUNT(*) FROM universe GROUP BY indice_source "
            "ORDER BY COUNT(*) DESC"):
        logger.info("  %-18s %4d", idx, n)


if __name__ == "__main__":
    sys.exit(main())
