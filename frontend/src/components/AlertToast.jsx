// optionslens/frontend/src/components/AlertToast.jsx
// Mounted at App level — persists across ALL page navigation.
// Polls /api/alert-engine/alerts every 5s.
// On new alerts: plays 3-beep sound + shows toast card for 8s.
// Tracks seen alert IDs in a ref to detect only new ones each poll.

import { useEffect, useRef, useState } from 'react'
import { useApp } from '../context/AppContext'
import client from '../api/client'

// ── Web Audio beep — exact port of app_v2_final.py Section 6 ─────────────────
// Three-tone pattern: 880Hz → 1100Hz → 880Hz
function playAlertBeep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)()
    const beep = (freq, start, duration) => {
      const osc  = ctx.createOscillator()
      const gain = ctx.createGain()
      osc.connect(gain)
      gain.connect(ctx.destination)
      osc.frequency.value = freq
      osc.type = 'sine'
      gain.gain.setValueAtTime(0.4, ctx.currentTime + start)
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + start + duration)
      osc.start(ctx.currentTime + start)
      osc.stop(ctx.currentTime + start + duration)
    }
    beep(880,  0,   0.15)
    beep(1100, 0.2, 0.15)
    beep(880,  0.4, 0.25)
  } catch {
    // AudioContext blocked before user gesture — silent fail
  }
}

// ── Single toast card ─────────────────────────────────────────────────────────
function ToastCard({ alert, onDismiss }) {
  const isBullish   = alert.signal_direction === 'BULLISH'
  const accentColor = isBullish ? '#0D9488' : '#DC2626'
  const bgColor     = isBullish ? 'rgba(13,148,136,0.06)' : 'rgba(220,38,38,0.06)'
  const arrow       = isBullish ? '▲' : '▼'

  const confidenceStyle = {
    HIGH:   { background: '#0D9488', color: '#fff' },
    MEDIUM: { background: '#D97706', color: '#fff' },
    LOW:    { background: '#A89585', color: '#fff' },
  }[alert.confidence] || { background: '#A89585', color: '#fff' }

  return (
    <div
      className="card fade-up"
      style={{
        background:  bgColor,
        borderColor: accentColor,
        borderLeft:  `3px solid ${accentColor}`,
        minWidth:    '280px',
        maxWidth:    '320px',
        position:    'relative',
        cursor:      'default',
      }}
    >
      {/* Dismiss button */}
      <button
        onClick={onDismiss}
        style={{
          position: 'absolute', top: 8, right: 10,
          color: '#A89585', fontSize: 14, lineHeight: 1,
          background: 'none', border: 'none', cursor: 'pointer',
        }}
        onMouseEnter={e => e.target.style.color = '#2C1810'}
        onMouseLeave={e => e.target.style.color = '#A89585'}
      >
        ✕
      </button>

      {/* Header row */}
      <div className="flex items-center gap-2 mb-2 pr-5">
        <span className="font-semibold text-sm" style={{ color: accentColor }}>
          {arrow} {alert.signal_direction}
        </span>
        <span className="mono text-xs px-1.5 py-0.5 rounded"
              style={confidenceStyle}>
          {alert.confidence}
        </span>
        <span className="mono text-xs font-semibold" style={{ color: '#2C1810' }}>
          {alert.symbol}
        </span>
      </div>

      {/* Signal details */}
      <div className="text-xs mono space-y-0.5" style={{ color: '#7A6355' }}>
        <div>
          <span style={{ color: '#A89585' }}>Signal: </span>
          {alert.strike} {alert.option_type} SHORT BUILDUP
        </div>
        <div>
          <span style={{ color: '#A89585' }}>Trade: </span>
          BUY{' '}
          <span style={{ color: accentColor, fontWeight: 600 }}>
            {alert.trade_strike} {alert.trade_option}
          </span>
        </div>
        <div>
          <span style={{ color: '#A89585' }}>OI Δ: </span>
          +{alert.oi_pct_change}%
          <span style={{ color: '#A89585' }}> · Speed: </span>
          {alert.oi_speed_pct_pm?.toFixed(1)}%/min
        </div>
        <div style={{ color: '#A89585', fontSize: 10, marginTop: 2 }}>
          {alert.triggered_at?.slice(11, 19)} IST
        </div>
      </div>
    </div>
  )
}

// ── Main AlertToast component ─────────────────────────────────────────────────
export default function AlertToast() {
  const { tokenValid } = useApp()
  const [toasts,  setToasts]  = useState([])   // [{toastId, alert}]
  const seenIdsRef = useRef(new Set())          // alert IDs already shown

  // Poll /api/alert-engine/alerts every 5s
  useEffect(() => {
    if (!tokenValid) return

    const poll = async () => {
      try {
        const res    = await client.get('/api/alert-engine/alerts')
        const alerts = res.data.alerts || []

        // Only show alerts we haven't seen before
        const newAlerts = alerts.filter(a => !seenIdsRef.current.has(a.id))

        if (newAlerts.length > 0) {
          playAlertBeep()
          setToasts(prev => [
            ...newAlerts.map(a => ({
              toastId: `toast-${a.id}-${Date.now()}`,
              alert:   a,
            })),
            ...prev,
          ].slice(0, 5))  // max 5 toasts visible at once
          newAlerts.forEach(a => seenIdsRef.current.add(a.id))
        }
      } catch {
        // Engine not running / network error — silent
      }
    }

    poll()  // immediate on mount
    const id = setInterval(poll, 5000)
    return () => clearInterval(id)
  }, [tokenValid])

  // Auto-dismiss oldest toast after 8s
  useEffect(() => {
    if (toasts.length === 0) return
    const id = setTimeout(() => {
      setToasts(prev => prev.slice(0, -1))
    }, 8000)
    return () => clearTimeout(id)
  }, [toasts])

  if (toasts.length === 0) return null

  return (
    <div
      style={{
        position:      'fixed',
        top:           '72px',    // just below nav bar
        right:         '16px',
        zIndex:        9999,
        display:       'flex',
        flexDirection: 'column',
        gap:           '8px',
        pointerEvents: 'none',   // allow clicks through empty space
      }}
    >
      {toasts.map(t => (
        <div key={t.toastId} style={{ pointerEvents: 'auto' }}>
          <ToastCard
            alert={t.alert}
            onDismiss={() =>
              setToasts(prev => prev.filter(x => x.toastId !== t.toastId))
            }
          />
        </div>
      ))}
    </div>
  )
}
