import { useEffect, useState } from 'react'
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import {
  ApiError,
  fetchNews,
  fetchStockArguedText,
  fetchStockChart,
  fetchStockDetail,
  fetchTickers,
} from '../api'
import { CompanyDescription } from '../components/CompanyDescription'
import { DirectionProbabilityBar } from '../components/DirectionProbabilityBar'
import { PriceHeadline } from '../components/PriceHeadline'
import { TickerSearch } from '../components/TickerSearch'
import type {
  ArguedTextSource,
  NewsItem,
  StockChartResponse,
  StockDetail,
  TickerListEntry,
} from '../types'

type TickersState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; tickers: TickerListEntry[] }

type DetailState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; data: StockDetail }

type ChartState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; data: StockChartResponse }

type ArguedTextState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'done'; texte: string | null; source: ArguedTextSource }

type NewsSourcesState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; news: NewsItem[] }

// Kept small -- this section only points the reader to the underlying
// articles, it isn't a substitute for the News & Analyse IA page's own
// full, paginated list for this ticker.
const SOURCES_LIMIT = 10

function fmt(value: number | null, decimals = 1): string {
  return value === null ? 'n/a' : value.toFixed(decimals)
}

function fmtVariation(pct: number | null): string {
  if (pct === null) return 'n/a'
  const sign = pct >= 0 ? '+' : ''
  return `${sign}${pct.toFixed(1)}%`
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

function loadDetail(ticker: string, setState: (s: DetailState) => void) {
  setState({ status: 'loading' })
  fetchStockDetail(ticker)
    .then((data) => setState({ status: 'ready', data }))
    .catch((err) => {
      const message =
        err instanceof ApiError ? err.message : `Erreur inattendue lors du chargement de ${ticker}.`
      setState({ status: 'error', message })
    })
}

function loadChart(ticker: string, setState: (s: ChartState) => void) {
  setState({ status: 'loading' })
  fetchStockChart(ticker)
    .then((data) => setState({ status: 'ready', data }))
    .catch((err) => {
      const message =
        err instanceof ApiError ? err.message : 'Erreur inattendue lors du chargement du graphique.'
      setState({ status: 'error', message })
    })
}

// Reuses GET /api/news's own `ticker` filter -- the exact same route and
// data (news_raw JOIN news_analysis) that News & Analyse IA's own "Lire
// l'article source" link is built from -- no dedicated route for this
// section.
function loadNewsSources(ticker: string, setState: (s: NewsSourcesState) => void) {
  setState({ status: 'loading' })
  fetchNews(SOURCES_LIMIT, 0, ticker)
    .then((data) => setState({ status: 'ready', news: data.news }))
    .catch((err) => {
      const message =
        err instanceof ApiError ? err.message : 'Erreur inattendue lors du chargement des sources.'
      setState({ status: 'error', message })
    })
}

export function StockPage() {
  const [tickersState, setTickersState] = useState<TickersState>({ status: 'loading' })
  const [selectedTicker, setSelectedTicker] = useState<string | null>(null)
  const [detailState, setDetailState] = useState<DetailState | null>(null)
  const [chartState, setChartState] = useState<ChartState | null>(null)
  const [arguedText, setArguedText] = useState<ArguedTextState>({ status: 'idle' })
  const [newsSources, setNewsSources] = useState<NewsSourcesState | null>(null)

  useEffect(() => {
    loadTickers(setTickersState)
  }, [])

  function handleSelectTicker(ticker: string) {
    setSelectedTicker(ticker)
    loadDetail(ticker, setDetailState)
    loadChart(ticker, setChartState)
    loadNewsSources(ticker, setNewsSources)
    // A new ticker resets any argued text from the previous selection --
    // never carried over, and never auto-generated for the new ticker
    // either (still requires its own explicit button click, same Groq-
    // quota discipline as "Resume du jour"'s SignalCard).
    setArguedText({ status: 'idle' })
  }

  async function handleGenerateArguedText() {
    if (!selectedTicker) return
    setArguedText({ status: 'loading' })
    try {
      const result = await fetchStockArguedText(selectedTicker)
      setArguedText({ status: 'done', texte: result.texte_argumente, source: result.source })
    } catch (err) {
      const message = err instanceof ApiError ? err.message : "Erreur inattendue lors de l'appel API."
      setArguedText({ status: 'error', message })
    }
  }

  return (
    <div>
      <h1 className="text-2xl font-bold text-gray-900">Analyse d'une action</h1>

      <div className="mt-4">
        {tickersState.status === 'loading' && (
          <p className="text-sm text-gray-500">Chargement de l'univers des tickers...</p>
        )}
        {tickersState.status === 'error' && (
          <div className="rounded-md border border-red-200 bg-red-50 p-4 text-red-700">
            <p className="font-medium">Impossible de charger la liste des tickers.</p>
            <p className="mt-1 text-sm">{tickersState.message}</p>
            <button
              type="button"
              onClick={() => loadTickers(setTickersState)}
              className="mt-3 rounded-md bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-700"
            >
              Reessayer
            </button>
          </div>
        )}
        {tickersState.status === 'ready' && (
          <TickerSearch tickers={tickersState.tickers} onSelect={handleSelectTicker} />
        )}
      </div>

      {!selectedTicker && (
        <div className="mt-8 rounded-md border border-gray-200 bg-gray-50 p-4 text-gray-600">
          Recherchez un ticker ou une entreprise ci-dessus pour afficher son analyse.
        </div>
      )}

      {selectedTicker && detailState?.status === 'loading' && (
        <div className="mt-8 flex items-center gap-3 text-gray-600">
          <span
            className="h-5 w-5 animate-spin rounded-full border-2 border-gray-300 border-t-indigo-600"
            aria-hidden="true"
          />
          Chargement de {selectedTicker}...
        </div>
      )}

      {selectedTicker && detailState?.status === 'error' && (
        <div className="mt-8 rounded-md border border-red-200 bg-red-50 p-4 text-red-700">
          <p className="font-medium">Impossible de charger {selectedTicker}.</p>
          <p className="mt-1 text-sm">{detailState.message}</p>
          <button
            type="button"
            onClick={() => loadDetail(selectedTicker, setDetailState)}
            className="mt-3 rounded-md bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-700"
          >
            Reessayer
          </button>
        </div>
      )}

      {selectedTicker && detailState?.status === 'ready' && (
        <StockDetailView
          detail={detailState.data}
          chartState={chartState}
          arguedText={arguedText}
          onGenerateArguedText={handleGenerateArguedText}
          newsSources={newsSources}
          onRetryNewsSources={() => loadNewsSources(detailState.data.ticker, setNewsSources)}
        />
      )}
    </div>
  )
}

function StockDetailView({
  detail,
  chartState,
  arguedText,
  onGenerateArguedText,
  newsSources,
  onRetryNewsSources,
}: {
  detail: StockDetail
  chartState: ChartState | null
  arguedText: ArguedTextState
  onGenerateArguedText: () => void
  newsSources: NewsSourcesState | null
  onRetryNewsSources: () => void
}) {
  return (
    <div className="mt-6 flex flex-col gap-6">
      <div className="rounded-lg border border-gray-200 bg-white p-5 shadow-sm">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="text-lg font-semibold text-gray-900">
            {detail.ticker} <span className="font-normal text-gray-500">-- {detail.nom_affiche}</span>
          </h2>
          <span className="rounded-full bg-gray-100 px-3 py-1 text-sm font-medium text-gray-700">
            Priorite : {detail.priorite}
          </span>
        </div>

        {(detail.sector || detail.industry) && (
          <p className="mt-1 text-sm text-gray-500">
            {[detail.sector, detail.industry].filter(Boolean).join(' - ')}
          </p>
        )}

        <div className="mt-2">
          <CompanyDescription ticker={detail.ticker} />
        </div>

        {/* Price first, big and coloured -- the single most immediately
            useful number on this page, ahead of every other score. */}
        <div className="mt-4">
          <PriceHeadline
            price={detail.prix_eur !== null ? detail.prix_eur : detail.current_price}
            currency={detail.prix_eur !== null ? 'EUR' : detail.devise}
            variationPct={detail.variations ? detail.variations['1j'] : null}
          />
          {detail.variations && (
            <p className="mt-1 text-xs text-gray-500">
              7j: {fmtVariation(detail.variations['7j'])} - 30j:{' '}
              {fmtVariation(detail.variations['30j'])}
            </p>
          )}
        </div>

        {/* Direction probabilities -- THE main forward-looking number now,
            ahead of the older secondary scores below. */}
        <div className="mt-4 border-t border-gray-100 pt-4">
          <DirectionProbabilityBar direction={detail.direction_probabilities} />
        </div>

        {/* Secondary scores -- smaller, below the price and the direction
            probabilities, kept for anyone who wants the structured detail. */}
        <div className="mt-4 grid grid-cols-2 gap-3 border-t border-gray-100 pt-4 text-xs sm:grid-cols-4">
          <ScoreStat label="Confiance" value={detail.confidence} />
          <ScoreStat label="RSI" value={detail.rsi} suffix={detail.rsi !== null && !detail.rsi_is_real ? ' (estime)' : ''} />
          <ScoreStat label="Prix / Valorisation" value={detail.price_valuation_score} />
          <ScoreStat label="Technique" value={detail.technical_score} />
          <ScoreStat label="Fondamental reel" value={detail.score_fondamental_reel} />
          <ScoreStat label="Score global (legacy)" value={detail.final_score} />
          <ScoreStat label="Volatilite" value={detail.volatility_score} />
          <ScoreStat label="Volume" value={detail.volume_score} />
          <ScoreStat label="MA 50" value={detail.ma_50} decimals={2} />
          <ScoreStat label="MA 200" value={detail.ma_200} decimals={2} />
        </div>
      </div>

      <div className="rounded-lg border border-gray-200 bg-white p-5 shadow-sm">
        <h3 className="text-base font-semibold text-gray-900">Prix &amp; moyennes mobiles</h3>
        <StockChartView chartState={chartState} />
      </div>

      <div className="rounded-lg border border-gray-200 bg-white p-5 shadow-sm">
        <h3 className="text-base font-semibold text-gray-900">Analyse argumentee (IA)</h3>
        <div className="mt-3">
          {arguedText.status === 'idle' && (
            <button
              type="button"
              onClick={onGenerateArguedText}
              className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700"
            >
              Generer l'analyse
            </button>
          )}

          {arguedText.status === 'loading' && (
            <div className="flex items-center gap-2 text-sm text-gray-500">
              <span
                className="h-4 w-4 animate-spin rounded-full border-2 border-gray-300 border-t-indigo-600"
                aria-hidden="true"
              />
              Generation en cours...
            </div>
          )}

          {arguedText.status === 'error' && (
            <div className="text-sm text-red-600">
              {arguedText.message}
              <button
                type="button"
                onClick={onGenerateArguedText}
                className="ml-2 font-medium underline hover:no-underline"
              >
                Reessayer
              </button>
            </div>
          )}

          {arguedText.status === 'done' && arguedText.texte && (
            <div>
              <p className="whitespace-pre-line text-sm text-gray-800">{arguedText.texte}</p>
              <p className="mt-2 text-xs text-gray-400">
                {arguedText.source === 'cache' ? 'Depuis le cache du jour' : "Generee a l'instant"}
              </p>
            </div>
          )}

          {arguedText.status === 'done' && !arguedText.texte && (
            <p className="text-sm text-gray-500">
              Analyse indisponible pour l'instant (quota Groq du jour atteint, cle API absente, ou
              erreur reseau cote serveur, ou {detail.ticker} n'a pas de donnee d'opportunite).
            </p>
          )}
        </div>
      </div>

      <div className="rounded-lg border border-gray-200 bg-white p-5 shadow-sm">
        <h3 className="text-base font-semibold text-gray-900">Sources</h3>
        <NewsSourcesSection
          ticker={detail.ticker}
          newsSources={newsSources}
          onRetry={onRetryNewsSources}
        />
      </div>
    </div>
  )
}

function NewsSourcesSection({
  ticker,
  newsSources,
  onRetry,
}: {
  ticker: string
  newsSources: NewsSourcesState | null
  onRetry: () => void
}) {
  if (!newsSources || newsSources.status === 'loading') {
    return (
      <div className="mt-3 flex items-center gap-2 text-sm text-gray-500">
        <span
          className="h-4 w-4 animate-spin rounded-full border-2 border-gray-300 border-t-indigo-600"
          aria-hidden="true"
        />
        Chargement des sources...
      </div>
    )
  }

  if (newsSources.status === 'error') {
    return (
      <div className="mt-3 text-sm text-red-600">
        {newsSources.message}
        <button
          type="button"
          onClick={onRetry}
          className="ml-2 font-medium underline hover:no-underline"
        >
          Reessayer
        </button>
      </div>
    )
  }

  if (newsSources.news.length === 0) {
    return (
      <p className="mt-3 text-sm text-gray-500">
        Aucune news analysee pour {ticker} pour l'instant -- pas de source disponible.
      </p>
    )
  }

  return (
    <ul className="mt-3 flex flex-col gap-2">
      {newsSources.news.map((item) => (
        <li key={item.news_id} className="text-sm">
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
            <span className="text-gray-700">{item.title}</span>
          )}
          <span className="ml-2 text-xs text-gray-400">
            {(item.published_at || '').slice(0, 10)}
            {item.source ? ` -- ${item.source}` : ''}
          </span>
        </li>
      ))}
    </ul>
  )
}

function ScoreStat({
  label,
  value,
  decimals = 1,
  suffix = '',
}: {
  label: string
  value: number | null
  decimals?: number
  suffix?: string
}) {
  return (
    <div>
      <span className="text-gray-500">{label}</span>
      <p className="text-sm font-semibold text-gray-900">
        {fmt(value, decimals)}
        {suffix}
      </p>
    </div>
  )
}

function StockChartView({ chartState }: { chartState: ChartState | null }) {
  if (!chartState || chartState.status === 'loading') {
    return (
      <div className="mt-4 flex items-center gap-2 text-sm text-gray-500">
        <span
          className="h-4 w-4 animate-spin rounded-full border-2 border-gray-300 border-t-indigo-600"
          aria-hidden="true"
        />
        Chargement du graphique...
      </div>
    )
  }

  if (chartState.status === 'error') {
    return <p className="mt-4 text-sm text-red-600">{chartState.message}</p>
  }

  const { points, devise_affichee } = chartState.data
  if (points.length === 0) {
    return (
      <p className="mt-4 text-sm text-gray-500">
        Aucun historique de prix pour ce ticker pour l'instant.
      </p>
    )
  }

  return (
    <div className="mt-4 h-80 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={points} margin={{ top: 5, right: 20, left: 0, bottom: 5 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
          <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={40} />
          <YAxis
            tick={{ fontSize: 11 }}
            width={60}
            label={{ value: devise_affichee, angle: -90, position: 'insideLeft', fontSize: 11 }}
          />
          <Tooltip />
          <Legend />
          <Line
            type="monotone"
            dataKey="close"
            name="Prix"
            stroke="#2b6cb0"
            dot={false}
            strokeWidth={1.5}
            connectNulls
          />
          <Line
            type="monotone"
            dataKey="ma_50"
            name="MA 50"
            stroke="#f59e0b"
            dot={false}
            strokeWidth={1.8}
            connectNulls
          />
          <Line
            type="monotone"
            dataKey="ma_200"
            name="MA 200"
            stroke="#1f4e79"
            dot={false}
            strokeWidth={1.8}
            connectNulls
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}
