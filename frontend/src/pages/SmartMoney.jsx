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
import InfoPanel, { Section, P, Callout, KV } from '../components/InfoPanel'

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

      <InfoPanel title="Understanding Open Interest, OI Change & PCR">
        <Section title="What is Open Interest?">
          <P>Open Interest (OI) is the total number of outstanding (unsettled) options contracts at a given strike. Unlike volume, which counts every trade in a session, OI only increases when a new contract is created (a buyer and seller open a fresh position) and decreases when a contract is closed or exercised. OI represents real, committed money — it's the market's structural positioning, not just intraday activity.</P>
          <Callout type="signal">The strikes with the highest OI are the most important levels on the chart. Large call OI above the spot price acts as resistance — the writers of those calls will hedge by selling the underlying if the market rises toward their strike. Large put OI below spot acts as support for the same reason. This is why markets often stall or reverse near high-OI strikes, especially approaching expiry.</Callout>
        </Section>

        <Section title="Reading the OI Butterfly Chart">
          <P>Green bars are Call OI, red bars are Put OI. The tallest bars mark the strikes where the most contracts are outstanding. Look for:</P>
          <P><strong>OI concentration zone:</strong> The cluster of strikes where both calls and puts have high OI. The market tends to stay within this zone approaching expiry because of the gravitational pull of max pain.</P>
          <P><strong>Asymmetric OI:</strong> If put OI is dramatically higher than call OI across most strikes, institutions are heavily hedged for downside. This is the normal state. If call OI is unusually high, it signals either large speculative long positioning or corporate hedging of equity portfolios.</P>
          <P><strong>Isolated spike at one strike:</strong> A single strike with dramatically higher OI than its neighbours is a significant level. Institutions have concentrated their positioning there — it acts as a strong magnet for the underlying price near expiry.</P>
        </Section>

        <Section title="Reading the OI Change Chart">
          <P>OI Change shows the delta in OI from the previous day's settlement. This is more actionable than absolute OI because it tells you what is happening <em>today</em>, not what accumulated over weeks.</P>
          <KV label="Green bar (positive OI change)" value="New contracts being created — fresh positioning entering the market. Combined with price direction tells you who is entering (see the four buildup types in Alert Engine)." />
          <KV label="Red bar (negative OI change)" value="Contracts being closed — existing positions being unwound. Could be profit-taking, stop-losses, or expiry-driven unwinding." />
          <Callout type="warning">Large OI buildup at an OTM strike suddenly is the most actionable signal. When institutions add large fresh OI at a specific strike that wasn't there yesterday, they're positioning for that level to be tested. This is what the Alert Engine is detecting and quantifying.</Callout>
        </Section>

        <Section title="PCR — Put-Call Ratio">
          <P>PCR = Total Put OI ÷ Total Call OI. It measures the aggregate directional bias of all positioning.</P>
          <KV label="PCR > 1.2 (teal)" value="More puts than calls outstanding. Heavy put positioning. Contrarian signal — if everyone is hedged for downside, the market may actually have limited downside because institutions are already protected. Markets often rally from extreme bearish PCR." />
          <KV label="PCR 0.8–1.2 (amber)" value="Balanced positioning. No strong directional signal from PCR alone." />
          <KV label="PCR < 0.8 (red)" value="More calls than puts. Heavy call positioning or lack of put hedging. Contrarian signal — excessive bullish positioning can mean the market is already fully long and vulnerable to a reversal on any negative news." />
          <Callout type="info">PCR is a contrarian indicator in extreme readings. The crowd being heavily positioned one way often precedes a move the other way, because everyone who wanted to be on that side is already in — there's no one left to push the market further.</Callout>
        </Section>

        <Section title="Max Pain">
          <P>Max Pain is the strike price at which the total payout to all option holders (both calls and puts) is minimised — or equivalently, where option writers (who are typically well-capitalised institutions) lose the least money at expiry. The calculation: for each possible strike as settlement price, compute what all outstanding calls and puts would pay out, and find the strike that minimises total payout.</P>
          <Callout type="signal">Markets have a documented tendency to gravitate toward Max Pain as expiry approaches, particularly in the final week. This is because option writers hedge dynamically, and their collective hedging activity nudges the underlying toward the level where they are most profitable. It's not manipulation — it's the natural outcome of delta hedging at scale.</Callout>
          <P>Use Max Pain as a gravitational reference. If spot is far above Max Pain and you're near expiry, there's structural pressure pulling it down. If spot is far below, there's upward pull. The closer you get to expiry, the stronger this effect becomes.</P>
        </Section>
      </InfoPanel>

      <InfoPanel title="Understanding Gamma Exposure (GEX)">
        <Section title="What is GEX?">
          <P>Gamma Exposure measures the total dollar amount of gamma that market makers (dealers) are carrying as a result of the options they've sold. Dealers are assumed to be the counterparty to most institutional and retail options flow — they're typically net short options.</P>
          <P>When dealers are short options, they must delta-hedge to stay directionally neutral. They do this by buying or selling the underlying continuously. The rate at which they need to adjust their hedge as the underlying moves is their gamma exposure. The formula: <strong>GEX = Gamma × OI × Lot Size × Spot² × 0.01</strong></P>
        </Section>

        <Section title="Positive vs Negative GEX — The Most Important Distinction">
          <Callout type="signal">Positive GEX (green bars, above the amber line): Dealers are net long gamma at this strike. When spot rises toward this strike, dealers SELL to re-hedge (they bought the underlying earlier at a lower delta). When spot falls, they BUY. This creates a self-stabilising, mean-reverting force. Markets pin near high positive GEX strikes — especially toward expiry when gamma is highest. This is why you see Nifty "stuck" near a round number for days near expiry.</Callout>
          <Callout type="warning">Negative GEX (red bars, below the amber line): Dealers are net short gamma here. When spot rises, they must BUY more (chasing the move to re-hedge). When spot falls, they must SELL (accelerating the fall). Negative GEX amplifies moves — the market trends faster and farther in negative GEX zones. This is where breakouts extend rather than reverse.</Callout>
        </Section>

        <Section title="The Zero-GEX Level (Amber Reference Line)">
          <P>The most important level on the GEX chart. The strike where Net GEX crosses from positive to negative is called the "Gamma Flip" point. Above this level (positive GEX territory), the market is dealer-stabilised. Below it (negative GEX territory), the market is dealer-destabilised.</P>
          <Callout type="warning">When spot crosses below the Gamma Flip level, the market regime changes. Mean-reversion gives way to trending. Breakdowns accelerate. Vol expands. This is often when "a normal pullback becomes a real correction" — the mechanical hedging flows change character entirely. Watch for when spot breaks the Gamma Flip on high volume.</Callout>
        </Section>

        <Section title="Implementing GEX in Decision-Making">
          <P><strong>Identifying pinning levels:</strong> The strike with the highest positive GEX is the magnetic center for the current expiry. As expiry approaches and gamma grows, the pin strengthens. Use this to identify where short-dated options straddles might be hopeless — the market may not go anywhere.</P>
          <P><strong>Trading breakouts:</strong> Before entering a breakout trade, check whether you're in positive or negative GEX territory. A breakout attempt from positive GEX is likely to fail and revert. The same breakout from negative GEX territory is more likely to extend — dealers will be forced to chase the move with you.</P>
          <P><strong>Volatility regime awareness:</strong> If aggregate GEX is deeply negative across all strikes (the entire market is in a short-gamma regime), expect higher intraday volatility, wider swings, and trend days. If aggregate GEX is strongly positive, expect lower volatility, tighter ranges, and mean-reversion.</P>
          <Callout type="info">A practical rule: in positive GEX, prefer selling options (theta strategies benefit from the pinning and low vol). In negative GEX, prefer buying options or trading momentum (the mechanical amplification works in your favour).</Callout>
        </Section>
      </InfoPanel>

    </div>
  )
}
