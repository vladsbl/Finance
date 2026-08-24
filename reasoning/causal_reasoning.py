#!/usr/bin/env python3
"""Module 7 -- causal reasoning: indirect consequence chains for major news.

When a news item analysed by reasoning/analyze_news.py turns out to be
IMPORTANT (see IMPORTANCE_THRESHOLD below), this module asks Groq to build a
short causal CHAIN of plausible indirect consequences, grounded strictly in
the Knowledge Graph's real relations for the affected ticker (competitors,
suppliers, clients, partners -- graph/build_graph.py, networkx). Example of
the kind of reasoning this produces (from the project's original plan):
"Chine limite le gallium -> semi-conducteurs touches -> fabricants europeens
plus exposes -> concurrents americains avantages -> mineurs beneficiaires."

Why importance >= 8, not a lower bar
-------------------------------------
news_analysis.importance is a 1-10 scale from analyze_news.py's own LLM call.
On the real data (690 analysed news at time of writing) the distribution is
heavily bimodal -- clustered at 6 (265 rows) and 8 (382 rows), essentially
never above 8 -- so "importance >= 8" is the natural "top tier" cut given how
the upstream scale is actually used in practice, not an arbitrarily chosen
number. Applied to ALL analysed tickers this is still too broad (382 rows
total, up to ~150/day on a busy day) -- but building a real causal chain
requires the ticker to actually be a SOURCE node in the Knowledge Graph
(direct_relations only has outbound edges for the 10 pilot tickers today,
see graph/build_graph.py and graph/sync_related_companies.py's docstring).
Restricting to tickers with real outbound relations narrows this to ~6-10
eligible news/day on a normal day (measured on 2026-07-22), which comfortably
fits CAUSAL_REASONING_DAILY_LIMIT below. reasoning/prioritize_news.py's
scoring is NOT reused here: it ranks candidates BEFORE analysis (to choose
what analyze_news.py spends its own quota on); this module runs AFTER
analysis and uses the importance analyze_news.py already computed.

Anti-hallucination design
--------------------------
The prompt hands the LLM the EXACT list of the source ticker's direct
relations (by type: concurrent/fournisseur/client/partenaire/...) and
instructs it to propagate the chain ONLY through those entities -- never
invent a company or relation that is not in the list. As a second, harder
guarantee (never trust an LLM's promise alone), every entity name/ticker in
the parsed response is cross-checked against the same known-entity set after
the call; anything that doesn't match is dropped and logged, never silently
kept (same "never guess" discipline as universe/fix_ticker_mapping.py).

Quota: a dedicated pool (llm_usage_causal_reasoning), separate from
analyze_news.py's llm_usage and daily_summary.py's llm_usage_summary /
llm_usage_ticker_analysis -- CAUSAL_REASONING_DAILY_LIMIT=5/day, set low
because this prompt (news + full relation list + multi-step chain output) is
markedly more token-hungry per call than a single argued paragraph. Results
are cached in `causal_chains` keyed by news_id (UNIQUE): a news item is never
re-processed once it has a chain, exactly like news_analysis never
re-analyses the same news_raw row twice.

Usage:
    python reasoning/causal_reasoning.py --dry-run           # list candidates only
    python reasoning/causal_reasoning.py                     # process up to quota
    python reasoning/causal_reasoning.py --limit 3           # cap this run

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

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

DB_PATH = os.path.join(REPO_ROOT, "data", "marketdb.db")
DATA_DIR = os.path.dirname(DB_PATH)

from ingestion.ssl_utils import configure_ca_bundle  # noqa: E402

CA_BUNDLE = configure_ca_bundle(DATA_DIR)

from graph.build_graph import build_graph, load_relations  # noqa: E402
# Reused as-is: same "direct relations, or None if the ticker isn't in the
# graph" helper daily_summary.py already relies on -- one shared definition.
from reasoning.daily_summary import companies_to_watch  # noqa: E402
# Reused as-is: the generic (table-parameterised) daily-usage-counter trio
# already introduced for the Resume du jour / Analyse d'une action quota
# split -- a third pool is just a third table name, no new logic needed.
from reasoning.daily_summary import (  # noqa: E402
    _create_usage_table_sql, bump_usage, get_usage,
)
# GROQ_MODEL/MAX_RETRIES/BACKOFF_BASE/MAX_ATTEMPTS_PER_RUN/
# MAX_CONSECUTIVE_FAILURES: shared across every Groq-calling module -- see
# reasoning/groq_config.py's own docstring.
from reasoning.groq_config import (  # noqa: E402
    BACKOFF_BASE,
    GROQ_MODEL,
    MAX_ATTEMPTS_PER_RUN,
    MAX_CONSECUTIVE_FAILURES,
    MAX_RETRIES,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("causal_reasoning")

# --- Configuration -----------------------------------------------------------

IMPORTANCE_THRESHOLD = 8   # see module docstring for calibration
CAUSAL_REASONING_DAILY_LIMIT = 5
USAGE_TABLE_CAUSAL = "llm_usage_causal_reasoning"

MAX_CHAIN_STEPS = 4  # source + up to 3 downstream/upstream entities

CREATE_CAUSAL_CHAINS_SQL = """
CREATE TABLE IF NOT EXISTS causal_chains (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    news_id               INTEGER NOT NULL UNIQUE,
    ticker_source         TEXT NOT NULL,
    chaine_raisonnement   TEXT NOT NULL,
    entreprises_impactees TEXT,
    confiance             REAL,
    model                 TEXT,
    created_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (news_id) REFERENCES news_raw (id)
);
"""

INSERT_CAUSAL_CHAIN_SQL = """
INSERT OR IGNORE INTO causal_chains
    (news_id, ticker_source, chaine_raisonnement, entreprises_impactees,
     confiance, model)
VALUES
    (:news_id, :ticker_source, :chaine_raisonnement, :entreprises_impactees,
     :confiance, :model);
"""

# Already-analysed news at/above the importance bar, not yet processed by this
# module (LEFT JOIN causal_chains ... IS NULL -- strict cache check), and
# whose ticker actually has at least one outbound relation in the Knowledge
# Graph (source_ticker in `relations` -- see module docstring: without this,
# there is nothing real to reason through).
CANDIDATES_SQL = """
SELECT a.news_id, r.ticker, r.title, r.summary_brut,
       a.company, a.sector, a.importance, a.tonalite, a.impact,
       a.horizon, a.confidence
FROM news_analysis a
JOIN news_raw r ON r.id = a.news_id
LEFT JOIN causal_chains c ON c.news_id = a.news_id
WHERE a.importance >= ?
  AND c.news_id IS NULL
  AND r.ticker IN (SELECT DISTINCT source_ticker FROM relations)
ORDER BY a.importance DESC, a.confidence DESC, a.created_at DESC;
"""


def load_eligible_news(conn, threshold=IMPORTANCE_THRESHOLD):
    conn.row_factory = sqlite3.Row
    return conn.execute(CANDIDATES_SQL, (threshold,)).fetchall()


# --- Anti-hallucination cross-check ------------------------------------------

_NAME_TICKER_RE = re.compile(r"^(.*?)(?:\s*\(([^)]+)\))?$")


def _known_entities(direct_rels, source_ticker, source_company):
    """{lowercased company names}, {uppercased tickers} that genuinely exist
    for this news: the source itself plus every entity in its Knowledge Graph
    direct relations (as returned by companies_to_watch/direct_relations,
    display strings like "Name" or "Name (TICKER)")."""
    names = set()
    tickers = {source_ticker.strip().upper()} if source_ticker else set()
    if source_company:
        names.add(source_company.strip().lower())

    for display_names in direct_rels.values():
        for display in display_names:
            m = _NAME_TICKER_RE.match(display)
            name = (m.group(1) or display).strip().lower()
            tk = (m.group(2) or "").strip().upper()
            if name:
                names.add(name)
            if tk:
                tickers.add(tk)
    return names, tickers


def _entity_is_known(entreprise, ticker, known_names, known_tickers):
    if ticker and ticker.strip().upper() in known_tickers:
        return True
    name = (entreprise or "").strip().lower()
    if not name:
        return False
    return any(name == kn or name in kn or kn in name for kn in known_names)


# --- Prompt / LLM call -------------------------------------------------------

SYSTEM_PROMPT_CAUSAL = (
    "Tu es un analyste financier specialise en raisonnement causal. On te "
    "donne une news deja analysee (entreprise, secteur, importance, tonalite, "
    "impact direct) et la liste EXACTE des entreprises reellement liees a "
    "l'entreprise concernee (concurrents, fournisseurs, clients, "
    "partenaires), telle qu'elle existe dans un graphe de connaissances "
    "verifie. Ta tache : construire une CHAINE de consequences indirectes "
    "plausibles, etape par etape, en partant de la news et en te propageant "
    "UNIQUEMENT a travers les entreprises listees ci-dessous.\n\n"
    "Regle absolue : n'invente JAMAIS une entreprise, un ticker ou une "
    "relation qui n'apparait pas explicitement dans la liste fournie. Si "
    "cette liste ne permet de construire aucune chaine plausible, reponds "
    "avec une chaine reduite a l'etape 1 (la source) et une confidence basse "
    "(<=20) -- ne force jamais un raisonnement au-dela de ce que les "
    "relations fournies permettent reellement.\n\n"
    "Reponds UNIQUEMENT avec un objet JSON valide, sans texte autour, avec "
    "exactement ces cles:\n"
    "{\n"
    '  "chaine": [\n'
    '    {"etape": 1, "entreprise": string, "ticker": string ou null, '
    '"mecanisme": string, "effet": "positif"|"negatif"|"neutre"},\n'
    "    ...\n"
    "  ],\n"
    '  "confidence": integer   // 0 a 100\n'
    "}\n\n"
    "L'etape 1 doit toujours etre l'entreprise source de la news elle-meme "
    "(mecanisme = l'impact direct de la news sur elle). Les etapes suivantes "
    f"(jusqu'a {MAX_CHAIN_STEPS} au total) doivent chacune correspondre a une "
    "entreprise de la liste fournie, avec un mecanisme de transmission "
    "explicite expliquant pourquoi cette entreprise est affectee par l'etape "
    "precedente."
)


def build_causal_prompt(news, direct_rels):
    lines = [
        f"Ticker source : {news['ticker']} ({news['company'] or news['ticker']})",
        f"Secteur : {news['sector'] or 'inconnu'}",
        f"Titre de la news : {news['title']}",
        f"Resume : {news['summary_brut'] or '(aucun)'}",
        f"Importance (1-10) : {news['importance']}",
        f"Tonalite : {news['tonalite']}",
        f"Impact direct deja identifie : {news['impact'] or '(non precise)'}",
        f"Horizon : {news['horizon'] or 'non precise'}",
        f"Confiance de l'analyse initiale : {news['confidence']}%",
        "",
        "Entreprises reellement liees (graphe de connaissances verifie) :",
    ]
    for rtype, names in direct_rels.items():
        lines.append(f"  - {rtype} : {', '.join(names)}")
    lines.append("")
    lines.append(
        "Construis la chaine de consequences indirectes demandee, en "
        "respectant strictement les regles du prompt systeme."
    )
    return "\n".join(lines)


def _is_rate_limit(exc):
    status = getattr(exc, "status_code", None)
    return status == 429 or "429" in str(exc) or "rate limit" in str(exc).lower()


def _is_daily_token_limit(exc):
    """Same Groq TPD (tokens-per-day) signal as analyze_news.py -- this
    prompt is notably token-heavy (full relation list + multi-step chain), so
    a run can hit the account-wide TPD cap well before CAUSAL_REASONING_DAILY_
    LIMIT itself. Must stop the whole run immediately, not retry."""
    text = str(exc).lower()
    return "tokens per day" in text or "(tpd)" in text


def _coerce_confidence(value):
    try:
        v = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, v))


def generate_causal_chain(client, news, direct_rels):
    completion = client.chat.completions.create(
        model=GROQ_MODEL,
        temperature=0.3,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT_CAUSAL},
            {"role": "user", "content": build_causal_prompt(news, direct_rels)},
        ],
    )
    content = completion.choices[0].message.content
    return json.loads(content)


def generate_with_retry(client, news, direct_rels):
    """generate_causal_chain with exponential backoff on transient (per-
    minute) 429s. A daily-token-limit 429 propagates immediately (see
    _is_daily_token_limit) so the caller can stop the whole run."""
    for attempt in range(MAX_RETRIES):
        try:
            return generate_causal_chain(client, news, direct_rels)
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


# --- Response validation / storage ------------------------------------------

def process_response(news, direct_rels, raw):
    """Validate `raw` (parsed LLM JSON) against the real Knowledge Graph
    relations for this news, dropping any hallucinated entity (never
    trusting the prompt's instructions alone). Returns
    (chaine_raisonnement_text, entreprises_impactees_list, confiance) --
    confiance is forced down to 20 if every non-source step was dropped as
    unknown (the resulting "chain" is just the source with no verified
    downstream consequence, which is not what the original confidence was
    computed against)."""
    known_names, known_tickers = _known_entities(
        direct_rels, news["ticker"], news["company"] or news["ticker"])

    steps = raw.get("chaine") or []
    kept_steps = []
    dropped = []
    for i, step in enumerate(steps[:MAX_CHAIN_STEPS]):
        entreprise = str(step.get("entreprise", "")).strip()
        ticker = step.get("ticker")
        ticker = str(ticker).strip() if ticker else None
        mecanisme = str(step.get("mecanisme", "")).strip()
        effet = str(step.get("effet", "neutre")).strip().lower()

        if i == 0:
            # Step 1 is always the source itself -- not subject to the
            # relation cross-check (it IS the entity the news is about).
            kept_steps.append({
                "etape": 1, "entreprise": entreprise or news["ticker"],
                "ticker": ticker or news["ticker"], "mecanisme": mecanisme,
                "effet": effet,
            })
            continue

        if _entity_is_known(entreprise, ticker, known_names, known_tickers):
            kept_steps.append({
                "etape": len(kept_steps) + 1, "entreprise": entreprise,
                "ticker": ticker, "mecanisme": mecanisme, "effet": effet,
            })
        else:
            dropped.append(entreprise or ticker or "(entreprise non nommee)")

    if dropped:
        logger.warning(
            "news_id=%s: %d entite(s) hors du graphe de connaissances "
            "ecartee(s) (jamais inventees dans le resultat stocke): %s",
            news["news_id"], len(dropped), ", ".join(dropped),
        )

    confidence = _coerce_confidence(raw.get("confidence"))
    if len(kept_steps) <= 1 and confidence > 20:
        confidence = 20

    lines = []
    for s in kept_steps:
        tk = f" ({s['ticker']})" if s["ticker"] else ""
        lines.append(f"{s['etape']}. {s['entreprise']}{tk} : {s['mecanisme']} "
                     f"[effet {s['effet']}]")
    chaine_texte = "\n".join(lines)

    impactees = [
        {"entreprise": s["entreprise"], "ticker": s["ticker"], "effet": s["effet"]}
        for s in kept_steps[1:]
    ]
    return chaine_texte, impactees, confidence


def store_causal_chain(conn, news_id, ticker, chaine_texte, impactees, confiance):
    conn.execute(INSERT_CAUSAL_CHAIN_SQL, {
        "news_id": news_id,
        "ticker_source": ticker,
        "chaine_raisonnement": chaine_texte,
        "entreprises_impactees": json.dumps(impactees, ensure_ascii=False),
        "confiance": confiance,
        "model": GROQ_MODEL,
    })
    conn.commit()


# --- Orchestration ------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Generate causal reasoning chains for high-importance news.")
    p.add_argument("--dry-run", action="store_true",
                   help="List eligible candidates only; no API calls.")
    p.add_argument("--limit", type=int, default=None,
                   help="Maximum number of news to process this run.")
    p.add_argument("--threshold", type=int, default=IMPORTANCE_THRESHOLD,
                   help=f"Minimum importance, 1-10 (default {IMPORTANCE_THRESHOLD}).")
    return p.parse_args(argv)


def run_causal_reasoning(conn, limit=None, threshold=IMPORTANCE_THRESHOLD):
    """Process up to `limit` (or as many as today's remaining quota allows,
    if `limit` is None) eligible news items, generating and storing a
    causal chain for each. Returns a stats dict -- never raises, any setup
    failure (missing API key, Groq client unavailable) is reported via
    stats["error"] instead:

        {"n_candidates": int, "processed": int, "failed": int,
         "skipped_no_relations": int, "quota_used": int, "quota_limit": int,
         "quota_exhausted": bool, "error": str|None}

    Shared by the CLI (main()) and dashboard/app.py's "Recalculer
    maintenant" button on the Raisonnement causal page -- both call this
    exact same function, so they can never drift into two slightly
    different selection/validation/storage paths."""
    conn.execute(CREATE_CAUSAL_CHAINS_SQL)
    conn.execute(_create_usage_table_sql(USAGE_TABLE_CAUSAL))
    conn.commit()

    today = date.today().isoformat()
    stats = {
        "n_candidates": 0, "processed": 0, "failed": 0,
        "skipped_no_relations": 0,
        "quota_used": get_usage(conn, USAGE_TABLE_CAUSAL, today),
        "quota_limit": CAUSAL_REASONING_DAILY_LIMIT,
        "quota_exhausted": False, "error": None,
    }

    relations = load_relations(conn)
    graph = build_graph(relations)

    candidates = load_eligible_news(conn, threshold=threshold)
    stats["n_candidates"] = len(candidates)
    if limit is not None:
        candidates = candidates[:limit]

    if not candidates:
        return stats

    remaining = max(0, CAUSAL_REASONING_DAILY_LIMIT - stats["quota_used"])
    if remaining <= 0:
        stats["quota_exhausted"] = True
        return stats

    from dotenv import load_dotenv
    load_dotenv()
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        stats["error"] = "GROQ_API_KEY absent. Ajoutez-la a votre .env."
        return stats

    try:
        import httpx
        from groq import Groq
        http_client = httpx.Client(verify=CA_BUNDLE) if CA_BUNDLE else None
        client = Groq(api_key=api_key, http_client=http_client)
    except Exception as exc:  # noqa: BLE001
        stats["error"] = f"Client Groq indisponible ({exc})."
        return stats

    attempts = 0
    consecutive_failures = 0

    def _note_failure():
        """Record one failed attempt; True once MAX_CONSECUTIVE_FAILURES
        have happened in a row -- the backstop bump_usage()'s success-only
        counter can never provide on its own (see
        reasoning/groq_config.py -- a deprecated/renamed model returning an
        error on every call would otherwise never trip the quota check)."""
        nonlocal consecutive_failures
        stats["failed"] += 1
        consecutive_failures += 1
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            logger.error(
                "%d echecs Groq consecutifs -- arret (modele/API probablement "
                "indisponible ou modele deprecie, voir reasoning/groq_config.py).",
                consecutive_failures)
            return True
        return False

    for news in candidates:
        if get_usage(conn, USAGE_TABLE_CAUSAL, today) >= CAUSAL_REASONING_DAILY_LIMIT:
            stats["quota_exhausted"] = True
            logger.warning("Quota atteint en cours de run. Arret.")
            break

        direct_rels = companies_to_watch(graph, relations, news["ticker"])
        if not direct_rels:
            # Should not normally happen given CANDIDATES_SQL's own filter,
            # but the graph is queried independently here -- never crash on
            # a mismatch, just skip with a clear reason.
            stats["skipped_no_relations"] += 1
            continue

        if attempts >= MAX_ATTEMPTS_PER_RUN:
            logger.warning(
                "Plafond de %d tentatives atteint pour ce run -- arret, "
                "meme si le quota quotidien (%d/%d) n'est pas atteint.",
                MAX_ATTEMPTS_PER_RUN, stats["processed"], CAUSAL_REASONING_DAILY_LIMIT)
            break
        attempts += 1

        try:
            raw = generate_with_retry(client, news, direct_rels)
        except Exception as exc:  # noqa: BLE001
            if _is_daily_token_limit(exc):
                stats["quota_exhausted"] = True
                logger.warning(
                    "Quota Groq quotidien (tokens/jour, TPD) atteint apres "
                    "%d chaine(s) generee(s). Arret du run: %s",
                    stats["processed"], exc)
                break
            logger.error("news_id=%s: appel LLM echoue (%s)", news["news_id"], exc)
            if _note_failure():
                break
            continue

        if not raw:
            if _note_failure():
                break
            continue

        chaine_texte, impactees, confiance = process_response(news, direct_rels, raw)
        try:
            store_causal_chain(conn, news["news_id"], news["ticker"],
                              chaine_texte, impactees, confiance)
        except sqlite3.Error as exc:
            conn.rollback()
            logger.error("news_id=%s: insertion echouee (%s)", news["news_id"], exc)
            if _note_failure():
                break
            continue

        bump_usage(conn, USAGE_TABLE_CAUSAL, today)
        consecutive_failures = 0
        stats["processed"] += 1
        logger.info("news_id=%s (%s) : chaine generee, confiance=%.0f%%, "
                    "%d entreprise(s) impactee(s).",
                    news["news_id"], news["ticker"], confiance, len(impactees))

    stats["quota_used"] = get_usage(conn, USAGE_TABLE_CAUSAL, today)
    return stats


# --- Read helpers (dashboard "Raisonnement causal" page + API) -----------------
#
# Relocated from dashboard/app.py: these were already conn-first/pure
# functions with no Streamlit dependency, so this is a straight move (no
# rewrite) -- dashboard/app.py now imports them from here instead of
# defining them locally, so the API (api/routers/causal_reasoning.py) and
# the Streamlit page share the exact same load/status/parse logic.

CAUSAL_CHAINS_SQL = """
SELECT c.id, c.news_id, c.ticker_source, c.chaine_raisonnement,
       c.entreprises_impactees, c.confiance, c.model, c.created_at,
       r.title AS news_title
FROM causal_chains c
LEFT JOIN news_raw r ON r.id = c.news_id
ORDER BY c.created_at DESC
LIMIT ?;
"""

CAUSAL_CHAIN_DISPLAY_LIMIT = 50


def load_causal_chains(conn, limit=CAUSAL_CHAIN_DISPLAY_LIMIT):
    """Stored causal chains, most recently generated first -- never
    restricted to a single "today" date the way Resume du jour is: this
    module runs on its own limited daily quota
    (CAUSAL_REASONING_DAILY_LIMIT), so a chain generated a few days ago is
    still the right thing to show, not a reason to show nothing. Each row
    also carries the originating news's title (news_title, None if the
    news_raw row is gone)."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(CAUSAL_CHAINS_SQL, (limit,)).fetchall()
    return [dict(r) for r in rows]


def parse_entreprises_impactees(raw_json):
    """[] on anything that isn't a valid JSON array -- never crash a caller
    over a malformed cell."""
    if not raw_json:
        return []
    try:
        parsed = json.loads(raw_json)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def causal_reasoning_status(conn):
    """(n_pending, quota_used, quota_limit, quota_remaining) for the
    "Recalculer maintenant" button -- a lightweight query, meant to be
    recomputed fresh right before a caller decides whether to offer the
    button (never cached), so the numbers shown are always accurate right
    before a click."""
    candidates = load_eligible_news(conn, threshold=IMPORTANCE_THRESHOLD)
    today = date.today().isoformat()
    used = get_usage(conn, USAGE_TABLE_CAUSAL, today)
    remaining = max(0, CAUSAL_REASONING_DAILY_LIMIT - used)
    return len(candidates), used, CAUSAL_REASONING_DAILY_LIMIT, remaining


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])

    if not os.path.exists(DB_PATH):
        logger.error("Database not found at %s.", DB_PATH)
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.execute(CREATE_CAUSAL_CHAINS_SQL)
    conn.execute(_create_usage_table_sql(USAGE_TABLE_CAUSAL))
    conn.commit()

    if args.dry_run:
        candidates = load_eligible_news(conn, threshold=args.threshold)
        logger.info("Candidats eligibles (importance >= %d, ticker dans le graphe, "
                    "pas encore traites) : %d", args.threshold, len(candidates))
        if args.limit is not None:
            candidates = candidates[:args.limit]
        for c in candidates:
            logger.info("  news_id=%s %s importance=%s confidence=%s%% : %s",
                        c["news_id"], c["ticker"], c["importance"],
                        c["confidence"], c["title"][:80])
        logger.info("[DRY-RUN] No API calls made.")
        conn.close()
        return 0

    stats = run_causal_reasoning(conn, limit=args.limit, threshold=args.threshold)
    conn.close()

    if stats["error"]:
        logger.error(stats["error"])
        return 1

    logger.info("Candidats eligibles (importance >= %d, ticker dans le graphe, "
                "pas encore traites) : %d", args.threshold, stats["n_candidates"])
    if stats["quota_exhausted"] and stats["processed"] == 0:
        logger.warning("Quota atteint (%d). Arret avant tout appel.",
                       CAUSAL_REASONING_DAILY_LIMIT)
    logger.info(
        "Termine. %d chaine(s) generee(s), %d echec(s), %d ignore(s) (sans "
        "relation KG). Quota utilise: %d/%d.",
        stats["processed"], stats["failed"], stats["skipped_no_relations"],
        stats["quota_used"], stats["quota_limit"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
