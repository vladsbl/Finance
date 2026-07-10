# Finance — Market Intelligence

A small market-analysis pipeline: it ingests stock prices, scores each stock on
fundamental and technical criteria, and serves the results in a Streamlit
dashboard. All data lives in a local SQLite database at `data/marketdb.db`.

## Setup

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt

# Copy the example env and fill in your keys
cp .env.example .env
```

The news + AI analysis steps need two API keys in `.env`:
`GROQ_API_KEY` (see [News & AI analysis](#news--ai-analysis)) and
`FINNHUB_API_KEY` (free key from <https://finnhub.io>).

### Always activate the venv first

**Every command (scripts and the dashboard) must run inside the `.venv`.** If
you run `streamlit run dashboard/app.py` with the *system* Python instead, you
get errors like `ModuleNotFoundError: No module named 'plotly'` because the
dependencies live in the venv, not the global interpreter.

Activate it in each new terminal before running anything:

```bash
# Windows - Git Bash
source .venv/Scripts/activate
# Windows - PowerShell / cmd
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

streamlit run dashboard/app.py
```

Alternatively, call the venv's interpreter explicitly without activating:

```bash
.venv/Scripts/python.exe -m streamlit run dashboard/app.py   # Windows
.venv/bin/python -m streamlit run dashboard/app.py           # macOS/Linux
```

Your prompt should show `(.venv)` once activated. Check you're on the right
interpreter with `which python` (Git Bash) / `where python` (cmd).

## Pipeline

Run the steps in order (each reads/writes `data/marketdb.db`):

| Step | Command | Writes table |
|------|---------|--------------|
| 1. Price history (2 years of daily bars) | `python ingestion/ingest_prices.py` | `price_history` |
| 2. Current snapshot (price, MAs, volume, volatility) | `python ingestion/fetch_prices.py` | `stocks` |
| 3. Fundamental score | `python analysis/fundamental/score.py` | `fundamental_scores` |
| 4. Combined score (fundamental + technical + volatility + volume) | `python analysis/combined_score.py` | `final_scores` |
| 5. Fetch news (Yahoo RSS + Finnhub) | `python ingestion/fetch_news.py` | `news_raw` |
| 6. Analyse news with LLM (Groq) | `python reasoning/analyze_news.py` | `news_analysis` |
| 7. Import relations (knowledge graph seed) | `python graph/import_relations.py` | `relations` |
| 8. Dashboard | `streamlit run dashboard/app.py` | — |

The **RSI** and the dashboard **price/moving-average chart** are computed from
the real daily closes stored in `price_history` by step 1. Without it, the RSI
falls back to a proxy and the chart has no series — so run `ingest_prices.py`
first.

## Refreshing price history (`ingest_prices.py`)

`ingestion/ingest_prices.py` downloads the last 2 years of daily OHLCV for every
tracked ticker and **upserts** them into `price_history`. The `(ticker, date)`
primary key means re-running is safe and idempotent: existing days are
refreshed and new trading days are appended — no duplicates.

Run it manually any time to catch up:

```bash
python ingestion/ingest_prices.py
```

### Scheduling (future cron job)

The script is meant to run once per day, after US market close, so the history
stays current. Example crontab entry (weekdays, 22:30 UTC ≈ 18:30 ET):

```cron
30 22 * * 1-5  cd /path/to/Finance && /path/to/Finance/.venv/bin/python ingestion/ingest_prices.py >> /var/log/ingest_prices.log 2>&1
```

On Windows, use Task Scheduler with an equivalent daily trigger calling
`.venv\Scripts\python.exe ingestion\ingest_prices.py`.

## News & AI analysis

Two steps, **run in this order** (analysis reads what the fetch stored):

```bash
python ingestion/fetch_news.py       # 1. collect news  -> news_raw
python reasoning/analyze_news.py     # 2. analyse them   -> news_analysis
```

`fetch_news.py` pulls recent headlines per ticker from the Yahoo Finance RSS
feed and the Finnhub `/company-news` endpoint (last 7 days), merges and
de-duplicates them, and upserts into `news_raw` (idempotent).

`analyze_news.py` sends each not-yet-analysed news item to **Groq** and stores a
structured JSON verdict (company, sector, importance 1-10, tone, likely impact,
horizon, confidence) in `news_analysis`. Results are cached: a news item is
never analysed twice.

### Groq API key

1. Create a free account at <https://console.groq.com>.
2. Generate an API key under **API Keys**.
3. Put it in `.env` as `GROQ_API_KEY=...`.

### Free-tier daily quota — watch it

The Groq free tier is capped (**~1000 requests/day**). Two safeguards are built
in:

* A **strict pre-filter** runs before any API call — it drops titles shorter
  than 20 characters, sponsored/ad content, and near-duplicate headlines for the
  same ticker.
* A **daily call counter** (`llm_usage` table) stops the run once
  `DAILY_CALL_LIMIT` (1000) is reached, and rate-limit errors (HTTP 429) are
  retried with exponential backoff.

**Always estimate first with `--dry-run`** (no API calls), then run for real:

```bash
python reasoning/analyze_news.py --dry-run                 # how many would run
python reasoning/analyze_news.py --tickers AAPL,MSFT       # subset of tickers
python reasoning/analyze_news.py --limit 50                # cap this run
```

The dashboard's **News & Analyse IA** page (sidebar navigation) shows, per
ticker, the analysed headlines with a colour-coded tone, importance and impact
summary.

## Knowledge graph (relations)

`data/relations_seed.csv` holds the known relations between tracked tickers and
their suppliers / clients / competitors / partners. Columns:

```
source_ticker,relation_type,target_name,target_ticker,notes
```

`target_ticker` may be empty for external entities that we don't track (e.g. an
unlisted supplier) — those become **external** nodes in the graph.

To update the graph, edit the CSV and re-import:

```bash
# 1. Add/edit rows in data/relations_seed.csv
python graph/import_relations.py     # upsert into the `relations` table
```

The import is **idempotent**: the upsert key is
`(source_ticker, relation_type, target_name)`, so re-running only refreshes
existing rows (target_ticker / notes) and adds new ones — no duplicates, safe to
re-run any time. `graph/build_graph.py` builds a networkx graph from the table;
the dashboard's **Knowledge Graph** page renders it interactively (pyvis) and
lists each ticker's direct relations.

## Large-scale universe (1900+ tickers)

`universe/build_universe.py` aggregates the constituents of 8 world indices
into the `universe` table (`ticker, nom, pays, indice_source, devise,
priorite`). Each ticker gets an ingestion **priority** based on data-source
coverage:

| priorite | Indices | Coverage |
|----------|---------|----------|
| `haute` | S&P 500 (US) | yfinance + Finnhub + Yahoo RSS |
| `moyenne` | STOXX 600, Nikkei 225, KOSPI 200, Hang Seng, B3 | yfinance + RSS |
| `basse` | CSI 300, Nifty 50 | mostly yfinance only |

### Batch price ingestion — `ingest_universe_prices.py`

Downloads daily OHLCV in **batches** via `yf.download` (not one ticker at a
time), pausing between batches and retrying with exponential backoff on
errors / rate limits. Rows go into the shared `price_history` table (idempotent
on `(ticker, date)`).

```bash
# Always test on a subset first:
python ingestion/ingest_universe_prices.py --priorite haute  --limit 50
python ingestion/ingest_universe_prices.py --priorite moyenne --limit 50
# Full run (only once you're confident):
python ingestion/ingest_universe_prices.py --priorite toutes
```

Options:

| Option | Default | Meaning |
|--------|---------|---------|
| `--priorite` | `toutes` | `haute` / `moyenne` / `basse` / `toutes` |
| `--limit N` | none | cap the number of tickers (for testing) |
| `--batch-size N` | `50` | tickers per `yf.download` call |
| `--pause S` | `3.0` | seconds slept between batches (throttle) |
| `--period P` | `1y` | yfinance history period |
| `--retries N` | `3` | attempts per batch before giving up |

Measured on subsets of 50 tickers (batch 25, pause 3s): ~5-6 s total,
~0.12 s/ticker. A full ~1900-ticker run is therefore on the order of a few
minutes — but that is many more batches hitting Yahoo back-to-back, so watch
for rate limiting and raise `--pause` / lower `--batch-size` if needed.

## Note on SSL / corporate networks

The ingestion scripts call `ingestion/ssl_utils.py:configure_ca_bundle()` before
any network client, building a CA bundle from the OS trust store (merged with
`certifi`). This works around networks that intercept TLS with a proxy whose
root certificate is only in the system store, which otherwise causes
`curl: (60) unable to get local issuer certificate` (yfinance) or equivalent
SSL verification errors (requests / groq).

## Tracked tickers

AAPL, MSFT, GOOGL, AMZN, NVDA, TSLA, META, JPM, JNJ, V
(defined in `ingestion/fetch_prices.py`).
