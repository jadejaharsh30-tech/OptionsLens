import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import { AppProvider } from './context/AppContext'
import App from './App'
import MarketStructure from './pages/MarketStructure'
import SmartMoney from './pages/SmartMoney'
import PositionLab from './pages/PositionLab'
import AlertEngine from './pages/AlertEngine'
import './index.css'

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <BrowserRouter>
      <AppProvider>
        <Routes>
          <Route path="/" element={<App />}>
            <Route index                element={<MarketStructure />} />
            <Route path="smart-money"   element={<SmartMoney />} />
            <Route path="position-lab"  element={<PositionLab />} />
            <Route path="alert-engine"  element={<AlertEngine />} />
          </Route>
        </Routes>
      </AppProvider>
    </BrowserRouter>
  </React.StrictMode>
)
