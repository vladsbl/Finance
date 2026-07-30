# Finance -- frontend (React + Vite + TypeScript + Tailwind)

First React page of the progressive migration away from the Streamlit
dashboard (`dashboard/app.py`), consuming the FastAPI backend (`api/`)
instead of duplicating any of its logic. See `api/main.py`'s module
docstring for the full migration order; this covers step 1, "Resume du
jour" only.

## Running locally

Three processes run side by side during this migration, each on its own
port -- none of them conflict:

| Process | Command (from repo root unless noted) | Port |
|---|---|---|
| FastAPI backend | `uvicorn api.main:app --reload --port 8000` | 8000 |
| Streamlit dashboard (still fully functional, untouched) | `streamlit run dashboard/app.py` | 8501 |
| This React frontend | `cd frontend && npm run dev` | 5173 |

Start the backend first (the frontend's dev server proxies `/api/*`
requests to it -- see `vite.config.ts`), then:

```bash
cd frontend
npm install   # first time only
npm run dev
```

Open the URL Vite prints (typically `http://localhost:5173`).

## What's here

- `src/types.ts` -- TypeScript mirrors of the JSON shapes
  `api/routers/daily_summary.py` returns (field names kept exactly as the
  backend names them).
- `src/api.ts` -- thin `fetch()` wrappers, one per backend route. Calls
  relative paths (`/api/...`); Vite's dev proxy forwards them to `:8000`.
- `src/components/SignalCard.tsx` -- one signal (ticker, score, risk,
  price) plus its own "Voir l'analyse argumentee" button, which only calls
  `GET /api/daily-summary/{ticker}/argued-text` on click -- never
  automatically, to respect the backend's Groq daily-quota gating
  (`TICKER_ANALYSIS_DAILY_LIMIT`, see `reasoning/daily_summary.py`).
- `src/App.tsx` -- fetches `GET /api/daily-summary` once on mount, renders
  loading / error (with retry) / empty (0 signals today) / ready states.

Styling is intentionally plain (default Tailwind utility classes, no theme
yet) -- the task at this stage is proving the React <-> FastAPI plumbing
works end-to-end, not the "Jarvis" visual identity from the Streamlit
dashboard, which comes later once more pages are migrated.

## Build

```bash
npm run build   # tsc -b && vite build -- output in frontend/dist/
```
