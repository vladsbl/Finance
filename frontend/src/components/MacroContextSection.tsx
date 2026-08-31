import { useEffect, useState } from 'react'
import { ApiError, fetchMacroContext } from '../api'
import type { MacroContextResponse } from '../types'
import { MarkdownText } from './MarkdownText'

type MacroContextState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; data: MacroContextResponse }

function load(setState: (s: MacroContextState) => void) {
  setState({ status: 'loading' })
  fetchMacroContext()
    .then((data) => setState({ status: 'ready', data }))
    .catch((err) => {
      const message =
        err instanceof ApiError ? err.message : 'Erreur inattendue lors du chargement du contexte mondial.'
      setState({ status: 'error', message })
    })
}

/**
 * Sits at the top of Resume du jour -- gives the macro/geopolitical
 * backdrop BEFORE the per-ticker signals below it, same reasoning the task
 * that added this section gave for not making it a separate nav page: a
 * single daily paragraph (one Groq call/day, cached server-side) has no
 * list/pagination/filtering of its own, so a whole page would be mostly
 * empty chrome around one block of text.
 */
export function MacroContextSection() {
  const [state, setState] = useState<MacroContextState>({ status: 'loading' })

  useEffect(() => {
    load(setState)
  }, [])

  if (state.status === 'loading') {
    return (
      <div className="mt-4 flex items-center gap-2 text-sm text-gray-500">
        <span
          className="h-4 w-4 animate-spin rounded-full border-2 border-gray-300 border-t-indigo-600"
          aria-hidden="true"
        />
        Chargement du contexte mondial...
      </div>
    )
  }

  if (state.status === 'error') {
    return (
      <div className="mt-4 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700">
        {state.message}
        <button
          type="button"
          onClick={() => load(setState)}
          className="ml-2 font-medium underline hover:no-underline"
        >
          Reessayer
        </button>
      </div>
    )
  }

  const { data } = state

  // "unavailable" is a normal degraded state (nothing collected yet, quota
  // exhausted, no API key) -- same convention as SignalCard's argued-text
  // handling, never shown as an error.
  if (data.source === 'unavailable' || !data.texte_court) {
    return (
      <div className="mt-4 rounded-lg border border-gray-200 bg-gray-50 p-4 text-sm text-gray-500">
        Aucun contexte mondial disponible pour le moment (aucune source macro
        collectee recemment -- lancez `python ingestion/fetch_macro_news.py`).
      </div>
    )
  }

  return <MacroContextCard data={{ ...data, texte_court: data.texte_court }} />
}

function MacroContextCard({ data }: { data: MacroContextResponse & { texte_court: string } }) {
  const [detailed, setDetailed] = useState(false)
  const hasDetailed = Boolean(data.texte_detaille)

  return (
    <div className="mt-4 rounded-lg border border-gray-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-base font-semibold text-gray-900">Contexte mondial</h2>
        <div className="flex items-center gap-3">
          {hasDetailed && (
            <div className="flex rounded-md border border-gray-300 text-xs font-medium">
              <button
                type="button"
                onClick={() => setDetailed(false)}
                className={`rounded-l-md px-3 py-1 ${!detailed ? 'bg-indigo-600 text-white' : 'text-gray-600 hover:bg-gray-50'}`}
              >
                Version courte
              </button>
              <button
                type="button"
                onClick={() => setDetailed(true)}
                className={`rounded-r-md px-3 py-1 ${detailed ? 'bg-indigo-600 text-white' : 'text-gray-600 hover:bg-gray-50'}`}
              >
                Version detaillee
              </button>
            </div>
          )}
          <span className="text-xs text-gray-400">{data.n_sources} source(s)</span>
        </div>
      </div>

      <div className="mt-3">
        <MarkdownText>{detailed && data.texte_detaille ? data.texte_detaille : data.texte_court!}</MarkdownText>
      </div>

      {detailed && data.secteurs_a_surveiller.length > 0 && (
        <div className="mt-3 border-t border-gray-100 pt-3">
          <h3 className="text-xs font-semibold text-gray-500">Secteurs a surveiller</h3>
          <ul className="mt-2 flex flex-col gap-1.5">
            {data.secteurs_a_surveiller.map((s, i) => (
              <li key={i} className="text-xs">
                <span className="rounded-full bg-amber-50 px-2 py-0.5 font-medium text-amber-800">
                  {s.secteur}
                </span>{' '}
                <span className="text-gray-600">{s.raison}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {data.sources.length > 0 && (
        <details className="mt-3">
          <summary className="cursor-pointer text-xs font-medium text-gray-500 hover:text-gray-700">
            Sources citees ({data.sources.length})
          </summary>
          <ul className="mt-2 flex flex-col gap-1">
            {data.sources.map((s, i) => (
              <li key={i} className="text-xs">
                <span className="text-gray-400">[{s.source}]</span>{' '}
                {s.url ? (
                  <a href={s.url} target="_blank" rel="noreferrer" className="text-indigo-700 hover:underline">
                    {s.title}
                  </a>
                ) : (
                  <span className="text-gray-700">{s.title}</span>
                )}{' '}
                <span className="text-gray-400">{(s.published_at || '').slice(0, 10)}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}
