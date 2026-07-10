#!/usr/bin/env python3
"""Analyse raw news with an LLM (Groq) and store structured results.

For every news row in ``news_raw`` that has no entry in ``news_analysis`` yet,
a strict pre-filter runs first (to protect the limited Groq free quota), then
the survivors are sent to Groq which returns a strict JSON verdict. Results are
cached in ``news_analysis`` so a news item is never analysed twice.

Usage:
    python reasoning/analyze_news.py --dry-run            # estimate quota only
    python reasoning/analyze_news.py                      # analyse everything
    python reasoning/analyze_news.py --tickers AAPL,MSFT  # subset of tickers
    python reasoning/analyze_news.py --limit 20           # cap this run

Requires GROQ_API_KEY in the environment / .env.
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import date

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

DB_PATH = os.path.join(REPO_ROOT, "data", "marketdb.db")
DATA_DIR = os.path.dirname(DB_PATH)

# Reuse the shared TLS fix; httpx (used by groq) ignores the SSL_CERT_FILE env
# var, so we also pass the bundle explicitly via verify=.
from ingestion.ssl_utils import configure_ca_bundle  # noqa: E402

CA_BUNDLE = configure_ca_bundle(DATA_DIR)

# Local (no-LLM) importance scoring, used to order candidates before the daily
# quota cap so the Groq budget goes to the news that matter most.
from reasoning.prioritize_news import compute_scores as compute_priority_scores  # noqa: E402

# --- Configuration ---------------------------------------------------------

# Most recent capable model on the Groq free tier at time of writing.
GROQ_MODEL = "llama-3.3-70b-versatile"

# Groq's free tier is limited (~1000 requests/day). Stay under it.
DAILY_CALL_LIMIT = 1000

MIN_TITLE_LEN = 20
MAX_RETRIES = 5          # for 429 rate-limit backoff
BACKOFF_BASE = 2.0       # seconds: 2, 4, 8, 16, 32

# Titles that look like ads / sponsored content are dropped before the LLM.
SPONSORED_PATTERNS = re.compile(
    r"\b(sponsored|advertisement|promoted|paid\s+post|paid\s+program|"
    r"presented\s+by|\[ad\]|advertorial|partner\s+content)\b",
    re.IGNORECASE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("analyze_news")


CREATE_ANALYSIS_SQL = """
CREATE TABLE IF NOT EXISTS news_analysis (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    news_id     INTEGER NOT NULL UNIQUE,
    company     TEXT,
    sector      TEXT,
    importance  INTEGER,
    tonalite    TEXT,
    impact      TEXT,
    horizon     TEXT,
    confidence  REAL,
    model       TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (news_id) REFERENCES news_raw (id)
);
"""

CREATE_USAGE_SQL = """
CREATE TABLE IF NOT EXISTS llm_usage (
    day   TEXT PRIMARY KEY,
    calls INTEGER NOT NULL DEFAULT 0
);
"""

INSERT_ANALYSIS_SQL = """
INSERT OR IGNORE INTO news_analysis
    (news_id, company, sector, importance, tonalite, impact,
     horizon, confidence, model)
VALUES
    (:news_id, :company, :sector, :importance, :tonalite, :impact,
     :horizon, :confidence, :model);
"""

# Unanalysed news, optionally restricted to a set of tickers.
UNANALYSED_SQL = """
SELECT r.id, r.ticker, r.title, r.summary_brut
FROM news_raw r
LEFT JOIN news_analysis a ON a.news_id = r.id
WHERE a.news_id IS NULL
ORDER BY r.ticker, r.id;
"""

# Titles already analysed, per ticker (to reject near-duplicates cheaply).
ANALYSED_TITLES_SQL = """
SELECT r.ticker, r.title
FROM news_analysis a
JOIN news_raw r ON r.id = a.news_id;
"""


# --- Pre-filter ------------------------------------------------------------

def _normalise_title(title):
    """Lowercase, strip punctuation, collapse whitespace for dup detection."""
    t = re.sub(r"[^\w\s]", " ", (title or "").lower())
    return re.sub(r"\s+", " ", t).strip()


def load_analysed_titles(conn):
    """Return {ticker: set(normalised_title)} for already-analysed news."""
    seen = {}
    for ticker, title in conn.execute(ANALYSED_TITLES_SQL):
        seen.setdefault(ticker, set()).add(_normalise_title(title))
    return seen


def prefilter(rows, analysed_titles):
    """Split candidate rows into kept vs. reasons-for-rejection counters.

    Rejects titles that are too short, look sponsored, or are near-duplicates
    of a title already analysed for the same ticker (or seen earlier in this
    same run). Returns (kept_rows, stats_dict).
    """
    kept = []
    stats = {"too_short": 0, "sponsored": 0, "duplicate": 0, "kept": 0}
    seen = {tk: set(titles) for tk, titles in analysed_titles.items()}

    for news_id, ticker, title, summary in rows:
        title = title or ""
        if len(title.strip()) < MIN_TITLE_LEN:
            stats["too_short"] += 1
            continue
        if SPONSORED_PATTERNS.search(title):
            stats["sponsored"] += 1
            continue

        norm = _normalise_title(title)
        ticker_seen = seen.setdefault(ticker, set())
        if norm in ticker_seen:
            stats["duplicate"] += 1
            continue
        ticker_seen.add(norm)

        kept.append((news_id, ticker, title, summary))
        stats["kept"] += 1

    return kept, stats


# --- Daily usage counter ---------------------------------------------------

def get_usage(conn, day):
    row = conn.execute("SELECT calls FROM llm_usage WHERE day = ?", (day,)).fetchone()
    return row[0] if row else 0


def bump_usage(conn, day):
    conn.execute(
        "INSERT INTO llm_usage (day, calls) VALUES (?, 1) "
        "ON CONFLICT(day) DO UPDATE SET calls = calls + 1",
        (day,),
    )
    conn.commit()


# --- LLM call --------------------------------------------------------------

SYSTEM_PROMPT = (
    "Tu es un analyste financier. On te donne un titre de news et un court "
    "resume concernant une action. Reponds UNIQUEMENT avec un objet JSON "
    "valide, sans texte autour, avec exactement ces cles:\n"
    '{\n'
    '  "company": string,            // entreprise concernee\n'
    '  "sector": string,             // secteur d\'activite\n'
    '  "importance": integer,        // 1 (anecdotique) a 10 (majeur)\n'
    '  "tonalite": string,           // "positive" | "neutre" | "negative"\n'
    '  "impact": string,             // impact probable, une phrase courte\n'
    '  "horizon": string,            // "court terme" | "moyen terme" | "long terme"\n'
    '  "confidence": integer         // 0 a 100, niveau de confiance\n'
    '}'
)


def _is_rate_limit(exc):
    status = getattr(exc, "status_code", None)
    return status == 429 or "429" in str(exc) or "rate limit" in str(exc).lower()


def _coerce_int(value, lo, hi, default):
    try:
        v = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def analyse_one(client, ticker, title, summary):
    """Call Groq for one news item and return a parsed/validated dict."""
    user_prompt = (
        f"Ticker: {ticker}\nTitre: {title}\nResume: {summary or '(aucun)'}"
    )
    completion = client.chat.completions.create(
        model=GROQ_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )
    content = completion.choices[0].message.content
    data = json.loads(content)
    return {
        "company": str(data.get("company", "")).strip() or ticker,
        "sector": str(data.get("sector", "")).strip(),
        "importance": _coerce_int(data.get("importance"), 1, 10, 5),
        "tonalite": str(data.get("tonalite", "neutre")).strip().lower(),
        "impact": str(data.get("impact", "")).strip(),
        "horizon": str(data.get("horizon", "")).strip().lower(),
        "confidence": _coerce_int(data.get("confidence"), 0, 100, 50),
    }


def analyse_with_retry(client, ticker, title, summary):
    """analyse_one with exponential backoff on 429 rate limits."""
    for attempt in range(MAX_RETRIES):
        try:
            return analyse_one(client, ticker, title, summary)
        except Exception as exc:  # noqa: BLE001
            if _is_rate_limit(exc) and attempt < MAX_RETRIES - 1:
                wait = BACKOFF_BASE * (2 ** attempt)
                logger.warning("Rate limit (429). Backoff %.0fs (try %d/%d)...",
                               wait, attempt + 1, MAX_RETRIES)
                time.sleep(wait)
                continue
            raise
    return None


# --- Orchestration ---------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(description="Analyse news with Groq LLM.")
    p.add_argument("--dry-run", action="store_true",
                   help="Report how many news would be analysed; no API calls.")
    p.add_argument("--limit", type=int, default=None,
                   help="Maximum number of news to analyse in this run.")
    p.add_argument("--tickers", type=str, default=None,
                   help="Comma-separated subset of tickers, e.g. AAPL,MSFT.")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    tickers = None
    if args.tickers:
        tickers = {t.strip().upper() for t in args.tickers.split(",") if t.strip()}

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(CREATE_ANALYSIS_SQL)
        conn.execute(CREATE_USAGE_SQL)
        conn.commit()
    except sqlite3.Error as exc:
        logger.error("Database error: %s", exc)
        return 1

    rows = conn.execute(UNANALYSED_SQL).fetchall()
    if tickers:
        rows = [r for r in rows if r[1] in tickers]

    # Order by descending local priority score (ticker priority, price move,
    # volume anomaly, keywords, freshness, cross-source bonus -- see
    # reasoning/prioritize_news.py) BEFORE the pre-filter, so that when the
    # pre-filter drops a cross-source duplicate it keeps the higher-scored
    # copy (it always keeps the first-seen occurrence per ticker).
    priority_scores = {r["news_id"]: r["score"] for r in compute_priority_scores(conn)}
    rows.sort(key=lambda r: priority_scores.get(r[0], 0.0), reverse=True)

    analysed_titles = load_analysed_titles(conn)
    kept, stats = prefilter(rows, analysed_titles)

    today = date.today().isoformat()
    used_today = get_usage(conn, today)
    remaining = max(0, DAILY_CALL_LIMIT - used_today)

    logger.info("Unanalysed: %d | pre-filter -> keep %d "
                "(too_short=%d sponsored=%d duplicate=%d)",
                len(rows), stats["kept"], stats["too_short"],
                stats["sponsored"], stats["duplicate"])
    logger.info("Daily quota: used %d / %d today, %d remaining.",
                used_today, DAILY_CALL_LIMIT, remaining)

    to_analyse = kept
    if args.limit is not None:
        to_analyse = to_analyse[:args.limit]

    if args.dry_run:
        would = min(len(to_analyse), remaining)
        logger.info("[DRY-RUN] Would analyse %d news (%d after quota cap). "
                    "No API calls made.", len(to_analyse), would)
        conn.close()
        return 0

    if not to_analyse:
        logger.info("Nothing to analyse.")
        conn.close()
        return 0

    if remaining <= 0:
        logger.warning("Daily quota reached (%d). Stopping before any call.",
                       DAILY_CALL_LIMIT)
        conn.close()
        return 0

    from dotenv import load_dotenv
    load_dotenv()
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        logger.error("GROQ_API_KEY not set. Add it to your .env.")
        conn.close()
        return 1

    import httpx
    from groq import Groq
    http_client = httpx.Client(verify=CA_BUNDLE) if CA_BUNDLE else None
    client = Groq(api_key=api_key, http_client=http_client)

    analysed = 0
    failed = 0
    for news_id, ticker, title, summary in to_analyse:
        if get_usage(conn, today) >= DAILY_CALL_LIMIT:
            logger.warning("Daily quota reached mid-run. Stopping.")
            break
        try:
            result = analyse_with_retry(client, ticker, title, summary)
        except Exception as exc:  # noqa: BLE001
            logger.error("news_id=%s: LLM call failed (%s)", news_id, exc)
            failed += 1
            continue

        if result is None:
            failed += 1
            continue

        result.update({"news_id": news_id, "model": GROQ_MODEL})
        try:
            conn.execute(INSERT_ANALYSIS_SQL, result)
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            logger.error("news_id=%s: insert failed (%s)", news_id, exc)
            failed += 1
            continue

        bump_usage(conn, today)
        analysed += 1
        if analysed % 10 == 0:
            logger.info("Progress: %d analysed (%d calls used today).",
                        analysed, get_usage(conn, today))

    logger.info("Done. Analysed %d, failed %d. Calls used today: %d/%d.",
                analysed, failed, get_usage(conn, today), DAILY_CALL_LIMIT)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
