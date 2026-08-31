#!/usr/bin/env python3
"""Daily "Contexte geopolitique et economique mondial" synthesis -- Phase 4
V1. Reuses TWO already-collected, free sources (no new paid feed, no
per-ticker LLM call added):

  1. ``macro_news`` (ingestion/fetch_macro_news.py) -- official Fed/ECB
     press-release RSS, raw storage only.
  2. Existing ``news_raw``/``news_analysis`` rows that happen to be
     macro/global in scope rather than company-specific -- identified here
     by a keyword filter over titles ALREADY classified by
     reasoning/analyze_news.py (no new Groq call for this step: importance/
     tonalite/impact are read straight from the existing analysis).

Both are combined into ONE Groq call/day that writes, in a single
JSON-mode completion, a SHORT briefing (same "briefing, not a dense
paragraph" spirit as reasoning/analyze_news.py's news narrative and
reasoning/daily_summary.py's argued text) AND a longer, pedagogical
DETAILED version for a reader with no economics background, plus a
structured "secteurs a surveiller" list -- three outputs, one call, so the
two-tier UX (see api/routers/macro_context.py) costs no more Groq quota
than the single-version V1 did. Cached per calendar day in
``macro_context_daily`` (like every other daily/per-day cache in this
project -- e.g. daily_summary_arguments) so re-loading the page never
re-generates the same day's synthesis twice.

Deliberately NOT per-ticker: this is a single global artifact, so its own
quota pool (MACRO_CONTEXT_DAILY_LIMIT) only needs to be large enough to
absorb a few retried attempts on a bad day, never a per-ticker budget.
"""

import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

DB_PATH = os.path.join(REPO_ROOT, "data", "marketdb.db")

from reasoning.groq_config import (  # noqa: E402
    BACKOFF_BASE,
    GROQ_MODEL,
    MAX_RETRIES,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("macro_context")


# --- Configuration -----------------------------------------------------------

# "dernieres 24-48h" per the task -- narrow enough that the briefing stays
# about what's current, not a rolling news archive.
MACRO_LOOKBACK_HOURS = 48

# Central banks don't publish daily (confirmed for real: on a live run the
# 48h window landed on a quiet Mon/weekend gap and returned zero items from
# BOTH feeds, even though each has several items/week) -- rather than show
# an empty briefing on any quiet day, gather_macro_sources() widens to this
# window ONCE if the primary 48h window is empty. Still bounded (not an
# unbounded archive search) and the actual window used is always surfaced
# to the prompt (see build_macro_context_prompt) and the API response, so
# the text never implies same-day freshness it doesn't have.
MACRO_LOOKBACK_FALLBACK_HOURS = 24 * 7

# Only ONE artifact is ever produced per day (unlike analyze_news.py's
# per-news-item or daily_summary.py's per-ticker quotas) -- this cap exists
# purely to stop a broken day (e.g. Groq erroring on every attempt) from
# being retried indefinitely across repeated page loads, not to budget
# multiple distinct generations.
MACRO_CONTEXT_DAILY_LIMIT = 5

USAGE_TABLE_MACRO_CONTEXT = "llm_usage_macro_context"

# Cap on how many source items (each direction) feed the prompt -- keeps
# the call's token cost bounded even on a very newsy day, and keeps the
# "sources citees" list the API returns to a sane size for the frontend.
MAX_MACRO_NEWS_ITEMS = 20
MAX_COMPANY_NEWS_ITEMS = 15
MAX_CONTENT_CHARS = 400


# `texte` kept as the column name for the SHORT version (not renamed to
# texte_court) so a fresh CREATE TABLE and the migration below agree on one
# name -- texte_detaille/secteurs_json are the two fields ADDED for the
# two-tier synthesis (see _ensure_two_tier_columns for existing databases
# created before this feature, e.g. this project's own dev DB, which
# already had one cached row under the old one-version schema).
CREATE_CONTEXT_SQL = """
CREATE TABLE IF NOT EXISTS macro_context_daily (
    day             TEXT PRIMARY KEY,
    texte           TEXT NOT NULL,
    texte_detaille  TEXT,
    secteurs_json   TEXT,
    n_sources       INTEGER NOT NULL,
    model           TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

CREATE_USAGE_SQL = f"""
CREATE TABLE IF NOT EXISTS {USAGE_TABLE_MACRO_CONTEXT} (
    day   TEXT PRIMARY KEY,
    calls INTEGER NOT NULL DEFAULT 0
);
"""


def _ensure_two_tier_columns(conn):
    """CREATE TABLE IF NOT EXISTS is a no-op on an already-existing table,
    so a macro_context_daily table created before the two-tier synthesis
    existed needs an explicit migration -- same pattern as
    reasoning/analyze_news.py's _ensure_narratives_horizon_column."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(macro_context_daily)")}
    if "texte_detaille" not in columns:
        conn.execute("ALTER TABLE macro_context_daily ADD COLUMN texte_detaille TEXT")
    if "secteurs_json" not in columns:
        conn.execute("ALTER TABLE macro_context_daily ADD COLUMN secteurs_json TEXT")
    conn.commit()


# --- Step 2: macro-scoped news already in news_raw/news_analysis -----------
#
# No fixed "Indices" sector exists in practice (a live check of
# news_analysis.sector found only per-company sectors like "Technologie"/
# "Energie" -- the LLM prompt in analyze_news.py never asks for a macro
# category), so the filter is keyword-based over the title instead, same
# "cheap, no new LLM call" spirit the task asked for. Bilingual (most
# titles are English from Yahoo/Finnhub, but the pipeline's own sector
# labels are sometimes French) and deliberately broad -- false positives
# here just mean one extra line of real context in the prompt, not a
# fabricated fact, so erring wide is safe.
MACRO_KEYWORD_PATTERN = re.compile(
    r"\b("
    r"fed|federal reserve|fomc|powell|ecb|banque centrale|central bank|"
    r"bce|lagarde|interest rate|taux d.int[ée]r[êe]t|taux directeur|"
    r"rate (?:cut|hike|decision)|inflation|cpi|consumer price|pib|gdp|"
    r"recession|r[ée]cession|unemployment|ch[ôo]mage|jobs report|"
    r"payrolls|tariffs?|droits? de douane|trade war|guerre commerciale|"
    r"opec|opep|oil price|prix du p[ée]trole|geopolit\w*|g[ée]opolit\w*|"
    r"sanctions?|war|guerre|shutdown|debt ceiling|plafond de la dette|"
    r"imf|fonds mon[ée]taire|world bank|banque mondiale|"
    r"stock market|wall street|march[ée]s? mondiaux|global markets|"
    r"treasury yield|bond market|dollar index|currency war"
    r")\b",
    re.IGNORECASE,
)

MACRO_NEWS_IMPORTANCE_MIN = 6


def load_macro_relevant_company_news(conn, since_iso):
    """Company news (news_raw JOIN news_analysis) published since
    `since_iso` whose TITLE matches MACRO_KEYWORD_PATTERN -- i.e. news that,
    despite being filed under one ticker, is really about the macro/global
    picture (a Fed rate decision covered by a stock-news wire, an OPEC
    supply story, etc.). Filters purely in Python over an already-small
    window (a few days of news_raw, not the whole 57k-row table) -- no
    SQLite REGEXP extension needed, and no new Groq call: importance/
    tonalite/impact are read straight from the existing news_analysis row.

    Only importance >= MACRO_NEWS_IMPORTANCE_MIN is kept, same bar
    reasoning/daily_summary.py's own load_macro_context uses to decide a
    news item is worth surfacing as macro context at all."""
    rows = conn.execute(
        "SELECT r.id, r.ticker, r.title, r.url, r.published_at, r.source, "
        "a.sector, a.importance, a.tonalite, a.impact "
        "FROM news_analysis a JOIN news_raw r ON r.id = a.news_id "
        "WHERE r.published_at >= ? AND a.importance >= ? "
        "ORDER BY r.published_at DESC",
        (since_iso, MACRO_NEWS_IMPORTANCE_MIN),
    ).fetchall()

    items = []
    for news_id, ticker, title, url, published_at, source, sector, importance, tonalite, impact in rows:
        if not title or not MACRO_KEYWORD_PATTERN.search(title):
            continue
        items.append({
            "kind": "company_news",
            "news_id": news_id,
            "ticker": ticker,
            "title": title,
            "url": url,
            "published_at": published_at,
            "source": source,
            "sector": sector,
            "importance": importance,
            "tonalite": tonalite,
            "impact": impact,
        })
        if len(items) >= MAX_COMPANY_NEWS_ITEMS:
            break
    return items


# --- Step 1 data: macro_news (central bank feeds) ---------------------------

def load_recent_macro_news(conn, since_iso):
    """Rows from ``macro_news`` (ingestion/fetch_macro_news.py) published
    since `since_iso`, most recent first, capped at MAX_MACRO_NEWS_ITEMS.
    Returns [] gracefully if the table doesn't exist yet (fetch script
    never run) instead of raising -- same degrade-gracefully convention as
    the rest of this module."""
    try:
        rows = conn.execute(
            "SELECT source, title, url, published_at, content_raw "
            "FROM macro_news WHERE published_at >= ? "
            "ORDER BY published_at DESC LIMIT ?",
            (since_iso, MAX_MACRO_NEWS_ITEMS),
        ).fetchall()
    except sqlite3.Error:
        return []

    return [
        {
            "kind": "macro_news",
            "source": source,
            "title": title,
            "url": url,
            "published_at": published_at,
            "content_raw": content_raw,
        }
        for source, title, url, published_at, content_raw in rows
    ]


def gather_macro_sources(conn, lookback_hours=MACRO_LOOKBACK_HOURS):
    """(macro_items, company_items, window_hours) for the last
    `lookback_hours` -- the two source lists SYSTEM_PROMPT_MACRO is built
    from, plus the window ACTUALLY used (see MACRO_LOOKBACK_FALLBACK_HOURS:
    widened once if the primary window is empty). Pure reads, no Groq call
    -- safe to call on every request (e.g. to build the API's "sources
    citees" list even on a cache hit)."""
    since_iso = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).isoformat()
    macro_items = load_recent_macro_news(conn, since_iso)
    company_items = load_macro_relevant_company_news(conn, since_iso)

    if not macro_items and not company_items and lookback_hours < MACRO_LOOKBACK_FALLBACK_HOURS:
        since_iso = (datetime.now(timezone.utc) - timedelta(hours=MACRO_LOOKBACK_FALLBACK_HOURS)).isoformat()
        macro_items = load_recent_macro_news(conn, since_iso)
        company_items = load_macro_relevant_company_news(conn, since_iso)
        if macro_items or company_items:
            return macro_items, company_items, MACRO_LOOKBACK_FALLBACK_HOURS

    return macro_items, company_items, lookback_hours


def sources_for_display(macro_items, company_items):
    """Trimmed, frontend-facing "sources citees" list -- title/url/source/
    date only, most recent first, both lists merged. Kept separate from the
    richer dicts gather_macro_sources returns (those carry extra fields the
    prompt needs, e.g. content_raw/impact, that the API has no reason to
    expose)."""
    display = []
    for it in macro_items:
        display.append({
            "source": it["source"],
            "title": it["title"],
            "url": it["url"],
            "published_at": it["published_at"],
        })
    for it in company_items:
        display.append({
            "source": it["ticker"],
            "title": it["title"],
            "url": it["url"],
            "published_at": it["published_at"],
        })
    display.sort(key=lambda d: d["published_at"] or "", reverse=True)
    return display


# --- Step 3: LLM synthesis ---------------------------------------------------
#
# Measured for real on 2026-08-31 against the live Groq API,
# openai/gpt-oss-120b, with a realistic 7-source prompt (5 macro_news +
# 2 macro-relevant company news, the MACRO_LOOKBACK_FALLBACK_HOURS case):
# 1571 prompt + 1951 completion = 3522 tokens/call, ~4.7s. At
# MACRO_CONTEXT_DAILY_LIMIT (5 attempts/day, only ever exercised on a bad
# day -- the normal case is exactly ONE call/day thanks to the per-day
# cache), that is at most ~17.6k tokens/day for this feature, a small
# fraction of the shared 200k TPD budget documented in
# reasoning/analyze_news.py's _is_daily_token_limit. Confirms the two-tier
# JSON completion (short + detailed + sectors, one call) stays comfortably
# sustainable at 1 generation/day -- the whole point of NOT splitting this
# into two separate Groq calls.
#
# ONE Groq call produces THREE things at once (JSON mode, like
# analyze_news.py's structured verdict) instead of two separate calls for a
# short + a detailed version -- half the daily quota cost for the same
# information, since both versions are grounded in the exact same source
# list anyway. secteurs_a_surveiller is structured (not prose) specifically
# so the frontend can render it as a compact badge list attached to the
# detailed view, without asking the model to also spell it out in
# texte_detaille's own prose (that would just repeat the same names/reasons
# twice for no benefit, at real extra token cost).

SYSTEM_PROMPT_MACRO = (
    "Tu es un analyste macroeconomique qui redige, en francais, un "
    "briefing quotidien du contexte geopolitique et economique mondial, en "
    "DEUX versions distinctes plus une liste de secteurs impactes -- "
    "reponds UNIQUEMENT avec un objet JSON valide, sans texte autour, avec "
    "exactement ces cles :\n"
    "{\n"
    '  "synthese_courte": string,       // version concise, voir regles ci-dessous\n'
    '  "synthese_detaillee": string,    // version pedagogique, voir regles ci-dessous\n'
    '  "secteurs_a_surveiller": [       // 0 a 5 elements, voir regles ci-dessous\n'
    '    {"secteur": string, "raison": string}\n'
    "  ]\n"
    "}\n\n"
    "Ce texte (les deux versions) donne le CONTEXTE GENERAL avant les "
    "signaux individuels par action affiches ailleurs dans l'application -- "
    "ne parle jamais d'une action ou d'un ticker precis, sauf si une source "
    "ci-dessous le fait elle-meme explicitement pour illustrer un theme "
    "macro plus large.\n\n"
    "Regle absolue, valable pour LES DEUX versions et la liste de secteurs "
    ": reste strictement fidele aux sources fournies ci-dessous (communiques "
    "officiels de banques centrales + news deja classifiees comme macro/"
    "mondiales par le pipeline) -- n'invente aucun fait, aucun chiffre, "
    "aucune declaration, aucun evenement, aucun mecanisme economique qui ne "
    "soit pas explicitement fonde sur ce qui est fourni. Si les sources "
    "sont limitees ou ne couvrent qu'un seul theme, dis-le simplement "
    "plutot que de combler les lacunes ou d'extrapoler.\n\n"
    "=== synthese_courte ===\n"
    "Format Markdown (le rendu gere gras/puces -- pas d'emoji, jamais). "
    "Longueur IMPERATIVE : 250 mots au total maximum. Structure EXACTE, "
    "dans cet ordre :\n"
    "1. Une phrase d'ouverture qui donne le ton general du jour (accalmie, "
    "tensions, incertitude, etc.), deduite uniquement des sources "
    "fournies.\n"
    "2. \"**Grands themes du jour**\" suivi d'une liste a puces (2 a 5 "
    "puces) des faits marquants, un par puce, en precisant entre "
    "parentheses la source (ex: \"(Fed)\", \"(BCE)\", ou le ticker si "
    "c'est une news classee par entreprise). Garde les chiffres precis "
    "presents dans les sources -- mets-les en **gras**.\n"
    "3. UNIQUEMENT si plusieurs sources convergent vers un meme fil "
    "conducteur : \"**Tendance notable**\" suivi d'une a deux phrases "
    "reliant ces sources. Sinon, n'ecris RIEN pour cette section -- pas de "
    "titre, pas de phrase generique.\n"
    "4. Une phrase de rappel honnete : ceci est une synthese qualitative "
    "basee sur les sources disponibles ce jour-la, pas une prediction ni "
    "une couverture exhaustive de l'actualite mondiale.\n\n"
    "=== synthese_detaillee ===\n"
    "Version PEDAGOGIQUE pour un lecteur SANS AUCUN bagage economique -- "
    "langage tres simple, phrases courtes, explique les MECANISMES (\"pourquoi "
    "ca compte\", \"comment ca marche concretement\"), jamais juste une liste "
    "de faits comme la version courte. Definis tout terme technique la "
    "premiere fois qu'il apparait (ex: \"le taux directeur, c'est le prix "
    "auquel les banques empruntent de l'argent a la banque centrale\"). "
    "Format Markdown. Longueur IMPERATIVE : 550 mots au total maximum. "
    "Structure EXACTE, dans cet ordre :\n"
    "1. Une ou deux phrases d'introduction, en langage courant, qui posent "
    "le contexte du jour.\n"
    "2. \"**Ce qui se passe**\" : pour chaque theme des sources fournies, "
    "2 a 3 phrases qui expliquent le fait ET le mecanisme derriere (pas "
    "juste l'annoncer -- explique pourquoi cela arrive et ce que ca "
    "signifie concretement). Garde les chiffres precis en **gras**.\n"
    "3. \"**Pourquoi ca compte**\" : 2 a 4 phrases qui relient ces "
    "evenements a des consequences concretes et comprehensibles (ex: \"quand "
    "une banque centrale monte ses taux, emprunter coute plus cher, ce qui "
    "peut freiner les investissements des entreprises\") -- uniquement des "
    "mecanismes reels et generalement admis, jamais une prediction "
    "specifique sur un marche ou un titre.\n"
    "4. Une phrase de rappel honnete (meme esprit que la version courte, "
    "reformulee simplement).\n"
    "Ne repete PAS la liste des secteurs a surveiller dans ce texte -- elle "
    "est fournie separement dans secteurs_a_surveiller, ne la duplique "
    "jamais en prose ici.\n\n"
    "=== secteurs_a_surveiller ===\n"
    "0 a 5 secteurs industriels reellement impliques ou impactes par les "
    "sources fournies ci-dessous (ex: Technologie, Energie, Financier, "
    "Defense, Sante, Matieres premieres, Immobilier, Consommation, "
    "Automobile, Industrie -- ou tout autre secteur pertinent si les "
    "sources le justifient). Pour chaque secteur, `raison` est UNE phrase "
    "courte et factuelle expliquant pourquoi ce secteur est concerne, "
    "fondee explicitement sur une source fournie -- jamais un secteur "
    "ajoute par simple plausibilite generale. Si aucun secteur ne ressort "
    "clairement des sources, renvoie une liste vide -- n'en invente "
    "aucun."
)


def _clip(text, max_chars=MAX_CONTENT_CHARS):
    text = (text or "").strip()
    if len(text) > max_chars:
        return text[:max_chars] + "..."
    return text


def build_macro_context_prompt(macro_items, company_items, window_hours=MACRO_LOOKBACK_HOURS):
    lines = []

    if window_hours > MACRO_LOOKBACK_HOURS:
        lines.append(
            f"Note : aucune source fraiche sur les dernieres {MACRO_LOOKBACK_HOURS}h, "
            f"les sources ci-dessous couvrent donc une fenetre plus large "
            f"(derniers {window_hours // 24} jours). Ne pretends pas que ces "
            f"informations datent du jour meme -- reste fidele aux dates "
            f"indiquees pour chaque source.\n"
        )

    if macro_items:
        lines.append("Communiques officiels de banques centrales (les dernieres 48h) :")
        for it in macro_items:
            lines.append(f"- [{it['source'].upper()}] {it['title']} ({(it['published_at'] or '')[:10]})")
            snippet = _clip(it.get("content_raw"))
            if snippet:
                lines.append(f"  Resume : {snippet}")
    else:
        lines.append(
            "Aucun communique de banque centrale collecte sur les dernieres "
            "48h -- n'invente aucune declaration de la Fed ou de la BCE."
        )

    lines.append("")
    if company_items:
        lines.append("News a portee macro/mondiale deja analysees par le pipeline :")
        for it in company_items:
            lines.append(
                f"- [{it['ticker']}] {it['title']} ({(it['published_at'] or '')[:10]}, "
                f"tonalite {it['tonalite']}, importance {it['importance']}/10) -- "
                f"impact identifie : {it['impact'] or 'non precise'}"
            )
    else:
        lines.append(
            "Aucune news macro/mondiale supplementaire identifiee dans les "
            "news deja analysees sur cette periode."
        )

    lines.append(
        "\nRedige le briefing structure demande, uniquement a partir des "
        "elements ci-dessus."
    )
    return "\n".join(lines)


def _is_rate_limit(exc):
    status = getattr(exc, "status_code", None)
    return status == 429 or "429" in str(exc) or "rate limit" in str(exc).lower()


MAX_SECTEURS = 5


def _parse_macro_completion(content):
    """Validate/coerce the JSON completion into
    {"texte_court", "texte_detaille", "secteurs"} -- defensive the same way
    analyze_news.py's analyse_one() coerces its own JSON fields, since a
    malformed or partial completion must degrade to "unavailable" (handled
    by the caller), never crash the whole request. Returns None if either
    text field is missing/empty -- a synthesis with no text in either tier
    is not usable, regardless of what secteurs_a_surveiller contains."""
    data = json.loads(content)

    texte_court = str(data.get("synthese_courte") or "").strip()
    texte_detaille = str(data.get("synthese_detaillee") or "").strip()
    if not texte_court or not texte_detaille:
        return None

    secteurs = []
    for item in (data.get("secteurs_a_surveiller") or [])[:MAX_SECTEURS]:
        if not isinstance(item, dict):
            continue
        secteur = str(item.get("secteur") or "").strip()
        raison = str(item.get("raison") or "").strip()
        if secteur and raison:
            secteurs.append({"secteur": secteur, "raison": raison})

    return {"texte_court": texte_court, "texte_detaille": texte_detaille, "secteurs": secteurs}


def generate_macro_context(client, macro_items, company_items, window_hours=MACRO_LOOKBACK_HOURS):
    prompt = build_macro_context_prompt(macro_items, company_items, window_hours)
    completion = client.chat.completions.create(
        model=GROQ_MODEL,
        temperature=0.4,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT_MACRO},
            {"role": "user", "content": prompt},
        ],
    )
    content = completion.choices[0].message.content
    if not content:
        return None
    return _parse_macro_completion(content)


def generate_with_retry(client, macro_items, company_items, window_hours=MACRO_LOOKBACK_HOURS):
    """generate_macro_context with exponential backoff on 429 rate limits --
    same pattern as every other Groq-calling module in this project."""
    for attempt in range(MAX_RETRIES):
        try:
            return generate_macro_context(client, macro_items, company_items, window_hours)
        except Exception as exc:  # noqa: BLE001
            if _is_rate_limit(exc) and attempt < MAX_RETRIES - 1:
                wait = BACKOFF_BASE * (2 ** attempt)
                logger.warning("Rate limit (429). Backoff %.0fs (try %d/%d)...",
                               wait, attempt + 1, MAX_RETRIES)
                time.sleep(wait)
                continue
            raise
    return None


# --- Cache + quota ------------------------------------------------------------

def get_usage(conn, day):
    row = conn.execute(
        f"SELECT calls FROM {USAGE_TABLE_MACRO_CONTEXT} WHERE day = ?", (day,)
    ).fetchone()
    return row[0] if row else 0


def bump_usage(conn, day):
    conn.execute(
        f"INSERT INTO {USAGE_TABLE_MACRO_CONTEXT} (day, calls) VALUES (?, 1) "
        f"ON CONFLICT(day) DO UPDATE SET calls = calls + 1",
        (day,),
    )
    conn.commit()


def load_cached_macro_context(conn, day):
    """None both when there's no row at all AND when the row predates the
    two-tier synthesis (texte_detaille NULL, e.g. this project's own dev DB
    had one such row from before this feature) -- an incomplete legacy
    cache is treated as a cache MISS so it gets regenerated in the new
    format, rather than serving a response with no detailed version."""
    row = conn.execute(
        "SELECT texte, texte_detaille, secteurs_json, n_sources "
        "FROM macro_context_daily WHERE day = ?", (day,)
    ).fetchone()
    if not row:
        return None
    texte_court, texte_detaille, secteurs_json, n_sources = row
    if not texte_detaille:
        return None
    try:
        secteurs = json.loads(secteurs_json) if secteurs_json else []
    except (TypeError, ValueError):
        secteurs = []
    return {"texte_court": texte_court, "texte_detaille": texte_detaille,
            "secteurs": secteurs, "n_sources": n_sources}


def save_macro_context(conn, day, texte_court, texte_detaille, secteurs, n_sources, model):
    conn.execute(
        "INSERT INTO macro_context_daily "
        "(day, texte, texte_detaille, secteurs_json, n_sources, model) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(day) DO UPDATE SET texte = excluded.texte, "
        "texte_detaille = excluded.texte_detaille, "
        "secteurs_json = excluded.secteurs_json, "
        "n_sources = excluded.n_sources, model = excluded.model",
        (day, texte_court, texte_detaille, json.dumps(secteurs, ensure_ascii=False), n_sources, model),
    )
    conn.commit()


def get_or_generate_macro_context(conn):
    """{"date", "texte_court", "texte_detaille", "secteurs_a_surveiller",
    "source", "n_sources", "window_hours", "sources"} -- source one of
    "cache" | "generated" | "unavailable" (no sources collected yet, no API
    key, quota exhausted, or a network error -- never raises for those,
    same graceful-degradation convention as get_or_generate_argued_text /
    get_or_generate_news_narrative). `sources` (the "sources citees" list)
    is ALWAYS freshly computed from macro_news/news_analysis regardless of
    cache status -- it's a cheap read, and it must stay accurate even for a
    cached text generated earlier the same day."""
    macro_items, company_items, window_hours = gather_macro_sources(conn)
    sources = sources_for_display(macro_items, company_items)
    today = date.today().isoformat()
    n_gathered = len(macro_items) + len(company_items)

    def _result(texte_court, texte_detaille, secteurs, source, n_sources=n_gathered):
        return {"date": today, "texte_court": texte_court, "texte_detaille": texte_detaille,
                "secteurs_a_surveiller": secteurs, "source": source,
                "n_sources": n_sources, "window_hours": window_hours, "sources": sources}

    try:
        conn.execute(CREATE_CONTEXT_SQL)
        conn.execute(CREATE_USAGE_SQL)
        conn.commit()
        _ensure_two_tier_columns(conn)
    except sqlite3.Error as exc:
        logger.warning("Tables macro_context_daily/%s indisponibles (%s).",
                        USAGE_TABLE_MACRO_CONTEXT, exc)
        return _result(None, None, [], "unavailable")

    cached = load_cached_macro_context(conn, today)
    if cached:
        return _result(cached["texte_court"], cached["texte_detaille"], cached["secteurs"],
                        "cache", n_sources=cached["n_sources"])

    # Same pytest guard as daily_summary.py/analyze_news.py: never burn real
    # Groq quota or require network access from a test run.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return _result(None, None, [], "unavailable")

    if not macro_items and not company_items:
        logger.info("Aucune source macro collectee sur les dernieres %dh (ni "
                    "meme sur le fallback %dh) -- rien a synthetiser.",
                    MACRO_LOOKBACK_HOURS, MACRO_LOOKBACK_FALLBACK_HOURS)
        return _result(None, None, [], "unavailable", n_sources=0)

    if get_usage(conn, today) >= MACRO_CONTEXT_DAILY_LIMIT:
        logger.info("Quota macro-context (%d/jour) deja atteint.", MACRO_CONTEXT_DAILY_LIMIT)
        return _result(None, None, [], "unavailable")

    from dotenv import load_dotenv
    load_dotenv()
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return _result(None, None, [], "unavailable")

    from ingestion.ssl_utils import configure_ca_bundle
    ca_bundle = configure_ca_bundle(os.path.dirname(DB_PATH))

    try:
        import httpx
        from groq import Groq
        http_client = httpx.Client(verify=ca_bundle) if ca_bundle else None
        client = Groq(api_key=api_key, http_client=http_client)
        result = generate_with_retry(client, macro_items, company_items, window_hours)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Generation macro-context echouee (%s).", exc)
        bump_usage(conn, today)
        return _result(None, None, [], "unavailable")

    bump_usage(conn, today)
    if not result:
        return _result(None, None, [], "unavailable")

    save_macro_context(conn, today, result["texte_court"], result["texte_detaille"],
                        result["secteurs"], n_gathered, GROQ_MODEL)
    return _result(result["texte_court"], result["texte_detaille"], result["secteurs"], "generated")


# --- CLI ---------------------------------------------------------------------

def main(argv=None):
    # Groq's output is French prose with real accents/typographic punctuation
    # (e.g. U+202F narrow no-break space) -- Windows' default console
    # codepage (cp1252) can't encode all of it and would crash a bare
    # print(). errors="replace" degrades to a substitute character instead
    # of crashing; the API/DB path (get_or_generate_macro_context) never
    # goes through stdout at all, so this only affects this CLI's own
    # terminal output.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    if not os.path.exists(DB_PATH):
        logger.error("Database not found at %s.", DB_PATH)
        return 1

    conn = sqlite3.connect(DB_PATH)
    result = get_or_generate_macro_context(conn)
    conn.close()

    print("\n" + "=" * 78)
    print(f"CONTEXTE MONDIAL - {result['date']} (source={result['source']}, "
          f"{result['n_sources']} source(s), fenetre={result['window_hours']}h)")
    print("=" * 78)
    if result["texte_court"]:
        print("--- VERSION COURTE ---\n")
        print(result["texte_court"])
        print("\n--- VERSION DETAILLEE (pedagogique) ---\n")
        print(result["texte_detaille"])
        if result["secteurs_a_surveiller"]:
            print("\n--- SECTEURS A SURVEILLER ---\n")
            for s in result["secteurs_a_surveiller"]:
                print(f"  - {s['secteur']} : {s['raison']}")
    else:
        print("Aucune synthese disponible (voir logs pour la raison : pas de "
              "source collectee, quota atteint, cle API manquante, ou erreur).")
    print("\n" + "=" * 78)
    if result["sources"]:
        print(f"\n{len(result['sources'])} source(s) citee(s) :")
        for s in result["sources"][:10]:
            print(f"  - [{s['source']}] {s['title']} ({(s['published_at'] or '')[:10]})")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
