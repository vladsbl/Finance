"""GET /api/news -- JSON view of dashboard/app.py's "News & Analyse IA"
page.

This route is a thin wrapper around reasoning/analyze_news.py's
load_news, price_before_after_news, news_summary_paragraph -- no
selection, price, or paragraph-assembly logic is reimplemented here.
price_before_after_news returns native-currency prices only (see its own
docstring); the EUR conversion below follows api/routers/stock.py's own
precedent (raw floats via dashboard/currency.py's get_rate_to_eur, not the
Streamlit-only display-string formatter).
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from api.dependencies import get_db
from dashboard.currency import get_rate_to_eur
from reasoning.analyze_news import (
    get_or_generate_news_narrative,
    load_news,
    news_summary_paragraph,
    price_before_after_news,
)
from reasoning.daily_summary import load_latest_scores_bulk
from reasoning.direction_probability import (
    compute_direction_probabilities,
    dominant_direction,
    load_causal_effects_bulk,
)

router = APIRouter(prefix="/api/news", tags=["news"])
VALID_DIRECTIONS = {"toutes", "hausse", "stagnation", "baisse"}

DEFAULT_LIMIT = 50
MAX_LIMIT = 500


def _price_context(conn, ticker, published_at):
    """Structured price-before/after-news context -- EUR conversion done
    here (native currency only in price_before_after_news, matching
    api/routers/stock.py's own precedent). `insufficient_data` +
    `insufficient_reason` replace dashboard/app.py's
    _format_price_before_after pre-formatted string: React renders its own
    "donnee insuffisante" presentation from these structured fields
    instead of parsing a sentence."""
    devise_row = conn.execute(
        "SELECT devise FROM universe WHERE ticker = ?", (ticker,)
    ).fetchone()
    devise = devise_row[0] if devise_row and devise_row[0] else "USD"
    rate = get_rate_to_eur(conn, devise)

    info = price_before_after_news(conn, ticker, published_at)
    base = {
        "devise": devise,
        "date_before": None, "price_before": None, "price_before_eur": None,
        "date_after": None, "price_after": None, "price_after_eur": None,
        "variation_pct": None,
    }

    if info is None:
        return {
            **base,
            "insufficient_data": True,
            "insufficient_reason": (
                "Aucun prix disponible avant ou a la date de cette news."
            ),
        }

    price_before_eur = (
        info["price_before"] * rate
        if rate is not None and info["price_before"] is not None else None
    )
    if info["variation_pct"] is None:
        return {
            **base,
            "date_before": info["date_before"],
            "price_before": info["price_before"],
            "price_before_eur": price_before_eur,
            "insufficient_data": True,
            "insufficient_reason": (
                "Rien de plus recent que le dernier prix disponible pour "
                "mesurer une evolution depuis cette news."
            ),
        }

    price_after_eur = info["price_after"] * rate if rate is not None else None
    return {
        **base,
        "date_before": info["date_before"],
        "price_before": info["price_before"],
        "price_before_eur": price_before_eur,
        "date_after": info["date_after"],
        "price_after": info["price_after"],
        "price_after_eur": price_after_eur,
        "variation_pct": info["variation_pct"],
        "insufficient_data": False,
        "insufficient_reason": None,
    }


def _news_to_dict(conn, row, direction):
    return {
        "news_id": row["news_id"],
        "ticker": row["ticker"],
        "title": row["title"],
        "url": row["url"],
        "published_at": row["published_at"],
        "source": row["source"],
        "company": row["company"],
        "sector": row["sector"],
        "importance": row["importance"],
        "tonalite": row["tonalite"],
        "impact": row["impact"],
        "horizon": row["horizon"],
        "confidence": row["confidence"],
        "summary_paragraph": news_summary_paragraph(row),
        "price_context": _price_context(conn, row["ticker"], row["published_at"]),
        "direction_probabilities": direction,
    }


def _build_directions_by_ticker(conn, tickers):
    """{ticker: direction_probabilities} for every DISTINCT ticker in
    `tickers` -- computed ONCE per ticker (not once per news item, even
    though several news items usually share the same ticker), from two
    bulk queries total (load_latest_scores_bulk + load_causal_effects_bulk)
    -- never one query per row. This is the GENERAL direction (no
    news-specific tonalite/importance folded in, unlike
    GET /api/news/{news_id}/narrative's own enriched, news-aware
    computation) -- the same free, Groq-less read shown everywhere else a
    ticker's direction appears before its enriched narrative is generated."""
    tickers = sorted({t for t in tickers if t})
    scores = load_latest_scores_bulk(conn, tickers)
    causal_effects = load_causal_effects_bulk(conn)

    directions = {}
    for t in tickers:
        s = scores[t]
        causal = causal_effects.get(t)
        directions[t] = compute_direction_probabilities(
            score_technique=s["technical_score"],
            score_prix_valorisation=s["price_valuation_score"],
            score_fondamental_reel=s["score_fondamental_reel"],
            causal_effect=causal["effet"] if causal else None,
            causal_confidence=causal["confiance"] if causal else None,
        )
    return directions


@router.get("")
def get_news(
    ticker: str | None = Query(None, description="Filtre sur un ticker precis"),
    direction: str = Query("toutes", description="toutes | hausse | stagnation | baisse"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Lignes par page"),
    offset: int = Query(0, ge=0, description="Nombre de lignes a sauter"),
    conn=Depends(get_db),
):
    """Every analysed news item, most recent first, optionally scoped to
    one ticker -- see load_news's own docstring for why the default sort
    here (global recency) differs from the Streamlit page's
    (ticker-then-importance-then-recency, moot there since that page
    always scopes to one ticker first via a selectbox).

    `direction` filters on each item's ticker's DOMINANT hausse/stagnation/
    baisse scenario -- applied server-side, BEFORE pagination, same
    reasoning as /api/opportunities' own `direction` param: this list
    genuinely paginates (hundreds of news across many pages), so a
    client-side filter would only narrow whatever page is currently
    loaded. `direction_probabilities` itself is the GENERAL (non-news-
    specific) read, computed once per distinct ticker via
    _build_directions_by_ticker -- a pure, Groq-free computation, not the
    enriched narrative's own per-news-item version.

    `limit`/`offset` paginate the already-sorted, already-filtered list,
    same convention as /api/opportunities and /api/correlations."""
    ticker = (ticker or "").strip().upper() or None
    direction = (direction or "toutes").strip().lower()
    if direction not in VALID_DIRECTIONS:
        raise HTTPException(
            status_code=422,
            detail=f"direction invalide : {direction!r} (attendu : {sorted(VALID_DIRECTIONS)})",
        )

    rows = load_news(conn, ticker)
    directions_by_ticker = _build_directions_by_ticker(conn, (r["ticker"] for r in rows))

    if direction != "toutes":
        rows = [
            r for r in rows
            if directions_by_ticker.get(r["ticker"]) is not None
            and dominant_direction(directions_by_ticker[r["ticker"]]) == direction
        ]

    n_total = len(rows)
    page = rows[offset:offset + limit]

    return {
        "news": [_news_to_dict(conn, r, directions_by_ticker.get(r["ticker"])) for r in page],
        "n_total": n_total,
        "ticker": ticker,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{news_id}/narrative")
def get_news_narrative(news_id: int, conn=Depends(get_db)):
    """AI-written explanation of ONE news item (what it means, why it
    matters, impact on the ticker and its Knowledge-Graph-related
    companies, plus a hausse/stagnation/baisse split) -- generated ON
    DEMAND, never as part of the list response above (see
    reasoning/analyze_news.py's get_or_generate_news_narrative for why:
    the list can page through thousands of items, so eagerly calling Groq
    for every row would be an uncontrolled cost). A GET here is what
    actually triggers generation on a cache miss, same convention as
    GET /api/daily-summary/{ticker}/argued-text -- the frontend calls this
    only when a user explicitly opens one news item's enriched view.

    Never a 5xx for a normal degraded state (dedicated quota exhausted, no
    API key, network error): source="unavailable" with texte=None
    distinguishes it from a real generation. 404 only when this news_id has
    no news_analysis row at all (never analysed, or doesn't exist)."""
    found, result = get_or_generate_news_narrative(conn, news_id)
    if not found:
        raise HTTPException(
            status_code=404,
            detail=f"Aucune news analysee avec l'id {news_id}.",
        )
    return result
