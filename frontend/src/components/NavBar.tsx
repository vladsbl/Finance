import { NavLink } from 'react-router-dom'

const LINKS = [
  { to: '/', label: 'Resume du jour' },
  { to: '/opportunities', label: 'Opportunites du jour' },
]

export function NavBar() {
  return (
    <nav className="border-b border-gray-200 bg-white">
      <div className="mx-auto flex max-w-4xl gap-1 px-4">
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
    </nav>
  )
}
