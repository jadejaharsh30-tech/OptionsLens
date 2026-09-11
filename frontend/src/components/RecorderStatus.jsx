// Recorder health indicator — lives in TopNav, visible on every page.
//
// The recorder's dangerous failure is silence: it stops, nobody notices, and
// weeks of unrecoverable market data are gone. So this is deliberately always
// on screen rather than tucked into a settings page, and it shows the last
// write time — a green dot with a stale timestamp is the thing to catch.

import { useEffect, useState } from 'react'
import client from '../api/client'

const POLL_MS = 30_000

// Phases come from the backend's CAS-aware session model.
const PHASE_LABEL = {
  CLOSED:     'Closed',
  PRE_OPEN:   'Pre-open',
  CONTINUOUS: 'Open',
  CAS_WINDOW: 'Auction',
  POST_CAS:   'Post-auction',
}

function dotColor({ running, session_phase, last_error, stale }) {
  if (last_error) return '#DC2626'
  if (!running)   return '#A89585'
  if (stale)      return '#D97706'   // running but not writing — the bad case
  if (session_phase === 'CLOSED') return '#A89585'
  return '#0D9488'
}

export default function RecorderStatus() {
  const [status, setStatus] = useState(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let cancelled = false
    const poll = () => {
      client.get('/api/recorder/status')
        .then(res => { if (!cancelled) { setStatus(res.data); setFailed(false) } })
        .catch(() => { if (!cancelled) setFailed(true) })
    }
    poll()
    const id = setInterval(poll, POLL_MS)
    return () => { cancelled = true; clearInterval(id) }
  }, [])

  if (failed || !status) return null

  // "Should be writing but hasn't in 5 minutes" is the condition worth flagging.
  const lastWrite = status.last_write_at ? new Date(status.last_write_at) : null
  const ageSec    = lastWrite ? (Date.now() - lastWrite.getTime()) / 1000 : null
  const shouldBeWriting = status.running && status.session_phase !== 'CLOSED'
  const stale = shouldBeWriting && (ageSec === null || ageSec > 300)

  const color = dotColor({ ...status, stale })
  const phase = PHASE_LABEL[status.session_phase] || status.session_phase

  const title = [
    `Recorder: ${status.running ? 'running' : 'stopped'}`,
    `Session: ${phase}`,
    status.last_write_at ? `Last write: ${lastWrite.toLocaleTimeString('en-IN')}` : 'No writes yet',
    `Rows this session: ${status.rows_written?.toLocaleString('en-IN') ?? 0}`,
    status.last_error ? `Error: ${status.last_error}` : null,
    stale ? 'WARNING: running but not writing' : null,
  ].filter(Boolean).join('\n')

  return (
    <div className="flex items-center gap-1.5 shrink-0" title={title}>
      <span
        className="inline-block rounded-full"
        style={{
          width: 7, height: 7, background: color,
          boxShadow: color === '#0D9488' ? '0 0 6px rgba(13,148,136,0.8)' : 'none',
        }}
      />
      <span className="text-xs mono" style={{ color: 'rgba(251,247,240,0.45)' }}>
        REC
      </span>
      <span className="text-xs mono" style={{ color: 'rgba(251,247,240,0.30)' }}>
        {status.rows_written > 0
          ? status.rows_written.toLocaleString('en-IN')
          : phase}
      </span>
    </div>
  )
}
