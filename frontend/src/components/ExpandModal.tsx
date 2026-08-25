import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

// Milliseconds the fade/scale transition runs -- kept as one constant so
// the CSS duration and the JS unmount delay below can never drift apart.
const TRANSITION_MS = 200

interface ExpandModalProps {
  isOpen: boolean
  onClose: () => void
  /** Optional heading shown in the modal's top bar, next to the close button. */
  title?: string
  children: React.ReactNode
}

/**
 * Generic full-screen "expand" modal -- a reusable overlay for showing any
 * content larger/richer than it can be shown inline in a table row, a
 * card, or a chart thumbnail. Not specific to any one page: mount it once
 * near the top of a page component, drive `isOpen` from local state, and
 * put whatever JSX belongs in the expanded view as `children`.
 *
 * Renders via a portal onto `document.body` so it always paints as a true
 * full-screen overlay regardless of where in the DOM tree it is mounted --
 * a page with e.g. a `transform`-ed ancestor (common with animation
 * libraries) would otherwise trap a `position: fixed` child inside that
 * ancestor's box instead of the viewport.
 *
 * Closes on: the X button, the Escape key, or a click on the dark
 * backdrop (clicks inside the content panel do not propagate to the
 * backdrop, so interacting with the expanded content itself never closes
 * it). Locks background scroll while open. Runs a real close animation:
 * the component stays mounted for TRANSITION_MS after `isOpen` goes
 * false so the fade/scale-out actually plays, instead of vanishing
 * instantly.
 *
 * Usage (see frontend/src/pages/CorrelationsPage.tsx for a full example):
 *
 *   const [expanded, setExpanded] = useState<Correlation | null>(null)
 *   ...
 *   <tr onClick={() => setExpanded(row)}>...</tr>
 *   ...
 *   <ExpandModal isOpen={expanded !== null} onClose={() => setExpanded(null)} title="Comparaison">
 *     {expanded && <MyRichComparisonView data={expanded} />}
 *   </ExpandModal>
 */
export function ExpandModal({ isOpen, onClose, title, children }: ExpandModalProps) {
  // Lags `isOpen` by TRANSITION_MS on the way out only, so the panel is
  // still in the DOM (and CSS can transition it back to its closed state)
  // instead of disappearing the instant the caller flips isOpen to false.
  const [mounted, setMounted] = useState(isOpen)
  // Separate from `mounted`: controls which CSS state (open vs closed)
  // the panel renders in -- set one tick after mounting so the browser
  // still sees the "closed" starting state first and actually animates
  // the transition, rather than starting already-open.
  const [visible, setVisible] = useState(false)
  const closeButtonRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (isOpen) {
      setMounted(true)
      // rAF, not a bare synchronous set: guarantees the browser paints
      // the initial (closed) state at least once before the transition
      // target class is applied, which is what actually makes it animate.
      const raf = requestAnimationFrame(() => setVisible(true))
      return () => cancelAnimationFrame(raf)
    }
    setVisible(false)
    const timer = window.setTimeout(() => setMounted(false), TRANSITION_MS)
    return () => window.clearTimeout(timer)
  }, [isOpen])

  useEffect(() => {
    if (!mounted) return
    closeButtonRef.current?.focus()

    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleKeyDown)

    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    return () => {
      window.removeEventListener('keydown', handleKeyDown)
      document.body.style.overflow = previousOverflow
    }
  }, [mounted, onClose])

  if (!mounted) return null

  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-label={title}
      onClick={onClose}
      className={`fixed inset-0 z-50 flex items-center justify-center bg-gray-950/60 p-4 transition-opacity duration-200 ${
        visible ? 'opacity-100' : 'opacity-0'
      }`}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className={`flex max-h-[90vh] w-full max-w-3xl flex-col overflow-hidden rounded-lg bg-white shadow-xl transition-all duration-200 ${
          visible ? 'scale-100 opacity-100' : 'scale-95 opacity-0'
        }`}
      >
        <div className="flex shrink-0 items-center justify-between border-b border-gray-200 px-5 py-3">
          <h2 className="text-base font-semibold text-gray-900">{title}</h2>
          <button
            ref={closeButtonRef}
            type="button"
            onClick={onClose}
            aria-label="Fermer"
            className="rounded-md p-1.5 text-gray-400 hover:bg-gray-100 hover:text-gray-600"
          >
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="h-5 w-5">
              <path d="M6.28 5.22a.75.75 0 0 0-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 1 0 1.06 1.06L10 11.06l3.72 3.72a.75.75 0 1 0 1.06-1.06L11.06 10l3.72-3.72a.75.75 0 0 0-1.06-1.06L10 8.94 6.28 5.22Z" />
            </svg>
          </button>
        </div>
        <div className="overflow-y-auto p-5">{children}</div>
      </div>
    </div>,
    document.body,
  )
}
