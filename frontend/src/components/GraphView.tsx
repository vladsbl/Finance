import { useEffect, useRef, useState } from 'react'
import ForceGraph2D, { type ForceGraphMethods } from 'react-force-graph-2d'
import type { GraphEdge, GraphNode } from '../types'

const PRIMARY_COLOR = '#22d3ee'
const EXTERNAL_COLOR = '#64748b'
const EDGE_COLOR = '#3b6b82'

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
      className={`relative ${height} w-full overflow-hidden rounded-2xl border border-cyan-400/20 bg-navy-950/40`}
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
          graphData={{ nodes, links: edges }}
          nodeLabel={(n) => `${n.display_name}${n.ticker ? ` (${n.ticker})` : ''}`}
          nodeColor={(n) => (n.kind === 'primary' ? PRIMARY_COLOR : EXTERNAL_COLOR)}
          nodeRelSize={5}
          nodeVal={(n) => (n.kind === 'primary' ? 6 : 2)}
          linkLabel={(l) => (l.notes ? `${l.relation_type} -- ${l.notes}` : l.relation_type)}
          linkColor={() => EDGE_COLOR}
          linkDirectionalArrowLength={4}
          linkDirectionalArrowRelPos={1}
          linkCurvature={0.15}
          onNodeClick={(n) => onNodeClick?.(n.ticker || String(n.id))}
          cooldownTicks={100}
          onEngineStop={() => fgRef.current?.zoomToFit(400, 40)}
        />
      )}
    </div>
  )
}
