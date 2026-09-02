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
// Extracted from TickerSearch's own render so a second, differently-styled
// input (NewsPage.tsx's free-text search, which layers ticker suggestions
// on top of its own multi-field search) can compute the exact same
// substring-then-fuzzy suggestion list without duplicating -- or drifting
// from -- this matching logic.
export function useTickerSuggestions(tickers: TickerListEntry[], query: string): TickerListEntry[] {
  return useMemo(() => {
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
}

// Presentational dropdown, also shared with NewsPage.tsx's own search
// input -- so the two suggestion lists look and behave identically
// (same active-row highlight, same mousedown-preventDefault trick to keep
// the input focused through a click) without copy-pasting the markup.
export function TickerSuggestionsList({
  suggestions,
  activeIndex,
  onPick,
}: {
  suggestions: TickerListEntry[]
  activeIndex: number
  onPick: (entry: TickerListEntry) => void
}) {
  return (
    <ul className="absolute z-10 mt-1 max-h-72 w-full overflow-y-auto rounded-xl border border-cyan-400/25 bg-navy-900/95 text-sm shadow-2xl backdrop-blur-md">
      {suggestions.map((entry, i) => (
        <li key={entry.ticker}>
          <button
            type="button"
            onMouseDown={(e) => e.preventDefault()} // keep input focus so onBlur doesn't fire first
            onClick={() => onPick(entry)}
            className={`flex w-full items-center justify-between gap-2 px-3 py-2 text-left ${
              i === activeIndex ? 'bg-cyan-400/15' : 'hover:bg-white/5'
            }`}
          >
            <span className="jarvis-metric font-medium text-cyan-200">{entry.ticker}</span>
            <span className="truncate text-faint">{entry.nom_affiche}</span>
          </button>
        </li>
      ))}
    </ul>
  )
}

export function TickerSearch({ tickers, onSelect, placeholder }: TickerSearchProps) {
  const [query, setQuery] = useState('')
  const [isOpen, setIsOpen] = useState(false)
  const [activeIndex, setActiveIndex] = useState(-1)
  const inputRef = useRef<HTMLInputElement>(null)

  const suggestions = useTickerSuggestions(tickers, query)

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
        className="w-full rounded-full border border-cyan-400/25 bg-navy-800/50 px-4 py-2 text-sm text-ink placeholder:text-faint backdrop-blur-md transition-all focus:border-cyan-300/70 focus:outline-none focus:ring-1 focus:ring-cyan-300/50"
      />

      {isOpen && suggestions.length > 0 && (
        <TickerSuggestionsList suggestions={suggestions} activeIndex={activeIndex} onPick={pick} />
      )}

      {isOpen && query.trim() && suggestions.length === 0 && (
        <div className="absolute z-10 mt-1 w-full rounded-xl border border-cyan-400/25 bg-navy-900/95 px-3 py-2 text-sm text-faint shadow-2xl backdrop-blur-md">
          Aucun resultat pour « {query} ».
        </div>
      )}
    </div>
  )
}
