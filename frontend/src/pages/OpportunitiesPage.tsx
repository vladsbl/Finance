import { useEffect, useState } from 'react'
import { ApiError, fetchOpportunites } from '../api'
import { DirectionFilter, dominantDirection } from '../components/DirectionFilter'
import type { DirectionFilterValue, OpportunitesResponse, Priorite } from '../types'

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
  direction: DirectionFilterValue,
  setState: (s: OppState) => void,
) {
  setState({ status: 'loading' })
  fetchOpportunites(priorite, PAGE_SIZE, page * PAGE_SIZE, direction)
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

const DIRECTION_LABELS: Record<Exclude<DirectionFilterValue, 'toutes'>, string> = {
  hausse: 'Hausse',
  stagnation: 'Stagnation',
  baisse: 'Baisse',
}

const DIRECTION_COLORS: Record<Exclude<DirectionFilterValue, 'toutes'>, string> = {
  hausse: 'text-emerald-400',
  stagnation: 'text-slate-400',
  baisse: 'text-red-400',
}

export function OpportunitiesPage() {
  const [priorite, setPriorite] = useState<Priorite>('toutes')
  const [direction, setDirection] = useState<DirectionFilterValue>('toutes')
  const [page, setPage] = useState(0)
  const [state, setState] = useState<OppState>({ status: 'loading' })

  // Re-fetched whenever the priorite filter, the direction filter, OR the
  // page changes -- `direction` is applied server-side (see api.ts's
  // fetchOpportunites and api/routers/opportunities.py's own `direction`
  // param), same reasoning as `priorite`: this list genuinely paginates
  // (up to ~2000 rows), so a client-side filter would only narrow whatever
  // page happens to be loaded.
  useEffect(() => {
    loadOpportunites(priorite, page, direction, setState)
  }, [priorite, direction, page])

  function handlePrioriteChange(next: Priorite) {
    setPriorite(next)
    setPage(0) // a new filter invalidates the old page count -- always restart at page 1
  }

  function handleDirectionChange(next: DirectionFilterValue) {
    setDirection(next)
    setPage(0)
  }

  return (
    <div>
      <h1 className="jarvis-title text-4xl font-bold">Opportunites du jour</h1>

      <div className="mt-4 flex flex-wrap gap-2">
        {PRIORITE_OPTIONS.map((option) => (
          <button
            key={option}
            type="button"
            onClick={() => handlePrioriteChange(option)}
            className={`jarvis-pill ${priorite === option ? 'jarvis-pill-active' : ''}`}
          >
            {option}
          </button>
        ))}
      </div>

      <div className="mt-3">
        <DirectionFilter value={direction} onChange={handleDirectionChange} />
      </div>

      {state.status === 'loading' && (
        <div className="mt-8 flex items-center gap-3 text-faint">
          <span className="jarvis-spinner h-5 w-5 animate-spin" aria-hidden="true" />
          Chargement des opportunites...
        </div>
      )}

      {state.status === 'error' && (
        <div className="jarvis-banner-error mt-8">
          <p className="font-medium">Impossible de charger les opportunites.</p>
          <p className="mt-1 text-sm">{state.message}</p>
          <button
            type="button"
            onClick={() => loadOpportunites(priorite, page, direction, setState)}
            className="jarvis-pill-danger mt-3"
          >
            Reessayer
          </button>
        </div>
      )}

      {state.status === 'ready' && (
        <>
          {state.data.staleness && (
            <p className="mt-4 text-sm text-amber-300">{state.data.staleness}</p>
          )}
          <p className="mt-1 text-sm text-faint">{state.data.n_total} ticker(s).</p>

          {state.data.opportunites.length === 0 ? (
            <div className="jarvis-empty mt-8">
              Aucune opportunite calculee pour cette priorite.
            </div>
          ) : (
            <div className="jarvis-card mt-4 overflow-x-auto p-2">
              <table className="min-w-full divide-y divide-cyan-400/10 text-sm">
                <thead>
                  <tr className="text-left text-faint">
                    <th className="px-2 py-2">Ticker</th>
                    <th className="px-2 py-2">Nom</th>
                    <th className="px-2 py-2">Priorite</th>
                    <th className="px-2 py-2">Score global</th>
                    <th className="px-2 py-2">Prix/Valo</th>
                    <th className="px-2 py-2">Technique</th>
                    <th className="px-2 py-2">News</th>
                    <th className="px-2 py-2">Fondamental reel</th>
                    <th className="px-2 py-2">Confiance</th>
                    <th className="px-2 py-2">Direction</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-cyan-400/5">
                  {state.data.opportunites.map((o) => (
                    <tr key={o.ticker} className="transition-colors hover:bg-white/5">
                      <td className="jarvis-metric px-2 py-2 font-medium text-cyan-200">{o.ticker}</td>
                      <td className="px-2 py-2 text-ink/80">{o.nom_affiche}</td>
                      <td className="px-2 py-2 text-faint">{o.priorite}</td>
                      <td className="jarvis-metric px-2 py-2 font-semibold text-ink">
                        {formatScore(o.score_global)}
                      </td>
                      <td className="jarvis-metric px-2 py-2 text-ink/70">
                        {formatScore(o.score_prix_valorisation)}
                      </td>
                      <td className="jarvis-metric px-2 py-2 text-ink/70">{formatScore(o.score_technique)}</td>
                      <td className="jarvis-metric px-2 py-2 text-ink/70">{formatScore(o.score_news)}</td>
                      <td className="jarvis-metric px-2 py-2 text-ink/70">
                        {formatScore(o.score_fondamental_reel)}
                      </td>
                      <td className="jarvis-metric px-2 py-2 text-ink/70">
                        {o.confiance === null ? 'n/a' : `${o.confiance.toFixed(0)}%`}
                      </td>
                      <td className="px-2 py-2">
                        {o.direction_probabilities === null ? (
                          <span className="text-slate-500">n/a</span>
                        ) : (
                          <span className={`font-medium ${DIRECTION_COLORS[dominantDirection(o.direction_probabilities)]}`}>
                            {DIRECTION_LABELS[dominantDirection(o.direction_probabilities)]}
                          </span>
                        )}
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
    </div>
  )
}
