// Mirrors the JSON shapes returned by api/routers/daily_summary.py exactly
// -- field names kept in French/snake_case to match the backend 1:1
// (reasoning/daily_summary.py's build_signal()), rather than translating
// them, so a backend field rename is a single obvious diff here too.

export interface PriceVariations {
  '1j': number | null
  '7j': number | null
  '30j': number | null
}

export interface PriceInfo {
  prix_actuel: number
  devise: string
  variations: PriceVariations
}

export interface Signal {
  ticker: string
  nom_affiche: string
  score_global: number
  confiance: number
  score_ajuste: number
  score_prix_valorisation: number | null
  score_technique: number | null
  score_news: number | null
  score_fondamental_reel: number | null
  explication: string
  risque: string
  conflit_composantes: boolean
  volatilite: number | null
  horizon: string
  entreprises_a_surveiller: Record<string, string[]> | null
  prix: PriceInfo | null
}

export interface DailySummaryResponse {
  signals: Signal[]
  dates_by_priority: Record<string, string>
  n_candidates: number
  staleness: string | null
}

export type ArguedTextSource = 'cache' | 'generated' | 'unavailable'

export interface ArguedTextResponse {
  ticker: string
  texte_argumente: string | null
  source: ArguedTextSource
}

// Mirrors api/routers/opportunities.py exactly.

export type Priorite = 'toutes' | 'haute' | 'moyenne' | 'basse'

export interface Opportunite {
  ticker: string
  nom_affiche: string
  priorite: Exclude<Priorite, 'toutes'>
  score_global: number | null
  score_prix_valorisation: number | null
  score_technique: number | null
  score_news: number | null
  score_fondamental_reel: number | null
  confiance: number | null
  explication: string | null
  date_calcul: string
}

export interface OpportunitesResponse {
  opportunites: Opportunite[]
  dates_by_priority: Record<string, string>
  staleness: string | null
  n_total: number
  limit: number
  offset: number
}

// Mirrors api/routers/stock.py exactly.

export interface TickerListEntry {
  ticker: string
  nom_affiche: string
}

export interface TickersResponse {
  tickers: TickerListEntry[]
}

export interface StockDetail {
  ticker: string
  nom_affiche: string
  priorite: string
  devise: string
  current_price: number | null
  prix_eur: number | null
  variations: PriceVariations | null
  ma_50: number | null
  ma_200: number | null
  volume: number | null
  volatility: number | null
  rsi: number | null
  rsi_is_real: boolean
  price_valuation_score: number | null
  technical_score: number | null
  volatility_score: number | null
  volume_score: number | null
  final_score: number | null
  confidence: number | null
  score_fondamental_reel: number | null
  sector: string | null
  industry: string | null
}

export interface StockChartPoint {
  date: string
  close: number | null
  ma_50: number | null
  ma_200: number | null
}

export interface StockChartResponse {
  ticker: string
  devise_affichee: string
  points: StockChartPoint[]
}
