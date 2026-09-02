import { useEffect, useMemo, useState } from 'react'
import { ApiError, fetchDailySummary } from '../api'
import { DirectionFilter, dominantDirection } from '../components/DirectionFilter'
import { MacroContextSection } from '../components/MacroContextSection'
import { SignalCard } from '../components/SignalCard'
import type { DailySummaryResponse, DirectionFilterValue } from '../types'

type SummaryState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; data: DailySummaryResponse }

function loadSummary(setState: (s: SummaryState) => void) {
  setState({ status: 'loading' })
  fetchDailySummary()
    .then((data) => setState({ status: 'ready', data }))
    .catch((err) => {
      const message =
        err instanceof ApiError
          ? err.message
          : "Erreur inattendue lors du chargement du resume du jour."
      setState({ status: 'error', message })
    })
}

export function DailySummaryPage() {
  const [state, setState] = useState<SummaryState>({ status: 'loading' })
  const [direction, setDirection] = useState<DirectionFilterValue>('toutes')

  // Fetched once on mount only -- this page never auto-refreshes/polls,
  // matching the backend's own Groq-quota discipline: the argued-text
  // route (see SignalCard) is only ever called on an explicit button
  // click, never automatically.
  useEffect(() => {
    loadSummary(setState)
  }, [])

  // Filtered entirely client-side -- this list has no pagination (TOP_N is
  // a handful of signals, already loaded in full), so re-fetching from the
  // backend for a filter change would be wasted work. See
  // DirectionFilter.tsx's own docstring for why Opportunites/News instead
  // filter server-side.
  const filteredSignals = useMemo(() => {
    if (state.status !== 'ready') return []
    if (direction === 'toutes') return state.data.signals
    return state.data.signals.filter(
      (s) => s.direction_probabilities !== null && dominantDirection(s.direction_probabilities) === direction,
    )
  }, [state, direction])

  return (
    <div>
      <h1 className="jarvis-title text-4xl font-bold">Resume du jour</h1>

      <MacroContextSection />

      <div className="mt-4">
        <DirectionFilter value={direction} onChange={setDirection} />
      </div>

      {state.status === 'loading' && (
        <div className="mt-8 flex items-center gap-3 text-faint">
          <span className="jarvis-spinner h-5 w-5 animate-spin" aria-hidden="true" />
          Chargement des signaux du jour...
        </div>
      )}

      {state.status === 'error' && (
        <div className="jarvis-banner-error mt-8">
          <p className="font-medium">Impossible de charger le resume du jour.</p>
          <p className="mt-1 text-sm">{state.message}</p>
          <button
            type="button"
            onClick={() => loadSummary(setState)}
            className="jarvis-pill-danger mt-3"
          >
            Reessayer
          </button>
        </div>
      )}

      {state.status === 'ready' && (
        <>
          {state.data.staleness && (
            <p className="mt-2 text-sm text-amber-300">{state.data.staleness}</p>
          )}
          <p className="mt-1 text-sm text-faint">
            {state.data.signals.length} signal(aux) retenu(s) sur {state.data.n_candidates}{' '}
            candidat(s) eligible(s).
          </p>

          {state.data.signals.length === 0 ? (
            <div className="jarvis-empty mt-8">
              Aucun signal ne depasse le seuil de confiance aujourd'hui.
            </div>
          ) : filteredSignals.length === 0 ? (
            <div className="jarvis-empty mt-8">
              Aucun signal ne correspond a ce filtre de direction.
            </div>
          ) : (
            <div className="mt-6 flex flex-col gap-4">
              {filteredSignals.map((signal) => (
                <SignalCard key={signal.ticker} signal={signal} />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  )
}
