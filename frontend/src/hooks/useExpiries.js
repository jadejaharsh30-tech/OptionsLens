// optionslens/frontend/src/hooks/useExpiries.js
import { useState, useEffect, useCallback } from 'react'
import client from '../api/client'

export default function useExpiries(symbol) {
  const [data,    setData]    = useState(null)
  const [loading, setLoading] = useState(false)
  const [error,   setError]   = useState(null)

  const fetch_ = useCallback(async () => {
    if (!symbol) return
    setLoading(true); setError(null)
    try {
      const res = await client.get(`/api/expiries/${symbol}`)
      setData(res.data.expiries || [])   // [{expiry: epoch, date: "DD-MM-YYYY"}, ...]
    } catch (e) {
      setError(e.response?.data?.detail || e.message)
    } finally {
      setLoading(false)
    }
  }, [symbol])

  useEffect(() => { fetch_() }, [fetch_])

  return { data, loading, error }
}
