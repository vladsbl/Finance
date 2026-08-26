import { useState } from 'react'
import { ApiError, fetchArguedText } from '../api'
import { CompanyDescription } from './CompanyDescription'
import { DirectionProbabilityBar } from './DirectionProbabilityBar'
import { ExpandModal } from './ExpandModal'
import { PriceHeadline } from './PriceHeadline'
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
  const [expanded, setExpanded] = useState(false)

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
        <div className="flex items-center gap-2">
          <span className={`rounded-full px-3 py-1 text-sm font-medium ${riskBadgeClass(signal.risque)}`}>
            Risque : {signal.risque}
          </span>
          <button
            type="button"
            onClick={() => setExpanded(true)}
            className="rounded-md border border-gray-300 px-3 py-1 text-sm font-medium text-gray-600 hover:bg-gray-50"
          >
            Detail complet
          </button>
        </div>
      </div>

      <div className="mt-2">
        <CompanyDescription ticker={signal.ticker} />
      </div>

      {/* Price first, big and coloured -- ahead of every other score. */}
      {signal.prix && (
        <div className="mt-3">
          <PriceHeadline
            price={signal.prix.prix_actuel}
            currency={signal.prix.devise}
            variationPct={signal.prix.variations['1j']}
          />
          <p className="mt-1 text-xs text-gray-500">
            7j: {formatVariation(signal.prix.variations['7j'])} - 30j:{' '}
            {formatVariation(signal.prix.variations['30j'])}
          </p>
        </div>
      )}

      {/* Direction probabilities -- THE main forward-looking number now. */}
      <div className="mt-3">
        <DirectionProbabilityBar direction={signal.direction_probabilities} />
      </div>

      {/* Secondary scores -- smaller, below the price and the probabilities. */}
      <div className="mt-3 flex flex-wrap gap-6 text-xs">
        <div>
          <span className="text-gray-500">Score ajuste</span>
          <p className="text-sm font-semibold text-gray-900">{signal.score_ajuste.toFixed(1)}</p>
        </div>
        <div>
          <span className="text-gray-500">Confiance</span>
          <p className="text-sm font-semibold text-gray-900">{signal.confiance.toFixed(0)}%</p>
        </div>
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

      <ExpandModal
        isOpen={expanded}
        onClose={() => setExpanded(false)}
        title={`${signal.ticker} -- ${signal.nom_affiche}`}
      >
        <SignalDetail signal={signal} />
      </ExpandModal>
    </div>
  )
}

function ScoreCell({ label, value }: { label: string; value: number | null }) {
  return (
    <div className="rounded-md bg-gray-50 p-3">
      <p className="text-xs text-gray-500">{label}</p>
      <p className="text-lg font-semibold text-gray-900">{value !== null ? value.toFixed(0) : 'n/a'}</p>
    </div>
  )
}

// The compact SignalCard only ever shows score_ajuste + confiance + prix --
// the four component scores and entreprises_a_surveiller (both already in
// the Signal payload) have nowhere to live inline without cluttering every
// card, so the expanded view is where they actually become visible.
function SignalDetail({ signal }: { signal: Signal }) {
  const watchEntries = signal.entreprises_a_surveiller
    ? Object.entries(signal.entreprises_a_surveiller).filter(([, tickers]) => tickers.length > 0)
    : []

  return (
    <div className="flex flex-col gap-5">
      <CompanyDescription ticker={signal.ticker} />

      {signal.prix && (
        <div>
          <PriceHeadline
            price={signal.prix.prix_actuel}
            currency={signal.prix.devise}
            variationPct={signal.prix.variations['1j']}
          />
          <p className="mt-1 text-xs text-gray-500">
            7j: {formatVariation(signal.prix.variations['7j'])} - 30j:{' '}
            {formatVariation(signal.prix.variations['30j'])}
          </p>
        </div>
      )}

      <DirectionProbabilityBar direction={signal.direction_probabilities} />

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <ScoreCell label="Score global" value={signal.score_global} />
        <ScoreCell label="Score ajuste" value={signal.score_ajuste} />
        <ScoreCell label="Confiance" value={signal.confiance} />
        <ScoreCell label="Volatilite" value={signal.volatilite} />
      </div>

      <div>
        <h3 className="text-sm font-semibold text-gray-900">Composantes du score</h3>
        <div className="mt-2 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <ScoreCell label="Prix / Valorisation" value={signal.score_prix_valorisation} />
          <ScoreCell label="Technique" value={signal.score_technique} />
          <ScoreCell label="News" value={signal.score_news} />
          <ScoreCell label="Fondamental reel" value={signal.score_fondamental_reel} />
        </div>
        {signal.conflit_composantes && (
          <p className="mt-2 text-xs font-medium text-amber-700">
            Contradiction detectee entre les composantes structurelles.
          </p>
        )}
      </div>

      <div>
        <h3 className="text-sm font-semibold text-gray-900">Explication</h3>
        <p className="mt-1 text-sm text-gray-700">{signal.explication}</p>
        <p className="mt-1 text-xs text-gray-400">{signal.horizon}</p>
      </div>

      {watchEntries.length > 0 && (
        <div>
          <h3 className="text-sm font-semibold text-gray-900">Entreprises a surveiller</h3>
          <ul className="mt-2 flex flex-col gap-1.5 text-sm">
            {watchEntries.map(([relation, watchTickers]) => (
              <li key={relation}>
                <span className="font-medium text-gray-700">{relation}</span>
                <span className="text-gray-500"> : {watchTickers.join(', ')}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
