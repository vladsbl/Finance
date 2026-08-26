import { useEffect, useState } from 'react'
import { ApiError, fetchNews, fetchNewsNarrative, fetchStockDetail, fetchTickers } from '../api'
import { CompanyDescription } from '../components/CompanyDescription'
import { DirectionProbabilityBar } from '../components/DirectionProbabilityBar'
import { ExpandModal } from '../components/ExpandModal'
import { MarkdownText } from '../components/MarkdownText'
import { PriceHeadline } from '../components/PriceHeadline'
import { TickerSearch } from '../components/TickerSearch'
import type {
  DirectionProbabilities,
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

type TickersState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; tickers: TickerListEntry[] }

// Matches the backend's own default (api/routers/news.py's DEFAULT_LIMIT).
const PAGE_SIZE = 50

const TONALITE_STYLES: Record<string, string> = {
  positive: 'bg-emerald-100 text-emerald-800',
  negative: 'bg-red-100 text-red-800',
  neutre: 'bg-gray-100 text-gray-700',
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

function loadNews(
  ticker: string | null,
  page: number,
  setState: (s: NewsState) => void,
) {
  setState({ status: 'loading' })
  fetchNews(PAGE_SIZE, page * PAGE_SIZE, ticker ?? undefined)
    .then((data) => setState({ status: 'ready', data }))
    .catch((err) => {
      const message =
        err instanceof ApiError ? err.message : 'Erreur inattendue lors du chargement des news.'
      setState({ status: 'error', message })
    })
}

function loadTickers(setState: (s: TickersState) => void) {
  setState({ status: 'loading' })
  fetchTickers()
    .then((data) => setState({ status: 'ready', tickers: data.tickers }))
    .catch((err) => {
      const message =
        err instanceof ApiError ? err.message : "Erreur inattendue lors du chargement de l'univers."
      setState({ status: 'error', message })
    })
}

export function NewsPage() {
  const [tickersState, setTickersState] = useState<TickersState>({ status: 'loading' })
  const [selectedTicker, setSelectedTicker] = useState<string | null>(null)
  const [page, setPage] = useState(0)
  const [newsState, setNewsState] = useState<NewsState>({ status: 'loading' })

  useEffect(() => {
    loadTickers(setTickersState)
  }, [])

  useEffect(() => {
    loadNews(selectedTicker, page, setNewsState)
  }, [selectedTicker, page])

  function handleSelectTicker(ticker: string) {
    setSelectedTicker(ticker)
    setPage(0)
  }

  function handleClearTicker() {
    setSelectedTicker(null)
    setPage(0)
  }

  return (
    <div>
      <h1 className="text-2xl font-bold text-gray-900">News &amp; Analyse IA</h1>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        {tickersState.status === 'ready' && (
          <TickerSearch
            tickers={tickersState.tickers}
            onSelect={handleSelectTicker}
            placeholder="Filtrer par ticker (ex: AAPL)..."
          />
        )}
        {selectedTicker && (
          <button
            type="button"
            onClick={handleClearTicker}
            className="rounded-md border border-gray-300 px-3 py-1.5 text-sm font-medium text-gray-600 hover:bg-gray-50"
          >
            Toutes les news ({selectedTicker} -&gt; tout)
          </button>
        )}
      </div>

      {newsState.status === 'loading' && (
        <div className="mt-8 flex items-center gap-3 text-gray-600">
          <span
            className="h-5 w-5 animate-spin rounded-full border-2 border-gray-300 border-t-indigo-600"
            aria-hidden="true"
          />
          Chargement des news...
        </div>
      )}

      {newsState.status === 'error' && (
        <div className="mt-8 rounded-md border border-red-200 bg-red-50 p-4 text-red-700">
          <p className="font-medium">Impossible de charger les news.</p>
          <p className="mt-1 text-sm">{newsState.message}</p>
          <button
            type="button"
            onClick={() => loadNews(selectedTicker, page, setNewsState)}
            className="mt-3 rounded-md bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-700"
          >
            Reessayer
          </button>
        </div>
      )}

      {newsState.status === 'ready' && (
        <>
          <p className="mt-4 text-sm text-gray-500">
            {newsState.data.n_total} news analysee(s)
            {newsState.data.ticker ? ` pour ${newsState.data.ticker}` : ''}, triees par date
            decroissante.
          </p>

          {newsState.data.news.length === 0 ? (
            <div className="mt-8 rounded-md border border-gray-200 bg-gray-50 p-4 text-gray-600">
              {newsState.data.ticker
                ? `Aucune news analysee pour ${newsState.data.ticker}.`
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
            <div className="mt-4 flex items-center justify-between text-sm text-gray-600">
              <button
                type="button"
                onClick={() => setPage((p) => Math.max(0, p - 1))}
                disabled={page === 0}
                className="rounded-md border border-gray-300 px-3 py-1.5 font-medium disabled:cursor-not-allowed disabled:opacity-40 enabled:hover:bg-gray-50"
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
                className="rounded-md border border-gray-300 px-3 py-1.5 font-medium disabled:cursor-not-allowed disabled:opacity-40 enabled:hover:bg-gray-50"
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

function NewsCard({ item }: { item: NewsItem }) {
  const meta = [item.company, item.sector, item.horizon, item.source ? `source: ${item.source}` : null]
    .filter(Boolean)
    .join(' · ')
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
    <div className="rounded-lg border border-gray-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span
          className={`rounded-full px-2.5 py-1 text-xs font-medium ${tonaliteStyle(item.tonalite)}`}
        >
          {item.tonalite}
        </span>
        <span className="text-sm font-semibold text-gray-900">
          Importance {item.importance ?? '?'}/10
        </span>
        <span className="text-sm text-gray-500">
          &middot; confiance {item.confidence !== null ? `${item.confidence.toFixed(0)}%` : '?'}
        </span>
        <button
          type="button"
          onClick={() => setExpanded(true)}
          className="ml-auto rounded-md border border-gray-300 px-3 py-1 text-xs font-medium text-gray-600 hover:bg-gray-50"
        >
          Agrandir
        </button>
        <span className="text-xs text-gray-400">
          {(item.published_at || '').slice(0, 10)}
        </span>
      </div>

      <h3 className="mt-2 text-base font-medium text-gray-900">
        {item.url ? (
          <a
            href={item.url}
            target="_blank"
            rel="noreferrer"
            className="text-indigo-700 hover:underline"
          >
            {item.title}
          </a>
        ) : (
          item.title
        )}
      </h3>
      {meta && <p className="mt-1 text-xs text-gray-500">{meta}</p>}

      {item.ticker && (
        <div className="mt-2">
          <CompanyDescription ticker={item.ticker} />
        </div>
      )}

      <p className="mt-3 text-sm text-gray-800">{item.summary_paragraph}</p>

      <p className="mt-3 text-xs text-gray-500">
        Prix avant/apres cette news : {formatPriceContext(item.price_context)}
      </p>

      <ExpandModal isOpen={expanded} onClose={() => setExpanded(false)} title={item.title}>
        <div className="flex flex-col gap-5">
          {meta && <p className="text-xs text-gray-500">{meta}</p>}

          {item.ticker && <CompanyDescription ticker={item.ticker} />}

          <p className="text-base text-gray-800">{item.summary_paragraph}</p>

          <div>
            <h3 className="text-sm font-semibold text-gray-900">Impact prix autour de la news</h3>
            <p className="mt-1 text-sm text-gray-700">{formatPriceContext(item.price_context)}</p>
          </div>

          {item.ticker && (
            <div>
              <h3 className="text-sm font-semibold text-gray-900">
                Contexte actuel de {item.ticker}
              </h3>
              {stockState.status === 'loading' && (
                <p className="mt-1 text-sm text-gray-500">Chargement...</p>
              )}
              {stockState.status === 'error' && (
                <p className="mt-1 text-sm text-red-600">{stockState.message}</p>
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
                      <span className="text-gray-500">Score final</span>
                      <p className="text-sm font-semibold text-gray-900">
                        {stockState.data.final_score !== null ? stockState.data.final_score.toFixed(0) : 'n/a'}
                      </p>
                    </div>
                    <div>
                      <span className="text-gray-500">Confiance</span>
                      <p className="text-sm font-semibold text-gray-900">
                        {stockState.data.confidence !== null ? `${stockState.data.confidence.toFixed(0)}%` : 'n/a'}
                      </p>
                    </div>
                  </div>
                </div>
              )}
            </div>
          )}

          <div className="border-t border-gray-100 pt-4">
            <h3 className="text-sm font-semibold text-gray-900">Analyse enrichie (IA)</h3>
            <p className="mt-1 text-xs text-gray-500">
              Explication redigee (de quoi parle la news, pourquoi elle compte, impact sur
              l'entreprise et ses entreprises liees) -- generee a la demande, mise en cache
              ensuite.
            </p>
            <div className="mt-3">
              {narrative.status === 'idle' && (
                <button
                  type="button"
                  onClick={handleLoadNarrative}
                  className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700"
                >
                  Voir l'analyse enrichie (IA)
                </button>
              )}

              {narrative.status === 'loading' && (
                <div className="flex items-center gap-2 text-sm text-gray-500">
                  <span
                    className="h-4 w-4 animate-spin rounded-full border-2 border-gray-300 border-t-indigo-600"
                    aria-hidden="true"
                  />
                  Generation en cours...
                </div>
              )}

              {narrative.status === 'error' && (
                <div className="text-sm text-red-600">
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
                  <p className="mt-2 text-xs text-gray-400">
                    {narrative.source === 'cache' ? 'Depuis le cache' : "Generee a l'instant"}
                  </p>
                </div>
              )}

              {narrative.status === 'done' && !narrative.texte && (
                <p className="text-sm text-gray-500">
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
              className="text-sm text-indigo-700 hover:underline"
            >
              Lire l'article source
            </a>
          )}
        </div>
      </ExpandModal>
    </div>
  )
}
