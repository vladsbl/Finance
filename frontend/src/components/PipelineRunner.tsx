import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, fetchPipelineStatus, runPipeline } from '../api'
import type { PipelineState, PipelineStatus } from '../types'

// While a run is live. run_daily.py takes minutes, so polling faster buys
// nothing and just adds noise to the network log.
const POLL_MS = 5000

// Long enough for "Termine" to actually register before the page reloads.
const RELOAD_DELAY_MS = 1600

// The success/failure message has to survive the reload that brings the new
// data in, so it is handed over through sessionStorage rather than kept in
// React state (which the reload throws away).
const FLASH_KEY = 'pipeline:flash'

interface Flash {
  status: 'success' | 'failed'
  message: string
}

function readAndClearFlash(): Flash | null {
  try {
    const raw = sessionStorage.getItem(FLASH_KEY)
    if (!raw) return null
    sessionStorage.removeItem(FLASH_KEY)
    return JSON.parse(raw) as Flash
  } catch {
    return null
  }
}

function buildFlash(state: PipelineState): Flash {
  const last = state.last_run
  if (state.status === 'success') {
    const detail = last ? ` (${last.n_ok}/${last.steps_total} etapes)` : ''
    return { status: 'success', message: `Recalcul termine${detail}.` }
  }
  const detail = last
    ? ` ${last.n_failed} etape(s) en echec sur ${last.steps_total}.`
    : ''
  return { status: 'failed', message: `Recalcul termine avec des erreurs.${detail}` }
}

/** "21/08/2026 a 14:32" from an ISO datetime, or null if unusable. */
function formatDateTime(iso: string | null | undefined): string | null {
  if (!iso) return null
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return null
  return d.toLocaleString('fr-FR', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/** The most trustworthy "last recalculated at" available.
 *
 * finished_at is exact but only exists for a run THIS API process launched
 * and saw finish; log_modified_at (the log file's mtime) is the fallback
 * that also covers runs from Windows Task Scheduler and survives an API
 * restart -- see api/routers/pipeline.py. */
function lastRunLabel(state: PipelineState | null): string | null {
  if (!state) return null
  return formatDateTime(state.finished_at) ?? formatDateTime(state.last_run?.log_modified_at)
}

const DOT_STYLES: Record<PipelineStatus, string> = {
  idle: 'bg-gray-300',
  running: 'bg-indigo-500',
  success: 'bg-emerald-500',
  failed: 'bg-red-500',
}

export function PipelineRunner() {
  const [state, setState] = useState<PipelineState | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)
  const [flash, setFlash] = useState<Flash | null>(() => readAndClearFlash())

  // Previous status, to fire the "just finished" handling exactly once
  // rather than on every poll that returns the same finished state.
  const previousStatus = useRef<PipelineStatus | null>(null)
  const reloadScheduled = useRef(false)

  const refresh = useCallback(async () => {
    try {
      setState(await fetchPipelineStatus())
    } catch {
      // A failed status poll is not worth a banner: the pipeline may well be
      // fine and the API only briefly unreachable. The button stays usable
      // and the next tick retries.
    }
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  // Poll only while something is actually running.
  useEffect(() => {
    if (state?.status !== 'running') return
    const id = window.setInterval(refresh, POLL_MS)
    return () => window.clearInterval(id)
  }, [state?.status, refresh])

  // running -> success/failed: stash the outcome, then reload so every page
  // on screen refetches against the freshly recalculated database.
  useEffect(() => {
    if (!state) return
    const previous = previousStatus.current
    previousStatus.current = state.status

    const justFinished =
      previous === 'running' && (state.status === 'success' || state.status === 'failed')
    if (!justFinished || reloadScheduled.current) return

    reloadScheduled.current = true
    setFlash(buildFlash(state))
    const id = window.setTimeout(() => window.location.reload(), RELOAD_DELAY_MS)
    return () => window.clearTimeout(id)
  }, [state])

  async function handleRun() {
    setActionError(null)
    setFlash(null)
    setStarting(true)
    try {
      // The 202 response already carries status "running", so the UI flips
      // over immediately instead of waiting for the first poll.
      setState(await runPipeline())
    } catch (err) {
      const message =
        err instanceof ApiError ? err.message : 'Erreur inattendue lors du lancement du recalcul.'
      setActionError(message)
      refresh()
    } finally {
      setStarting(false)
    }
  }

  const isRunning = state?.status === 'running'
  const busy = isRunning || starting
  const last = state?.last_run
  const lastLabel = lastRunLabel(state)

  return (
    <div className="flex flex-col items-end gap-1 py-2 text-right">
      <div className="flex items-center gap-2">
        {isRunning && last && (
          <span className="text-xs text-gray-500">
            {last.current_step
              ? `${last.current_step} (${last.steps_done + 1}/${last.steps_total})`
              : `${last.steps_done}/${last.steps_total} etapes`}
          </span>
        )}

        <button
          type="button"
          onClick={handleRun}
          disabled={busy}
          title={
            busy
              ? 'Un recalcul est deja en cours'
              : 'Relance pipeline/run_daily.py (plusieurs minutes)'
          }
          className="flex items-center gap-2 rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {busy && (
            <span
              className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/40 border-t-white"
              aria-hidden="true"
            />
          )}
          {busy ? 'En cours...' : 'Recalculer maintenant'}
        </button>
      </div>

      <div className="flex items-center gap-1.5 text-xs">
        <span
          className={`h-1.5 w-1.5 rounded-full ${DOT_STYLES[state?.status ?? 'idle']}`}
          aria-hidden="true"
        />
        <span className="text-gray-500">
          {lastLabel ? `Dernier recalcul : ${lastLabel}` : 'Aucun recalcul enregistre'}
        </span>
      </div>

      {flash && (
        <span
          className={`text-xs font-medium ${
            flash.status === 'success' ? 'text-emerald-700' : 'text-red-600'
          }`}
        >
          {flash.message}
        </span>
      )}

      {actionError && <span className="max-w-xs text-xs text-red-600">{actionError}</span>}
    </div>
  )
}
