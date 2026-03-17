// App shell — conditionally renders TokenGate or the full dashboard
import { Outlet } from 'react-router-dom'
import { useApp } from './context/AppContext'
import TokenGate from './components/TokenGate'
import TopNav from './components/TopNav'
import AlertToast from './components/AlertToast'

export default function App() {
  const { tokenValid } = useApp()

  if (!tokenValid) return <TokenGate />

  return (
    <div className="min-h-screen flex flex-col" style={{ background: '#FBF7F0' }}>
      <TopNav />
      <AlertToast />
      <main className="flex-1 p-5 overflow-auto">
        <Outlet />
      </main>
    </div>
  )
}
