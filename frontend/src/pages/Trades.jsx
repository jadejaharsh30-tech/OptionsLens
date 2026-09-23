// Trades — lifecycle, open positions and the journal.
//
// The journal panel leads with average MAE next to win rate on purpose. Win
// rate alone flatters a strategy that is being stopped out of trades that would
// have worked; MAE is what tells you the stop is too tight.

import { useCallback, useEffect, useState } from 'react'
import { useApp } from '../context/AppContext'
import client from '../api/client'
import LoadingSpinner from '../components/LoadingSpinner'
import ErrorBanner from '../components/ErrorBanner'

const CARD = { background: '#FDFAF6', border: '1px solid #EDE3D8', borderRadius: 6 }

const STATE_COLOR = {
  SIGNAL: '#A89585', PROPOSED: '#D97706', OPEN: '#0D9488',
  CLOSED: '#7A6355', JOURNALED: '#A89585', REJECTED: '#DC2626',
}

const inr = v =>
  v === null || v === undefined ? '—'
    : `${v < 0 ? '-' : ''}₹${Math.abs(v).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`

function Stat({ label, value, color, hint }) {
  return (
    <div title={hint}>
      <div className="text-xs mono" style={{ color: '#A89585' }}>{label}</div>
      <div className="text-lg mono font-semibold" style={{ color: color || '#7A6355' }}>
        {value}
      </div>
    </div>
  )
}

function JournalPanel({ stats }) {
  if (!stats || !stats.trades) {
    return (
      <div className="p-4 mb-4" style={CARD}>
        <p className="text-xs" style={{ color: '#A89585' }}>
          {stats?.note || 'No closed trades yet.'}
        </p>
      </div>
    )
  }

  const pnlColor = stats.total_pnl >= 0 ? '#0D9488' : '#DC2626'

  return (
    <div className="p-4 mb-4" style={CARD}>
      <div className="flex items-center gap-8 flex-wrap">
        <Stat label="TRADES" value={stats.trades} />
        <Stat label="WIN RATE" value={`${stats.win_rate_pct}%`} />
        <Stat label="NET P&L" value={inr(stats.total_pnl)} color={pnlColor} />
        <Stat label="EXPECTANCY" value={inr(stats.expectancy)}
              hint="Average P&L per trade, net of all charges" />
        <Stat label="PROFIT FACTOR" value={stats.profit_factor ?? '—'}
              hint="Gross wins divided by gross losses" />
        <Stat label="AVG MAE" value={inr(stats.avg_mae)} color="#DC2626"
              hint="Average worst unrealised loss before exit. If winners routinely sit deeply underwater, the stop is too tight." />
      </div>

      {stats.by_exit_reason && (
        <div className="mt-4 flex gap-4 flex-wrap">
          {Object.entries(stats.by_exit_reason).map(([reason, v]) => (
            <span key={reason} className="text-xs mono px-2 py-1 rounded"
                  style={{ background: '#FBF7F0', color: '#7A6355' }}>
              {reason}: {v.count} ({inr(v.pnl)})
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

function PortfolioPanel({ portfolio }) {
  if (!portfolio || !portfolio.open_positions) return null

  return (
    <div className="p-4 mb-4" style={CARD}>
      <div className="flex items-center gap-8 flex-wrap">
        <Stat label="OPEN" value={portfolio.open_positions} />
        <Stat label="UNREALISED" value={inr(portfolio.total_unrealized)}
              color={portfolio.total_unrealized >= 0 ? '#0D9488' : '#DC2626'} />
        <Stat label="RISK" value={inr(portfolio.committed_risk)} />
        <Stat label="NET DELTA" value={portfolio.net_delta?.toFixed(2)} />
        <Stat label="NET VEGA" value={portfolio.net_vega?.toFixed(2)}
              color={portfolio.net_vega < 0 ? '#DC2626' : '#0D9488'} />
        <Stat label="NET THETA" value={portfolio.net_theta?.toFixed(2)} />
      </div>

      {portfolio.warnings?.length > 0 && (
        <ul className="mt-3 space-y-1">
          {portfolio.warnings.map((w, i) => (
            <li key={i} className="text-xs" style={{ color: '#D97706' }}>⚠ {w}</li>
          ))}
        </ul>
      )}
    </div>
  )
}

function TradeRow({ trade }) {
  const color = STATE_COLOR[trade.state] || '#7A6355'
  const pnl = trade.realized_pnl

  return (
    <tr style={{ borderTop: '1px solid #EDE3D8' }}>
      <td className="py-2 px-3">
        <span className="text-xs mono px-1.5 py-0.5 rounded"
              style={{ background: `${color}18`, color }}>
          {trade.state}
        </span>
      </td>
      <td className="py-2 px-3 text-xs mono" style={{ color: '#7A6355' }}>
        {trade.symbol}
      </td>
      <td className="py-2 px-3 text-xs mono" style={{ color: '#7A6355' }}>
        {trade.legs.map((l, i) => (
          <div key={i}>{l.action} {l.lots}x {l.strike}{l.option_type}</div>
        ))}
      </td>
      <td className="py-2 px-3 text-xs mono" style={{ color: '#A89585' }}>
        {trade.signal_id || 'manual'}
      </td>
      <td className="py-2 px-3 text-xs mono text-right"
          style={{ color: pnl === null ? '#A89585' : pnl >= 0 ? '#0D9488' : '#DC2626' }}>
        {inr(pnl)}
      </td>
      <td className="py-2 px-3 text-xs mono text-right" style={{ color: '#DC2626' }}>
        {inr(trade.mae)}
      </td>
      <td className="py-2 px-3 text-xs mono text-right" style={{ color: '#0D9488' }}>
        {inr(trade.mfe)}
      </td>
      <td className="py-2 px-3 text-xs mono" style={{ color: '#A89585' }}>
        {trade.exit_reason || '—'}
      </td>
      <td className="py-2 px-3 text-xs" style={{ color: '#A89585', maxWidth: 260 }}>
        {trade.postmortem || trade.notes || '—'}
      </td>
    </tr>
  )
}

export default function Trades() {
  const { symbol, expiry } = useApp()

  const [trades,    setTrades]    = useState([])
  const [stats,     setStats]     = useState(null)
  const [portfolio, setPortfolio] = useState(null)
  const [filter,    setFilter]    = useState('')
  const [loading,   setLoading]   = useState(false)
  const [error,     setError]     = useState(null)

  const load = useCallback(async () => {
    setLoading(true); setError(null)
    try {
      const params = filter ? { state: filter } : {}
      const [t, j] = await Promise.all([
        client.get('/api/trades', { params }),
        client.get('/api/trades/journal'),
      ])
      setTrades(t.data.trades || [])
      setStats(j.data)

      if (expiry?.expiry) {
        try {
          const p = await client.get('/api/trades/portfolio', {
            params: { symbol, expiry_epoch: expiry.expiry, expiry_date: expiry.date },
          })
          setPortfolio(p.data)
        } catch {
          setPortfolio(null)   // no open positions is not an error
        }
      }
    } catch (e) {
      setError(e.response?.data?.detail || e.message || 'Failed to load trades')
    } finally {
      setLoading(false)
    }
  }, [filter, symbol, expiry])

  useEffect(() => { load() }, [load])

  return (
    <div className="max-w-6xl">
      <h1 className="text-lg font-display font-semibold mb-1" style={{ color: '#7A6355' }}>
        Trades
      </h1>
      <p className="text-xs mb-4" style={{ color: '#A89585' }}>
        Paper trading. Every position is sized against a risk budget and closed
        by rule, and every rejected signal is kept with its reason.
      </p>

      <PortfolioPanel portfolio={portfolio} />
      <JournalPanel stats={stats} />

      <div className="flex items-center gap-2 mb-3">
        {['', 'OPEN', 'CLOSED', 'JOURNALED', 'REJECTED'].map(s => (
          <button
            key={s || 'ALL'}
            onClick={() => setFilter(s)}
            className="text-xs mono px-3 py-1 rounded transition-colors"
            style={filter === s
              ? { background: 'rgba(13,148,136,0.15)', color: '#0D9488',
                  border: '1px solid rgba(13,148,136,0.35)' }
              : { background: 'transparent', color: '#A89585',
                  border: '1px solid #EDE3D8' }}
          >
            {s || 'ALL'}
          </button>
        ))}
        <button onClick={load} className="text-xs mono px-3 py-1 rounded"
                style={{ background: 'transparent', color: '#A89585',
                         border: '1px solid #EDE3D8' }}>
          ↻
        </button>
      </div>

      {error && <ErrorBanner message={error} />}
      {loading && <LoadingSpinner />}

      {!loading && trades.length === 0 && (
        <div className="p-4" style={CARD}>
          <p className="text-xs" style={{ color: '#A89585' }}>
            No trades yet. Propose one via <code>POST /api/trades/propose</code>.
          </p>
        </div>
      )}

      {trades.length > 0 && (
        <div className="overflow-x-auto" style={CARD}>
          <table className="w-full" style={{ borderCollapse: 'collapse' }}>
            <thead>
              <tr className="text-xs mono" style={{ color: '#A89585' }}>
                <th className="py-2 px-3 text-left">STATE</th>
                <th className="py-2 px-3 text-left">SYMBOL</th>
                <th className="py-2 px-3 text-left">LEGS</th>
                <th className="py-2 px-3 text-left">SIGNAL</th>
                <th className="py-2 px-3 text-right">P&L</th>
                <th className="py-2 px-3 text-right" title="Worst unrealised loss">MAE</th>
                <th className="py-2 px-3 text-right" title="Best unrealised gain">MFE</th>
                <th className="py-2 px-3 text-left">EXIT</th>
                <th className="py-2 px-3 text-left">NOTES</th>
              </tr>
            </thead>
            <tbody>
              {trades.map(t => <TradeRow key={t.trade_id} trade={t} />)}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
