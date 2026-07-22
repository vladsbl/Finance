#!/usr/bin/env python3
"""Fetch recent news per ticker from the tracked universe and store them in SQLite.

Sources (chosen per ticker's ingestion priority, from the ``universe`` table):
  * Yahoo Finance RSS feed (https://finance.yahoo.com/rss/headline?s=TICKER)
    -- used for every ticker, all priorities.
  * Finnhub /company-news REST API (last 7 days)
    -- US-only (``priorite = haute``): Finnhub's free tier returns HTTP 403 on
    non-US tickers (see diagnostics/international_coverage_report.md), so
    ``moyenne``/``basse`` tickers skip it entirely rather than waste calls.

The two sources are merged and de-duplicated (by URL, falling back to a
normalised title), then upserted into the ``news_raw`` table. Re-running is
idempotent thanks to a UNIQUE(ticker, dedup_key) index.

Tickers are read from the ``universe`` table (populated by
universe/build_universe.py), processed in BATCHES with a pause between
batches to avoid hammering Yahoo's RSS feed across ~1900 tickers at once.

Usage:
    python ingestion/fetch_news.py --priorite haute  --limit 100
    python ingestion/fetch_news.py --priorite moyenne --limit 100
    python ingestion/fetch_news.py --priorite toutes            # full universe

Options:
    --priorite   haute | moyenne | basse | toutes   (default: toutes)
    --limit N    cap the number of tickers processed (default: no cap)
    --batch-size N   tickers per batch (default: 50)
    --pause S    seconds slept between batches (default: 3.0)

Requires FINNHUB_API_KEY in the environment / .env (Yahoo RSS needs no key).
"""

import argparse
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

YAHOO_RSS_URL = "https://finance.yahoo.com/rss/headline?s={ticker}"
FINNHUB_NEWS_URL = "https://finnhub.io/api/v1/company-news"
FINNHUB_LOOKBACK_DAYS = 7
# Cap Finnhub news per ticker (keep the most recent) to protect the LLM quota.
# Yahoo RSS is already ~20/ticker and needs no cap.
FINNHUB_MAX_PER_TICKER = 20
# Finnhub free tier only covers US tickers (HTTP 403 elsewhere) -- see
# diagnostics/international_coverage_report.md. Only these priorities call it.
FINNHUB_PRIORITIES = {"haute"}
REQUEST_TIMEOUT = 20
USER_AGENT = "Finance-pipeline/1.0 (+news ingestion)"
PER_TICKER_SLEEP = 0.3  # be gentle with the feeds within a batch

VALID_PRIORITIES = {"haute", "moyenne", "basse"}

# Retry/backoff on Finnhub 429s -- same pattern as reasoning/analyze_news.py's
# Groq retry (exponential backoff: 2, 4, 8, 16, 32s).
MAX_RETRIES = 5
BACKOFF_BASE = 2.0

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


def _is_rate_limit(exc):
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status == 429 or "429" in str(exc) or "rate limit" in str(exc).lower()


def _is_forbidden(exc):
    """Finnhub's free tier returns HTTP 403 for non-US tickers (see
    diagnostics/international_coverage_report.md) -- expected and routine at
    priorite=haute scale (haute includes many international tickers, not just
    US large caps), never a real failure."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status == 403


def fetch_finnhub_with_retry(ticker, session, api_key):
    """fetch_finnhub with exponential backoff on 429 rate limits.

    Finnhub's free tier enforces its own per-minute call rate independent of
    Yahoo; hammering it sequentially across hundreds of US tickers triggers
    429s well before the ticker list is exhausted. Same backoff pattern as
    Groq's retry in reasoning/analyze_news.py (2, 4, 8, 16, 32s).
    """
    for attempt in range(MAX_RETRIES):
        try:
            return fetch_finnhub(ticker, session, api_key)
        except Exception as exc:  # noqa: BLE001
            if _is_rate_limit(exc) and attempt < MAX_RETRIES - 1:
                wait = BACKOFF_BASE * (2 ** attempt)
                logger.warning("%s: Finnhub rate limit (429). Backoff %.0fs (try %d/%d)...",
                               ticker, wait, attempt + 1, MAX_RETRIES)
                time.sleep(wait)
                continue
            raise
    return []


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


def load_tickers(conn, priorite, limit):
    """Return [(ticker, priorite), ...] from the universe table.

    Mirrors ingestion/ingest_universe_prices.py's --priorite/--limit contract
    for consistency across the two large-scale ingestion scripts.
    """
    if priorite == "toutes":
        sql = "SELECT ticker, priorite FROM universe ORDER BY priorite, ticker"
        params = ()
    else:
        sql = "SELECT ticker, priorite FROM universe WHERE priorite = ? ORDER BY ticker"
        params = (priorite,)
    rows = conn.execute(sql, params).fetchall()
    if limit is not None:
        rows = rows[:limit]
    return rows


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def fetch_one(ticker, priorite, session, finnhub_key):
    """Fetch + merge news for a single ticker. Returns (merged_items, n_yahoo,
    n_finnhub, finnhub_status), finnhub_status one of "ok" (called, may be 0
    results), "forbidden" (expected 403, non-US ticker), "error" (a genuine
    failure), or "skipped" (no key / priorite not in FINNHUB_PRIORITIES)."""
    yahoo_items, finnhub_items = [], []
    finnhub_status = "skipped"

    try:
        yahoo_items = fetch_yahoo_rss(ticker, session)
    except Exception as exc:  # noqa: BLE001
        logger.error("%s: Yahoo RSS failed (%s)", ticker, exc)

    # priorite=haute is not US-only (it includes many international tickers,
    # e.g. Tokyo/London/Seoul large caps) -- Finnhub's free tier 403s on all
    # of those. That is an EXPECTED, routine outcome at this scale, not a
    # failure: logged at debug level and tracked as its own status rather
    # than as an error.
    if finnhub_key and priorite in FINNHUB_PRIORITIES:
        try:
            finnhub_items = fetch_finnhub_with_retry(ticker, session, finnhub_key)
            finnhub_status = "ok"
        except Exception as exc:  # noqa: BLE001
            if _is_forbidden(exc):
                logger.debug("%s: Finnhub 403 (non-US ticker, expected).", ticker)
                finnhub_status = "forbidden"
            else:
                logger.error("%s: Finnhub failed (%s)", ticker, exc)
                finnhub_status = "error"

    merged = merge_dedup(yahoo_items, finnhub_items)
    return merged, len(yahoo_items), len(finnhub_items), finnhub_status


def parse_args(argv):
    p = argparse.ArgumentParser(description="Batch universe news ingestion.")
    p.add_argument("--priorite", default="toutes",
                   choices=["haute", "moyenne", "basse", "toutes"])
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--batch-size", type=int, default=50)
    p.add_argument("--pause", type=float, default=3.0)
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])

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

    try:
        tickers = load_tickers(conn, args.priorite, args.limit)
    except sqlite3.Error as exc:
        logger.error("Could not read universe (run universe/build_universe.py): %s", exc)
        conn.close()
        return 1

    if not tickers:
        logger.warning("No tickers for priorite=%s. Nothing to do.", args.priorite)
        conn.close()
        return 1

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    batches = list(_chunks(tickers, args.batch_size))
    logger.info("Priorite=%s | %d tickers | %d batch(es) of %d | pause=%ss",
                args.priorite, len(tickers), len(batches), args.batch_size,
                args.pause)

    start = time.time()
    total_inserted = total_yahoo = total_finnhub = 0
    finnhub_status_counts = {"ok": 0, "forbidden": 0, "error": 0, "skipped": 0}

    for b_i, batch in enumerate(batches, start=1):
        b_start = time.time()
        batch_inserted = 0
        for ticker, priorite in batch:
            merged, n_yahoo, n_finnhub, finnhub_status = fetch_one(
                ticker, priorite, session, finnhub_key)
            total_yahoo += n_yahoo
            total_finnhub += n_finnhub
            finnhub_status_counts[finnhub_status] += 1

            if merged:
                try:
                    cur = conn.executemany(INSERT_SQL, merged)
                    conn.commit()
                    inserted = cur.rowcount if cur.rowcount is not None else 0
                except sqlite3.Error as exc:
                    conn.rollback()
                    logger.error("%s: insert failed (%s)", ticker, exc)
                    inserted = 0
                batch_inserted += max(inserted, 0)

            time.sleep(PER_TICKER_SLEEP)

        total_inserted += batch_inserted
        logger.info("Batch %d/%d: %d tickers, %d new rows, %.1fs",
                    b_i, len(batches), len(batch), batch_inserted,
                    time.time() - b_start)
        if b_i < len(batches):
            time.sleep(args.pause)

    elapsed = time.time() - start
    conn.close()

    logger.info("=" * 52)
    logger.info("Done in %.1fs. %d tickers | yahoo=%d finnhub=%d fetched | "
                "%d new rows inserted.", elapsed, len(tickers), total_yahoo,
                total_finnhub, total_inserted)
    logger.info(
        "Finnhub: %d ok | %d 403 non-US (attendu, pas un echec) | "
        "%d echecs reels | %d ignores (hors haute / pas de cle).",
        finnhub_status_counts["ok"], finnhub_status_counts["forbidden"],
        finnhub_status_counts["error"], finnhub_status_counts["skipped"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
