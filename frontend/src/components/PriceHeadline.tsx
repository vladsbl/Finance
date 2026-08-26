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
    return <p className="text-sm text-gray-500">Prix indisponible</p>
  }

  const positive = variationPct !== null && variationPct >= 0
  const colorClass =
    variationPct === null ? 'text-gray-900' : positive ? 'text-emerald-600' : 'text-red-600'

  return (
    <div>
      <p className={`text-3xl font-bold leading-tight ${colorClass}`}>
        {price.toFixed(2)} {currency}
      </p>
      {variationPct !== null && (
        <p className={`text-sm font-semibold ${colorClass}`}>
          {positive ? '+' : ''}
          {variationPct.toFixed(1)}% ({variationLabel})
        </p>
      )}
    </div>
  )
}
