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

import html
import json
import os
import sqlite3
import sys
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# Make the project importable so we can reuse the RSI logic instead of
# duplicating it.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from analysis.combined_score import compute_rsi, proxy_rsi  # noqa: E402
from analysis.price_valuation_scores_universe import compute_volatility  # noqa: E402
from dashboard.currency import CURRENCY_SYMBOLS, format_amount, get_rate_to_eur  # noqa: E402
from dashboard.glossaire import GLOSSAIRE, highlight_terms, term_span  # noqa: E402
from reasoning.daily_summary import (  # noqa: E402
    MIN_CONFIDENCE, TICKER_ANALYSIS_DAILY_LIMIT, USAGE_TABLE_TICKER_ANALYSIS,
    add_argued_texts, build_daily_summary, build_signal,
    load_cached_argument, load_display_name, load_opportunite_for_ticker,
    staleness_note, staleness_summary,
)

DB_PATH = os.path.join(REPO_ROOT, "data", "marketdb.db")

# Score thresholds for colour coding. These are cell/badge BACKGROUND colours
# with white text on top (see color_row()/RISK_COLOR), so their contrast is
# independent of the page's own background -- chosen/verified here for a
# >=4.5:1 white-text contrast ratio (WCAG AA), which the original light-theme
# values (#1b8a3a / #c77d0a / #b3261e) did not all meet: MID in particular
# was ~3.3:1 (fails AA) before this pass. Darkened rather than brightened,
# since brightening a badge background always REDUCES contrast with white
# text -- the "neon" look here comes from the surrounding dark theme
# (style.css) and glow effects, not from the badges being bright themselves.
GOOD, WEAK = 60.0, 40.0
COLOR_GOOD = "#0a7a45"  # deep emerald, ~6.6:1 vs white
COLOR_MID = "#9c6108"   # deep amber, ~5.1:1 vs white
COLOR_BAD = "#a81f2d"   # deep crimson, ~7.3:1 vs white

STYLE_CSS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "style.css")


def inject_style():
    """Load dashboard/style.css once and inject it as global page CSS (the
    "Jarvis" reskin: dark background, cyan glow, futuristic fonts). Paired
    with .streamlit/config.toml (dark base theme) for native widget colours;
    this only adds the flourishes on top. Called once from main(), before
    any page renders, so every page inherits it without per-page code."""
    try:
        with open(STYLE_CSS_PATH, "r", encoding="utf-8") as f:
            css = f.read()
    except OSError:
        return
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)


# --- Data access -----------------------------------------------------------

LATEST_STOCKS_SQL = """
SELECT s.symbol, s.current_price, s.ma_50, s.ma_200, s.volume, s.volatility,
       COALESCE(u.devise, 'USD') AS devise
FROM stocks s
JOIN (SELECT symbol, MAX(id) AS max_id FROM stocks GROUP BY symbol) l
  ON s.id = l.max_id
LEFT JOIN universe u ON u.ticker = s.symbol;
"""

LATEST_FINAL_SQL = """
SELECT f.symbol, f.price_valuation_score, f.technical_score, f.volatility_score,
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


@st.cache_data(show_spinner=False)
def load_exchange_rate(currency):
    """EUR conversion rate for `currency`, cached once per calendar day (see
    dashboard/currency.py's own exchange_rates cache) AND once per
    Streamlit session (st.cache_data) so a page rerun never hits the
    database, let alone the network, for a rate already fetched today.
    Returns None if unavailable -- callers must fall back to the native
    currency (see dashboard/currency.format_amount), never guess a rate."""
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(DB_PATH)
    try:
        return get_rate_to_eur(conn, currency)
    finally:
        conn.close()


# --- Full-universe per-ticker detail ("Analyse d'une action") --------------
#
# `stocks`/`load_data()` above stay pilot-only (10 tickers) -- they still
# back "Vue d'ensemble"'s legacy Top 10 + stats. "Analyse d'une action" reads
# ANY universe ticker instead, computing everything from price_history +
# final_scores + fundamental_real_scores directly so it never depends on the
# pilot-only `stocks` snapshot table.

LATEST_TICKER_SCORES_SQL = """
SELECT price_valuation_score, technical_score, volatility_score, volume_score,
       final_score, confidence
FROM final_scores WHERE symbol = ? ORDER BY id DESC LIMIT 1;
"""

LATEST_TICKER_FUNDAMENTAL_SQL = """
SELECT score_global FROM fundamental_real_scores
WHERE symbol = ? ORDER BY id DESC LIMIT 1;
"""

TICKER_PRICE_HISTORY_SQL = """
SELECT date, close, volume FROM price_history
WHERE ticker = ? AND close IS NOT NULL ORDER BY date;
"""


@st.cache_data(show_spinner=False)
def load_universe_ticker_list():
    """All tracked tickers, sorted -- the full universe (~1900), not just
    the 10 legacy pilots."""
    if not os.path.exists(DB_PATH):
        return []
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute("SELECT ticker FROM universe ORDER BY ticker").fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [r[0] for r in rows]


@st.cache_data(show_spinner=False)
def load_ticker_detail(ticker):
    """Best-effort detail for ANY universe ticker. Each pillar (price/
    valuation, technical, fundamental-real, legacy volatility/volume/final
    scores) is independently None if that pillar hasn't been computed for
    this ticker -- callers must display "N/A" per missing piece, same
    graceful-degradation convention as the rest of the app, never raise.
    Returns None only if the ticker isn't in `universe` at all."""
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(DB_PATH)
    try:
        u = conn.execute(
            "SELECT nom, devise, priorite, nom_entreprise FROM universe WHERE ticker = ?",
            (ticker,),
        ).fetchone()
        if u is None:
            return None
        nom, devise, priorite, nom_entreprise = u
        nom_affiche = (nom_entreprise if nom_entreprise and nom_entreprise != ticker
                       else (nom or ticker))

        final_row = conn.execute(LATEST_TICKER_SCORES_SQL, (ticker,)).fetchone()
        (price_valuation_score, technical_score, volatility_score,
         volume_score, final_score, confidence) = final_row or (None,) * 6

        fund_row = conn.execute(LATEST_TICKER_FUNDAMENTAL_SQL, (ticker,)).fetchone()
        score_fondamental_reel = fund_row[0] if fund_row else None

        hist_rows = conn.execute(TICKER_PRICE_HISTORY_SQL, (ticker,)).fetchall()
    finally:
        conn.close()

    history_df = pd.DataFrame(hist_rows, columns=["date", "close", "volume"])
    current_price = ma_50 = ma_200 = volatility = last_volume = rsi = None
    rsi_is_real = False
    if not history_df.empty:
        closes = history_df["close"].tolist()
        current_price = closes[-1]
        ma_50_last = pd.Series(closes).rolling(50).mean().iloc[-1]
        ma_200_last = pd.Series(closes).rolling(200).mean().iloc[-1]
        ma_50 = float(ma_50_last) if pd.notna(ma_50_last) else None
        ma_200 = float(ma_200_last) if pd.notna(ma_200_last) else None
        volatility = compute_volatility(closes)
        last_volume = history_df["volume"].iloc[-1]

        real_rsi = compute_rsi(closes)
        if real_rsi is not None:
            rsi, rsi_is_real = real_rsi, True
        elif ma_50 is not None and ma_200 is not None:
            rsi, rsi_is_real = proxy_rsi(current_price, ma_50, ma_200), False

    return {
        "ticker": ticker, "nom_affiche": nom_affiche, "devise": devise or "USD",
        "priorite": priorite,
        "current_price": current_price, "ma_50": ma_50, "ma_200": ma_200,
        "volume": last_volume, "volatility": volatility,
        "rsi": rsi, "rsi_is_real": rsi_is_real,
        "price_valuation_score": price_valuation_score,
        "technical_score": technical_score,
        "volatility_score": volatility_score,
        "volume_score": volume_score,
        "final_score": final_score,
        "confidence": confidence,
        "score_fondamental_reel": score_fondamental_reel,
        "history": (history_df[["date", "close"]] if not history_df.empty
                   else pd.DataFrame(columns=["date", "close"])),
    }


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
        "symbol", "final_score", "price_valuation_score", "technical_score",
        "volatility_score", "volume_score", "confidence",
    ]].rename(columns={
        "symbol": "Symbol",
        "final_score": "Final Score",
        "price_valuation_score": "Prix/Valo",
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
            "Final Score": "{:.1f}", "Prix/Valo": "{:.1f}",
            "Technical": "{:.1f}", "Volatility": "{:.1f}",
            "Volume": "{:.1f}", "Confidence": "{:.0f}%",
        })
    )
    st.dataframe(styled, use_container_width=True, hide_index=True)
    st.caption("Green: score > 60 · Orange: 40-60 · Red: < 40")


def render_detail():
    """Ticker picker + top-line metrics for ANY universe ticker (not just
    the 10 legacy pilots -- see load_ticker_detail). Returns the selected
    symbol, or None if `universe` is empty."""
    st.subheader("Detail d'une action")

    tickers = load_universe_ticker_list()
    if not tickers:
        st.error("Aucun ticker dans `universe`. Lance `python universe/build_universe.py`.")
        return None

    names_by_ticker = load_universe_names()
    symbol = st.selectbox(
        "Select a stock", tickers, key="detail_symbol",
        format_func=lambda t: f"{t} - {names_by_ticker.get(t, t)}"
                              if names_by_ticker.get(t, t) != t else t,
    )
    detail = load_ticker_detail(symbol)
    if detail is None:
        st.error(f"{symbol} introuvable dans `universe`.")
        return None

    # Display-only EUR conversion (see dashboard/currency.py): the raw price
    # in price_history is never touched, only what's shown here.
    devise = detail["devise"]
    rate = load_exchange_rate(devise)
    price_help = None if devise == "EUR" else (
        f"Converti depuis {devise} (taux du jour)" if rate is not None
        else f"Taux {devise}->EUR indisponible : montant affiche en {devise} d'origine"
    )

    def _fmt(value, fmt="{:.1f}"):
        return fmt.format(value) if value is not None else "N/A"

    c1, c2, c3 = st.columns(3)
    c1.metric("Current price", format_amount(detail["current_price"], devise, rate),
              help=price_help)
    c2.metric("MA 50", format_amount(detail["ma_50"], devise, rate),
              help=GLOSSAIRE["MA 50"] + (f" {price_help}" if price_help else ""))
    c3.metric("MA 200", format_amount(detail["ma_200"], devise, rate),
              help=GLOSSAIRE["MA 200"] + (f" {price_help}" if price_help else ""))

    c4, c5, c6 = st.columns(3)
    volume = detail["volume"]
    c4.metric("Volume", f"{int(volume):,}" if volume is not None else "N/A",
              help=GLOSSAIRE["Volume"])
    c5.metric("Volatility", _fmt(detail["volatility"], "{:.2%}"), help=GLOSSAIRE["Volatility"])
    rsi = detail["rsi"]
    rsi_label = (_fmt(rsi) + ("" if detail["rsi_is_real"] else " (proxy)")) if rsi is not None else "N/A"
    c6.metric("RSI (14)", rsi_label, help=GLOSSAIRE["RSI"])

    st.markdown("**Scores**")
    s1, s2, s3 = st.columns(3)
    s1.metric("Prix/Valorisation", _fmt(detail["price_valuation_score"]),
              help=GLOSSAIRE["Prix/Valorisation"])
    s2.metric("Technical", _fmt(detail["technical_score"]), help=GLOSSAIRE["Technical"])
    s3.metric("Fondamental reel", _fmt(detail["score_fondamental_reel"]),
              help=GLOSSAIRE["Fondamental reel"])
    s4, s5, s6 = st.columns(3)
    s4.metric("Volatility score", _fmt(detail["volatility_score"]), help=GLOSSAIRE["Volatility"])
    s5.metric("Volume score", _fmt(detail["volume_score"]), help=GLOSSAIRE["Volume"])
    confidence = detail["confidence"]
    s6.metric("Confidence", _fmt(confidence, "{:.0f}%"), help=GLOSSAIRE["Confidence"])

    return symbol


def render_chart(symbol):
    st.subheader("Prix & moyennes mobiles")
    detail = load_ticker_detail(symbol)
    prices = detail["history"] if detail else pd.DataFrame()

    if prices.empty:
        st.info(
            "No price history for this stock yet. Run "
            f"`python ingestion/ingest_universe_prices.py --tickers {symbol}` "
            "to populate `price_history`."
        )
        return

    prices = prices.copy()
    prices["date"] = pd.to_datetime(prices["date"])
    prices = prices.sort_values("date")
    # True moving averages from the real daily closes (NaN until the window
    # fills, so plotly simply starts each line where it becomes valid).
    prices["ma_50"] = prices["close"].rolling(50).mean()
    prices["ma_200"] = prices["close"].rolling(200).mean()

    # Display-only EUR conversion (see dashboard/currency.py): applied to the
    # whole series at once (vectorised multiply), not per data point. The
    # underlying price_history table is never touched.
    devise = detail["devise"]
    if devise == "EUR":
        price_unit = "€"
    else:
        rate = load_exchange_rate(devise)
        if rate is not None:
            prices[["close", "ma_50", "ma_200"]] *= rate
            price_unit = "€"
        else:
            price_unit = CURRENCY_SYMBOLS.get(devise, devise)

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
        xaxis_title="Date", yaxis_title=f"Price ({price_unit})",
        height=470, margin=dict(t=50, b=70),
        # Legend below the plot (not above, alongside the title) -- the
        # previous y=1.02/x=0 placement shared the same top-left corner as
        # the title, so the two visually overlapped.
        legend=dict(orientation="h", yanchor="top", y=-0.22, xanchor="center", x=0.5),
        hovermode="x unified",
    )
    st.plotly_chart(fig, use_container_width=True)


def _entreprises_a_surveiller_block(surveiller):
    st.markdown(term_span("Entreprises a surveiller", "Entreprises a surveiller"),
                unsafe_allow_html=True)
    for rtype, names in surveiller.items():
        st.markdown(f"- **{rtype}** : {', '.join(names)}")


def render_ai_analysis_section(symbol):
    """On-demand Groq-argued text for ANY ticker on this page -- generalises
    reasoning/daily_summary.py's Resume-du-jour mechanism (same build_signal,
    same add_argued_texts call) beyond just the daily top-3 signals, but
    draws from its OWN daily quota pool (USAGE_TABLE_TICKER_ANALYSIS,
    TICKER_ANALYSIS_DAILY_LIMIT=10/day) instead of Resume du jour's
    (DAILY_LLM_CALL_LIMIT=3/day) -- browsing dozens of tickers/day here can
    never exhaust, or be exhausted by, Resume du jour's own budget. Both
    still share the same daily_summary_arguments cache table/key, so a
    ticker generated via either entry point is reused for free by the
    other. Never calls Groq on page load: the cache (today, ticker) is
    checked FIRST, and Groq is only ever reached via an explicit button
    click."""
    st.subheader("Analyse argumentee (IA)")

    if not os.path.exists(DB_PATH):
        return
    conn = sqlite3.connect(DB_PATH)
    try:
        opp_row = load_opportunite_for_ticker(conn, symbol)
        if opp_row is None:
            st.info(
                "Pas de donnees d'opportunite pour ce ticker (score global, "
                "technique, fondamental, news). Lance "
                "`python reasoning/opportunity_scoring.py --priorite toutes` "
                "pour l'inclure."
            )
            return

        relations = load_relations()
        graph = build_graph(relations)
        signal = build_signal(conn, opp_row, graph, relations)

        today = date.today().isoformat()
        cached = load_cached_argument(conn, today, symbol)

        if cached:
            st.markdown(f"##### {highlight_terms(cached)}", unsafe_allow_html=True)
        else:
            st.caption("Analyse IA pas encore generee pour ce ticker aujourd'hui.")
            if st.button("Generer l'analyse", key=f"gen_ai_{symbol}"):
                with st.spinner("Generation en cours (Groq)..."):
                    add_argued_texts(conn, [signal], usage_table=USAGE_TABLE_TICKER_ANALYSIS,
                                      call_limit=TICKER_ANALYSIS_DAILY_LIMIT)
                if signal.get("texte_argumente"):
                    st.markdown(f"##### {highlight_terms(signal['texte_argumente'])}",
                                unsafe_allow_html=True)
                else:
                    st.warning(
                        "Generation indisponible pour l'instant (quota Groq du jour "
                        "atteint, cle API absente, ou erreur reseau) -- voir le detail "
                        "structure ci-dessous."
                    )

        with st.expander("Detail des composantes (donnees structurees)"):
            c1, c2 = st.columns(2)
            c1.metric("Score ajuste", f"{signal['score_ajuste']:.1f}",
                      help=GLOSSAIRE["Score ajuste"])
            c2.metric("Confiance", f"{signal['confiance']:.0f}%", help=GLOSSAIRE["Confiance"])
            st.markdown(highlight_terms(signal["explication"]), unsafe_allow_html=True)
            risk_tip = html.escape(GLOSSAIRE["Risque"], quote=True)
            conflict_note = (" - composantes structurelles en contradiction"
                             if signal["conflit_composantes"] else "")
            st.markdown(
                f"<span style='cursor:help;border-bottom:1px dotted #6b7280;' "
                f"title=\"{risk_tip}\">Risque : {signal['risque']}</span>"
                f"<span style='font-size:0.85em;color:gray;'>{conflict_note}</span>",
                unsafe_allow_html=True,
            )
            if signal["volatilite"] is not None:
                st.caption(f"Volatilite annualisee : {signal['volatilite']:.0%}")
            if signal["entreprises_a_surveiller"]:
                _entreprises_a_surveiller_block(signal["entreprises_a_surveiller"])
    finally:
        conn.close()


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


# --- Knowledge graph -------------------------------------------------------

from graph.build_graph import build_graph, direct_relations  # noqa: E402
from graph.build_graph import load_relations as _load_relation_rows  # noqa: E402


@st.cache_data(show_spinner=False)
def load_relations():
    """Return relation rows (list of dicts); empty list if none/absent."""
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = _load_relation_rows(conn)
        conn.close()
    except (sqlite3.Error, pd.errors.DatabaseError):
        return []
    return rows


@st.cache_data(show_spinner=False)
def load_universe_names():
    """{ticker: display_name} for every tracked ticker, same priority as
    elsewhere: nom_entreprise (yfinance), falling back to nom, falling back
    to the ticker itself. Used to label Knowledge Graph "primary" nodes with
    a real company name instead of the bare ticker."""
    if not os.path.exists(DB_PATH):
        return {}
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT ticker, COALESCE(NULLIF(nom_entreprise, ticker), "
            "NULLIF(nom, ''), ticker) FROM universe"
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return {}
    return dict(rows)


def _graph_html(graph, names):
    """Render the networkx graph to a self-contained interactive HTML (pyvis).

    Primary (tracked) nodes are labelled "TICKER - Company Name" using
    `names` (falls back to the bare ticker if not found) instead of just the
    ticker, so the graph is as readable as the external nodes (which already
    carry a real company name from the relations data)."""
    # pyvis renders into a self-contained iframe (components.html), which the
    # page's global CSS (dashboard/style.css) cannot reach -- colours must be
    # set here directly so the graph matches the dark "Jarvis" theme instead
    # of showing up as a stark white rectangle on the dark page.
    from pyvis.network import Network
    net = Network(height="560px", width="100%", directed=True,
                  cdn_resources="in_line", bgcolor="#0b111a",
                  font_color="#dce8f5")
    net.repulsion(node_distance=160, spring_length=140)
    for node, d in graph.nodes(data=True):
        primary = d["kind"] == "primary"
        if primary:
            display_name = names.get(node, node)
            label = f"{node} - {display_name}" if display_name != node else node
            title = f"{display_name} ({node})"
        else:
            label = d["label"]
            title = f"{d['label']} ({d['ticker']})" if d["ticker"] else d["label"]
        net.add_node(node, label=label, title=title,
                     color="#22d3ee" if primary else "#94a3b8",
                     size=26 if primary else 16)
    for u, v, d in graph.edges(data=True):
        net.add_edge(u, v, label=d["relation"], title=d.get("notes", ""),
                     color="#3b82a6")
    html_out = net.generate_html(notebook=False)
    # pyvis only colours the #mynetwork container itself; the surrounding
    # <body> keeps the browser's default white margin, showing up as a thin
    # bright edge around an otherwise dark graph. One extra style rule closes
    # that gap without touching pyvis's own generated markup.
    html_out = html_out.replace(
        "</head>", "<style>body{background:#0b111a;margin:0;}</style></head>", 1
    )
    # Mouse-only zoom/pan (wheel + drag) has no visible affordance and is easy
    # to get lost in. pyvis's own template calls drawGraph() synchronously
    # (see the script right before </body>), so the global `network` variable
    # already holds the live vis-network instance by the time this script
    # runs -- no need to wait for a load event. moveTo({scale}) and fit() are
    # vis-network's real public API (there is no network.zoom() method).
    controls = """
<div style="position:fixed;top:12px;right:12px;z-index:1000;
            display:flex;flex-direction:column;gap:6px;">
  <button onclick="__graphZoomIn()" title="Zoom avant" class="graph-ctrl-btn">+</button>
  <button onclick="__graphZoomOut()" title="Zoom arriere" class="graph-ctrl-btn">&minus;</button>
  <button onclick="__graphRecenter()" title="Recentrer la vue" class="graph-ctrl-btn">&#10021;</button>
</div>
<style>
.graph-ctrl-btn {
    width: 32px; height: 32px;
    background: rgba(11,17,26,0.85);
    border: 1px solid rgba(95,227,255,0.45);
    color: #5fe3ff;
    border-radius: 6px;
    font-size: 16px;
    font-family: 'Share Tech Mono', monospace, sans-serif;
    cursor: pointer;
    box-shadow: 0 0 8px rgba(34,211,238,0.15);
}
.graph-ctrl-btn:hover {
    background: rgba(11,17,26,1);
    border-color: #7ee8ff;
    box-shadow: 0 0 14px rgba(34,211,238,0.4);
}
</style>
<script>
function __graphZoomIn() {
    if (typeof network === "undefined" || !network) return;
    network.moveTo({scale: network.getScale() * 1.25,
                     animation: {duration: 200, easingFunction: "easeInOutQuad"}});
}
function __graphZoomOut() {
    if (typeof network === "undefined" || !network) return;
    network.moveTo({scale: network.getScale() * 0.8,
                     animation: {duration: 200, easingFunction: "easeInOutQuad"}});
}
function __graphRecenter() {
    if (typeof network === "undefined" || !network) return;
    network.fit({animation: {duration: 300, easingFunction: "easeInOutQuad"}});
}
</script>
"""
    return html_out.replace("</body>", controls + "</body>", 1)


def render_graph_page():
    st.subheader("Knowledge Graph")
    relations = load_relations()
    if not relations:
        st.info(
            "Aucune relation enregistree. Lance "
            "`python graph/import_relations.py`."
        )
        return

    # "Primary" (highlighted) nodes reflect TODAY's best opportunites --
    # not a fixed 10-pilot-ticker list -- so the graph's emphasis moves with
    # the data instead of always spotlighting the same tickers. Reuses
    # load_opportunites(), which now resolves the latest date_calcul PER
    # universe.priorite tier independently (see OPPORTUNITES_SQL's own
    # docstring) -- the top 10 can therefore span tiers refreshed on
    # different days, so its own date label is shown per-date, not assumed
    # to be a single value.
    opp_df, _ = load_opportunites()
    top_tickers = list(opp_df["ticker"].head(10)) if not opp_df.empty else []

    graph = build_graph(relations, tracked=set(top_tickers))
    n_primary = sum(1 for _, d in graph.nodes(data=True) if d["kind"] == "primary")
    n_external = graph.number_of_nodes() - n_primary
    st.caption(f"{graph.number_of_nodes()} noeuds ({n_primary} suivis, "
               f"{n_external} externes) - {graph.number_of_edges()} relations")
    if top_tickers:
        top_dates = sorted(set(opp_df["date_calcul"].head(10)))
        date_label = top_dates[0] if len(top_dates) == 1 else f"dates variees ({', '.join(top_dates)})"
        st.caption(
            f"Noeuds mis en avant = top 10 opportunites (donnees du {date_label}) : "
            + ", ".join(top_tickers)
        )

    try:
        import streamlit.components.v1 as components
        components.html(_graph_html(graph, load_universe_names()), height=580, scrolling=False)
    except Exception as exc:  # noqa: BLE001 - pyvis missing / render issue
        st.warning(f"Graphe interactif indisponible ({exc}). "
                   "Relations en texte ci-dessous.")

    st.divider()
    st.markdown("**Relations directes par ticker**")
    tickers = sorted({r["source_ticker"].strip() for r in relations})
    names_by_ticker = load_universe_names()
    # Default the picker to today's #1 opportunite if it actually has
    # relations data; otherwise fall back to the first ticker alphabetically
    # (Streamlit's own default) rather than forcing a choice with no data.
    default_index = 0
    for t in top_tickers:
        if t in tickers:
            default_index = tickers.index(t)
            break
    ticker = st.selectbox(
        "Ticker", tickers, key="graph_ticker", index=default_index,
        format_func=lambda t: f"{t} - {names_by_ticker.get(t, t)}"
                              if names_by_ticker.get(t, t) != t else t,
    )
    grouped = direct_relations(relations, ticker)
    if not grouped:
        st.write(f"{ticker} : aucune relation directe connue.")
    else:
        for rtype, names in grouped.items():
            st.markdown(f"- **{rtype}** : {', '.join(names)}")


# --- Opportunites du jour ---------------------------------------------------

# nom_affiche: nom_entreprise (yfinance) in priority, falling back to nom
# (scraped at universe-build time) when nom_entreprise is null/empty OR fell
# back to the bare ticker itself (see universe/fetch_company_names.py),
# falling back to the ticker as the last resort. Never null.
#
# The `latest` subquery resolves the freshest date_calcul PER priorite tier
# independently (mirrors reasoning.daily_summary.resolve_data_dates_by_
# priority) instead of a single WHERE date_calcul = MAX(date_calcul) across
# the whole table -- that single-global-date form silently drops an entire
# tier the moment another tier gets recomputed more recently (observed for
# real: "basse" refreshed daily during the Knowledge Graph extension work
# while "haute"/"moyenne" sat 5 days stale, making them vanish from this
# page and from the Knowledge Graph page's "top 10" highlight entirely).
OPPORTUNITES_SQL = """
SELECT o.ticker,
       COALESCE(NULLIF(u.nom_entreprise, o.ticker), NULLIF(u.nom, ''), o.ticker) AS nom_affiche,
       o.score_global, o.score_prix_valorisation, o.score_technique,
       o.score_news, o.score_fondamental_reel, o.explication, o.confiance,
       o.date_calcul, u.priorite
FROM opportunites o
JOIN universe u ON u.ticker = o.ticker
JOIN (
    SELECT u2.priorite AS priorite, MAX(o2.date_calcul) AS max_date
    FROM opportunites o2 JOIN universe u2 ON u2.ticker = o2.ticker
    GROUP BY u2.priorite
) latest ON latest.priorite = u.priorite AND latest.max_date = o.date_calcul
ORDER BY (o.score_global IS NULL), o.score_global DESC;
"""


@st.cache_data(show_spinner=False)
def load_opportunites():
    """Return (df, error). Empty df (no error) if the table doesn't exist yet."""
    if not os.path.exists(DB_PATH):
        return pd.DataFrame(), None
    try:
        conn = sqlite3.connect(DB_PATH)
        df = pd.read_sql_query(OPPORTUNITES_SQL, conn)
        conn.close()
    except (sqlite3.Error, pd.errors.DatabaseError):
        return pd.DataFrame(), None
    return df, None


@st.cache_data(show_spinner=False)
def load_universe_priorities():
    """All distinct priorite values in universe (not just the ones already
    scored in `opportunites`), so the filter always offers every tier even
    before opportunity_scoring.py has been run for it."""
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT DISTINCT priorite FROM universe "
            "WHERE priorite IS NOT NULL ORDER BY priorite"
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    return [r[0] for r in rows]


def render_opportunities_page():
    st.subheader("Opportunites du jour")
    df, error = load_opportunites()
    if error:
        st.error(error)
        return
    if df.empty:
        st.info(
            "Aucune opportunite calculee. Lance "
            "`python reasoning/opportunity_scoring.py --priorite haute`."
        )
        return

    # Each priorite tier is already resolved to its OWN latest date_calcul
    # (see OPPORTUNITES_SQL's docstring), so `df` can legitimately mix dates
    # across tiers -- show the single date when they agree, or the same
    # per-tier freshness breakdown as Resume du jour when they diverge,
    # rather than silently picking one row's date as if it applied to all.
    dates_by_priority = df.groupby("priorite")["date_calcul"].max().to_dict()
    if len(set(dates_by_priority.values())) == 1:
        st.caption(f"Calcule le {next(iter(dates_by_priority.values()))} - {len(df)} tickers")
    else:
        st.caption(f"{len(df)} tickers")
        note = staleness_summary(dates_by_priority)
        if note:
            st.warning(note)

    # A fixed, tiny set of options (4 tiers) is a click choice, not something
    # to search for -- st.pills (pure click, no text input) avoids the false
    # "you can type here" affordance a searchable st.selectbox gives for a
    # list this short.
    priorites = ["toutes"] + load_universe_priorities()
    choice = st.pills("Priorite univers", priorites, key="opp_priorite",
                       default="toutes", required=True, help=GLOSSAIRE["Priorite"])
    sub = df if choice == "toutes" else df[df["priorite"] == choice]

    if sub.empty:
        st.warning(
            f"Aucune opportunite calculee pour la priorite '{choice}'. "
            f"Lance `python reasoning/opportunity_scoring.py --priorite {choice}`."
        )
        return

    table = sub[[
        "ticker", "nom_affiche", "priorite", "score_global", "score_prix_valorisation",
        "score_technique", "score_news", "score_fondamental_reel", "confiance",
    ]].rename(columns={
        "ticker": "Ticker", "nom_affiche": "Nom", "priorite": "Priorite",
        "score_global": "Score global", "score_prix_valorisation": "Prix/Valo",
        "score_technique": "Technique", "score_news": "News",
        "score_fondamental_reel": "Fondamental reel", "confiance": "Confiance",
    })

    def color_row(row):
        bg = score_color(row["Score global"])
        return [f"background-color: {bg}; color: white;"] * len(row)

    styled = (
        table.style
        .apply(color_row, axis=1)
        .format({
            "Score global": "{:.1f}", "Prix/Valo": "{:.1f}",
            "Technique": "{:.1f}", "News": "{:.1f}",
            "Fondamental reel": "{:.1f}", "Confiance": "{:.0f}%",
        }, na_rep="n/a")
    )
    st.dataframe(styled, use_container_width=True, hide_index=True)
    st.caption("Trie par score global decroissant. "
               "Vert: score > 60 - Orange: 40-60 - Rouge: < 40")

    st.divider()
    st.markdown("**Explication detaillee par ticker**")
    tickers = sub["ticker"].tolist()
    if not tickers:
        st.write("Aucun ticker pour cette priorite.")
        return
    ticker = st.selectbox("Ticker", tickers, key="opp_ticker")
    row = sub[sub["ticker"] == ticker].iloc[0]

    st.markdown(f"#### {row['ticker']} -- {row['nom_affiche']}")
    c1, c2, c3 = st.columns(3)
    score = row["score_global"]
    c1.metric("Score global", f"{score:.1f}" if pd.notna(score) else "n/a",
              help=GLOSSAIRE["Score global"])
    c2.metric("Confiance", f"{row['confiance']:.0f}%", help=GLOSSAIRE["Confiance"])
    c3.metric("Priorite univers", row["priorite"], help=GLOSSAIRE["Priorite"])
    if pd.notna(row["explication"]):
        st.markdown(highlight_terms(row["explication"]), unsafe_allow_html=True)
    else:
        st.write(row["explication"])


# --- Resume du jour ----------------------------------------------------------

RISK_COLOR = {"Faible": COLOR_GOOD, "Modere": COLOR_MID, "Eleve": COLOR_BAD}


@st.cache_data(show_spinner=False)
def load_daily_summary():
    """Return (signals, dates_by_priority, n_candidates, error).
    dates_by_priority is {priorite: date_calcul} -- see
    reasoning.daily_summary.build_daily_summary's docstring: candidates are
    drawn from EVERY universe.priorite tier at its own latest date_calcul,
    not a single global-max date that would silently hide whichever tier
    was recomputed less recently than another."""
    if not os.path.exists(DB_PATH):
        return [], {}, 0, None
    try:
        conn = sqlite3.connect(DB_PATH)
        signals, dates_by_priority, n_candidates = build_daily_summary(conn)
        add_argued_texts(conn, signals)
        conn.close()
    except sqlite3.Error as exc:
        return [], {}, 0, str(exc)
    return signals, dates_by_priority, n_candidates, None


def render_daily_summary_page():
    st.subheader("Resume du jour")
    signals, dates_by_priority, n_candidates, error = load_daily_summary()
    if error:
        st.error(error)
        return
    if not dates_by_priority:
        st.info(
            "Aucune opportunite calculee pour l'instant. Lance "
            "`python reasoning/opportunity_scoring.py --priorite haute` "
            "puis reviens sur cette page."
        )
        return

    if len(set(dates_by_priority.values())) == 1:
        date_label = next(iter(dates_by_priority.values()))
    else:
        date_label = "plusieurs dates par palier (voir ci-dessous)"
    st.caption(
        f"{date_label} - {len(signals)} signal(aux) retenu(s) sur {n_candidates} "
        f"candidat(s) eligible(s) (confiance >= {MIN_CONFIDENCE:.0f}%)"
    )
    note = staleness_summary(dates_by_priority)
    if note:
        st.warning(note)

    if not signals:
        st.warning(
            "Aucun signal ne depasse le seuil de confiance minimal aujourd'hui. "
            "Qualite avant quantite : mieux vaut aucun signal qu'un mauvais "
            "choix force."
        )
        return

    for rank, s in enumerate(signals, start=1):
        with st.container(border=True):
            c1, c2, c3 = st.columns([2, 1, 1])
            c1.markdown(f"### #{rank}  {s['ticker']} -- {s['nom_affiche']}")
            c2.metric("Score ajuste", f"{s['score_ajuste']:.1f}",
                      help=GLOSSAIRE["Score ajuste"])
            c3.metric("Confiance", f"{s['confiance']:.0f}%", help=GLOSSAIRE["Confiance"])

            if s.get("texte_argumente"):
                st.markdown(f"##### {highlight_terms(s['texte_argumente'])}",
                            unsafe_allow_html=True)

            risk_bg = RISK_COLOR.get(s["risque"], COLOR_MID)
            risk_tip = html.escape(GLOSSAIRE["Risque"], quote=True)
            conflict_note = " - composantes structurelles en contradiction" if s["conflit_composantes"] else ""
            st.markdown(
                f"<span style='background-color:{risk_bg};color:white;"
                f"padding:2px 10px;border-radius:12px;font-size:0.85em;"
                f"cursor:help;' title=\"{risk_tip}\">"
                f"Risque : {s['risque']}</span>"
                f"<span style='font-size:0.85em;color:gray;'>{conflict_note}</span>",
                unsafe_allow_html=True,
            )
            st.caption(s["horizon"])

            if s.get("texte_argumente"):
                with st.expander("Detail des composantes (donnees structurees)"):
                    st.markdown(highlight_terms(s["explication"]), unsafe_allow_html=True)
                    if s["volatilite"] is not None:
                        st.caption(f"Volatilite annualisee : {s['volatilite']:.0%}")
                    if s["entreprises_a_surveiller"]:
                        _entreprises_a_surveiller_block(s["entreprises_a_surveiller"])
            else:
                st.markdown(highlight_terms(s["explication"]), unsafe_allow_html=True)
                if s["volatilite"] is not None:
                    st.caption(f"Volatilite annualisee : {s['volatilite']:.0%}")
                if s["entreprises_a_surveiller"]:
                    _entreprises_a_surveiller_block(s["entreprises_a_surveiller"])


# --- Raisonnement causal (module 7) -------------------------------------------

CAUSAL_CHAINS_SQL = """
SELECT c.id, c.news_id, c.ticker_source, c.chaine_raisonnement,
       c.entreprises_impactees, c.confiance, c.model, c.created_at,
       r.title AS news_title
FROM causal_chains c
LEFT JOIN news_raw r ON r.id = c.news_id
ORDER BY c.created_at DESC
LIMIT ?;
"""

CAUSAL_CHAIN_DISPLAY_LIMIT = 50


@st.cache_data(show_spinner=False)
def load_causal_chains(limit=CAUSAL_CHAIN_DISPLAY_LIMIT):
    """Return (chains, error). `chains` is a list of dicts, most recently
    generated first -- never restricted to a single "today" date the way
    Resume du jour is: causal_reasoning.py runs on its own limited quota
    (see reasoning/causal_reasoning.py's CAUSAL_REASONING_DAILY_LIMIT), so a
    chain generated a few days ago is still the right thing to show, not a
    reason to show nothing."""
    if not os.path.exists(DB_PATH):
        return [], None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(CAUSAL_CHAINS_SQL, (limit,)).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        return [], str(exc)
    return [dict(r) for r in rows], None


def _parse_entreprises_impactees(raw_json):
    """[] on anything that isn't a valid JSON array -- never crash the page
    over a malformed cell."""
    if not raw_json:
        return []
    try:
        parsed = json.loads(raw_json)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


EFFET_COLOR = {"positif": COLOR_GOOD, "negatif": COLOR_BAD, "neutre": COLOR_MID}


def render_causal_reasoning_page():
    st.subheader("Raisonnement causal")
    chains, error = load_causal_chains()
    if error:
        st.error(error)
        return
    if not chains:
        st.info(
            "Aucune chaine de raisonnement causal generee pour l'instant. "
            "Lance `python reasoning/causal_reasoning.py` (limite par un "
            "quota Groq quotidien dedie) puis reviens sur cette page."
        )
        return

    latest_date = (chains[0]["created_at"] or "")[:10]
    note = staleness_note(latest_date) if latest_date else None
    if note:
        st.warning(note)

    st.caption(
        f"{len(chains)} chaine(s) de raisonnement causal disponible(s) "
        f"(la plus recente : {latest_date}), triees par date decroissante."
    )

    for chain in chains:
        chain_date = (chain["created_at"] or "")[:10]
        with st.container(border=True):
            c1, c2 = st.columns([3, 1])
            c1.markdown(f"### {chain['ticker_source']}  --  {chain_date}")
            if chain["confiance"] is not None:
                c2.metric("Confiance", f"{chain['confiance']:.0f}%",
                          help=GLOSSAIRE["Confiance"])

            if chain.get("news_title"):
                st.caption(f"News d'origine : {chain['news_title']}")
            else:
                st.caption(f"News d'origine : news_id={chain['news_id']} "
                          f"(titre indisponible)")

            st.markdown(highlight_terms(chain["chaine_raisonnement"]),
                        unsafe_allow_html=True)

            impactees = _parse_entreprises_impactees(chain["entreprises_impactees"])
            if impactees:
                with st.expander(f"Entreprises impactees ({len(impactees)})"):
                    for entry in impactees:
                        effet = str(entry.get("effet") or "neutre").strip().lower()
                        color = EFFET_COLOR.get(effet, COLOR_MID)
                        nom = html.escape(str(entry.get("entreprise") or ""), quote=True)
                        ticker = entry.get("ticker")
                        ticker_part = f" ({html.escape(str(ticker), quote=True)})" if ticker else ""
                        st.markdown(
                            f"<span style='background-color:{color};color:white;"
                            f"padding:2px 10px;border-radius:12px;font-size:0.85em;'>"
                            f"{nom}{ticker_part} -- {effet}</span>",
                            unsafe_allow_html=True,
                        )
            st.caption(f"Modele : {chain['model']}" if chain.get("model") else "")


# --- Correlations decouvertes (module 8) --------------------------------------

CORRELATIONS_SQL = """
SELECT ticker_source, ticker_target, relation_type, source_table, lag,
       lag_direction, coefficient, p_value, p_value_corrigee, n_observations,
       methode, correction, created_at
FROM correlations_discovered
ORDER BY ABS(coefficient) DESC;
"""


@st.cache_data(show_spinner=False)
def load_correlations():
    """Return (correlations, error). Each dict also carries the two
    tickers' real display names (nom_source/nom_target) so the page never
    shows a bare, unfamiliar ticker with no context."""
    if not os.path.exists(DB_PATH):
        return [], None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(CORRELATIONS_SQL).fetchall()]
        for row in rows:
            row["nom_source"] = load_display_name(conn, row["ticker_source"])
            row["nom_target"] = load_display_name(conn, row["ticker_target"])
        conn.close()
    except sqlite3.Error as exc:
        return [], str(exc)
    return rows, None


def _format_lag_direction(row):
    """Plain-language description of the lag/direction pair -- never uses
    "predit"/"cause" wording (see render_correlations_page's own caution
    note): always framed as an observed co-movement pattern, not a forecast."""
    direction = row["lag_direction"]
    if direction == "simultane":
        return "Simultanee (meme jour de bourse)"
    lag_days = abs(row["lag"])
    if direction == "source_precede_target":
        return (f"{row['ticker_source']} en avance sur {row['ticker_target']} "
                f"de {lag_days} jour(s) de bourse")
    return (f"{row['ticker_target']} en avance sur {row['ticker_source']} "
            f"de {lag_days} jour(s) de bourse")


def render_correlations_page():
    st.subheader("Correlations decouvertes")
    st.info(
        "Correlation statistique observee sur l'historique disponible -- "
        "**ce n'est pas une preuve de causalite**. Deux actions peuvent "
        "evoluer ensemble pour bien d'autres raisons qu'un lien economique "
        "direct : secteur commun, sentiment de marche general, ou simple "
        "coincidence statistique. Ces resultats servent a orienter "
        "l'attention vers des paires deja liees dans le graphe de "
        "connaissances -- jamais a predire un mouvement futur."
    )

    correlations, error = load_correlations()
    if error:
        st.error(error)
        return
    if not correlations:
        st.info(
            "Aucune correlation calculee pour l'instant. Lance "
            "`python reasoning/correlation_discovery.py --source relations` "
            "puis reviens sur cette page."
        )
        return

    st.caption(
        f"{len(correlations)} correlation(s) retenue(s) ({term_span('p-value corrigee', 'P-value')} "
        f"&lt; 0.05, apres correction pour tests multiples), triees par force "
        f"de correlation decroissante.",
        unsafe_allow_html=True,
    )

    for row in correlations:
        with st.container(border=True):
            st.markdown(
                f"### {row['ticker_source']} ({row['nom_source']})  &harr;  "
                f"{row['ticker_target']} ({row['nom_target']})",
                unsafe_allow_html=True,
            )
            st.caption(
                f"Relation d'origine : {row['relation_type']} "
                f"(source : {'Knowledge Graph valide' if row['source_table'] == 'relations' else row['source_table']})"
            )

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Coefficient (Spearman)", f"{row['coefficient']:+.3f}",
                      help=GLOSSAIRE["Correlation"])
            c2.metric("P-value corrigee", f"{row['p_value_corrigee']:.4g}",
                      help=GLOSSAIRE["P-value"])
            c3.metric("Lag", _format_lag_direction(row), help=GLOSSAIRE["Lag"])
            c4.metric("Observations", str(row["n_observations"]))

            st.caption(
                f"Methode : {row['methode']} -- correction : {row['correction']} "
                f"-- {term_span('significativite statistique', 'Significativite statistique')}",
                unsafe_allow_html=True,
            )


# --- Pages ------------------------------------------------------------------

def _get_scored_data():
    """Load scored data or stop the page with an error banner."""
    df, history, error = load_data()
    if error:
        st.error(error)
        st.stop()
    return df, history


def page_daily_summary():
    """Today's strongest advisory signals (price/valuation+technical+news, no LLM)."""
    render_daily_summary_page()


def page_overview():
    """Top 10 ranking + global statistics."""
    df, _ = _get_scored_data()
    render_top10(df)
    st.divider()
    render_stats(df)


def page_stock():
    """Per-stock detail + price/MA chart + on-demand AI analysis -- covers
    the full universe (~1900 tickers), not just the 10 legacy pilots."""
    symbol = render_detail()
    if symbol is None:
        return
    st.divider()
    render_chart(symbol)
    st.divider()
    render_ai_analysis_section(symbol)


def page_news():
    """News feed with LLM analysis."""
    render_news_page()


def page_graph():
    """Knowledge graph of ticker relations."""
    render_graph_page()


def page_opportunities():
    """Aggregated opportunity scores (price/valuation + technical + news, no LLM)."""
    render_opportunities_page()


def page_causal_reasoning():
    """Causal reasoning chains (reasoning/causal_reasoning.py, module 7) --
    indirect consequence chains grounded in the Knowledge Graph."""
    render_causal_reasoning_page()


def page_correlations():
    """Discovered correlations (reasoning/correlation_discovery.py, module 8)
    -- statistically retained co-movements between already-known related
    tickers, never framed as prediction or causation."""
    render_correlations_page()


# --- Main ------------------------------------------------------------------

def main():
    st.set_page_config(page_title="Market Intelligence Dashboard", layout="wide")
    inject_style()
    st.title("Market Intelligence Dashboard")

    pages = [
        st.Page(page_daily_summary, title="Resume du jour", icon=":material/bolt:",
                url_path="daily-summary", default=True),
        st.Page(page_overview, title="Vue d'ensemble", icon=":material/leaderboard:",
                url_path="overview"),
        st.Page(page_stock, title="Analyse d'une action", icon=":material/query_stats:",
                url_path="stock"),
        st.Page(page_news, title="News & Analyse IA", icon=":material/newspaper:",
                url_path="news"),
        st.Page(page_graph, title="Knowledge Graph", icon=":material/hub:",
                url_path="graph"),
        st.Page(page_opportunities, title="Opportunites du jour",
                icon=":material/trending_up:", url_path="opportunities"),
        st.Page(page_causal_reasoning, title="Raisonnement causal",
                icon=":material/account_tree:", url_path="causal-reasoning"),
        st.Page(page_correlations, title="Correlations decouvertes",
                icon=":material/scatter_plot:", url_path="correlations"),
    ]
    nav = st.navigation(pages)

    with st.sidebar:
        if st.button("Refresh Data"):
            load_data.clear()
            load_news.clear()
            load_relations.clear()
            load_opportunites.clear()
            load_universe_priorities.clear()
            load_daily_summary.clear()
            load_universe_names.clear()
            st.rerun()

    nav.run()


if __name__ == "__main__":
    main()
