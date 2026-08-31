import type {
  AddRelationPayload,
  ArguedTextResponse,
  CausalChainsResponse,
  CompanyDescriptionResponse,
  CausalReasoningRunStats,
  CausalReasoningStatus,
  NewsFacetsResponse,
  NewsNarrativeResponse,
  NewsResponse,
  CorrelationsResponse,
  DailySummaryResponse,
  DirectionFilterValue,
  GraphResponse,
  MacroContextResponse,
  ManualRelation,
  ManualRelationsResponse,
  OpportunitesResponse,
  PipelineState,
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, init)
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

  // DELETE routes here always return a small JSON body ({"deleted": ...}),
  // but a bodyless 204 would otherwise make response.json() throw -- keep
  // this generic rather than special-casing DELETE, in case a future
  // route legitimately returns 204.
  const text = await response.text()
  return (text ? JSON.parse(text) : undefined) as T
}

function getJson<T>(path: string): Promise<T> {
  return request<T>(path)
}

function postJson<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

function deleteRequest<T>(path: string): Promise<T> {
  return request<T>(path, { method: 'DELETE' })
}

export function fetchDailySummary(): Promise<DailySummaryResponse> {
  return getJson<DailySummaryResponse>('/daily-summary')
}

export function fetchArguedText(ticker: string): Promise<ArguedTextResponse> {
  return getJson<ArguedTextResponse>(`/daily-summary/${encodeURIComponent(ticker)}/argued-text`)
}

// --- /api/macro-context -----------------------------------------------------
//
// A GET here is what actually triggers generation on a cache miss (one
// Groq call/day, cached server-side) -- same on-demand-via-GET convention
// as fetchArguedText/fetchNewsNarrative above, no separate "generate"
// button needed.
export function fetchMacroContext(): Promise<MacroContextResponse> {
  return getJson<MacroContextResponse>('/macro-context')
}

export function fetchOpportunites(
  priorite: Priorite = 'toutes',
  limit = 50,
  offset = 0,
  direction: DirectionFilterValue = 'toutes',
): Promise<OpportunitesResponse> {
  const params = new URLSearchParams({
    priorite,
    direction,
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

// Permanently cached server-side (see reasoning/company_description.py) --
// safe to call every time a ticker's detail is shown, a second call for
// the same ticker only ever reads the cache, never regenerates.
export function fetchCompanyDescription(ticker: string): Promise<CompanyDescriptionResponse> {
  return getJson<CompanyDescriptionResponse>(`/stock/${encodeURIComponent(ticker)}/description`)
}

// --- /api/graph -------------------------------------------------------------

export function fetchGraph(ticker?: string): Promise<GraphResponse> {
  const suffix = ticker ? `?${new URLSearchParams({ ticker }).toString()}` : ''
  return getJson<GraphResponse>(`/graph${suffix}`)
}

export function fetchManualRelations(): Promise<ManualRelationsResponse> {
  return getJson<ManualRelationsResponse>('/graph/relations/manual')
}

export function addManualRelation(payload: AddRelationPayload): Promise<ManualRelation> {
  return postJson<ManualRelation>('/graph/relations', payload)
}

export function deleteManualRelation(id: number): Promise<{ deleted: boolean; id: number }> {
  return deleteRequest<{ deleted: boolean; id: number }>(`/graph/relations/${id}`)
}

// --- /api/correlations --------------------------------------------------------

export function fetchCorrelations(
  limit = 50,
  offset = 0,
  search = '',
): Promise<CorrelationsResponse> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  // Omitted entirely when blank: the backend treats an empty ?search= as
  // "no filter" anyway, so sending it would only make the default view's
  // URLs (and the network log) noisier for no behavioural difference.
  if (search.trim()) params.set('search', search.trim())
  return getJson<CorrelationsResponse>(`/correlations?${params.toString()}`)
}

// --- /api/pipeline ------------------------------------------------------------

export function fetchPipelineStatus(): Promise<PipelineState> {
  return getJson<PipelineState>('/pipeline/status')
}

// Returns as soon as the pipeline has been LAUNCHED (HTTP 202), not when it
// finishes -- the caller polls fetchPipelineStatus from there.
export function runPipeline(): Promise<PipelineState> {
  return postJson<PipelineState>('/pipeline/run', {})
}

// --- /api/causal-reasoning -----------------------------------------------------

export function fetchCausalChains(limit = 50): Promise<CausalChainsResponse> {
  const params = new URLSearchParams({ limit: String(limit) })
  return getJson<CausalChainsResponse>(`/causal-reasoning?${params.toString()}`)
}

export function fetchCausalReasoningStatus(): Promise<CausalReasoningStatus> {
  return getJson<CausalReasoningStatus>('/causal-reasoning/status')
}

// Runs synchronously server-side (quota-capped at 5/day, unlike the
// minutes-long pipeline run) -- this resolves once generation is done, no
// polling needed.
export function runCausalReasoning(): Promise<CausalReasoningRunStats> {
  return postJson<CausalReasoningRunStats>('/causal-reasoning/run', {})
}

// --- /api/news ------------------------------------------------------------------

// Grouped into one options object (rather than more positional params)
// specifically because GET /api/news now combines FIVE independent filters
// (see api/routers/news.py's own get_news docstring) -- a 6th positional
// bool/string param would be unreadable at call sites. All optional: an
// empty/omitted filters object is the unfiltered "all recent news" view.
export interface NewsFilters {
  ticker?: string
  search?: string
  sector?: string
  zone?: string
  direction?: DirectionFilterValue
}

export function fetchNews(limit = 50, offset = 0, filters: NewsFilters = {}): Promise<NewsResponse> {
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
    direction: filters.direction ?? 'toutes',
  })
  if (filters.ticker?.trim()) params.set('ticker', filters.ticker.trim())
  if (filters.search?.trim()) params.set('search', filters.search.trim())
  if (filters.sector?.trim()) params.set('sector', filters.sector.trim())
  if (filters.zone?.trim()) params.set('zone', filters.zone.trim())
  return getJson<NewsResponse>(`/news?${params.toString()}`)
}

// Populates the News & Analyse IA page's sector/zone filter widgets --
// fetched once (not per keystroke/filter change like fetchNews itself),
// see api/routers/news.py's GET /api/news/facets for the full contract.
export function fetchNewsFacets(): Promise<NewsFacetsResponse> {
  return getJson<NewsFacetsResponse>('/news/facets')
}

// On-demand only (see api/routers/news.py's own docstring) -- never called
// for a whole list page, only when a user opens one news item's enriched view.
export function fetchNewsNarrative(newsId: number): Promise<NewsNarrativeResponse> {
  return getJson<NewsNarrativeResponse>(`/news/${newsId}/narrative`)
}
