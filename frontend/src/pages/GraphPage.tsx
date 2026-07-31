import { useEffect, useState } from 'react'
import {
  addManualRelation,
  ApiError,
  deleteManualRelation,
  fetchGraph,
  fetchManualRelations,
  fetchTickers,
} from '../api'
import { GraphView } from '../components/GraphView'
import { TickerSearch } from '../components/TickerSearch'
import type { GraphResponse, ManualRelation, TickerListEntry } from '../types'

type GraphState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; data: GraphResponse }

type TickersState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; tickers: TickerListEntry[] }

type ManualState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; relations: ManualRelation[]; relationTypes: string[] }

type AddState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'done'; message: string }

function loadGraph(mode: 'default' | string, setState: (s: GraphState) => void) {
  setState({ status: 'loading' })
  fetchGraph(mode === 'default' ? undefined : mode)
    .then((data) => setState({ status: 'ready', data }))
    .catch((err) => {
      const message = err instanceof ApiError ? err.message : 'Erreur inattendue lors du chargement du graphe.'
      setState({ status: 'error', message })
    })
}

function loadTickers(setState: (s: TickersState) => void) {
  setState({ status: 'loading' })
  fetchTickers()
    .then((data) => setState({ status: 'ready', tickers: data.tickers }))
    .catch((err) => {
      const message = err instanceof ApiError ? err.message : "Erreur inattendue lors du chargement de l'univers."
      setState({ status: 'error', message })
    })
}

function loadManual(setState: (s: ManualState) => void) {
  setState({ status: 'loading' })
  fetchManualRelations()
    .then((data) => setState({ status: 'ready', relations: data.relations, relationTypes: data.relation_types }))
    .catch((err) => {
      const message = err instanceof ApiError ? err.message : 'Erreur inattendue lors du chargement des relations manuelles.'
      setState({ status: 'error', message })
    })
}

export function GraphPage() {
  const [mode, setMode] = useState<'default' | string>('default')
  const [graphState, setGraphState] = useState<GraphState>({ status: 'loading' })
  const [tickersState, setTickersState] = useState<TickersState>({ status: 'loading' })
  const [manualState, setManualState] = useState<ManualState>({ status: 'loading' })

  useEffect(() => {
    loadGraph('default', setGraphState)
    loadTickers(setTickersState)
    loadManual(setManualState)
  }, [])

  function refreshAfterChange() {
    loadGraph(mode, setGraphState)
    loadManual(setManualState)
  }

  function handleCenterOnTicker(ticker: string) {
    setMode(ticker)
    loadGraph(ticker, setGraphState)
  }

  function handleShowDefault() {
    setMode('default')
    loadGraph('default', setGraphState)
  }

  return (
    <div>
      <h1 className="text-2xl font-bold text-gray-900">Knowledge Graph</h1>

      <div className="mt-4 rounded-lg border border-gray-200 bg-white p-5 shadow-sm">
        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            onClick={handleShowDefault}
            className={`rounded-full px-4 py-1.5 text-sm font-medium ${
              mode === 'default'
                ? 'bg-indigo-600 text-white'
                : 'bg-gray-100 text-gray-700 hover:bg-gray-200'
            }`}
          >
            Top opportunites du jour
          </button>

          {tickersState.status === 'ready' && (
            <TickerSearch
              tickers={tickersState.tickers}
              onSelect={handleCenterOnTicker}
              placeholder="Centrer sur un ticker..."
            />
          )}
        </div>

        {graphState.status === 'ready' && (
          <p className="mt-3 text-sm text-gray-500">
            {graphState.data.nodes.length} noeuds ({graphState.data.n_primary} suivis,{' '}
            {graphState.data.n_external} externes) - {graphState.data.edges.length} relations
            {graphState.data.mode === 'top_opportunities' && graphState.data.top_tickers && (
              <>
                {' '}
                - noeuds mis en avant (top opportunites) :{' '}
                {graphState.data.top_tickers.join(', ')}
              </>
            )}
            {graphState.data.mode === 'ticker' && (
              <> - centre sur {graphState.data.ticker}</>
            )}
          </p>
        )}

        <div className="mt-4">
          {graphState.status === 'loading' && (
            <div className="flex h-[560px] items-center justify-center gap-3 text-gray-600">
              <span
                className="h-5 w-5 animate-spin rounded-full border-2 border-gray-300 border-t-indigo-600"
                aria-hidden="true"
              />
              Chargement du graphe...
            </div>
          )}

          {graphState.status === 'error' && (
            <div className="rounded-md border border-red-200 bg-red-50 p-4 text-red-700">
              <p className="font-medium">Impossible de charger le graphe.</p>
              <p className="mt-1 text-sm">{graphState.message}</p>
              <button
                type="button"
                onClick={() => loadGraph(mode, setGraphState)}
                className="mt-3 rounded-md bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-700"
              >
                Reessayer
              </button>
            </div>
          )}

          {graphState.status === 'ready' && (
            <GraphView
              nodes={graphState.data.nodes}
              edges={graphState.data.edges}
              onNodeClick={handleCenterOnTicker}
            />
          )}
        </div>
      </div>

      <AddRelationForm
        tickersState={tickersState}
        manualState={manualState}
        onChanged={refreshAfterChange}
      />

      <ManualRelationsPanel manualState={manualState} onDeleted={refreshAfterChange} />
    </div>
  )
}

function AddRelationForm({
  tickersState,
  manualState,
  onChanged,
}: {
  tickersState: TickersState
  manualState: ManualState
  onChanged: () => void
}) {
  const [sourceTicker, setSourceTicker] = useState<string | null>(null)
  const [relationType, setRelationType] = useState('')
  const [targetMode, setTargetMode] = useState<'univers' | 'externe'>('univers')
  const [targetTickerUniv, setTargetTickerUniv] = useState<string | null>(null)
  const [targetNameManual, setTargetNameManual] = useState('')
  const [targetTickerManual, setTargetTickerManual] = useState('')
  const [notes, setNotes] = useState('')
  const [resetKey, setResetKey] = useState(0)
  const [addState, setAddState] = useState<AddState>({ status: 'idle' })

  const relationTypes = manualState.status === 'ready' ? manualState.relationTypes : []
  const tickerNames = tickersState.status === 'ready'
    ? Object.fromEntries(tickersState.tickers.map((t) => [t.ticker, t.nom_affiche]))
    : {}

  function resetForm() {
    setSourceTicker(null)
    setRelationType('')
    setTargetTickerUniv(null)
    setTargetNameManual('')
    setTargetTickerManual('')
    setNotes('')
    setResetKey((k) => k + 1)
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!sourceTicker) {
      setAddState({ status: 'error', message: 'Choisissez un ticker source.' })
      return
    }
    if (!relationType) {
      setAddState({ status: 'error', message: 'Choisissez un type de relation.' })
      return
    }

    const targetName = targetMode === 'univers'
      ? (targetTickerUniv ? tickerNames[targetTickerUniv] ?? targetTickerUniv : '')
      : targetNameManual.trim()
    const targetTicker = targetMode === 'univers' ? targetTickerUniv : (targetTickerManual.trim().toUpperCase() || null)

    if (targetMode === 'univers' && !targetTickerUniv) {
      setAddState({ status: 'error', message: 'Choisissez un ticker cible.' })
      return
    }
    if (!targetName) {
      setAddState({ status: 'error', message: "Le nom de l'entreprise cible est obligatoire." })
      return
    }
    if (targetTicker === sourceTicker) {
      setAddState({ status: 'error', message: 'Le ticker cible doit etre different du ticker source.' })
      return
    }

    setAddState({ status: 'loading' })
    try {
      const created = await addManualRelation({
        source_ticker: sourceTicker,
        relation_type: relationType,
        target_name: targetName,
        target_ticker: targetTicker,
        notes: notes.trim() || null,
      })
      setAddState({
        status: 'done',
        message: `Relation ajoutee : ${created.source_ticker} -- ${created.relation_type} --> ${created.target_name}` +
          (created.target_ticker ? ` (${created.target_ticker})` : ''),
      })
      resetForm()
      onChanged()
    } catch (err) {
      const message = err instanceof ApiError ? err.message : "Erreur inattendue lors de l'ajout de la relation."
      setAddState({ status: 'error', message })
    }
  }

  return (
    <div className="mt-6 rounded-lg border border-gray-200 bg-white p-5 shadow-sm">
      <h2 className="text-base font-semibold text-gray-900">Ajouter une relation manuellement</h2>
      <p className="mt-1 text-sm text-gray-500">
        Relation ecrite directement dans le Knowledge Graph actif, sans passer par la generation Groq --
        vous etes ici la validation humaine.
      </p>

      <form onSubmit={handleSubmit} className="mt-4 flex flex-col gap-4">
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div>
            <label className="mb-1 block text-sm font-medium text-gray-700">Ticker source</label>
            {tickersState.status === 'ready' ? (
              <TickerSearch
                key={`source-${resetKey}`}
                tickers={tickersState.tickers}
                onSelect={setSourceTicker}
                placeholder="Rechercher le ticker source..."
              />
            ) : (
              <p className="text-sm text-gray-500">Chargement de l'univers...</p>
            )}
          </div>

          <div>
            <label className="mb-1 block text-sm font-medium text-gray-700">Type de relation</label>
            <select
              value={relationType}
              onChange={(e) => setRelationType(e.target.value)}
              className="w-full max-w-md rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
            >
              <option value="">-- Choisir --</option>
              {relationTypes.map((rt) => (
                <option key={rt} value={rt}>{rt}</option>
              ))}
            </select>
          </div>
        </div>

        <div>
          <span className="mb-1 block text-sm font-medium text-gray-700">Cible</span>
          <div className="flex gap-4 text-sm text-gray-700">
            <label className="flex items-center gap-1.5">
              <input
                type="radio"
                checked={targetMode === 'univers'}
                onChange={() => setTargetMode('univers')}
              />
              Dans l'univers (recherche)
            </label>
            <label className="flex items-center gap-1.5">
              <input
                type="radio"
                checked={targetMode === 'externe'}
                onChange={() => setTargetMode('externe')}
              />
              Externe (saisie manuelle)
            </label>
          </div>
        </div>

        {targetMode === 'univers' ? (
          tickersState.status === 'ready' ? (
            <TickerSearch
              key={`target-${resetKey}`}
              tickers={tickersState.tickers}
              onSelect={setTargetTickerUniv}
              placeholder="Rechercher le ticker cible..."
            />
          ) : (
            <p className="text-sm text-gray-500">Chargement de l'univers...</p>
          )
        ) : (
          <div className="flex flex-col gap-3">
            <p className="text-xs text-amber-700">
              Cible hors univers : jamais verifiee empiriquement (pas de confirmation qu'elle est
              reellement cotee), contrairement aux ajouts automatiques qui passent par une
              verification yfinance avant d'entrer dans l'univers.
            </p>
            <input
              type="text"
              value={targetNameManual}
              onChange={(e) => setTargetNameManual(e.target.value)}
              placeholder="Nom de l'entreprise cible"
              className="w-full max-w-md rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
            />
            <input
              type="text"
              value={targetTickerManual}
              onChange={(e) => setTargetTickerManual(e.target.value)}
              placeholder="Ticker cible (optionnel)"
              className="w-full max-w-md rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
            />
          </div>
        )}

        <div>
          <label className="mb-1 block text-sm font-medium text-gray-700">
            Justification / note (optionnel)
          </label>
          <textarea
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            rows={2}
            className="w-full max-w-md rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
          />
        </div>

        <div>
          <button
            type="submit"
            disabled={addState.status === 'loading'}
            className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
          >
            {addState.status === 'loading' ? 'Ajout en cours...' : 'Ajouter'}
          </button>
        </div>

        {addState.status === 'error' && (
          <p className="text-sm text-red-600">{addState.message}</p>
        )}
        {addState.status === 'done' && (
          <p className="text-sm text-emerald-700">{addState.message}</p>
        )}
      </form>
    </div>
  )
}

function ManualRelationsPanel({
  manualState,
  onDeleted,
}: {
  manualState: ManualState
  onDeleted: () => void
}) {
  const [deletingIds, setDeletingIds] = useState<Set<number>>(new Set())
  const [deleteError, setDeleteError] = useState<string | null>(null)

  async function handleDelete(id: number) {
    setDeleteError(null)
    setDeletingIds((s) => new Set(s).add(id))
    try {
      await deleteManualRelation(id)
      onDeleted()
    } catch (err) {
      const message = err instanceof ApiError ? err.message : 'Erreur inattendue lors de la suppression.'
      setDeleteError(message)
    } finally {
      setDeletingIds((s) => {
        const next = new Set(s)
        next.delete(id)
        return next
      })
    }
  }

  return (
    <div className="mt-6 rounded-lg border border-gray-200 bg-white p-5 shadow-sm">
      <h2 className="text-base font-semibold text-gray-900">Relations ajoutees manuellement</h2>

      {manualState.status === 'loading' && (
        <p className="mt-3 text-sm text-gray-500">Chargement...</p>
      )}
      {manualState.status === 'error' && (
        <p className="mt-3 text-sm text-red-600">{manualState.message}</p>
      )}
      {deleteError && <p className="mt-3 text-sm text-red-600">{deleteError}</p>}

      {manualState.status === 'ready' && (
        manualState.relations.length === 0 ? (
          <p className="mt-3 text-sm text-gray-500">Aucune relation ajoutee manuellement pour l'instant.</p>
        ) : (
          <ul className="mt-3 flex flex-col gap-2">
            {manualState.relations.map((rel) => {
              const targetDisplay = rel.target_ticker ? `${rel.target_name} (${rel.target_ticker})` : rel.target_name
              return (
                <li
                  key={rel.id}
                  className="flex items-center justify-between gap-3 rounded-md border border-gray-100 bg-gray-50 px-3 py-2 text-sm"
                >
                  <span>
                    <code className="text-gray-900">{rel.source_ticker}</code>{' '}
                    -- <span className="font-semibold">{rel.relation_type}</span> --{'>'} {targetDisplay}
                    {rel.notes && <span className="text-gray-500"> -- {rel.notes}</span>}
                  </span>
                  <button
                    type="button"
                    onClick={() => handleDelete(rel.id)}
                    disabled={deletingIds.has(rel.id)}
                    className="shrink-0 rounded-md border border-red-200 px-3 py-1 text-xs font-medium text-red-700 hover:bg-red-50 disabled:opacity-50"
                  >
                    {deletingIds.has(rel.id) ? 'Suppression...' : 'Supprimer'}
                  </button>
                </li>
              )
            })}
          </ul>
        )
      )}
    </div>
  )
}
