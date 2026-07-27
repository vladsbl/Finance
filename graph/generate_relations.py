#!/usr/bin/env python3
"""Extend the Knowledge Graph beyond the 10 pilot tickers -- PROPOSAL stage.

The active Knowledge Graph (graph/build_graph.py, `relations` table) only
ever has outbound edges for the 10 pilot tickers (data/relations_seed.csv was
hand-curated). This module PROPOSES relations for the wider universe (503
"haute" priority tickers to start) via Groq, grounded in REAL anchors --
each ticker's real company name, real sector, real country (never guessed
from the model's memory alone) -- and stores every proposal in a SEPARATE
table, `relations_generated`, with statut='a_valider'. Nothing here ever
writes to the active `relations` table, and the Knowledge Graph
(graph/build_graph.py) and reasoning/causal_reasoning.py are not touched or
even imported: this script is purely generate-and-store-for-review.

Anti-hallucination design (two independent layers, same discipline as
reasoning/causal_reasoning.py and universe/fix_ticker_mapping.py):
  1. The PROMPT anchors every call in real, already-known facts (company
     name, sector, country from `universe`/ticker_sector_cache) and asks for
     a short justification + a 0-100 confidence per proposed relation --
     never asks the model to invent facts, only to reason from what it is
     given plus its own general knowledge of well-known business relationships.
  2. Every CITED company (never the source ticker, which is already real) is
     cross-checked against `universe` after the call -- offline, via
     universe/fix_ticker_mapping.py's own name-normalisation (exact or
     subset token match; loose "any shared word" overlap is deliberately
     NOT used, to avoid e.g. matching "American Express" against "American
     Airlines" on the shared word "american"). A match resolves to that
     company's REAL ticker. NO match is INSERTED into `universe` at this
     stage -- unresolved names are stored anyway (for visibility) but
     flagged resolved=0, exactly the same "list separately, decide later"
     discipline already used for graph/sync_related_companies.py's new
     related companies.

Sampling for the review batch (see select_diverse_sample): one ticker per
distinct sector (from ticker_sector_cache, populated by
universe/fetch_sector_info.py), cycling through sectors again if fewer than
`n` distinct sectors exist, so a small sample still surfaces sector-specific
quality variance instead of testing 20 tickers from the same industry.

Usage:
    python universe/fetch_sector_info.py --priorite haute   # prerequisite (yfinance only, no Groq)
    python graph/generate_relations.py --sample 20 --dry-run
    python graph/generate_relations.py --sample 20
    python graph/generate_relations.py --sample 503          # full haute universe, run across days

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

# Reused as-is: the exact same legal-form-stripping name tokeniser already
# validated for identity checks elsewhere in this project (offline here --
# no yfinance/network call, we only match against names already in `universe`).
from universe.fix_ticker_mapping import (  # noqa: E402
    _normalise_name, _with_retry, validate_ticker,
)
# Reused as-is: same generic (table-parameterised) daily-usage-counter trio
# introduced for the daily_summary.py / causal_reasoning.py quota pools.
from reasoning.daily_summary import (  # noqa: E402
    _create_usage_table_sql, bump_usage, get_usage,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("generate_relations")

# --- Configuration -----------------------------------------------------------

GROQ_MODEL = "llama-3.3-70b-versatile"
MAX_RETRIES = 5
BACKOFF_BASE = 2.0

USAGE_TABLE_RELATIONS_GEN = "llm_usage_relations_generation"
# Deliberately generous relative to the other pools (llm_usage_summary=3,
# llm_usage_ticker_analysis=10, llm_usage_causal_reasoning=5): this is a
# one-off, human-supervised BACKFILL across the 503 haute tickers, spread
# over several days on purpose (see module docstring / --sample), not a
# recurring daily job competing with the others for the same day's budget.
# Recalibrate after measuring real per-call token cost on the 20-sample test
# (this module logs total tokens used per run for exactly that purpose).
RELATIONS_GEN_DAILY_LIMIT = 60

ALLOWED_RELATION_TYPES = {"concurrent", "fournisseur", "client", "dependance"}

CREATE_RELATIONS_GENERATED_SQL = """
CREATE TABLE IF NOT EXISTS relations_generated (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_ticker   TEXT NOT NULL,
    relation_type   TEXT NOT NULL,
    target_name     TEXT NOT NULL,
    target_ticker   TEXT,
    resolved        INTEGER NOT NULL DEFAULT 0,
    justification   TEXT,
    confiance       REAL,
    statut          TEXT NOT NULL DEFAULT 'a_valider',
    model           TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (source_ticker, relation_type, target_name)
);
"""

INSERT_RELATION_GENERATED_SQL = """
INSERT INTO relations_generated
    (source_ticker, relation_type, target_name, target_ticker, resolved,
     justification, confiance, statut, model)
VALUES
    (:source_ticker, :relation_type, :target_name, :target_ticker, :resolved,
     :justification, :confiance, 'a_valider', :model)
ON CONFLICT(source_ticker, relation_type, target_name) DO UPDATE SET
    target_ticker = excluded.target_ticker,
    resolved      = excluded.resolved,
    justification = excluded.justification,
    confiance     = excluded.confiance,
    model         = excluded.model;
"""


# --- Sector-diverse sampling --------------------------------------------------

def load_sector_grouped_tickers(conn, priorite="haute"):
    """{sector_or_'Inconnu': [ticker, ...]} for tickers already cached in
    ticker_sector_cache (see universe/fetch_sector_info.py). Order within
    each sector is alphabetical for determinism."""
    rows = conn.execute(
        "SELECT u.ticker, c.sector FROM universe u "
        "JOIN ticker_sector_cache c ON c.ticker = u.ticker "
        "WHERE u.priorite = ? ORDER BY u.ticker",
        (priorite,),
    ).fetchall()
    grouped = {}
    for ticker, sector in rows:
        grouped.setdefault(sector or "Inconnu", []).append(ticker)
    return grouped


def select_diverse_sample(grouped, n):
    """Up to `n` tickers, one per distinct sector first (in sorted sector
    order, for reproducibility), then cycling back through sectors for any
    remainder -- so even a small sample spans as many sectors as exist."""
    sectors = sorted(grouped)
    queues = {s: list(grouped[s]) for s in sectors}
    selected = []
    while len(selected) < n and any(queues.values()):
        for s in sectors:
            if len(selected) >= n:
                break
            if queues[s]:
                selected.append(queues[s].pop(0))
    return selected


# --- Universe name resolution (offline, no network) ---------------------------

def build_universe_index(conn):
    """{ticker: (nom_tokens, nom_entreprise_tokens)} for every universe row."""
    rows = conn.execute("SELECT ticker, nom, nom_entreprise FROM universe").fetchall()
    return {ticker: (_normalise_name(nom), _normalise_name(nom_entreprise))
            for ticker, nom, nom_entreprise in rows}


# Bare commodity/raw-material names -- when a "dependance" relation's
# target_name reduces to just one of these (e.g. "Lithium", "Copper",
# "Petrole"), it names a MATERIAL, not a company, and must never be treated
# as a candidate for company-name resolution or ticker verification (see
# _GENERIC_SINGLE_WORDS below and verify_and_add_company's docstring).
COMMODITY_WORDS = {
    "lithium", "copper", "gold", "silver", "oil", "gas", "steel", "wheat",
    "corn", "cobalt", "nickel", "aluminum", "aluminium", "petrole",
    "petroliere", "petroleum", "coal", "uranium", "zinc", "platinum",
    "palladium",
}

# Generic corporate nouns and commodity names that, taken ALONE (as the only
# token on one side of a match), describe far too many real-world companies
# to safely resolve a subset match -- as opposed to a genuinely distinctive
# single-word brand/proper noun (e.g. "Deere", "Schlumberger", "Alphabet",
# "TSMC"), which IS safe to match this way. Discovered by testing, not
# theorised: "Guidewire Software, Inc." and "Unity Software Inc." (two
# unrelated real companies, neither in `universe`) both matched ticker
# SOW.DE purely because SOW.DE's own name is literally the single word
# "Software"; "Crown Holdings, Inc." matched "Crown Castle" (CCI) on
# "crown"; "Lithium" and "Copper" (bare commodity names proposed for
# "dependance" relations, not company names at all) matched real miners
# Tianqi Lithium and Jiangxi Copper purely because those tickers' names
# happen to contain that one word. Kept as a small, reviewed list rather
# than a generic dictionary lookup -- same pragmatic style as
# universe/fix_ticker_mapping.py's own _LEGAL_FORM_WORDS.
_GENERIC_SINGLE_WORDS = COMMODITY_WORDS | {
    "software", "systems", "solutions", "technologies", "technology",
    "industries", "industry", "resources", "materials", "capital",
    "partners", "ventures", "global", "international", "energy", "power",
    "services", "networks", "network", "labs", "laboratories", "sciences",
    "crown", "financial", "financials", "bank", "insurance", "properties",
    "media", "communications", "digital", "data",
}


def _joined_form(tokens):
    """Order-independent, space-independent form of a token set (sorted and
    concatenated) -- lets "Exxon Mobil Corporation" (tokens {"exxon",
    "mobil", "corporation"}) match universe ticker XOM, whose real name
    merges the brand into one word: "ExxonMobil Holdings Corporation"
    (tokens {"exxonmobil", "corporation"} after legal-form stripping). Both
    join to "corporationexxonmobil". A narrowly-targeted extra check, not a
    replacement for exact/subset matching -- two genuinely different
    multi-word names would need to share the exact same multiset of word
    fragments to collide here, which real company names essentially never do."""
    return "".join(sorted(tokens))


def _tokens_match(a, b):
    """Exact match, subset match (gated when the smaller side is a single
    generic/commodity token -- see _GENERIC_SINGLE_WORDS docstring: some real
    `universe` entries reduce to one generic word after legal-form stripping,
    which would otherwise be a "subset" of ANY multi-word target containing
    that word), or joined-form equality (see _joined_form -- catches a
    word-merged brand name like "ExxonMobil" vs "Exxon Mobil"). Loose "any
    shared word" overlap is deliberately NOT used in any of these (see module
    docstring: avoids matching e.g. "American Express" against "American
    Airlines" on the shared word "american")."""
    if not a or not b:
        return False
    if a == b:
        return True
    smaller, larger = (a, b) if len(a) <= len(b) else (b, a)
    if len(smaller) == 1 and next(iter(smaller)) in _GENERIC_SINGLE_WORDS:
        return _joined_form(a) == _joined_form(b)
    if smaller <= larger:
        return True
    return _joined_form(a) == _joined_form(b)


def resolve_target(target_name, target_ticker_guess, universe_index):
    """Match a Groq-cited company against a ticker ALREADY in `universe` --
    NEVER inserts a new ticker (see module docstring). Returns
    (matched_ticker_or_None, resolved_bool). A ticker guess is only trusted
    if the target's name also plausibly matches that ticker's own real
    name(s) -- a guessed ticker that happens to exist in `universe` but names
    a completely different company must not be silently accepted."""
    target_tokens = _normalise_name(target_name)

    if target_ticker_guess:
        tk = target_ticker_guess.strip().upper()
        cand = universe_index.get(tk)
        if cand and (not target_tokens or _tokens_match(target_tokens, cand[0])
                     or _tokens_match(target_tokens, cand[1])):
            return tk, True

    if not target_tokens:
        return None, False
    for ticker, (n1, n2) in universe_index.items():
        if _tokens_match(target_tokens, n1) or _tokens_match(target_tokens, n2):
            return ticker, True
    return None, False


# --- Prompt / LLM call -------------------------------------------------------

SYSTEM_PROMPT_RELATIONS = (
    "Tu es un analyste sectoriel. On te donne le nom reel, le secteur reel "
    "(classification GICS) et le pays reel d'une entreprise cotee en bourse. "
    "Ta tache : proposer entre 2 et 4 relations d'affaires reelles et "
    "notables pour cette entreprise, parmi ces types :\n"
    "  - concurrent : un concurrent direct dans le meme secteur/marche\n"
    "  - fournisseur : un fournisseur cle de cette entreprise\n"
    "  - client : un client cle de cette entreprise\n"
    "  - dependance : une dependance notable a une matiere premiere ou un "
    "composant critique -- UNIQUEMENT si cela a un sens reel pour ce "
    "secteur (ne force jamais ce type)\n\n"
    "Pour chaque relation : donne le nom reel de l'entreprise concernee "
    "(jamais une entreprise fictive ou approximative), son ticker boursier "
    "si tu le connais avec une certitude raisonnable (sinon null -- ne "
    "devine jamais un ticker), une justification courte (une phrase) et un "
    "niveau de confiance (0 a 100) reflechissant a quel point cette relation "
    "est reelle et notable aujourd'hui. Reste strictement factuel : "
    "n'invente aucune relation douteuse uniquement pour atteindre 4 -- mieux "
    "vaut 2 relations solides que 4 dont une hasardeuse.\n\n"
    "Reponds UNIQUEMENT avec un objet JSON valide, sans texte autour, avec "
    "exactement cette cle:\n"
    "{\n"
    '  "relations": [\n'
    '    {"relation_type": "concurrent"|"fournisseur"|"client"|"dependance",\n'
    '     "target_name": string, "target_ticker": string ou null,\n'
    '     "justification": string, "confidence": integer},\n'
    "    ...\n"
    "  ]\n"
    "}"
)


def build_relations_prompt(ticker, nom_entreprise, sector, pays):
    return (
        f"Entreprise : {nom_entreprise} (ticker {ticker})\n"
        f"Secteur (GICS) : {sector or 'inconnu'}\n"
        f"Pays : {pays or 'inconnu'}\n\n"
        "Propose les relations demandees pour cette entreprise, en "
        "respectant strictement les regles du prompt systeme."
    )


def _is_rate_limit(exc):
    status = getattr(exc, "status_code", None)
    return status == 429 or "429" in str(exc) or "rate limit" in str(exc).lower()


def _is_daily_token_limit(exc):
    text = str(exc).lower()
    return "tokens per day" in text or "(tpd)" in text


def generate_relations_for_ticker(client, ticker, nom_entreprise, sector, pays):
    """Returns (parsed_json_dict, total_tokens_used_or_None)."""
    completion = client.chat.completions.create(
        model=GROQ_MODEL,
        temperature=0.3,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT_RELATIONS},
            {"role": "user", "content": build_relations_prompt(
                ticker, nom_entreprise, sector, pays)},
        ],
    )
    content = completion.choices[0].message.content
    usage = getattr(completion, "usage", None)
    total_tokens = getattr(usage, "total_tokens", None) if usage else None
    return json.loads(content), total_tokens


def generate_with_retry(client, ticker, nom_entreprise, sector, pays):
    for attempt in range(MAX_RETRIES):
        try:
            return generate_relations_for_ticker(client, ticker, nom_entreprise, sector, pays)
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
    return None, None


def _coerce_confidence(value):
    try:
        v = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, v))


def process_relations(ticker, raw, universe_index):
    """Validate/resolve one ticker's raw Groq response. Returns a list of row
    dicts ready for INSERT_RELATION_GENERATED_SQL."""
    rows = []
    for item in (raw or {}).get("relations") or []:
        rtype = str(item.get("relation_type", "")).strip().lower()
        if rtype not in ALLOWED_RELATION_TYPES:
            logger.warning("%s: type de relation inattendu ignore (%r)", ticker, rtype)
            continue
        target_name = str(item.get("target_name", "")).strip()
        if not target_name:
            continue
        target_ticker_guess = item.get("target_ticker")
        target_ticker_guess = str(target_ticker_guess).strip() if target_ticker_guess else None

        matched_ticker, resolved = resolve_target(target_name, target_ticker_guess, universe_index)

        rows.append({
            "source_ticker": ticker,
            "relation_type": rtype,
            "target_name": target_name,
            "target_ticker": matched_ticker,
            "resolved": 1 if resolved else 0,
            "justification": str(item.get("justification", "")).strip(),
            "confiance": _coerce_confidence(item.get("confidence")),
            "model": GROQ_MODEL,
        })
    return rows


def store_relations(conn, rows):
    for row in rows:
        conn.execute(INSERT_RELATION_GENERATED_SQL, row)
    conn.commit()


# --- Verification / addition of unresolved "a revoir" companies --------------
#
# Companies a Groq call cited that are NOT yet in `universe` are never
# guessed onto an existing ticker (see resolve_target). This section applies
# the SAME empirical, never-guess verification already used and validated by
# graph/sync_related_companies.py for the Knowledge Graph's related
# companies (BYDDY/RIVN/SSNLF added, HNHPF/TSM correctly left unresolved):
# search Yahoo by name, require BOTH a real, currently-tradeable price
# history (validate_ticker) AND a confirmed identity match
# (_same_company) before ever adding anything to `universe`. A bare
# commodity/raw-material name (see COMMODITY_WORDS) is never even searched --
# it is not a company and searching for one would only waste an API call on
# a search that (correctly) cannot succeed. Anything else that fails
# verification (private company like Cargill, delisted/merged company like
# Praxair) simply stays resolved=0 -- exactly the same "no ticker guessed"
# outcome as a commodity, just reached by an actual empirical check instead
# of a name-pattern shortcut.

def ensure_verification_column(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(relations_generated)")}
    if "verification_tentee" not in cols:
        conn.execute(
            "ALTER TABLE relations_generated "
            "ADD COLUMN verification_tentee INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()


def is_commodity_name(target_name):
    """True if EVERY token of `target_name` (after legal-form stripping) is
    a bare commodity/raw-material word -- e.g. "Lithium", "Petroliere" match,
    but "Peabody Energy Corporation" does not (real company name, even
    though a supplier of a commodity)."""
    tokens = _normalise_name(target_name)
    return bool(tokens) and tokens <= COMMODITY_WORDS


def load_unverified_target_names(conn):
    """Distinct target_name values across ALL of relations_generated (any
    source ticker, any day's batch) that are unresolved and have never been
    through verify_and_add_company -- so a company cited by several source
    tickers is only ever searched/verified ONCE, and a name already resolved
    to an existing universe ticker is never re-checked."""
    ensure_verification_column(conn)
    rows = conn.execute(
        "SELECT DISTINCT target_name FROM relations_generated "
        "WHERE resolved = 0 AND verification_tentee = 0"
    ).fetchall()
    return [r[0] for r in rows]


def search_new_company(company_name):
    """Search Yahoo by company name for a ticker not yet in `universe` at
    all -- unlike fix_ticker_mapping.py's search_candidate (built to find an
    ALTERNATE ticker for a company that already has one under repair, and
    which restricts results to that existing ticker's exchange suffix), we
    have no existing ticker to anchor a suffix to here, so every EQUITY
    result is a candidate. Returns (ticker_or_None, real_name_or_None).

    Identity check is deliberately STRICTER than resolve_target's (exact
    token-set match, or _joined_form equality for word-merged brand names --
    NO subset/containment matching), and does NOT reuse
    fix_ticker_mapping.py's _same_company (bool(expected_tokens &
    candidate_tokens), i.e. ANY shared word). Both weaker rules are fine in
    their original contexts -- resolve_target only ever produces a REVIEW
    suggestion in relations_generated, a human checks it before it means
    anything; _same_company is normally applied to a single, already
    strongly-suggested candidate (a CSV's own ticker, or the LLM's own high-
    confidence guess), not looped over several raw search results. This
    function, by contrast, WRITES DIRECTLY to the live `universe` table used
    throughout the app (scoring, dashboard, causal_reasoning) with no human
    in the loop, and loops over up to 8 raw search results -- exactly the
    combination that produced a real false positive in testing: searching
    "General Electric Company" returned Portland General Electric (POR)
    among the results, and the loose subset/any-shared-word rules both
    accepted it purely because "General Electric" is contained in "Portland
    General Electric". Exact-or-joined-form matching correctly rejects that
    (and did not lose a single one of the 7 genuinely correct matches found
    in the same test run -- all 7 turned out to already be exact matches)."""
    import yfinance as yf
    try:
        results = _with_retry(lambda: yf.Search(company_name, max_results=8).quotes)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Recherche echouee pour %r (%s)", company_name, exc)
        return None, None
    target_tokens = _normalise_name(company_name)
    for q in results or []:
        if q.get("quoteType") != "EQUITY":
            continue
        sym = q.get("symbol")
        if not sym:
            continue
        if not validate_ticker(sym):
            continue
        info = _fetch_info(sym)
        real_name = (info.get("longName") or info.get("shortName") or "").strip()
        cand_tokens = _normalise_name(real_name)
        if target_tokens and cand_tokens and (
                target_tokens == cand_tokens
                or _joined_form(target_tokens) == _joined_form(cand_tokens)):
            return sym, real_name
        logger.info("%s: candidat %s rejete (identite ne correspond pas "
                    "exactement: attendu %r, trouve %r)",
                    company_name, sym, company_name, real_name)
    return None, None


def _fetch_info(ticker):
    import yfinance as yf
    try:
        return _with_retry(lambda: yf.Ticker(ticker).info) or {}
    except Exception:  # noqa: BLE001
        return {}


SOURCE_LABEL_VERIFIED = "Relations generees (Groq, verifie)"


def verify_and_add_company(conn, target_name):
    """Attempt to resolve `target_name` to a real, currently-tradeable
    ticker NOT already in `universe`, and if found and identity-confirmed,
    add it (priorite='basse') plus cache its real sector/industry. Returns
    (ticker_or_None, added_bool). Commodities, private companies (no public
    ticker exists to find, e.g. Cargill) and delisted/merged companies (e.g.
    Praxair, absorbed into Linde in 2018) all naturally fail validate_ticker
    or _same_company and return (None, False) -- never a guessed
    replacement ticker."""
    if is_commodity_name(target_name):
        return None, False

    sym, real_name = search_new_company(target_name)
    if not sym:
        return None, False

    existing = conn.execute("SELECT 1 FROM universe WHERE ticker = ?", (sym,)).fetchone()
    if existing:
        return sym, False  # already tracked under a name our token-match missed

    info = _fetch_info(sym)
    pays = info.get("country") or "Unknown"
    devise = info.get("currency") or "USD"
    sector = (info.get("sector") or "").strip() or None
    industry = (info.get("industry") or "").strip() or None

    try:
        conn.execute(
            "INSERT INTO universe (ticker, nom, pays, indice_source, devise, "
            "priorite, nom_entreprise) VALUES (?, ?, ?, ?, ?, 'basse', ?)",
            (sym, target_name, pays, SOURCE_LABEL_VERIFIED, devise, real_name or target_name),
        )
        conn.execute(
            "INSERT INTO ticker_sector_cache (ticker, sector, industry) VALUES (?, ?, ?) "
            "ON CONFLICT(ticker) DO UPDATE SET sector = excluded.sector, "
            "industry = excluded.industry, fetched_at = CURRENT_TIMESTAMP",
            (sym, sector, industry),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        logger.warning("%s: deja present (%s), ignore.", sym, exc)
        return sym, False
    return sym, True


def process_unresolved_relations(conn):
    """Verify/add every not-yet-attempted unresolved target_name (across the
    whole of relations_generated, not just the latest batch), updating every
    relations_generated row sharing that name. Returns (newly_added_tickers,
    n_commodity_skipped, n_checked_not_found)."""
    names = load_unverified_target_names(conn)
    newly_added = []
    n_commodity = 0
    n_not_found = 0

    for name in names:
        if is_commodity_name(name):
            n_commodity += 1
            conn.execute(
                "UPDATE relations_generated SET verification_tentee = 1 "
                "WHERE target_name = ? AND resolved = 0",
                (name,),
            )
            conn.commit()
            continue

        ticker, added = verify_and_add_company(conn, name)
        if ticker:
            conn.execute(
                "UPDATE relations_generated SET target_ticker = ?, resolved = 1, "
                "verification_tentee = 1 WHERE target_name = ? AND resolved = 0",
                (ticker, name),
            )
            if added:
                newly_added.append(ticker)
                logger.info("%s : verifie et ajoute a universe (priorite basse, ticker %s).",
                            name, ticker)
            else:
                logger.info("%s : verifie, deja suivi sous %s.", name, ticker)
        else:
            n_not_found += 1
            conn.execute(
                "UPDATE relations_generated SET verification_tentee = 1 "
                "WHERE target_name = ? AND resolved = 0",
                (name,),
            )
            logger.info("%s : aucun ticker reel trouve/confirme (matiere "
                        "premiere, entreprise privee, ou fusionnee) -- reste "
                        "a_valider sans ticker.", name)
        conn.commit()

    return newly_added, n_commodity, n_not_found


# --- Orchestration ------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Propose Knowledge Graph relations for the wider universe (review stage).")
    p.add_argument("--sample", type=int, default=20,
                   help="Number of tickers to process this run (default 20).")
    p.add_argument("--priorite", default="haute", choices=["haute", "moyenne", "basse"])
    p.add_argument("--dry-run", action="store_true",
                   help="Show the selected sample only; no API calls.")
    p.add_argument("--no-verify", action="store_true",
                   help="Skip the post-generation verify/add-to-universe pass "
                        "for unresolved ('a revoir') companies.")
    p.add_argument("--verify-only", action="store_true",
                   help="Run ONLY the verify/add-to-universe pass on all "
                        "not-yet-attempted unresolved rows; no Groq generation.")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])

    if not os.path.exists(DB_PATH):
        logger.error("Database not found at %s.", DB_PATH)
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.execute(CREATE_RELATIONS_GENERATED_SQL)
    conn.execute(_create_usage_table_sql(USAGE_TABLE_RELATIONS_GEN))
    conn.commit()
    ensure_verification_column(conn)

    if args.verify_only:
        newly_added, n_commodity, n_not_found = process_unresolved_relations(conn)
        logger.info(
            "Verification terminee. %d entreprise(s) verifiee(s) et ajoutee(s) "
            "a universe (%s), %d matiere(s) premiere(s) ignoree(s), %d "
            "non trouvee(s)/non confirmee(s).",
            len(newly_added), ", ".join(newly_added) if newly_added else "aucune",
            n_commodity, n_not_found,
        )
        conn.close()
        return 0

    grouped = load_sector_grouped_tickers(conn, args.priorite)
    if not grouped:
        logger.error(
            "Aucun secteur en cache pour priorite=%s. Lancez d'abord: "
            "python universe/fetch_sector_info.py --priorite %s",
            args.priorite, args.priorite)
        conn.close()
        return 1

    n_sectors = len(grouped)
    n_tickers = sum(len(v) for v in grouped.values())
    logger.info("%d secteurs distincts en cache sur %d tickers (priorite=%s).",
                n_sectors, n_tickers, args.priorite)

    already_done = {r[0] for r in conn.execute(
        "SELECT DISTINCT source_ticker FROM relations_generated")}
    grouped_remaining = {s: [t for t in ts if t not in already_done]
                        for s, ts in grouped.items()}
    grouped_remaining = {s: ts for s, ts in grouped_remaining.items() if ts}

    sample = select_diverse_sample(grouped_remaining, args.sample)
    logger.info("Echantillon selectionne (%d tickers, deja traites exclus) : %s",
                len(sample), ", ".join(sample))

    if args.dry_run or not sample:
        conn.close()
        return 0

    today = date.today().isoformat()
    used_today = get_usage(conn, USAGE_TABLE_RELATIONS_GEN, today)
    remaining_quota = max(0, RELATIONS_GEN_DAILY_LIMIT - used_today)
    logger.info("Quota generation relations : %d/%d utilises aujourd'hui, %d restants.",
                used_today, RELATIONS_GEN_DAILY_LIMIT, remaining_quota)
    if remaining_quota <= 0:
        logger.warning("Quota atteint. Arret avant tout appel.")
        conn.close()
        return 0
    sample = sample[:remaining_quota]

    from dotenv import load_dotenv
    load_dotenv()
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        logger.error("GROQ_API_KEY absent. Ajoutez-la a votre .env.")
        conn.close()
        return 1

    import httpx
    from groq import Groq
    http_client = httpx.Client(verify=CA_BUNDLE) if CA_BUNDLE else None
    client = Groq(api_key=api_key, http_client=http_client)

    universe_index = build_universe_index(conn)
    universe_rows = dict(conn.execute(
        "SELECT ticker, nom_entreprise FROM universe"))
    sector_map = dict(conn.execute("SELECT ticker, sector FROM ticker_sector_cache"))
    pays_map = dict(conn.execute("SELECT ticker, pays FROM universe"))

    processed, failed, total_tokens = 0, 0, 0
    for ticker in sample:
        if get_usage(conn, USAGE_TABLE_RELATIONS_GEN, today) >= RELATIONS_GEN_DAILY_LIMIT:
            logger.warning("Quota atteint en cours de run. Arret.")
            break

        nom_entreprise = universe_rows.get(ticker) or ticker
        sector = sector_map.get(ticker)
        pays = pays_map.get(ticker)

        try:
            raw, tokens = generate_with_retry(client, ticker, nom_entreprise, sector, pays)
        except Exception as exc:  # noqa: BLE001
            if _is_daily_token_limit(exc):
                logger.warning(
                    "Quota Groq quotidien (tokens/jour, TPD) atteint apres "
                    "%d ticker(s) traite(s). Arret du run: %s", processed, exc)
                break
            logger.error("%s: appel LLM echoue (%s)", ticker, exc)
            failed += 1
            continue

        if not raw:
            failed += 1
            continue
        if tokens:
            total_tokens += tokens

        rows = process_relations(ticker, raw, universe_index)
        try:
            store_relations(conn, rows)
        except sqlite3.Error as exc:
            conn.rollback()
            logger.error("%s: insertion echouee (%s)", ticker, exc)
            failed += 1
            continue

        bump_usage(conn, USAGE_TABLE_RELATIONS_GEN, today)
        processed += 1
        n_resolved = sum(1 for r in rows if r["resolved"])
        logger.info("%s (%s, secteur=%s) : %d relation(s) proposee(s), "
                    "%d resolue(s) dans universe, %d a revoir.",
                    ticker, nom_entreprise, sector, len(rows), n_resolved,
                    len(rows) - n_resolved)

    avg_tokens = (total_tokens / processed) if processed else 0
    logger.info(
        "Termine. %d ticker(s) traite(s), %d echec(s). Tokens Groq utilises "
        "cette session : %d (moyenne %.0f/ticker). Quota utilise: %d/%d.",
        processed, failed, total_tokens, avg_tokens,
        get_usage(conn, USAGE_TABLE_RELATIONS_GEN, today), RELATIONS_GEN_DAILY_LIMIT,
    )

    if not args.no_verify:
        newly_added, n_commodity, n_not_found = process_unresolved_relations(conn)
        logger.info(
            "Verification des 'a revoir' terminee. %d entreprise(s) verifiee(s) "
            "et ajoutee(s) a universe (%s), %d matiere(s) premiere(s) ignoree(s), "
            "%d non trouvee(s)/non confirmee(s).",
            len(newly_added), ", ".join(newly_added) if newly_added else "aucune",
            n_commodity, n_not_found,
        )

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
