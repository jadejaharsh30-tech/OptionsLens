// Lazy-loads react-plotly.js so the large Plotly bundle doesn't block
// initial paint. Falls back to a spinner while the chunk loads.
import { lazy, Suspense } from 'react'
import LoadingSpinner from './LoadingSpinner'

const Plot = lazy(() => import('react-plotly.js'))

export default function PlotWrapper(props) {
  return (
    <Suspense fallback={<LoadingSpinner label="Loading chart..." />}>
      <Plot {...props} />
    </Suspense>
  )
}
