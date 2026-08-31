#!/usr/bin/env python3
"""Fetch macro/geopolitical news from official central bank RSS feeds and
store them in SQLite -- Phase 4 V1 ("Contexte geopolitique et economique
mondial"), free/official sources only, no paid economic calendar.

Sources (verified reachable and returning real, current items before this
module was written -- see MACRO_FEEDS below for the exact URLs):
  * Federal Reserve Board -- all press releases (monetary policy releases
    are a subset of this feed, already tagged with their own <category>,
    so a single feed covers both without risking cross-feed duplicates).
  * European Central Bank -- press releases, speeches, interviews and press
    conferences (ECB publishes these as one combined feed, not split by
    type).

Same spirit as ingestion/fetch_news.py: raw storage only, no LLM call at
this step (reasoning/macro_context.py does the LLM synthesis, separately
and on its own quota). Stored in ``macro_news``, a table separate from
``news_raw`` -- that one stays dedicated to per-company news (see its own
docstring), this one is macro/central-bank news with no ticker.

Usage:
    python ingestion/fetch_macro_news.py

Requires no API key -- both feeds are free, public RSS with no auth.
"""

import logging
import os
import sqlite3
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

DB_PATH = os.path.join(REPO_ROOT, "data", "marketdb.db")
DATA_DIR = os.path.dirname(DB_PATH)

# Configure the CA bundle before importing network clients (see ssl_utils),
# same convention as ingestion/fetch_news.py.
try:
    from ingestion.ssl_utils import configure_ca_bundle
except ImportError:
    from ssl_utils import configure_ca_bundle

configure_ca_bundle(DATA_DIR)

import requests  # noqa: E402
from xml.etree import ElementTree as ET  # noqa: E402

# Reused as-is from ingestion/fetch_news.py -- same title-cleaning,
# RFC-822-date-parsing and dedup-key logic, no reason to duplicate it for
# a feed that is structurally the same kind of RSS <item> list.
from ingestion.fetch_news import _clean, _dedup_key, _parse_date  # noqa: E402

# Verified 2026-08-31 by direct HTTP GET (200 OK, real current items dated
# this same week) before writing this module -- see the task's own
# instruction to never assume a feed exists without checking first.
MACRO_FEEDS = {
    "fed": "https://www.federalreserve.gov/feeds/press_all.xml",
    "ecb": "https://www.ecb.europa.eu/rss/press.xml",
}

REQUEST_TIMEOUT = 20
USER_AGENT = "Finance-pipeline/1.0 (+macro news ingestion)"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fetch_macro_news")


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS macro_news (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,
    title         TEXT NOT NULL,
    url           TEXT,
    published_at  TEXT,
    content_raw   TEXT,
    dedup_key     TEXT NOT NULL,
    UNIQUE (source, dedup_key)
);
"""

INSERT_SQL = """
INSERT OR IGNORE INTO macro_news
    (source, title, url, published_at, content_raw, dedup_key)
VALUES
    (:source, :title, :url, :published_at, :content_raw, :dedup_key);
"""


def fetch_feed(source, url, session):
    """Return a list of macro-news dicts from one central-bank RSS feed.

    Generic RSS 2.0 <item> parsing (title/link/description/pubDate) --
    same shape as ingestion/fetch_news.py's fetch_yahoo_rss, minus the
    per-ticker scoping. The ECB feed has no per-item <description> (unlike
    the Fed's), so content_raw is simply empty for those -- title alone is
    still enough signal for the macro filter/synthesis downstream."""
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
            "source": source,
            "title": title,
            "url": link,
            "published_at": published_at,
            "content_raw": description,
            "dedup_key": _dedup_key(link, title),
        })
    return items


def ensure_table(conn):
    conn.execute(CREATE_TABLE_SQL)
    conn.commit()


def main(argv=None):
    logger.info("Opening SQLite database at %s ...", DB_PATH)
    try:
        conn = sqlite3.connect(DB_PATH)
        ensure_table(conn)
    except sqlite3.Error as exc:
        logger.error("Database error: %s", exc)
        return 1

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    total_fetched = total_inserted = 0
    for source, url in MACRO_FEEDS.items():
        try:
            items = fetch_feed(source, url, session)
        except Exception as exc:  # noqa: BLE001
            logger.error("%s: feed fetch failed (%s)", source, exc)
            continue

        total_fetched += len(items)
        inserted = 0
        if items:
            try:
                cur = conn.executemany(INSERT_SQL, items)
                conn.commit()
                inserted = cur.rowcount if cur.rowcount is not None else 0
            except sqlite3.Error as exc:
                conn.rollback()
                logger.error("%s: insert failed (%s)", source, exc)
        total_inserted += max(inserted, 0)
        logger.info("%s: %d items fetched, %d new rows.", source, len(items), max(inserted, 0))

    conn.close()
    logger.info("Done. %d items fetched across %d feed(s), %d new rows inserted.",
                total_fetched, len(MACRO_FEEDS), total_inserted)
    return 0


if __name__ == "__main__":
    sys.exit(main())
