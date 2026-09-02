/**
 * Prominent price + variation display -- the price is meant to be the
 * first thing a reader sees on any ticker-detail surface (SignalCard,
 * StockPage, NewsCard, ChainCard, the correlations comparison view),
 * ahead of every other score. Colour follows the sign of `variationPct`
 * (green >= 0, red < 0); price itself is neutral dark when no variation is
 * available at all, since there is nothing to colour it by.
 *
 * Usage:
 *   <PriceHeadline price={106.2} currency="EUR" variationPct={1.4} variationLabel="1j" />
 */
interface PriceHeadlineProps {
  price: number | null
  currency: string
  variationPct: number | null
  variationLabel?: string
}

export function PriceHeadline({ price, currency, variationPct, variationLabel = '1j' }: PriceHeadlineProps) {
  if (price === null) {
    return <p className="text-sm text-faint">Prix indisponible</p>
  }

  const positive = variationPct !== null && variationPct >= 0
  const colorClass =
    variationPct === null ? 'text-ink' : positive ? 'text-emerald-400' : 'text-red-400'
  const glowClass =
    variationPct === null ? '' : positive ? 'drop-shadow-[0_0_10px_rgba(52,211,153,0.35)]' : 'drop-shadow-[0_0_10px_rgba(248,113,113,0.35)]'

  return (
    <div>
      <p className={`jarvis-metric text-3xl font-bold leading-tight ${colorClass} ${glowClass}`}>
        {price.toFixed(2)} {currency}
      </p>
      {variationPct !== null && (
        <p className={`jarvis-metric text-sm font-semibold ${colorClass}`}>
          {positive ? '+' : ''}
          {variationPct.toFixed(1)}% ({variationLabel})
        </p>
      )}
    </div>
  )
}
