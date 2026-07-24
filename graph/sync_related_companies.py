#!/usr/bin/env python3
"""Ensure every company related to a tracked ticker (competitor/supplier/
client/partner, from the Knowledge Graph `relations` table) is itself
tracked in `universe`.

The Knowledge Graph today only ever draws relations FOR the 10 pilot
tickers (data/relations_seed.csv has no other source_ticker), but many of
their ~20 distinct related companies (AMD, Intel, TSMC, Mastercard, Bank of
America, ...) are not in `universe` at all -- they only ever show up as
small "external" graph nodes with no score data behind them. This script
closes that gap: for each related company missing from `universe`, it
verifies a real, tradeable ticker via yfinance (never guessing) using the
exact same two-step method as universe/fix_ticker_mapping.py's Category B:

  1. Try the ticker the relations CSV already provides (after stripping
     stray whitespace -- e.g. "MS " for Morgan Stanley). Validate it has
     real price history AND that its yfinance .info longName shares a
     token with the expected company name (see _same_company) -- a
     non-empty price history alone does not prove it is the right company.
  2. If that fails, search Yahoo by company name (same approach as
     search_candidate), still identity-verified before being accepted.

Only a verified match is added to `universe` (priorite="basse" by
default -- these are context/background tickers, not actively scored
opportunities). Anything that fails both steps is reported separately with
a reason, never guessed.

Usage:
    python graph/sync_related_companies.py --dry-run   # report only
    python graph/sync_related_companies.py              # apply
"""

import argparse
import logging
import os
import re
import sqlite3
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

DB_PATH = os.path.join(REPO_ROOT, "data", "marketdb.db")

# Reuses the exact same empirically-verified-only approach (never guess a
# ticker) already built and validated for the universe ticker-mapping
# cleanup -- no need to duplicate that logic here.
from universe.fix_ticker_mapping import (  # noqa: E402
    _same_company, search_candidate, validate_ticker,
)
from ingestion.fetch_prices import SYMBOLS as PILOT_TICKERS  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("sync_related_companies")

DEFAULT_PRIORITE = "basse"
SOURCE_LABEL = "Knowledge Graph (relations)"


def load_related_companies(conn):
    """Distinct (name, ticker) pairs from `relations` that are NOT one of
    the 10 pilot source tickers themselves (those are trivially already in
    `universe`)."""
    rows = conn.execute(
        "SELECT DISTINCT target_name, target_ticker FROM relations "
        "WHERE target_ticker IS NOT NULL AND TRIM(target_ticker) != ''"
    ).fetchall()
    seen = {}
    for name, ticker in rows:
        ticker = ticker.strip()
        if ticker in PILOT_TICKERS:
            continue
        seen.setdefault(ticker, name)  # first name seen per ticker
    return sorted(seen.items())  # [(ticker, name), ...]


def already_tracked(conn, ticker):
    row = conn.execute("SELECT 1 FROM universe WHERE ticker = ?", (ticker,)).fetchone()
    return row is not None


def _fetch_info(ticker):
    import yfinance as yf
    try:
        return yf.Ticker(ticker).info or {}
    except Exception:  # noqa: BLE001
        return {}


def diagnose(conn):
    """Returns (already_tracked, verified, unresolved) -- three lists of
    dicts, never touching the database."""
    related = load_related_companies(conn)
    logger.info("Entreprises liees distinctes (hors pilotes): %d", len(related))

    tracked, verified, unresolved = [], [], []
    for csv_ticker, name in related:
        if already_tracked(conn, csv_ticker):
            tracked.append({"ticker": csv_ticker, "name": name})
            logger.info("%s (%s) : deja dans universe.", csv_ticker, name)
            continue

        # Step 1: the CSV's own ticker, cleaned up and empirically verified.
        csv_real_name = None
        if validate_ticker(csv_ticker):
            ok, csv_real_name = _same_company(name, csv_ticker)
            if ok:
                info = _fetch_info(csv_ticker)
                verified.append({
                    "ticker": csv_ticker, "name": name, "real_name": csv_real_name,
                    "method": "csv_ticker_verified", "info": info,
                })
                logger.info("%s (%s) : ticker CSV verifie (%s).",
                            csv_ticker, name, csv_real_name)
                continue

        # Step 2: search by name (same identity-verification as Category B).
        found, real_name, _quotes = search_candidate(csv_ticker, name)
        if found:
            info = _fetch_info(found)
            verified.append({
                "ticker": found, "name": name, "real_name": real_name,
                "method": "search_verified", "info": info,
                "csv_ticker_rejected": csv_ticker,
            })
            logger.info("%s (%s) : ticker CSV rejete, trouve %s par recherche (%s).",
                        csv_ticker, name, found, real_name)
            continue

        if csv_real_name:
            # The ticker DOES trade and resolve to a real company (csv_real_name)
            # -- it just failed the word-overlap identity check, most likely
            # because `name` is a brand/acronym (e.g. "Foxconn", "TSMC") that
            # never appears verbatim in the formal yfinance longName (e.g.
            # "Hon Hai Precision Industry Co., Ltd.", "Taiwan Semiconductor
            # Manufacturing Company Limited"). Reported with the real name
            # attached so a human can judge the alias in one look, rather than
            # silently accepting OR burying a plausibly-correct match under a
            # generic "not found" -- never guessed/auto-accepted here.
            reason = (f"ticker CSV valide et actif, mais nom retourne ({csv_real_name!r}) "
                      f"ne partage aucun mot avec {name!r} -- probable alias/acronyme, "
                      f"a valider manuellement")
        else:
            reason = ("ticker CSV invalide/inactif et aucun candidat "
                      "trouve par recherche du nom")
        unresolved.append({"ticker": csv_ticker, "name": name, "reason": reason})
        logger.info("%s (%s) : NON RESOLU (%s).", csv_ticker, name, reason)

    return tracked, verified, unresolved


def apply_additions(conn, verified, priorite=DEFAULT_PRIORITE):
    applied = []
    for row in verified:
        ticker = row["ticker"]
        info = row.get("info") or {}
        nom = row["name"]
        nom_entreprise = row.get("real_name") or nom
        pays = info.get("country") or "Unknown"
        devise = info.get("currency") or "USD"
        try:
            conn.execute(
                "INSERT INTO universe (ticker, nom, pays, indice_source, devise, "
                "priorite, nom_entreprise) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ticker, nom, pays, SOURCE_LABEL, devise, priorite, nom_entreprise),
            )
            conn.commit()
            applied.append(row)
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            logger.warning("%s: deja present (%s), ignore.", ticker, exc)
        except sqlite3.Error as exc:
            conn.rollback()
            logger.error("%s: insertion echouee (%s).", ticker, exc)
    return applied


def print_report(tracked, verified, unresolved, applied=None):
    print("\n" + "=" * 100)
    print(f"DEJA SUIVIES DANS UNIVERSE ({len(tracked)})")
    print("=" * 100)
    for r in tracked:
        print(f"  {r['ticker']:<10} {r['name']}")

    print("\n" + "=" * 100)
    print("VERIFIEES ET " + ("AJOUTEES" if applied else "A AJOUTER") +
          f" A UNIVERSE ({len(verified)})")
    print("=" * 100)
    for r in verified:
        note = f" (CSV proposait {r['csv_ticker_rejected']})" if r.get("csv_ticker_rejected") else ""
        print(f"  {r['ticker']:<10} {r['name']:<25} -> {r['real_name']}{note}")

    print("\n" + "=" * 100)
    print(f"NON RESOLUES ({len(unresolved)})")
    print("=" * 100)
    for r in unresolved:
        print(f"  {r['ticker']:<10} {r['name']:<25} {r['reason']}")

    print("\n" + "=" * 100)
    print(f"Total: {len(tracked)} deja suivies | {len(verified)} verifiees | "
          f"{len(unresolved)} non resolues")
    print("=" * 100 + "\n")


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Ensure Knowledge Graph related companies are tracked in universe.")
    p.add_argument("--dry-run", action="store_true", help="Report only, no database changes.")
    p.add_argument("--priorite", default=DEFAULT_PRIORITE,
                   choices=["haute", "moyenne", "basse"])
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])

    if not os.path.exists(DB_PATH):
        logger.error("Database not found at %s.", DB_PATH)
        return 1

    conn = sqlite3.connect(DB_PATH)
    tracked, verified, unresolved = diagnose(conn)

    applied = None
    if not args.dry_run:
        applied = apply_additions(conn, verified, priorite=args.priorite)
        logger.info("Ajoutees a universe: %d/%d.", len(applied), len(verified))

    print_report(tracked, verified, unresolved, applied=applied)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
