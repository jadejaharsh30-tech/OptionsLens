// optionslens/frontend/src/components/InfoPanel.jsx
// Collapsible knowledge panel — shown below charts/sections.
// Warm cream background, amber left border, Playfair Display headers.
// Stays closed by default so it doesn't dominate the UI.

export default function InfoPanel({ title, children, defaultOpen = false }) {
  return (
    <details
      open={defaultOpen}
      style={{ marginTop: 8 }}
    >
      <summary
        className="mono text-xs cursor-pointer select-none flex items-center gap-2"
        style={{ color: '#A89585', listStyle: 'none', outline: 'none' }}
      >
        <span
          style={{
            display: 'inline-block',
            width: 14, height: 14,
            borderRadius: '50%',
            background: 'rgba(200,134,10,0.15)',
            border: '1px solid rgba(200,134,10,0.35)',
            textAlign: 'center',
            lineHeight: '12px',
            fontSize: 9,
            color: '#C8860A',
            fontWeight: 700,
            flexShrink: 0,
          }}
        >
          i
        </span>
        <span style={{ color: '#A89585' }}>{title}</span>
        <span style={{ color: '#D4C4B0', fontSize: 9, marginLeft: 2 }}>
          click to expand
        </span>
      </summary>

      <div
        style={{
          marginTop: 10,
          padding: '18px 20px',
          background: 'rgba(251,247,240,0.7)',
          border: '1px solid #E8DDD0',
          borderLeft: '3px solid #C8860A',
          borderRadius: '0 8px 8px 0',
          lineHeight: 1.7,
        }}
      >
        {children}
      </div>
    </details>
  )
}

// ── Shared typography helpers used inside InfoPanel ───────────────────────────

export function Section({ title, children }) {
  return (
    <div style={{ marginBottom: 18 }}>
      <div
        className="font-display"
        style={{
          color: '#2C1810',
          fontSize: 13,
          fontWeight: 600,
          marginBottom: 6,
          borderBottom: '1px solid #E8DDD0',
          paddingBottom: 4,
        }}
      >
        {title}
      </div>
      <div style={{ color: '#5A4030', fontSize: 13 }}>
        {children}
      </div>
    </div>
  )
}

export function P({ children }) {
  return (
    <p style={{ color: '#5A4030', fontSize: 13, marginBottom: 8, lineHeight: 1.7 }}>
      {children}
    </p>
  )
}

export function Callout({ type = 'info', children }) {
  const styles = {
    info:    { bg: 'rgba(13,148,136,0.06)',  border: '#0D9488', icon: '→', color: '#0D9488' },
    warning: { bg: 'rgba(200,134,10,0.06)',  border: '#C8860A', icon: '⚡', color: '#C8860A' },
    signal:  { bg: 'rgba(44,24,16,0.04)',    border: '#7A6355', icon: '◆', color: '#7A6355' },
  }
  const s = styles[type] || styles.info
  return (
    <div
      style={{
        background: s.bg,
        borderLeft: `3px solid ${s.border}`,
        padding: '8px 12px',
        marginBottom: 8,
        borderRadius: '0 4px 4px 0',
        fontSize: 12.5,
        color: '#5A4030',
        lineHeight: 1.65,
      }}
    >
      <span style={{ color: s.color, marginRight: 6, fontWeight: 700 }}>{s.icon}</span>
      {children}
    </div>
  )
}

export function KV({ label, value }) {
  return (
    <div style={{ display: 'flex', gap: 8, marginBottom: 4, fontSize: 12.5 }}>
      <span className="mono" style={{ color: '#A89585', minWidth: 140, flexShrink: 0 }}>{label}</span>
      <span style={{ color: '#2C1810' }}>{value}</span>
    </div>
  )
}
