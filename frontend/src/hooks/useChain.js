import { useState, useEffect, useCallback } from 'react'
import client from '../api/client'

export default function useChain(symbol, expiry) {
  const [data,    setData]    = useState(null)
  const [loading, setLoading] = useState(false)
  const [error,   setError]   = useState(null)

  const fetch_ = useCallback(async () => {
    if (!symbol || !expiry) return
    setLoading(true)
    setError(null)
    try {
      const res = await client.get(`/api/chain/${symbol}`, {
        params: { expiry_epoch: expiry.expiry, expiry_date: expiry.date },
      })
      setData(res.data)
    } catch (e) {
      setError(e.response?.data?.detail || e.message || 'Failed to fetch chain')
    } finally {
      setLoading(false)
    }
  }, [symbol, expiry])

  useEffect(() => { fetch_() }, [fetch_])

  return { data, loading, error, refetch: fetch_ }
}
