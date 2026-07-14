import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { createPlatformAdapter, type PlatformAdapter } from '@/platform'

const PlatformContext = createContext<PlatformAdapter | null>(null)

export function PlatformProvider({ children }: { children: ReactNode }) {
  const [adapter] = useState(() => createPlatformAdapter())

  useEffect(() => {
    adapter.log('info', 'Platform adapter initialized')
  }, [adapter])

  return <PlatformContext.Provider value={adapter}>{children}</PlatformContext.Provider>
}

export function usePlatform(): PlatformAdapter {
  const adapter = useContext(PlatformContext)
  if (!adapter) {
    throw new Error('usePlatform must be used within a PlatformProvider')
  }
  return adapter
}
