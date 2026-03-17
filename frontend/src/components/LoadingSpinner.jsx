export default function LoadingSpinner({ label = 'Loading...' }) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-20">
      <div className="spinner" />
      <span className="text-sm mono" style={{ color: '#A89585' }}>{label}</span>
    </div>
  )
}
