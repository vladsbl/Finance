import { useRef } from 'react'
import ForceGraph2D, { type ForceGraphMethods } from 'react-force-graph-2d'
import type { GraphEdge, GraphNode } from '../types'

const PRIMARY_COLOR = '#4f46e5'
const EXTERNAL_COLOR = '#9ca3af'
const EDGE_COLOR = '#94a3b8'

interface GraphViewProps {
  nodes: GraphNode[]
  edges: GraphEdge[]
  onNodeClick?: (ticker: string) => void
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
export function GraphView({ nodes, edges, onNodeClick }: GraphViewProps) {
  const fgRef = useRef<ForceGraphMethods<GraphNode, GraphEdge> | undefined>(undefined)

  function zoomBy(factor: number) {
    const fg = fgRef.current
    if (!fg) return
    fg.zoom(fg.zoom() * factor, 250)
  }

  function recenter() {
    fgRef.current?.zoomToFit(400, 40)
  }

  return (
    <div className="relative h-[560px] w-full overflow-hidden rounded-md border border-gray-200 bg-gray-50">
      <div className="absolute right-3 top-3 z-10 flex flex-col gap-1.5">
        <button
          type="button"
          onClick={() => zoomBy(1.3)}
          title="Zoom avant"
          className="flex h-8 w-8 items-center justify-center rounded-md border border-gray-300 bg-white text-sm font-semibold text-gray-700 shadow-sm hover:bg-gray-50"
        >
          +
        </button>
        <button
          type="button"
          onClick={() => zoomBy(0.75)}
          title="Zoom arriere"
          className="flex h-8 w-8 items-center justify-center rounded-md border border-gray-300 bg-white text-sm font-semibold text-gray-700 shadow-sm hover:bg-gray-50"
        >
          &minus;
        </button>
        <button
          type="button"
          onClick={recenter}
          title="Recentrer la vue"
          className="flex h-8 w-8 items-center justify-center rounded-md border border-gray-300 bg-white text-xs font-semibold text-gray-700 shadow-sm hover:bg-gray-50"
        >
          &#8862;
        </button>
      </div>

      {nodes.length === 0 ? (
        <div className="flex h-full items-center justify-center text-sm text-gray-500">
          Aucune relation a afficher.
        </div>
      ) : (
        <ForceGraph2D<GraphNode, GraphEdge>
          ref={fgRef}
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
