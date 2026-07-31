import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { NavBar } from './components/NavBar'
import { DailySummaryPage } from './pages/DailySummaryPage'
import { GraphPage } from './pages/GraphPage'
import { OpportunitiesPage } from './pages/OpportunitiesPage'
import { StockPage } from './pages/StockPage'

function App() {
  return (
    <BrowserRouter>
      <NavBar />
      <div className="mx-auto max-w-4xl px-4 py-8">
        <Routes>
          <Route path="/" element={<DailySummaryPage />} />
          <Route path="/opportunities" element={<OpportunitiesPage />} />
          <Route path="/stock" element={<StockPage />} />
          <Route path="/graph" element={<GraphPage />} />
        </Routes>
      </div>
    </BrowserRouter>
  )
}

export default App
