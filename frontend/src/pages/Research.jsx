// Research — signal backtesting and data coverage.
//
// Design intent: this page must make it hard to fool yourself. Data readiness
// is shown before anything else, the null-comparison column sits beside every
// result, and verdicts are rendered as words (NO_EDGE, INSUFFICIENT_DATA)
// rather than as a number the eye can talk itself into.

import { useCallback, useEffect, useState } from 'react'
import { useApp } from '../context/AppContext'
import client from '../api/client'
import LoadingSpinner from '../components/LoadingSpinner'
import ErrorBanner from '../components/ErrorBanner'

const CARD = {
  background: '#FDFAF6',
  border: '1px solid #EDE3D8',
  borderRadius: 6,
}

const VERDICT_STYLE = {
  EDGE:              { color: '#0D9488', label: 'Edge vs null' },
  INVERSE_EDGE:      { color: '#DC2626', label: 'Inverse edge' },
  NO_EDGE:           { color: '#A89585', label: 'No edge' },
  INSUFFICIENT_DATA: { color: '#D97706', label: 'Insufficient data' },
  INDETERMINATE:     { color: '#A89585', label: 'Indeterminate' },
}

function Stat({ label, value, hint }) {
  return (
    <div title={hint}>
      <div className="text-xs mono" style={{ color: '#A89585' }}>{label}</div>
      <div className="text-lg mono font-semibold" style={{ color: '#7A6355' }}>{value}</div>
    </div>
  )
}

function ReadinessBanner({ readiness }) {
  if (!readiness) return null
  const ok = readiness.statistically_meaningful
  const color = ok ? '#0D9488' : '#D97706'

  return (
    <div className="p-4 mb-4" style={{ ...CARD, borderLeft: `3px solid ${color}` }}>
      <div className="flex items-center gap-6 flex-wrap">
        <Stat label="SESSIONS RECORDED" value={readiness.sessions_recorded} />
        <Stat label="FIRST" value={readiness.first_session || '—'} />
        <Stat label="LAST" value={readiness.last_session || '—'} />
        <Stat
          label="WALK-FORWARD SPLITS"
          value={readiness.walk_forward?.possible_splits ?? 0}
          hint="Out-of-sample train/test cycles available at the current window sizes"
        />
      </div>
      <p className="text-xs mt-3" style={{ color: ok ? '#7A6355' : '#D97706' }}>
        {readiness.guidance}
      </p>
    </div>
  )
}

function HorizonTable({ horizons }) {
  const rows = Object.entries(horizons || {})
  if (!rows.length) return null

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs mono" style={{ borderCollapse: 'collapse' }}>
        <thead>
          <tr style={{ color: '#A89585', textAlign: 'right' }}>
            <th className="py-2 px-3" style={{ textAlign: 'left' }}>HORIZON</th>
            <th className="py-2 px-3">N</th>
            <th className="py-2 px-3">HIT %</th>
            <th className="py-2 px-3">MEAN bps</th>
            <th className="py-2 px-3">NULL bps</th>
            <th className="py-2 px-3">EDGE bps</th>
            <th className="py-2 px-3" title="Welch t on the signal-minus-null difference">t</th>
            <th className="py-2 px-3">MAE</th>
            <th className="py-2 px-3">MFE</th>
            <th className="py-2 px-3" style={{ textAlign: 'left' }}>VERDICT</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([h, d]) => {
            const v = VERDICT_STYLE[d.verdict] || VERDICT_STYLE.INDETERMINATE
            const num = (x, dp = 2) =>
              x === null || x === undefined ? '—' : Number(x).toFixed(dp)
            return (
              <tr key={h} style={{ borderTop: '1px solid #EDE3D8', textAlign: 'right' }}>
                <td className="py-2 px-3 font-semibold"
                    style={{ textAlign: 'left', color: '#7A6355' }}>{h}</td>
                <td className="py-2 px-3" style={{ color: '#7A6355' }}>{d.signal?.n ?? 0}</td>
                <td className="py-2 px-3" style={{ color: '#7A6355' }}>
                  {num(d.signal?.hit_rate_pct, 1)}
                </td>
                <td className="py-2 px-3" style={{ color: '#7A6355' }}>
                  {num(d.signal?.mean_bps)}
                </td>
                <td className="py-2 px-3" style={{ color: '#A89585' }}>
                  {num(d.null?.mean_bps)}
                </td>
                <td className="py-2 px-3 font-semibold"
                    style={{ color: d.edge_bps > 0 ? '#0D9488' : '#DC2626' }}>
                  {num(d.edge_bps)}
                </td>
                <td className="py-2 px-3" style={{ color: '#7A6355' }}>
                  {num(d.edge_t_stat, 2)}
                </td>
                <td className="py-2 px-3" style={{ color: '#DC2626' }}>
                  {num(d.signal?.mean_mae_bps, 1)}
                </td>
                <td className="py-2 px-3" style={{ color: '#0D9488' }}>
                  {num(d.signal?.mean_mfe_bps, 1)}
                </td>
                <td className="py-2 px-3" style={{ textAlign: 'left', color: v.color }}>
                  {v.label}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export default function Research() {
  const { symbol } = useApp()

  const [signals,   setSignals]   = useState([])
  const [selected,  setSelected]  = useState('')
  const [readiness, setReadiness] = useState(null)
  const [result,    setResult]    = useState(null)
  const [loading,   setLoading]   = useState(false)
  const [error,     setError]     = useState(null)

  useEffect(() => {
    client.get('/api/backtest/signals')
      .then(res => {
        const list = res.data.signals || []
        setSignals(list)
        if (list.length && !selected) setSelected(list[0].signal_id)
      })
      .catch(e => setError(e.response?.data?.detail || 'Failed to load signals'))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (!symbol) return
    client.get('/api/backtest/data-readiness', { params: { symbol } })
      .then(res => setReadiness(res.data))
      .catch(() => setReadiness(null))
  }, [symbol])

  const runBacktest = useCallback(async () => {
    if (!selected) return
    setLoading(true); setError(null); setResult(null)
    try {
      const res = await client.post('/api/backtest/run', {
        signal_id: selected,
        symbol,
      })
      setResult(res.data)
    } catch (e) {
      setError(e.response?.data?.detail || e.message || 'Backtest failed')
    } finally {
      setLoading(false)
    }
  }, [selected, symbol])

  const activeSignal = signals.find(s => s.signal_id === selected)

  return (
    <div className="max-w-6xl">
      <h1 className="text-lg font-display font-semibold mb-1" style={{ color: '#7A6355' }}>
        Research
      </h1>
      <p className="text-xs mb-4" style={{ color: '#A89585' }}>
        Replays recorded chains through the same signal code that runs live, then
        compares against random entries taken at the same times of day.
      </p>

      <ReadinessBanner readiness={readiness} />

      <div className="p-4 mb-4" style={CARD}>
        <div className="flex items-end gap-3 flex-wrap">
          <div>
            <label className="text-xs mono block mb-1" style={{ color: '#A89585' }}>
              SIGNAL
            </label>
            <select
              value={selected}
              onChange={e => setSelected(e.target.value)}
              className="text-xs mono rounded px-2 py-1.5 focus:outline-none"
              style={{ background: '#FBF7F0', border: '1px solid #EDE3D8', color: '#7A6355' }}
            >
              {signals.map(s => (
                <option key={s.key} value={s.signal_id}>
                  {s.signal_id} (v{s.version})
                </option>
              ))}
            </select>
          </div>

          <button
            onClick={runBacktest}
            disabled={loading || !selected}
            className="text-xs mono rounded px-4 py-1.5 transition-opacity"
            style={{
              background: '#0D9488', color: '#fff',
              opacity: loading || !selected ? 0.5 : 1,
            }}
          >
            {loading ? 'Running…' : 'Run backtest'}
          </button>

          <span className="text-xs" style={{ color: '#A89585' }}>{symbol}</span>
        </div>

        {activeSignal && (
          <p className="text-xs mt-3" style={{ color: '#A89585' }}>
            {activeSignal.description}
          </p>
        )}
      </div>

      {error && <ErrorBanner message={error} />}
      {loading && <LoadingSpinner />}

      {result && (
        <div className="p-4" style={CARD}>
          <div className="flex items-center gap-8 flex-wrap mb-4">
            <Stat label="SESSIONS" value={result.sessions} />
            <Stat label="EVALUATIONS" value={result.evaluations?.toLocaleString('en-IN')} />
            <Stat label="FIRED" value={result.signals_fired?.toLocaleString('en-IN')} />
            <Stat label="FIRE RATE" value={`${result.fire_rate_pct}%`} />
            <Stat label="RUN" value={result.run_id}
                  hint="Reproduce this exact run with the same signal version, params and seed" />
          </div>

          <HorizonTable horizons={result.horizons} />

          {result.notes?.length > 0 && (
            <ul className="mt-4 space-y-1">
              {result.notes.map((n, i) => (
                <li key={i} className="text-xs" style={{ color: '#D97706' }}>• {n}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
