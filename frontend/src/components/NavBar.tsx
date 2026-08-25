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
      {/* flex-wrap rather than a wider container: five tabs plus the runner
          overflow max-w-4xl, and the nav has to keep the same width as the
          page content below it. When it does not fit, the runner drops to
          its own right-aligned line instead of squashing the tabs. */}
      <div className="mx-auto flex max-w-4xl flex-wrap items-center justify-between gap-x-4 px-4">
        <div className="flex flex-wrap gap-1">
          {LINKS.map((link) => (
            <NavLink
              key={link.to}
              to={link.to}
              end={link.to === '/'}
              className={({ isActive }) =>
                `border-b-2 px-3 py-3 text-sm font-medium ${
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
            action are in the same place on every page. */}
        <PipelineRunner />
      </div>
    </nav>
  )
}
