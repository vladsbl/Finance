import { useEffect, useState } from 'react'
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { ApiError, fetchCorrelations, fetchStockChart, fetchStockDetail } from '../api'
import { CompanyDescription } from '../components/CompanyDescription'
import { DirectionProbabilityBar } from '../components/DirectionProbabilityBar'
import { ExpandModal } from '../components/ExpandModal'
import { PriceHeadline } from '../components/PriceHeadline'
import type { Correlation, CorrelationBadge, CorrelationsResponse, StockDetail } from '../types'

type CorrState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; data: CorrelationsResponse }

// Matches the backend's own default (api/routers/correlations.py's
// DEFAULT_LIMIT) -- same discipline as OpportunitesPage's own PAGE_SIZE
// constant, so the client's page-count math always agrees with what it
// actually requested.
const PAGE_SIZE = 50

// Long enough that typing a word fires ONE request instead of one per
// character, short enough that the table still feels responsive.
const SEARCH_DEBOUNCE_MS = 300

const BADGE_STYLES: Record<CorrelationBadge['severity'], string> = {
  warning: 'bg-amber-400/15 text-amber-300',
  info: 'bg-cyan-400/15 text-cyan-200',
}

const BADGE_LABELS: Record<CorrelationBadge['type'], string> = {
  inter_market_lag: 'Decalage inter-marche',
  mean_reversion: 'Retour a la moyenne',
  lag_caution: 'Lag : prudence',
}

const BADGE_ICONS: Record<CorrelationBadge['severity'], string> = {
  warning: '⚠',
  info: 'ℹ',
}

function formatCoefficient(coef: number): string {
  return coef >= 0 ? `+${coef.toFixed(3)}` : coef.toFixed(3)
}

function formatPValue(p: number): string {
  return p.toExponential(2)
}

// relation_type comes straight off the Knowledge Graph (concurrent /
// fournisseur / client / partenaire / dependance -- same vocabulary as
// AddRelationForm's own dropdown on the Knowledge Graph page), read as
// "source -- relation_type --> target" everywhere else it's shown (e.g.
// ManualRelationsPanel). Turned into one plain-French sentence here so the
// relation is legible at a glance instead of a bare English/technical
// label.
function describeRelation(correlation: Correlation): string {
  const { nom_source, nom_target, relation_type } = correlation
  switch (relation_type) {
    case 'concurrent':
      return 'Ces deux entreprises sont concurrentes.'
    case 'partenaire':
      return 'Ces deux entreprises sont partenaires.'
    case 'fournisseur':
      return `${nom_source} est fournisseur de ${nom_target}.`
    case 'client':
      return `${nom_source} est client de ${nom_target}.`
    case 'dependance':
      return `${nom_source} depend de ${nom_target}.`
    default:
      return `Relation detectee entre ces deux entreprises : ${relation_type}.`
  }
}

function BadgePill({ badge }: { badge: Correlation['badge'] }) {
  if (!badge) return <span className="text-slate-600">--</span>
  return (
    <span
      title={badge.message}
      className={`inline-flex cursor-help items-center gap-1 rounded-full px-2.5 py-1 text-xs font-medium ${BADGE_STYLES[badge.severity]}`}
    >
      <span aria-hidden="true">{BADGE_ICONS[badge.severity]}</span>
      {BADGE_LABELS[badge.type]}
    </span>
  )
}

export function CorrelationsPage() {
  // `search` is what is in the input -- updated on every keystroke so the
  // field stays responsive. `appliedSearch` is what has actually been sent
  // to the API, updated only once typing pauses (debounce effect below).
  const [search, setSearch] = useState('')
  const [appliedSearch, setAppliedSearch] = useState('')
  const [page, setPage] = useState(0)
  // Bumped by the "Reessayer" button to re-run the fetch effect without
  // changing the page or the search.
  const [reloadKey, setReloadKey] = useState(0)
  const [state, setState] = useState<CorrState>({ status: 'loading' })
  // The row currently shown in the expanded comparison view, or null when
  // the modal is closed.
  const [expanded, setExpanded] = useState<Correlation | null>(null)

  // Debounce. The pagination reset lives HERE rather than in the input's
  // onChange so it is tied to the search actually being applied; React
  // batches both setStates, so the fetch effect below still runs once.
  useEffect(() => {
    const timer = setTimeout(() => {
      setAppliedSearch(search)
      setPage(0)
    }, SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [search])

  useEffect(() => {
    // A slower earlier request must never overwrite a newer one's result:
    // when searching as you type, "App" can easily resolve after "Apple".
    let cancelled = false
    setState({ status: 'loading' })
    fetchCorrelations(PAGE_SIZE, page * PAGE_SIZE, appliedSearch)
      .then((data) => {
        if (!cancelled) setState({ status: 'ready', data })
      })
      .catch((err) => {
        if (cancelled) return
        const message =
          err instanceof ApiError
            ? err.message
            : 'Erreur inattendue lors du chargement des correlations.'
        setState({ status: 'error', message })
      })
    return () => {
      cancelled = true
    }
  }, [page, appliedSearch, reloadKey])

  return (
    <div>
      <h1 className="jarvis-title text-4xl font-bold">Correlations decouvertes</h1>

      <div className="jarvis-banner-info mt-4 text-sm">
        Correlation statistique observee sur l'historique disponible -- <strong>ce n'est pas une
        preuve de causalite</strong>. Deux actions peuvent evoluer ensemble pour bien d'autres
        raisons qu'un lien economique direct : secteur commun, sentiment de marche general, ou
        simple coincidence statistique. Ces resultats servent a orienter l'attention vers des
        paires deja liees dans le graphe de connaissances -- jamais a predire un mouvement futur.
      </div>

      <div className="mt-6">
        <label
          htmlFor="correlation-search"
          className="mb-1 block text-sm font-medium text-muted"
        >
          Rechercher une entreprise
        </label>
        {/* Free text, not the TickerSearch autocomplete used on the other
            pages: here the point is to search by COMPANY NAME without
            knowing the ticker, and a partial name ("Energy") is a
            legitimate query meant to match many pairs at once -- an
            exact-pick autocomplete would defeat that. */}
        <div className="relative w-full max-w-md">
          <input
            id="correlation-search"
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Nom d'entreprise (ex: Apple, Energy...)"
            className="w-full rounded-full border border-cyan-400/25 bg-navy-800/50 px-4 py-2 pr-20 text-sm text-ink placeholder:text-faint backdrop-blur-md transition-all focus:border-cyan-300/70 focus:outline-none focus:ring-1 focus:ring-cyan-300/50"
          />
          {search && (
            <button
              type="button"
              onClick={() => setSearch('')}
              className="absolute right-2 top-1/2 -translate-y-1/2 rounded-full px-2 py-0.5 text-xs font-medium text-faint hover:bg-white/10 hover:text-ink"
            >
              Effacer
            </button>
          )}
        </div>
        <p className="mt-1 text-xs text-faint">
          Recherche sur le NOM de l'entreprise (partielle, insensible a la casse) : affiche les
          paires dont l'une des deux entreprises correspond.
        </p>
      </div>

      {state.status === 'loading' && (
        <div className="mt-8 flex items-center gap-3 text-faint">
          <span className="jarvis-spinner h-5 w-5 animate-spin" aria-hidden="true" />
          Chargement des correlations...
        </div>
      )}

      {state.status === 'error' && (
        <div className="jarvis-banner-error mt-8">
          <p className="font-medium">Impossible de charger les correlations.</p>
          <p className="mt-1 text-sm">{state.message}</p>
          <button
            type="button"
            onClick={() => setReloadKey((k) => k + 1)}
            className="jarvis-pill-danger mt-3"
          >
            Reessayer
          </button>
        </div>
      )}

      {state.status === 'ready' && (
        <>
          <p className="mt-4 text-sm text-faint">
            {state.data.search ? (
              <>
                {state.data.n_total} paire(s) impliquant une entreprise dont le nom contient
                &laquo;&nbsp;{state.data.search}&nbsp;&raquo;, sur {state.data.n_before_dedup}{' '}
                correlation(s) retenue(s) au total, triees par force de correlation
                decroissante.
              </>
            ) : (
              <>
                {state.data.n_before_dedup} correlation(s) retenue(s) (p-value corrigee &lt;
                0.05, apres correction pour tests multiples). {state.data.n_total} affichee(s)
                ci-dessous (paires symetriques du Knowledge Graph fusionnees en une seule
                ligne), triees par force de correlation decroissante.
              </>
            )}
          </p>

          {state.data.correlations.length === 0 ? (
            state.data.search ? (
              <div className="jarvis-banner-warning mt-8">
                <p className="font-medium">
                  Aucune paire ne correspond a &laquo;&nbsp;{state.data.search}&nbsp;&raquo;.
                </p>
                <p className="mt-1 text-sm">
                  La recherche porte sur le NOM de l'entreprise (pas le ticker), et seules les
                  paires ayant une correlation retenue apparaissent ici. Essayez un nom plus
                  court, ou une autre entreprise.
                </p>
                <button
                  type="button"
                  onClick={() => setSearch('')}
                  className="jarvis-pill mt-3"
                >
                  Effacer la recherche
                </button>
              </div>
            ) : (
              <div className="jarvis-empty mt-8">
                Aucune correlation calculee pour l'instant.
              </div>
            )
          ) : (
            <div className="jarvis-card mt-4 overflow-x-auto p-2">
              <table className="min-w-full divide-y divide-cyan-400/10 text-sm">
                <thead>
                  <tr className="text-left text-faint">
                    <th className="px-2 py-2">Paire</th>
                    <th className="px-2 py-2">Relation d'origine</th>
                    <th className="px-2 py-2">Coefficient</th>
                    <th className="px-2 py-2">P-value corrigee</th>
                    <th className="px-2 py-2">Lag</th>
                    <th className="px-2 py-2">Observations</th>
                    <th className="px-2 py-2">Note</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-cyan-400/5">
                  {state.data.correlations.map((c) => (
                    <tr
                      key={c.id}
                      onClick={() => setExpanded(c)}
                      className="cursor-pointer align-top transition-colors hover:bg-white/5"
                      title="Cliquer pour agrandir la comparaison"
                    >
                      <td className="px-2 py-2">
                        <div className="jarvis-metric font-medium text-cyan-200">
                          {c.ticker_source} &harr; {c.ticker_target}
                        </div>
                        <div className="text-xs text-faint">
                          {c.nom_source} &harr; {c.nom_target}
                        </div>
                      </td>
                      <td className="px-2 py-2 text-ink/70">{c.relation_type}</td>
                      <td className="jarvis-metric px-2 py-2 font-semibold text-ink">
                        {formatCoefficient(c.coefficient)}
                      </td>
                      <td className="jarvis-metric px-2 py-2 text-ink/70">{formatPValue(c.p_value_corrigee)}</td>
                      <td className="px-2 py-2 text-ink/70">{c.lag_label}</td>
                      <td className="jarvis-metric px-2 py-2 text-ink/70">{c.n_observations}</td>
                      <td className="px-2 py-2">
                        <BadgePill badge={c.badge} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {state.data.n_total > 0 && (
            <div className="mt-4 flex items-center justify-between text-sm text-faint">
              <button
                type="button"
                onClick={() => setPage((p) => Math.max(0, p - 1))}
                disabled={page === 0}
                className="jarvis-pill"
              >
                Precedent
              </button>
              <span>
                Page {page + 1} sur {Math.max(1, Math.ceil(state.data.n_total / PAGE_SIZE))}
              </span>
              <button
                type="button"
                onClick={() => setPage((p) => p + 1)}
                disabled={(page + 1) * PAGE_SIZE >= state.data.n_total}
                className="jarvis-pill"
              >
                Suivant
              </button>
            </div>
          )}
        </>
      )}

      <ExpandModal
        isOpen={expanded !== null}
        onClose={() => setExpanded(null)}
        title={expanded ? `${expanded.ticker_source} ↔ ${expanded.ticker_target}` : ''}
      >
        {expanded && <CorrelationComparison correlation={expanded} />}
      </ExpandModal>
    </div>
  )
}

// --- Expanded comparison view ------------------------------------------------

type DetailState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; data: StockDetail }

interface ChartPoint {
  date: string
  source: number | null
  target: number | null
}

type ChartState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'unavailable' } // not enough overlapping history to compare
  | { status: 'ready'; points: ChartPoint[] }

function loadDetail(ticker: string, setState: (s: DetailState) => void) {
  setState({ status: 'loading' })
  fetchStockDetail(ticker)
    .then((data) => setState({ status: 'ready', data }))
    .catch((err) => {
      const message =
        err instanceof ApiError ? err.message : `Erreur lors du chargement de ${ticker}.`
      setState({ status: 'error', message })
    })
}

// Minimum overlapping trading days below which a comparative chart is more
// misleading than useful (a two- or three-point line isn't a real
// visual comparison).
const MIN_CHART_POINTS = 5

async function loadComparisonChart(
  sourceTicker: string,
  targetTicker: string,
  setState: (s: ChartState) => void,
) {
  setState({ status: 'loading' })
  try {
    const [sourceChart, targetChart] = await Promise.all([
      fetchStockChart(sourceTicker),
      fetchStockChart(targetTicker),
    ])
    const sourceByDate = new Map(sourceChart.points.map((p) => [p.date, p.close]))
    const targetByDate = new Map(targetChart.points.map((p) => [p.date, p.close]))
    const commonDates = [...sourceByDate.keys()]
      .filter((d) => targetByDate.has(d) && sourceByDate.get(d) !== null && targetByDate.get(d) !== null)
      .sort()

    if (commonDates.length < MIN_CHART_POINTS) {
      setState({ status: 'unavailable' })
      return
    }

    // Normalised to "% change since the first common date" -- the two
    // tickers can trade at wildly different price levels (and currencies),
    // so overlaying raw closes would just show two flat-looking lines at
    // different scales. A %-change baseline is the standard way to make
    // co-movement visually comparable regardless of price level.
    const firstSource = sourceByDate.get(commonDates[0])!
    const firstTarget = targetByDate.get(commonDates[0])!
    const points: ChartPoint[] = commonDates.map((date) => ({
      date,
      source: ((sourceByDate.get(date)! - firstSource) / firstSource) * 100,
      target: ((targetByDate.get(date)! - firstTarget) / firstTarget) * 100,
    }))
    setState({ status: 'ready', points })
  } catch (err) {
    const message =
      err instanceof ApiError ? err.message : 'Erreur lors du chargement du graphique comparatif.'
    setState({ status: 'error', message })
  }
}

function formatVariation(pct: number | null): string {
  if (pct === null) return 'n/a'
  const sign = pct >= 0 ? '+' : ''
  return `${sign}${pct.toFixed(1)}%`
}

function CorrelationComparison({ correlation }: { correlation: Correlation }) {
  const [sourceState, setSourceState] = useState<DetailState>({ status: 'loading' })
  const [targetState, setTargetState] = useState<DetailState>({ status: 'loading' })
  const [chartState, setChartState] = useState<ChartState>({ status: 'loading' })

  useEffect(() => {
    loadDetail(correlation.ticker_source, setSourceState)
    loadDetail(correlation.ticker_target, setTargetState)
    loadComparisonChart(correlation.ticker_source, correlation.ticker_target, setChartState)
  }, [correlation.ticker_source, correlation.ticker_target])

  return (
    <div className="flex flex-col gap-6">
      {/* The relation type, plain-French and up front -- readable at a
          glance instead of buried in the table's raw relation_type column. */}
      <p className="rounded-2xl bg-cyan-400/10 px-3 py-2 text-sm font-medium text-cyan-100">
        {describeRelation(correlation)}
      </p>

      {/* Correlation metrics, in large -- the reason these two tickers are
          being compared in the first place. */}
      <div className="grid grid-cols-2 gap-4 rounded-2xl border border-cyan-400/10 bg-white/5 p-4 sm:grid-cols-4">
        <Metric label="Coefficient (Spearman)" value={formatCoefficient(correlation.coefficient)} />
        <Metric label="P-value corrigee" value={formatPValue(correlation.p_value_corrigee)} />
        <Metric label="Lag" value={correlation.lag_label} small />
        <Metric label="Observations" value={String(correlation.n_observations)} />
      </div>
      {correlation.badge && (
        <p
          className={`-mt-3 rounded-2xl px-3 py-2 text-sm ${
            correlation.badge.severity === 'warning'
              ? 'bg-amber-400/15 text-amber-300'
              : 'bg-cyan-400/15 text-cyan-200'
          }`}
        >
          {correlation.badge.severity === 'warning' ? '⚠' : 'ℹ'} {correlation.badge.message}
        </p>
      )}

      {/* Side-by-side company comparison. */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <CompanyPanel ticker={correlation.ticker_source} nom={correlation.nom_source} state={sourceState} />
        <CompanyPanel ticker={correlation.ticker_target} nom={correlation.nom_target} state={targetState} />
      </div>

      {/* Comparative price chart. */}
      <div>
        <h3 className="jarvis-heading mb-2 text-sm font-bold">
          Evolution comparee du prix (% depuis le premier jour commun)
        </h3>
        {chartState.status === 'loading' && (
          <p className="text-sm text-faint">Chargement du graphique...</p>
        )}
        {chartState.status === 'error' && (
          <p className="text-sm text-red-400">{chartState.message}</p>
        )}
        {chartState.status === 'unavailable' && (
          <p className="text-sm text-faint">
            Historique de prix insuffisant en commun entre {correlation.ticker_source} et{' '}
            {correlation.ticker_target} pour un graphique comparatif.
          </p>
        )}
        {chartState.status === 'ready' && (
          <div className="h-64 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chartState.points} margin={{ top: 5, right: 20, left: 0, bottom: 5 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#22d3ee" strokeOpacity={0.12} />
                <XAxis dataKey="date" tick={{ fontSize: 11, fill: '#7c93ad' }} minTickGap={40} stroke="#22d3ee" strokeOpacity={0.2} />
                <YAxis
                  tick={{ fontSize: 11, fill: '#7c93ad' }}
                  width={50}
                  stroke="#22d3ee"
                  strokeOpacity={0.2}
                  tickFormatter={(v: number) => `${v.toFixed(0)}%`}
                />
                <Tooltip
                  formatter={(v: number) => `${v.toFixed(1)}%`}
                  contentStyle={{ background: '#0d1520', border: '1px solid rgba(34,211,238,0.3)', borderRadius: 12, fontSize: 12 }}
                  labelStyle={{ color: '#8ec9f2' }}
                />
                <Legend wrapperStyle={{ fontSize: 12, color: '#8ec9f2' }} />
                <Line
                  type="monotone"
                  dataKey="source"
                  name={correlation.ticker_source}
                  stroke="#5fe3ff"
                  dot={false}
                  strokeWidth={1.8}
                  connectNulls
                />
                <Line
                  type="monotone"
                  dataKey="target"
                  name={correlation.ticker_target}
                  stroke="#fbbf24"
                  dot={false}
                  strokeWidth={1.8}
                  connectNulls
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        )}
      </div>
    </div>
  )
}

function Metric({ label, value, small }: { label: string; value: string; small?: boolean }) {
  return (
    <div>
      <span className="text-xs text-faint">{label}</span>
      <p className={`jarvis-metric ${small ? 'text-sm font-semibold text-ink' : 'text-xl font-semibold text-ink'}`}>
        {value}
      </p>
    </div>
  )
}

function CompanyPanel({
  ticker,
  nom,
  state,
}: {
  ticker: string
  nom: string
  state: DetailState
}) {
  return (
    <div className="rounded-2xl border border-cyan-400/15 p-4">
      <h3 className="jarvis-heading text-base font-bold">
        {ticker} <span className="font-normal text-faint">-- {nom}</span>
      </h3>

      {state.status === 'loading' && <p className="mt-2 text-sm text-faint">Chargement...</p>}
      {state.status === 'error' && <p className="mt-2 text-sm text-red-400">{state.message}</p>}

      {state.status === 'ready' && (
        <div className="mt-2 flex flex-col gap-2 text-sm">
          <CompanyDescription ticker={ticker} />

          <PriceHeadline
            price={state.data.prix_eur !== null ? state.data.prix_eur : state.data.current_price}
            currency={state.data.prix_eur !== null ? 'EUR' : state.data.devise}
            variationPct={state.data.variations ? state.data.variations['1j'] : null}
          />
          {state.data.variations && (
            <p className="text-xs text-faint">
              7j: {formatVariation(state.data.variations['7j'])} - 30j:{' '}
              {formatVariation(state.data.variations['30j'])}
            </p>
          )}

          <DirectionProbabilityBar direction={state.data.direction_probabilities} />

          <div className="grid grid-cols-2 gap-2">
            <ScoreCell label="Prix/Valorisation" value={state.data.price_valuation_score} />
            <ScoreCell label="Technique" value={state.data.technical_score} />
            <ScoreCell label="Fondamental reel" value={state.data.score_fondamental_reel} />
            <ScoreCell label="Confiance" value={state.data.confidence} suffix="%" />
          </div>
        </div>
      )}
    </div>
  )
}

function ScoreCell({ label, value, suffix }: { label: string; value: number | null; suffix?: string }) {
  return (
    <div>
      <span className="text-xs text-faint">{label}</span>
      <p className="jarvis-metric font-medium text-ink">{value === null ? 'n/a' : `${value.toFixed(0)}${suffix ?? ''}`}</p>
    </div>
  )
}
