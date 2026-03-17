export default function ErrorBanner({ message, onRetry }) {
  return (
    <div className="card flex items-center justify-between gap-4 my-2"
         style={{ borderColor: 'rgba(220,38,38,0.3)', background: 'rgba(220,38,38,0.04)' }}>
      <span className="text-sm mono" style={{ color: '#DC2626' }}>⚠ {message}</span>
      {onRetry && (
        <button
          onClick={onRetry}
          className="text-xs mono rounded px-3 py-1 transition-colors shrink-0"
          style={{ border: '1px solid #E8DDD0', color: '#7A6355' }}
          onMouseEnter={e => e.target.style.color = '#2C1810'}
          onMouseLeave={e => e.target.style.color = '#7A6355'}
        >
          Retry
        </button>
      )}
    </div>
  )
}
