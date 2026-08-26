import { NavLink } from 'react-router-dom'
import { PipelineRunner } from './PipelineRunner'

const LINKS = [
  { to: '/', label: 'Resume du jour' },
  { to: '/opportunities', label: 'Opportunites du jour' },
  { to: '/stock', label: "Analyse d'une action" },
  { to: '/graph', label: 'Knowledge Graph' },
  { to: '/correlations', label: 'Correlations decouvertes' },
  { to: '/causal-reasoning', label: 'Raisonnement causal' },
  { to: '/news', label: 'News & Analyse IA' },
]

export function NavBar() {
  return (
    <nav className="border-b border-gray-200 bg-white">
      {/* Full window width -- unlike the page content below it (App.tsx's
          own max-w-4xl reading-width wrapper), the nav bar itself spans the
          whole viewport, not a centered reading column.
          Never wraps: seven tabs plus the runner don't always fit even a
          wide viewport, so instead of letting the tabs break onto a second
          line (which breaks the single-row alignment), the tabs row
          scrolls horizontally on its own and the runner stays pinned on
          the right -- min-w-0 is required for a flex child to be allowed
          to shrink below its content width, which is what makes
          overflow-x-auto actually kick in here instead of the row just
          pushing PipelineRunner off screen. */}
      <div className="flex w-full items-center gap-x-4 px-4 sm:px-6 lg:px-8">
        <div className="flex min-w-0 flex-1 gap-1 overflow-x-auto">
          {LINKS.map((link) => (
            <NavLink
              key={link.to}
              to={link.to}
              end={link.to === '/'}
              className={({ isActive }) =>
                `shrink-0 whitespace-nowrap border-b-2 px-3 py-3 text-sm font-medium ${
                  isActive
                    ? 'border-indigo-600 text-indigo-600'
                    : 'border-transparent text-gray-500 hover:text-gray-700'
                }`
              }
            >
              {link.label}
            </NavLink>
          ))}
        </div>

        {/* Lives in the NavBar so the freshness date and the recalculate
            action are in the same place on every page. shrink-0 keeps it
            from ever being squeezed by the tabs row above. */}
        <div className="shrink-0">
          <PipelineRunner />
        </div>
      </div>
    </nav>
  )
}
