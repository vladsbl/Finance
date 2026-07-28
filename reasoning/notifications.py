#!/usr/bin/env python3
"""Module 11 -- Telegram alerts for today's strongest opportunities.

Separate from reasoning/daily_summary.py's "Resume du jour" (top 3 signals,
regardless of how high or low they score): this module scans ALL of today's
`opportunites` and alerts on any ticker whose score_ajuste (see
reasoning/daily_summary.py's compute_adjusted_score: score_global *
confiance/100 -- reused as-is, not reimplemented) clears MIN_SCORE_AJUSTE.
That could be zero tickers on a quiet day, or several on a strong one -- it
is deliberately NOT capped at 3. No qualifying ticker means no message at
all: a push notification is only useful if it is rare enough to trust.

Each qualifying ticker reuses build_signal() (same construction as the
dashboard's "Analyse d'une action" and daily_summary.py's own top-N) so the
alert is never a second, drifting implementation of "what a signal is". The
argued text is read from the shared daily_summary_arguments cache
(load_cached_argument) if another consumer already generated it today --
this module NEVER calls Groq itself (no LLM quota of its own): if no cached
text exists yet, the alert falls back to the same structured checkmark
(check mark/cross/dot) `explication` string opportunity_scoring.py already
computed, exactly like daily_summary.py's own CLI/dashboard fallback.

Sends via Telegram's plain HTTP API (requests), reading TELEGRAM_BOT_TOKEN /
TELEGRAM_CHAT_ID from the environment (.env, python-dotenv) -- never
hardcoded, never logged. Any send failure (missing credentials, network
error, bad token, Telegram-side rejection) is caught and logged; it must
never raise into run_daily.py and abort the pipeline.

Usage:
    python reasoning/notifications.py                  # real send, threshold 70
    python reasoning/notifications.py --dry-run         # build + print, no send
    python reasoning/notifications.py --min-score 50    # lower bar (testing)
"""

import argparse
import logging
import os
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
DATA_DIR = os.path.dirname(DB_PATH)

# CA bundle before any requests.post() call to Telegram -- same pattern as
# every other module here that makes an HTTPS call (analyze_news.py,
# daily_summary.py, generate_relations.py). Explicit here rather than relying
# on daily_summary's own import doing it as a side effect, so this module
# also works correctly if ever imported/run on its own.
from ingestion.ssl_utils import configure_ca_bundle  # noqa: E402

configure_ca_bundle(DATA_DIR)

from graph.build_graph import build_graph, load_relations  # noqa: E402
from reasoning.daily_summary import (  # noqa: E402
    build_signal, compute_adjusted_score, load_cached_argument,
    load_opportunites_multi, resolve_data_dates_by_priority, staleness_summary,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("notifications")

# --- Configuration -----------------------------------------------------------

# Deliberately independent of daily_summary.py's MIN_CONFIDENCE/TOP_N: this
# is an alert threshold on the ADJUSTED score itself, not a "top 3" pick --
# score_ajuste = score_global * confiance/100 already blends quality and
# trustworthiness in one number (see compute_adjusted_score's own
# docstring), so a single cutoff on it is enough here.
MIN_SCORE_AJUSTE = 70.0

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_TIMEOUT_S = 15


# --- Selection ---------------------------------------------------------------

def find_notable_opportunities(conn, dates_by_priority, min_score_ajuste=MIN_SCORE_AJUSTE):
    """All `opportunites` rows -- across EVERY priorite tier, each at its
    own latest date_calcul (see reasoning.daily_summary.
    resolve_data_dates_by_priority) -- whose score_ajuste clears
    `min_score_ajuste`, sorted descending. Returns [] if none do (or if
    dates_by_priority is empty/None) -- never raises. Scanning every tier
    independently (not a single global-max date) matters here specifically:
    an alert meant to catch "anything notable across the whole universe"
    must not silently go blind to "haute"/"moyenne" the moment "basse"
    happens to be refreshed more recently (see
    resolve_data_date's docstring for the bug this replaced)."""
    if not dates_by_priority:
        return []
    rows = load_opportunites_multi(conn, dates_by_priority)
    notable = [
        r for r in rows
        if r["confiance"] is not None
        and compute_adjusted_score(r["score_global"], r["confiance"]) >= min_score_ajuste
    ]
    notable.sort(key=lambda r: compute_adjusted_score(r["score_global"], r["confiance"]),
                 reverse=True)
    return notable


def build_notification_signals(conn, rows):
    """build_signal() for each row, reusing the SAME construction as
    dashboard/app.py's per-ticker AI section and daily_summary.py's own
    top-N -- never a second, drifting definition of a "signal". Attaches a
    cached argued text if one already exists for today (never generates a
    new one -- this module has no Groq quota of its own)."""
    relations = load_relations(conn)
    graph = build_graph(relations)
    today = data_date_today_str()
    signals = []
    for row in rows:
        signal = build_signal(conn, row, graph, relations)
        signal["texte_argumente"] = load_cached_argument(conn, today, row["ticker"])
        signals.append(signal)
    return signals


def data_date_today_str():
    from datetime import date
    return date.today().isoformat()


# --- Message formatting -------------------------------------------------------

def _fmt_pct(value):
    return f"{value:.0%}" if value is not None else "n/a"


def format_signal_block(signal):
    """One ticker's Telegram block: header + score line + risk line + either
    the cached argued paragraph or the structured checkmark/cross/dot
    `explication` fallback -- same fallback discipline as
    daily_summary.py's own CLI/dashboard rendering, never a blank block."""
    lines = [
        f"*{signal['ticker']}* ({signal['nom_affiche']})",
        f"Score ajuste : *{signal['score_ajuste']:.1f}*/100 "
        f"(brut {signal['score_global']:.1f} x confiance {signal['confiance']:.0f}%)",
        f"Risque : {signal['risque']}"
        + (" (composantes en contradiction)" if signal["conflit_composantes"] else "")
        + (f", volatilite {_fmt_pct(signal['volatilite'])}" if signal["volatilite"] else ""),
    ]
    if signal.get("texte_argumente"):
        lines.append(signal["texte_argumente"])
    else:
        lines.append(signal["explication"])
    watch = signal.get("entreprises_a_surveiller")
    if watch:
        parts = [f"{rtype}: {', '.join(names)}" for rtype, names in watch.items()]
        lines.append("Entreprises liees : " + " | ".join(parts))
    return "\n".join(lines)


def format_message(signals, dates_by_priority):
    distinct_dates = set(dates_by_priority.values()) if dates_by_priority else set()
    if len(distinct_dates) == 1:
        header = f"*Opportunites du jour* -- {next(iter(distinct_dates))}"
    else:
        header = "*Opportunites du jour*"
    note = staleness_summary(dates_by_priority)
    parts = [header]
    if note:
        parts.append(f"_{note}_")
    parts.append(f"{len(signals)} opportunite(s) au-dessus du seuil.")
    parts.append("")
    for signal in signals:
        parts.append(format_signal_block(signal))
        parts.append("")
    return "\n".join(parts).strip()


# --- Telegram send -------------------------------------------------------------

def send_telegram_message(text, token=None, chat_id=None):
    """POST to Telegram's sendMessage endpoint. Returns True on confirmed
    delivery, False on ANY failure (missing credentials, network error, bad
    token, chat not found, markdown parse error) -- never raises, so a
    notification failure can never crash the caller (run_daily.py)."""
    token = token or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID absent. Notification non envoyee.")
        return False

    import requests
    url = TELEGRAM_API_URL.format(token=token)
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, data=payload, timeout=TELEGRAM_TIMEOUT_S)
        data = resp.json() if resp.content else {}
    except (requests.RequestException, ValueError) as exc:
        logger.error("Envoi Telegram echoue (reseau/reponse invalide): %s", exc)
        return False

    if resp.status_code == 200 and data.get("ok"):
        return True

    # A markdown entity-parsing error is the one failure mode worth a single
    # plain-text retry (a company name containing an unbalanced '*'/'_' must
    # never mean the whole alert is silently lost) -- anything else (bad
    # token, chat not found, network) is not retried.
    description = str(data.get("description", ""))
    if "can't parse entities" in description.lower():
        logger.warning("Echec de parsing Markdown Telegram (%s). Nouvelle tentative en texte brut.",
                        description)
        try:
            resp = requests.post(url, data={"chat_id": chat_id, "text": text},
                                 timeout=TELEGRAM_TIMEOUT_S)
            data = resp.json() if resp.content else {}
        except (requests.RequestException, ValueError) as exc:
            logger.error("Nouvelle tentative (texte brut) echouee: %s", exc)
            return False
        if resp.status_code == 200 and data.get("ok"):
            return True

    logger.error("Telegram a rejete le message (statut %s): %s", resp.status_code, data)
    return False


# --- Orchestration ------------------------------------------------------------

def run_notifications(conn, min_score_ajuste=MIN_SCORE_AJUSTE, dry_run=False,
                       date_override=None):
    """Full pipeline: resolve today's data, find notable opportunities,
    build signals, format, send (unless dry_run). Returns (sent_bool,
    message_or_None, n_notable) -- sent_bool is False (not an error) when
    there was simply nothing to report."""
    dates_by_priority = resolve_data_dates_by_priority(conn, date_override)
    if not dates_by_priority:
        logger.info("Aucune donnee dans opportunites -- rien a notifier.")
        return False, None, 0

    rows = find_notable_opportunities(conn, dates_by_priority, min_score_ajuste)
    if not rows:
        logger.info("Aucune opportunite ne depasse le seuil (%.0f) pour %s -- "
                    "aucune notification envoyee (comportement attendu, pas une erreur).",
                    min_score_ajuste, dates_by_priority)
        return False, None, 0

    signals = build_notification_signals(conn, rows)
    message = format_message(signals, dates_by_priority)

    if dry_run:
        logger.info("[DRY-RUN] %d opportunite(s) notable(s) pour %s. Message non envoye.",
                    len(signals), dates_by_priority)
        return False, message, len(signals)

    ok = send_telegram_message(message)
    if ok:
        logger.info("Notification Telegram envoyee (%d opportunite(s), seuil %.0f, dates %s).",
                    len(signals), min_score_ajuste, dates_by_priority)
    else:
        logger.warning("Notification Telegram NON envoyee (echec -- voir logs ci-dessus). "
                        "Le pipeline continue normalement.")
    return ok, message, len(signals)


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Send a Telegram alert for today's opportunities above a score threshold.")
    p.add_argument("--min-score", type=float, default=MIN_SCORE_AJUSTE,
                   help=f"Minimum score_ajuste to alert on (default {MIN_SCORE_AJUSTE:.0f}).")
    p.add_argument("--dry-run", action="store_true",
                   help="Build and print the message; never sends to Telegram.")
    p.add_argument("--date", default=None, help="Override data_date (YYYY-MM-DD), for testing.")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])

    from dotenv import load_dotenv
    load_dotenv()

    if not os.path.exists(DB_PATH):
        logger.error("Database not found at %s.", DB_PATH)
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _, message, n = run_notifications(
        conn, min_score_ajuste=args.min_score, dry_run=args.dry_run,
        date_override=args.date,
    )
    conn.close()

    if message:
        print("\n" + "=" * 70)
        print(message)
        print("=" * 70 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
