import { useState } from 'react'
import { useApp } from '../context/AppContext'

export default function TokenGate() {
  const { setToken } = useApp()
  const [input,   setInput]   = useState('')
  const [loading, setLoading] = useState(false)
  const [error,   setError]   = useState(null)

  const handleValidate = async () => {
    if (!input.trim()) return
    setLoading(true); setError(null)
    const result = await setToken(input.trim())
    setLoading(false)
    if (!result.ok) setError(result.error)
  }

  const handleKeyDown = e => { if (e.key === 'Enter' && e.ctrlKey) handleValidate() }

  return (
    <div className="min-h-screen flex items-center justify-center p-6"
         style={{ background: '#FBF7F0' }}>

      {/* Subtle warm glow behind card */}
      <div className="absolute inset-0 pointer-events-none"
           style={{
             background: 'radial-gradient(ellipse at 50% 40%, rgba(200,134,10,0.07) 0%, transparent 65%)',
           }} />

      <div className="relative w-full max-w-md">
        {/* Decorative top bar */}
        <div className="h-1 rounded-t-xl" style={{ background: 'linear-gradient(90deg, #C8860A, #D97706, #0D9488)' }} />

        <div className="card rounded-t-none"
             style={{ borderTop: 'none', borderRadius: '0 0 10px 10px' }}>

          {/* Header */}
          <div className="mb-8 pt-2">
            <div className="flex items-center gap-2 mb-4">
              <div className="w-2 h-2 rounded-full animate-pulse" style={{ background: '#D97706' }} />
              <span className="text-xs mono uppercase tracking-widest" style={{ color: '#A89585' }}>
                OptionsLens v1.0
              </span>
            </div>
            <h1 className="font-display text-3xl font-semibold mb-2" style={{ color: '#2C1810' }}>
              Connect to Fyers
            </h1>
            <p className="text-sm leading-relaxed" style={{ color: '#7A6355' }}>
              Paste your daily access token to begin. Tokens expire at midnight —
              paste a fresh one each morning.
            </p>
          </div>

          {/* Token input */}
          <div className="flex flex-col gap-3">
            <label className="text-xs mono uppercase tracking-wider" style={{ color: '#A89585' }}>
              Access Token
            </label>
            <textarea
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
              rows={5}
              className="w-full rounded-lg p-3 text-xs mono resize-none focus:outline-none
                         transition-all duration-150 leading-relaxed"
              style={{
                background: '#FBF7F0',
                border: `1px solid ${error ? '#DC2626' : '#E8DDD0'}`,
                color: '#2C1810',
              }}
              onFocus={e => { if (!error) e.target.style.borderColor = '#C8860A' }}
              onBlur={e  => { if (!error) e.target.style.borderColor = '#E8DDD0' }}
            />

            {error && (
              <p className="text-xs mono flex items-center gap-1.5" style={{ color: '#DC2626' }}>
                <span>⚠</span> {error}
              </p>
            )}

            <button
              onClick={handleValidate}
              disabled={loading || !input.trim()}
              className="w-full py-3 rounded-lg text-sm font-semibold
                         transition-all duration-150 disabled:opacity-40 disabled:cursor-not-allowed"
              style={{
                background: loading || !input.trim() ? '#E8DDD0' : '#2C1810',
                color: loading || !input.trim() ? '#A89585' : '#FBF7F0',
              }}
            >
              {loading
                ? <span className="flex items-center justify-center gap-2">
                    <span className="w-4 h-4 rounded-full border-2 inline-block animate-spin"
                          style={{ borderColor: 'rgba(251,247,240,0.3)', borderTopColor: '#FBF7F0' }} />
                    Validating…
                  </span>
                : 'Connect'}
            </button>
          </div>

          {/* How to get token */}
          <div className="mt-6 pt-5" style={{ borderTop: '1px solid #E8DDD0' }}>
            <details>
              <summary className="text-xs cursor-pointer transition-colors select-none"
                       style={{ color: '#A89585' }}
                       onMouseEnter={e => e.target.style.color = '#7A6355'}
                       onMouseLeave={e => e.target.style.color = '#A89585'}>
                How to generate a token
              </summary>
              <div className="mt-3 space-y-1.5 text-xs mono pl-3"
                   style={{ color: '#A89585', borderLeft: '2px solid #E8DDD0' }}>
                <p>1. Run <span style={{ color: '#2C1810' }}>fyers_login_test.py</span></p>
                <p>2. Copy <span style={{ color: '#2C1810' }}>access_token</span> from the JSON output</p>
                <p>3. Paste it above and click Connect</p>
                <p style={{ color: '#D4C4B0' }} className="pt-1">Tip: Ctrl+Enter to connect</p>
              </div>
            </details>
          </div>

        </div>
      </div>
    </div>
  )
}
