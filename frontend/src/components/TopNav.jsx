// Top navigation bar — Golden Hour theme
// Deep warm brown bar with cream text and teal/amber accents
import { useEffect, useState } from 'react'
import { NavLink } from 'react-router-dom'
import { useApp } from '../context/AppContext'
import client from '../api/client'
import RecorderStatus from './RecorderStatus'

const NAV_TABS = [
  { to: '/',              label: 'Market Structure', end: true  },
  { to: '/smart-money',   label: 'Smart Money',      end: false },
  { to: '/position-lab',  label: 'Position Lab',     end: false },
  { to: '/alert-engine',  label: 'Alert Engine',     end: false },
  { to: '/research',      label: 'Research',         end: false },
]

export default function TopNav() {
  const { symbol, setSymbol, symbols, expiry, setExpiry, spot, clearToken } = useApp()
  const [expiries,    setExpiries]    = useState([])
  const [lastUpdated, setLastUpdated] = useState(null)

  useEffect(() => {
    if (!symbol) return
    client.get(`/api/expiries/${symbol}`)
      .then(res => {
        const list = res.data.expiries || []
        setExpiries(list)
        if (list.length > 0) setExpiry(list[0])
        setLastUpdated(new Date())
      })
      .catch(() => setExpiries([]))
  }, [symbol, setExpiry])

  const timeStr = lastUpdated
    ? lastUpdated.toLocaleTimeString('en-IN', {
        timeZone: 'Asia/Kolkata',
        hour: '2-digit', minute: '2-digit',
      })
    : null

  return (
    <nav
      className="h-14 flex items-center gap-4 px-5 shrink-0"
      style={{
        background: '#2C1810',
        boxShadow: '0 2px 8px rgba(44,24,16,0.18)',
      }}
    >
      {/* Brand */}
      <span className="font-display font-semibold text-base tracking-wide shrink-0"
            style={{ color: '#FBF7F0', letterSpacing: '0.04em' }}>
        Options<span style={{ color: '#D97706' }}>Lens</span>
      </span>

      <div className="w-px h-5 shrink-0" style={{ background: 'rgba(251,247,240,0.15)' }} />

      {/* Symbol selector */}
      <select
        value={symbol}
        onChange={e => setSymbol(e.target.value)}
        className="text-xs mono rounded px-2 py-1.5 focus:outline-none cursor-pointer"
        style={{
          background: 'rgba(251,247,240,0.08)',
          border: '1px solid rgba(251,247,240,0.15)',
          color: '#FBF7F0',
        }}
      >
        {symbols.length > 0
          ? symbols.map(s => <option key={s.key} value={s.key}
              style={{ background: '#2C1810', color: '#FBF7F0' }}>{s.key}</option>)
          : <option style={{ background: '#2C1810', color: '#FBF7F0' }}>NIFTY</option>
        }
      </select>

      {/* Expiry selector */}
      <select
        value={expiry?.expiry || ''}
        onChange={e => {
          const found = expiries.find(ex => String(ex.expiry) === e.target.value)
          if (found) setExpiry(found)
        }}
        className="text-xs mono rounded px-2 py-1.5 focus:outline-none cursor-pointer"
        style={{
          background: 'rgba(251,247,240,0.08)',
          border: '1px solid rgba(251,247,240,0.15)',
          color: '#FBF7F0',
        }}
      >
        {expiries.length > 0
          ? expiries.map(ex => (
              <option key={ex.expiry} value={String(ex.expiry)}
                style={{ background: '#2C1810', color: '#FBF7F0' }}>{ex.date}</option>
            ))
          : <option style={{ background: '#2C1810', color: '#FBF7F0' }}>Loading…</option>
        }
      </select>

      {/* Spot price */}
      {spot && (
        <div className="flex items-baseline gap-1.5 shrink-0">
          <span className="text-xs mono" style={{ color: 'rgba(251,247,240,0.45)' }}>SPOT</span>
          <span className="text-sm mono font-semibold" style={{ color: '#FBF7F0' }}>
            ₹{spot.toLocaleString('en-IN', { maximumFractionDigits: 2 })}
          </span>
        </div>
      )}

      {/* Timestamp */}
      {timeStr && (
        <span className="text-xs mono shrink-0" style={{ color: 'rgba(251,247,240,0.30)' }}>
          {timeStr} IST
        </span>
      )}

      {/* Recorder health — always visible; silence is the failure mode */}
      <RecorderStatus />

      <div className="flex-1" />

      {/* Page tabs */}
      <div className="flex items-center gap-0.5">
        {NAV_TABS.map(tab => (
          <NavLink
            key={tab.to}
            to={tab.to}
            end={tab.end}
            className={({ isActive }) =>
              `px-4 py-1.5 text-xs rounded transition-all duration-150 ${
                isActive ? 'font-medium' : ''
              }`
            }
            style={({ isActive }) => isActive
              ? { background: 'rgba(217,119,6,0.20)', color: '#D97706',
                  border: '1px solid rgba(217,119,6,0.35)' }
              : { color: 'rgba(251,247,240,0.55)',
                  border: '1px solid transparent' }
            }
          >
            {tab.label}
          </NavLink>
        ))}
      </div>

      <div className="w-px h-5 shrink-0" style={{ background: 'rgba(251,247,240,0.15)' }} />

      {/* Disconnect */}
      <button
        onClick={clearToken}
        title="Clear token and reconnect"
        className="text-xs mono transition-colors shrink-0 px-1"
        style={{ color: 'rgba(251,247,240,0.35)' }}
        onMouseEnter={e => e.target.style.color = '#DC2626'}
        onMouseLeave={e => e.target.style.color = 'rgba(251,247,240,0.35)'}
      >
        ⏻
      </button>
    </nav>
  )
}
