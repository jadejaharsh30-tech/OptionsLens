// optionslens/frontend/src/pages/AlertEngine.jsx
// Five sections:
//   1. Status header         — running indicator, active symbols, alert count, last poll
//   2. Pending confirmation  — Stage 1 spikes awaiting Stage 2 (⏳ Awaiting Premium Confirmation)
//   3. Chain snapshot        — live monitored strikes table (shown when running)
//   4. Control panel         — symbol checkboxes + threshold sliders (shown when stopped)
//   5. Alert log             — today's confirmed alerts with suppress button

import { useState, useEffect, useCallback, useRef } from 'react'
import { useApp } from '../context/AppContext'
import client from '../api/client'
import ErrorBanner from '../components/ErrorBanner'
import LoadingSpinner from '../components/LoadingSpinner'
import InfoPanel, { Section, P, Callout, KV } from '../components/InfoPanel'

const ALL_SYMBOLS = ['NIFTY', 'BANKNIFTY', 'RELIANCE', 'TCS', 'HDFCBANK', 'INFY', 'ICICIBANK']

const fmt = n => {
  if (n == null) return '—'
  const abs = Math.abs(n)
  if (abs >= 1e6) return `${(n / 1e6).toFixed(1)}M`
  if (abs >= 1e3) return `${(n / 1e3).toFixed(0)}K`
  return String(n)
}

// ── Status indicator ──────────────────────────────────────────────────────────
function StatusDot({ status }) {
  const map = {
    ok:           { color: '#0D9488', label: 'Live' },
    market_closed:{ color: '#D97706', label: 'Market Closed' },
    error:        { color: '#DC2626', label: 'Error' },
    starting:     { color: '#C8860A', label: 'Starting…' },
    idle:         { color: '#A89585', label: 'Idle' },
  }
  const { color, label } = map[status] || map.idle
  return (
    <div className="flex items-center gap-1.5">
      <div style={{
        width: 8, height: 8, borderRadius: '50%', background: color,
        boxShadow: status === 'ok' ? `0 0 6px ${color}` : 'none',
        animation: status === 'ok' ? 'pulse 2s infinite' : 'none',
      }} />
      <span className="text-xs mono" style={{ color }}>{label}</span>
    </div>
  )
}

// ── Confidence badge ──────────────────────────────────────────────────────────
function ConfidenceBadge({ level }) {
  const styles = {
    HIGH:   { background: '#0D9488', color: '#fff' },
    MEDIUM: { background: '#D97706', color: '#fff' },
    LOW:    { background: '#A89585', color: '#fff' },
  }
  return (
    <span className="mono text-xs px-1.5 py-0.5 rounded"
      style={styles[level] || styles.LOW}>
      {level}
    </span>
  )
}

// ── Shared style helpers ──────────────────────────────────────────────────────
const SL = { color: '#2C1810', fontWeight: 600, fontSize: 13 }   // section label

export default function AlertEngine() {
  const { tokenValid } = useApp()

  // ── Config (control panel) ────────────────────────────────────────────────
  const [selectedSymbols, setSelectedSymbols] = useState(['NIFTY', 'BANKNIFTY'])
  const [pollInterval,    setPollInterval]    = useState(10)
  const [spikeThreshold,  setSpikeThreshold]  = useState(500)
  const [speedWindow,     setSpeedWindow]     = useState(5)
  const [confirmPolls,    setConfirmPolls]    = useState(4)
  const [minVolume,       setMinVolume]       = useState(100)
  const [strikesEither,   setStrikesEither]   = useState(1)

  // ── NEW: Adaptive threshold (default OFF — preserves existing behaviour) ──
  const [useAdaptive, setUseAdaptive] = useState(false)
  const [adaptivePct, setAdaptivePct] = useState(90)

  // ── Runtime state ─────────────────────────────────────────────────────────
  const [status,             setStatus]            = useState(null)
  const [alerts,             setAlerts]            = useState([])
  const [snapshot,           setSnapshot]          = useState({})
  const [startErr,           setStartErr]          = useState(null)
  const [starting,           setStarting]          = useState(false)
  const [showThresholdsInfo, setShowThresholdsInfo]= useState(false)

  // ── Poll all three endpoints every 5s ─────────────────────────────────────
  const pollAll = useCallback(async () => {
    try {
      const [sRes, aRes, cRes] = await Promise.all([
        client.get('/api/alert-engine/status'),
        client.get('/api/alert-engine/alerts'),
        client.get('/api/alert-engine/chain-snapshot'),
      ])
      setStatus(sRes.data)
      setAlerts(aRes.data.alerts || [])
      setSnapshot(cRes.data.snapshots || {})
    } catch { /* engine not started yet — ignore */ }
  }, [])

  useEffect(() => {
    pollAll()
    const id = setInterval(pollAll, 5000)
    return () => clearInterval(id)
  }, [pollAll])

  // ── Start engine ──────────────────────────────────────────────────────────
  const handleStart = async () => {
    if (selectedSymbols.length === 0) {
      setStartErr('Select at least one symbol.')
      return
    }
    setStarting(true); setStartErr(null)
    try {
      await client.post('/api/alert-engine/start', {
        symbols:                selectedSymbols,
        poll_interval_sec:      pollInterval,
        oi_spike_threshold_pct: spikeThreshold,
        oi_speed_window_min:    speedWindow,
        premium_confirm_polls:  confirmPolls,
        min_volume_filter:      minVolume,
        strikes_either_side:    strikesEither,
        // NEW: adaptive threshold fields (default false = original behaviour)
        use_adaptive_threshold: useAdaptive,
        adaptive_percentile:    adaptivePct,
      })
      await pollAll()
    } catch (e) {
      setStartErr(e.response?.data?.detail || e.message)
    } finally {
      setStarting(false)
    }
  }

  // ── Stop engine ───────────────────────────────────────────────────────────
  const handleStop = async () => {
    try { await client.post('/api/alert-engine/stop'); await pollAll() }
    catch (e) { setStartErr(e.response?.data?.detail || e.message) }
  }

  // ── Suppress alert ────────────────────────────────────────────────────────
  const handleSuppress = async (alertId) => {
    try {
      await client.delete(`/api/alert-engine/alerts/${alertId}`)
      setAlerts(prev => prev.filter(a => a.id !== alertId))
    } catch { /* silent */ }
  }

  const isRunning    = status?.running === true
  const totalPending = Object.values(status?.pending_spikes || {})
    .reduce((s, arr) => s + arr.length, 0)
  const inputStyle = { background: '#FBF7F0', border: '1px solid #E8DDD0', color: '#2C1810' }
  const focusGold  = e => { e.target.style.borderColor = '#C8860A' }
  const blurSand   = e => { e.target.style.borderColor = '#E8DDD0' }

  return (
    <div className="flex flex-col gap-5 fade-up">

      {/* ── 1. Status header ──────────────────────────────────────────────── */}
      <div className="card flex flex-wrap items-center gap-6">

        {/* Status dot */}
        <div>
          <div className="text-xs mb-1" style={{ color: '#A89585' }}>Engine</div>
          <StatusDot status={status?.last_poll_status || 'idle'} />
        </div>

        {/* Runtime metrics — only when running */}
        {isRunning && <>
          <div>
            <div className="text-xs mb-0.5" style={{ color: '#A89585' }}>Symbols</div>
            <div className="mono text-sm font-semibold" style={{ color: '#2C1810' }}>
              {status.active_symbols?.join(', ') || '—'}
            </div>
          </div>
          <div>
            <div className="text-xs mb-0.5" style={{ color: '#A89585' }}>Alerts Today</div>
            <div className="mono text-xl font-semibold" style={{ color: '#C8860A' }}>
              {status.alert_count_session}
            </div>
          </div>
          <div>
            <div className="text-xs mb-0.5" style={{ color: '#A89585' }}>Pending</div>
            <div className="mono text-xl font-semibold"
              style={{ color: totalPending > 0 ? '#D97706' : '#A89585' }}>
              {totalPending}
            </div>
          </div>
          {/* BUG FIX: format last_poll_at as HH:MM:SS IST so it visibly updates each poll */}
          <div>
            <div className="text-xs mb-0.5" style={{ color: '#A89585' }}>Last Poll</div>
            <div className="mono text-sm" style={{ color: '#2C1810' }}>
              {status.last_poll_at
                ? new Date(status.last_poll_at).toLocaleTimeString('en-IN', {
                    timeZone: 'Asia/Kolkata',
                    hour: '2-digit', minute: '2-digit', second: '2-digit',
                  })
                : '—'}
            </div>
          </div>
          <div>
            <div className="text-xs mb-0.5" style={{ color: '#A89585' }}>Pending Confirms</div>
            <div className="mono text-sm font-semibold"
              style={{ color: totalPending > 0 ? '#D97706' : '#A89585' }}>
              {totalPending}
            </div>
          </div>
          {status?.config && (
            <div>
              <div className="text-xs mb-0.5" style={{ color: '#A89585' }}>OI Threshold</div>
              <div className="mono text-sm" style={{ color: '#7A6355' }}>
                {status.config.oi_spike_threshold_pct}%
                {/* NEW: show adaptive badge when active */}
                {status.config.use_adaptive_threshold && (
                  <span className="ml-1.5 mono text-xs px-1.5 py-0.5 rounded"
                    style={{ background: 'rgba(13,148,136,0.10)', color: '#0D9488',
                             border: '1px solid rgba(13,148,136,0.25)' }}>
                    adaptive {status.config.adaptive_percentile}th
                  </span>
                )}
              </div>
            </div>
          )}
        </>}

        <div className="flex-1" />

        {/* Start / Stop button */}
        {!isRunning ? (
          <button
            onClick={handleStart}
            disabled={starting || selectedSymbols.length === 0}
            className="px-6 py-2 rounded-lg text-sm font-semibold transition-all
                       disabled:opacity-40 disabled:cursor-not-allowed"
            style={{ background: '#2C1810', color: '#FBF7F0' }}
            onMouseEnter={e => { if (!starting) e.target.style.background = '#3D2418' }}
            onMouseLeave={e => { e.target.style.background = '#2C1810' }}
          >
            {starting
              ? <span className="flex items-center gap-2">
                  <span className="w-4 h-4 rounded-full border-2 inline-block animate-spin"
                    style={{ borderColor: 'rgba(251,247,240,0.3)', borderTopColor: '#FBF7F0' }} />
                  Starting…
                </span>
              : '▶ Start Engine'}
          </button>
        ) : (
          <button
            onClick={handleStop}
            className="px-6 py-2 rounded-lg text-sm font-semibold transition-all"
            style={{ background: '#DC2626', color: '#fff' }}
            onMouseEnter={e => e.target.style.background = '#B91C1C'}
            onMouseLeave={e => e.target.style.background = '#DC2626'}
          >
            ■ Stop Engine
          </button>
        )}
      </div>

      {startErr && <ErrorBanner message={startErr} />}
      {isRunning && status?.last_error && (
        <ErrorBanner message={`Engine error: ${status.last_error}`} />
      )}

      {/* ── 2. Pending confirmation spikes ────────────────────────────────── */}
      {/* BUG FIX: this section was missing from the rewrite — restored from original */}
      {isRunning && totalPending > 0 && (
        <div className="card"
          style={{ borderColor: 'rgba(217,119,6,0.3)', background: 'rgba(217,119,6,0.03)' }}>
          <h3 className="mb-3" style={{ ...SL, color: '#B45309' }}>
            ⏳ Awaiting Premium Confirmation
            <span className="mono text-xs font-normal ml-2" style={{ color: '#A89585' }}>
              Stage 1 fired — watching for SHORT_BUILDUP
            </span>
          </h3>
          <div className="flex flex-wrap gap-3">
            {Object.entries(status.pending_spikes).flatMap(([sym, spikes]) =>
              spikes.map((sp, i) => (
                <div key={`${sym}-${i}`} className="card py-2 px-3"
                  style={{ background: 'rgba(217,119,6,0.06)', borderColor: 'rgba(217,119,6,0.20)' }}>
                  <div className="mono text-xs font-semibold" style={{ color: '#B45309' }}>
                    {sym} · {sp.strike} {sp.option_type}
                  </div>
                  <div className="mono text-xs mt-0.5" style={{ color: '#7A6355' }}>
                    OI +{sp.oi_pct}%
                    <span style={{ color: '#A89585' }}> · Poll </span>
                    {sp.polls_waited}/{status.config?.premium_confirm_polls}
                  </div>
                  {/* NEW: show LTP at spike so you can watch it move */}
                  <div className="mono text-xs mt-0.5" style={{ color: '#A89585' }}>
                    LTP at spike: ₹{sp.ltp_at_spike?.toFixed(2)}
                  </div>
                </div>
              ))
            )}
          </div>
        </div>
      )}

      {/* ── 3. Live chain snapshot table ──────────────────────────────────── */}
      {isRunning && Object.keys(snapshot).length > 0 && (
        <div className="card overflow-x-auto">
          <h3 className="mb-4" style={SL}>
            Live Monitoring — Chain Snapshot
            <span className="mono text-xs font-normal ml-2" style={{ color: '#A89585' }}>
              ATM ± {status?.config?.strikes_either_side} strikes
            </span>
          </h3>
          {Object.entries(snapshot).map(([sym, snap]) => {
            // snap can be either:
            //   new engine: { spot, expiry_date, atm_strikes, rows, updated_at }
            //   original engine: flat array of row objects
            const isArray   = Array.isArray(snap)
            const rows      = isArray ? snap : (snap.rows || [])
            const spot      = isArray ? rows[0]?.strike && null : snap.spot
            const expiryDate= isArray ? rows[0]?.expiry_date : snap.expiry_date
            const updatedAt = isArray ? rows[0]?.timestamp  : snap.updated_at
            const atmStrikes= isArray ? null : snap.atm_strikes

            // Derive spot from context when running original engine (not in chain rows)
            // Just show all rows sorted — no ATM filter when atm_strikes absent
            const sortedRows = [...rows].sort(
              (a, b) => a.strike - b.strike || a.option_type.localeCompare(b.option_type)
            )

            // ATM strike = middle of the sorted unique strikes
            const uniqueStrikes = [...new Set(rows.map(r => r.strike))].sort((a, b) => a - b)
            const midStrike = atmStrikes
              ? atmStrikes[Math.floor(atmStrikes.length / 2)]
              : uniqueStrikes[Math.floor(uniqueStrikes.length / 2)]

            return (
            <div key={sym} className="mb-6 last:mb-0">
              {/* Per-symbol sub-header */}
              <div className="flex items-center gap-3 mb-2">
                <span className="mono font-semibold text-sm" style={{ color: '#2C1810' }}>{sym}</span>
                <span className="mono text-xs" style={{ color: '#A89585' }}>
                  {expiryDate && <>Expiry {expiryDate}&nbsp;·&nbsp;</>}
                  {updatedAt && <>{updatedAt.slice(11, 19)} IST</>}
                </span>
              </div>
              <table className="w-full text-xs mono">
                <thead>
                  <tr style={{ borderBottom: '1px solid #E8DDD0' }}>
                    {['Strike', 'Type', 'OI', 'OI Δ vs Prev', 'OI% vs Settle', 'LTP', 'Volume', 'ATM?'].map(h => (
                      <th key={h} className="text-left pb-2 pr-5 font-medium"
                        style={{ color: '#A89585' }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {sortedRows.map((r, i) => {
                      const atmHighlight = Number(r.strike) === Number(midStrike)
                      return (
                        <tr key={i}
                          style={{
                            borderBottom: '1px solid rgba(232,221,208,0.5)',
                            background: atmHighlight ? 'rgba(200,134,10,0.04)' : 'transparent',
                          }}>
                          <td className="py-1.5 pr-5 font-semibold"
                            style={{ color: atmHighlight ? '#C8860A' : '#2C1810' }}>
                            {r.strike}
                          </td>
                          <td className="py-1.5 pr-5 font-medium"
                            style={{ color: r.option_type === 'CE' ? '#0D9488' : '#DC2626' }}>
                            {r.option_type}
                          </td>
                          <td className="py-1.5 pr-5" style={{ color: '#2C1810' }}>
                            {fmt(r.oi)}
                          </td>
                          <td className="py-1.5 pr-5"
                            style={{ color: (r.oi_change ?? 0) >= 0 ? '#0D9488' : '#DC2626' }}>
                            {(r.oi_change ?? 0) >= 0 ? '+' : ''}{fmt(r.oi_change)}
                          </td>
                          <td className="py-1.5 pr-5" style={{ color: '#7A6355' }}>
                            {r.oi_change_pct != null
                              ? `${r.oi_change_pct >= 0 ? '+' : ''}${r.oi_change_pct.toFixed(1)}%`
                              : '—'}
                          </td>
                          <td className="py-1.5 pr-5" style={{ color: '#2C1810' }}>
                            ₹{r.ltp?.toFixed(2) ?? '—'}
                          </td>
                          <td className="py-1.5 pr-5" style={{ color: '#7A6355' }}>
                            {fmt(r.volume)}
                          </td>
                          <td className="py-1.5 pr-5" style={{ color: '#C8860A', fontWeight: 600 }}>
                            {atmHighlight ? '◆ ATM' : ''}
                          </td>
                        </tr>
                      )
                    })}
                </tbody>
              </table>
            </div>
            )
          })}
        </div>
      )}

      {/* ── 4. Control panel (shown when stopped) ─────────────────────────── */}
      {!isRunning && (
        <div className="grid grid-cols-2 gap-5">

          {/* Symbol checkboxes */}
          <div className="card">
            <h3 className="mb-4" style={SL}>Symbols to Monitor</h3>
            <div className="grid grid-cols-2 gap-y-3 gap-x-4">
              {ALL_SYMBOLS.map(sym => (
                <label key={sym}
                  className="flex items-center gap-2 cursor-pointer"
                  style={{ color: selectedSymbols.includes(sym) ? '#2C1810' : '#A89585' }}>
                  <input
                    type="checkbox"
                    checked={selectedSymbols.includes(sym)}
                    onChange={e => setSelectedSymbols(prev =>
                      e.target.checked ? [...prev, sym] : prev.filter(s => s !== sym)
                    )}
                    style={{ accentColor: '#C8860A', width: 14, height: 14 }}
                  />
                  <span className="mono text-sm font-medium">{sym}</span>
                </label>
              ))}
            </div>
            <p className="text-xs mono mt-4" style={{ color: '#A89585' }}>
              {selectedSymbols.length} symbol{selectedSymbols.length !== 1 ? 's' : ''} selected
            </p>
          </div>

          {/* Threshold sliders */}
          <div className="card">
            <h3 className="mb-4 flex items-center gap-2" style={SL}>
              Engine Thresholds
              <button
                onClick={() => setShowThresholdsInfo(true)}
                className="mono text-xs flex items-center justify-center transition-all"
                title="What do these thresholds mean?"
                style={{
                  width: 16, height: 16, borderRadius: '50%',
                  background: 'rgba(200,134,10,0.15)',
                  border: '1px solid rgba(200,134,10,0.35)',
                  color: '#C8860A', fontWeight: 700, fontSize: 10,
                  outline: 'none',
                }}
                onMouseEnter={e => {
                  e.target.style.background = 'rgba(200,134,10,0.25)'
                  e.target.style.color = '#B45309'
                }}
                onMouseLeave={e => {
                  e.target.style.background = 'rgba(200,134,10,0.15)'
                  e.target.style.color = '#C8860A'
                }}
              >
                ?
              </button>
            </h3>
            {[
              {
                label: 'OI Spike Threshold', value: spikeThreshold, min: 50, max: 2000, step: 50, unit: '%', setter: setSpikeThreshold,
                hint: 'OI % from session open to trigger Stage 1'
              },
              {
                label: 'Speed Window', value: speedWindow, min: 1, max: 15, step: 1, unit: 'min', setter: setSpeedWindow,
                hint: 'Rolling window for OI velocity'
              },
              {
                label: 'Confirm Polls', value: confirmPolls, min: 1, max: 10, step: 1, unit: '', setter: setConfirmPolls,
                hint: 'Polls to wait for premium confirmation'
              },
              {
                label: 'Min Volume', value: minVolume, min: 10, max: 5000, step: 10, unit: '', setter: setMinVolume,
                hint: 'Minimum volume for a strike to be monitored'
              },
              {
                label: 'Strikes Either Side', value: strikesEither, min: 1, max: 5, step: 1, unit: '', setter: setStrikesEither,
                hint: 'ATM ± n strikes to watch'
              },
              {
                label: 'Poll Interval', value: pollInterval, min: 3, max: 30, step: 1, unit: 's', setter: setPollInterval,
                hint: 'Fyers API call cadence'
              },
            ].map(s => (
              <div key={s.label} className="mb-3 last:mb-0">
                <div className="flex justify-between text-xs mono mb-1">
                  <span title={s.hint} style={{ color: '#7A6355', cursor: 'help' }}>
                    {s.label}
                  </span>
                  <span style={{ color: '#C8860A', fontWeight: 600 }}>
                    {s.value}{s.unit}
                  </span>
                </div>
                <input type="range" min={s.min} max={s.max} step={s.step}
                  value={s.value} onChange={e => s.setter(Number(e.target.value))}
                  className="w-full h-1.5" />
              </div>
            ))}

            {/* NEW: Adaptive threshold toggle — tucked below existing sliders */}
            <div className="pt-3 mt-1" style={{ borderTop: '1px solid #E8DDD0' }}>
              <div className="flex items-center gap-3 mb-2">
                <button
                  onClick={() => setUseAdaptive(v => !v)}
                  className="text-xs mono px-3 py-1.5 rounded transition-all"
                  style={{
                    background: useAdaptive ? 'rgba(13,148,136,0.10)' : 'transparent',
                    color:       useAdaptive ? '#0D9488' : '#A89585',
                    border:      useAdaptive ? '1px solid rgba(13,148,136,0.3)' : '1px solid #E8DDD0',
                    cursor: 'pointer',
                  }}>
                  {useAdaptive ? '◆ Adaptive threshold ON' : '◇ Adaptive threshold OFF'}
                </button>
              </div>
              {useAdaptive ? (
                <div className="flex flex-col gap-2">
                  <p className="text-xs mono" style={{ color: '#A89585' }}>
                    Uses Nth percentile of historical OI spikes per symbol. Falls back to {spikeThreshold}% until 50+ samples collected.
                  </p>
                  <div className="flex items-center gap-2">
                    <label className="text-xs mono" style={{ color: '#A89585' }}>Percentile:</label>
                    <input
                      type="number" min="70" max="99" step="1"
                      value={adaptivePct}
                      onChange={e => setAdaptivePct(Number(e.target.value))}
                      className="w-16 text-xs mono px-2 py-1 rounded"
                      style={inputStyle}
                      onFocus={focusGold} onBlur={blurSand}
                    />
                    <span className="text-xs mono" style={{ color: '#A89585' }}>(Recommend 85–95)</span>
                  </div>
                </div>
              ) : (
                <p className="text-xs mono" style={{ color: '#A89585' }}>
                  Static flat threshold. Enable adaptive for symbol-calibrated detection.
                </p>
              )}
            </div>
          </div>
        </div>
      )}

      {/* ── 5. Alert log ──────────────────────────────────────────────────── */}
      <div className="card overflow-x-auto">
        <h3 className="mb-4 flex items-center gap-3" style={SL}>
          Alert Log — Today
          {alerts.length > 0 && (
            <span className="mono text-xs px-2 py-0.5 rounded font-normal"
              style={{ background: 'rgba(200,134,10,0.12)', color: '#C8860A' }}>
              {alerts.length} confirmed signal{alerts.length !== 1 ? 's' : ''}
            </span>
          )}
        </h3>

        {alerts.length === 0 ? (
          <div className="py-10 text-center mono text-sm" style={{ color: '#A89585' }}>
            {isRunning
              ? 'Engine running — no confirmed alerts yet this session.'
              : 'Start the engine to begin monitoring.'}
          </div>
        ) : (
          <table className="w-full text-xs mono">
            <thead>
              <tr style={{ borderBottom: '1px solid #E8DDD0' }}>
                {['Time', 'Symbol', 'Strike', 'Type', 'Signal', 'Trade', 'OI Δ%', 'Speed', 'Confidence', ''].map(h => (
                  <th key={h} className="text-left pb-2 pr-4 font-medium"
                    style={{ color: '#A89585' }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {alerts.map((a, i) => {
                const isBullish   = a.signal_direction === 'BULLISH'
                const signalColor = isBullish ? '#0D9488' : '#DC2626'
                return (
                  <tr key={a.id ?? i}
                    className="transition-colors"
                    style={{ borderBottom: '1px solid rgba(232,221,208,0.5)' }}>
                    <td className="py-2 pr-4" style={{ color: '#7A6355' }}>
                      {a.triggered_at?.slice(11, 19)}
                    </td>
                    <td className="py-2 pr-4 font-semibold" style={{ color: '#2C1810' }}>{a.symbol}</td>
                    <td className="py-2 pr-4 font-semibold" style={{ color: '#2C1810' }}>{a.strike}</td>
                    <td className="py-2 pr-4"
                      style={{ color: a.option_type === 'CE' ? '#0D9488' : '#DC2626' }}>
                      {a.option_type}
                    </td>
                    <td className="py-2 pr-4 font-semibold" style={{ color: signalColor }}>
                      {isBullish ? '▲' : '▼'} {a.signal_direction}
                    </td>
                    <td className="py-2 pr-4 font-semibold" style={{ color: signalColor }}>
                      BUY {a.trade_strike} {a.trade_option}
                    </td>
                    <td className="py-2 pr-4" style={{ color: '#C8860A' }}>
                      +{a.oi_pct_change}%
                    </td>
                    <td className="py-2 pr-4" style={{ color: '#7A6355' }}>
                      {a.oi_speed_pct_pm?.toFixed(1)}%/m
                    </td>
                    <td className="py-2 pr-4">
                      <ConfidenceBadge level={a.confidence} />
                    </td>
                    <td className="py-2">
                      <button
                        onClick={() => handleSuppress(a.id)}
                        className="text-xs mono px-2 py-0.5 rounded transition-all"
                        style={{ color: '#A89585', border: '1px solid #E8DDD0' }}
                        onMouseEnter={e => {
                          e.target.style.color = '#DC2626'
                          e.target.style.borderColor = 'rgba(220,38,38,0.3)'
                        }}
                        onMouseLeave={e => {
                          e.target.style.color = '#A89585'
                          e.target.style.borderColor = '#E8DDD0'
                        }}
                      >
                        suppress
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>

      <InfoPanel title="Understanding the Alert Engine — Thresholds, Workflow & Signal Interpretation">
        <Section title="The Two-Stage Detection Workflow">
          <P>The engine follows a strict two-stage process to ensure only high-quality, confirmed signals generate alerts. A single threshold breach is not enough — the market must confirm the direction before an alert fires.</P>
          <Callout type="signal">Stage 1 — Spike Detection: The engine monitors the OI % change vs yesterday's closing OI (called oichp — Fyers calculates this directly as ((today OI − yesterday OI) ÷ yesterday OI) × 100). When this exceeds the OI Spike Threshold at any ATM ± n strike, Stage 1 fires. The strike enters the "Awaiting Confirmation" window. Crucially, this metric is start-time independent — it doesn't matter when you launched the app, the reference point is always yesterday's settlement.</Callout>
          <Callout type="warning">Stage 2 — Premium Confirmation: Over the next N polls (Confirm Polls setting), the engine watches two things simultaneously: (1) Is the option's premium (LTP) dropping while OI is still growing from the spike point? (2) Is the direction consistent with institutional writing (SHORT_BUILDUP)? Only when both conditions are met within the confirmation window does the engine fire an alert.</Callout>
        </Section>

        <Section title="Threshold Settings">
          <KV label="OI Spike Threshold (default 500%)" value="Stage 1 trigger. The engine fires only when a strike's OI has grown by this % vs yesterday's closing settlement. At 500%, you need a 6× increase in OI to trigger. Lower for more sensitivity (more signals, more noise). Raise heavily for expiry week." />
          <KV label="Speed Window (default 5 min)" value="Rolling mathematical window (in minutes) for calculating OI velocity. A 5-minute window smooths out tick-level noise and identifies sustained institutional accumulation. A shorter window reacts to violent, instantaneous betting bursts." />
          <KV label="Confirm Polls (default 4)" value="The time duration the engine spends in Stage 2 (Awaiting Premium Confirmation). E.g., 4 polls × 10s = 40 seconds. The engine demands proof that the option's LTP is dropping WHILE OI is rising before firing the alert." />
          <KV label="Min Volume (default 100)" value="A hard liquidity gate. If a strike has zero initial OI and low volume, a single retail order of 10 lots could trigger a 10,000% spike. This filter drops those illiquid statistical anomalies before they hit Stage 1." />
          <KV label="Strikes Either Side (default 1)" value="The surveillance net around the current Spot price. Set to 1, the engine tracks the exact ATM strike + 1 ITM + 1 OTM. In violently trending markets, increasing this ensures you don't miss buildup happening slightly away from Spot. At 1, the engine watches 3 strikes (ATM-1, ATM, ATM+1). Raise to 2 in trending markets where institutional positioning sits 1-2 strikes away from spot in the trend direction. Keep at 1 for clean, focused signals in ranging/choppy markets." />
          <KV label="Poll Interval (default 10s)" value="Fyers API call cadence. Lower = more real-time, higher API load. Raise to 15-20s if you're hitting Fyers rate limits or experiencing connection issues. A full Stage 1 → Stage 2 cycle at default settings takes ~50 seconds (10s × (1 spike poll + 4 confirm polls))." />
        </Section>

        <Section title="Adaptive Threshold (New)">
          <P>When enabled, the OI Spike Threshold is replaced by the Nth percentile of historical oichp values for that symbol, computed from the rolling oi_snapshots table. Instead of asking "did OI grow more than 500%?", it asks "did OI grow more than it does 90% of the time for this symbol?" — making Stage 1 a genuine outlier detector calibrated to each symbol's own behaviour.</P>
          <Callout type="info">Adaptive mode requires 50+ historical oichp samples before activating. Until then it falls back to your static threshold silently. Samples accumulate as the engine runs across sessions. Enable after the first few days of operation for best results.</Callout>
        </Section>

        <Section title="Reading the Alert Log">
          <P><strong>Signal Direction (▲ BULLISH / ▼ BEARISH):</strong> The engine's conclusion about the underlying's direction based on which side (CE or PE) had the SHORT_BUILDUP. BULLISH means PUT writers are entering — they're selling puts = they expect no downside. BEARISH means CALL writers are entering — they're selling calls = they expect no upside.</P>
          <P><strong>Trade:</strong> The suggested trade entry — the opposite side from where the writing occurred. If 23500 PE SHORT_BUILDUP → BUY 23500 CE. This is the most direct translation of the signal: if institutions are confidently writing puts at 23500, buy calls at the same strike to participate in the expected upside.</P>
          <P><strong>OI Δ%:</strong> The oichp value that triggered Stage 1 — how much OI grew vs yesterday's settlement at this strike. Higher = stronger conviction in the positioning.</P>
          <P><strong>Speed:</strong> OI velocity in %/min at the time of Stage 1 detection. High speed (50%+/min) = aggressive, fast institutional entry. Low speed (5-10%/min) = gradual accumulation, less urgency.</P>
          <Callout type="warning">Confidence badge interpretation: HIGH (3+ points) means strong OI spike + fast speed + high volume — all three confirming factors aligned. MEDIUM means 1-2 confirming factors. LOW means the signal met the minimum threshold but lacks breadth of confirmation. Weight HIGH signals more heavily, but don't ignore MEDIUM signals when other context (GEX, skew, market structure) aligns.</Callout>
        </Section>

        <Section title="Suppression Filters — Why Signals Disappear">
          <P>Not every Stage 1 spike becomes a confirmed alert. Two automatic suppression filters prevent low-quality signals:</P>
          <KV label="PREMIUM_NOT_CONFIRMED" value="OI spiked but premium didn't drop within the confirmation window. The OI spike may have been batch-reporting from exchange rather than real-time institutional entry, OR the market moved against the writers before they could establish their position. Either way, the directional signal was not clean." />
          <KV label="DUAL_SIDE_WRITING" value="Both the CE and PE at the same strike had oichp ≥ 200% simultaneously. This is event hedging — institutions are buying a straddle/strangle, not taking a directional bet. Both sides writing aggressively = they expect a big move but don't know which direction. Not actionable for directional trading." />
        </Section>
      </InfoPanel>

      {/* ── Standalone Modal for Engine Thresholds ─────────────────────────── */}
      {showThresholdsInfo && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center p-4 fade-up"
          style={{ background: 'rgba(44, 24, 16, 0.6)', backdropFilter: 'blur(3px)' }}
          onClick={() => setShowThresholdsInfo(false)}
        >
          <div
            className="card relative overflow-y-auto"
            style={{
              width: '90%', maxWidth: 640, maxHeight: '85vh',
              background: '#FBF7F0',
              border: '1px solid #E8DDD0',
              boxShadow: '0 20px 40px rgba(44, 24, 16, 0.25)',
              padding: 0,
            }}
            onClick={e => e.stopPropagation()}
          >
            {/* Modal Header */}
            <div
              className="px-6 py-4 flex items-center justify-between"
              style={{ borderBottom: '1px solid #E8DDD0', background: 'rgba(251,247,240,0.95)', position: 'sticky', top: 0, zIndex: 10 }}
            >
              <h2 className="font-display font-semibold" style={{ color: '#2C1810', fontSize: 16 }}>
                Understanding the 6 Engine Thresholds
              </h2>
              <button
                onClick={() => setShowThresholdsInfo(false)}
                className="text-xl leading-none"
                style={{ color: '#A89585', outline: 'none' }}
                onMouseEnter={e => e.target.style.color = '#DC2626'}
                onMouseLeave={e => e.target.style.color = '#A89585'}
              >
                &times;
              </button>
            </div>

            {/* Modal Body */}
            <div className="px-6 py-5">
              <P>
                The Alert Engine detects institutional option writing by analyzing Open Interest (OI) spikes and correlating them with premium price action.
                These thresholds govern the sensitivity of the two-stage detection logic.
              </P>

              <div className="mt-6 mb-2">
                <KV label="OI Spike Threshold" value="Stage 1 trigger. The engine fires only when a strike's OI has grown by this % vs yesterday's closing settlement. At 500%, you need a 6× increase in OI to trigger. Lower for more sensitivity (more signals, more noise). Raise heavily for expiry week." />
              </div>
              <div className="mb-2">
                <KV label="Speed Window" value="Rolling mathematical window (in minutes) for calculating OI velocity. A 5-minute window smooths out tick-level noise and identifies sustained institutional accumulation. A shorter window reacts to violent, instantaneous betting bursts." />
              </div>
              <div className="mb-2">
                <KV label="Confirm Polls" value="The time duration the engine spends in Stage 2 (Awaiting Premium Confirmation). E.g., 4 polls × 10s = 40 seconds. The engine demands proof that the option's LTP is dropping WHILE OI is rising before firing the alert." />
              </div>
              <div className="mb-2">
                <KV label="Min Volume" value="A hard liquidity gate. If a strike has zero initial OI and low volume, a single retail order of 10 lots could trigger a 10,000% spike. This filter drops those illiquid statistical anomalies before they hit Stage 1." />
              </div>
              <div className="mb-2">
                <KV label="Strikes Either Side" value="The surveillance net around the current Spot price. Set to 1, the engine tracks the exact ATM strike + 1 ITM + 1 OTM. In violently trending markets, increasing this ensures you don't miss buildup happening slightly away from Spot." />
              </div>
              <div className="mb-2">
                <KV label="Poll Interval" value="Fyers API call cadence. Governs how frequently the backend polls fresh snapshots. Faster polling allows quicker reaction times but risks hitting broker API rate limits." />
              </div>

              <div className="mt-6">
                <Callout type="warning">
                  Remember: Decreasing thresholds creates more sensitivity and faster alerts, but greatly increases the chance of fakeouts and noise. Start with the default baseline, track the hit-rate on a quiet day vs a trending day, and adjust accordingly.
                </Callout>
              </div>
            </div>

            {/* Modal Footer */}
            <div className="px-6 py-4 flex justify-end"
              style={{ borderTop: '1px solid #E8DDD0', background: 'rgba(232,221,208,0.2)' }}>
              <button
                onClick={() => setShowThresholdsInfo(false)}
                className="px-4 py-1.5 rounded-lg text-sm font-semibold transition-all"
                style={{ background: '#2C1810', color: '#FBF7F0' }}
                onMouseEnter={e => e.target.style.background = '#3D2418'}
                onMouseLeave={e => e.target.style.background = '#2C1810'}
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
