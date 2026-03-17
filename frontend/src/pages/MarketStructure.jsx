// Module 1 — Market Structure
// Charts: 3D IV surface, per-expiry skew curves, ATM term structure
// Also shows IV Rank gauge row + summary metrics

import { useState } from 'react'
import { useApp } from '../context/AppContext'
import useSurface from '../hooks/useSurface'
import useIVRank from '../hooks/useIVRank'
import IVRankGauge from '../components/IVRankGauge'
import PlotWrapper from '../components/PlotWrapper'
import LoadingSpinner from '../components/LoadingSpinner'
import ErrorBanner from '../components/ErrorBanner'

// ── Shared Plotly Golden Hour layout defaults ─────────────────────────────────
const LAYOUT_BASE = {
  paper_bgcolor: 'transparent',
  plot_bgcolor:  '#FDFAF6',   // barely-warm off-white chart area
  font:  { family: 'IBM Plex Mono, monospace', color: '#7A6355', size: 11 },
  margin: { t: 36, b: 48, l: 52, r: 20 },
  xaxis: { gridcolor: '#EDE3D8', zerolinecolor: '#D4C4B0', color: '#7A6355' },
  yaxis: { gridcolor: '#EDE3D8', zerolinecolor: '#D4C4B0', color: '#7A6355' },
  legend: { font: { color: '#7A6355', size: 11 }, bgcolor: 'transparent' },
}

const CONFIG = { displayModeBar: false, responsive: true }

const TITLE_STYLE = { font: { color: '#2C1810', size: 13, family: 'Source Sans 3, sans-serif', weight: 600 }, x: 0.02 }

export default function MarketStructure() {
  const { symbol } = useApp()
  const { data: surface, loading, error, refetch } = useSurface(symbol)
  const { data: ivrank } = useIVRank(symbol)

  const [activeExpiry, setActiveExpiry] = useState(null)

  if (loading) return <LoadingSpinner label={`Fetching IV surface for ${symbol}…`} />
  if (error)   return <ErrorBanner message={error} onRetry={refetch} />
  if (!surface) return null

  const expiries       = Object.keys(surface.skew || {})
  const selectedExpiry = activeExpiry || expiries[0]

  // ── Chart 1: 3D IV Surface ──────────────────────────────────────────────────
  const dteCols = [...new Set(surface.surface.map(r => r.days_to_expiry))].sort((a,b)=>a-b)
  const monRows = [...new Set(surface.surface.map(r => r.moneyness))].sort((a,b)=>a-b)
  const lookup  = {}
  surface.surface.forEach(r => { lookup[`${r.moneyness}_${r.days_to_expiry}`] = r.mid_iv })
  const zMatrix = monRows.map(m => dteCols.map(d => lookup[`${m}_${d}`] ?? null))

  const surface3DData = [{
    type:       'surface',
    x:          dteCols,
    y:          monRows,
    z:          zMatrix,
    colorscale: [[0,'#00d4aa'],[0.35,'#00d4aa'],[0.65,'#ffa502'],[1,'#ff4757']],
    showscale:  true,
    opacity:    0.92,
    colorbar: {
      title: 'IV %', titlefont: { color: '#7A6355', size: 10 },
      tickfont: { color: '#7A6355', size: 10 }, thickness: 12, len: 0.6,
    },
    hovertemplate: 'DTE: %{x}d<br>Moneyness: %{y:.4f}<br>IV: %{z:.1f}%<extra></extra>',
  }]

  const surface3DLayout = {
    ...LAYOUT_BASE,
    title:  { text: 'IV Surface', ...TITLE_STYLE },
    height: 430,
    margin: { t: 40, b: 10, l: 10, r: 10 },
    scene: {
      xaxis: { title: 'DTE',       color: '#7A6355', gridcolor: '#EDE3D8', backgroundcolor: '#FDFAF6', titlefont: { size: 10 } },
      yaxis: { title: 'Moneyness', color: '#7A6355', gridcolor: '#EDE3D8', backgroundcolor: '#FDFAF6', titlefont: { size: 10 } },
      zaxis: { title: 'IV %',      color: '#7A6355', gridcolor: '#EDE3D8', backgroundcolor: '#FDFAF6', titlefont: { size: 10 } },
      bgcolor: '#FDFAF6',
      camera: { eye: { x: 1.6, y: -1.6, z: 1.1 } },
    },
  }

  // ── Chart 2: Skew ───────────────────────────────────────────────────────────
  const skewRows = surface.skew?.[selectedExpiry] || []
  const skewData = [
    {
      type: 'scatter', mode: 'lines+markers', name: 'Call IV',
      x: skewRows.map(r => r.moneyness), y: skewRows.map(r => r.call_iv),
      line: { color: '#00d4aa', width: 2 }, marker: { color: '#00d4aa', size: 5 },
      hovertemplate: 'K/S: %{x:.4f}<br>Call IV: %{y:.1f}%<extra></extra>',
    },
    {
      type: 'scatter', mode: 'lines+markers', name: 'Put IV',
      x: skewRows.map(r => r.moneyness), y: skewRows.map(r => r.put_iv),
      line: { color: '#ff4757', width: 2 }, marker: { color: '#ff4757', size: 5 },
      hovertemplate: 'K/S: %{x:.4f}<br>Put IV: %{y:.1f}%<extra></extra>',
    },
  ]

  const skewLayout = {
    ...LAYOUT_BASE,
    title:  { text: `IV Skew  ·  ${selectedExpiry}`, ...TITLE_STYLE },
    height: 300,
    xaxis:  { ...LAYOUT_BASE.xaxis, title: 'Moneyness (K / S)' },
    yaxis:  { ...LAYOUT_BASE.yaxis, title: 'IV (%)' },
    shapes: [{ type:'line', x0:1, x1:1, yref:'paper', y0:0, y1:1,
               line:{ color:'#D4C4B0', dash:'dot', width:1 } }],
    annotations: [{ x:1, y:1, yref:'paper', xanchor:'left',
                    text:'ATM', showarrow:false,
                    font:{ color:'#A89585', size:9, family:'IBM Plex Mono' } }],
  }

  // ── Chart 3: Term Structure ─────────────────────────────────────────────────
  const ts = surface.term_structure || []
  const tsData = [{
    type: 'scatter', mode: 'lines+markers', name: 'ATM IV',
    x: ts.map(r => r.days_to_expiry), y: ts.map(r => r.atm_iv),
    line: { color: '#D97706', width: 2.5 }, marker: { color: '#D97706', size: 8 },
    text: ts.map(r => r.expiry_date),
    hovertemplate: '%{text}<br>DTE: %{x}d<br>ATM IV: %{y:.1f}%<extra></extra>',
  }]

  const tsLayout = {
    ...LAYOUT_BASE,
    title:  { text: 'Term Structure  ·  ATM IV', ...TITLE_STYLE },
    height: 300,
    xaxis:  { ...LAYOUT_BASE.xaxis, title: 'Days to Expiry' },
    yaxis:  { ...LAYOUT_BASE.yaxis, title: 'ATM IV (%)' },
  }

  return (
    <div className="flex flex-col gap-5">

      {/* IV Rank + summary row */}
      <div className="flex items-stretch gap-4 flex-wrap">
        <IVRankGauge symbol={symbol} ivRank={ivrank?.iv_rank} currentIV={ivrank?.current_iv} />

        <div className="card flex flex-wrap gap-x-8 gap-y-4 flex-1 items-center">
          {[
            { label: 'Spot',         value: surface.spot ? `₹${surface.spot.toLocaleString('en-IN',{maximumFractionDigits:2})}` : '—' },
            { label: 'Expiries',     value: expiries.length },
            { label: 'Surface Pts',  value: surface.surface.length },
            { label: 'History Days', value: ivrank?.history_days ?? '—' },
          ].map(m => (
            <div key={m.label}>
              <div className="text-xs mb-0.5" style={{ color: '#A89585' }}>{m.label}</div>
              <div className="mono font-semibold text-base" style={{ color: '#2C1810' }}>{m.value}</div>
            </div>
          ))}
        </div>

        {ivrank?.note && (
          <div className="card flex-1 text-xs mono self-center leading-relaxed"
               style={{ borderColor: 'rgba(217,119,6,0.3)', color: '#B45309', background: 'rgba(217,119,6,0.06)' }}>
            ℹ {ivrank.note}
          </div>
        )}
      </div>

      {/* 3D Surface */}
      {zMatrix.length > 0 && dteCols.length > 0 ? (
        <div className="card p-0 overflow-hidden">
          <PlotWrapper data={surface3DData} layout={surface3DLayout} config={CONFIG} style={{ width:'100%' }} />
        </div>
      ) : (
        <div className="card text-sm text-center py-10 mono" style={{ color: '#A89585' }}>
          Not enough expiry data for 3D surface — need at least 2 expiries with valid IV.
        </div>
      )}

      {/* Skew + Term Structure */}
      <div className="grid grid-cols-2 gap-5">
        <div className="card p-0 overflow-hidden flex flex-col">
          {expiries.length > 0 && (
            <div className="flex gap-1 p-3 pb-0 flex-wrap" style={{ borderBottom: '1px solid #E8DDD0' }}>
              {expiries.map(exp => (
                <button key={exp} onClick={() => setActiveExpiry(exp)}
                  className="text-xs mono px-3 py-1 rounded-t transition-all"
                  style={(activeExpiry || expiries[0]) === exp
                    ? { background:'rgba(200,134,10,0.10)', color:'#C8860A', border:'1px solid rgba(200,134,10,0.30)' }
                    : { color:'#A89585', border:'1px solid transparent' }
                  }>
                  {exp}
                </button>
              ))}
            </div>
          )}
          {skewRows.length > 0
            ? <PlotWrapper data={skewData} layout={skewLayout} config={CONFIG} style={{ width:'100%' }} />
            : <div className="flex-1 flex items-center justify-center text-xs mono p-8"
                   style={{ color: '#A89585' }}>No skew data for {selectedExpiry}</div>
          }
        </div>

        <div className="card p-0 overflow-hidden">
          {ts.length > 0
            ? <PlotWrapper data={tsData} layout={tsLayout} config={CONFIG} style={{ width:'100%' }} />
            : <div className="flex items-center justify-center h-full text-xs mono p-8"
                   style={{ color: '#A89585' }}>No term structure data available.</div>
          }
        </div>
      </div>

    </div>
  )
}
