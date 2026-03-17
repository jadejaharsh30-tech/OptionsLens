// optionslens/frontend/src/api/client.js
// Central Axios instance. Token injected per-request from localStorage.
// Vite proxy forwards /api/* → localhost:8000 in dev.
// In production, set VITE_API_URL in .env.

import axios from 'axios'

const BASE_URL = import.meta.env.VITE_API_URL || ''

const client = axios.create({ baseURL: BASE_URL })

// Inject Bearer token on every request
client.interceptors.request.use((config) => {
  const token = localStorage.getItem('fyers_token')
  if (token) {
    config.headers['Authorization'] = `Bearer ${token}`
  }
  return config
})

// Centralised error logging (non-blocking)
client.interceptors.response.use(
  res => res,
  err => {
    // 401 means token expired — surface-level handling done in components
    if (err.response?.status === 401) {
      console.warn('[OptionsLens] Token expired or invalid.')
    }
    return Promise.reject(err)
  }
)

export default client
