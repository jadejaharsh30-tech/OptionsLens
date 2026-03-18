// Module 1 — Market Structure
// Charts: 3D IV surface, per-expiry skew curves, ATM term structure
// Also shows IV Rank gauge row + summary metrics + IV vs RV panel

import { useState } from 'react'
import { useApp } from '../context/AppContext'
import useSurface from '../hooks/useSurface'
import useIVRank from '../hooks/useIVRank'
import IVRankGauge from '../components/IVRankGauge'
import IVvsRVPanel from '../components/IVvsRVPanel'
import PlotWrapper from '../components/PlotWrapper'
import LoadingSpinner from '../components/LoadingSpinner'
import ErrorBanner from '../components/ErrorBanner'
import InfoPanel, { Section, P, Callout, KV } from '../components/InfoPanel'

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

  const [activeExpiry,   setActiveExpiry]   = useState(null)
  const [sviInterpolate, setSviInterpolate] = useState(false)

  const { data: surface, loading, error, refetch } = useSurface(symbol, sviInterpolate)
  const { data: ivrank } = useIVRank(symbol)

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

  // Separate interpolated points for overlay scatter
  const interpPts = sviInterpolate ? surface.surface.filter(r => r.interpolated) : []

  const surface3DData = [
    {
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
    },
    // SVI fitted points overlay — only visible when interpolate is on
    ...(interpPts.length > 0 ? [{
      type: 'scatter3d', mode: 'markers', name: 'SVI fitted',
      x: interpPts.map(r => r.days_to_expiry),
      y: interpPts.map(r => r.moneyness),
      z: interpPts.map(r => r.mid_iv),
      marker: { size: 2.5, color: '#D97706', opacity: 0.55 },
      hovertemplate: 'DTE: %{x}d<br>Moneyness: %{y:.4f}<br>IV (SVI): %{z:.1f}%<extra></extra>',
      showlegend: true,
    }] : []),
  ]

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

      {/* IV vs Realized Vol Panel */}
      <IVvsRVPanel data={ivrank} />

      {/* SVI toggle */}
      <div className="flex items-center gap-3">
        <button
          onClick={() => setSviInterpolate(v => !v)}
          className="text-xs mono px-3 py-1.5 rounded transition-all"
          style={{
            background: sviInterpolate ? 'rgba(200,134,10,0.12)' : 'transparent',
            color:       sviInterpolate ? '#C8860A' : '#A89585',
            border:      sviInterpolate ? '1px solid rgba(200,134,10,0.3)' : '1px solid #E8DDD0',
          }}
        >
          {sviInterpolate ? '◆ SVI interpolated' : '◇ Raw surface'}
        </button>
        {sviInterpolate && (
          <span className="text-xs mono" style={{ color: '#A89585' }}>
            Amber dots = SVI fitted points · solid mesh = market data
          </span>
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

      <InfoPanel title="Understanding the IV Surface + IV Rank">
        <Section title="What is Implied Volatility (IV)?">
          <P>Every option has a price. Implied Volatility is what you get when you reverse-engineer that price through the Black-Scholes model — it's the market's consensus expectation of how much the underlying will move over the option's lifetime, expressed as an annualised percentage. When IV is 20%, the market expects roughly ±20% moves over the next year, or ±20%/√12 ≈ ±5.8% over the next month. IV is not a forecast — it's a price. It rises when demand for options increases (fear, uncertainty, upcoming events) and falls when demand drops (complacency, post-event calm).</P>
        </Section>

        <Section title="Reading the 3D Surface">
          <P>The three axes are: X = Days to Expiry (DTE), Y = Moneyness (Strike ÷ Spot, where 1.0 = ATM), Z = Mid-IV%. The surface shows you the entire options market's pricing structure in one view.</P>
          <Callout type="info">A healthy, normal surface has its lowest point near ATM (moneyness = 1.0) and rises toward both wings — this is the volatility smile. It slopes upward as DTE increases — this is the term structure. When either of these shapes distorts, it signals something.</Callout>
          <P><strong>Steep left wing (moneyness &lt; 0.97 has very high IV):</strong> The market is aggressively pricing in downside tail risk. Institutions/hedgers are buying deep OTM puts. Often seen before major uncertainty events or when large players are hedging portfolios.</P>
          <P><strong>Flat surface across strikes:</strong> IV is priced uniformly — the market sees no directional skew. Unusual. Either a very calm period or the market hasn't yet priced in known risks.</P>
          <P><strong>Spike on a specific expiry:</strong> That particular expiry date has elevated IV across all strikes. Look at what event (RBI policy, Union Budget, quarterly results) falls within that expiry window — the spike IS the market pricing that event.</P>
          <P><strong>Jagged/noisy spikes:</strong> Far OTM strikes with near-zero LTP produce unstable IV calculations. Ignore these — the solver is fitting noise, not real market pricing.</P>
          <P><strong>SVI interpolation:</strong> Toggle "SVI interpolated" to smooth gaps in the surface. The SVI (Stochastic Volatility Inspired) model fits a parametric smile curve per expiry and fills in strikes where no market data exists. Amber dots show the fitted points. Use the smoothed surface for visual analysis; rely on market data (raw mode) for actual trade pricing.</P>
        </Section>

        <Section title="The Summary Metrics Explained">
          <KV label="Expiries" value="Number of distinct expiry dates for which Fyers returned chain data. The surface uses up to 6. More expiries = more complete surface across time." />
          <KV label="Surface Pts" value="Total (strike × expiry) combinations with a successfully solved IV. Gaps appear where LTP was 0 (illiquid strike) or the solver didn't converge (deep OTM with near-zero premium). A good surface has 100+ points." />
          <KV label="History Days" value="Days of daily IV snapshots stored locally. Builds from 0 on day 1 — the backend captures a snapshot at 15:20 IST each trading day. IV Rank becomes meaningful after 5+ days and reliable after 30+ days." />
          <KV label="Current IV" value="Average implied volatility of near-ATM options (within 2% of spot) for the nearest expiry. This is the single number that summarises 'how expensive' options are right now." />
        </Section>

        <Section title="IV Rank — How to Use It">
          <P>IV Rank tells you where current IV sits relative to its own history over the past year. Formula: <strong>(Current IV − 52w Low) ÷ (52w High − 52w Low) × 100</strong>. It answers "is IV high or low compared to its own recent history?" — which is more useful than the raw IV number alone.</P>
          <Callout type="signal">IV Rank 0–30 (Low IV, green gauge): Options are cheap relative to history. This is a buying environment — volatility is likely to mean-revert upward. Consider long options strategies (buying straddles, strangles, calls or puts directionally). Premium is not expensive.</Callout>
          <Callout type="warning">IV Rank 30–60 (Medium IV, amber gauge): Normal range. No strong edge from IV alone — rely on directional analysis. Neutral strategies (iron condors, butterflies) work here.</Callout>
          <Callout type="signal">IV Rank 60–100 (High IV, red gauge): Options are expensive. This is a selling environment — volatility is likely to mean-revert downward. Consider short volatility strategies (selling spreads, covered calls, cash-secured puts). The key risk: IV can stay elevated or spike higher if the catalyst hasn't resolved yet. Never sell naked options in high IV without defining your risk.</Callout>
        </Section>

        <Section title="Implementing in Decision-Making">
          <P><strong>Before entering any options trade:</strong> Check IV Rank first. If you're about to buy a call and IV Rank is 85, you're buying expensive. If IV Rank is 15 and you're about to sell a put spread, you may not be collecting enough premium to justify the risk.</P>
          <P><strong>Identifying event risk:</strong> If the surface has a visible spike on a specific expiry's DTE column, something is being priced for that date. Research what events fall in that window before trading it.</P>
          <P><strong>Post-event trades:</strong> IV typically collapses after a major event (earnings, RBI policy) regardless of which way the market moves — this is the "IV crush". Selling premium just before an event and buying it back after (if the underlying doesn't move too much) is a classic strategy, but dangerous because the event itself can move the underlying enough to offset the IV collapse.</P>
        </Section>
      </InfoPanel>

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

      <InfoPanel title="Understanding IV Skew + Term Structure">
        <Section title="IV Skew — What It Shows">
          <P>The skew chart plots implied volatility (Y axis) against moneyness — Strike ÷ Spot (X axis) — for a single expiry. Moneyness of 1.0 is ATM. Below 1.0 are OTM puts (and ITM calls). Above 1.0 are OTM calls (and ITM puts). The teal line is Call IV, the red line is Put IV for each strike.</P>
          <Callout type="signal">Put IV consistently higher than Call IV is the normal state in equity markets and is called negative skew. The market systematically pays more for downside protection than upside participation. This reflects the asymmetry of fear — people buy puts to hedge portfolios but buy calls speculatively.</Callout>
          <P><strong>The slope of the put IV line:</strong> A steep downward slope from far OTM puts (left side, moneyness ~0.95-0.97) toward ATM means institutions are aggressively paying up for tail risk protection — deep OTM puts are very expensive. The steeper the slope, the more the market fears a sharp sudden drop. Flat put skew = complacency.</P>
          <P><strong>Call IV rising on the right wing:</strong> When OTM calls (moneyness &gt; 1.02) have elevated IV, it signals demand for upside exposure — often seen in trending rallies, short-squeeze environments, or when institutions are buying calls to participate in anticipated moves.</P>
          <P><strong>When put and call IV converge or cross:</strong> The skew has flattened or reversed. This is unusual and often signals a regime change — either a bullish shift (puts are no longer bid, fear has left the market) or symmetric event risk where both directions are being protected equally.</P>
        </Section>

        <Section title="Implementing Skew in Decision-Making">
          <Callout type="warning">Buying puts when put IV is much higher than call IV means you're paying a premium for the skew. The option is already expensive because everyone else wants the same protection. Consider put spreads (buy ATM put, sell OTM put) to reduce the cost of skew.</Callout>
          <Callout type="info">Selling puts when put skew is steep means you're collecting the skew premium — you're getting paid more than fair value for taking on downside risk. This works well in high-IV-rank, steep-skew environments. Define your risk with a spread.</Callout>
          <P><strong>Skew as a sentiment gauge:</strong> Monitor how the skew changes day to day. If put skew was steep yesterday and has flattened today, it means institutions are no longer buying protection aggressively — a potential sentiment shift worth watching. If call skew spikes suddenly, large players may be buying upside — a bullish signal.</P>
          <P><strong>Calendar-adjusted skew:</strong> Compare the skew across different expiry tabs. If the near-term expiry has steeper put skew than the far-term, near-term downside fear dominates. If far-term put skew is steeper, there's concern about a later catalyst.</P>
        </Section>

        <Section title="Term Structure — What It Shows">
          <P>The term structure plots ATM IV (Y axis) against Days to Expiry (X axis). Each dot is one expiry's ATM implied volatility. It shows how the market prices uncertainty across time.</P>
          <Callout type="signal">Upward sloping (near low IV, far high IV) = normal contango. The market expects more cumulative uncertainty further out — makes intuitive sense. This is the default healthy state. Options with longer life cost more in IV terms.</Callout>
          <Callout type="warning">Downward sloping (near high IV, far low IV) = backwardation. A near-term event dominates all pricing. Earnings, RBI policy decisions, election results, budget announcements — all create backwardation because the market cannot see past the event. The near-term expiry that brackets the event gets the elevated IV, everything after it is calm.</Callout>
          <P><strong>Kink or bump in the middle:</strong> A specific intermediate expiry is elevated above the smooth curve. Look at what event falls within that expiry window — the bump IS the market pricing that event. This is how you identify which dates the market considers risky.</P>
          <P><strong>Flat term structure:</strong> All expiries priced at similar IV. Either the market is uniformly calm or there's no clear dominant catalyst. Can happen in long low-volatility periods.</P>
        </Section>

        <Section title="Implementing Term Structure in Decision-Making">
          <P><strong>Calendar spreads:</strong> Sell the expiry with the highest IV (most expensive), buy the expiry with lower IV. You profit if the expensive expiry's IV mean-reverts toward the cheaper one. This is a pure volatility trade, direction-neutral. Works best when term structure is inverted (near high, far low) and you expect the near-term event to resolve without a large move.</P>
          <P><strong>Expiry selection for directional trades:</strong> If you have a directional view, pick your expiry carefully. Buying options in a high-IV expiry is expensive — you need a larger move to profit. If a low-IV expiry exists just before an event you're trading, it may offer better risk/reward than the event expiry itself (if your view is that the event produces a move before the expiry).</P>
          <Callout type="info">The most actionable insight from term structure: when backwardation appears (near-term IV suddenly spikes above far-term), something specific is being priced for that near-term window. If you don't know what the event is, find out before trading that expiry.</Callout>
        </Section>
      </InfoPanel>

    </div>
  )
}
