import { lazy, Suspense } from 'react'
import { BrowserRouter, useLocation } from 'react-router-dom'
import { WorkspaceProvider } from '@naumi/shared/hooks/WorkspaceProvider'
import { PlatformProvider } from '@naumi/shared/platform'
import '@/index.css'

const Web2 = lazy(() => import('@naumi/web2').then((module) => ({ default: module.Web2 })))
const LegacyApp = lazy(() => import('@/LegacyApp').then((module) => ({ default: module.LegacyApp })))

function AppLoading() {
  return <main aria-busy="true" aria-label="正在加载界面" style={{ minHeight: '100vh', background: '#f7f8f5' }} />
}

function AppSurface() {
  const location = useLocation()
  const launchedView = new URLSearchParams(location.search).get('naumiView')
  const web2 = launchedView === 'web2' || location.pathname === '/web2' || location.pathname.startsWith('/web2/')
  return <Suspense fallback={<AppLoading />}>{web2 ? <Web2 /> : <LegacyApp />}</Suspense>
}

function App() {
  return (
    <BrowserRouter>
      <PlatformProvider>
        <WorkspaceProvider><AppSurface /></WorkspaceProvider>
      </PlatformProvider>
    </BrowserRouter>
  )
}

export default App
