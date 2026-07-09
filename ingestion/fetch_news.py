#!/usr/bin/env python3
"""Fetch recent news per ticker from two sources and store them in SQLite.

Sources:
  * Yahoo Finance RSS feed (https://finance.yahoo.com/rss/headline?s=TICKER)
  * Finnhub /company-news REST API (last 7 days)

The two sources are merged and de-duplicated (by URL, falling back to a
normalised title), then upserted into the ``news_raw`` table. Re-running is
idempotent thanks to a UNIQUE(ticker, dedup_key) index.

Run directly:
    python ingestion/fetch_news.py

Requires FINNHUB_API_KEY in the environment / .env (Yahoo RSS needs no key).
"""

import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

DB_PATH = os.path.join(REPO_ROOT, "data", "marketdb.db")
DATA_DIR = os.path.dirname(DB_PATH)

# Configure the CA bundle before importing network clients (see ssl_utils).
try:
    from ingestion.ssl_utils import configure_ca_bundle
except ImportError:
    from ssl_utils import configure_ca_bundle

configure_ca_bundle(DATA_DIR)

import requests  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from ingestion.fetch_prices import SYMBOLS  # noqa: E402

YAHOO_RSS_URL = "https://finance.yahoo.com/rss/headline?s={ticker}"
FINNHUB_NEWS_URL = "https://finnhub.io/api/v1/company-news"
FINNHUB_LOOKBACK_DAYS = 7
# Cap Finnhub news per ticker (keep the most recent) to protect the LLM quota.
# Yahoo RSS is already ~20/ticker and needs no cap.
FINNHUB_MAX_PER_TICKER = 20
REQUEST_TIMEOUT = 20
USER_AGENT = "Finance-pipeline/1.0 (+news ingestion)"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fetch_news")


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS news_raw (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker        TEXT NOT NULL,
    source        TEXT NOT NULL,
    title         TEXT NOT NULL,
    url           TEXT,
    published_at  TEXT,
    summary_brut  TEXT,
    dedup_key     TEXT NOT NULL,
    UNIQUE (ticker, dedup_key)
);
"""

INSERT_SQL = """
INSERT OR IGNORE INTO news_raw
    (ticker, source, title, url, published_at, summary_brut, dedup_key)
VALUES
    (:ticker, :source, :title, :url, :published_at, :summary_brut, :dedup_key);
"""


def _dedup_key(url, title):
    """Stable key for de-duplication: prefer the URL, fall back to the title."""
    if url:
        return url.strip().lower().rstrip("/")
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def _clean(text):
    if not text:
        return ""
    # Strip any HTML tags that sometimes appear in RSS descriptions.
    return re.sub(r"<[^>]+>", "", text).strip()


def fetch_yahoo_rss(ticker, session):
    """Return a list of news dicts from the Yahoo Finance RSS feed."""
    url = YAHOO_RSS_URL.format(ticker=ticker)
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    root = ET.fromstring(resp.content)
    items = []
    for item in root.iterfind(".//item"):
        title = _clean(item.findtext("title"))
        if not title:
            continue
        link = (item.findtext("link") or "").strip()
        description = _clean(item.findtext("description"))
        pub = item.findtext("pubDate")
        published_at = _parse_date(pub)
        items.append({
            "ticker": ticker,
            "source": "yahoo_rss",
            "title": title,
            "url": link,
            "published_at": published_at,
            "summary_brut": description,
            "dedup_key": _dedup_key(link, title),
        })
    return items


def fetch_finnhub(ticker, session, api_key):
    """Return a list of news dicts from the Finnhub company-news endpoint."""
    to_date = datetime.now(timezone.utc).date()
    from_date = to_date - timedelta(days=FINNHUB_LOOKBACK_DAYS)
    params = {
        "symbol": ticker,
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
        "token": api_key,
    }
    resp = session.get(FINNHUB_NEWS_URL, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    data = resp.json()
    if not isinstance(data, list):
        data = []
    # Keep only the most recent FINNHUB_MAX_PER_TICKER items.
    data = sorted(data, key=lambda e: e.get("datetime") or 0, reverse=True)
    data = data[:FINNHUB_MAX_PER_TICKER]

    items = []
    for entry in data:
        title = _clean(entry.get("headline"))
        if not title:
            continue
        link = (entry.get("url") or "").strip()
        summary = _clean(entry.get("summary"))
        ts = entry.get("datetime")
        published_at = (
            datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            if ts else None
        )
        items.append({
            "ticker": ticker,
            "source": "finnhub",
            "title": title,
            "url": link,
            "published_at": published_at,
            "summary_brut": summary,
            "dedup_key": _dedup_key(link, title),
        })
    return items


def _parse_date(value):
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        return value


def merge_dedup(*item_lists):
    """Merge news lists, keeping the first occurrence of each (ticker, key)."""
    seen = set()
    merged = []
    for items in item_lists:
        for it in items:
            key = (it["ticker"], it["dedup_key"])
            if key in seen:
                continue
            seen.add(key)
            merged.append(it)
    return merged


def ensure_table(conn):
    conn.execute(CREATE_TABLE_SQL)
    conn.commit()


def main():
    load_dotenv()
    finnhub_key = os.getenv("FINNHUB_API_KEY")
    if not finnhub_key:
        logger.warning("FINNHUB_API_KEY not set - Finnhub source will be skipped.")

    logger.info("Opening SQLite database at %s ...", DB_PATH)
    try:
        conn = sqlite3.connect(DB_PATH)
        ensure_table(conn)
    except sqlite3.Error as exc:
        logger.error("Database error: %s", exc)
        return 1

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    total_inserted = 0
    for ticker in SYMBOLS:
        yahoo_items, finnhub_items = [], []

        try:
            yahoo_items = fetch_yahoo_rss(ticker, session)
        except Exception as exc:
            logger.error("%s: Yahoo RSS failed (%s)", ticker, exc)

        if finnhub_key:
            try:
                finnhub_items = fetch_finnhub(ticker, session, finnhub_key)
            except Exception as exc:
                logger.error("%s: Finnhub failed (%s)", ticker, exc)

        merged = merge_dedup(yahoo_items, finnhub_items)
        if not merged:
            logger.info("%-6s no news", ticker)
            continue

        try:
            cur = conn.executemany(INSERT_SQL, merged)
            conn.commit()
            inserted = cur.rowcount if cur.rowcount is not None else 0
        except sqlite3.Error as exc:
            conn.rollback()
            logger.error("%s: insert failed (%s)", ticker, exc)
            continue

        total_inserted += max(inserted, 0)
        logger.info("%-6s yahoo=%d finnhub=%d merged=%d new=%d",
                    ticker, len(yahoo_items), len(finnhub_items),
                    len(merged), max(inserted, 0))
        # Be gentle with the feeds.
        time.sleep(0.3)

    conn.close()
    logger.info("Done. %d new news rows inserted.", total_inserted)
    return 0


if __name__ == "__main__":
    sys.exit(main())
