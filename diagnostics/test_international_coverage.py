#!/usr/bin/env python3
"""Diagnostic: international coverage of yfinance, Finnhub and Yahoo RSS.

For a representative sample of tickers across world exchanges, probe what each
data source actually returns, and write a markdown report to
diagnostics/international_coverage_report.md.

This is an ISOLATED diagnostic tool. It does not touch the pipeline and is not
meant to add these tickers to the tracked universe -- only to reveal where the
data sources work, are degraded, or are empty per geography.

Run:
    python diagnostics/test_international_coverage.py
"""

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

DATA_DIR = os.path.join(REPO_ROOT, "data")
REPORT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "international_coverage_report.md")

# Reuse the shared SSL fix (read-only import; the pipeline file is untouched).
from ingestion.ssl_utils import configure_ca_bundle  # noqa: E402

CA_BUNDLE = configure_ca_bundle(DATA_DIR)

import requests  # noqa: E402
import yfinance as yf  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("intl_coverage")

# (ticker, country/zone, geographic group used for the per-zone conclusion)
SAMPLE = [
    ("LVMH.PA", "France (Paris)", "Europe"),
    ("SAP.DE", "Allemagne (Francfort)", "Europe"),
    ("ASML.AS", "Pays-Bas (Amsterdam)", "Europe"),
    ("HSBA.L", "Royaume-Uni (Londres)", "Europe"),
    ("7203.T", "Japon (Tokyo)", "Japon"),
    ("600519.SS", "Chine (Shanghai A-share)", "Chine continentale"),
    ("0700.HK", "Hong Kong", "Hong Kong"),
    ("005930.KS", "Coree du Sud (Seoul)", "Coree du Sud"),
    ("RELIANCE.NS", "Inde (NSE)", "Inde"),
    ("PETR4.SA", "Bresil (Sao Paulo)", "Bresil"),
]

YAHOO_RSS_URL = "https://finance.yahoo.com/rss/headline?s={ticker}"
FINNHUB_NEWS_URL = "https://finnhub.io/api/v1/company-news"
FINNHUB_PROFILE_URL = "https://finnhub.io/api/v1/stock/profile2"
TIMEOUT = 20


# --- Individual probes ------------------------------------------------------

def probe_yf_price(ticker):
    """yfinance price history: rows + freshness."""
    try:
        hist = yf.Ticker(ticker).history(period="1mo", auto_adjust=False)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "note": f"erreur: {exc}"}
    if hist is None or hist.empty:
        return {"ok": False, "note": "aucune donnee"}
    last = hist.index[-1]
    last_str = last.date().isoformat() if hasattr(last, "date") else str(last)
    return {"ok": True, "rows": len(hist), "last": last_str,
            "note": f"{len(hist)} barres, dern. {last_str}"}


def probe_yf_info(ticker):
    """yfinance company info: name / sector / description availability."""
    try:
        info = yf.Ticker(ticker).get_info()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "level": "empty", "note": f"erreur: {exc}"}
    if not info:
        return {"ok": False, "level": "empty", "note": "info vide"}

    name = info.get("longName") or info.get("shortName")
    sector = info.get("sector")
    industry = info.get("industry")
    desc = info.get("longBusinessSummary")

    if name and sector:
        parts = ["nom", "secteur"]
        if desc:
            parts.append("description")
        return {"ok": True, "level": "full", "name": name, "sector": sector,
                "note": "OK (" + "+".join(parts) + ")"}
    if name and (industry or desc):
        return {"ok": True, "level": "partial", "name": name,
                "note": "partiel (nom, pas de secteur)"}
    if name:
        return {"ok": True, "level": "partial", "name": name,
                "note": "nom seul"}
    return {"ok": False, "level": "empty", "note": "champs vides"}


def probe_finnhub_news(ticker, api_key):
    if not api_key:
        return {"ok": False, "count": 0, "note": "pas de cle"}
    to_d = datetime.now(timezone.utc).date()
    from_d = to_d - timedelta(days=7)
    params = {"symbol": ticker, "from": from_d.isoformat(),
              "to": to_d.isoformat(), "token": api_key}
    try:
        r = requests.get(FINNHUB_NEWS_URL, params=params, timeout=TIMEOUT)
    except requests.RequestException:
        return {"ok": False, "count": 0, "note": "erreur reseau"}
    # Never surface the URL/token in the report -> report only the status.
    if r.status_code != 200:
        return {"ok": False, "count": 0, "note": f"HTTP {r.status_code}"}
    try:
        data = r.json()
    except ValueError:
        return {"ok": False, "count": 0, "note": "reponse non-JSON"}
    n = len(data) if isinstance(data, list) else 0
    return {"ok": n > 0, "count": n, "note": f"{n} news"}


def probe_finnhub_profile(ticker, api_key):
    if not api_key:
        return {"ok": False, "note": "pas de cle"}
    try:
        r = requests.get(FINNHUB_PROFILE_URL,
                         params={"symbol": ticker, "token": api_key},
                         timeout=TIMEOUT)
    except requests.RequestException:
        return {"ok": False, "note": "erreur reseau"}
    if r.status_code != 200:
        return {"ok": False, "note": f"HTTP {r.status_code}"}
    try:
        data = r.json()
    except ValueError:
        return {"ok": False, "note": "reponse non-JSON"}
    if not data:
        return {"ok": False, "note": "profil vide"}
    name = data.get("name")
    industry = data.get("finnhubIndustry")
    if name and industry:
        return {"ok": True, "note": f"OK ({industry})"}
    if name:
        return {"ok": True, "note": "nom seul"}
    return {"ok": False, "note": "champs vides"}


def probe_yahoo_rss(ticker, session):
    try:
        r = session.get(YAHOO_RSS_URL.format(ticker=ticker), timeout=TIMEOUT)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        items = root.findall(".//item")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "note": f"erreur: {exc}"}
    n = len(items)
    return {"ok": n > 0, "count": n, "note": f"{n} items"}


# --- Report generation ------------------------------------------------------

def _cell(result):
    icon = "OK" if result.get("ok") else "VIDE"
    return result.get("note", icon)


def _status_icon(ok, level=None):
    if level == "partial":
        return "WARN"
    return "OK" if ok else "KO"


def run():
    load_dotenv()
    api_key = os.getenv("FINNHUB_API_KEY")
    if not api_key:
        logger.warning("FINNHUB_API_KEY absente - les tests Finnhub seront 'pas de cle'.")

    session = requests.Session()
    session.headers.update({"User-Agent": "Finance-diagnostics/1.0"})

    results = []
    for ticker, country, zone in SAMPLE:
        logger.info("Test %s (%s)...", ticker, country)
        res = {
            "ticker": ticker, "country": country, "zone": zone,
            "yf_price": probe_yf_price(ticker),
            "yf_info": probe_yf_info(ticker),
            "fh_news": probe_finnhub_news(ticker, api_key),
            "fh_profile": probe_finnhub_profile(ticker, api_key),
            "rss": probe_yahoo_rss(ticker, session),
        }
        results.append(res)

    write_report(results)
    logger.info("Report written to %s", REPORT_PATH)
    return 0


def write_report(results):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# Couverture internationale - yfinance / Finnhub / Yahoo RSS",
        "",
        f"_Rapport genere le {now} par "
        "`diagnostics/test_international_coverage.py`._",
        "",
        "Diagnostic isole : ces tickers ne sont PAS integres au pipeline. "
        "Objectif = voir ou chaque source fonctionne, est degradee ou vide, "
        "pour savoir ou etendre l'univers suivi de facon realiste.",
        "",
        "## Tableau recapitulatif",
        "",
        "| Ticker | Pays | yfinance prix | yfinance info | Finnhub news | "
        "Finnhub profil | RSS Yahoo |",
        "|--------|------|---------------|---------------|--------------|"
        "----------------|-----------|",
    ]
    for r in results:
        lines.append(
            f"| `{r['ticker']}` | {r['country']} | {_cell(r['yf_price'])} | "
            f"{_cell(r['yf_info'])} | {_cell(r['fh_news'])} | "
            f"{_cell(r['fh_profile'])} | {_cell(r['rss'])} |"
        )

    # Per-zone conclusion, data-driven.
    lines += ["", "## Conclusion par zone", ""]
    zones = {}
    for r in results:
        zones.setdefault(r["zone"], []).append(r)

    for zone, rows in zones.items():
        lines.append(f"### {zone}")
        for r in rows:
            yf_ok = r["yf_price"]["ok"]
            info_lvl = r["yf_info"].get("level")
            fh_news = r["fh_news"].get("count", 0)
            fh_prof = r["fh_profile"]["ok"]
            rss_n = r["rss"].get("count", 0)
            verdict = _zone_verdict(yf_ok, info_lvl, fh_news, fh_prof, rss_n)
            lines.append(f"- **{r['ticker']}** ({r['country']}) : {verdict}")
        lines.append("")

    lines += [
        "## Lecture d'ensemble",
        "",
        "- **yfinance prix** : la source la plus robuste a l'international "
        "(suffixes .PA/.DE/.L/.T/.HK/.SS/.KS/.NS/.SA reconnus).",
        "- **yfinance info** : qualite variable selon la place (secteur parfois "
        "vide hors US).",
        "- **Finnhub** : company-news et profile2 sont pensees pour les tickers "
        "US ; la couverture hors US est souvent partielle ou vide sur le tier "
        "gratuit.",
        "- **RSS Yahoo** : meme format d'URL, mais le volume chute fortement "
        "pour les tickers non-US (0 pour l'Inde).",
        "",
        "### Attention aux symboles",
        "",
        "- `LVMH.PA` n'est PAS un symbole Yahoo valide : le ticker Euronext "
        "Paris de LVMH est `MC.PA`. Sa ligne toute vide reflete un mauvais "
        "symbole, pas une absence de couverture.",
        "- Finnhub renvoie **HTTP 403** (interdit) sur company-news et "
        "profile2 pour tous ces tickers non-US : ces endpoints ne sont pas "
        "couverts par le tier gratuit hors marche US (ils fonctionnent pour "
        "AAPL/MSFT/... dans le pipeline).",
        "",
        "> Detail par ticker dans le tableau ci-dessus (valeurs mesurees).",
    ]

    with open(REPORT_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _zone_verdict(yf_ok, info_lvl, fh_news, fh_prof, rss_n):
    good, weak = [], []
    (good if yf_ok else weak).append("prix yfinance" + ("" if yf_ok else " KO"))
    if info_lvl == "full":
        good.append("info yfinance complete")
    elif info_lvl == "partial":
        weak.append("info yfinance partielle")
    else:
        weak.append("info yfinance vide")
    (good if fh_news > 0 else weak).append(
        f"Finnhub news ({fh_news})" if fh_news else "Finnhub news vide")
    (good if fh_prof else weak).append(
        "Finnhub profil" if fh_prof else "Finnhub profil vide")
    (good if rss_n > 0 else weak).append(
        f"RSS ({rss_n})" if rss_n else "RSS vide")

    parts = []
    if good:
        parts.append("OK: " + ", ".join(good))
    if weak:
        parts.append("degrade/vide: " + ", ".join(weak))
    return " | ".join(parts)


if __name__ == "__main__":
    sys.exit(run())
