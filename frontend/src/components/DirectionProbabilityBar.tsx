import type { DirectionProbabilities } from '../types'

/**
 * The hausse/stagnation/baisse split -- meant to be THE primary number
 * shown on any ticker-detail surface, ahead of the older secondary scores
 * (score global, confiance, volatilite, sous-scores). Always renders its
 * `disclaimer` alongside the percentages: this is a qualitative estimate,
 * never a statistical forecast (see reasoning/direction_probability.py).
 *
 * Usage:
 *   <DirectionProbabilityBar direction={signal.direction_probabilities} />
 *
 * Renders nothing when `direction` is null (ticker had neither a
 * technical nor a price/valuation score to compute from).
 */
export function DirectionProbabilityBar({ direction }: { direction: DirectionProbabilities | null }) {
  if (!direction) return null

  return (
    <div>
      {direction.horizon && (
        <p className="text-xs font-medium uppercase tracking-wide text-indigo-600">
          Estimation {direction.horizon}
        </p>
      )}

      <div className="mt-1 flex items-center gap-5">
        <div>
          <p className="text-xs font-medium text-gray-500">Hausse</p>
          <p className="text-2xl font-bold text-emerald-600">{direction.hausse}%</p>
        </div>
        <div>
          <p className="text-xs font-medium text-gray-500">Stagnation</p>
          <p className="text-2xl font-bold text-gray-500">{direction.stagnation}%</p>
        </div>
        <div>
          <p className="text-xs font-medium text-gray-500">Baisse</p>
          <p className="text-2xl font-bold text-red-600">{direction.baisse}%</p>
        </div>
      </div>

      <div className="mt-2 flex h-2 w-full max-w-sm overflow-hidden rounded-full bg-gray-100" aria-hidden="true">
        <div className="bg-emerald-500" style={{ width: `${direction.hausse}%` }} />
        <div className="bg-gray-300" style={{ width: `${direction.stagnation}%` }} />
        <div className="bg-red-500" style={{ width: `${direction.baisse}%` }} />
      </div>

      <p className="mt-2 max-w-md text-xs italic text-gray-500">{direction.disclaimer}</p>
    </div>
  )
}
