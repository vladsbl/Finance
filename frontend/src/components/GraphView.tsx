import { useEffect, useRef, useState } from 'react'
import ForceGraph2D, { type ForceGraphMethods, type NodeObject } from 'react-force-graph-2d'
import type { GraphEdge, GraphNode } from '../types'

// --- "Glowing sphere of connected particles" node styling -------------------
//
// Two node "sizes" as before (primary/tracked vs external), but each is now
// drawn as three layers -- a soft radial-gradient halo, a solid core dot,
// and (primary only, and only once zoomed in enough to read it) a small
// ticker label -- instead of force-graph's flat default circle. HIT_R below
// is deliberately kept >= the OLD default circle radius (nodeRelSize(5) *
// sqrt(nodeVal), i.e. ~12.2px for primary/val=6 and ~7.1px for external/
// val=2, the values this component used before this restyle) so clicking a
// node is never LESS precise than it was -- the glow is purely decorative,
// painted on top of (never instead of) a hit area at least as generous as
// before. See nodePointerAreaPaint below, which is what actually makes this
// guarantee real: nodeCanvasObject's custom drawing does NOT auto-generate
// a matching hit area, so without an explicit nodePointerAreaPaint clicks
// would silently stop matching what's drawn.
const PRIMARY_CORE_R = 5
const PRIMARY_HALO_R = 15
const PRIMARY_HIT_R = 13

const EXTERNAL_CORE_R = 3
const EXTERNAL_HALO_R = 8
const EXTERNAL_HIT_R = 8

// Ticker labels (primary nodes only -- drawing one on all 500+ external
// nodes would be unreadable clutter, not "nets") only render once the user
// has zoomed in this far, so the default zoomed-out view stays a clean
// glowing web rather than a wall of overlapping text.
const LABEL_MIN_SCALE = 1.6

function paintNode(node: NodeObject<GraphNode>, ctx: CanvasRenderingContext2D, globalScale: number) {
  const x = node.x ?? 0
  const y = node.y ?? 0
  const isPrimary = node.kind === 'primary'
  const coreR = isPrimary ? PRIMARY_CORE_R : EXTERNAL_CORE_R
  const haloR = isPrimary ? PRIMARY_HALO_R : EXTERNAL_HALO_R

  // Halo: radial gradient fading to fully transparent -- this (not
  // ctx.shadowBlur) is what gives the glow, and stays cheap even with
  // hundreds of nodes on screen since it's one gradient fill per node, no
  // blur filter re-rasterised every frame.
  const halo = ctx.createRadialGradient(x, y, 0, x, y, haloR)
  if (isPrimary) {
    halo.addColorStop(0, 'rgba(95, 227, 255, 0.55)')
    halo.addColorStop(0.5, 'rgba(34, 211, 238, 0.18)')
    halo.addColorStop(1, 'rgba(34, 211, 238, 0)')
  } else {
    halo.addColorStop(0, 'rgba(148, 163, 184, 0.22)')
    halo.addColorStop(1, 'rgba(148, 163, 184, 0)')
  }
  ctx.fillStyle = halo
  ctx.beginPath()
  ctx.arc(x, y, haloR, 0, 2 * Math.PI)
  ctx.fill()

  // Solid core.
  ctx.beginPath()
  ctx.fillStyle = isPrimary ? '#5fe3ff' : '#94a3b8'
  ctx.arc(x, y, coreR, 0, 2 * Math.PI)
  ctx.fill()
  if (isPrimary) {
    ctx.lineWidth = 1
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.85)'
    ctx.stroke()
  }

  // Ticker label -- see LABEL_MIN_SCALE above. Font size compensates for
  // globalScale so the text stays a constant ON-SCREEN size while zooming
  // (matching how force-graph's own built-in label rendering behaves),
  // instead of growing/shrinking with the graph and becoming either
  // unreadably tiny or comically large.
  if (isPrimary && node.ticker && globalScale >= LABEL_MIN_SCALE) {
    const fontPx = 12 / globalScale
    ctx.font = `600 ${fontPx}px 'Share Tech Mono', monospace`
    ctx.textAlign = 'center'
    ctx.textBaseline = 'top'
    // A thin dark outline behind the text keeps it legible over the web of
    // links/other halos regardless of what's directly behind it -- the
    // actual "labels doivent rester nets" requirement.
    ctx.lineWidth = 3 / globalScale
    ctx.strokeStyle = 'rgba(5, 7, 10, 0.85)'
    ctx.strokeText(node.ticker, x, y + haloR * 0.55)
    ctx.fillStyle = '#dce8f5'
    ctx.fillText(node.ticker, x, y + haloR * 0.55)
  }
}

// Hit-test canvas paint -- force-graph maintains an invisible second canvas
// where every node/link is flat-filled in a unique colour, and a click is
// resolved by reading back the pixel colour under the cursor. Setting
// nodeCanvasObject WITHOUT this makes clicks fall back to a generic
// estimate that does not match the custom shape above; this makes the
// clickable area an explicit circle of radius PRIMARY_HIT_R/EXTERNAL_HIT_R
// -- at least as big as the plain circle this component drew before the
// glow restyle (see the constants' own comment), so clicking/hovering a
// node is never less precise than it was.
function paintNodeHitArea(node: NodeObject<GraphNode>, color: string, ctx: CanvasRenderingContext2D) {
  const x = node.x ?? 0
  const y = node.y ?? 0
  const r = node.kind === 'primary' ? PRIMARY_HIT_R : EXTERNAL_HIT_R
  ctx.fillStyle = color
  ctx.beginPath()
  ctx.arc(x, y, r, 0, 2 * Math.PI)
  ctx.fill()
}

// --- Links: thin, semi-transparent cyan, with a stable (never flickering
// between re-renders) per-link opacity variation for a sense of depth in
// the web, instead of every one of the (commonly 800+) edges looking like
// one flat, identical line. ---------------------------------------------

/** Both string ids (before the simulation starts) and resolved node object
 * references (every frame once it's running) are valid link.source/target
 * values at runtime -- GraphEdge's own TS type only reflects the initial
 * shape fetched from the API, so this narrows either case down to a plain
 * id string for hashing. */
function endpointId(end: GraphEdge['source'] | GraphNode): string {
  if (end && typeof end === 'object') return String((end as GraphNode).id ?? '')
  return String(end ?? '')
}

/** Cheap, deterministic (djb2) string hash -- same link always gets the
 * same opacity across re-renders/frames, so the "depth" effect reads as a
 * fixed property of the network rather than random flicker. */
function hashString(s: string): number {
  let h = 5381
  for (let i = 0; i < s.length; i++) h = (h * 33) ^ s.charCodeAt(i)
  return h >>> 0
}

function linkOpacity(link: GraphEdge): number {
  const key = `${endpointId(link.source)}|${endpointId(link.target)}`
  const MIN = 0.12
  const MAX = 0.42
  return MIN + (hashString(key) % 1000) / 1000 * (MAX - MIN)
}

function linkRgba(link: GraphEdge): string {
  return `rgba(34, 211, 238, ${linkOpacity(link).toFixed(3)})`
}

interface GraphViewProps {
  nodes: GraphNode[]
  edges: GraphEdge[]
  onNodeClick?: (ticker: string) => void
  /** Tailwind height class for the graph's container. Defaults to the
   * inline card size; the full-screen view (GraphPage) passes a taller one. */
  height?: string
}

// react-force-graph-2d over vis-network here: dashboard/app.py's pyvis
// rendering IS vis-network under the hood, so a vis-network port would
// look closer to the Streamlit page, but vis-network itself is vanilla JS
// -- using it from React means hand-rolling a useEffect/useRef wrapper
// that manually creates/destroys/updates a Network instance outside
// React's own render cycle (the community wrapper, react-graph-vis, is
// thin and largely unmaintained). react-force-graph-2d is a real React
// component instead: graphData is just a prop (React handles re-renders
// when it changes), zoom/recenter are exposed as imperative ref methods
// (zoom(), zoomToFit()) rather than reaching into a global `network`
// variable the way dashboard/app.py's injected <script> does, and it ships
// its own TypeScript types. Trade-off: the visual style (canvas-rendered
// circles vs vis-network's DOM/SVG nodes) won't be pixel-identical to the
// Streamlit page, but nothing in the task asked for that -- only an
// interactive graph with zoom/recenter, cleanly wired into React.
export function GraphView({ nodes, edges, onNodeClick, height = 'h-[560px]' }: GraphViewProps) {
  const fgRef = useRef<ForceGraphMethods<GraphNode, GraphEdge> | undefined>(undefined)
  const containerRef = useRef<HTMLDivElement>(null)
  // ForceGraph2D's `width`/`height` props default to window.innerWidth/
  // innerHeight when omitted (NOT the size of whatever container it's
  // rendered into -- react-force-graph has no auto-sizing of its own).
  // That mismatch is exactly what broke "recentrer": zoomToFit() centers
  // the graph within the CANVAS's own coordinate space, which -- left at
  // the window's full size -- is much bigger than this component's actual
  // visible box (clipped by its `overflow-hidden` container). The graph
  // WAS being centered correctly, just centered within a canvas far
  // larger than the visible viewport, so the visible top-left corner only
  // ever showed that oversized canvas's own top-left region instead of
  // its true center. Measuring the real container box with a
  // ResizeObserver and passing those exact pixel dimensions as `width`/
  // `height` below makes the canvas's coordinate space match what is
  // actually visible, so zoomToFit's center is the real center.
  const [dimensions, setDimensions] = useState<{ width: number; height: number } | null>(null)

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0]
      if (!entry) return
      const { width, height: measuredHeight } = entry.contentRect
      setDimensions({ width, height: measuredHeight })
    })
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  function zoomBy(factor: number) {
    const fg = fgRef.current
    if (!fg) return
    fg.zoom(fg.zoom() * factor, 250)
  }

  function recenter() {
    fgRef.current?.zoomToFit(400, 40)
  }

  return (
    <div
      ref={containerRef}
      className={`jarvis-graph-bg relative ${height} w-full overflow-hidden rounded-2xl border border-cyan-400/20`}
    >
      <div className="absolute right-3 top-3 z-10 flex flex-col gap-1.5">
        <button
          type="button"
          onClick={() => zoomBy(1.3)}
          title="Zoom avant"
          className="jarvis-pill flex h-8 w-8 items-center justify-center !rounded-full !p-0 text-sm font-semibold"
        >
          +
        </button>
        <button
          type="button"
          onClick={() => zoomBy(0.75)}
          title="Zoom arriere"
          className="jarvis-pill flex h-8 w-8 items-center justify-center !rounded-full !p-0 text-sm font-semibold"
        >
          &minus;
        </button>
        <button
          type="button"
          onClick={recenter}
          title="Recentrer la vue"
          className="jarvis-pill flex h-8 w-8 items-center justify-center !rounded-full !p-0 text-xs font-semibold"
        >
          &#8862;
        </button>
      </div>

      {nodes.length === 0 ? (
        <div className="flex h-full items-center justify-center text-sm text-faint">
          Aucune relation a afficher.
        </div>
      ) : !dimensions ? (
        <div className="flex h-full items-center justify-center text-sm text-faint">
          <span
            className="h-5 w-5 animate-spin rounded-full border-2 border-cyan-400/20 border-t-cyan-400"
            aria-hidden="true"
          />
        </div>
      ) : (
        <ForceGraph2D<GraphNode, GraphEdge>
          ref={fgRef}
          width={dimensions.width}
          height={dimensions.height}
          backgroundColor="rgba(0,0,0,0)"
          graphData={{ nodes, links: edges }}
          nodeLabel={(n) => `${n.display_name}${n.ticker ? ` (${n.ticker})` : ''}`}
          nodeCanvasObject={paintNode}
          nodePointerAreaPaint={paintNodeHitArea}
          linkLabel={(l) => (l.notes ? `${l.relation_type} -- ${l.notes}` : l.relation_type)}
          linkColor={linkRgba}
          linkWidth={0.6}
          linkDirectionalArrowLength={4}
          linkDirectionalArrowRelPos={1}
          linkDirectionalArrowColor={linkRgba}
          linkCurvature={0.15}
          onNodeClick={(n) => onNodeClick?.(n.ticker || String(n.id))}
          cooldownTicks={100}
          onEngineStop={() => fgRef.current?.zoomToFit(400, 40)}
        />
      )}
    </div>
  )
}
