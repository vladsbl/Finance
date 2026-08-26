import type { DirectionFilterValue, DirectionProbabilities } from '../types'

// Shared filter control for every list page that shows a
// direction_probabilities field (Resume du jour, Opportunites du jour,
// News & Analyse IA, Raisonnement causal) -- same button style as
// OpportunitiesPage.tsx's own pre-existing `priorite` filter (rounded-full,
// bg-indigo-600 when active, bg-gray-100/hover:bg-gray-200 otherwise) so the
// two filters read as one consistent family of controls.
const DIRECTION_OPTIONS: { value: DirectionFilterValue; label: string }[] = [
  { value: 'toutes', label: 'Toutes' },
  { value: 'hausse', label: 'Hausse probable' },
  { value: 'stagnation', label: 'Stagnation probable' },
  { value: 'baisse', label: 'Baisse probable' },
]

export function DirectionFilter({
  value,
  onChange,
}: {
  value: DirectionFilterValue
  onChange: (next: DirectionFilterValue) => void
}) {
  return (
    <div className="flex gap-2">
      {DIRECTION_OPTIONS.map((option) => (
        <button
          key={option.value}
          type="button"
          onClick={() => onChange(option.value)}
          className={`rounded-full px-4 py-1.5 text-sm font-medium ${
            value === option.value
              ? 'bg-indigo-600 text-white'
              : 'bg-gray-100 text-gray-700 hover:bg-gray-200'
          }`}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}

// TypeScript mirror of reasoning/direction_probability.py's
// dominant_direction() -- used only by the two pages that filter entirely
// client-side (DailySummaryPage, CausalReasoningPage) because their full
// list is already loaded in one shot with no server-side pagination;
// Opportunites and News have real pagination and filter via the backend's
// own `direction` query param instead (see api.ts's fetchOpportunites /
// fetchNews). Ties break toward stagnation, same conservative read as the
// Python original.
export function dominantDirection(direction: DirectionProbabilities): Exclude<DirectionFilterValue, 'toutes'> {
  const { hausse, stagnation, baisse } = direction
  if (hausse > stagnation && hausse > baisse) return 'hausse'
  if (baisse > stagnation && baisse > hausse) return 'baisse'
  return 'stagnation'
}
