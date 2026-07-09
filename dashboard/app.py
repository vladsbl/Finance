#!/usr/bin/env python3
"""Streamlit dashboard for the market intelligence pipeline.

Reads the latest snapshot per symbol from ``stocks`` and the latest weighted
scores from ``final_scores`` (data/marketdb.db), and presents a ranking, a
per-stock detail view, a price/MA chart and global statistics.

Run:
    streamlit run dashboard/app.py

Data note
---------
``stocks`` stores one snapshot (price + moving averages) per ingestion run, so
MA50/MA200 are single values drawn as horizontal reference lines, and the price
is shown over whatever snapshot history exists. RSI is not persisted in
``final_scores``; it is recomputed here by reusing analysis/combined_score.py
(real RSI-14 when enough history exists, otherwise a documented proxy).
"""

import os
import sqlite3
import sys

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# Make the project importable so we can reuse the RSI logic instead of
# duplicating it.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from analysis.combined_score import compute_rsi, proxy_rsi  # noqa: E402

DB_PATH = os.path.join(REPO_ROOT, "data", "marketdb.db")

# Score thresholds for colour coding.
GOOD, WEAK = 60.0, 40.0
COLOR_GOOD = "#1b8a3a"
COLOR_MID = "#c77d0a"
COLOR_BAD = "#b3261e"


# --- Data access -----------------------------------------------------------

LATEST_STOCKS_SQL = """
SELECT s.symbol, s.current_price, s.ma_50, s.ma_200, s.volume, s.volatility
FROM stocks s
JOIN (SELECT symbol, MAX(id) AS max_id FROM stocks GROUP BY symbol) l
  ON s.id = l.max_id;
"""

LATEST_FINAL_SQL = """
SELECT f.symbol, f.fundamental_score, f.technical_score, f.volatility_score,
       f.volume_score, f.final_score, f.confidence
FROM final_scores f
JOIN (SELECT symbol, MAX(id) AS max_id FROM final_scores GROUP BY symbol) l
  ON f.id = l.max_id;
"""

PRICE_HISTORY_SQL = """
SELECT ticker AS symbol, date, close
FROM price_history
WHERE close IS NOT NULL
ORDER BY ticker, date;
"""


@st.cache_data(show_spinner=False)
def load_data():
    """Load and merge the source tables. Returns (df, history, error).

    ``df`` is one row per symbol with stock metrics + scores + computed RSI.
    ``history`` maps symbol -> DataFrame(date, close) of real daily bars.
    ``error`` is a human-readable string when data can't be loaded, else None.
    """
    if not os.path.exists(DB_PATH):
        return None, None, (
            f"Database not found at `{DB_PATH}`.\n\n"
            "Run the pipeline first: `python ingestion/fetch_prices.py`, "
            "`python ingestion/ingest_prices.py`, then "
            "`python analysis/fundamental/score.py` and "
            "`python analysis/combined_score.py`."
        )

    try:
        conn = sqlite3.connect(DB_PATH)
        stocks = pd.read_sql_query(LATEST_STOCKS_SQL, conn)
        finals = pd.read_sql_query(LATEST_FINAL_SQL, conn)
        # price_history is optional: absent until ingest_prices.py has run.
        try:
            history_raw = pd.read_sql_query(PRICE_HISTORY_SQL, conn)
        except (sqlite3.Error, pd.errors.DatabaseError):
            history_raw = pd.DataFrame(columns=["symbol", "date", "close"])
        conn.close()
    except (sqlite3.Error, pd.errors.DatabaseError) as exc:
        return None, None, f"Could not read the database: {exc}"

    if stocks.empty:
        return None, None, (
            "The `stocks` table is empty. Run `python ingestion/fetch_prices.py`."
        )
    if finals.empty:
        return None, None, (
            "The `final_scores` table is empty. "
            "Run `python analysis/combined_score.py`."
        )

    df = stocks.merge(finals, on="symbol", how="inner")
    if df.empty:
        return None, None, (
            "No symbol is present in both `stocks` and `final_scores`."
        )

    history = {
        sym: g[["date", "close"]].reset_index(drop=True)
        for sym, g in history_raw.groupby("symbol")
    }

    # Recompute RSI per symbol (real if enough history, else proxy).
    rsis, is_real = [], []
    for _, row in df.iterrows():
        prices = history.get(row["symbol"], pd.DataFrame())
        series = prices["close"].tolist() if not prices.empty else []
        rsi = compute_rsi(series)
        if rsi is None:
            rsi = proxy_rsi(row["current_price"], row["ma_50"], row["ma_200"])
            is_real.append(False)
        else:
            is_real.append(True)
        rsis.append(round(rsi, 1))
    df["rsi"] = rsis
    df["rsi_is_real"] = is_real

    df = df.sort_values("final_score", ascending=False).reset_index(drop=True)
    return df, history, None


# --- UI helpers ------------------------------------------------------------

def score_color(value):
    if value is None or pd.isna(value):
        return COLOR_MID
    if value > GOOD:
        return COLOR_GOOD
    if value < WEAK:
        return COLOR_BAD
    return COLOR_MID


def render_top10(df):
    st.subheader("Top 10 Actions")

    table = df.head(10)[[
        "symbol", "final_score", "fundamental_score", "technical_score",
        "volatility_score", "volume_score", "confidence",
    ]].rename(columns={
        "symbol": "Symbol",
        "final_score": "Final Score",
        "fundamental_score": "Fundamental",
        "technical_score": "Technical",
        "volatility_score": "Volatility",
        "volume_score": "Volume",
        "confidence": "Confidence",
    })

    def color_row(row):
        bg = score_color(row["Final Score"])
        return [f"background-color: {bg}; color: white;"] * len(row)

    styled = (
        table.style
        .apply(color_row, axis=1)
        .format({
            "Final Score": "{:.1f}", "Fundamental": "{:.1f}",
            "Technical": "{:.1f}", "Volatility": "{:.1f}",
            "Volume": "{:.1f}", "Confidence": "{:.0f}%",
        })
    )
    st.dataframe(styled, use_container_width=True, hide_index=True)
    st.caption("Green: score > 60 · Orange: 40-60 · Red: < 40")


def render_detail(df):
    st.subheader("Detail d'une action")

    symbols = df["symbol"].tolist()
    symbol = st.selectbox("Select a stock", symbols, key="detail_symbol")
    row = df[df["symbol"] == symbol].iloc[0]

    c1, c2, c3 = st.columns(3)
    c1.metric("Current price", f"${row['current_price']:.2f}")
    c2.metric("MA 50", f"${row['ma_50']:.2f}")
    c3.metric("MA 200", f"${row['ma_200']:.2f}")

    c4, c5, c6 = st.columns(3)
    volume = row["volume"]
    c4.metric("Volume", f"{int(volume):,}" if pd.notna(volume) else "N/A")
    vol = row["volatility"]
    c5.metric("Volatility", f"{vol:.2%}" if pd.notna(vol) else "N/A")
    rsi_label = f"{row['rsi']:.1f}" + ("" if row["rsi_is_real"] else " (proxy)")
    c6.metric("RSI (14)", rsi_label)

    st.markdown("**Scores**")
    s1, s2, s3 = st.columns(3)
    s1.metric("Fundamental", f"{row['fundamental_score']:.1f}")
    s2.metric("Technical", f"{row['technical_score']:.1f}")
    s3.metric("Volatility score", f"{row['volatility_score']:.1f}")
    s4, s5, s6 = st.columns(3)
    s4.metric("Volume score", f"{row['volume_score']:.1f}")
    s5.metric("Final score", f"{row['final_score']:.1f}")
    s6.metric("Confidence", f"{row['confidence']:.0f}%")

    return symbol


def render_chart(df, history, symbol):
    st.subheader("Prix & moyennes mobiles")
    prices = history.get(symbol, pd.DataFrame())

    if prices.empty:
        st.info(
            "No price history for this stock yet. "
            "Run `python ingestion/ingest_prices.py` to populate `price_history`."
        )
        return

    prices = prices.copy()
    prices["date"] = pd.to_datetime(prices["date"])
    prices = prices.sort_values("date")
    # True moving averages from the real daily closes (NaN until the window
    # fills, so plotly simply starts each line where it becomes valid).
    prices["ma_50"] = prices["close"].rolling(50).mean()
    prices["ma_200"] = prices["close"].rolling(200).mean()

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=prices["date"], y=prices["close"],
        mode="lines", name="Price", line=dict(color="#2b6cb0", width=1.5),
    ))
    fig.add_trace(go.Scatter(
        x=prices["date"], y=prices["ma_50"],
        mode="lines", name="MA 50", line=dict(color="orange", width=1.8),
    ))
    fig.add_trace(go.Scatter(
        x=prices["date"], y=prices["ma_200"],
        mode="lines", name="MA 200", line=dict(color="#1f4e79", width=1.8),
    ))
    fig.update_layout(
        title=f"{symbol} - price vs moving averages ({len(prices)} trading days)",
        xaxis_title="Date", yaxis_title="Price ($)",
        height=440, margin=dict(t=50, b=40),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        hovermode="x unified",
    )
    st.plotly_chart(fig, use_container_width=True)


def render_stats(df):
    st.subheader("Statistiques globales")
    best = df.iloc[0]
    worst = df.iloc[-1]

    c1, c2, c3 = st.columns(3)
    c1.metric("Stocks analysed", f"{len(df)}")
    c2.metric("Average score", f"{df['final_score'].mean():.1f}")
    c3.metric("Average volatility", f"{df['volatility'].mean():.2%}")

    c4, c5 = st.columns(2)
    c4.metric("Best", best["symbol"], f"{best['final_score']:.1f}")
    c5.metric("Worst", worst["symbol"], f"{worst['final_score']:.1f}")


# --- News & AI analysis ----------------------------------------------------

NEWS_SQL = """
SELECT n.ticker, n.title, n.url, n.published_at, n.source,
       a.company, a.sector, a.importance, a.tonalite, a.impact,
       a.horizon, a.confidence
FROM news_analysis a
JOIN news_raw n ON n.id = a.news_id
ORDER BY n.ticker, a.importance DESC, n.published_at DESC;
"""


@st.cache_data(show_spinner=False)
def load_news():
    """Return (news_df, error). Empty df (no error) when nothing analysed yet."""
    if not os.path.exists(DB_PATH):
        return pd.DataFrame(), None
    try:
        conn = sqlite3.connect(DB_PATH)
        news = pd.read_sql_query(NEWS_SQL, conn)
        conn.close()
    except (sqlite3.Error, pd.errors.DatabaseError):
        # news_raw / news_analysis not created yet.
        return pd.DataFrame(), None
    return news, None


def tonalite_color(tonalite):
    t = (tonalite or "").lower()
    if t.startswith("pos"):
        return COLOR_GOOD
    if t.startswith("neg"):
        return COLOR_BAD
    return "#6b7280"  # neutre


def render_news_page():
    st.subheader("News & Analyse IA")
    news, error = load_news()
    if error:
        st.error(error)
        return
    if news.empty:
        st.info(
            "Aucune news analysee pour l'instant. Lance :\n\n"
            "1. `python ingestion/fetch_news.py`\n"
            "2. `python reasoning/analyze_news.py`"
        )
        return

    tickers = sorted(news["ticker"].unique())
    ticker = st.selectbox("Ticker", tickers, key="news_ticker")
    sub = news[news["ticker"] == ticker].reset_index(drop=True)
    st.caption(f"{len(sub)} news analysees pour {ticker}")

    for _, r in sub.iterrows():
        color = tonalite_color(r["tonalite"])
        badge = (
            f"<span style='background:{color};color:white;padding:2px 10px;"
            f"border-radius:12px;font-size:0.8em'>{r['tonalite']}</span>"
        )
        importance = int(r["importance"]) if pd.notna(r["importance"]) else "?"
        confidence = int(r["confidence"]) if pd.notna(r["confidence"]) else "?"
        title = r["title"]
        title_html = (
            f"<a href='{r['url']}' target='_blank'>{title}</a>"
            if r["url"] else title
        )
        st.markdown(
            f"{badge}&nbsp;&nbsp;<b>Importance {importance}/10</b>"
            f"&nbsp;&nbsp;&middot;&nbsp;&nbsp;confiance {confidence}%<br>"
            f"{title_html}",
            unsafe_allow_html=True,
        )
        meta = " &middot; ".join(
            str(x) for x in [r["company"], r["sector"], r["horizon"],
                             f"source: {r['source']}"] if x
        )
        st.caption(meta)
        if r["impact"]:
            st.write(r["impact"])
        st.divider()


# --- Pages ------------------------------------------------------------------

def _get_scored_data():
    """Load scored data or stop the page with an error banner."""
    df, history, error = load_data()
    if error:
        st.error(error)
        st.stop()
    return df, history


def page_overview():
    """Top 10 ranking + global statistics."""
    df, _ = _get_scored_data()
    render_top10(df)
    st.divider()
    render_stats(df)


def page_stock():
    """Per-stock detail + price/MA chart."""
    df, history = _get_scored_data()
    symbol = render_detail(df)
    st.divider()
    render_chart(df, history, symbol)


def page_news():
    """News feed with LLM analysis."""
    render_news_page()


# --- Main ------------------------------------------------------------------

def main():
    st.set_page_config(page_title="Market Intelligence Dashboard", layout="wide")
    st.title("Market Intelligence Dashboard")

    pages = [
        st.Page(page_overview, title="Vue d'ensemble", icon=":material/leaderboard:",
                url_path="overview", default=True),
        st.Page(page_stock, title="Analyse d'une action", icon=":material/query_stats:",
                url_path="stock"),
        st.Page(page_news, title="News & Analyse IA", icon=":material/newspaper:",
                url_path="news"),
    ]
    nav = st.navigation(pages)

    with st.sidebar:
        if st.button("Refresh Data"):
            load_data.clear()
            load_news.clear()
            st.rerun()

    nav.run()


if __name__ == "__main__":
    main()
