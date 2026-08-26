import { useCallback, useEffect, useState } from 'react'
import {
  ApiError,
  fetchCausalChains,
  fetchCausalReasoningStatus,
  fetchStockDetail,
  runCausalReasoning,
} from '../api'
import { CompanyDescription } from '../components/CompanyDescription'
import { DirectionProbabilityBar } from '../components/DirectionProbabilityBar'
import { ExpandModal } from '../components/ExpandModal'
import { PriceHeadline } from '../components/PriceHeadline'
import type {
  CausalChain,
  CausalChainsResponse,
  CausalReasoningRunStats,
  CausalReasoningStatus,
  EffetImpact,
  StockDetail,
} from '../types'

type StockDetailState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; data: StockDetail }

type ChainsState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; data: CausalChainsResponse }

type StatusState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; data: CausalReasoningStatus }

type RunState =
  | { status: 'idle' }
  | { status: 'running' }
  | { status: 'error'; message: string }
  | { status: 'done'; stats: CausalReasoningRunStats }

const EFFET_STYLES: Record<string, string> = {
  positif: 'bg-emerald-100 text-emerald-800',
  negatif: 'bg-red-100 text-red-800',
  neutre: 'bg-gray-100 text-gray-700',
}

function effetStyle(effet: string): string {
  return EFFET_STYLES[effet as EffetImpact] ?? EFFET_STYLES.neutre
}

function loadChains(setState: (s: ChainsState) => void) {
  setState({ status: 'loading' })
  fetchCausalChains()
    .then((data) => setState({ status: 'ready', data }))
    .catch((err) => {
      const message =
        err instanceof ApiError
          ? err.message
          : 'Erreur inattendue lors du chargement des chaines causales.'
      setState({ status: 'error', message })
    })
}

function loadStatus(setState: (s: StatusState) => void) {
  setState({ status: 'loading' })
  fetchCausalReasoningStatus()
    .then((data) => setState({ status: 'ready', data }))
    .catch((err) => {
      const message =
        err instanceof ApiError
          ? err.message
          : 'Erreur inattendue lors du chargement du statut.'
      setState({ status: 'error', message })
    })
}

function runResultMessage(stats: CausalReasoningRunStats): { kind: 'error' | 'warning' | 'success'; text: string } {
  if (stats.error) {
    return { kind: 'error', text: `Echec de la generation : ${stats.error}` }
  }
  if (stats.quota_exhausted && stats.processed === 0) {
    return { kind: 'warning', text: 'Quota atteint pour aujourd\'hui, reessayez demain.' }
  }
  if (stats.processed === 0) {
    return {
      kind: 'warning',
      text: `Aucune chaine generee (${stats.failed} echec(s), ${stats.skipped_no_relations} ignoree(s) sans relation dans le graphe de connaissances).`,
    }
  }
  const failedSuffix = stats.failed ? ` ${stats.failed} echec(s).` : ''
  return {
    kind: 'success',
    text: `${stats.processed} nouvelle(s) chaine(s) de raisonnement causal generee(s).${failedSuffix}`,
  }
}

export function CausalReasoningPage() {
  const [chainsState, setChainsState] = useState<ChainsState>({ status: 'loading' })
  const [statusState, setStatusState] = useState<StatusState>({ status: 'loading' })
  const [runState, setRunState] = useState<RunState>({ status: 'idle' })

  const refresh = useCallback(() => {
    loadChains(setChainsState)
    loadStatus(setStatusState)
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  async function handleRun() {
    setRunState({ status: 'running' })
    try {
      const stats = await runCausalReasoning()
      setRunState({ status: 'done', stats })
      // Same "refresh after a click, no manual reload" behaviour as the
      // Streamlit button (load_causal_chains.clear() there) -- picks up
      // whatever was just generated and the now-updated quota/backlog.
      refresh()
    } catch (err) {
      const message =
        err instanceof ApiError ? err.message : "Erreur inattendue lors du recalcul."
      setRunState({ status: 'error', message })
    }
  }

  const busy = runState.status === 'running'
  const statusReady = statusState.status === 'ready' ? statusState.data : null
  const quotaExhausted = statusReady ? statusReady.quota_remaining <= 0 : false
  const nothingPending = statusReady ? statusReady.n_pending === 0 : false
  const buttonDisabled = busy || quotaExhausted || nothingPending || statusState.status !== 'ready'

  return (
    <div>
      <h1 className="text-2xl font-bold text-gray-900">Raisonnement causal</h1>

      <div className="mt-4 rounded-lg border border-gray-200 bg-white p-5 shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="text-sm text-gray-600">
            {statusState.status === 'loading' && 'Chargement du statut...'}
            {statusState.status === 'error' && (
              <span className="text-red-600">{statusState.message}</span>
            )}
            {statusState.status === 'ready' && (
              <>
                {statusState.data.n_pending} news eligible(s) en attente de traitement -- quota du
                jour : {statusState.data.quota_used}/{statusState.data.quota_limit} utilise(s),{' '}
                {statusState.data.quota_remaining} restant(s).
              </>
            )}
          </div>

          <button
            type="button"
            onClick={handleRun}
            disabled={buttonDisabled}
            className="flex items-center gap-2 rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy && (
              <span
                className="h-4 w-4 animate-spin rounded-full border-2 border-white/40 border-t-white"
                aria-hidden="true"
              />
            )}
            {busy ? 'Generation en cours...' : 'Recalculer maintenant'}
          </button>
        </div>

        {quotaExhausted && !busy && (
          <p className="mt-3 text-sm text-amber-700">
            Quota atteint pour aujourd'hui, reessayez demain.
          </p>
        )}

        {runState.status === 'error' && (
          <p className="mt-3 text-sm text-red-600">{runState.message}</p>
        )}

        {runState.status === 'done' && (() => {
          const result = runResultMessage(runState.stats)
          const cls =
            result.kind === 'error'
              ? 'text-red-600'
              : result.kind === 'warning'
                ? 'text-amber-700'
                : 'text-emerald-700'
          return <p className={`mt-3 text-sm ${cls}`}>{result.text}</p>
        })()}
      </div>

      <div className="mt-6">
        {chainsState.status === 'loading' && (
          <div className="flex items-center gap-3 text-gray-600">
            <span
              className="h-5 w-5 animate-spin rounded-full border-2 border-gray-300 border-t-indigo-600"
              aria-hidden="true"
            />
            Chargement des chaines causales...
          </div>
        )}

        {chainsState.status === 'error' && (
          <div className="rounded-md border border-red-200 bg-red-50 p-4 text-red-700">
            <p className="font-medium">Impossible de charger les chaines causales.</p>
            <p className="mt-1 text-sm">{chainsState.message}</p>
            <button
              type="button"
              onClick={() => loadChains(setChainsState)}
              className="mt-3 rounded-md bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-700"
            >
              Reessayer
            </button>
          </div>
        )}

        {chainsState.status === 'ready' && (
          <>
            {chainsState.data.staleness && (
              <p className="mb-3 text-sm text-amber-700">{chainsState.data.staleness}</p>
            )}

            {chainsState.data.chains.length === 0 ? (
              <div className="rounded-md border border-gray-200 bg-gray-50 p-4 text-gray-600">
                Aucune chaine de raisonnement causal generee pour l'instant. Utilisez le bouton
                "Recalculer maintenant" ci-dessus (limite par un quota Groq quotidien dedie).
              </div>
            ) : (
              <>
                <p className="mb-3 text-sm text-gray-500">
                  {chainsState.data.n_total} chaine(s) de raisonnement causal disponible(s),
                  triees par date decroissante.
                </p>
                <div className="flex flex-col gap-4">
                  {chainsState.data.chains.map((chain) => (
                    <ChainCard key={chain.id} chain={chain} />
                  ))}
                </div>
              </>
            )}
          </>
        )}
      </div>
    </div>
  )
}

function ChainCard({ chain }: { chain: CausalChain }) {
  const chainDate = (chain.created_at || '').slice(0, 10)
  const [expanded, setExpanded] = useState(false)
  const [stockState, setStockState] = useState<StockDetailState>({ status: 'loading' })

  useEffect(() => {
    if (!expanded) return
    setStockState({ status: 'loading' })
    fetchStockDetail(chain.ticker_source)
      .then((data) => setStockState({ status: 'ready', data }))
      .catch((err) => {
        const message = err instanceof ApiError ? err.message : "Erreur inattendue lors du chargement de l'action."
        setStockState({ status: 'error', message })
      })
  }, [expanded, chain.ticker_source])

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-lg font-semibold text-gray-900">
          {chain.ticker_source} <span className="font-normal text-gray-500">-- {chainDate}</span>
        </h3>
        <div className="flex items-center gap-2">
          {chain.confiance !== null && (
            <span className="rounded-full bg-gray-100 px-3 py-1 text-sm font-medium text-gray-700">
              Confiance : {chain.confiance.toFixed(0)}%
            </span>
          )}
          <button
            type="button"
            onClick={() => setExpanded(true)}
            className="rounded-md border border-gray-300 px-3 py-1 text-sm font-medium text-gray-600 hover:bg-gray-50"
          >
            Agrandir
          </button>
        </div>
      </div>

      <div className="mt-2">
        <CompanyDescription ticker={chain.ticker_source} />
      </div>

      <p className="mt-1 text-xs text-gray-500">
        News d'origine :{' '}
        {chain.news_title ? chain.news_title : `news_id=${chain.news_id} (titre indisponible)`}
      </p>

      <p className="mt-3 whitespace-pre-line text-sm text-gray-800">{chain.chaine_raisonnement}</p>

      {chain.entreprises_impactees.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {chain.entreprises_impactees.map((entry, i) => (
            <span
              key={i}
              className={`rounded-full px-2.5 py-1 text-xs font-medium ${effetStyle(entry.effet)}`}
            >
              {entry.entreprise}
              {entry.ticker ? ` (${entry.ticker})` : ''} -- {entry.effet}
            </span>
          ))}
        </div>
      )}

      {chain.model && <p className="mt-3 text-xs text-gray-400">Modele : {chain.model}</p>}

      <ExpandModal
        isOpen={expanded}
        onClose={() => setExpanded(false)}
        title={`${chain.ticker_source} -- chaine causale du ${chainDate}`}
      >
        <div className="flex flex-col gap-5">
          <CompanyDescription ticker={chain.ticker_source} />

          <p className="whitespace-pre-line text-base text-gray-800">{chain.chaine_raisonnement}</p>

          {chain.entreprises_impactees.length > 0 && (
            <div>
              <h3 className="text-sm font-semibold text-gray-900">Entreprises impactees</h3>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {chain.entreprises_impactees.map((entry, i) => (
                  <span
                    key={i}
                    className={`rounded-full px-2.5 py-1 text-xs font-medium ${effetStyle(entry.effet)}`}
                  >
                    {entry.entreprise}
                    {entry.ticker ? ` (${entry.ticker})` : ''} -- {entry.effet}
                  </span>
                ))}
              </div>
            </div>
          )}

          <div>
            <h3 className="text-sm font-semibold text-gray-900">
              Contexte actuel de {chain.ticker_source}
            </h3>
            {stockState.status === 'loading' && (
              <p className="mt-1 text-sm text-gray-500">Chargement...</p>
            )}
            {stockState.status === 'error' && (
              <p className="mt-1 text-sm text-red-600">{stockState.message}</p>
            )}
            {stockState.status === 'ready' && (
              <div className="mt-2">
                <PriceHeadline
                  price={stockState.data.prix_eur !== null ? stockState.data.prix_eur : stockState.data.current_price}
                  currency={stockState.data.prix_eur !== null ? 'EUR' : stockState.data.devise}
                  variationPct={stockState.data.variations ? stockState.data.variations['1j'] : null}
                />
                <div className="mt-3">
                  <DirectionProbabilityBar direction={stockState.data.direction_probabilities} />
                </div>
                <div className="mt-3 flex gap-6 text-xs">
                  <div>
                    <span className="text-gray-500">Score final</span>
                    <p className="text-sm font-semibold text-gray-900">
                      {stockState.data.final_score !== null ? stockState.data.final_score.toFixed(0) : 'n/a'}
                    </p>
                  </div>
                  <div>
                    <span className="text-gray-500">Confiance</span>
                    <p className="text-sm font-semibold text-gray-900">
                      {stockState.data.confidence !== null ? `${stockState.data.confidence.toFixed(0)}%` : 'n/a'}
                    </p>
                  </div>
                </div>
              </div>
            )}
          </div>

          {chain.news_title && (
            <p className="text-xs text-gray-500">News d'origine : {chain.news_title}</p>
          )}
          {chain.model && <p className="text-xs text-gray-400">Modele : {chain.model}</p>}
        </div>
      </ExpandModal>
    </div>
  )
}
