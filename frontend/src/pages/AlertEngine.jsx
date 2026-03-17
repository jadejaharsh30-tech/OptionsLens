// optionslens/frontend/src/pages/AlertEngine.jsx
// Four sections:
//   1. Status header      — running indicator, active symbols, alert count, last poll
//   2. Control panel      — symbol checkboxes + threshold sliders (shown when stopped)
//   3. Chain snapshot     — live monitored strikes table (shown when running)
//   4. Alert log          — today's confirmed alerts with suppress button

import { useState, useEffect, useCallback, useRef } from 'react'
import { useApp } from '../context/AppContext'
import client from '../api/client'
import ErrorBanner from '../components/ErrorBanner'
import LoadingSpinner from '../components/LoadingSpinner'

const ALL_SYMBOLS = ['NIFTY','BANKNIFTY','RELIANCE','TCS','HDFCBANK','INFY','ICICIBANK']

const fmt = n => {
  if (n == null) return '—'
  const abs = Math.abs(n)
  if (abs >= 1e6) return `${(n/1e6).toFixed(1)}M`
  if (abs >= 1e3) return `${(n/1e3).toFixed(0)}K`
  return String(n)
}

// ── Status indicator ──────────────────────────────────────────────────────────
function StatusDot({ status }) {
  const map = {
    ok:            { color: '#0D9488', label: 'Live' },
    market_closed: { color: '#D97706', label: 'Market Closed' },
    error:         { color: '#DC2626', label: 'Error' },
    starting:      { color: '#C8860A', label: 'Starting…' },
    idle:          { color: '#A89585', label: 'Idle' },
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
  const [selectedSymbols, setSelectedSymbols] = useState(['NIFTY','BANKNIFTY'])
  const [pollInterval,    setPollInterval]    = useState(10)
  const [spikeThreshold,  setSpikeThreshold]  = useState(500)
  const [speedWindow,     setSpeedWindow]     = useState(5)
  const [confirmPolls,    setConfirmPolls]    = useState(4)
  const [minVolume,       setMinVolume]       = useState(100)
  const [strikesEither,   setStrikesEither]   = useState(1)

  // ── Runtime state ─────────────────────────────────────────────────────────
  const [status,   setStatus]   = useState(null)
  const [alerts,   setAlerts]   = useState([])
  const [snapshot, setSnapshot] = useState({})
  const [startErr, setStartErr] = useState(null)
  const [starting, setStarting] = useState(false)

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

  const isRunning       = status?.running === true
  const totalPending    = Object.values(status?.pending_spikes || {})
                            .reduce((s, arr) => s + arr.length, 0)
  const inputStyle      = { background: '#FBF7F0', border: '1px solid #E8DDD0', color: '#2C1810' }
  const focusGold       = e => { e.target.style.borderColor = '#C8860A' }
  const blurSand        = e => { e.target.style.borderColor = '#E8DDD0' }

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
            <div className="text-xs mb-0.5" style={{ color: '#A89585' }}>Last Poll</div>
            <div className="mono text-sm" style={{ color: '#2C1810' }}>
              {status.last_poll_at
                ? new Date(status.last_poll_at).toLocaleTimeString('en-IN',
                    { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', second: '2-digit' })
                : '—'}
            </div>
          </div>
          <div>
            <div className="text-xs mb-0.5" style={{ color: '#A89585' }}>Pending Confirms</div>
            <div className="mono text-sm font-semibold" style={{ color: totalPending > 0 ? '#D97706' : '#A89585' }}>
              {totalPending}
            </div>
          </div>
          {status?.config && (
            <div>
              <div className="text-xs mb-0.5" style={{ color: '#A89585' }}>OI Threshold</div>
              <div className="mono text-sm" style={{ color: '#7A6355' }}>
                {status.config.oi_spike_threshold_pct}%
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

      {/* ── 2. Control panel (shown when stopped) ─────────────────────────── */}
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
            <h3 className="mb-4" style={SL}>Engine Thresholds</h3>
            {[
              { label: 'OI Spike Threshold', value: spikeThreshold, min: 50,  max: 2000, step: 50,  unit: '%',   setter: setSpikeThreshold,
                hint: 'OI % from session open to trigger Stage 1' },
              { label: 'Speed Window',        value: speedWindow,    min: 1,   max: 15,   step: 1,   unit: 'min', setter: setSpeedWindow,
                hint: 'Rolling window for OI velocity' },
              { label: 'Confirm Polls',       value: confirmPolls,   min: 1,   max: 10,   step: 1,   unit: '',    setter: setConfirmPolls,
                hint: 'Polls to wait for premium confirmation' },
              { label: 'Min Volume',          value: minVolume,      min: 10,  max: 5000, step: 10,  unit: '',    setter: setMinVolume,
                hint: 'Minimum volume for a strike to be monitored' },
              { label: 'Strikes Either Side', value: strikesEither,  min: 1,   max: 5,    step: 1,   unit: '',    setter: setStrikesEither,
                hint: 'ATM ± n strikes to watch' },
              { label: 'Poll Interval',       value: pollInterval,   min: 3,   max: 30,   step: 1,   unit: 's',   setter: setPollInterval,
                hint: 'Fyers API call cadence' },
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
          </div>
        </div>
      )}

      {/* ── 3. Pending confirmation spikes ────────────────────────────────── */}
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
                </div>
              ))
            )}
          </div>
        </div>
      )}

      {/* ── 4. Live chain snapshot table ──────────────────────────────────── */}
      {isRunning && Object.keys(snapshot).length > 0 && (
        <div className="card overflow-x-auto">
          <h3 className="mb-4" style={SL}>
            Live Monitoring — Chain Snapshot
            <span className="mono text-xs font-normal ml-2" style={{ color: '#A89585' }}>
              ATM ± {status?.config?.strikes_either_side} strikes
            </span>
          </h3>
          {Object.entries(snapshot).map(([sym, snap]) => (
            <div key={sym} className="mb-6 last:mb-0">
              {/* Per-symbol sub-header */}
              <div className="flex items-center gap-3 mb-2">
                <span className="mono font-semibold text-sm" style={{ color: '#2C1810' }}>{sym}</span>
                <span className="mono text-xs" style={{ color: '#A89585' }}>
                  Spot ₹{snap.spot?.toLocaleString('en-IN', { maximumFractionDigits: 2 })}
                  &nbsp;·&nbsp; Expiry {snap.expiry_date}
                  &nbsp;·&nbsp; {snap.updated_at?.slice(11, 19)} IST
                </span>
              </div>
              <table className="w-full text-xs mono">
                <thead>
                  <tr style={{ borderBottom: '1px solid #E8DDD0' }}>
                    {['Strike','Type','OI','OI Δ vs Prev','OI% vs Settle','LTP','Volume','ATM?'].map(h => (
                      <th key={h} className="text-left pb-2 pr-5 font-medium"
                          style={{ color: '#A89585' }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {(snap.rows || [])
                    .filter(r => snap.atm_strikes?.includes(r.strike))
                    .sort((a, b) => a.strike - b.strike || a.option_type.localeCompare(b.option_type))
                    .map((r, i) => {
                      const isAtm = r.strike === Math.min(...(snap.atm_strikes || []),
                        ...snap.atm_strikes.map(s => Math.abs(s - snap.spot)))
                        || snap.atm_strikes?.includes(r.strike)
                      const atmHighlight = snap.atm_strikes?.length > 0 &&
                        r.strike === snap.atm_strikes[Math.floor(snap.atm_strikes.length / 2)]
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
          ))}
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
                {['Time','Symbol','Strike','Type','Signal','Trade','OI Δ%','Speed','Confidence',''].map(h => (
                  <th key={h} className="text-left pb-2 pr-4 font-medium"
                      style={{ color: '#A89585' }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {alerts.map((a, i) => {
                const isBullish = a.signal_direction === 'BULLISH'
                const signalColor = isBullish ? '#0D9488' : '#DC2626'
                return (
                  <tr key={a.id ?? i}
                      style={{ borderBottom: '1px solid rgba(232,221,208,0.5)' }}
                      onMouseEnter={e => e.currentTarget.style.background = 'rgba(200,134,10,0.03)'}
                      onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>
                    <td className="py-2 pr-4" style={{ color: '#7A6355' }}>
                      {a.triggered_at?.slice(11, 19)}
                    </td>
                    <td className="py-2 pr-4 font-semibold" style={{ color: '#2C1810' }}>
                      {a.symbol}
                    </td>
                    <td className="py-2 pr-4 font-semibold" style={{ color: '#2C1810' }}>
                      {a.strike}
                    </td>
                    <td className="py-2 pr-4 font-medium"
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

    </div>
  )
}
