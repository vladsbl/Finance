#!/usr/bin/env python3
"""Analyse raw news with an LLM (Groq) and store structured results.

For every news row in ``news_raw`` that has no entry in ``news_analysis`` yet,
a strict pre-filter runs first (to protect the limited Groq free quota), then
the survivors are sent to Groq which returns a strict JSON verdict. Results are
cached in ``news_analysis`` so a news item is never analysed twice.

Candidates are drawn from ALL tickers with collected news (no ticker
restriction in this module -- see reasoning/prioritize_news.py for the
ranking), ordered by descending priority score, and capped two ways:
  * DAILY_CALL_LIMIT total Groq calls/day (existing counter, ``llm_usage``).
  * MAX_NEWS_PER_TICKER_PER_DAY (5) analyses per ticker/day, so a single
    heavily-covered ticker cannot absorb the whole day's budget at the
    expense of the other haute-priority tickers.

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
from graph.build_graph import build_graph, direct_relations, load_relations  # noqa: E402
from reasoning.direction_probability import (  # noqa: E402
    HORIZON_BASE,
    HORIZON_NEWS,
    compute_direction_probabilities,
    load_causal_effect_for_ticker,
)

# GROQ_MODEL/MAX_RETRIES/BACKOFF_BASE/MAX_ATTEMPTS_PER_RUN/
# MAX_CONSECUTIVE_FAILURES: shared across every Groq-calling module -- see
# reasoning/groq_config.py's own docstring for why (a single place to fix a
# model deprecation, and the anti-backlog-runaway guard this script's own
# incident motivated).
from reasoning.groq_config import (  # noqa: E402
    BACKOFF_BASE,
    GROQ_MODEL,
    MAX_ATTEMPTS_PER_RUN,
    MAX_CONSECUTIVE_FAILURES,
    MAX_RETRIES,
)

# --- Configuration ---------------------------------------------------------

# Groq's free tier is limited (~1000 requests/day). Stay under it.
DAILY_CALL_LIMIT = 1000

# Coverage now spans up to 503 haute-priority tickers (see
# ingestion/fetch_news.py), each potentially with dozens of news items --
# without a per-ticker cap, a single heavily-covered ticker could absorb a
# large share of the day's Groq budget at the expense of the other tickers.
# This does NOT add a separate quota counter: it reads today's per-ticker
# counts straight from the existing news_analysis table, alongside the
# existing global DAILY_CALL_LIMIT (llm_usage).
MAX_NEWS_PER_TICKER_PER_DAY = 5

MIN_TITLE_LEN = 20

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

def get_usage(conn, day, table="llm_usage"):
    # `table` is always one of this module's own hardcoded constants (never
    # external/user input) -- see reasoning/daily_summary.py's identical
    # convention for its own two quota tables.
    row = conn.execute(f"SELECT calls FROM {table} WHERE day = ?", (day,)).fetchone()
    return row[0] if row else 0


def bump_usage(conn, day, table="llm_usage"):
    conn.execute(
        f"INSERT INTO {table} (day, calls) VALUES (?, 1) "
        f"ON CONFLICT(day) DO UPDATE SET calls = calls + 1",
        (day,),
    )
    conn.commit()


def get_today_ticker_counts(conn, day):
    """{ticker: count} of news already analysed today, derived from the
    existing news_analysis table (no separate counter to keep in sync)."""
    rows = conn.execute(
        "SELECT r.ticker, COUNT(*) FROM news_analysis a "
        "JOIN news_raw r ON r.id = a.news_id "
        "WHERE date(a.created_at) = ? "
        "GROUP BY r.ticker",
        (day,),
    ).fetchall()
    return dict(rows)


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


def _is_daily_token_limit(exc):
    """Groq's free ``on_demand`` tier enforces a Tokens-Per-Day (TPD) cap
    (200k as of the openai/gpt-oss-120b migration, confirmed via the
    account's own 429 error message on 2026-08-25 -- was 100k under the
    now-deprecated llama-3.3-70b-versatile) that, for this prompt's
    typical size, binds much sooner than DAILY_CALL_LIMIT's request-count
    assumption -- observed in practice around 500-550 analyses/day when
    this module has the full daily budget to itself (~380 tokens/analysis,
    per a real 334-analysis/~127k-token run), well short of 1000.
    Distinguished from a transient per-minute rate limit because Groq's
    own error message quotes a multi-minute wait: retrying it with the
    usual few-second backoff cannot possibly succeed, so this signal must
    stop the whole run immediately instead of burning through the rest of
    the candidate list one exhausted retry loop at a time (which, at
    thousands of remaining items, would take hours for no benefit)."""
    text = str(exc).lower()
    return "tokens per day" in text or "(tpd)" in text


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
    """analyse_one with exponential backoff on transient (per-minute) 429
    rate limits. A daily-token-limit 429 (TPD, see _is_daily_token_limit) is
    NOT retried here -- it propagates immediately so the caller can stop the
    whole run instead of wasting a backoff that cannot possibly succeed."""
    for attempt in range(MAX_RETRIES):
        try:
            return analyse_one(client, ticker, title, summary)
        except Exception as exc:  # noqa: BLE001
            if _is_daily_token_limit(exc):
                raise
            if _is_rate_limit(exc) and attempt < MAX_RETRIES - 1:
                wait = BACKOFF_BASE * (2 ** attempt)
                logger.warning("Rate limit (429). Backoff %.0fs (try %d/%d)...",
                               wait, attempt + 1, MAX_RETRIES)
                time.sleep(wait)
                continue
            raise
    return None


# --- Read helpers (dashboard "News & Analyse IA" page + API) -----------------
#
# Relocated from dashboard/app.py: NEWS_SQL/load_news and
# _price_before_after_news were already conn-first/pure (no Streamlit,
# no pandas dependency in the latter) -- straight moves. _news_summary_
# paragraph is adapted here to take a plain dict (this module's own
# load_news() return shape) instead of a pandas Series, since neither the
# API nor this module use pandas -- dashboard/app.py's own thin wrapper
# converts back to a DataFrame for its existing pandas-based iteration.

NEWS_SQL = """
SELECT n.id AS news_id, n.ticker, n.title, n.url, n.published_at, n.source,
       a.company, a.sector, a.importance, a.tonalite, a.impact,
       a.horizon, a.confidence
FROM news_analysis a
JOIN news_raw n ON n.id = a.news_id
ORDER BY n.published_at DESC;
"""

NEWS_BY_TICKER_SQL = """
SELECT n.id AS news_id, n.ticker, n.title, n.url, n.published_at, n.source,
       a.company, a.sector, a.importance, a.tonalite, a.impact,
       a.horizon, a.confidence
FROM news_analysis a
JOIN news_raw n ON n.id = a.news_id
WHERE n.ticker = ?
ORDER BY n.published_at DESC;
"""


def load_news(conn, ticker=None):
    """Every analysed news item (news_raw JOIN news_analysis), most recent
    first -- optionally scoped to one ticker. Plain dicts, shared by the
    API and (via dashboard/app.py's own thin wrapper) the Streamlit page.

    Sort differs deliberately from the dashboard's historical ordering
    (ticker, importance DESC, published_at DESC): that page always scopes
    to one ticker first via a selectbox, so grouping by ticker was moot
    there. The API additionally serves an unscoped "all recent news"
    default view, so global recency is the natural primary sort here."""
    conn.row_factory = sqlite3.Row
    if ticker:
        rows = conn.execute(NEWS_BY_TICKER_SQL, (ticker,)).fetchall()
    else:
        rows = conn.execute(NEWS_SQL).fetchall()
    return [dict(r) for r in rows]


def price_before_after_news(conn, ticker, published_at):
    """Price just before a news's publication (last close ON OR BEFORE that
    date) and the most recent close available now (i.e. "after", however
    much time has actually passed since) -- with the % variation between
    them. Returns None if there's no close AT OR BEFORE the news date at
    all (news older than price_history's coverage), or a dict with
    variation_pct=None if there's simply no close strictly AFTER the
    "before" one yet (news too recent for anything to have changed since)
    -- the caller must show "donnee insuffisante" for that case, never
    compute a variation against the exact same price twice. Native
    currency only (no EUR conversion here -- see dashboard/currency.py /
    api/routers/stock.py's own precedent: that's a display-layer concern
    for the caller)."""
    if not published_at:
        return None
    news_date = str(published_at)[:10]

    before_row = conn.execute(
        "SELECT date, close FROM price_history WHERE ticker = ? AND close IS NOT NULL "
        "AND date <= ? ORDER BY date DESC LIMIT 1",
        (ticker, news_date),
    ).fetchone()
    if not before_row:
        return None
    date_before, price_before = before_row

    after_row = conn.execute(
        "SELECT date, close FROM price_history WHERE ticker = ? AND close IS NOT NULL "
        "ORDER BY date DESC LIMIT 1",
        (ticker,),
    ).fetchone()
    date_after, price_after = after_row if after_row else (None, None)

    if not date_after or date_after <= date_before or not price_before:
        return {"date_before": date_before, "price_before": price_before,
                "date_after": None, "price_after": None, "variation_pct": None}

    variation_pct = (price_after - price_before) / price_before * 100.0
    return {"date_before": date_before, "price_before": price_before,
            "date_after": date_after, "price_after": price_after,
            "variation_pct": variation_pct}


def news_summary_paragraph(row):
    """A short multi-sentence paragraph assembled from ALREADY-STORED
    news_analysis fields (importance, tonalite, impact, horizon) -- no new
    LLM call, purely a clearer write-up of the exact same analysis instead
    of a single terse sentence. `row` is a plain dict (or sqlite3.Row) with
    importance already an int or None -- unlike dashboard/app.py's original
    pandas-Series version, no pd.notna() needed."""
    company = row["company"] or row["ticker"]
    sector = row["sector"]
    importance = row["importance"]
    tonalite = str(row["tonalite"] or "neutre").lower()
    impact = row["impact"]
    horizon = row["horizon"]

    sentences = []
    if importance is not None and importance >= 8:
        sentences.append(
            f"Cette news est jugee tres importante ({importance}/10) pour {company}"
            + (f", dans le secteur {sector}" if sector else "") + "."
        )
    elif importance is not None:
        sentences.append(
            f"Importance evaluee a {importance}/10 pour {company}"
            + (f" ({sector})" if sector else "") + "."
        )
    tonalite_txt = {"positive": "plutot positive", "negative": "plutot negative"}.get(
        tonalite, "neutre")
    sentences.append(f"La tonalite generale de cette news est {tonalite_txt}.")
    if impact:
        sentences.append(f"Impact identifie par l'analyse : {impact}.")
    if horizon:
        sentences.append(f"Horizon concerne : {horizon}.")
    return " ".join(sentences)


# --- Enriched narrative (LLM, ON-DEMAND ONLY) -------------------------------
#
# news_summary_paragraph() above stays free (no LLM call, assembled from
# already-stored fields) because it is rendered for EVERY row of a news
# list -- up to 50/page, and the list itself can be paged through
# thousands of items, so calling Groq there would mean tens of Groq calls
# per single page load. The richer, AI-written briefing this section adds
# (what the news concretely means, its key facts, and its likely impact on
# the ticker AND its Knowledge-Graph-related companies -- price and the
# hausse/stagnation/baisse split are shown separately by the frontend's own
# PriceHeadline/DirectionProbabilityBar, never repeated in this text) is
# instead generated ON DEMAND for ONE news item at a time -- same "click a
# button, generate just this one"
# pattern as reasoning/daily_summary.py's argued text for a signal/ticker,
# with its OWN separate quota pool (NEWS_NARRATIVE_DAILY_LIMIT,
# llm_usage_news_narrative) so it can never compete with, or be crowded
# out by, the existing per-item importance/tonalite/impact analysis quota
# (DAILY_CALL_LIMIT/llm_usage above) or daily_summary.py's own two pools.
# Cached forever per news_id (not per-day: a past news item's story never
# changes) in news_narratives, so each item is ever generated at most once.

CREATE_NARRATIVES_SQL = """
CREATE TABLE IF NOT EXISTS news_narratives (
    news_id               INTEGER PRIMARY KEY,
    texte                 TEXT NOT NULL,
    direction_hausse      INTEGER,
    direction_stagnation  INTEGER,
    direction_baisse      INTEGER,
    direction_horizon     TEXT,
    direction_explication TEXT,
    model                 TEXT,
    created_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (news_id) REFERENCES news_raw (id)
);
"""


def _ensure_narratives_horizon_column(conn):
    """CREATE TABLE IF NOT EXISTS is a no-op on an already-existing table,
    so a news_narratives table created before direction_horizon existed
    (2026-08-25's first version of this feature) needs an explicit
    migration -- not just a wider CREATE statement above, which only
    matters for a brand-new database."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(news_narratives)")}
    if "direction_horizon" not in columns:
        conn.execute("ALTER TABLE news_narratives ADD COLUMN direction_horizon TEXT")
        conn.commit()

CREATE_USAGE_NARRATIVE_SQL = """
CREATE TABLE IF NOT EXISTS llm_usage_news_narrative (
    day   TEXT PRIMARY KEY,
    calls INTEGER NOT NULL DEFAULT 0
);
"""

# Re-measured for real on 2026-08-26 against the live Groq API,
# openai/gpt-oss-120b, after the structured-briefing rewrite (SYSTEM_PROMPT_
# NARRATIVE below) added the news's own raw summary_brut text as input (the
# only source of real figures/quotes) and a richer Markdown-sectioned
# output: 4 real news items, GLW/RF/JPM/UBER, spanning "no related
# companies" to JPM's 6 -- 1368 to 1836 tokens/call, ~1685 average. Cost
# roughly 1.6-1.7x the previous plain-paragraph version's measured 1056
# tokens/call (see git history for that measurement) -- the summary_brut
# input and the extra section headers/bullets are real, not free.
# Re-measured again the same day after adding the probability-calibration
# rules (the "certainty must match hausse/stagnation/baisse, not the raw
# news tonalite" fix, and the emphatic no-percentage-leak instructions it
# took two iterations to actually enforce -- the model quoted "72 %"
# verbatim the first time despite being told not to): RF, 2512 tokens/call
# (1800 prompt + 712 completion) -- the longer system prompt (now
# permanently included in every call) accounts for the increase, not a
# per-item variable.
# NEWS_NARRATIVE_DAILY_LIMIT lowered from 50 to 30 to compensate for the
# EARLIER increase: even at a worse-than-observed ~2700 tokens/call
# (matching this latest 2512 measurement), 30/day is ~81k tokens/day,
# still comfortably inside the shared 200k TPD budget on a day
# analyze_news.py's own quota is also busy, while remaining far more than
# anyone will plausibly click through by hand (this is opt-in per news
# item, a button click, not a page load) -- no further change needed.
NEWS_NARRATIVE_DAILY_LIMIT = 30

# Truncated, not the full article: summary_brut is the scraped RSS
# summary (median ~170 chars, p90 ~500, occasional outliers up to ~3000) --
# this is the ONLY place actual figures (price targets, EPS, revenue
# guidance) and analyst quotes can come from, since importance/tonalite/
# impact are themselves short LLM-derived labels with no numbers in them.
# Capped so a rare long outlier can't blow up prompt cost.
MAX_SUMMARY_BRUT_CHARS = 1500

SYSTEM_PROMPT_NARRATIVE = (
    "Tu es un analyste financier qui rédige, en français, un briefing "
    "structuré et rapide à lire sur une news -- pas un paragraphe dense, un "
    "vrai format de briefing. Lecteur non trader professionnel : reste "
    "accessible, sans jargon non expliqué. Positionnement moyen/long terme "
    "(1 semaine à 1 an) -- jamais de day trading, jamais de prix d'entrée "
    "ou de stop-loss précis.\n\n"
    "Règle absolue : reste strictement fidèle au texte source fourni "
    "ci-dessous -- n'invente aucun fait, aucun chiffre, aucune citation, "
    "aucune entreprise liée qui ne soit pas explicitement listée. Si le "
    "texte source ne contient pas de chiffre précis ou de citation "
    "d'analyste, ne dis JAMAIS le contraire : dis simplement que la source "
    "ne fournit pas ce niveau de détail, n'en invente aucun.\n\n"
    "Important : le lecteur voit déjà, séparément, la description de "
    "l'entreprise, le prix actuel et les probabilités hausse/stagnation/"
    "baisse -- NE LES RÉPÈTE JAMAIS dans ce texte, sous AUCUNE forme, meme "
    "reformulee ou approximee. Interdiction stricte d'ecrire des tournures "
    "comme \"avec une probabilite de hausse de 72 %\", \"X % de chances\", "
    "\"les indicateurs donnent Y % de probabilite\" ou tout autre chiffre "
    "de pourcentage directionnel dans le texte -- aucun chiffre de "
    "probabilité, aucun cours de bourse. Ne présente pas non plus "
    "l'entreprise en début de texte : va directement au sujet de LA "
    "NEWS.\n\n"
    "Calibrage de la certitude (règle absolue) : les 3 probabilités "
    "hausse/stagnation/baisse fournies ci-dessous SERVENT UNIQUEMENT A "
    "CHOISIR TON NIVEAU DE CERTITUDE (des mots comme \"probablement\", "
    "\"incertain\", \"pourrait\"), JAMAIS a etre ecrites ou paraphrasees "
    "en chiffre dans le texte -- jamais la tonalité brute de la news. "
    "Même une news au ton très négatif ou très positif ne justifie PAS un "
    "texte tranché si les probabilités ne le sont pas : une news alarmante "
    "avec hausse 22 % / stagnation 53 % / baisse 25 % ne doit PAS produire "
    "un texte qui annonce une baisse quasi certaine -- stagnation est ici "
    "le scénario dominant, le texte doit le refléter (incertitude, "
    "attentisme), pas la tonalité seule. Règles précises :\n"
    "- Un scénario ne peut être affirmé avec assurance QUE s'il dépasse "
    "60 % -- utilise alors un langage clair mais jamais absolu "
    "(\"probablement\", pas \"certainement\"), et SANS jamais citer le "
    "chiffre lui-meme.\n"
    "- Si aucun scénario ne dépasse 60 %, ou si les deux scénarios "
    "directionnels sont proches (écart de 10 points ou moins), exprime "
    "explicitement l'incertitude (\"la situation reste incertaine\", "
    "\"aucune tendance nette ne se dégage\", \"à surveiller plutôt qu'à "
    "trancher\") -- ne choisis JAMAIS un camp comme si c'était clair.\n"
    "- Si stagnation est le scénario dominant, dis-le explicitement (ex: "
    "\"le marché pourrait rester attentiste\", \"pas de mouvement clair "
    "attendu à court terme\") plutôt que de ne parler que de hausse ou de "
    "baisse.\n\n"
    "Format Markdown (le rendu gère gras/puces/citations -- PAS d'emoji, "
    "jamais). Longueur IMPERATIVE : 300 mots au total maximum. Structure "
    "EXACTE, dans cet ordre :\n\n"
    "1. Une phrase d'ouverture (pas de titre) qui identifie clairement de "
    "quoi parle la news et qui est concerné -- pas de description "
    "generale de l'entreprise, uniquement le sujet de cette news precise.\n\n"
    "2. \"**En resume**\" suivi d'une liste a puces (3 a 5 puces) des faits "
    "cles de la news. GARDE les chiffres precis presents dans le texte "
    "source (objectifs de prix, prevision de chiffre d'affaires, EPS, "
    "pourcentages de croissance, etc.) -- mets-les en **gras** dans la "
    "puce concernee. N'invente jamais un chiffre absent du texte source.\n\n"
    "3. UNIQUEMENT si le texte source NOMME explicitement une source "
    "identifiable (un analyste, une banque, un dirigeant nomme, une "
    "agence) ET rapporte precisement son avis ou sa declaration : resume "
    "ce message en une phrase, en citation Markdown (ligne commencant par "
    "\"> \"). Une description vague type \"avis mitiges\" ou \"opinions "
    "partagees\" SANS source nommee ne compte PAS -- ne fabrique jamais "
    "une citation ou une source a partir de ca. Si aucune source nommee "
    "n'est identifiable : n'ecris RIEN pour cette section -- pas de "
    "titre, pas de \">\", pas de phrase generique, pas de ligne vide "
    "dediee -- passe directement au point suivant comme si ce point "
    "n'existait pas.\n\n"
    "4. Si le texte source mentionne explicitement d'autres avis, "
    "objectifs ou notations differents et attribuables (ex: un autre "
    "analyste nomme, un consensus chiffre), une phrase de mise en "
    "perspective. Sinon : n'ecris RIEN pour cette section, meme regle "
    "qu'au point 3 (aucun residu, aucune ligne vide, aucun symbole de "
    "citation orphelin).\n\n"
    "5. \"**Ce que ca signifie pour l'action {NOM_TICKER}**\" suivi d'une "
    "interpretation claire et directe de l'impact potentiel, 2-3 phrases "
    "courtes maximum -- respecte STRICTEMENT la regle de calibrage de la "
    "certitude ci-dessus (les probabilites fournies, pas la tonalite). "
    "IMPORTANT : si une divergence entre le signal general "
    "du ticker et cette news specifique est signalee ci-dessous (deux "
    "chiffres deja calcules, jamais a recalculer toi-meme), explique-la "
    "explicitement ici -- ex: \"a court terme, pression negative probable ; "
    "les indicateurs techniques restent toutefois positifs sur un horizon "
    "plus long\" -- ne laisse JAMAIS les deux lectures se contredire sans "
    "explication. Si aucune divergence n'est signalee, ignore ce point.\n\n"
    "6. UNIQUEMENT si des entreprises liees sont listees ci-dessous : "
    "\"**Entreprises liees a surveiller**\" suivi d'une liste a puces, une "
    "puce par entreprise, expliquant brievement comment cette news "
    "pourrait l'affecter. Si aucune entreprise liee n'est fournie, omets "
    "entierement cette section (jamais de section vide, jamais "
    "d'entreprise inventee).\n\n"
    "Ton direct, phrases courtes, **gras** sur les points cles pour une "
    "lecture rapide. Reponds uniquement avec le texte en Markdown, rien "
    "d'autre (pas de preambule, pas de \"Voici le briefing\")."
)


def build_news_narrative_prompt(row, related_companies, direction=None, divergence_note=None):
    nom_affiche = row["company"] or row["ticker"]
    summary = (row["summary_brut"] or "").strip()
    if len(summary) > MAX_SUMMARY_BRUT_CHARS:
        summary = summary[:MAX_SUMMARY_BRUT_CHARS] + "..."

    lines = [
        f"Ticker : {row['ticker']} ({nom_affiche})",
        f"Titre de la news : {row['title']}",
        "Texte source de la news (peut etre en anglais -- synthetise en francais) :",
        summary if summary else "(aucun texte source disponible au-dela du titre)",
        f"\nImportance : {row['importance']}/10, tonalite {row['tonalite']}, "
        f"horizon {row['horizon'] or 'non precise'}",
        f"Impact identifie par l'analyse existante : {row['impact'] or 'non precise'}",
    ]
    if related_companies:
        parts = [f"{rtype}: {', '.join(names)}" for rtype, names in related_companies.items()]
        lines.append("Entreprises liees (graphe de connaissances) : " + " | ".join(parts))
    else:
        lines.append("Aucune entreprise liee trouvee dans le graphe de connaissances pour ce ticker.")

    # This is the ONLY thing section 5's degree of confidence is allowed to
    # be calibrated against -- NOT the news's raw tonalite, which is why a
    # sharply negative tonalite could previously produce a confidently
    # bearish text even when stagnation was the actual dominant bucket
    # (e.g. JPM: hausse 22 / stagnation 53 / baisse 25 -- nowhere near a
    # clear "baisse attendue"). Never to be echoed verbatim in the output
    # (see SYSTEM_PROMPT_NARRATIVE's "Important" block) -- it is
    # calibration input only, the percentages themselves are already shown
    # separately by the frontend.
    if direction:
        lines.append(
            f"\nProbabilites deja calculees (hausse/stagnation/baisse) -- "
            f"CES CHIFFRES NE DOIVENT JAMAIS APPARAITRE, MEME REFORMULES, "
            f"DANS LE TEXTE : ils servent UNIQUEMENT a choisir ton niveau de "
            f"certitude (assure / incertain / attentiste) dans la section 5 : "
            f"hausse {direction['hausse']}% / stagnation {direction['stagnation']}% / "
            f"baisse {direction['baisse']}% ({direction['horizon']})."
        )
    else:
        lines.append(
            "\nAucune probabilite directionnelle disponible pour ce ticker -- "
            "reste general dans la section 5, n'affirme aucune direction precise."
        )

    if divergence_note:
        lines.append(f"\n{divergence_note}")

    lines.append(
        f"\nRedige le briefing structure demande, en remplacant "
        f"{{NOM_TICKER}} par \"{row['ticker']}\" dans le titre de la "
        f"section 5, uniquement a partir des elements ci-dessus."
    )
    return "\n".join(lines)


def _build_direction_for_news(conn, ticker, news_tonalite=None, news_importance=None):
    """Same compute_direction_probabilities() reused by
    reasoning/daily_summary.py -- see that module's build_signal() for the
    Resume-du-jour/Analyse-d'une-action equivalent. Local import: avoids a
    module-load-time dependency from this file onto daily_summary.py's own
    (heavier) import chain, matching the lazy-import convention already
    used elsewhere in this project for cross-module reuse.

    `news_tonalite`/`news_importance` (default None, i.e. the ticker's
    GENERAL state only) let a caller fold in ONE specific news item's own
    sentiment -- see compute_direction_probabilities' own docstring for
    why this exists: without it, the percentages shown on the News page
    never reflected the very news item displayed right above them."""
    from reasoning.daily_summary import load_ticker_detail

    detail = load_ticker_detail(conn, ticker)
    if detail is None:
        return None
    causal = load_causal_effect_for_ticker(conn, ticker)
    return compute_direction_probabilities(
        score_technique=detail["technical_score"],
        score_prix_valorisation=detail["price_valuation_score"],
        score_fondamental_reel=detail["score_fondamental_reel"],
        causal_effect=causal["effet"] if causal else None,
        causal_confidence=causal["confiance"] if causal else None,
        news_tonalite=news_tonalite,
        news_importance=news_importance,
    )


def _dominant_direction(direction):
    """'hausse' | 'stagnation' | 'baisse' -- whichever of the three is
    largest (ties broken toward stagnation, the conservative read)."""
    h, s, b = direction["hausse"], direction["stagnation"], direction["baisse"]
    if h > s and h > b:
        return "hausse"
    if b > s and b > h:
        return "baisse"
    return "stagnation"


def _build_divergence_note(direction_general, direction_with_news):
    """None if there's nothing worth flagging (either direction is
    unavailable, or both agree on which way the evidence leans); otherwise
    a factual, already-computed data line for the prompt to relay -- the
    LLM never invents or judges the divergence itself, it only explains a
    real one this module already detected (see SYSTEM_PROMPT_NARRATIVE's
    own instruction to never leave two contradicting numbers unexplained)."""
    if not direction_general or not direction_with_news:
        return None
    dom_general = _dominant_direction(direction_general)
    dom_news = _dominant_direction(direction_with_news)
    if dom_general == dom_news:
        return None
    return (
        f"Divergence detectee entre le signal general de ce ticker et cette "
        f"news specifique -- explique-la explicitement dans le texte (voir "
        f"consigne du systeme a ce sujet). Hors cette news, le signal "
        f"technique/valorisation/causal general penche plutot vers "
        f"'{dom_general}' ({HORIZON_BASE}) : hausse {direction_general['hausse']}% / "
        f"stagnation {direction_general['stagnation']}% / baisse {direction_general['baisse']}%. "
        f"En tenant compte de cette news specifique, la lecture devient "
        f"'{dom_news}' ({HORIZON_NEWS}) : hausse {direction_with_news['hausse']}% / "
        f"stagnation {direction_with_news['stagnation']}% / baisse {direction_with_news['baisse']}%."
    )


def generate_news_narrative(client, conn, row):
    relations = load_relations(conn)
    graph = build_graph(relations)
    related = direct_relations(relations, row["ticker"]) if graph.has_node(row["ticker"]) else None

    direction_general = _build_direction_for_news(conn, row["ticker"])
    direction = _build_direction_for_news(
        conn, row["ticker"],
        news_tonalite=row["tonalite"], news_importance=row["importance"],
    )
    divergence_note = _build_divergence_note(direction_general, direction)

    prompt = build_news_narrative_prompt(row, related, direction, divergence_note)
    completion = client.chat.completions.create(
        model=GROQ_MODEL,
        temperature=0.4,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT_NARRATIVE},
            {"role": "user", "content": prompt},
        ],
    )
    text = completion.choices[0].message.content
    return (text.strip() if text else None), direction


def load_cached_narrative(conn, news_id):
    row = conn.execute(
        "SELECT texte, direction_hausse, direction_stagnation, direction_baisse, "
        "direction_horizon, direction_explication FROM news_narratives WHERE news_id = ?",
        (news_id,),
    ).fetchone()
    if not row:
        return None
    texte, hausse, stagnation, baisse, horizon, explication = row
    direction = (
        {
            "hausse": hausse, "stagnation": stagnation, "baisse": baisse,
            "horizon": horizon, "explication": explication,
        }
        if hausse is not None else None
    )
    return {"texte": texte, "direction_probabilities": direction}


def save_narrative(conn, news_id, texte, direction, model):
    conn.execute(
        "INSERT INTO news_narratives "
        "(news_id, texte, direction_hausse, direction_stagnation, direction_baisse, "
        "direction_horizon, direction_explication, model) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(news_id) DO UPDATE SET texte = excluded.texte, "
        "direction_hausse = excluded.direction_hausse, "
        "direction_stagnation = excluded.direction_stagnation, "
        "direction_baisse = excluded.direction_baisse, "
        "direction_horizon = excluded.direction_horizon, "
        "direction_explication = excluded.direction_explication, "
        "model = excluded.model",
        (
            news_id, texte,
            direction["hausse"] if direction else None,
            direction["stagnation"] if direction else None,
            direction["baisse"] if direction else None,
            direction["horizon"] if direction else None,
            direction["explication"] if direction else None,
            model,
        ),
    )
    conn.commit()


def get_or_generate_news_narrative(conn, news_id):
    """(found, result) -- found=False means this news_id has no
    news_analysis row at all (caller should 404). result is
    {"news_id", "texte", "direction_probabilities", "source"} with source
    one of "cache" | "generated" | "unavailable" (quota exhausted, no API
    key, or a network error -- never raises for those, same graceful-
    degradation convention as daily_summary.py's get_or_generate_argued_text)."""
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT n.id AS news_id, n.ticker, n.title, n.summary_brut, "
        "a.company, a.sector, a.importance, a.tonalite, a.impact, a.horizon "
        "FROM news_analysis a JOIN news_raw n ON n.id = a.news_id WHERE n.id = ?",
        (news_id,),
    ).fetchone()
    if row is None:
        return False, None

    try:
        conn.execute(CREATE_NARRATIVES_SQL)
        conn.execute(CREATE_USAGE_NARRATIVE_SQL)
        conn.commit()
        _ensure_narratives_horizon_column(conn)
    except sqlite3.Error as exc:
        logger.warning("Table news_narratives indisponible (%s).", exc)
        return True, {"news_id": news_id, "texte": None,
                      "direction_probabilities": None, "source": "unavailable"}

    cached = load_cached_narrative(conn, news_id)
    if cached:
        return True, {"news_id": news_id, **cached, "source": "cache"}

    # Same pytest guard as daily_summary.py's add_argued_texts: never burn
    # real Groq quota / require network access from a test run.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True, {"news_id": news_id, "texte": None,
                      "direction_probabilities": None, "source": "unavailable"}

    today = date.today().isoformat()
    used = get_usage(conn, today, table="llm_usage_news_narrative")
    if used >= NEWS_NARRATIVE_DAILY_LIMIT:
        logger.info("Quota narrative news (%d/jour) deja atteint.", NEWS_NARRATIVE_DAILY_LIMIT)
        return True, {"news_id": news_id, "texte": None,
                      "direction_probabilities": None, "source": "unavailable"}

    from dotenv import load_dotenv
    load_dotenv()
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return True, {"news_id": news_id, "texte": None,
                      "direction_probabilities": None, "source": "unavailable"}

    try:
        import httpx
        from groq import Groq
        http_client = httpx.Client(verify=CA_BUNDLE) if CA_BUNDLE else None
        client = Groq(api_key=api_key, http_client=http_client)
        text, direction = generate_news_narrative(client, conn, row)
    except Exception as exc:  # noqa: BLE001
        logger.warning("news_id=%s: generation narrative echouee (%s).", news_id, exc)
        return True, {"news_id": news_id, "texte": None,
                      "direction_probabilities": None, "source": "unavailable"}

    if not text:
        return True, {"news_id": news_id, "texte": None,
                      "direction_probabilities": None, "source": "unavailable"}

    save_narrative(conn, news_id, text, direction, GROQ_MODEL)
    bump_usage(conn, today, table="llm_usage_news_narrative")
    return True, {"news_id": news_id, "texte": text,
                  "direction_probabilities": direction, "source": "generated"}


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

    ticker_counts = get_today_ticker_counts(conn, today)

    analysed = 0
    failed = 0
    skipped_ticker_cap = 0
    attempts = 0
    consecutive_failures = 0

    def _note_failure():
        """Record one failed attempt; True once MAX_CONSECUTIVE_FAILURES
        have happened in a row -- the backstop bump_usage()'s success-only
        counter can never provide on its own, since it never increments
        when EVERY call fails for a reason unrelated to quota (a
        deprecated/renamed model returning 404 on every request, e.g., is
        exactly how this run once burned through a 51,443-item backlog
        without ever tripping the quota check -- see
        reasoning/groq_config.py)."""
        nonlocal failed, consecutive_failures
        failed += 1
        consecutive_failures += 1
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            logger.error(
                "%d echecs Groq consecutifs -- arret (modele/API probablement "
                "indisponible ou modele deprecie, voir reasoning/groq_config.py). "
                "%d news restantes pour un prochain run.",
                consecutive_failures, len(to_analyse) - attempts)
            return True
        return False

    for news_id, ticker, title, summary in to_analyse:
        if get_usage(conn, today) >= DAILY_CALL_LIMIT:
            logger.warning("Daily quota reached mid-run. Stopping.")
            break
        if ticker_counts.get(ticker, 0) >= MAX_NEWS_PER_TICKER_PER_DAY:
            skipped_ticker_cap += 1
            continue
        if attempts >= MAX_ATTEMPTS_PER_RUN:
            logger.warning(
                "Plafond de %d tentatives atteint pour ce run (succes+echecs "
                "confondus) -- arret, meme si le quota quotidien (%d/%d) "
                "n'est pas atteint. %d news restantes pour un prochain run.",
                MAX_ATTEMPTS_PER_RUN, get_usage(conn, today), DAILY_CALL_LIMIT,
                len(to_analyse) - attempts)
            break

        attempts += 1
        try:
            result = analyse_with_retry(client, ticker, title, summary)
        except Exception as exc:  # noqa: BLE001
            if _is_daily_token_limit(exc):
                logger.warning(
                    "Quota Groq quotidien (tokens/jour, TPD) atteint apres "
                    "%d analyses reussies aujourd'hui. Arret du run: %s",
                    analysed, exc)
                break
            logger.error("news_id=%s: LLM call failed (%s)", news_id, exc)
            if _note_failure():
                break
            continue

        if result is None:
            if _note_failure():
                break
            continue

        result.update({"news_id": news_id, "model": GROQ_MODEL})
        try:
            conn.execute(INSERT_ANALYSIS_SQL, result)
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            logger.error("news_id=%s: insert failed (%s)", news_id, exc)
            if _note_failure():
                break
            continue

        bump_usage(conn, today)
        consecutive_failures = 0
        ticker_counts[ticker] = ticker_counts.get(ticker, 0) + 1
        analysed += 1
        if analysed % 10 == 0:
            logger.info("Progress: %d analysed (%d calls used today).",
                        analysed, get_usage(conn, today))

    logger.info(
        "Done. Analysed %d, failed %d, skipped %d (plafond %d/ticker/jour "
        "atteint), %d tentatives au total. Calls used today: %d/%d. "
        "Tickers touches aujourd'hui: %d.",
        analysed, failed, skipped_ticker_cap, MAX_NEWS_PER_TICKER_PER_DAY,
        attempts, get_usage(conn, today), DAILY_CALL_LIMIT, len(ticker_counts),
    )
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
