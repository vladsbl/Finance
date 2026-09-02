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
        <p className="jarvis-metric text-xs font-medium uppercase tracking-wide text-cyan-300">
          Estimation {direction.horizon}
        </p>
      )}

      <div className="mt-1 flex items-center gap-5">
        <div>
          <p className="text-xs font-medium text-faint">Hausse</p>
          <p className="jarvis-metric text-2xl font-bold text-emerald-400">{direction.hausse}%</p>
        </div>
        <div>
          <p className="text-xs font-medium text-faint">Stagnation</p>
          <p className="jarvis-metric text-2xl font-bold text-slate-400">{direction.stagnation}%</p>
        </div>
        <div>
          <p className="text-xs font-medium text-faint">Baisse</p>
          <p className="jarvis-metric text-2xl font-bold text-red-400">{direction.baisse}%</p>
        </div>
      </div>

      <div className="mt-2 flex h-2 w-full max-w-sm overflow-hidden rounded-full bg-white/10" aria-hidden="true">
        <div className="bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.6)]" style={{ width: `${direction.hausse}%` }} />
        <div className="bg-slate-500" style={{ width: `${direction.stagnation}%` }} />
        <div className="bg-red-400 shadow-[0_0_8px_rgba(248,113,113,0.6)]" style={{ width: `${direction.baisse}%` }} />
      </div>

      <p className="mt-2 max-w-md text-xs italic text-faint">{direction.disclaimer}</p>
    </div>
  )
}
