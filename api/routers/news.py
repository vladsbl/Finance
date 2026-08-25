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

from fastapi import APIRouter, Depends, Query

from api.dependencies import get_db
from dashboard.currency import get_rate_to_eur
from reasoning.analyze_news import (
    load_news,
    news_summary_paragraph,
    price_before_after_news,
)

router = APIRouter(prefix="/api/news", tags=["news"])

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


def _news_to_dict(conn, row):
    return {
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
    }


@router.get("")
def get_news(
    ticker: str | None = Query(None, description="Filtre sur un ticker precis"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Lignes par page"),
    offset: int = Query(0, ge=0, description="Nombre de lignes a sauter"),
    conn=Depends(get_db),
):
    """Every analysed news item, most recent first, optionally scoped to
    one ticker -- see load_news's own docstring for why the default sort
    here (global recency) differs from the Streamlit page's
    (ticker-then-importance-then-recency, moot there since that page
    always scopes to one ticker first via a selectbox).

    `limit`/`offset` paginate the already-sorted, already-filtered list,
    same convention as /api/opportunities and /api/correlations."""
    ticker = (ticker or "").strip().upper() or None
    rows = load_news(conn, ticker)

    n_total = len(rows)
    page = rows[offset:offset + limit]

    return {
        "news": [_news_to_dict(conn, r) for r in page],
        "n_total": n_total,
        "ticker": ticker,
        "limit": limit,
        "offset": offset,
    }
