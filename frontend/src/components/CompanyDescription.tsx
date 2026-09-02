import { useEffect, useState } from 'react'
import { ApiError, fetchCompanyDescription } from '../api'

type DescriptionState =
  | { status: 'loading' }
  | { status: 'ready'; description: string | null }

/**
 * Short French company description -- meant to be shown SYSTEMATICALLY
 * wherever a ticker is presented in detail (SignalCard, StockPage,
 * ChainCard, NewsCard, the correlations comparison view), same 5 spots and
 * same prominence convention as PriceHeadline: visible immediately, near
 * the top, never buried among secondary details.
 *
 * Backed by a PERMANENTLY cached endpoint (see
 * reasoning/company_description.py) -- a company's description never goes
 * stale, so this fetches once per mount and never re-generates; re-mounting
 * with the same ticker (e.g. re-opening a modal) just reads the cache
 * again, at no Groq cost.
 *
 * Renders nothing (not even a placeholder) when the description isn't
 * available yet (quota exhausted, not generated yet) -- a missing
 * description is a soft, expected gap in the still-backfilling universe,
 * not an error worth a "chargement..." flash or a red message.
 *
 * Usage:
 *   <h2>{ticker} -- {nom_affiche}</h2>
 *   <CompanyDescription ticker={ticker} />
 *   <PriceHeadline ... />
 */
export function CompanyDescription({ ticker }: { ticker: string }) {
  const [state, setState] = useState<DescriptionState>({ status: 'loading' })

  useEffect(() => {
    let cancelled = false
    setState({ status: 'loading' })
    fetchCompanyDescription(ticker)
      .then((result) => {
        if (!cancelled) setState({ status: 'ready', description: result.description })
      })
      .catch((err) => {
        // Non-critical, decorative content: a failed fetch (unknown
        // ticker, network hiccup) just means no description shows, never
        // an error banner on an otherwise-working page.
        void (err as ApiError)
        if (!cancelled) setState({ status: 'ready', description: null })
      })
    return () => {
      cancelled = true
    }
  }, [ticker])

  if (state.status === 'ready' && !state.description) return null

  return (
    <p className="text-sm text-ink/80">
      {state.status === 'loading' ? (
        <span className="italic text-faint">Chargement de la description...</span>
      ) : (
        state.description
      )}
    </p>
  )
}
