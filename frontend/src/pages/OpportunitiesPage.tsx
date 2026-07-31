import { useEffect, useState } from 'react'
import { ApiError, fetchOpportunites } from '../api'
import type { OpportunitesResponse, Priorite } from '../types'

type OppState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; data: OpportunitesResponse }

const PRIORITE_OPTIONS: Priorite[] = ['toutes', 'haute', 'moyenne', 'basse']

// Matches the backend's own default (api/routers/opportunities.py's
// DEFAULT_LIMIT) -- kept as an explicit constant here rather than relying
// on the server's default so the client's own page-count math
// (Math.ceil(n_total / PAGE_SIZE)) always agrees with what it actually
// requested, even if a caller changes PAGE_SIZE without touching the
// backend default.
const PAGE_SIZE = 50

function loadOpportunites(
  priorite: Priorite,
  page: number,
  setState: (s: OppState) => void,
) {
  setState({ status: 'loading' })
  fetchOpportunites(priorite, PAGE_SIZE, page * PAGE_SIZE)
    .then((data) => setState({ status: 'ready', data }))
    .catch((err) => {
      const message =
        err instanceof ApiError
          ? err.message
          : 'Erreur inattendue lors du chargement des opportunites.'
      setState({ status: 'error', message })
    })
}

function formatScore(score: number | null): string {
  return score === null ? 'n/a' : score.toFixed(1)
}

export function OpportunitiesPage() {
  const [priorite, setPriorite] = useState<Priorite>('toutes')
  const [page, setPage] = useState(0)
  const [state, setState] = useState<OppState>({ status: 'loading' })

  // Re-fetched whenever the priorite filter OR the page changes -- this is
  // a plain click-choice filter over a fixed, tiny set of 4 tiers (same
  // "not a searchable field" discipline as the Streamlit dashboard's
  // st.pills for this exact filter), not a free-text search, so refetching
  // per click/page is cheap and keeps the client simple (no need to fetch
  // every tier/page once and filter client-side).
  useEffect(() => {
    loadOpportunites(priorite, page, setState)
  }, [priorite, page])

  function handlePrioriteChange(next: Priorite) {
    setPriorite(next)
    setPage(0) // a new filter invalidates the old page count -- always restart at page 1
  }

  return (
    <div>
      <h1 className="text-2xl font-bold text-gray-900">Opportunites du jour</h1>

      <div className="mt-4 flex gap-2">
        {PRIORITE_OPTIONS.map((option) => (
          <button
            key={option}
            type="button"
            onClick={() => handlePrioriteChange(option)}
            className={`rounded-full px-4 py-1.5 text-sm font-medium ${
              priorite === option
                ? 'bg-indigo-600 text-white'
                : 'bg-gray-100 text-gray-700 hover:bg-gray-200'
            }`}
          >
            {option}
          </button>
        ))}
      </div>

      {state.status === 'loading' && (
        <div className="mt-8 flex items-center gap-3 text-gray-600">
          <span
            className="h-5 w-5 animate-spin rounded-full border-2 border-gray-300 border-t-indigo-600"
            aria-hidden="true"
          />
          Chargement des opportunites...
        </div>
      )}

      {state.status === 'error' && (
        <div className="mt-8 rounded-md border border-red-200 bg-red-50 p-4 text-red-700">
          <p className="font-medium">Impossible de charger les opportunites.</p>
          <p className="mt-1 text-sm">{state.message}</p>
          <button
            type="button"
            onClick={() => loadOpportunites(priorite, page, setState)}
            className="mt-3 rounded-md bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-700"
          >
            Reessayer
          </button>
        </div>
      )}

      {state.status === 'ready' && (
        <>
          {state.data.staleness && (
            <p className="mt-4 text-sm text-amber-700">{state.data.staleness}</p>
          )}
          <p className="mt-1 text-sm text-gray-500">{state.data.n_total} ticker(s).</p>

          {state.data.opportunites.length === 0 ? (
            <div className="mt-8 rounded-md border border-gray-200 bg-gray-50 p-4 text-gray-600">
              Aucune opportunite calculee pour cette priorite.
            </div>
          ) : (
            <div className="mt-4 overflow-x-auto">
              <table className="min-w-full divide-y divide-gray-200 text-sm">
                <thead>
                  <tr className="text-left text-gray-500">
                    <th className="py-2 pr-4">Ticker</th>
                    <th className="py-2 pr-4">Nom</th>
                    <th className="py-2 pr-4">Priorite</th>
                    <th className="py-2 pr-4">Score global</th>
                    <th className="py-2 pr-4">Prix/Valo</th>
                    <th className="py-2 pr-4">Technique</th>
                    <th className="py-2 pr-4">News</th>
                    <th className="py-2 pr-4">Fondamental reel</th>
                    <th className="py-2 pr-4">Confiance</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {state.data.opportunites.map((o) => (
                    <tr key={o.ticker} className="hover:bg-gray-50">
                      <td className="py-2 pr-4 font-medium text-gray-900">{o.ticker}</td>
                      <td className="py-2 pr-4 text-gray-700">{o.nom_affiche}</td>
                      <td className="py-2 pr-4 text-gray-500">{o.priorite}</td>
                      <td className="py-2 pr-4 font-semibold text-gray-900">
                        {formatScore(o.score_global)}
                      </td>
                      <td className="py-2 pr-4 text-gray-600">
                        {formatScore(o.score_prix_valorisation)}
                      </td>
                      <td className="py-2 pr-4 text-gray-600">{formatScore(o.score_technique)}</td>
                      <td className="py-2 pr-4 text-gray-600">{formatScore(o.score_news)}</td>
                      <td className="py-2 pr-4 text-gray-600">
                        {formatScore(o.score_fondamental_reel)}
                      </td>
                      <td className="py-2 pr-4 text-gray-600">
                        {o.confiance === null ? 'n/a' : `${o.confiance.toFixed(0)}%`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {state.data.n_total > 0 && (
            <div className="mt-4 flex items-center justify-between text-sm text-gray-600">
              <button
                type="button"
                onClick={() => setPage((p) => Math.max(0, p - 1))}
                disabled={page === 0}
                className="rounded-md border border-gray-300 px-3 py-1.5 font-medium disabled:cursor-not-allowed disabled:opacity-40 enabled:hover:bg-gray-50"
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
                className="rounded-md border border-gray-300 px-3 py-1.5 font-medium disabled:cursor-not-allowed disabled:opacity-40 enabled:hover:bg-gray-50"
              >
                Suivant
              </button>
            </div>
          )}
        </>
      )}
    </div>
  )
}
