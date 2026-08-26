// Small standalone Levenshtein-distance helper so TickerSearch can tolerate
// typos in a company NAME (never the ticker itself -- a ticker is a short,
// exact code, so "fixing" it would just guess wrong). Deliberately not a
// dependency (fuse.js and friends): the whole thing is two pure functions,
// easy to unit-test in isolation from the input component.

/** Classic edit distance (insertions/deletions/substitutions), case-sensitive. */
export function levenshteinDistance(a: string, b: string): number {
  if (a === b) return 0
  if (a.length === 0) return b.length
  if (b.length === 0) return a.length

  let previousRow = Array.from({ length: b.length + 1 }, (_, j) => j)

  for (let i = 1; i <= a.length; i++) {
    const currentRow = [i]
    for (let j = 1; j <= b.length; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1
      currentRow.push(
        Math.min(
          previousRow[j] + 1, // deletion
          currentRow[j - 1] + 1, // insertion
          previousRow[j - 1] + cost, // substitution
        ),
      )
    }
    previousRow = currentRow
  }

  return previousRow[b.length]
}

/** How many typos a query of this length is allowed before it stops matching. */
export function fuzzyMaxDistance(queryLength: number): number {
  if (queryLength <= 4) return 1
  return 2
}

/**
 * Smallest edit distance between `query` and either the whole `name` or any
 * single word within it (so "aple" can match "Apple Inc." via the word
 * "apple" without being penalised for the rest of the company name).
 * Both inputs are expected already lower-cased by the caller.
 */
export function bestNameMatchDistance(query: string, name: string): number {
  const words = name.split(/\s+/).filter(Boolean)
  let best = levenshteinDistance(query, name)
  for (const word of words) {
    best = Math.min(best, levenshteinDistance(query, word))
  }
  return best
}

/** True when `query` is within the typo budget of `name` (whole or per-word). */
export function isFuzzyNameMatch(query: string, name: string): boolean {
  return bestNameMatchDistance(query, name) <= fuzzyMaxDistance(query.length)
}
