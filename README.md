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
```

## Pipeline

Run the steps in order (each reads/writes `data/marketdb.db`):

| Step | Command | Writes table |
|------|---------|--------------|
| 1. Price history (2 years of daily bars) | `python ingestion/ingest_prices.py` | `price_history` |
| 2. Current snapshot (price, MAs, volume, volatility) | `python ingestion/fetch_prices.py` | `stocks` |
| 3. Fundamental score | `python analysis/fundamental/score.py` | `fundamental_scores` |
| 4. Combined score (fundamental + technical + volatility + volume) | `python analysis/combined_score.py` | `final_scores` |
| 5. Dashboard | `streamlit run dashboard/app.py` | — |

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

## Note on SSL / corporate networks

`ingest_prices.py` builds a CA bundle from the OS trust store (merged with
`certifi`) before calling yfinance. This works around networks that intercept
TLS with a proxy whose root certificate is only in the system store, which
otherwise causes `curl: (60) unable to get local issuer certificate` errors.

## Tracked tickers

AAPL, MSFT, GOOGL, AMZN, NVDA, TSLA, META, JPM, JNJ, V
(defined in `ingestion/fetch_prices.py`).
