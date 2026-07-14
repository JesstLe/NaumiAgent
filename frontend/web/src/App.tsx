import { useEffect } from 'react'
import { BrowserRouter } from 'react-router-dom'
import { AppRoutes } from '@/routes'
import { PlatformProvider, usePlatform } from '@/platform'
import { useLocaleStore } from '@/stores/localeStore'
import '@/i18n'
import '@/index.css'

function AppInitializer() {
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

  return <AppRoutes />
}

function App() {
  return (
    <BrowserRouter>
      <PlatformProvider>
        <AppInitializer />
      </PlatformProvider>
    </BrowserRouter>
  )
}

export default App
