import { useEffect, useState } from 'react'
import { ApiError, fetchDailySummary } from './api'
import { SignalCard } from './components/SignalCard'
import type { DailySummaryResponse } from './types'

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

function App() {
  const [state, setState] = useState<SummaryState>({ status: 'loading' })

  // Fetched once on mount only -- this page never auto-refreshes/polls,
  // matching the backend's own Groq-quota discipline: the argued-text
  // route (see SignalCard) is only ever called on an explicit button
  // click, never automatically.
  useEffect(() => {
    loadSummary(setState)
  }, [])

  return (
    <div className="mx-auto max-w-3xl px-4 py-8">
      <h1 className="text-2xl font-bold text-gray-900">Resume du jour</h1>

      {state.status === 'loading' && (
        <div className="mt-8 flex items-center gap-3 text-gray-600">
          <span
            className="h-5 w-5 animate-spin rounded-full border-2 border-gray-300 border-t-indigo-600"
            aria-hidden="true"
          />
          Chargement des signaux du jour...
        </div>
      )}

      {state.status === 'error' && (
        <div className="mt-8 rounded-md border border-red-200 bg-red-50 p-4 text-red-700">
          <p className="font-medium">Impossible de charger le resume du jour.</p>
          <p className="mt-1 text-sm">{state.message}</p>
          <button
            type="button"
            onClick={() => loadSummary(setState)}
            className="mt-3 rounded-md bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-700"
          >
            Reessayer
          </button>
        </div>
      )}

      {state.status === 'ready' && (
        <>
          {state.data.staleness && (
            <p className="mt-2 text-sm text-amber-700">{state.data.staleness}</p>
          )}
          <p className="mt-1 text-sm text-gray-500">
            {state.data.signals.length} signal(aux) retenu(s) sur {state.data.n_candidates}{' '}
            candidat(s) eligible(s).
          </p>

          {state.data.signals.length === 0 ? (
            <div className="mt-8 rounded-md border border-gray-200 bg-gray-50 p-4 text-gray-600">
              Aucun signal ne depasse le seuil de confiance aujourd'hui.
            </div>
          ) : (
            <div className="mt-6 flex flex-col gap-4">
              {state.data.signals.map((signal) => (
                <SignalCard key={signal.ticker} signal={signal} />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  )
}

export default App
