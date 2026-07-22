#!/usr/bin/env python3
"""Daily Summary -- the strongest investment signals detected TODAY.

This is an explicit, opinionated advisory output (not a neutral alert feed):
the project is a personal financial-advisory tool, so each signal is
presented with its supporting arguments, an explicit risk level, and a "today
only" horizon -- this is about what to look at right now, not a forecast for
next week or next month.

Selection logic
----------------
Candidates come from today's rows in `opportunites` (date_calcul = today).
Ranking uses an ADJUSTED score, not the raw score_global, because a high raw
score built on a single, unverified signal should never outrank a lower but
well-supported one:

    score_ajuste = score_global * (confiance / 100)

Rationale: `confiance` (see reasoning/opportunity_scoring.py) already measures
exactly "how much of this score can we trust" -- it is 100 when all four
components (price/valuation, technical, fresh news, real fundamentals) are
present, and lower when some are missing or stale. Multiplying makes confiance a direct, transparent
discount on the raw score: a ticker at 80 with 33% confiance scores 26.6,
well below a ticker at 73 with 83% confiance (60.8) -- exactly the ordering a
person reading a daily "top picks" list would expect and trust. It is a single
line, easy to retune later (e.g. sqrt(confiance/100) to soften the penalty) if
the weighting ever needs adjusting.

Tickers below MIN_CONFIDENCE are excluded outright regardless of score --
quality over quantity, per the project's own guidance: showing 0, 1 or 2
signals is preferable to forcing a 3rd pick nobody should act on.

Risk level is derived (not scored by an LLM) from three signals:
  * annualised volatility (same bands as analysis/fundamental/score.py's
    score_volatility: >40% high, <20% low)
  * confiance itself (a signal that isn't fully backed carries more risk)
  * coherence between the three "structural" components -- price/valuation,
    technical, and real fundamentals (news is deliberately excluded from this
    check: it is a same-day opinion signal, not a structural read on the
    business) -- if any one is clearly strong while another is clearly weak,
    that contradiction raises risk (e.g. attractive fundamentals but selling
    technicals, or vice versa, is a genuinely less clear-cut situation)

"Companies to watch" queries the Knowledge Graph (graph/build_graph.py,
networkx) for each retained ticker's direct relations (competitor/supplier/
client/partner); a ticker absent from the graph simply gets no such section,
never an error.

Argued text (LLM)
------------------
On top of the structured data above, each retained signal gets a short
written paragraph from Groq (same model/retry/backoff pattern as
reasoning/analyze_news.py) explaining WHY it matters today, grounded strictly
in the data already computed (never invents facts). Capped at
DAILY_LLM_CALL_LIMIT calls/day (its own counter, `llm_usage_summary`, kept
separate from analyze_news.py's `llm_usage` so the two quotas never interfere).
Any failure (missing API key, network error, rate limit exhaustion) is
swallowed: the signal simply keeps its structured presentation, never a crash.

Usage:
    python reasoning/daily_summary.py
"""

import argparse
import logging
import os
import sqlite3
import sys
import time
from datetime import date

# The report uses check/cross marks (✓/✗/•) inherited from opportunity_scoring's
# explication text; Windows consoles often default to cp1252, which can't
# encode them. Force UTF-8 stdout so the CLI report never crashes on print().
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from analysis.price_valuation_scores_universe import compute_volatility  # noqa: E402
from graph.build_graph import build_graph, direct_relations, load_relations  # noqa: E402

DB_PATH = os.path.join(REPO_ROOT, "data", "marketdb.db")
DATA_DIR = os.path.dirname(DB_PATH)

# CA bundle before importing anything httpx-based (groq uses httpx). See
# ingestion/ssl_utils.py -- same pattern as analyze_news.py / fetch_company_names.py.
from ingestion.ssl_utils import configure_ca_bundle  # noqa: E402

CA_BUNDLE = configure_ca_bundle(DATA_DIR)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("daily_summary")

# --- Configuration -----------------------------------------------------------

TOP_N = 3
# Quality gate: a ticker below this confiance is never shown, however high
# its raw score_global -- fewer, trustworthy signals beat a forced 3rd pick.
MIN_CONFIDENCE = 50.0

# Same thresholds as reasoning/opportunity_scoring.py's own component labels.
THRESH_SOLID = 60.0
THRESH_FAIBLE = 40.0

# Same volatility bands as analysis/fundamental/score.py's score_volatility().
VOL_HIGH = 0.40
VOL_LOW = 0.20

HORIZON_LABEL = ("Signal du jour - a surveiller aujourd'hui, "
                "pas une prevision a moyen/long terme.")


# --- Scoring / risk ------------------------------------------------------------

def compute_adjusted_score(score_global, confiance):
    """score_ajuste = score_global * (confiance/100). See module docstring."""
    return round(score_global * (confiance / 100.0), 2)


def _classify(score):
    """haute / neutre / basse against the same bands opportunity_scoring.py
    uses for its own ✓/•/✗ explanation labels, or None if unavailable."""
    if score is None:
        return None
    if score >= THRESH_SOLID:
        return "haute"
    if score < THRESH_FAIBLE:
        return "basse"
    return "neutre"


def has_conflict(*scores):
    """True when any two of the given "structural" scores clearly disagree
    (one solid, another weak) -- a genuinely less clear-cut situation, not
    just a small gap. Pass price/valuation, technique and fondamental_reel;
    news is deliberately excluded (see module docstring)."""
    classified = {_classify(s) for s in scores if s is not None}
    return "haute" in classified and "basse" in classified


def compute_risk(volatility, confiance, conflict):
    """Faible / Modere / Eleve from a simple, transparent point system."""
    points = 0
    if volatility is not None:
        if volatility > VOL_HIGH:
            points += 2
        elif volatility > VOL_LOW:
            points += 0  # normal range, no penalty
        else:
            points += 0
    if confiance < 70.0:
        points += 1
    if conflict:
        points += 2

    if points <= 1:
        return "Faible"
    if points <= 3:
        return "Modere"
    return "Eleve"


# --- Data access -----------------------------------------------------------

def load_today_opportunites(conn, today):
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM opportunites WHERE date_calcul = ? AND score_global IS NOT NULL",
        (today,),
    ).fetchall()


def load_price_series(conn, ticker):
    rows = conn.execute(
        "SELECT close FROM price_history WHERE ticker = ? AND close IS NOT NULL "
        "ORDER BY date",
        (ticker,),
    ).fetchall()
    return [r[0] for r in rows]


def load_display_name(conn, ticker):
    """nom_entreprise (yfinance longName/shortName) in priority, falling back
    to `nom` (scraped from the index constituent page at universe-build time)
    if nom_entreprise is null/empty, falling back to the ticker itself if
    neither is usable. nom_entreprise practically never ends up empty (it
    already falls back to the ticker at fetch time -- see
    universe/fetch_company_names.py) so NULLIF(..., ticker) treats "fell back
    to the bare ticker" the same as "no name", letting `nom` take over."""
    row = conn.execute(
        "SELECT COALESCE(NULLIF(nom_entreprise, ticker), NULLIF(nom, ''), ticker) "
        "FROM universe WHERE ticker = ?",
        (ticker,),
    ).fetchone()
    return row[0] if row else ticker


def companies_to_watch(graph, relations, ticker):
    """Direct relations grouped by type, or None if the ticker isn't in the
    Knowledge Graph at all (never an error in that case)."""
    if not graph.has_node(ticker):
        return None
    grouped = direct_relations(relations, ticker)
    return grouped or None


# --- Argued text (Groq LLM) --------------------------------------------------

GROQ_MODEL = "llama-3.3-70b-versatile"
MAX_RETRIES = 5      # for 429 rate-limit backoff, same as analyze_news.py
BACKOFF_BASE = 2.0   # seconds: 2, 4, 8, 16, 32

# At most TOP_N signals/day, so this is naturally capped -- kept as an
# explicit constant (rather than reusing TOP_N directly) so the quota is
# self-documenting and easy to retune independently of the ranking size.
DAILY_LLM_CALL_LIMIT = 3

CREATE_SUMMARY_USAGE_SQL = """
CREATE TABLE IF NOT EXISTS llm_usage_summary (
    day   TEXT PRIMARY KEY,
    calls INTEGER NOT NULL DEFAULT 0
);
"""

# Persisted so the same day's argued text is reused by every consumer (CLI
# run, dashboard page load, dashboard refresh) instead of being regenerated
# -- without this, whichever process happens to run first would burn the
# whole day's quota and every other consumer would silently see the
# structured-only fallback for the rest of the day, even though the text had
# already been produced once.
CREATE_ARGUMENTS_SQL = """
CREATE TABLE IF NOT EXISTS daily_summary_arguments (
    day        TEXT NOT NULL,
    ticker     TEXT NOT NULL,
    texte      TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (day, ticker)
);
"""

SYSTEM_PROMPT_ARGUMENT = (
    "Tu es un analyste financier qui redige un court paragraphe en francais "
    "pour expliquer POURQUOI un signal d'investissement merite l'attention "
    "AUJOURD'HUI. Regle absolue : reste strictement fidele aux donnees "
    "fournies -- n'invente aucun fait, aucun chiffre, aucune information qui "
    "n'y figure pas explicitement. Appuie-toi uniquement sur les scores, les "
    "explications, le niveau de risque et les entreprises liees fournis. "
    "Style : clair, concis, professionnel, 3 a 5 phrases. N'enumere pas "
    "mecaniquement les chiffres deja donnes : explique ce qu'ils signifient "
    "pour un investisseur. Reponds uniquement avec le paragraphe, sans titre "
    "ni introduction ni markdown."
)


def _is_rate_limit(exc):
    status = getattr(exc, "status_code", None)
    return status == 429 or "429" in str(exc) or "rate limit" in str(exc).lower()


def get_summary_usage(conn, day):
    row = conn.execute(
        "SELECT calls FROM llm_usage_summary WHERE day = ?", (day,)
    ).fetchone()
    return row[0] if row else 0


def bump_summary_usage(conn, day):
    conn.execute(
        "INSERT INTO llm_usage_summary (day, calls) VALUES (?, 1) "
        "ON CONFLICT(day) DO UPDATE SET calls = calls + 1",
        (day,),
    )
    conn.commit()


def load_cached_argument(conn, day, ticker):
    row = conn.execute(
        "SELECT texte FROM daily_summary_arguments WHERE day = ? AND ticker = ?",
        (day, ticker),
    ).fetchone()
    return row[0] if row else None


def save_argument(conn, day, ticker, texte):
    conn.execute(
        "INSERT INTO daily_summary_arguments (day, ticker, texte) VALUES (?, ?, ?) "
        "ON CONFLICT(day, ticker) DO UPDATE SET texte = excluded.texte",
        (day, ticker, texte),
    )
    conn.commit()


def build_argument_prompt(signal):
    lines = [
        f"Entreprise : {signal['nom_affiche']} ({signal['ticker']})",
        f"Score global : {signal['score_global']:.1f}/100 "
        f"(confiance {signal['confiance']:.0f}%, score ajuste {signal['score_ajuste']:.1f})",
        f"Detail des composantes : {signal['explication']}",
        f"Niveau de risque retenu : {signal['risque']}",
    ]
    if signal.get("conflit_composantes"):
        lines.append(
            "Attention : contradiction detectee entre composantes structurelles "
            "(prix/valorisation, technique, fondamental reel)."
        )
    if signal.get("volatilite") is not None:
        lines.append(f"Volatilite annualisee : {signal['volatilite']:.0%}")
    watch = signal.get("entreprises_a_surveiller")
    if watch:
        parts = [f"{rtype}: {', '.join(names)}" for rtype, names in watch.items()]
        lines.append("Entreprises liees (graphe de connaissances) : " + " | ".join(parts))
    lines.append(
        "Redige le paragraphe explicatif demande, uniquement a partir des "
        "elements ci-dessus."
    )
    return "\n".join(lines)


def generate_argued_text(client, signal):
    completion = client.chat.completions.create(
        model=GROQ_MODEL,
        temperature=0.4,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT_ARGUMENT},
            {"role": "user", "content": build_argument_prompt(signal)},
        ],
    )
    text = completion.choices[0].message.content
    return text.strip() if text else None


def generate_with_retry(client, signal):
    """generate_argued_text with exponential backoff on 429 rate limits."""
    for attempt in range(MAX_RETRIES):
        try:
            return generate_argued_text(client, signal)
        except Exception as exc:  # noqa: BLE001
            if _is_rate_limit(exc) and attempt < MAX_RETRIES - 1:
                wait = BACKOFF_BASE * (2 ** attempt)
                logger.warning("Rate limit (429). Backoff %.0fs (try %d/%d)...",
                               wait, attempt + 1, MAX_RETRIES)
                time.sleep(wait)
                continue
            raise
    return None


def add_argued_texts(conn, signals):
    """Best-effort enrichment: sets signal["texte_argumente"] for each signal,
    reusing a cached text from `daily_summary_arguments` when today's text for
    that ticker was already generated (by an earlier CLI run or dashboard
    load), and generating fresh ones via Groq for the rest, up to
    DAILY_LLM_CALL_LIMIT NEW calls/day. Never raises -- any failure (missing
    key, no network, quota exhausted, API error) just leaves the affected
    signal(s) at texte_argumente=None, and callers (CLI print, dashboard)
    fall back to the pre-existing structured-only presentation."""
    for s in signals:
        s.setdefault("texte_argumente", None)

    if not signals:
        return

    # Dashboard page tests (tests/test_dashboard_pages.py) exercise this exact
    # code path against the real production database via AppTest -- without
    # this guard, every test run would burn real Groq quota and require
    # network access. PYTEST_CURRENT_TEST is set automatically by pytest for
    # the duration of each test.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return

    today_real = date.today().isoformat()

    try:
        conn.execute(CREATE_SUMMARY_USAGE_SQL)
        conn.execute(CREATE_ARGUMENTS_SQL)
        conn.commit()
    except sqlite3.Error as exc:
        logger.warning("Tables llm_usage_summary/daily_summary_arguments "
                        "indisponibles (%s). Repli sur presentation structuree.", exc)
        return

    pending = []
    for s in signals:
        cached = load_cached_argument(conn, today_real, s["ticker"])
        if cached:
            s["texte_argumente"] = cached
        else:
            pending.append(s)

    if not pending:
        return

    used = get_summary_usage(conn, today_real)
    remaining = max(0, DAILY_LLM_CALL_LIMIT - used)
    if remaining <= 0:
        logger.info("Quota LLM resume (%d/jour) deja atteint (%d utilises). "
                     "Repli sur presentation structuree pour les tickers restants.",
                     DAILY_LLM_CALL_LIMIT, used)
        return

    from dotenv import load_dotenv
    load_dotenv()
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        logger.warning("GROQ_API_KEY absent. Repli sur presentation structuree.")
        return

    try:
        import httpx
        from groq import Groq
        http_client = httpx.Client(verify=CA_BUNDLE) if CA_BUNDLE else None
        client = Groq(api_key=api_key, http_client=http_client)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Client Groq indisponible (%s). "
                        "Repli sur presentation structuree.", exc)
        return

    for s in pending[:remaining]:
        try:
            text = generate_with_retry(client, s)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "%s: generation du texte argumente echouee (%s). "
                "Repli sur presentation structuree pour ce ticker.",
                s["ticker"], exc,
            )
            continue
        if not text:
            continue
        s["texte_argumente"] = text
        save_argument(conn, today_real, s["ticker"], text)
        bump_summary_usage(conn, today_real)


# --- Orchestration ---------------------------------------------------------

def build_daily_summary(conn, today=None):
    """Return (signals, today, n_candidates). ``signals`` has at most TOP_N
    entries, fewer if not enough tickers clear MIN_CONFIDENCE."""
    today = today or date.today().isoformat()

    rows = load_today_opportunites(conn, today)
    eligible = [r for r in rows if r["confiance"] is not None and r["confiance"] >= MIN_CONFIDENCE]

    ranked = sorted(
        eligible,
        key=lambda r: compute_adjusted_score(r["score_global"], r["confiance"]),
        reverse=True,
    )
    top = ranked[:TOP_N]

    relations = load_relations(conn)
    graph = build_graph(relations)

    signals = []
    for r in top:
        closes = load_price_series(conn, r["ticker"])
        volatility = compute_volatility(closes) if closes else None
        conflict = has_conflict(
            r["score_prix_valorisation"], r["score_technique"], r["score_fondamental_reel"]
        )
        risk = compute_risk(volatility, r["confiance"], conflict)
        watch = companies_to_watch(graph, relations, r["ticker"])
        nom_affiche = load_display_name(conn, r["ticker"])

        signals.append({
            "ticker": r["ticker"],
            "nom_affiche": nom_affiche,
            "score_global": r["score_global"],
            "confiance": r["confiance"],
            "score_ajuste": compute_adjusted_score(r["score_global"], r["confiance"]),
            "score_prix_valorisation": r["score_prix_valorisation"],
            "score_technique": r["score_technique"],
            "score_news": r["score_news"],
            "score_fondamental_reel": r["score_fondamental_reel"],
            "explication": r["explication"],
            "risque": risk,
            "conflit_composantes": conflict,
            "volatilite": volatility,
            "horizon": HORIZON_LABEL,
            "entreprises_a_surveiller": watch,
        })

    return signals, today, len(eligible)


# --- CLI ---------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(description="Build today's daily investment summary.")
    p.add_argument("--date", default=None, help="Override date_calcul (YYYY-MM-DD), for testing.")
    return p.parse_args(argv)


def _fmt_pct(value):
    return f"{value:.0%}" if value is not None else "n/a"


def print_summary(signals, today, n_candidates):
    print("\n" + "=" * 78)
    print(f"RESUME DU JOUR - {today}")
    print("=" * 78)
    if not signals:
        print(f"Aucun signal ne depasse le seuil de confiance minimal "
              f"({MIN_CONFIDENCE:.0f}%) parmi {n_candidates} candidat(s) eligible(s).")
        print("=" * 78 + "\n")
        return

    print(f"{len(signals)} signal(aux) retenu(s) sur {n_candidates} candidat(s) eligibles "
          f"(confiance >= {MIN_CONFIDENCE:.0f}%).\n")

    for rank, s in enumerate(signals, start=1):
        print(f"#{rank} {s['ticker']} ({s['nom_affiche']}) - score ajuste {s['score_ajuste']:.1f} "
              f"(brut {s['score_global']:.1f} x confiance {s['confiance']:.0f}%)")
        if s.get("texte_argumente"):
            print(f"    {s['texte_argumente']}")
            print()
        print(f"    Risque: {s['risque']}" +
              (" (composantes structurelles en contradiction)" if s["conflit_composantes"] else "") +
              (f" - volatilite annualisee {_fmt_pct(s['volatilite'])}" if s["volatilite"] else ""))
        print(f"    Horizon: {s['horizon']}")
        print(f"    Arguments: {s['explication']}")
        if s["entreprises_a_surveiller"]:
            parts = [f"{rtype}: {', '.join(names)}"
                    for rtype, names in s["entreprises_a_surveiller"].items()]
            print(f"    Entreprises a surveiller: {' | '.join(parts)}")
        print()
    print("=" * 78 + "\n")


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])

    if not os.path.exists(DB_PATH):
        logger.error("Database not found at %s.", DB_PATH)
        return 1

    conn = sqlite3.connect(DB_PATH)
    signals, today, n_candidates = build_daily_summary(conn, today=args.date)
    add_argued_texts(conn, signals)
    conn.close()

    print_summary(signals, today, n_candidates)
    logger.info("Resume genere pour %s : %d signal(aux) (sur %d candidats eligibles).",
                today, len(signals), n_candidates)
    return 0


if __name__ == "__main__":
    sys.exit(main())
