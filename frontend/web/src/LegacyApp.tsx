import { useEffect } from 'react'
import { usePlatform } from '@naumi/shared/platform'
import { ConnectionBootstrap } from '@/components/ConnectionBootstrap'
import { useLocaleStore } from '@/stores/localeStore'
import '@/i18n'

export function LegacyApp() {
  const platform = usePlatform()
  const initializeLocale = useLocaleStore((state) => state.initialize)

  useEffect(() => {
    let cancelled = false
    void platform.getSetting('locale').then((savedLocale) => {
      if (!cancelled) initializeLocale(savedLocale)
    })
    return () => {
      cancelled = true
    }
  }, [platform, initializeLocale])

  return <ConnectionBootstrap />
}
