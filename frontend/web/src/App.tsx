import { useEffect } from 'react'
import { BrowserRouter, useLocation } from 'react-router-dom'
import { Web2 } from '@naumi/web2'
import { WorkspaceProvider } from '@naumi/shared/hooks/WorkspaceProvider'
import { ConnectionBootstrap } from '@/components/ConnectionBootstrap'
import { PlatformProvider, usePlatform } from '@naumi/shared/platform'
import { useLocaleStore } from '@/stores/localeStore'
import '@/i18n'
import '@/index.css'

function LocaleInitializer() {
  const location = useLocation()
  const platform = usePlatform()
  const initializeLocale = useLocaleStore((state) => state.initialize)

  useEffect(() => {
    let cancelled = false
    platform.getSetting('locale').then((savedLocale) => {
      if (!cancelled) {
        initializeLocale(savedLocale)
      }
    })
    return () => {
      cancelled = true
    }
  }, [platform, initializeLocale])

  return location.pathname === '/web2' || location.pathname.startsWith('/web2/') ? <Web2 /> : <ConnectionBootstrap />
}

function App() {
  return (
    <BrowserRouter>
      <PlatformProvider>
        <WorkspaceProvider><LocaleInitializer /></WorkspaceProvider>
      </PlatformProvider>
    </BrowserRouter>
  )
}

export default App
