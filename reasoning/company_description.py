#!/usr/bin/env python3
"""Short French company description -- generated ONCE per ticker, cached
forever.

Unlike every other Groq-backed text in this project (daily_summary.py's
argued text, analyze_news.py's news narrative), a company description has
no "today" component: what Freeport-McMoRan actually does does not change
day to day, so there is no reason to ever regenerate it once written. This
is the one piece of generated content in the whole pipeline with a
PERMANENT cache -- no (day, ticker) key, just (ticker).

That also makes the quota math simple: the universe is a fixed, finite set
(~2000 tickers as of 2026-08-25). A modest daily generation cap still
covers the whole universe within a few weeks and is NEVER paid again after
that -- unlike daily_summary.py/analyze_news.py's quotas, which reset and
get spent again every single day.

Usage:
    from reasoning.company_description import get_or_generate_company_description
    found, result = get_or_generate_company_description(conn, "AAPL")
"""

import os
import sqlite3
from datetime import date

CREATE_DESCRIPTIONS_SQL = """
CREATE TABLE IF NOT EXISTS company_descriptions (
    ticker      TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    model       TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

CREATE_USAGE_SQL = """
CREATE TABLE IF NOT EXISTS llm_usage_company_description (
    day   TEXT PRIMARY KEY,
    calls INTEGER NOT NULL DEFAULT 0
);
"""

# The universe is finite (~2000 tickers) and each one is generated AT MOST
# ONCE ever -- this quota simply goes unused every day after the universe
# is fully backfilled, unlike the daily reasoning/analyze_news.py or
# reasoning/daily_summary.py quotas, which are spent again every single day.
#
# Measured for real on 2026-08-25 against the live Groq API,
# openai/gpt-oss-120b: 305 prompt tokens (fixed-ish, this prompt barely
# varies in length) + a completion that swung from 274 to 1322 tokens
# across otherwise-identical calls (579 to 1627 total) -- openai/gpt-oss-
# 120b is a reasoning model whose hidden chain-of-thought is billed as
# completion tokens, so the SAME short 1-2 sentence answer can cost very
# different amounts run to run. Sized to 60/day against the WORST observed
# case (60 x ~1627 = ~98k tokens/day, roughly half the shared 200k TPD
# budget) rather than the typical case, so a bad-variance day still leaves
# real headroom for reasoning/analyze_news.py's own much larger quota --
# covers the ~2000-ticker universe in ~5-6 weeks, entirely one-time.
COMPANY_DESCRIPTION_DAILY_LIMIT = 60

USAGE_TABLE = "llm_usage_company_description"

SYSTEM_PROMPT_COMPANY_DESCRIPTION = (
    "Tu rediges une description courte et claire, en francais, d'une "
    "entreprise cotee en bourse, pour un lecteur non specialiste. "
    "Longueur IMPERATIVE : 1 a 2 phrases maximum, couvrant : le secteur "
    "d'activite, l'activite principale concrete de l'entreprise, et son "
    "positionnement (ex: leader, acteur de niche, challenger). "
    "Si un secteur/une industrie sont fournis ci-dessous, base-toi dessus. "
    "Sinon, appuie-toi sur ta connaissance generale de cette entreprise "
    "reelle -- ne dis jamais que l'information est indisponible, "
    "decris l'entreprise du mieux que tu peux. "
    "Interdiction absolue : ne mentionne jamais un cours de bourse, un "
    "score, une performance recente ou une prevision -- uniquement une "
    "presentation factuelle et intemporelle de l'entreprise elle-meme. "
    "Reponds uniquement avec la description, sans guillemets ni texte "
    "autour."
)


def _create_tables(conn):
    conn.execute(CREATE_DESCRIPTIONS_SQL)
    conn.execute(CREATE_USAGE_SQL)
    conn.commit()


def load_cached_description(conn, ticker):
    row = conn.execute(
        "SELECT description FROM company_descriptions WHERE ticker = ?",
        (ticker,),
    ).fetchone()
    return row[0] if row else None


def save_description(conn, ticker, description, model):
    conn.execute(
        "INSERT INTO company_descriptions (ticker, description, model) VALUES (?, ?, ?) "
        "ON CONFLICT(ticker) DO UPDATE SET description = excluded.description, "
        "model = excluded.model",
        (ticker, description, model),
    )
    conn.commit()


def get_usage(conn, day):
    row = conn.execute(
        f"SELECT calls FROM {USAGE_TABLE} WHERE day = ?", (day,)
    ).fetchone()
    return row[0] if row else 0


def bump_usage(conn, day):
    conn.execute(
        f"INSERT INTO {USAGE_TABLE} (day, calls) VALUES (?, 1) "
        f"ON CONFLICT(day) DO UPDATE SET calls = calls + 1",
        (day,),
    )
    conn.commit()


def _sector_industry(conn, ticker):
    row = conn.execute(
        "SELECT sector, industry FROM ticker_sector_cache WHERE ticker = ?",
        (ticker,),
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def build_company_description_prompt(nom_affiche, ticker, sector, industry):
    lines = [f"Entreprise : {nom_affiche} ({ticker})"]
    lines.append(f"Secteur connu : {sector}" if sector else "Secteur connu : non precise.")
    lines.append(f"Industrie connue : {industry}" if industry else "Industrie connue : non precisee.")
    lines.append("\nRedige la description en 1 a 2 phrases demandee.")
    return "\n".join(lines)


def generate_company_description(client, model, nom_affiche, ticker, sector, industry):
    completion = client.chat.completions.create(
        model=model,
        temperature=0.3,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT_COMPANY_DESCRIPTION},
            {"role": "user", "content": build_company_description_prompt(
                nom_affiche, ticker, sector, industry)},
        ],
    )
    text = completion.choices[0].message.content
    return text.strip() if text else None


def get_or_generate_company_description(conn, ticker):
    """(found, result) -- found=False means `ticker` isn't in `universe` at
    all (caller should 404). result is {"ticker", "description", "source"}
    with source one of "cache" | "generated" | "unavailable" (quota
    exhausted, no API key, network error -- never raises for those, same
    graceful-degradation convention as the rest of this project).

    Unlike daily_summary.py's argued text or analyze_news.py's narrative,
    the cache check here has NO day component: once a ticker has a
    description, it is reused forever, never regenerated -- see the
    module docstring for why that is correct here specifically."""
    # Local import: reasoning.daily_summary pulls in a heavier import
    # chain (analysis/, graph/, its own Groq config) that this small,
    # standalone module should not need just to look up a display name --
    # same lazy-import convention used elsewhere in this project for
    # cross-module reuse (e.g. reasoning/direction_probability.py).
    from reasoning.daily_summary import load_display_name

    row = conn.execute("SELECT 1 FROM universe WHERE ticker = ?", (ticker,)).fetchone()
    if row is None:
        return False, None

    _create_tables(conn)

    cached = load_cached_description(conn, ticker)
    if cached:
        return True, {"ticker": ticker, "description": cached, "source": "cache"}

    # Same pytest guard as daily_summary.py's add_argued_texts /
    # analyze_news.py's get_or_generate_news_narrative -- never burn real
    # Groq quota or require network access from a test run.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True, {"ticker": ticker, "description": None, "source": "unavailable"}

    today = date.today().isoformat()
    if get_usage(conn, today) >= COMPANY_DESCRIPTION_DAILY_LIMIT:
        return True, {"ticker": ticker, "description": None, "source": "unavailable"}

    from dotenv import load_dotenv
    load_dotenv()
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return True, {"ticker": ticker, "description": None, "source": "unavailable"}

    try:
        import httpx
        from groq import Groq
        from ingestion.ssl_utils import configure_ca_bundle
        from reasoning.groq_config import GROQ_MODEL

        REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ca_bundle = configure_ca_bundle(os.path.join(REPO_ROOT, "data"))
        http_client = httpx.Client(verify=ca_bundle) if ca_bundle else None
        client = Groq(api_key=api_key, http_client=http_client)

        nom_affiche = load_display_name(conn, ticker)
        sector, industry = _sector_industry(conn, ticker)
        description = generate_company_description(
            client, GROQ_MODEL, nom_affiche, ticker, sector, industry)
    except Exception:  # noqa: BLE001
        return True, {"ticker": ticker, "description": None, "source": "unavailable"}

    if not description:
        return True, {"ticker": ticker, "description": None, "source": "unavailable"}

    save_description(conn, ticker, description, GROQ_MODEL)
    bump_usage(conn, today)
    return True, {"ticker": ticker, "description": description, "source": "generated"}
