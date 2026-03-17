export default function IVRankGauge({ ivRank, currentIV, symbol }) {
  const rank    = ivRank ?? 0
  const hasData = ivRank != null

  const radius = 38
  const cx = 54, cy = 54
  const arcLen = Math.PI * radius
  const filled = (rank / 100) * arcLen

  // Golden Hour colours: teal for low, amber for mid, red for high
  const color = !hasData ? '#E8DDD0'
    : rank < 30 ? '#0D9488'
    : rank < 60 ? '#D97706'
    : '#DC2626'

  const label = !hasData ? 'NO DATA'
    : rank < 30 ? 'LOW IV'
    : rank < 60 ? 'MED IV'
    : 'HIGH IV'

  const r = radius
  const d = `M ${cx - r} ${cy} A ${r} ${r} 0 0 1 ${cx + r} ${cy}`

  return (
    <div className="card flex flex-col items-center gap-1 py-3"
         style={{ minWidth: '130px' }}>
      <span className="text-xs mono uppercase tracking-wider mb-1"
            style={{ color: '#A89585' }}>
        {symbol}
      </span>

      <svg width="108" height="64" viewBox="0 0 108 64" overflow="visible">
        {/* Track */}
        <path d={d} fill="none" stroke="#E8DDD0" strokeWidth="9" strokeLinecap="round" />
        {/* Fill */}
        <path d={d} fill="none" stroke={color} strokeWidth="9" strokeLinecap="round"
          strokeDasharray={`${hasData ? filled : 0} ${arcLen}`}
          style={{ transition: 'stroke-dasharray 0.7s cubic-bezier(0.4,0,0.2,1)' }}
        />
        {/* Rank number */}
        <text x={cx} y={cy - 4} textAnchor="middle" fill="#2C1810"
              fontSize="22" fontFamily="Playfair Display, serif" fontWeight="600">
          {hasData ? Math.round(rank) : '—'}
        </text>
        {/* Label */}
        <text x={cx} y={cy + 13} textAnchor="middle" fill={color}
              fontSize="8" fontFamily="IBM Plex Mono, monospace" letterSpacing="1.5">
          {label}
        </text>
      </svg>

      <div className="text-center mt-1">
        <div className="text-xs" style={{ color: '#A89585' }}>Current IV</div>
        <div className="mono text-sm font-semibold" style={{ color }}>
          {currentIV != null ? `${currentIV.toFixed(1)}%` : '—'}
        </div>
      </div>
    </div>
  )
}
