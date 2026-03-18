// optionslens/frontend/src/components/IVvsRVPanel.jsx
// IV vs Realized Vol comparison panel.
// Shows: vol premium badge (RICH/CHEAP/FAIR VALUE), RV metrics, ATM IV vs RV area chart.
// Mounts in MarketStructure below the IV Rank row.

import {
  AreaChart, Area, XAxis, YAxis,
  CartesianGrid, Tooltip, ResponsiveContainer,
} from 'recharts'

const AXIS_TICK = { fill: '#7A6355', fontSize: 10, fontFamily: 'IBM Plex Mono' }
const GRID = { strokeDasharray: '3 3', stroke: '#EDE3D8' }

// ── Vol Premium Badge ─────────────────────────────────────────────────────────
function VolPremiumBadge({ premium }) {
  if (premium == null) return null

  const rich  = premium > 2
  const cheap = premium < -2
  const color  = rich ? '#DC2626' : cheap ? '#0D9488' : '#D97706'
  const bg     = rich
    ? 'rgba(220,38,38,0.08)'
    : cheap
    ? 'rgba(13,148,136,0.08)'
    : 'rgba(217,119,6,0.08)'
  const border = rich
    ? 'rgba(220,38,38,0.25)'
    : cheap
    ? 'rgba(13,148,136,0.25)'
    : 'rgba(217,119,6,0.25)'
  const label  = rich ? 'IV RICH' : cheap ? 'IV CHEAP' : 'FAIR VALUE'

  return (
    <div
      className="flex items-center gap-2 px-3 py-1.5 rounded mono text-xs font-semibold"
      style={{ background: bg, border: `1px solid ${border}`, color }}
    >
      {label}&nbsp;
      <span style={{ fontWeight: 400 }}>
        {premium > 0 ? '+' : ''}{premium.toFixed(1)} pp
      </span>
    </div>
  )
}

// ── Custom Tooltip ────────────────────────────────────────────────────────────
const IVRVTooltip = ({ active, payload, label }) => {
  if (!active || !payload?.length) return null
  return (
    <div
      className="card text-xs mono"
      style={{ padding: '8px 12px', minWidth: 150,
               boxShadow: '0 4px 16px rgba(44,24,16,0.12)' }}
    >
      <div className="font-semibold mb-1.5" style={{ color: '#2C1810' }}>{label}</div>
      {payload.map(p => (
        <div key={p.name} style={{ color: p.color }}>
          {p.name}: {p.value?.toFixed(1)}%
        </div>
      ))}
      {payload.length >= 2 && payload[0]?.value != null && payload[1]?.value != null && (
        <div className="mt-1 pt-1" style={{ borderTop: '1px solid #E8DDD0', color: '#A89585' }}>
          Premium: {(payload[0].value - payload[1].value).toFixed(1)} pp
        </div>
      )}
    </div>
  )
}

// ── Main Component ────────────────────────────────────────────────────────────
export default function IVvsRVPanel({ data }) {
  if (!data) return null

  const { current_iv, rv_20d, rv_60d, vol_premium, iv_rv_series } = data

  const hasHistory = iv_rv_series && iv_rv_series.length > 0

  // Format MM-DD for axis labels
  const chartData = (iv_rv_series || []).map(r => ({
    ...r,
    label: r.date.slice(5),
  }))

  return (
    <div className="card flex flex-col gap-4">

      {/* Header + badge */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <h3 className="text-sm font-semibold" style={{ color: '#2C1810' }}>
          IV vs Realized Vol
        </h3>
        <VolPremiumBadge premium={vol_premium} />
      </div>

      {/* Metric strip */}
      <div className="grid grid-cols-3 gap-4">
        {[
          { label: 'ATM IV',  value: current_iv, color: '#D97706' },
          { label: 'RV 20d',  value: rv_20d,     color: '#0D9488' },
          { label: 'RV 60d',  value: rv_60d,     color: '#7A6355' },
        ].map(({ label, value, color }) => (
          <div key={label}>
            <div className="text-xs mono mb-0.5" style={{ color: '#A89585' }}>{label}</div>
            <div className="mono font-semibold text-base" style={{ color }}>
              {value != null ? `${value.toFixed(1)}%` : '—'}
            </div>
          </div>
        ))}
      </div>

      {/* Vol premium interpretation hint */}
      {vol_premium != null && (
        <div className="text-xs mono leading-relaxed" style={{ color: '#A89585' }}>
          {vol_premium > 2
            ? `Options priced ${vol_premium.toFixed(1)} pp above recent realized moves — selling premium has an edge.`
            : vol_premium < -2
            ? `Options priced ${Math.abs(vol_premium).toFixed(1)} pp below recent realized moves — buying premium has an edge.`
            : 'Options fairly priced vs recent realized moves — no strong vol edge from this metric alone.'}
        </div>
      )}

      {/* Time-series area chart */}
      {hasHistory ? (
        <ResponsiveContainer width="100%" height={200}>
          <AreaChart data={chartData} margin={{ top: 5, right: 10, bottom: 5, left: 24 }}>
            <CartesianGrid {...GRID} />
            <XAxis
              dataKey="label"
              tick={AXIS_TICK}
              interval="preserveStartEnd"
            />
            <YAxis
              tickFormatter={v => `${v.toFixed(0)}%`}
              tick={AXIS_TICK}
              domain={['auto', 'auto']}
            />
            <Tooltip content={<IVRVTooltip />} />
            <Area
              type="monotone"
              dataKey="atm_iv"
              name="ATM IV"
              stroke="#D97706"
              fill="rgba(217,119,6,0.08)"
              strokeWidth={2}
              dot={false}
            />
            <Area
              type="monotone"
              dataKey="rv_20d"
              name="RV 20d"
              stroke="#0D9488"
              fill="rgba(13,148,136,0.06)"
              strokeWidth={1.5}
              dot={false}
            />
          </AreaChart>
        </ResponsiveContainer>
      ) : (
        <div
          className="flex items-center justify-center h-20 mono text-xs rounded"
          style={{ color: '#A89585', background: 'rgba(232,221,208,0.3)' }}
        >
          {rv_20d == null
            ? 'Loading realized vol from Fyers historical prices…'
            : 'RV loaded — IV overlay builds after daily snapshots accumulate'}
        </div>
      )}

    </div>
  )
}
