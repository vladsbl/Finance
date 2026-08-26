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

// Mirrors reasoning/direction_probability.py's compute_direction_probabilities()
// exactly -- hausse + stagnation + baisse always sum to 100. Never a
// statistical forecast; `disclaimer` must be shown alongside these
// percentages wherever they're displayed (see the module's own docstring).
export interface DirectionProbabilities {
  hausse: number
  stagnation: number
  baisse: number
  // Optional: absent on a small number of pre-existing cached news
  // narratives generated before this field existed (see
  // reasoning/analyze_news.py's _ensure_narratives_horizon_column) --
  // every freshly computed result always has it.
  horizon?: string | null
  explication: string
  disclaimer: string
}

// Mirrors api/routers/stock.py's GET /api/stock/{ticker}/description
// exactly. Unlike ArguedTextResponse, this has no per-day freshness
// concept -- see reasoning/company_description.py's own docstring for why
// a company description is cached permanently instead.
export type CompanyDescriptionSource = 'cache' | 'generated' | 'unavailable'

export interface CompanyDescriptionResponse {
  ticker: string
  description: string | null
  source: CompanyDescriptionSource
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
  direction_probabilities: DirectionProbabilities | null
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

// Mirrors the `direction` query param shared by /api/opportunities and
// /api/news, and the dominant-scenario helper used client-side for
// /api/daily-summary and /api/causal-reasoning (which have no such param --
// see DirectionFilter.tsx's own docstring for why).
export type DirectionFilterValue = 'toutes' | 'hausse' | 'stagnation' | 'baisse'

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
  direction_probabilities: DirectionProbabilities | null
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
  direction_probabilities: DirectionProbabilities | null
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

// Mirrors api/routers/graph.py exactly.

export type GraphNodeKind = 'primary' | 'external'

export interface GraphNode {
  id: string
  kind: GraphNodeKind
  ticker: string
  label: string
  display_name: string
}

export interface GraphEdge {
  source: string
  target: string
  relation_type: string
  notes: string
}

export interface GraphResponse {
  mode: 'top_opportunities' | 'ticker'
  top_tickers?: string[]
  ticker?: string
  nodes: GraphNode[]
  edges: GraphEdge[]
  n_primary: number
  n_external: number
}

export interface ManualRelation {
  id: number
  source_ticker: string
  relation_type: string
  target_name: string
  target_ticker: string | null
  notes: string | null
}

export interface ManualRelationsResponse {
  relations: ManualRelation[]
  relation_types: string[]
}

export interface AddRelationPayload {
  source_ticker: string
  relation_type: string
  target_name: string
  target_ticker?: string | null
  notes?: string | null
}

// Mirrors api/routers/correlations.py exactly.

export type CorrelationBadgeType = 'inter_market_lag' | 'mean_reversion' | 'lag_caution'
export type CorrelationBadgeSeverity = 'warning' | 'info'

export interface CorrelationBadge {
  type: CorrelationBadgeType
  severity: CorrelationBadgeSeverity
  message: string
}

export interface Correlation {
  id: number
  ticker_source: string
  nom_source: string
  ticker_target: string
  nom_target: string
  relation_type: string
  source_table: string
  lag: number
  lag_direction: string
  lag_label: string
  coefficient: number
  p_value: number
  p_value_corrigee: number
  n_observations: number
  methode: string
  correction: string
  meme_marche: boolean
  badge: CorrelationBadge | null
  created_at: string
}

export interface CorrelationsResponse {
  correlations: Correlation[]
  n_before_dedup: number
  n_total: number
  // Echo of the applied name filter, normalised server-side (trimmed, null
  // when absent) -- lets the UI caption say what it actually filtered on
  // rather than re-deriving it from its own input state.
  search: string | null
  limit: number
  offset: number
}

// Mirrors api/routers/pipeline.py exactly.

export type PipelineStatus = 'idle' | 'running' | 'success' | 'failed'

export interface PipelineStepInfo {
  name: string
  status: 'ok' | 'failed'
  elapsed: number
  error: string | null
}

// Parsed out of data/logs/run_daily.log by pipeline/run_log.py, so it stays
// populated across an API restart -- and covers runs started by Windows
// Task Scheduler, which the API process never saw launch.
export interface PipelineLastRun {
  // Times only, no date: run_daily.py logs with datefmt "%H:%M:%S".
  // log_modified_at is the one real datetime available.
  log_time_start: string
  log_time_end: string | null
  completed: boolean
  steps: PipelineStepInfo[]
  steps_total: number
  steps_done: number
  n_ok: number
  n_failed: number
  current_step: string | null
  duree_secondes: number | null
  log_modified_at: string
}

export interface PipelineState {
  task_id: string | null
  status: PipelineStatus
  started_at: string | null
  finished_at: string | null
  returncode: number | null
  error: string | null
  last_run: PipelineLastRun | null
  log_file: string
}

// Mirrors api/routers/causal_reasoning.py exactly.

export type EffetImpact = 'positif' | 'negatif' | 'neutre'

export interface EntrepriseImpactee {
  entreprise: string
  ticker?: string | null
  effet: EffetImpact | string
}

export interface CausalChain {
  id: number
  news_id: number
  news_title: string | null
  ticker_source: string
  chaine_raisonnement: string
  entreprises_impactees: EntrepriseImpactee[]
  confiance: number | null
  model: string | null
  created_at: string
  direction_probabilities: DirectionProbabilities | null
}

export interface CausalChainsResponse {
  chains: CausalChain[]
  n_total: number
  staleness: string | null
}

export interface CausalReasoningStatus {
  n_pending: number
  quota_used: number
  quota_limit: number
  quota_remaining: number
}

// Same shape as reasoning/causal_reasoning.py's run_causal_reasoning() stats
// dict, returned as-is by POST /api/causal-reasoning/run.
export interface CausalReasoningRunStats {
  n_candidates: number
  processed: number
  failed: number
  skipped_no_relations: number
  quota_used: number
  quota_limit: number
  quota_exhausted: boolean
  error: string | null
}

// Mirrors api/routers/news.py exactly.

export type Tonalite = 'positive' | 'negative' | 'neutre' | string

export interface NewsPriceContext {
  devise: string
  date_before: string | null
  price_before: number | null
  price_before_eur: number | null
  date_after: string | null
  price_after: number | null
  price_after_eur: number | null
  variation_pct: number | null
  insufficient_data: boolean
  insufficient_reason: string | null
}

export interface NewsItem {
  news_id: number
  ticker: string
  title: string
  url: string | null
  published_at: string
  source: string | null
  company: string | null
  sector: string | null
  importance: number | null
  tonalite: Tonalite
  impact: string | null
  horizon: string | null
  confidence: number | null
  summary_paragraph: string
  price_context: NewsPriceContext
  direction_probabilities: DirectionProbabilities | null
}

export interface NewsResponse {
  news: NewsItem[]
  n_total: number
  ticker: string | null
  limit: number
  offset: number
}

// Mirrors api/routers/news.py's GET /api/news/{news_id}/narrative exactly.
export type NewsNarrativeSource = 'cache' | 'generated' | 'unavailable'

export interface NewsNarrativeResponse {
  news_id: number
  texte: string | null
  direction_probabilities: DirectionProbabilities | null
  source: NewsNarrativeSource
}
