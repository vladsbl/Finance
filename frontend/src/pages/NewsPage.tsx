import { useEffect, useState } from 'react'
import { ApiError, fetchNews, fetchNewsFacets, fetchNewsNarrative, fetchStockDetail, fetchTickers } from '../api'
import { CompanyDescription } from '../components/CompanyDescription'
import { DirectionFilter, dominantDirection } from '../components/DirectionFilter'
import { DirectionProbabilityBar } from '../components/DirectionProbabilityBar'
import { ExpandModal } from '../components/ExpandModal'
import { MarkdownText } from '../components/MarkdownText'
import { PriceHeadline } from '../components/PriceHeadline'
import { TickerSuggestionsList, useTickerSuggestions } from '../components/TickerSearch'
import type {
  DirectionFilterValue,
  DirectionProbabilities,
  NewsFacetsResponse,
  NewsItem,
  NewsNarrativeSource,
  NewsPriceContext,
  NewsResponse,
  StockDetail,
  TickerListEntry,
} from '../types'

type StockDetailState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; data: StockDetail }

// Kept local to each card: opening one news item's enriched narrative must
// never block or reset another's (each click is its own independent GET,
// matching the backend's own dedicated per-news-item quota/cache).
type NarrativeState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | {
      status: 'done'
      texte: string | null
      source: NewsNarrativeSource
      direction: DirectionProbabilities | null
    }

type NewsState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; data: NewsResponse }

type FacetsState =
  | { status: 'loading' }
  | { status: 'error' }
  | { status: 'ready'; data: NewsFacetsResponse }

type TickersState =
  | { status: 'loading' }
  | { status: 'error' }
  | { status: 'ready'; tickers: TickerListEntry[] }

// Matches the backend's own default (api/routers/news.py's DEFAULT_LIMIT).
const PAGE_SIZE = 50

// Same debounce window as CorrelationsPage.tsx's own free-text search --
// long enough that typing a word fires ONE request instead of one per
// character, short enough that the list still feels responsive.
const SEARCH_DEBOUNCE_MS = 300

const TONALITE_STYLES: Record<string, string> = {
  positive: 'bg-emerald-400/15 text-emerald-300',
  negative: 'bg-red-400/15 text-red-300',
  neutre: 'bg-white/10 text-ink/80',
}

function tonaliteStyle(tonalite: string): string {
  const key = (tonalite || '').toLowerCase()
  if (key.startsWith('pos')) return TONALITE_STYLES.positive
  if (key.startsWith('neg')) return TONALITE_STYLES.negative
  return TONALITE_STYLES.neutre
}

function formatPriceContext(ctx: NewsPriceContext): string {
  if (ctx.insufficient_data) {
    const suffix = ctx.insufficient_reason ?? ''
    if (ctx.price_before_eur !== null && ctx.date_before) {
      return `Donnee insuffisante : dernier prix disponible ${ctx.price_before_eur.toFixed(2)} EUR (${ctx.date_before}). ${suffix}`
    }
    return `Donnee insuffisante : ${suffix}`
  }
  const sign = (ctx.variation_pct ?? 0) >= 0 ? '+' : ''
  return (
    `${ctx.price_before_eur!.toFixed(2)} EUR (${ctx.date_before}) -> ` +
    `${ctx.price_after_eur!.toFixed(2)} EUR (${ctx.date_after}), ` +
    `${sign}${ctx.variation_pct!.toFixed(1)}%`
  )
}

const DIRECTION_LABELS: Record<Exclude<DirectionFilterValue, 'toutes'>, string> = {
  hausse: 'Hausse',
  stagnation: 'Stagnation',
  baisse: 'Baisse',
}

const DIRECTION_BADGE_STYLES: Record<Exclude<DirectionFilterValue, 'toutes'>, string> = {
  hausse: 'bg-emerald-400/15 text-emerald-300',
  stagnation: 'bg-white/10 text-ink/80',
  baisse: 'bg-red-400/15 text-red-300',
}

function loadNews(
  filters: { ticker?: string; search?: string; sector: string; zone: string; direction: DirectionFilterValue },
  page: number,
  setState: (s: NewsState) => void,
) {
  setState({ status: 'loading' })
  fetchNews(PAGE_SIZE, page * PAGE_SIZE, filters)
    .then((data) => setState({ status: 'ready', data }))
    .catch((err) => {
      const message =
        err instanceof ApiError ? err.message : 'Erreur inattendue lors du chargement des news.'
      setState({ status: 'error', message })
    })
}

function loadFacets(setState: (s: FacetsState) => void) {
  setState({ status: 'loading' })
  fetchNewsFacets()
    .then((data) => setState({ status: 'ready', data }))
    // Facets are a filter-building convenience, not core content -- a
    // failure here degrades to "no sector/zone filter shown" rather than
    // blocking the news list itself (search/direction still work fine).
    .catch(() => setState({ status: 'error' }))
}

function loadTickers(setState: (s: TickersState) => void) {
  setState({ status: 'loading' })
  fetchTickers()
    .then((data) => setState({ status: 'ready', tickers: data.tickers }))
    // Same convenience-not-core-content convention as loadFacets: a
    // failure here just means no ticker suggestions dropdown, never a
    // blocked news list (free-text search keeps working regardless).
    .catch(() => setState({ status: 'error' }))
}

export function NewsPage() {
  // `search` is what is in the input -- updated on every keystroke so the
  // field stays responsive. `appliedSearch` is what has actually been sent
  // to the API, updated only once typing pauses (debounce effect below) --
  // same split as CorrelationsPage.tsx's own free-text search.
  const [search, setSearch] = useState('')
  const [appliedSearch, setAppliedSearch] = useState('')
  // Set only by picking a ticker suggestion below -- while non-null, the
  // fetch effect uses the backend's EXACT `ticker` filter instead of
  // `search` (closer to the old ticker-only filter's precision: a
  // suggestion click means "show me this ticker", not "find text
  // resembling this ticker"). Cleared the moment the user types anything
  // else, so free-text search resumes without a stale ticker lock.
  const [selectedTicker, setSelectedTicker] = useState<string | null>(null)
  const [showSuggestions, setShowSuggestions] = useState(false)
  const [activeIndex, setActiveIndex] = useState(-1)
  const [sector, setSector] = useState('')
  const [zone, setZone] = useState('')
  const [direction, setDirection] = useState<DirectionFilterValue>('toutes')
  const [page, setPage] = useState(0)
  const [newsState, setNewsState] = useState<NewsState>({ status: 'loading' })
  const [facetsState, setFacetsState] = useState<FacetsState>({ status: 'loading' })
  const [tickersState, setTickersState] = useState<TickersState>({ status: 'loading' })

  useEffect(() => {
    loadFacets(setFacetsState)
    loadTickers(setTickersState)
  }, [])

  // Same substring-then-fuzzy matching TickerSearch itself uses -- reacts
  // to the LIVE `search` value (not the debounced `appliedSearch`), since
  // suggestions must appear while typing, not 300ms after.
  const suggestions = useTickerSuggestions(
    tickersState.status === 'ready' ? tickersState.tickers : [],
    selectedTicker ? '' : search,
  )

  // Debounce: the pagination reset lives HERE rather than in the input's
  // onChange so it is tied to the search actually being applied; React
  // batches both setStates, so the fetch effect below still runs once.
  useEffect(() => {
    const timer = setTimeout(() => {
      setAppliedSearch(search)
      setPage(0)
    }, SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [search])

  // ticker/search/sector/zone/direction are ALL applied server-side and
  // combine (see api/routers/news.py's own get_news docstring) -- this
  // list genuinely paginates (hundreds of news across many pages), so a
  // client-side filter would only narrow whatever page happens to be
  // loaded. `search` is omitted entirely once a suggestion has been picked
  // (selectedTicker set) -- the input then just DISPLAYS "TICKER - Nom",
  // it is no longer a free-text query to also send.
  useEffect(() => {
    const filters = selectedTicker
      ? { ticker: selectedTicker, sector, zone, direction }
      : { search: appliedSearch, sector, zone, direction }
    loadNews(filters, page, setNewsState)
  }, [selectedTicker, appliedSearch, sector, zone, direction, page])

  function pickSuggestion(entry: TickerListEntry) {
    setSearch(`${entry.ticker} - ${entry.nom_affiche}`)
    setSelectedTicker(entry.ticker)
    setShowSuggestions(false)
    setActiveIndex(-1)
    setPage(0)
  }

  function handleSearchChange(next: string) {
    setSearch(next)
    // Any manual edit -- even editing text that still contains the
    // previously-picked ticker -- exits "exact ticker" mode and returns to
    // free-text search, per the task's own step 3.
    setSelectedTicker(null)
    setShowSuggestions(true)
    setActiveIndex(-1)
  }

  function handleSearchKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (!showSuggestions || suggestions.length === 0) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActiveIndex((i) => Math.min(i + 1, suggestions.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActiveIndex((i) => Math.max(i - 1, 0))
    } else if (e.key === 'Enter') {
      if (activeIndex >= 0) {
        e.preventDefault()
        pickSuggestion(suggestions[activeIndex])
      }
    } else if (e.key === 'Escape') {
      setShowSuggestions(false)
      setActiveIndex(-1)
    }
  }

  function handleSectorChange(next: string) {
    setSector(next)
    setPage(0)
  }

  function handleZoneChange(next: string) {
    setZone(next)
    setPage(0)
  }

  function handleDirectionChange(next: DirectionFilterValue) {
    setDirection(next)
    setPage(0)
  }

  return (
    <div>
      <h1 className="jarvis-title text-4xl font-bold">News &amp; Analyse IA</h1>

      <div className="mt-4">
        <label htmlFor="news-search" className="mb-1 block text-sm font-medium text-muted">
          Rechercher
        </label>
        {/* Free text across title/entreprise/secteur/zone/ticker (see
            reasoning/analyze_news.py's filter_news_by_search), WITH a
            ticker/entreprise suggestions dropdown layered on top (see
            useTickerSuggestions) -- picking a suggestion narrows to that
            exact ticker; typing past it (or ignoring the dropdown
            entirely) keeps using the multi-field free-text search. */}
        <div className="relative w-full max-w-md">
          <input
            id="news-search"
            type="text"
            value={search}
            onChange={(e) => handleSearchChange(e.target.value)}
            onFocus={() => setShowSuggestions(true)}
            onBlur={() => {
              // Delay so a click on a suggestion registers before the list unmounts.
              setTimeout(() => setShowSuggestions(false), 150)
            }}
            onKeyDown={handleSearchKeyDown}
            placeholder="Titre, entreprise, secteur, zone, ticker (ex: Apple, Energie, Asie...)"
            className="w-full rounded-full border border-cyan-400/25 bg-navy-800/50 px-4 py-2 pr-20 text-sm text-ink placeholder:text-faint backdrop-blur-md transition-all focus:border-cyan-300/70 focus:outline-none focus:ring-1 focus:ring-cyan-300/50"
          />
          {search && (
            <button
              type="button"
              onClick={() => handleSearchChange('')}
              className="absolute right-2 top-1/2 -translate-y-1/2 rounded-full px-2 py-0.5 text-xs font-medium text-faint hover:bg-white/10 hover:text-ink"
            >
              Effacer
            </button>
          )}
          {showSuggestions && !selectedTicker && suggestions.length > 0 && (
            <TickerSuggestionsList suggestions={suggestions} activeIndex={activeIndex} onPick={pickSuggestion} />
          )}
        </div>
      </div>

      {facetsState.status === 'ready' && facetsState.data.sectors.length > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <label htmlFor="news-sector" className="text-sm font-medium text-muted">
            Secteur
          </label>
          {/* Native <select> (not buttons): 450+ distinct real sector
              strings at last count -- see load_news_facets' own docstring
              -- far too many for a button row, and a native select gets
              free browser typeahead for that many options. */}
          <select
            id="news-sector"
            value={sector}
            onChange={(e) => handleSectorChange(e.target.value)}
            className="rounded-full border border-cyan-400/25 bg-navy-800/50 px-3 py-1.5 text-sm text-ink backdrop-blur-md focus:border-cyan-300/70 focus:outline-none focus:ring-1 focus:ring-cyan-300/50"
          >
            <option value="">Tous les secteurs</option>
            {facetsState.data.sectors.map((s) => (
              <option key={s.value} value={s.value}>
                {s.value} ({s.count})
              </option>
            ))}
          </select>
        </div>
      )}

      {facetsState.status === 'ready' && facetsState.data.zones.length > 0 && (
        <div className="mt-3">
          <span className="mb-1 block text-sm font-medium text-muted">Zone geographique</span>
          {/* Button row (not a dropdown): only a handful of distinct real
              zone values at last count -- see load_news_facets' own
              docstring -- small enough that buttons stay readable, same
              pill style as DirectionFilter. */}
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => handleZoneChange('')}
              className={`jarvis-pill ${zone === '' ? 'jarvis-pill-active' : ''}`}
            >
              Toutes les zones
            </button>
            {facetsState.data.zones.map((z) => (
              <button
                key={z.value}
                type="button"
                onClick={() => handleZoneChange(z.value)}
                className={`jarvis-pill ${zone === z.value ? 'jarvis-pill-active' : ''}`}
              >
                {z.value} ({z.count})
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="mt-3">
        <DirectionFilter value={direction} onChange={handleDirectionChange} />
      </div>

      {newsState.status === 'loading' && (
        <div className="mt-8 flex items-center gap-3 text-faint">
          <span className="jarvis-spinner h-5 w-5 animate-spin" aria-hidden="true" />
          Chargement des news...
        </div>
      )}

      {newsState.status === 'error' && (
        <div className="jarvis-banner-error mt-8">
          <p className="font-medium">Impossible de charger les news.</p>
          <p className="mt-1 text-sm">{newsState.message}</p>
          <button
            type="button"
            onClick={() =>
              loadNews(
                selectedTicker
                  ? { ticker: selectedTicker, sector, zone, direction }
                  : { search: appliedSearch, sector, zone, direction },
                page,
                setNewsState,
              )
            }
            className="jarvis-pill-danger mt-3"
          >
            Reessayer
          </button>
        </div>
      )}

      {newsState.status === 'ready' && (
        <>
          <p className="mt-4 text-sm text-faint">
            {newsState.data.n_total} news analysee(s)
            {newsState.data.ticker ? ` pour ${newsState.data.ticker}` : ''}
            {newsState.data.search ? ` pour « ${newsState.data.search} »` : ''}
            {newsState.data.sector ? ` · secteur ${newsState.data.sector}` : ''}
            {newsState.data.zone ? ` · zone ${newsState.data.zone}` : ''}, triees par date
            decroissante.
          </p>

          {newsState.data.news.length === 0 ? (
            <div className="jarvis-empty mt-8">
              {newsState.data.ticker || newsState.data.search || newsState.data.sector || newsState.data.zone
                ? 'Aucune news ne correspond a ces filtres.'
                : "Aucune news analysee pour l'instant. Lance ingestion/fetch_news.py puis reasoning/analyze_news.py."}
            </div>
          ) : (
            <div className="mt-4 flex flex-col gap-4">
              {newsState.data.news.map((item) => (
                <NewsCard key={item.news_id} item={item} />
              ))}
            </div>
          )}

          {newsState.data.n_total > 0 && (
            <div className="mt-4 flex items-center justify-between text-sm text-faint">
              <button
                type="button"
                onClick={() => setPage((p) => Math.max(0, p - 1))}
                disabled={page === 0}
                className="jarvis-pill"
              >
                Precedent
              </button>
              <span>
                Page {page + 1} sur {Math.max(1, Math.ceil(newsState.data.n_total / PAGE_SIZE))}
              </span>
              <button
                type="button"
                onClick={() => setPage((p) => p + 1)}
                disabled={(page + 1) * PAGE_SIZE >= newsState.data.n_total}
                className="jarvis-pill"
              >
                Suivant
              </button>
            </div>
          )}
        </>
      )}
    </div>
  )
}

// Compact "[Secteur] Zone" badge next to tonalite/importance -- omitted
// entirely (not "non renseigne" text) when both are empty, e.g. every
// news_analysis row analysed before zone_geographique existed (see
// reasoning/analyze_news.py's _ensure_zone_geographique_column: those
// existing rows are never backfilled, just left NULL). Showing nothing is
// less noisy than a "non renseigne" badge repeated on hundreds of older
// cards, while new analyses do get it.
function sectorZoneLabel(item: NewsItem): string | null {
  const parts = [item.sector, item.zone_geographique].filter(Boolean)
  return parts.length > 0 ? parts.join(' · ') : null
}

function NewsCard({ item }: { item: NewsItem }) {
  const meta = [item.company, item.horizon, item.source ? `source: ${item.source}` : null]
    .filter(Boolean)
    .join(' · ')
  const sectorZone = sectorZoneLabel(item)
  const [expanded, setExpanded] = useState(false)
  const [stockState, setStockState] = useState<StockDetailState>({ status: 'loading' })
  const [narrative, setNarrative] = useState<NarrativeState>({ status: 'idle' })

  useEffect(() => {
    if (!expanded || !item.ticker) return
    setStockState({ status: 'loading' })
    fetchStockDetail(item.ticker)
      .then((data) => setStockState({ status: 'ready', data }))
      .catch((err) => {
        const message = err instanceof ApiError ? err.message : "Erreur inattendue lors du chargement de l'action."
        setStockState({ status: 'error', message })
      })
  }, [expanded, item.ticker])

  async function handleLoadNarrative() {
    setNarrative({ status: 'loading' })
    try {
      const result = await fetchNewsNarrative(item.news_id)
      setNarrative({
        status: 'done',
        texte: result.texte,
        source: result.source,
        direction: result.direction_probabilities,
      })
    } catch (err) {
      const message = err instanceof ApiError ? err.message : "Erreur inattendue lors de l'appel API."
      setNarrative({ status: 'error', message })
    }
  }

  return (
    <div className="jarvis-card p-5">
      <div className="flex flex-wrap items-center gap-2">
        <span
          className={`rounded-full px-2.5 py-1 text-xs font-medium ${tonaliteStyle(item.tonalite)}`}
        >
          {item.tonalite}
        </span>
        <span className="jarvis-metric text-sm font-semibold text-ink">
          Importance {item.importance ?? '?'}/10
        </span>
        <span className="text-sm text-faint">
          &middot; confiance {item.confidence !== null ? `${item.confidence.toFixed(0)}%` : '?'}
        </span>
        {item.direction_probabilities && (
          <span
            className={`rounded-full px-2.5 py-1 text-xs font-medium ${DIRECTION_BADGE_STYLES[dominantDirection(item.direction_probabilities)]}`}
          >
            {DIRECTION_LABELS[dominantDirection(item.direction_probabilities)]}
          </span>
        )}
        {sectorZone && (
          <span className="rounded-full bg-cyan-400/15 px-2.5 py-1 text-xs font-medium text-cyan-200">
            {sectorZone}
          </span>
        )}
        <button
          type="button"
          onClick={() => setExpanded(true)}
          className="jarvis-pill ml-auto !py-1 text-xs"
        >
          Agrandir
        </button>
        <span className="text-xs text-slate-500">
          {(item.published_at || '').slice(0, 10)}
        </span>
      </div>

      <h3 className="mt-2 text-base font-medium text-ink">
        {item.url ? (
          <a
            href={item.url}
            target="_blank"
            rel="noreferrer"
            className="text-cyan-300 hover:underline"
          >
            {item.title}
          </a>
        ) : (
          item.title
        )}
      </h3>
      {meta && <p className="mt-1 text-xs text-faint">{meta}</p>}

      {item.ticker && (
        <div className="mt-2">
          <CompanyDescription ticker={item.ticker} />
        </div>
      )}

      <p className="mt-3 text-sm text-ink/85">{item.summary_paragraph}</p>

      <p className="jarvis-metric mt-3 text-xs text-faint">
        Prix avant/apres cette news : {formatPriceContext(item.price_context)}
      </p>

      <ExpandModal isOpen={expanded} onClose={() => setExpanded(false)} title={item.title}>
        <div className="flex flex-col gap-5">
          {meta && <p className="text-xs text-faint">{meta}</p>}

          {item.ticker && <CompanyDescription ticker={item.ticker} />}

          <p className="text-base text-ink/85">{item.summary_paragraph}</p>

          <div>
            <h3 className="jarvis-heading text-sm font-bold">Impact prix autour de la news</h3>
            <p className="jarvis-metric mt-1 text-sm text-ink/70">{formatPriceContext(item.price_context)}</p>
          </div>

          {item.ticker && (
            <div>
              <h3 className="jarvis-heading text-sm font-bold">
                Contexte actuel de {item.ticker}
              </h3>
              {stockState.status === 'loading' && (
                <p className="mt-1 text-sm text-faint">Chargement...</p>
              )}
              {stockState.status === 'error' && (
                <p className="mt-1 text-sm text-red-400">{stockState.message}</p>
              )}
              {stockState.status === 'ready' && (
                <div className="mt-2">
                  <PriceHeadline
                    price={stockState.data.prix_eur !== null ? stockState.data.prix_eur : stockState.data.current_price}
                    currency={stockState.data.prix_eur !== null ? 'EUR' : stockState.data.devise}
                    variationPct={stockState.data.variations ? stockState.data.variations['1j'] : null}
                  />
                  <div className="mt-3">
                    {/* Once the enriched narrative below has been generated,
                        it carries a probability split that also factors in
                        THIS news item's own tonalite/importance (see
                        reasoning/direction_probability.py's news-context
                        parameters) -- showing that one here instead of the
                        ticker's general-only read is what makes this number
                        agree with the text just below it, each clearly
                        scoped by its own horizon label. */}
                    <DirectionProbabilityBar
                      direction={
                        narrative.status === 'done' && narrative.direction
                          ? narrative.direction
                          : stockState.data.direction_probabilities
                      }
                    />
                  </div>
                  <div className="mt-3 flex gap-6 text-xs">
                    <div>
                      <span className="text-faint">Score final</span>
                      <p className="jarvis-metric text-sm font-semibold text-ink">
                        {stockState.data.final_score !== null ? stockState.data.final_score.toFixed(0) : 'n/a'}
                      </p>
                    </div>
                    <div>
                      <span className="text-faint">Confiance</span>
                      <p className="jarvis-metric text-sm font-semibold text-ink">
                        {stockState.data.confidence !== null ? `${stockState.data.confidence.toFixed(0)}%` : 'n/a'}
                      </p>
                    </div>
                  </div>
                </div>
              )}
            </div>
          )}

          <div className="border-t border-cyan-400/15 pt-4">
            <h3 className="jarvis-heading text-sm font-bold">Analyse enrichie (IA)</h3>
            <p className="mt-1 text-xs text-faint">
              Explication redigee (de quoi parle la news, pourquoi elle compte, impact sur
              l'entreprise et ses entreprises liees) -- generee a la demande, mise en cache
              ensuite.
            </p>
            <div className="mt-3">
              {narrative.status === 'idle' && (
                <button
                  type="button"
                  onClick={handleLoadNarrative}
                  className="jarvis-pill-primary"
                >
                  Voir l'analyse enrichie (IA)
                </button>
              )}

              {narrative.status === 'loading' && (
                <div className="flex items-center gap-2 text-sm text-faint">
                  <span className="jarvis-spinner h-4 w-4 animate-spin" aria-hidden="true" />
                  Generation en cours...
                </div>
              )}

              {narrative.status === 'error' && (
                <div className="text-sm text-red-400">
                  {narrative.message}
                  <button
                    type="button"
                    onClick={handleLoadNarrative}
                    className="ml-2 font-medium underline hover:no-underline"
                  >
                    Reessayer
                  </button>
                </div>
              )}

              {narrative.status === 'done' && narrative.texte && (
                <div>
                  <MarkdownText>{narrative.texte}</MarkdownText>
                  <p className="mt-2 text-xs text-slate-500">
                    {narrative.source === 'cache' ? 'Depuis le cache' : "Generee a l'instant"}
                  </p>
                </div>
              )}

              {narrative.status === 'done' && !narrative.texte && (
                <p className="text-sm text-faint">
                  Analyse enrichie indisponible pour l'instant (quota dedie atteint, cle API
                  absente, ou erreur reseau cote serveur).
                </p>
              )}
            </div>
          </div>

          {item.url && (
            <a
              href={item.url}
              target="_blank"
              rel="noreferrer"
              className="text-sm text-cyan-300 hover:underline"
            >
              Lire l'article source
            </a>
          )}
        </div>
      </ExpandModal>
    </div>
  )
}
