import type {
  ArguedTextResponse,
  DailySummaryResponse,
  OpportunitesResponse,
  Priorite,
  StockChartResponse,
  StockDetail,
  TickersResponse,
} from './types'

// Relative paths on purpose -- Vite's dev proxy (vite.config.ts) forwards
// /api/* to the FastAPI backend (uvicorn on :8000), so this module never
// hardcodes an absolute origin. Swap this constant for an env-driven base
// URL only when this stops being a Vite dev server (Tauri build, PWA
// deployed on its own origin) -- everything else here stays unchanged.
const API_BASE = '/api'

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function getJson<T>(path: string): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`)
  } catch {
    // fetch() only throws on a network-level failure (backend down, CORS
    // blocked, DNS, ...) -- never for a 4xx/5xx HTTP status, which is
    // handled below instead.
    throw new ApiError(0, "Impossible de joindre l'API. Le serveur FastAPI tourne-t-il (uvicorn api.main:app) ?")
  }

  if (!response.ok) {
    let detail = `Erreur API (${response.status})`
    try {
      const body = await response.json()
      if (body?.detail) detail = body.detail
    } catch {
      // Response body wasn't JSON (or was empty) -- keep the generic message.
    }
    throw new ApiError(response.status, detail)
  }

  return response.json() as Promise<T>
}

export function fetchDailySummary(): Promise<DailySummaryResponse> {
  return getJson<DailySummaryResponse>('/daily-summary')
}

export function fetchArguedText(ticker: string): Promise<ArguedTextResponse> {
  return getJson<ArguedTextResponse>(`/daily-summary/${encodeURIComponent(ticker)}/argued-text`)
}

export function fetchOpportunites(
  priorite: Priorite = 'toutes',
  limit = 50,
  offset = 0,
): Promise<OpportunitesResponse> {
  const params = new URLSearchParams({
    priorite,
    limit: String(limit),
    offset: String(offset),
  })
  return getJson<OpportunitesResponse>(`/opportunities?${params.toString()}`)
}

// --- /api/tickers + /api/stock/{ticker}* ----------------------------------------

export function fetchTickers(): Promise<TickersResponse> {
  return getJson<TickersResponse>('/tickers')
}

export function fetchStockDetail(ticker: string): Promise<StockDetail> {
  return getJson<StockDetail>(`/stock/${encodeURIComponent(ticker)}`)
}

export function fetchStockChart(ticker: string): Promise<StockChartResponse> {
  return getJson<StockChartResponse>(`/stock/${encodeURIComponent(ticker)}/chart`)
}

export function fetchStockArguedText(ticker: string): Promise<ArguedTextResponse> {
  return getJson<ArguedTextResponse>(`/stock/${encodeURIComponent(ticker)}/argued-text`)
}
