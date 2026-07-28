#!/usr/bin/env python3
"""Daily Summary -- the strongest investment signals detected TODAY.

This is an explicit, opinionated advisory output (not a neutral alert feed):
the project is a personal financial-advisory tool, so each signal is
presented with its supporting arguments, an explicit risk level, and a "today
only" horizon -- this is about what to look at right now, not a forecast for
next week or next month.

Selection logic
----------------
Candidates come from the LATEST date_calcul actually present in
`opportunites` (see resolve_data_date) -- not necessarily today's calendar
date. `opportunites` is only as fresh as the last
reasoning/opportunity_scoring.py run; filtering strictly on date.today()
used to silently return zero candidates whenever a day was skipped, even
though a fully-computed snapshot existed. The date actually used is always
returned alongside the signals and must be surfaced by callers (see
staleness_note) so stale data is visible, never silent. Ranking uses an
ADJUSTED score, not the raw score_global, because a high raw
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

def load_opportunites_for_date(conn, data_date):
    """opportunites rows for one exact date_calcul (with a real score), ACROSS
    every priorite tier at once. Renamed from load_today_opportunites: the
    date passed in is not necessarily today's calendar date (see
    resolve_data_date) -- the old name implied a literalness that no longer
    holds. Superseded, for cross-tier selection, by load_opportunites_multi
    below -- see resolve_data_date's docstring for why a single flat date
    silently drops any tier recomputed less recently than another."""
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM opportunites WHERE date_calcul = ? AND score_global IS NOT NULL",
        (data_date,),
    ).fetchall()


def load_opportunites_multi(conn, dates_by_priority):
    """opportunites rows across ALL priorite tiers, each restricted to its
    OWN latest date_calcul (see resolve_data_dates_by_priority) -- the
    combined replacement for load_opportunites_for_date's single flat date
    whenever candidates must be drawn from the whole universe, not just
    whichever tier happens to hold the table's global MAX(date_calcul)."""
    conn.row_factory = sqlite3.Row
    rows = []
    for priorite, data_date in dates_by_priority.items():
        rows.extend(conn.execute(
            "SELECT o.* FROM opportunites o JOIN universe u ON u.ticker = o.ticker "
            "WHERE u.priorite = ? AND o.date_calcul = ? AND o.score_global IS NOT NULL",
            (priorite, data_date),
        ).fetchall())
    return rows


def load_opportunite_for_ticker(conn, ticker):
    """The single latest opportunites row for one specific ticker (any
    date_calcul, most recent first), or None if it has never been scored by
    reasoning/opportunity_scoring.py, or was scored but every component
    failed (score_global IS NULL). Used by dashboard/app.py's "Analyse d'une
    action" page to build an on-demand build_signal() for ANY ticker, not
    just the tickers in today's top N."""
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM opportunites WHERE ticker = ? AND score_global IS NOT NULL "
        "ORDER BY date_calcul DESC LIMIT 1",
        (ticker,),
    ).fetchone()


def resolve_data_date(conn, explicit_date):
    """The single date_calcul to use across the WHOLE `opportunites` table
    at once: `explicit_date` if given, otherwise the latest date_calcul
    present ANYWHERE in the table, regardless of which universe.priorite
    tier produced it.

    Superseded, for cross-tier selection (Resume du jour, notifications),
    by resolve_data_dates_by_priority() below: once one priorite tier
    (e.g. "basse") starts being recomputed on its own, more frequent
    cadence than the others, this single global MAX(date_calcul) silently
    "wins" for that tier and hides every other tier's rows from that day
    onward -- even though their own last snapshot (just a few days older)
    is still perfectly valid. Observed for real: "haute"/"moyenne" hadn't
    been recomputed in 5 days while "basse" had just been refreshed, and
    every "today's opportunities" consumer ended up drawing 100% of its
    candidates from "basse" alone, silently. Kept here only for callers
    that genuinely want one flat date across the whole table (e.g. a
    --date override applied uniformly for testing a specific past
    snapshot)."""
    if explicit_date:
        return explicit_date
    row = conn.execute("SELECT MAX(date_calcul) FROM opportunites").fetchone()
    return row[0] if row else None


def resolve_data_dates_by_priority(conn, explicit_date=None):
    """{priorite: date_calcul} -- the latest date_calcul available for EACH
    universe.priorite tier (haute/moyenne/basse) INDEPENDENTLY, instead of
    one date across the whole table (see resolve_data_date's docstring for
    why that silently hides whichever tier was recomputed less recently).
    This is the primary building block for any "today's best across the
    whole universe" selection (build_daily_summary,
    reasoning/notifications.py): each tier contributes its own rows at its
    own freshest date, so a stale tier still participates -- it is simply
    flagged as stale (see staleness_summary), never silently dropped.

    `explicit_date` (the CLI's --date override, for testing a specific past
    snapshot) applies uniformly to every tier when given."""
    if explicit_date:
        priorites = [r[0] for r in conn.execute(
            "SELECT DISTINCT priorite FROM universe WHERE priorite IS NOT NULL")]
        return {p: explicit_date for p in priorites}
    rows = conn.execute(
        "SELECT u.priorite, MAX(o.date_calcul) FROM opportunites o "
        "JOIN universe u ON u.ticker = o.ticker "
        "WHERE u.priorite IS NOT NULL "
        "GROUP BY u.priorite"
    ).fetchall()
    return {priorite: data_date for priorite, data_date in rows if data_date}


def data_age_days(data_date):
    """Whole calendar days between `data_date` (a YYYY-MM-DD date_calcul) and
    today. None if data_date is falsy or unparsable (nothing to compare)."""
    if not data_date:
        return None
    try:
        d = date.fromisoformat(data_date)
    except ValueError:
        return None
    return (date.today() - d).days


def staleness_note(data_date):
    """Human-readable staleness warning, or None when the data is from today
    (age 0) or unavailable. Surfaced in both the CLI (print_summary) and the
    dashboard so a stale `opportunites` snapshot -- e.g. the daily pipeline
    run was skipped -- is immediately visible instead of silent."""
    age = data_age_days(data_date)
    if not age:
        return None
    day_word = "jour" if age == 1 else "jours"
    return f"Donnees du {data_date}, non recalculees depuis {age} {day_word}."


def staleness_summary(dates_by_priority):
    """Human-readable freshness line across ALL priorite tiers at once: a
    single staleness_note()-style line when every tier happens to share the
    same date_calcul (nothing to distinguish), or an explicit per-tier
    breakdown when they differ -- e.g. "haute : donnees du 2026-07-22 (5j)
    | moyenne : donnees du 2026-07-22 (5j) | basse : donnees du 2026-07-27
    (0j)" -- so a stale tier is always visible, never averaged away or
    silently hidden behind a single date that happens to belong to a
    different, fresher tier."""
    if not dates_by_priority:
        return None
    distinct_dates = set(dates_by_priority.values())
    if len(distinct_dates) <= 1:
        return staleness_note(next(iter(distinct_dates), None))
    parts = []
    for priorite in sorted(dates_by_priority):
        data_date = dates_by_priority[priorite]
        age = data_age_days(data_date)
        age_txt = f"{age}j" if age is not None else "age inconnu"
        parts.append(f"{priorite} : donnees du {data_date} ({age_txt})")
    return "Fraicheur par palier -- " + " | ".join(parts)


def load_price_series(conn, ticker):
    rows = conn.execute(
        "SELECT close FROM price_history WHERE ticker = ? AND close IS NOT NULL "
        "ORDER BY date",
        (ticker,),
    ).fetchall()
    return [r[0] for r in rows]


# Trading-day lags (not calendar days) for the price variation shown
# alongside every signal -- short/medium/long enough to be readable at a
# glance without drowning the signal in a full price history chart.
PRICE_VARIATION_LAGS = {"1j": 1, "7j": 7, "30j": 30}


def compute_price_variation(conn, ticker):
    """{"prix_actuel": float, "devise": str, "variations": {"1j": pct|None,
    "7j": pct|None, "30j": pct|None}}, or None if price_history has no
    usable close for this ticker at all. Percentages are computed on the
    NATIVE-currency close series (a % change is currency-invariant, so no
    EUR conversion is needed here -- only the absolute "prix_actuel" itself
    needs converting for display, which callers do via dashboard/currency.py
    at render/prompt-build time, keeping this module currency-agnostic).
    A lag longer than the available history yields None for that specific
    window rather than a misleading calculation against too short a
    series -- never invents a number that isn't there."""
    closes = load_price_series(conn, ticker)
    if not closes:
        return None

    current = closes[-1]
    variations = {}
    for label, lag in PRICE_VARIATION_LAGS.items():
        if len(closes) <= lag or not closes[-1 - lag]:
            variations[label] = None
        else:
            base = closes[-1 - lag]
            variations[label] = (current - base) / base * 100.0

    devise_row = conn.execute(
        "SELECT devise FROM universe WHERE ticker = ?", (ticker,)
    ).fetchone()
    devise = devise_row[0] if devise_row and devise_row[0] else "USD"

    return {"prix_actuel": current, "devise": devise, "variations": variations}


def format_price_line(conn, price_info):
    """Human-readable price line with EUR conversion (display-only, same
    convention as dashboard/currency.py -- price_history itself is never
    touched or re-stored in a different currency), e.g. "172.34 EUR
    (variation 1j: +0.8%, 7j: +2.1%, 30j: -1.4%)". None if `price_info` is
    None (no price data at all for this ticker). A missing individual
    variation (too little history for that lag) is shown as "n/a", never
    silently omitted -- so the reader knows it was checked, not forgotten."""
    if price_info is None:
        return None
    # Lazy import: dashboard/currency.py has no Streamlit dependency (pure
    # sqlite3/yfinance), so this is safe to call from a CLI/reasoning
    # context, but kept local rather than a module-level import to avoid
    # reasoning/ taking a permanent hard dependency on dashboard/.
    from dashboard.currency import format_amount, get_rate_to_eur

    rate = get_rate_to_eur(conn, price_info["devise"])
    prix_fmt = format_amount(price_info["prix_actuel"], price_info["devise"], rate)

    def _fmt_var(pct):
        return f"{pct:+.1f}%" if pct is not None else "n/a"

    variations = price_info["variations"]
    var_parts = [f"{label}: {_fmt_var(variations[label])}" for label in PRICE_VARIATION_LAGS]
    return f"{prix_fmt} (variation {', '.join(var_parts)})"


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

# Two SEPARATE daily quota pools, each its own table, so browsing dozens of
# tickers/day on "Analyse d'une action" (dashboard/app.py) can never block --
# or be blocked by -- Resume du jour's own 3 signals. Both stay comfortably
# within Groq's real ceiling (~270-290 analyses/day observed, tied to the
# free tier's 100k-tokens/day limit, not a request-count limit): 3 + 10 = 13
# combined worst case, nowhere close.
DAILY_LLM_CALL_LIMIT = 3            # Resume du jour (its TOP_N signals)
TICKER_ANALYSIS_DAILY_LIMIT = 10    # "Analyse d'une action", any ticker on demand

USAGE_TABLE_SUMMARY = "llm_usage_summary"
USAGE_TABLE_TICKER_ANALYSIS = "llm_usage_ticker_analysis"

# Persisted so the same day's argued text is reused by every consumer (CLI
# run, dashboard page load, dashboard refresh) instead of being regenerated
# -- without this, whichever process happens to run first would burn the
# whole day's quota and every other consumer would silently see the
# structured-only fallback for the rest of the day, even though the text had
# already been produced once. Shared across BOTH quota pools above: a ticker
# generated via either entry point is cached under the same (day, ticker)
# key, so the two features never pay twice for the same ticker/day.
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
    "Tu es un analyste financier qui redige une analyse structuree en "
    "francais pour expliquer POURQUOI un signal d'investissement merite "
    "l'attention AUJOURD'HUI. Le lecteur n'est pas trader professionnel : "
    "reste accessible, sans jargon non explique.\n\n"
    "Regle absolue : reste strictement fidele aux donnees fournies -- "
    "n'invente aucun fait, aucun chiffre, et surtout aucun contexte "
    "macro-economique (taux d'interet, tensions geopolitiques, matieres "
    "premieres, decisions de banque centrale) qui n'apparaisse pas "
    "explicitement ci-dessous. Si aucun contexte macro/causal/news n'est "
    "fourni, n'en mentionne aucun -- dis-toi que ce ticker n'a simplement "
    "pas d'actualite macro notable en ce moment, ce n'est pas une lacune a "
    "combler par toi-meme.\n\n"
    "Structure attendue, en 3 paragraphes distincts separes par un saut de "
    "ligne. Longueur IMPERATIVE : 180 mots au total maximum (60 mots par "
    "paragraphe environ) -- sois dense et va a l'essentiel, ne developpe "
    "pas au-dela :\n"
    "1. Situation de l'entreprise : ce que les scores fournis (prix/"
    "valorisation, technique, fondamental reel, risque) signifient "
    "concretement pour quelqu'un qui envisage d'investir -- pas une "
    "enumeration mecanique des chiffres, explique ce qu'ils impliquent. Si "
    "un prix actuel et une variation recente sont fournis ci-dessous, "
    "mentionne-les naturellement dans ce paragraphe (jamais un chiffre qui "
    "n'y figure pas).\n"
    "2. Contexte macro-economique ou sectoriel -- UNIQUEMENT si une chaine "
    "causale ou une news importante est fournie ci-dessous ; sinon, limite "
    "ce paragraphe aux entreprises liees deja fournies (concurrents/"
    "fournisseurs/clients), sans inventer de contexte macro absent.\n"
    "3. Synthese : en quoi la combinaison de la situation propre a "
    "l'entreprise et du contexte (macro s'il existe, sinon sectoriel/"
    "concurrentiel) rend -- ou ne rend pas clairement -- cette action "
    "interessante AUJOURD'HUI specifiquement, pas de maniere generale.\n\n"
    "Style : phrases completes, ton professionnel mais clair, sans "
    "markdown ni titres de section visibles (les 3 paragraphes suffisent a "
    "structurer). Reponds uniquement avec le texte de l'analyse, rien "
    "d'autre."
)


def _is_rate_limit(exc):
    status = getattr(exc, "status_code", None)
    return status == 429 or "429" in str(exc) or "rate limit" in str(exc).lower()


def _create_usage_table_sql(table):
    # `table` is always one of the two hardcoded constants above (never
    # external/user input), so this f-string is not an injection risk --
    # sqlite3 has no parameter placeholder for identifiers.
    return (
        f"CREATE TABLE IF NOT EXISTS {table} ("
        f"    day   TEXT PRIMARY KEY,"
        f"    calls INTEGER NOT NULL DEFAULT 0"
        f");"
    )


def get_usage(conn, table, day):
    row = conn.execute(f"SELECT calls FROM {table} WHERE day = ?", (day,)).fetchone()
    return row[0] if row else 0


def bump_usage(conn, table, day):
    conn.execute(
        f"INSERT INTO {table} (day, calls) VALUES (?, 1) "
        f"ON CONFLICT(day) DO UPDATE SET calls = calls + 1",
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


# Same importance bar as reasoning/causal_reasoning.py's own trigger
# threshold (IMPORTANCE_THRESHOLD) -- a news item below this bar is routine,
# not the kind of thing worth surfacing as "macro context" in an argued text.
MACRO_NEWS_IMPORTANCE_THRESHOLD = 8


def load_macro_context(conn, ticker):
    """Real, pipeline-grounded macro/causal context for `ticker`, or None if
    nothing exists. This is the ONLY source build_argument_prompt() is
    allowed to draw a macro-economic angle from -- the prompt explicitly
    forbids inventing one, so a ticker with no real signal here simply gets
    no macro paragraph, never a fabricated one.

    Checked in priority order:
      1. The most recent causal reasoning chain for this ticker
         (reasoning/causal_reasoning.py, causal_chains.ticker_source) -- a
         chain is inherently macro-adjacent (it traces indirect
         consequences through the Knowledge Graph), so it outranks a bare
         news item when both exist.
      2. The most recent high-importance news analysed for this ticker
         (news_analysis.importance >= MACRO_NEWS_IMPORTANCE_THRESHOLD,
         joined to news_raw for the ticker match) -- same bar
         causal_reasoning.py itself uses to decide a news item is worth
         reasoning about at all.
    Both mechanisms run on their own limited Groq quotas, so most tickers
    on most days will legitimately have neither -- that is expected, not a
    gap to fill."""
    row = conn.execute(
        "SELECT chaine_raisonnement, confiance, created_at FROM causal_chains "
        "WHERE ticker_source = ? ORDER BY created_at DESC LIMIT 1",
        (ticker,),
    ).fetchone()
    if row:
        chaine, confiance, created_at = row
        return {
            "type": "chaine_causale",
            "texte": chaine,
            "confiance": confiance,
            "date": (created_at or "")[:10],
        }

    row = conn.execute(
        "SELECT r.title, a.sector, a.impact, a.tonalite, a.importance, a.created_at "
        "FROM news_analysis a JOIN news_raw r ON r.id = a.news_id "
        "WHERE r.ticker = ? AND a.importance >= ? "
        "ORDER BY a.created_at DESC LIMIT 1",
        (ticker, MACRO_NEWS_IMPORTANCE_THRESHOLD),
    ).fetchone()
    if row:
        title, sector, impact, tonalite, importance, created_at = row
        return {
            "type": "news_importante",
            "titre": title,
            "secteur": sector,
            "impact": impact,
            "tonalite": tonalite,
            "importance": importance,
            "date": (created_at or "")[:10],
        }
    return None


def build_argument_prompt(signal, macro_context=None, conn=None):
    lines = [
        f"Entreprise : {signal['nom_affiche']} ({signal['ticker']})",
        f"Score global : {signal['score_global']:.1f}/100 "
        f"(confiance {signal['confiance']:.0f}%, score ajuste {signal['score_ajuste']:.1f})",
        f"Detail des composantes : {signal['explication']}",
        f"Niveau de risque retenu : {signal['risque']}",
    ]
    # Real price + variation, exactly as computed/shown in the structured
    # metrics -- given here so the LLM can refer to it naturally instead of
    # guessing or omitting it; conn=None (no caller-supplied connection)
    # skips this line gracefully rather than crashing.
    if conn is not None and signal.get("prix"):
        price_line = format_price_line(conn, signal["prix"])
        if price_line:
            lines.append(f"Prix actuel et variation recente : {price_line}")
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

    if macro_context is None:
        lines.append(
            "\nAucun contexte macro-economique ou causal recent disponible "
            "pour ce ticker dans le pipeline -- n'en invente aucun, "
            "limite-toi au contexte sectoriel/concurrentiel deja fourni "
            "ci-dessus si besoin."
        )
    elif macro_context["type"] == "chaine_causale":
        lines.append(
            f"\nContexte causal disponible (chaine de raisonnement generee "
            f"le {macro_context['date']}, confiance {macro_context['confiance']:.0f}%) "
            f"-- utilise-la comme contexte macro/sectoriel reel, ne la "
            f"reformule pas mot pour mot :\n{macro_context['texte']}"
        )
    else:
        lines.append(
            f"\nNews importante recente disponible ({macro_context['date']}, "
            f"importance {macro_context['importance']}/10, tonalite "
            f"{macro_context['tonalite']}) -- utilise-la comme contexte macro/"
            f"sectoriel reel :\n"
            f"Titre : {macro_context['titre']}\n"
            f"Secteur concerne : {macro_context['secteur'] or 'non precise'}\n"
            f"Impact identifie : {macro_context['impact']}"
        )

    lines.append(
        "\nRedige l'analyse structuree en 3 paragraphes demandee, "
        "uniquement a partir des elements ci-dessus."
    )
    return "\n".join(lines)


def generate_argued_text(client, conn, signal):
    macro_context = load_macro_context(conn, signal["ticker"])
    completion = client.chat.completions.create(
        model=GROQ_MODEL,
        temperature=0.4,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT_ARGUMENT},
            {"role": "user", "content": build_argument_prompt(signal, macro_context, conn=conn)},
        ],
    )
    text = completion.choices[0].message.content
    return text.strip() if text else None


def generate_with_retry(client, conn, signal):
    """generate_argued_text with exponential backoff on 429 rate limits."""
    for attempt in range(MAX_RETRIES):
        try:
            return generate_argued_text(client, conn, signal)
        except Exception as exc:  # noqa: BLE001
            if _is_rate_limit(exc) and attempt < MAX_RETRIES - 1:
                wait = BACKOFF_BASE * (2 ** attempt)
                logger.warning("Rate limit (429). Backoff %.0fs (try %d/%d)...",
                               wait, attempt + 1, MAX_RETRIES)
                time.sleep(wait)
                continue
            raise
    return None


def add_argued_texts(conn, signals, usage_table=USAGE_TABLE_SUMMARY,
                      call_limit=DAILY_LLM_CALL_LIMIT):
    """Best-effort enrichment: sets signal["texte_argumente"] for each signal,
    reusing a cached text from `daily_summary_arguments` when today's text for
    that ticker was already generated (by an earlier CLI run or dashboard
    load, via EITHER caller below), and generating fresh ones via Groq for
    the rest, up to `call_limit` NEW calls/day drawn from `usage_table`.
    Never raises -- any failure (missing key, no network, quota exhausted,
    API error) just leaves the affected signal(s) at texte_argumente=None,
    and callers (CLI print, dashboard) fall back to the pre-existing
    structured-only presentation.

    Two distinct callers, two distinct quota pools (never mixed):
      * reasoning/daily_summary.py's own build_daily_summary() (today's
        TOP_N signals) -- defaults: USAGE_TABLE_SUMMARY, DAILY_LLM_CALL_LIMIT.
      * dashboard/app.py's "Analyse d'une action" (any ticker, on demand) --
        passes USAGE_TABLE_TICKER_ANALYSIS, TICKER_ANALYSIS_DAILY_LIMIT
        explicitly, so browsing dozens of tickers/day can never exhaust (or
        be exhausted by) Resume du jour's own 3-call/day budget.
    Both share the SAME daily_summary_arguments cache table/key (day,
    ticker): whichever caller generates a ticker's text first, the other
    reuses it for free."""
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

    # Deliberately the real calendar day, NOT build_daily_summary()'s
    # data_date: this is a rate-limit/cache window against actual wall-clock
    # days (the Groq quota resets daily regardless of how stale opportunites
    # happens to be), so it must stay tied to date.today() even when the
    # signals themselves come from an older snapshot.
    today_real = date.today().isoformat()

    try:
        conn.execute(_create_usage_table_sql(usage_table))
        conn.execute(CREATE_ARGUMENTS_SQL)
        conn.commit()
    except sqlite3.Error as exc:
        logger.warning("Table %s/daily_summary_arguments indisponible (%s). "
                        "Repli sur presentation structuree.", usage_table, exc)
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

    used = get_usage(conn, usage_table, today_real)
    remaining = max(0, call_limit - used)
    if remaining <= 0:
        logger.info("Quota LLM (%s, %d/jour) deja atteint (%d utilises). "
                     "Repli sur presentation structuree pour les tickers restants.",
                     usage_table, call_limit, used)
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
            text = generate_with_retry(client, conn, s)
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
        bump_usage(conn, usage_table, today_real)


# --- Orchestration ---------------------------------------------------------

def build_daily_summary(conn, today=None):
    """Return (signals, dates_by_priority, n_candidates). ``signals`` has at
    most TOP_N entries, fewer if not enough tickers clear MIN_CONFIDENCE.
    ``dates_by_priority`` is {priorite: date_calcul} (see
    resolve_data_dates_by_priority) -- NOT a single date -- because
    candidates are now drawn from EVERY priorite tier at its own latest
    date_calcul, so a tier recomputed less recently than another is still
    considered, never silently excluded (see resolve_data_date's docstring
    for the bug this replaced: "basse" being refreshed more often than
    "haute"/"moyenne" used to make the latter invisible to this exact
    function). Callers must surface dates_by_priority (and ideally
    staleness_summary(dates_by_priority)) so a stale tier is visible,
    never silent."""
    dates_by_priority = resolve_data_dates_by_priority(conn, today)
    if not dates_by_priority:
        return [], {}, 0

    rows = load_opportunites_multi(conn, dates_by_priority)
    eligible = [r for r in rows if r["confiance"] is not None and r["confiance"] >= MIN_CONFIDENCE]

    ranked = sorted(
        eligible,
        key=lambda r: compute_adjusted_score(r["score_global"], r["confiance"]),
        reverse=True,
    )
    top = ranked[:TOP_N]

    relations = load_relations(conn)
    graph = build_graph(relations)
    signals = [build_signal(conn, r, graph, relations) for r in top]

    return signals, dates_by_priority, len(eligible)


def build_signal(conn, row, graph, relations):
    """Build one `signals` entry (see build_daily_summary's docstring) from
    a single `opportunites` row. Extracted so build_daily_summary() (today's
    top N) and dashboard/app.py's per-ticker "Analyse d'une action" AI
    section (any ticker, on demand) share the exact same construction --
    they must never drift apart into two slightly different signal shapes.
    `row` needs score_prix_valorisation/score_technique/score_fondamental_reel/
    score_global/confiance/explication/ticker (sqlite3.Row or dict, same
    shape as opportunites). `graph`/`relations` come from
    graph.build_graph.load_relations/build_graph (a caller-supplied cache is
    fine -- this function does no I/O for them itself)."""
    closes = load_price_series(conn, row["ticker"])
    volatility = compute_volatility(closes) if closes else None
    conflict = has_conflict(
        row["score_prix_valorisation"], row["score_technique"], row["score_fondamental_reel"]
    )
    risk = compute_risk(volatility, row["confiance"], conflict)
    watch = companies_to_watch(graph, relations, row["ticker"])
    nom_affiche = load_display_name(conn, row["ticker"])
    prix = compute_price_variation(conn, row["ticker"])

    return {
        "ticker": row["ticker"],
        "nom_affiche": nom_affiche,
        "score_global": row["score_global"],
        "confiance": row["confiance"],
        "score_ajuste": compute_adjusted_score(row["score_global"], row["confiance"]),
        "score_prix_valorisation": row["score_prix_valorisation"],
        "score_technique": row["score_technique"],
        "score_news": row["score_news"],
        "score_fondamental_reel": row["score_fondamental_reel"],
        "explication": row["explication"],
        "risque": risk,
        "conflit_composantes": conflict,
        "volatilite": volatility,
        "horizon": HORIZON_LABEL,
        "entreprises_a_surveiller": watch,
        "prix": prix,
    }


# --- CLI ---------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(description="Build today's daily investment summary.")
    p.add_argument("--date", default=None, help="Override date_calcul (YYYY-MM-DD), for testing.")
    return p.parse_args(argv)


def _fmt_pct(value):
    return f"{value:.0%}" if value is not None else "n/a"


def print_summary(signals, dates_by_priority, n_candidates):
    print("\n" + "=" * 78)
    if dates_by_priority:
        dates_str = ", ".join(f"{p}={d}" for p, d in sorted(dates_by_priority.items()))
    else:
        dates_str = "aucune donnee"
    print(f"RESUME DU JOUR - {dates_str}")
    note = staleness_summary(dates_by_priority)
    if note:
        print(f"[!] {note}")
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
    signals, dates_by_priority, n_candidates = build_daily_summary(conn, today=args.date)
    add_argued_texts(conn, signals)
    conn.close()

    print_summary(signals, dates_by_priority, n_candidates)
    logger.info("Resume genere (%s) : %d signal(aux) (sur %d candidats eligibles).",
                dates_by_priority, len(signals), n_candidates)
    return 0


if __name__ == "__main__":
    sys.exit(main())
