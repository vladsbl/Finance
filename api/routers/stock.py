"""GET /api/stock/* and GET /api/tickers -- JSON view of dashboard/app.py's
"Analyse d'une action" page.

Every route here is a thin wrapper around reasoning/daily_summary.py
(load_ticker_detail, compute_price_variation, load_price_chart_series,
load_all_tickers_with_names) and api/dependencies.py's
get_or_generate_argued_text (shared with GET /api/daily-summary/{ticker}/
argued-text) -- no scoring, price-history, or Groq-prompting logic is
reimplemented here.
"""

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies import get_db, get_or_generate_argued_text, normalise_ticker
from dashboard.currency import get_rate_to_eur
from reasoning.daily_summary import (
    compute_price_variation,
    load_all_tickers_with_names,
    load_price_chart_series,
    load_ticker_detail,
)
from reasoning.direction_probability import (
    compute_direction_probabilities,
    load_causal_effect_for_ticker,
)

router = APIRouter(prefix="/api/stock", tags=["stock"])

# GET /api/tickers lives outside the /api/stock prefix (it populates the
# search box BEFORE any ticker is chosen) -- a second, prefix-less router in
# this same module so main.py only needs one extra include_router() call.
tickers_router = APIRouter(tags=["stock"])


@tickers_router.get("/api/tickers")
def get_tickers(conn=Depends(get_db)):
    """{ticker, nom_affiche} for every tracked ticker -- deliberately
    light (no scores, no prices) so the frontend can load the whole
    universe once to populate a search/autocomplete box instead of
    round-tripping the API on every keystroke."""
    names = load_all_tickers_with_names(conn)
    return {
        "tickers": [
            {"ticker": ticker, "nom_affiche": nom_affiche}
            for ticker, nom_affiche in names.items()
        ]
    }


def _sector_row(conn, ticker):
    row = conn.execute(
        "SELECT sector, industry FROM ticker_sector_cache WHERE ticker = ?",
        (ticker,),
    ).fetchone()
    if row is None:
        return None, None
    return row[0], row[1]


@router.get("/{ticker}")
def get_stock_detail(ticker: str, conn=Depends(get_db)):
    """Full per-ticker detail: the 4 pillar/legacy scores + confidence from
    load_ticker_detail(), price + 1j/7j/30j variation from
    compute_price_variation() (price converted to EUR here, the same
    display-only conversion dashboard/app.py applies at render time --
    price_history itself is never touched), priorite, and sector/industry
    from ticker_sector_cache if that ticker has been through
    universe/fetch_sector_info.py at all. 404 only if the ticker isn't in
    `universe`, never for a merely-missing pillar (those come back as null,
    same graceful-degradation convention as the rest of this module)."""
    ticker = normalise_ticker(ticker)

    detail = load_ticker_detail(conn, ticker)
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail=f"{ticker} n'est pas dans l'univers suivi.",
        )

    price_info = compute_price_variation(conn, ticker)
    prix_eur = None
    if price_info is not None:
        rate = get_rate_to_eur(conn, price_info["devise"])
        prix_eur = price_info["prix_actuel"] * rate if rate is not None else None

    sector, industry = _sector_row(conn, ticker)

    causal = load_causal_effect_for_ticker(conn, ticker)
    direction = compute_direction_probabilities(
        score_technique=detail["technical_score"],
        score_prix_valorisation=detail["price_valuation_score"],
        score_fondamental_reel=detail["score_fondamental_reel"],
        causal_effect=causal["effet"] if causal else None,
        causal_confidence=causal["confiance"] if causal else None,
    )

    return {
        "ticker": detail["ticker"],
        "nom_affiche": detail["nom_affiche"],
        "priorite": detail["priorite"],
        "devise": detail["devise"],
        "current_price": detail["current_price"],
        "prix_eur": prix_eur,
        "variations": price_info["variations"] if price_info else None,
        "ma_50": detail["ma_50"],
        "ma_200": detail["ma_200"],
        "volume": detail["volume"],
        "volatility": detail["volatility"],
        "rsi": detail["rsi"],
        "rsi_is_real": detail["rsi_is_real"],
        "price_valuation_score": detail["price_valuation_score"],
        "technical_score": detail["technical_score"],
        "volatility_score": detail["volatility_score"],
        "volume_score": detail["volume_score"],
        "final_score": detail["final_score"],
        "confidence": detail["confidence"],
        "score_fondamental_reel": detail["score_fondamental_reel"],
        "sector": sector,
        "industry": industry,
        "direction_probabilities": direction,
    }


@router.get("/{ticker}/argued-text")
def get_stock_argued_text(ticker: str, conn=Depends(get_db)):
    """Identical on-demand AI analysis as
    GET /api/daily-summary/{ticker}/argued-text -- see
    api/dependencies.py's get_or_generate_argued_text for the shared
    cache-then-generate flow (same USAGE_TABLE_TICKER_ANALYSIS quota pool,
    10/day, shared with the daily-summary page since it's the same button
    on the same underlying data). 404 only if the ticker has no
    opportunites row at all (never scored)."""
    ticker = normalise_ticker(ticker)

    found, result = get_or_generate_argued_text(conn, ticker)
    if not found:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Aucune donnee d'opportunite pour {ticker}. Lance "
                "`python reasoning/opportunity_scoring.py --priorite toutes` "
                "pour l'inclure."
            ),
        )
    return result


@router.get("/{ticker}/chart")
def get_stock_chart(ticker: str, conn=Depends(get_db)):
    """Full price/MA50/MA200 series for the price chart -- see
    reasoning/daily_summary.py's load_price_chart_series for the rolling-
    window computation and EUR-conversion fallback (mirrors
    dashboard/app.py's render_chart exactly). 404 only if the ticker isn't
    in `universe`; `points` is an empty list (not a 404) if the ticker
    exists but has no price_history rows yet."""
    ticker = normalise_ticker(ticker)

    chart = load_price_chart_series(conn, ticker)
    if chart is None:
        raise HTTPException(
            status_code=404,
            detail=f"{ticker} n'est pas dans l'univers suivi.",
        )
    return chart
