import { useState } from 'react'
import { ApiError, fetchArguedText } from '../api'
import type { Signal } from '../types'

const RISK_STYLES: Record<string, string> = {
  Faible: 'bg-emerald-100 text-emerald-800',
  Modere: 'bg-amber-100 text-amber-800',
  Eleve: 'bg-red-100 text-red-800',
}

function riskBadgeClass(risque: string): string {
  return RISK_STYLES[risque] ?? 'bg-gray-100 text-gray-800'
}

function formatVariation(pct: number | null): string {
  if (pct === null) return 'n/a'
  const sign = pct >= 0 ? '+' : ''
  return `${sign}${pct.toFixed(1)}%`
}

// Argued-text load state kept local to each card: fetching one ticker's
// text must never block or reset the others (each button click is its own
// independent GET, matching the backend's per-ticker Groq-quota handling).
type ArguedTextState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'done'; texte: string | null; source: 'cache' | 'generated' | 'unavailable' }

export function SignalCard({ signal }: { signal: Signal }) {
  const [arguedText, setArguedText] = useState<ArguedTextState>({ status: 'idle' })

  async function handleLoadArguedText() {
    setArguedText({ status: 'loading' })
    try {
      const result = await fetchArguedText(signal.ticker)
      setArguedText({ status: 'done', texte: result.texte_argumente, source: result.source })
    } catch (err) {
      const message = err instanceof ApiError ? err.message : "Erreur inattendue lors de l'appel API."
      setArguedText({ status: 'error', message })
    }
  }

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-lg font-semibold text-gray-900">
          {signal.ticker} <span className="font-normal text-gray-500">-- {signal.nom_affiche}</span>
        </h3>
        <span className={`rounded-full px-3 py-1 text-sm font-medium ${riskBadgeClass(signal.risque)}`}>
          Risque : {signal.risque}
        </span>
      </div>

      <div className="mt-3 flex flex-wrap gap-6 text-sm">
        <div>
          <span className="text-gray-500">Score ajuste</span>
          <p className="text-xl font-semibold text-gray-900">{signal.score_ajuste.toFixed(1)}</p>
        </div>
        <div>
          <span className="text-gray-500">Confiance</span>
          <p className="text-xl font-semibold text-gray-900">{signal.confiance.toFixed(0)}%</p>
        </div>
        {signal.prix && (
          <div>
            <span className="text-gray-500">Prix actuel</span>
            <p className="text-xl font-semibold text-gray-900">
              {signal.prix.prix_actuel.toFixed(2)} {signal.prix.devise}
            </p>
            <p className="text-xs text-gray-500">
              1j: {formatVariation(signal.prix.variations['1j'])} - 7j:{' '}
              {formatVariation(signal.prix.variations['7j'])} - 30j:{' '}
              {formatVariation(signal.prix.variations['30j'])}
            </p>
          </div>
        )}
      </div>

      <p className="mt-3 text-sm text-gray-700">{signal.explication}</p>

      {signal.conflit_composantes && (
        <p className="mt-2 text-xs font-medium text-amber-700">
          Contradiction detectee entre les composantes structurelles.
        </p>
      )}

      <p className="mt-2 text-xs text-gray-400">{signal.horizon}</p>

      <div className="mt-4 border-t border-gray-100 pt-4">
        {arguedText.status === 'idle' && (
          <button
            type="button"
            onClick={handleLoadArguedText}
            className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700"
          >
            Voir l'analyse argumentee (IA)
          </button>
        )}

        {arguedText.status === 'loading' && (
          <div className="flex items-center gap-2 text-sm text-gray-500">
            <span
              className="h-4 w-4 animate-spin rounded-full border-2 border-gray-300 border-t-indigo-600"
              aria-hidden="true"
            />
            Generation en cours...
          </div>
        )}

        {arguedText.status === 'error' && (
          <div className="text-sm text-red-600">
            {arguedText.message}
            <button
              type="button"
              onClick={handleLoadArguedText}
              className="ml-2 font-medium underline hover:no-underline"
            >
              Reessayer
            </button>
          </div>
        )}

        {arguedText.status === 'done' && arguedText.texte && (
          <div>
            <p className="whitespace-pre-line text-sm text-gray-800">{arguedText.texte}</p>
            <p className="mt-2 text-xs text-gray-400">
              {arguedText.source === 'cache' ? 'Depuis le cache du jour' : 'Generee a l\'instant'}
            </p>
          </div>
        )}

        {arguedText.status === 'done' && !arguedText.texte && (
          <p className="text-sm text-gray-500">
            Analyse indisponible pour l'instant (quota Groq du jour atteint, cle API absente, ou
            erreur reseau cote serveur) -- voir le detail structure ci-dessus.
          </p>
        )}
      </div>
    </div>
  )
}
