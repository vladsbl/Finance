import { useMemo, useRef, useState } from 'react'
import { bestNameMatchDistance, fuzzyMaxDistance } from '../fuzzySearch'
import type { TickerListEntry } from '../types'

const MAX_SUGGESTIONS = 20

// Fuzzy fallback only kicks in past this length -- below it, a couple of
// typo-tolerant edits would match almost anything and the suggestion list
// stops being useful.
const MIN_QUERY_LENGTH_FOR_FUZZY = 3

interface TickerSearchProps {
  tickers: TickerListEntry[]
  onSelect: (ticker: string) => void
  placeholder?: string
}

// A real filtered-list search box, not a native <select> -- with ~2000
// tickers, a <select> would force the browser to render every option up
// front and gives no way to search by company name, only by scrolling.
// Filtering happens entirely client-side against the already-loaded
// GET /api/tickers list (fetched once by the parent), so keystrokes never
// hit the network.
export function TickerSearch({ tickers, onSelect, placeholder }: TickerSearchProps) {
  const [query, setQuery] = useState('')
  const [isOpen, setIsOpen] = useState(false)
  const [activeIndex, setActiveIndex] = useState(-1)
  const inputRef = useRef<HTMLInputElement>(null)

  const suggestions = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return []

    const substringMatches = tickers.filter(
      (t) => t.ticker.toLowerCase().includes(q) || t.nom_affiche.toLowerCase().includes(q),
    )
    if (substringMatches.length > 0) return substringMatches.slice(0, MAX_SUGGESTIONS)

    // Nothing matched literally -- fall back to a typo-tolerant search on
    // the company NAME only (never the ticker: a ticker is a short exact
    // code, "correcting" it would just be a wrong guess). Handles "Aple" or
    // "Microsft" still finding Apple / Microsoft.
    if (q.length < MIN_QUERY_LENGTH_FOR_FUZZY) return []
    const maxDist = fuzzyMaxDistance(q.length)
    const fuzzyMatches = tickers
      .map((t) => ({ t, dist: bestNameMatchDistance(q, t.nom_affiche.toLowerCase()) }))
      .filter((entry) => entry.dist <= maxDist)
      .sort((a, b) => a.dist - b.dist)
      .map((entry) => entry.t)
    return fuzzyMatches.slice(0, MAX_SUGGESTIONS)
  }, [tickers, query])

  function pick(entry: TickerListEntry) {
    onSelect(entry.ticker)
    setQuery(`${entry.ticker} - ${entry.nom_affiche}`)
    setIsOpen(false)
    setActiveIndex(-1)
    inputRef.current?.blur()
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (!isOpen || suggestions.length === 0) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActiveIndex((i) => Math.min(i + 1, suggestions.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActiveIndex((i) => Math.max(i - 1, 0))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      if (activeIndex >= 0) pick(suggestions[activeIndex])
    } else if (e.key === 'Escape') {
      setIsOpen(false)
      setActiveIndex(-1)
    }
  }

  return (
    <div className="relative w-full max-w-md">
      <input
        ref={inputRef}
        type="text"
        value={query}
        placeholder={placeholder ?? 'Rechercher un ticker ou une entreprise...'}
        onChange={(e) => {
          setQuery(e.target.value)
          setIsOpen(true)
          setActiveIndex(-1)
        }}
        onFocus={() => setIsOpen(true)}
        onBlur={() => {
          // Delay so a click on a suggestion registers before the list unmounts.
          setTimeout(() => setIsOpen(false), 150)
        }}
        onKeyDown={handleKeyDown}
        className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
      />

      {isOpen && suggestions.length > 0 && (
        <ul className="absolute z-10 mt-1 max-h-72 w-full overflow-y-auto rounded-md border border-gray-200 bg-white text-sm shadow-lg">
          {suggestions.map((entry, i) => (
            <li key={entry.ticker}>
              <button
                type="button"
                onMouseDown={(e) => e.preventDefault()} // keep input focus so onBlur doesn't fire first
                onClick={() => pick(entry)}
                className={`flex w-full items-center justify-between gap-2 px-3 py-2 text-left ${
                  i === activeIndex ? 'bg-indigo-50' : 'hover:bg-gray-50'
                }`}
              >
                <span className="font-medium text-gray-900">{entry.ticker}</span>
                <span className="truncate text-gray-500">{entry.nom_affiche}</span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {isOpen && query.trim() && suggestions.length === 0 && (
        <div className="absolute z-10 mt-1 w-full rounded-md border border-gray-200 bg-white px-3 py-2 text-sm text-gray-500 shadow-lg">
          Aucun resultat pour « {query} ».
        </div>
      )}
    </div>
  )
}
