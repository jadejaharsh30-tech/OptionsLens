import { useState, useEffect, useCallback } from 'react'
import client from '../api/client'

export default function useSurface(symbol) {
  const [data,    setData]    = useState(null)
  const [loading, setLoading] = useState(false)
  const [error,   setError]   = useState(null)

  const fetch_ = useCallback(async () => {
    if (!symbol) return
    setLoading(true)
    setError(null)
    try {
      const res = await client.get(`/api/surface/${symbol}`)
      setData(res.data)
    } catch (e) {
      setError(e.response?.data?.detail || e.message || 'Failed to fetch surface data')
    } finally {
      setLoading(false)
    }
  }, [symbol])

  useEffect(() => { fetch_() }, [fetch_])

  return { data, loading, error, refetch: fetch_ }
}
