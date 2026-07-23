#!/usr/bin/env python3
"""Diagnose and mechanically fix universe tickers that fail on all 3 pillars
(price/valuation, technical, real fundamental) -- e.g. the systematic 404
"Quote not found" failures observed since analysis/fundamental_real/score.py's
first full-universe run.

A ticker is considered a total failure if:
  * it has no row in ``final_scores`` (no price_valuation_score AND no
    technical_score), AND
  * its latest ``fundamental_real_scores`` row has score_global IS NULL.

Two DISTINCT kinds of candidate fix are generated, and only one is ever
applied automatically:

  Category A -- MECHANICAL, auto-applied after empirical validation.
    Rule "nordic_class_hyphen": Yahoo Finance requires a hyphen before a
    share-class letter on Nordic exchanges (Stockholm .ST, Oslo .OL,
    Copenhagen .CO, Helsinki .HE), e.g. Ericsson B shares are "ERIC-B.ST",
    never "ERICB.ST". Universe's Wikipedia-table scraper dropped that
    hyphen. Detected via regex: suffix in {ST,OL,CO,HE}, ticker root ends in
    a class letter (A/B/C/D), root >= 2 chars. This is the ONLY rule applied
    automatically -- it is a pure formatting fix (same company, same
    listing, Yahoo just requires punctuation we didn't emit), and every
    candidate is verified against yfinance (a real, non-empty price history)
    BEFORE being written to `universe`.

  Category B -- LOOKUP-based, surfaced but NEVER auto-applied.
    For everything else (root symbol appears entirely wrong, e.g. France's
    scraper stored abbreviation-style codes like "AIRP.PA" instead of the
    real Yahoo ticker "AI.PA"), a candidate is searched via yfinance's
    Search-by-company-name API, filtered to the SAME market suffix as the
    original ticker and quoteType == EQUITY. A non-empty price history alone
    does NOT prove the candidate is the same company (a search for the
    short/generic name "BRF" returned a real, tradeable, but entirely
    unrelated Brazilian real-estate fund) -- so every candidate's real
    .info longName/shortName is fetched and compared against the expected
    company name (see _same_company); only an identity-verified AND
    price-validated candidate is reported. This is a real lookup against
    Yahoo's own data, not a guess from memory -- but since it is not a
    documented, generalisable mechanical rule, it is reported for manual
    case-by-case validation only. Nothing in this category touches the
    database.

  Category C -- no validated candidate found at all (delisted, merged, or
    genuinely unidentifiable). Listed with a reason, no candidate proposed.

Traceability: every Category A fix is both applied to `universe.ticker` AND
recorded in a new `ticker_corrections` table (old_ticker, new_ticker,
pattern, method, validated_at) -- the old ticker is never silently lost.

Usage:
    python universe/fix_ticker_mapping.py --dry-run   # report only, no writes
    python universe/fix_ticker_mapping.py             # apply Category A fixes
"""

import argparse
import logging
import os
import re
import sqlite3
import sys
import time

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

import yfinance as yf  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fix_ticker_mapping")

MAX_RETRIES = 5
BACKOFF_BASE = 2.0

CREATE_CORRECTIONS_SQL = """
CREATE TABLE IF NOT EXISTS ticker_corrections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    old_ticker    TEXT NOT NULL,
    new_ticker    TEXT NOT NULL,
    pattern       TEXT NOT NULL,
    method        TEXT NOT NULL,
    validated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# Nordic exchanges where Yahoo requires a hyphen before the share-class
# letter. Only these four suffixes are in scope for the mechanical rule.
NORDIC_SUFFIXES = {"ST", "OL", "CO", "HE"}
CLASS_LETTERS = set("ABCD")

NORDIC_CLASS_RE = re.compile(r"^([A-Z0-9]{2,})([A-D])\.(ST|OL|CO|HE)$")


def _is_rate_limit(exc):
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status == 429 or "429" in str(exc) or "rate limit" in str(exc).lower()


def _with_retry(fn, *args, **kwargs):
    for attempt in range(MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            if _is_rate_limit(exc) and attempt < MAX_RETRIES - 1:
                wait = BACKOFF_BASE * (2 ** attempt)
                logger.warning("Rate limit (429). Backoff %.0fs (try %d/%d)...",
                               wait, attempt + 1, MAX_RETRIES)
                time.sleep(wait)
                continue
            raise
    return None


# --- Data access -------------------------------------------------------------

def load_total_failures(conn):
    """Tickers with no final_scores row AND a NULL latest fundamental_real
    score -- the "fails on all 3 pillars" set, recomputed live (never
    hardcoded)."""
    universe_rows = conn.execute(
        "SELECT ticker, pays, indice_source, priorite, nom, nom_entreprise "
        "FROM universe"
    ).fetchall()
    universe = {r[0]: r for r in universe_rows}

    final_tickers = {r[0] for r in conn.execute("SELECT DISTINCT symbol FROM final_scores")}

    latest_fund = {}
    for symbol, score_global in conn.execute(
        "SELECT symbol, score_global FROM fundamental_real_scores ORDER BY id DESC"
    ):
        latest_fund.setdefault(symbol, score_global)

    failures = []
    for ticker, row in universe.items():
        if ticker in final_tickers:
            continue
        if latest_fund.get(ticker) is not None:
            continue
        failures.append({
            "ticker": ticker,
            "pays": row[1],
            "indice_source": row[2],
            "priorite": row[3],
            "nom": row[4],
            "nom_entreprise": row[5],
        })
    return sorted(failures, key=lambda r: (r["pays"] or "", r["ticker"]))


# --- Validation ----------------------------------------------------------------

def validate_ticker(candidate):
    """True if yfinance returns real, non-empty price history for candidate."""
    try:
        hist = _with_retry(lambda: yf.Ticker(candidate).history(period="5d"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("%s: validation failed (%s)", candidate, exc)
        return False
    return hist is not None and not hist.empty


# --- Category A: mechanical Nordic class-letter hyphen ------------------------

def nordic_class_hyphen_candidate(ticker):
    m = NORDIC_CLASS_RE.match(ticker)
    if not m:
        return None
    root, letter, suffix = m.groups()
    return f"{root}-{letter}.{suffix}"


# --- Category B: company-name lookup (surfaced, never auto-applied) ----------

# Legal-form words stripped before comparing names -- otherwise "SA"/"AG"/
# "Holding" etc. dominate the token overlap and mask a real mismatch (or, in
# reverse, a shared legal-form word could wrongly look like a match).
_LEGAL_FORM_WORDS = {
    "sa", "ag", "nv", "se", "ab", "oyj", "plc", "ltd", "inc", "corp", "co",
    "company", "companhia", "holding", "holdings", "group", "groupe",
    "spa", "publ", "s", "a", "the",
}


def _normalise_name(name):
    text = re.sub(r"[^\w\s]", " ", (name or "").lower())
    tokens = {t for t in text.split() if t and t not in _LEGAL_FORM_WORDS}
    return tokens


def _same_company(expected_name, candidate_symbol):
    """Identity check: fetch the candidate's real .info name and require at
    least one meaningful (non-legal-form) token in common with the expected
    company name. A non-empty price history only proves a symbol trades --
    it says nothing about which company it is (e.g. a Yahoo search for the
    short/generic query "BRF" returned an unrelated Brazilian real-estate
    fund whose price history is perfectly valid, just for the wrong
    company) -- this check is what actually protects against that."""
    expected_tokens = _normalise_name(expected_name)
    if not expected_tokens:
        return True, None  # nothing to compare against; caller decides
    try:
        info = _with_retry(lambda: yf.Ticker(candidate_symbol).info)
    except Exception as exc:  # noqa: BLE001
        logger.debug("%s: .info lookup failed (%s)", candidate_symbol, exc)
        return False, None
    real_name = (info or {}).get("longName") or (info or {}).get("shortName") or ""
    candidate_tokens = _normalise_name(real_name)
    return bool(expected_tokens & candidate_tokens), real_name


def search_candidate(ticker, company_name):
    """Search Yahoo by company name, keep only same-suffix EQUITY quotes,
    validate the top match's price history AND its real identity (see
    _same_company). Returns (candidate_or_None, resolved_name_or_None,
    all_quotes_seen)."""
    suffix = ticker.rsplit(".", 1)[-1] if "." in ticker else None
    query = company_name or ticker
    try:
        results = _with_retry(lambda: yf.Search(query, max_results=8).quotes)
    except Exception as exc:  # noqa: BLE001
        logger.debug("%s: search failed for %r (%s)", ticker, query, exc)
        return None, None, []

    same_market = [
        q for q in results
        if q.get("quoteType") == "EQUITY"
        and suffix and q.get("symbol", "").endswith(f".{suffix}")
    ]
    for q in same_market:
        sym = q.get("symbol")
        if not sym or sym == ticker:
            continue
        if not validate_ticker(sym):
            continue
        ok, real_name = _same_company(company_name, sym)
        if ok:
            return sym, real_name, results
        logger.info("%s: candidat %s rejete (identite ne correspond pas: "
                    "attendu %r, trouve %r)", ticker, sym, company_name, real_name)
    return None, None, results


def _looks_like_name(value, ticker):
    """A usable company name has at least one letter and isn't just the
    ticker echoed back. Guards against two distinct data-quality issues seen
    in `universe`: nom_entreprise falling back to the bare ticker (expected,
    handled elsewhere), and nom_entreprise instead holding a purely numeric
    scraper artifact (e.g. an ISIN-like code such as "3734810" for BNP
    Paribas) -- neither is a real name to search Yahoo with."""
    if not value or value == ticker:
        return False
    return any(c.isalpha() for c in value)


def _best_name(nom_entreprise, nom, ticker):
    if _looks_like_name(nom_entreprise, ticker):
        return nom_entreprise
    if _looks_like_name(nom, ticker):
        return nom
    return None


# --- Orchestration ---------------------------------------------------------

def diagnose(conn):
    failures = load_total_failures(conn)
    logger.info("Tickers en echec total (3 piliers): %d", len(failures))

    category_a, category_b, category_c = [], [], []

    for i, row in enumerate(failures, start=1):
        ticker = row["ticker"]
        name = _best_name(row["nom_entreprise"], row["nom"], ticker)

        candidate = nordic_class_hyphen_candidate(ticker)
        if candidate and validate_ticker(candidate):
            category_a.append({**row, "candidate": candidate, "pattern": "nordic_class_hyphen"})
            logger.info("[%d/%d] %s -> %s (Categorie A: mecanique, valide)",
                        i, len(failures), ticker, candidate)
            continue

        if name is None:
            # No usable company name anywhere in `universe` for this ticker
            # -- searching Yahoo by the broken ticker string itself has no
            # reliable identity to verify against, so don't even try.
            category_c.append(row)
            logger.info("[%d/%d] %s : aucun nom d'entreprise fiable disponible "
                        "(Categorie C)", i, len(failures), ticker)
            continue

        found, real_name, _all_quotes = search_candidate(ticker, name)
        if found:
            category_b.append({**row, "candidate": found, "real_name": real_name})
            logger.info("[%d/%d] %s -> %s (Categorie B: trouve par recherche, "
                        "identite verifiee = %r, NON applique)",
                        i, len(failures), ticker, found, real_name)
            continue

        category_c.append(row)
        logger.info("[%d/%d] %s : aucun candidat trouve (Categorie C)", i, len(failures), ticker)

    return category_a, category_b, category_c


def apply_category_a(conn, category_a):
    conn.execute(CREATE_CORRECTIONS_SQL)
    conn.commit()
    applied = []
    for row in category_a:
        old, new = row["ticker"], row["candidate"]
        try:
            conn.execute("UPDATE universe SET ticker = ? WHERE ticker = ?", (new, old))
            conn.execute(
                "INSERT INTO ticker_corrections (old_ticker, new_ticker, pattern, method) "
                "VALUES (?, ?, ?, ?)",
                (old, new, row["pattern"], "mechanical_validated"),
            )
            conn.commit()
            applied.append(row)
        except sqlite3.Error as exc:
            conn.rollback()
            logger.error("%s -> %s: DB update failed (%s)", old, new, exc)
    return applied


def print_report(category_a, category_b, category_c, applied=None):
    print("\n" + "=" * 100)
    print("CATEGORIE A -- correction mecanique validee" +
          (" (appliquee)" if applied else " (dry-run, non appliquee)"))
    print("=" * 100)
    for r in category_a:
        print(f"  {r['ticker']:<12} -> {r['candidate']:<14} [{r['pattern']}] "
              f"{r['pays']:<15} {_best_name(r['nom_entreprise'], r['nom'], r['ticker']) or '?'}")

    print("\n" + "=" * 100)
    print("CATEGORIE B -- candidat trouve par recherche du nom, identite verifiee "
          "(NON applique, decision requise)")
    print("=" * 100)
    for r in category_b:
        expected = _best_name(r['nom_entreprise'], r['nom'], r['ticker']) or '?'
        print(f"  {r['ticker']:<12} -> {r['candidate']:<14} "
              f"attendu={expected!r:<30} trouve={r['real_name']!r}")

    print("\n" + "=" * 100)
    print(f"CATEGORIE C -- aucun candidat trouve ({len(category_c)} tickers)")
    print("=" * 100)
    for r in category_c:
        name = _best_name(r['nom_entreprise'], r['nom'], r['ticker']) or "(nom indisponible)"
        print(f"  {r['ticker']:<12} {r['pays']:<15} {name}")

    print("\n" + "=" * 100)
    print(f"Total: {len(category_a)} categorie A | {len(category_b)} categorie B | "
          f"{len(category_c)} categorie C")
    print("=" * 100 + "\n")


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Diagnose and mechanically fix total-failure universe tickers.")
    p.add_argument("--dry-run", action="store_true",
                    help="Report only, apply no database changes.")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])

    if not os.path.exists(DB_PATH):
        logger.error("Database not found at %s.", DB_PATH)
        return 1

    conn = sqlite3.connect(DB_PATH)
    category_a, category_b, category_c = diagnose(conn)

    applied = None
    if not args.dry_run:
        applied = apply_category_a(conn, category_a)
        logger.info("Categorie A appliquee: %d/%d tickers corriges dans universe.",
                    len(applied), len(category_a))

    print_report(category_a, category_b, category_c, applied=applied)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
