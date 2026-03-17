import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // All /api/* and /health calls forwarded to FastAPI backend
      // No hardcoded URLs in components — everything goes through this proxy
      '/api':    'http://localhost:8000',
      '/health': 'http://localhost:8000',
    }
  }
})
