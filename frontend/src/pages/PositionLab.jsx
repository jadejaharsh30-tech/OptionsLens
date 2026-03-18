// Module 3 — Position Lab
// Strategy builder → BS pricing → net Greeks + payoff diagram + scenario sliders
// Per-leg expiry selector enables calendar spreads and diagonal spreads.

import { useState, useCallback } from 'react'
import PlotWrapper from '../components/PlotWrapper'
import { useApp } from '../context/AppContext'
import useExpiries from '../hooks/useExpiries'
import client from '../api/client'
import LoadingSpinner from '../components/LoadingSpinner'
import ErrorBanner from '../components/ErrorBanner'

// ── Plotly Golden Hour defaults ───────────────────────────────────────────────
const LAYOUT_BASE = {
  paper_bgcolor: 'transparent',
  plot_bgcolor:  '#FDFAF6',
  font:  { family: 'IBM Plex Mono, monospace', color: '#7A6355', size: 11 },
  margin: { t: 36, b: 52, l: 64, r: 20 },
  xaxis: { gridcolor: '#EDE3D8', zerolinecolor: '#D4C4B0', color: '#7A6355' },
  yaxis: { gridcolor: '#EDE3D8', zerolinecolor: '#D4C4B0', color: '#7A6355' },
  legend: { font: { color: '#7A6355', size: 11 }, bgcolor: 'transparent' },
}
const CONFIG   = { displayModeBar: false, responsive: true }
const GREEKS   = ['delta', 'gamma', 'vega', 'theta', 'rho']
const EMPTY_LEG = { option_type: 'CE', action: 'BUY', qty: 1, strike: '', iv: '', expiry: null }
const gColor    = v => v > 0 ? '#0D9488' : v < 0 ? '#DC2626' : '#A89585'

export default function PositionLab() {
  const { symbol, spot } = useApp()

  // ── Expiry list for all legs ──────────────────────────────────────────────
  const { data: expiriesData } = useExpiries(symbol)
  const expiries = expiriesData || []

  // ── Per-leg chain cache: { "DD-MM-YYYY": chainData } ─────────────────────
  const [legChains, setLegChains] = useState({})

  const [legs,    setLegs]    = useState([{ ...EMPTY_LEG }])
  const [result,  setResult]  = useState(null)
  const [loading, setLoading] = useState(false)
  const [calcErr, setCalcErr] = useState(null)

  // Scenario sliders
  const [spotShift, setSpotShift] = useState(0)
  const [ivShift,   setIvShift]   = useState(0)
  const [daysAdv,   setDaysAdv]   = useState(0)

  // ── Leg management ────────────────────────────────────────────────────────
  const updateLeg = (i, field, value) =>
    setLegs(prev => prev.map((l, idx) => idx === i ? { ...l, [field]: value } : l))

  const addLeg    = () => setLegs(prev => [...prev, { ...EMPTY_LEG }])
  const removeLeg = i  => setLegs(prev => prev.filter((_, idx) => idx !== i))

  // ── Expiry change — lazy-fetch chain for that expiry ──────────────────────
  const onLegExpiryChange = useCallback(async (i, expiryObj) => {
    // Reset strike and IV when expiry changes
    setLegs(prev => prev.map((l, idx) =>
      idx === i ? { ...l, expiry: expiryObj, strike: '', iv: '' } : l
    ))

    if (!expiryObj || legChains[expiryObj.date]) return  // already cached

    try {
      const res = await client.get(`/api/chain/${symbol}`, {
        params: { expiry_epoch: expiryObj.expiry, expiry_date: expiryObj.date }
      })
      setLegChains(prev => ({ ...prev, [expiryObj.date]: res.data }))
    } catch (e) {
      console.error('Failed to fetch chain for expiry', expiryObj.date, e)
    }
  }, [symbol, legChains])

  // ── Strike change — autofill IV from per-leg chain ───────────────────────
  const onStrikeChange = (i, strikeVal) => {
    updateLeg(i, 'strike', strikeVal)
    const leg = legs[i]
    if (!leg.expiry || !strikeVal) return
    const chainData = legChains[leg.expiry.date]
    if (!chainData) return
    const row = chainData.chain.find(
      r => r.strike === Number(strikeVal) && r.option_type === leg.option_type
    )
    if (row?.iv != null) updateLeg(i, 'iv', row.iv.toFixed(1))
  }

  // ── Calculate ─────────────────────────────────────────────────────────────
  const calculate = useCallback(async () => {
    const incomplete = legs.some(l => !l.expiry || !l.strike || !l.iv || !l.qty)
    if (!spot || incomplete) {
      setCalcErr('Fill in Expiry, Strike, IV%, and Qty for every leg.')
      return
    }
    setLoading(true); setCalcErr(null)
    try {
      const body = {
        spot: spot * (1 + spotShift / 100),
        legs: legs.map(l => {
          const chainData = legChains[l.expiry.date]
          const row       = chainData?.chain.find(r => r.strike === Number(l.strike))
          const baseT     = row ? (chainData.T || 30 / 365) : 30 / 365
          const T         = Math.max(baseT - daysAdv / 365, 1e-9)
          return {
            strike:      Number(l.strike),
            T,
            option_type: l.option_type,
            action:      l.action,
            qty:         Number(l.qty),
            iv:          (Number(l.iv) + ivShift) / 100,
          }
        }),
        spot_range_pct: 15,
        num_points:     200,
      }
      const res = await client.post('/api/position-lab/calculate', body)
      setResult(res.data)
    } catch (e) {
      setCalcErr(e.response?.data?.detail || e.message)
    } finally {
      setLoading(false)
    }
  }, [legs, spot, spotShift, ivShift, daysAdv, legChains])

  // ── Payoff chart ──────────────────────────────────────────────────────────
  const breakevens = result?.payoff?.breakevens || []
  const payoffData = result ? [
    {
      type: 'scatter', mode: 'lines', name: 'P&L at Expiry',
      x: result.payoff.spot_range, y: result.payoff.pnl_expiry,
      line: { color: '#0D9488', width: 2.5 },
    },
    {
      type: 'scatter', mode: 'lines', name: 'P&L Today',
      x: result.payoff.spot_range, y: result.payoff.pnl_today,
      line: { color: '#D97706', width: 1.5, dash: 'dot' },
    },
  ] : []

  const payoffLayout = {
    ...LAYOUT_BASE,
    title: { text: 'Payoff Diagram', font: { color: '#2C1810', size: 13, family: 'Source Sans 3, sans-serif', weight: 600 }, x: 0.02 },
    height: 320,
    xaxis: { ...LAYOUT_BASE.xaxis, title: 'Spot Price (₹)' },
    yaxis: { ...LAYOUT_BASE.yaxis, title: 'P&L (₹)' },
    shapes: [
      { type: 'line', x0: 0, x1: 1, xref: 'paper', y0: 0, y1: 0,
        line: { color: '#D4C4B0', dash: 'dot', width: 1 } },
      ...(spot ? [{ type: 'line', x0: spot * (1 + spotShift / 100), x1: spot * (1 + spotShift / 100),
                    yref: 'paper', y0: 0, y1: 1,
                    line: { color: '#A89585', dash: 'dash', width: 1 } }] : []),
      ...breakevens.map(be => ({
        type: 'line', x0: be, x1: be, yref: 'paper', y0: 0, y1: 1,
        line: { color: '#DC2626', dash: 'dot', width: 1 },
      })),
    ],
    annotations: breakevens.map(be => ({
      x: be, y: 0.02, yref: 'paper', xanchor: 'center',
      text: `BE ₹${Math.round(be).toLocaleString('en-IN')}`,
      showarrow: false,
      font: { color: '#DC2626', size: 9, family: 'IBM Plex Mono' },
    })),
  }

  const inputStyle = { background: '#FBF7F0', border: '1px solid #E8DDD0', color: '#2C1810' }
  const inputFocus = e => { e.target.style.borderColor = '#C8860A' }
  const inputBlur  = e => { e.target.style.borderColor = '#E8DDD0' }

  return (
    <div className="grid grid-cols-3 gap-5 items-start fade-up">

      {/* ── Left column: builder + sliders ─────────────────────────────── */}
      <div className="col-span-1 flex flex-col gap-4">

        {/* Strategy builder */}
        <div className="card">
          <h3 className="text-sm font-semibold mb-4" style={{ color: '#2C1810' }}>
            Strategy Builder
          </h3>

          {legs.map((leg, i) => (
            <div key={i} className="mb-4 pb-4 last:border-0 last:mb-0 last:pb-0"
                 style={{ borderBottom: '1px solid #E8DDD0' }}>
              <div className="flex items-center justify-between mb-2">
                <span className="text-xs mono" style={{ color: '#A89585' }}>Leg {i + 1}</span>
                {legs.length > 1 && (
                  <button onClick={() => removeLeg(i)}
                    className="text-xs mono transition-colors"
                    style={{ color: '#A89585' }}
                    onMouseEnter={e => e.target.style.color = '#DC2626'}
                    onMouseLeave={e => e.target.style.color = '#A89585'}>
                    × Remove
                  </button>
                )}
              </div>

              <div className="grid grid-cols-2 gap-2">
                {/* Option type + Action */}
                <select value={leg.option_type}
                  onChange={e => updateLeg(i, 'option_type', e.target.value)}
                  className="text-xs mono rounded px-2 py-1.5 focus:outline-none"
                  style={inputStyle} onFocus={inputFocus} onBlur={inputBlur}>
                  <option value="CE">CALL (CE)</option>
                  <option value="PE">PUT (PE)</option>
                </select>

                <select value={leg.action}
                  onChange={e => updateLeg(i, 'action', e.target.value)}
                  className="text-xs mono rounded px-2 py-1.5 focus:outline-none"
                  style={inputStyle} onFocus={inputFocus} onBlur={inputBlur}>
                  <option value="BUY">BUY</option>
                  <option value="SELL">SELL</option>
                </select>

                {/* Expiry selector — full width */}
                <div className="col-span-2">
                  <select
                    value={leg.expiry ? leg.expiry.date : ''}
                    onChange={e => {
                      const chosen = expiries.find(exp => exp.date === e.target.value) || null
                      onLegExpiryChange(i, chosen)
                    }}
                    className="w-full text-xs mono rounded px-2 py-1.5 focus:outline-none"
                    style={inputStyle} onFocus={inputFocus} onBlur={inputBlur}
                  >
                    <option value="">— Select Expiry —</option>
                    {expiries.map(exp => (
                      <option key={exp.date} value={exp.date}>{exp.date}</option>
                    ))}
                  </select>
                </div>

                {/* Strike selector — uses per-leg chain */}
                <div className="col-span-2">
                  {(() => {
                    const legChain   = leg.expiry ? legChains[leg.expiry.date] : null
                    const legStrikes = legChain
                      ? [...new Set(legChain.chain.map(r => r.strike))].sort((a, b) => a - b)
                      : []
                    return (
                      <select
                        value={leg.strike}
                        onChange={e => onStrikeChange(i, e.target.value)}
                        disabled={!leg.expiry || legStrikes.length === 0}
                        className="w-full text-xs mono rounded px-2 py-1.5 focus:outline-none"
                        style={{
                          ...inputStyle,
                          opacity: (!leg.expiry || legStrikes.length === 0) ? 0.5 : 1,
                        }}
                        onFocus={inputFocus} onBlur={inputBlur}
                      >
                        <option value="">
                          {!leg.expiry ? '— Select expiry first —' : '— Strike —'}
                        </option>
                        {legStrikes.map(s => (
                          <option key={s} value={s}>{s}</option>
                        ))}
                      </select>
                    )
                  })()}
                </div>

                {/* IV */}
                <div className="relative col-span-1">
                  <input type="number" placeholder="IV %" value={leg.iv} step="0.1"
                    onChange={e => updateLeg(i, 'iv', e.target.value)}
                    className="w-full text-xs mono rounded px-2 py-1.5 pr-6 focus:outline-none"
                    style={inputStyle} onFocus={inputFocus} onBlur={inputBlur} />
                  <span className="absolute right-2 top-1/2 -translate-y-1/2 text-xs mono
                                   pointer-events-none" style={{ color: '#A89585' }}>%</span>
                </div>

                {/* Qty */}
                <input type="number" placeholder="Qty (lots)" value={leg.qty} min={1}
                  onChange={e => updateLeg(i, 'qty', e.target.value)}
                  className="text-xs mono rounded px-2 py-1.5 focus:outline-none"
                  style={inputStyle} onFocus={inputFocus} onBlur={inputBlur} />
              </div>
            </div>
          ))}

          <button onClick={addLeg}
            className="w-full py-2 mt-2 text-xs mono rounded transition-all"
            style={{ border: '1px dashed #D4C4B0', color: '#A89585' }}
            onMouseEnter={e => { e.target.style.borderColor = '#C8860A'; e.target.style.color = '#C8860A' }}
            onMouseLeave={e => { e.target.style.borderColor = '#D4C4B0'; e.target.style.color = '#A89585' }}>
            + Add Leg
          </button>
        </div>

        {/* Scenario sliders */}
        <div className="card">
          <h3 className="text-sm font-semibold mb-4" style={{ color: '#2C1810' }}>Scenario</h3>
          {[
            { label: 'Spot Shift', value: spotShift, min: -20, max: 20, step: 1, unit: '%', setter: setSpotShift },
            { label: 'IV Shift',   value: ivShift,   min: -50, max: 50, step: 1, unit: '%', setter: setIvShift   },
            { label: 'Days Fwd',   value: daysAdv,   min: 0,   max: 30, step: 1, unit: 'd', setter: setDaysAdv   },
          ].map(s => (
            <div key={s.label} className="mb-4 last:mb-0">
              <div className="flex justify-between text-xs mono mb-1.5">
                <span style={{ color: '#7A6355' }}>{s.label}</span>
                <span style={{ color: s.value > 0 ? '#0D9488' : s.value < 0 ? '#DC2626' : '#A89585' }}>
                  {s.value > 0 ? '+' : ''}{s.value}{s.unit}
                </span>
              </div>
              <input type="range" min={s.min} max={s.max} step={s.step}
                value={s.value} onChange={e => s.setter(Number(e.target.value))}
                className="w-full h-1.5" />
            </div>
          ))}
        </div>

        {/* Calculate button */}
        <button onClick={calculate} disabled={loading}
          className="w-full py-3 rounded-lg text-sm font-semibold transition-all
                     disabled:opacity-40 disabled:cursor-not-allowed"
          style={{ background: '#2C1810', color: '#FBF7F0' }}
          onMouseEnter={e => { if (!loading) e.target.style.background = '#3D2418' }}
          onMouseLeave={e => { e.target.style.background = '#2C1810' }}>
          {loading
            ? <span className="flex items-center justify-center gap-2">
                <span className="w-4 h-4 rounded-full border-2 inline-block animate-spin"
                      style={{ borderColor: 'rgba(251,247,240,0.3)', borderTopColor: '#FBF7F0' }} />
                Pricing…
              </span>
            : 'Calculate'}
        </button>

        {calcErr && <ErrorBanner message={calcErr} />}
      </div>

      {/* ── Right columns: results ──────────────────────────────────────── */}
      <div className="col-span-2 flex flex-col gap-4">

        {!result && !loading && (
          <div className="card flex flex-col items-center justify-center h-48 gap-3">
            <div className="text-3xl opacity-30">📐</div>
            <span className="mono text-sm" style={{ color: '#A89585' }}>
              Build a strategy and click Calculate
            </span>
          </div>
        )}

        {loading && <LoadingSpinner label="Pricing strategy…" />}

        {result && (
          <>
            {/* Net Greeks */}
            <div className="card">
              <h3 className="text-sm font-semibold mb-4" style={{ color: '#2C1810' }}>Net Greeks</h3>
              <div className="grid grid-cols-5 gap-4">
                {GREEKS.map(g => {
                  const val    = result.net_greeks[g]
                  const fmtVal = Math.abs(val) < 0.001
                    ? val.toExponential(2)
                    : Math.abs(val) > 100
                    ? val.toFixed(1)
                    : val.toFixed(4)
                  return (
                    <div key={g}>
                      <div className="text-xs mono mb-1 capitalize" style={{ color: '#A89585' }}>{g}</div>
                      <div className="mono font-semibold text-sm" style={{ color: gColor(val) }}>
                        {fmtVal}
                      </div>
                    </div>
                  )
                })}
              </div>
            </div>

            {/* Per-leg breakdown table */}
            <div className="card overflow-x-auto">
              <h3 className="text-sm font-semibold mb-4" style={{ color: '#2C1810' }}>Leg Breakdown</h3>
              <table className="w-full text-xs mono border-collapse">
                <thead>
                  <tr style={{ borderBottom: '1px solid #E8DDD0' }}>
                    {['Expiry','Strike','Type','Action','Qty','IV%','LTP','Δ','Γ','V','Θ'].map(h => (
                      <th key={h} className="text-left pb-2 pr-4 font-medium" style={{ color: '#A89585' }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {result.leg_details.map((l, i) => (
                    <tr key={i} style={{ borderBottom: '1px solid rgba(232,221,208,0.5)' }}>
                      <td className="py-2 pr-4" style={{ color: '#A89585' }}>
                        {legs[i]?.expiry?.date ?? '—'}
                      </td>
                      <td className="py-2 pr-4 font-semibold" style={{ color: '#2C1810' }}>{l.strike}</td>
                      <td className="py-2 pr-4" style={{ color: l.option_type === 'CE' ? '#0D9488' : '#DC2626' }}>{l.option_type}</td>
                      <td className="py-2 pr-4" style={{ color: l.action === 'BUY' ? '#0D9488' : '#DC2626' }}>{l.action}</td>
                      <td className="py-2 pr-4" style={{ color: '#2C1810' }}>{l.qty}</td>
                      <td className="py-2 pr-4" style={{ color: '#D97706' }}>{l.iv_pct}%</td>
                      <td className="py-2 pr-4" style={{ color: '#2C1810' }}>₹{l.ltp}</td>
                      <td className="py-2 pr-4" style={{ color: gColor(l.delta) }}>{l.delta?.toFixed(4)}</td>
                      <td className="py-2 pr-4" style={{ color: '#7A6355' }}>{l.gamma?.toExponential(3)}</td>
                      <td className="py-2 pr-4" style={{ color: gColor(l.vega) }}>{l.vega?.toFixed(4)}</td>
                      <td className="py-2 pr-4" style={{ color: gColor(l.theta) }}>{l.theta?.toFixed(4)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Payoff diagram */}
            <div className="card p-0 overflow-hidden">
              <PlotWrapper data={payoffData} layout={payoffLayout} config={CONFIG}
                style={{ width: '100%' }} />
            </div>

            {/* Breakevens */}
            {breakevens.length > 0 && (
              <div className="card flex flex-wrap gap-4 items-center">
                <span className="text-xs mono" style={{ color: '#A89585' }}>
                  Breakeven{breakevens.length > 1 ? 's' : ''}:
                </span>
                {breakevens.map((be, i) => (
                  <span key={i} className="mono text-sm font-semibold" style={{ color: '#DC2626' }}>
                    ₹{Math.round(be).toLocaleString('en-IN')}
                  </span>
                ))}
              </div>
            )}
          </>
        )}
      </div>

    </div>
  )
}
