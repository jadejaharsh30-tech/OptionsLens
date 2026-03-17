// optionslens/frontend/src/context/AppContext.jsx
// Global state: token validity, selected symbol, selected expiry, spot price.
// Token is persisted to localStorage. Validated against /api/auth/validate.

import { createContext, useContext, useState, useEffect } from 'react'
import client from '../api/client'

const AppContext = createContext(null)

export function AppProvider({ children }) {
  const [token,      setTokenState] = useState(localStorage.getItem('fyers_token') || '')
  const [tokenValid, setTokenValid] = useState(false)
  const [symbol,     setSymbol]     = useState('NIFTY')
  const [expiry,     setExpiry]     = useState(null)   // { expiry: int, date: "DD-MM-YYYY" }
  const [spot,       setSpot]       = useState(null)
  const [symbols,    setSymbols]    = useState([])

  // Validate token against backend and store it
  const setToken = async (newToken) => {
    localStorage.setItem('fyers_token', newToken)
    setTokenState(newToken)
    try {
      const res = await client.get('/api/auth/validate', {
        headers: { Authorization: `Bearer ${newToken}` },
      })
      setSpot(res.data.nifty_ltp)
      setTokenValid(true)
      return { ok: true }
    } catch (e) {
      setTokenValid(false)
      return {
        ok: false,
        error: e.response?.data?.detail || 'Validation failed. Check your token.',
      }
    }
  }

  // Clear token (logout / re-paste)
  const clearToken = () => {
    localStorage.removeItem('fyers_token')
    setTokenState('')
    setTokenValid(false)
    setSpot(null)
  }

  // Load symbol list once (doesn't need auth)
  useEffect(() => {
    client.get('/api/symbols')
      .then(res => setSymbols(res.data.symbols))
      .catch(() => {})
  }, [])

  // Re-validate on mount if a stored token exists
  useEffect(() => {
    if (token) setToken(token)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <AppContext.Provider value={{
      token, tokenValid, setToken, clearToken,
      symbol, setSymbol,
      expiry, setExpiry,
      spot,   setSpot,
      symbols,
    }}>
      {children}
    </AppContext.Provider>
  )
}

export const useApp = () => {
  const ctx = useContext(AppContext)
  if (!ctx) throw new Error('useApp must be used within AppProvider')
  return ctx
}
