// Module 2 — Smart Money
// OI butterfly, OI change bars, GEX profile, OI detail table
// Auto-refreshes every 60s during market hours (09:15–15:30 IST)

import { useEffect } from 'react'
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid,
  Tooltip, ReferenceLine, ResponsiveContainer, Cell,
} from 'recharts'
import { useApp } from '../context/AppContext'
import useOI from '../hooks/useOI'
import LoadingSpinner from '../components/LoadingSpinner'
import ErrorBanner from '../components/ErrorBanner'

// ── Formatters ────────────────────────────────────────────────────────────────
const fmt = n => {
  if (n == null) return '—'
  const abs = Math.abs(n)
  if (abs >= 1e6) return `${(n/1e6).toFixed(1)}M`
  if (abs >= 1e3) return `${(n/1e3).toFixed(0)}K`
  return String(n)
}

const fmtGex = n => {
  if (n == null) return '—'
  const abs  = Math.abs(n)
  const sign = n >= 0 ? '+' : '-'
  if (abs >= 1e9) return `${sign}${(abs/1e9).toFixed(1)}B`
  if (abs >= 1e6) return `${sign}${(abs/1e6).toFixed(1)}M`
  if (abs >= 1e3) return `${sign}${(abs/1e3).toFixed(0)}K`
  return `${sign}${abs.toFixed(0)}`
}

// ── Custom tooltip for OI / change charts ─────────────────────────────────────
const OITooltip = ({ active, payload, label }) => {
  if (!active || !payload?.length) return null
  return (
    <div className="card text-xs mono" style={{ padding:'8px 12px', minWidth:120,
      boxShadow:'0 4px 16px rgba(44,24,16,0.12)' }}>
      <div className="font-semibold mb-1.5" style={{ color:'#2C1810' }}>Strike {label}</div>
      {payload.map(p => (
        <div key={p.name} style={{ color: p.fill }}>{p.name}: {fmt(p.value)}</div>
      ))}
    </div>
  )
}

const GEXTooltip = ({ active, payload, label }) => {
  if (!active || !payload?.length) return null
  return (
    <div className="card text-xs mono" style={{ padding:'8px 12px',
      boxShadow:'0 4px 16px rgba(44,24,16,0.12)' }}>
      <div className="font-semibold mb-1.5" style={{ color:'#2C1810' }}>Strike {label}</div>
      <div style={{ color: payload[0]?.value >= 0 ? '#0D9488' : '#DC2626' }}>
        Net GEX: {fmtGex(payload[0]?.value)}
      </div>
    </div>
  )
}

const AXIS_TICK = { fill: '#7A6355', fontSize: 10, fontFamily: 'IBM Plex Mono' }
const GRID = { strokeDasharray: '3 3', stroke: '#EDE3D8' }

export default function SmartMoney() {
  const { symbol, expiry } = useApp()
  const { data: oi, loading, error, refetch } = useOI(symbol, expiry)

  useEffect(() => {
    const id = setInterval(() => {
      const now = new Date()
      const ist = new Date(now.toLocaleString('en-US', { timeZone: 'Asia/Kolkata' }))
      const h = ist.getHours(), m = ist.getMinutes()
      const open  = h > 9  || (h === 9  && m >= 15)
      const close = h < 15 || (h === 15 && m <= 30)
      if (open && close) refetch()
    }, 60_000)
    return () => clearInterval(id)
  }, [refetch])

  if (loading) return <LoadingSpinner label="Fetching OI data…" />
  if (error)   return <ErrorBanner message={error} onRetry={refetch} />
  if (!oi)     return null

  const tableData = oi.oi_table || []

  const pcrStyle = !oi.pcr ? '#A89585'
    : oi.pcr > 1.2 ? '#0D9488'
    : oi.pcr < 0.8 ? '#DC2626'
    : '#D97706'

  return (
    <div className="flex flex-col gap-5 fade-up">

      {/* Summary strip */}
      <div className="grid grid-cols-4 gap-4">
        {[
          { label: 'PCR',           value: oi.pcr?.toFixed(2) ?? '—',                  color: pcrStyle },
          { label: 'Max Pain',      value: `₹${oi.max_pain?.toLocaleString('en-IN')}`, color: '#2C1810' },
          { label: 'Total Call OI', value: fmt(oi.total_call_oi),                       color: '#0D9488' },
          { label: 'Total Put OI',  value: fmt(oi.total_put_oi),                        color: '#DC2626' },
        ].map(m => (
          <div key={m.label} className="card">
            <div className="text-xs mb-1" style={{ color: '#A89585' }}>{m.label}</div>
            <div className="mono text-xl font-semibold" style={{ color: m.color }}>{m.value}</div>
          </div>
        ))}
      </div>

      {/* OI by Strike */}
      <div className="card">
        <h3 className="text-sm font-semibold mb-1" style={{ color: '#2C1810' }}>
          Open Interest by Strike
          <span className="text-xs mono ml-2" style={{ color: '#A89585' }}>{expiry?.date}</span>
        </h3>
        <p className="text-xs mono mb-4" style={{ color: '#A89585' }}>
          Largest OI strikes = key support / resistance levels
        </p>
        <ResponsiveContainer width="100%" height={280}>
          <BarChart data={tableData} margin={{ top:5, right:20, bottom:5, left:20 }}>
            <CartesianGrid {...GRID} />
            <XAxis dataKey="strike" tick={AXIS_TICK} />
            <YAxis tickFormatter={fmt} tick={AXIS_TICK} />
            <Tooltip content={<OITooltip />} />
            <Bar dataKey="call_oi" name="Call OI" fill="#0D9488" opacity={0.80} radius={[3,3,0,0]} />
            <Bar dataKey="put_oi"  name="Put OI"  fill="#DC2626" opacity={0.80} radius={[3,3,0,0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>

      {/* OI Change + GEX */}
      <div className="grid grid-cols-2 gap-5">

        {/* OI Change */}
        <div className="card">
          <h3 className="text-sm font-semibold mb-1" style={{ color: '#2C1810' }}>
            OI Change vs Prev Session
          </h3>
          <p className="text-xs mono mb-4" style={{ color: '#A89585' }}>
            Green = buildup &nbsp;·&nbsp; Red = unwinding
          </p>
          <ResponsiveContainer width="100%" height={250}>
            <BarChart data={tableData} margin={{ top:5, right:10, bottom:5, left:20 }}>
              <CartesianGrid {...GRID} />
              <XAxis dataKey="strike" tick={AXIS_TICK} />
              <YAxis tickFormatter={fmt} tick={AXIS_TICK} />
              <ReferenceLine y={0} stroke="#D4C4B0" />
              <Tooltip content={<OITooltip />} />
              <Bar dataKey="call_oi_change" name="Call Δ" radius={[3,3,0,0]}>
                {tableData.map((r, i) => (
                  <Cell key={i} fill={r.call_oi_change >= 0 ? '#0D9488' : '#DC2626'} opacity={0.80} />
                ))}
              </Bar>
              <Bar dataKey="put_oi_change" name="Put Δ" radius={[3,3,0,0]}>
                {tableData.map((r, i) => (
                  <Cell key={i} fill={r.put_oi_change >= 0 ? '#DC2626' : '#0D9488'} opacity={0.80} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>

        {/* GEX Profile */}
        <div className="card">
          <h3 className="text-sm font-semibold mb-1" style={{ color: '#2C1810' }}>
            Gamma Exposure (GEX)
          </h3>
          <p className="text-xs mono mb-4" style={{ color: '#A89585' }}>
            +GEX = dealers long gamma (pin) &nbsp;·&nbsp; −GEX = short gamma (vol expansion)
          </p>
          <ResponsiveContainer width="100%" height={250}>
            <BarChart data={oi.gex_profile || []} margin={{ top:5, right:10, bottom:5, left:30 }}>
              <CartesianGrid {...GRID} />
              <XAxis dataKey="strike" tick={AXIS_TICK} />
              <YAxis tickFormatter={fmtGex} tick={AXIS_TICK} />
              <ReferenceLine y={0} stroke="#D97706" strokeWidth={1} strokeDasharray="4 2" />
              <Tooltip content={<GEXTooltip />} />
              <Bar dataKey="net_gex" name="Net GEX" radius={[3,3,0,0]}>
                {(oi.gex_profile || []).map((r, i) => (
                  <Cell key={i} fill={r.net_gex >= 0 ? '#0D9488' : '#DC2626'} opacity={0.80} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* OI Detail Table */}
      <div className="card overflow-x-auto">
        <h3 className="text-sm font-semibold mb-4" style={{ color: '#2C1810' }}>
          Strike-wise OI Detail
        </h3>
        <table className="w-full text-xs mono border-collapse">
          <thead>
            <tr style={{ borderBottom: '1px solid #E8DDD0' }}>
              {['Strike','Call OI','Put OI','Call Δ OI','Put Δ OI','Call LTP','Put LTP'].map(h => (
                <th key={h} className="text-left pb-2 pr-5 font-medium" style={{ color: '#A89585' }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {tableData.map((row, i) => (
              <tr key={i} className="transition-colors"
                  style={{ borderBottom: '1px solid rgba(232,221,208,0.5)' }}
                  onMouseEnter={e => e.currentTarget.style.background = 'rgba(200,134,10,0.03)'}
                  onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>
                <td className="py-2 pr-5 font-semibold" style={{ color: '#2C1810' }}>{row.strike}</td>
                <td className="py-2 pr-5" style={{ color: '#0D9488' }}>{fmt(row.call_oi)}</td>
                <td className="py-2 pr-5" style={{ color: '#DC2626' }}>{fmt(row.put_oi)}</td>
                <td className="py-2 pr-5" style={{ color: row.call_oi_change >= 0 ? '#0D9488' : '#DC2626' }}>
                  {row.call_oi_change >= 0 ? '+' : ''}{fmt(row.call_oi_change)}
                </td>
                <td className="py-2 pr-5" style={{ color: row.put_oi_change >= 0 ? '#DC2626' : '#0D9488' }}>
                  {row.put_oi_change >= 0 ? '+' : ''}{fmt(row.put_oi_change)}
                </td>
                <td className="py-2 pr-5" style={{ color: '#2C1810' }}>₹{row.call_ltp?.toFixed(2) ?? '—'}</td>
                <td className="py-2 pr-5" style={{ color: '#2C1810' }}>₹{row.put_ltp?.toFixed(2)  ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

    </div>
  )
}
