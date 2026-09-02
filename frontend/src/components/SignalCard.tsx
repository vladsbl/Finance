import { useState } from 'react'
import { ApiError, fetchArguedText } from '../api'
import { CompanyDescription } from './CompanyDescription'
import { DirectionProbabilityBar } from './DirectionProbabilityBar'
import { ExpandModal } from './ExpandModal'
import { PriceHeadline } from './PriceHeadline'
import type { Signal } from '../types'

const RISK_STYLES: Record<string, string> = {
  Faible: 'bg-emerald-400/15 text-emerald-300',
  Modere: 'bg-amber-400/15 text-amber-300',
  Eleve: 'bg-red-400/15 text-red-300',
}

function riskBadgeClass(risque: string): string {
  return RISK_STYLES[risque] ?? 'bg-white/10 text-ink/80'
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
    <div className="jarvis-card p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="jarvis-heading text-lg font-bold">
          {signal.ticker} <span className="font-normal text-faint">-- {signal.nom_affiche}</span>
        </h3>
        <div className="flex items-center gap-2">
          <span className={`rounded-full px-3 py-1 text-sm font-medium ${riskBadgeClass(signal.risque)}`}>
            Risque : {signal.risque}
          </span>
          <button
            type="button"
            onClick={() => setExpanded(true)}
            className="jarvis-pill !py-1 text-xs"
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
          <p className="mt-1 text-xs text-faint">
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
          <span className="text-faint">Score ajuste</span>
          <p className="jarvis-metric text-sm font-semibold text-ink">{signal.score_ajuste.toFixed(1)}</p>
        </div>
        <div>
          <span className="text-faint">Confiance</span>
          <p className="jarvis-metric text-sm font-semibold text-ink">{signal.confiance.toFixed(0)}%</p>
        </div>
      </div>

      <p className="mt-3 text-sm text-ink/80">{signal.explication}</p>

      {signal.conflit_composantes && (
        <p className="mt-2 text-xs font-medium text-amber-300">
          Contradiction detectee entre les composantes structurelles.
        </p>
      )}

      <p className="mt-2 text-xs text-slate-500">{signal.horizon}</p>

      <div className="mt-4 border-t border-cyan-400/15 pt-4">
        {arguedText.status === 'idle' && (
          <button
            type="button"
            onClick={handleLoadArguedText}
            className="jarvis-pill-primary"
          >
            Voir l'analyse argumentee (IA)
          </button>
        )}

        {arguedText.status === 'loading' && (
          <div className="flex items-center gap-2 text-sm text-faint">
            <span
              className="h-4 w-4 animate-spin rounded-full border-2 border-cyan-400/20 border-t-cyan-400"
              aria-hidden="true"
            />
            Generation en cours...
          </div>
        )}

        {arguedText.status === 'error' && (
          <div className="text-sm text-red-400">
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
            <p className="whitespace-pre-line text-sm text-ink/80">{arguedText.texte}</p>
            <p className="mt-2 text-xs text-slate-500">
              {arguedText.source === 'cache' ? 'Depuis le cache du jour' : 'Generee a l\'instant'}
            </p>
          </div>
        )}

        {arguedText.status === 'done' && !arguedText.texte && (
          <p className="text-sm text-faint">
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
    <div className="rounded-xl border border-cyan-400/10 bg-white/5 p-3">
      <p className="text-xs text-faint">{label}</p>
      <p className="jarvis-metric text-lg font-semibold text-ink">{value !== null ? value.toFixed(0) : 'n/a'}</p>
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
          <p className="mt-1 text-xs text-faint">
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
        <h3 className="jarvis-heading text-sm font-bold">Composantes du score</h3>
        <div className="mt-2 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <ScoreCell label="Prix / Valorisation" value={signal.score_prix_valorisation} />
          <ScoreCell label="Technique" value={signal.score_technique} />
          <ScoreCell label="News" value={signal.score_news} />
          <ScoreCell label="Fondamental reel" value={signal.score_fondamental_reel} />
        </div>
        {signal.conflit_composantes && (
          <p className="mt-2 text-xs font-medium text-amber-300">
            Contradiction detectee entre les composantes structurelles.
          </p>
        )}
      </div>

      <div>
        <h3 className="jarvis-heading text-sm font-bold">Explication</h3>
        <p className="mt-1 text-sm text-ink/80">{signal.explication}</p>
        <p className="mt-1 text-xs text-slate-500">{signal.horizon}</p>
      </div>

      {watchEntries.length > 0 && (
        <div>
          <h3 className="jarvis-heading text-sm font-bold">Entreprises a surveiller</h3>
          <ul className="mt-2 flex flex-col gap-1.5 text-sm">
            {watchEntries.map(([relation, watchTickers]) => (
              <li key={relation}>
                <span className="font-medium text-ink/80">{relation}</span>
                <span className="text-faint"> : {watchTickers.join(', ')}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
