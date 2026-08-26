import { describe, expect, it } from 'vitest'
import { bestNameMatchDistance, isFuzzyNameMatch, levenshteinDistance } from './fuzzySearch'

describe('levenshteinDistance', () => {
  it('is 0 for identical strings', () => {
    expect(levenshteinDistance('apple', 'apple')).toBe(0)
  })

  it('counts a single substitution as distance 1', () => {
    expect(levenshteinDistance('apple', 'appla')).toBe(1)
  })

  it('counts a single deletion as distance 1', () => {
    expect(levenshteinDistance('microsoft', 'microsft')).toBe(1)
  })

  it('handles empty strings', () => {
    expect(levenshteinDistance('', 'abc')).toBe(3)
    expect(levenshteinDistance('abc', '')).toBe(3)
  })
})

describe('bestNameMatchDistance', () => {
  it('matches against a single word inside a longer company name', () => {
    // "aple" vs the word "apple" inside "apple inc." -- should not be
    // penalised for the rest of the name.
    expect(bestNameMatchDistance('aple', 'apple inc.')).toBe(1)
  })

  it('is 0 when the query equals a word in the name', () => {
    expect(bestNameMatchDistance('apple', 'apple inc.')).toBe(0)
  })
})

describe('isFuzzyNameMatch', () => {
  it('accepts a common typo on a short company name', () => {
    expect(isFuzzyNameMatch('aple', 'apple inc.')).toBe(true)
  })

  it('accepts a missing letter on a longer company name', () => {
    expect(isFuzzyNameMatch('microsft', 'microsoft corporation')).toBe(true)
  })

  it('rejects an unrelated query', () => {
    expect(isFuzzyNameMatch('xyzxyz', 'apple inc.')).toBe(false)
  })

  it('rejects a query too different even at a similar length', () => {
    expect(isFuzzyNameMatch('tesla', 'nvidia corporation')).toBe(false)
  })
})
