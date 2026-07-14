import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useWorkbenchConnection } from '@/hooks/useWorkbenchConnection'
import { useSessionStore } from '@/stores/sessionStore'
import { AppRoutes } from '@/routes'

export function ConnectionBootstrap() {
  const { t } = useTranslation()
  const setSnapshot = useSessionStore((state) => state.setSnapshot)
  const connectionOptions = useMemo(
    () => ({
      onSnapshot: setSnapshot,
    }),
    [setSnapshot],
  )
  const { status, bootstrap, error, isReady } = useWorkbenchConnection(connectionOptions)
  const [isRetrying, setIsRetrying] = useState(false)

  useEffect(() => {
    bootstrap().catch(() => {
      // Errors are surfaced via the error state.
    })
  }, [bootstrap])

  const handleRetry = async () => {
    setIsRetrying(true)
    try {
      await bootstrap()
    } finally {
      setIsRetrying(false)
    }
  }

  if (!isReady && !error) {
    return (
      <div className="flex h-screen items-center justify-center bg-neutral-50 text-neutral-700">
        <div className="text-center">
          <div className="mb-4 h-8 w-8 animate-spin rounded-full border-2 border-neutral-300 border-t-blue-600 mx-auto"></div>
          <p>{t('connection.connecting')}</p>
        </div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex h-screen items-center justify-center bg-neutral-50 text-neutral-700">
        <div className="max-w-md rounded-lg border border-red-200 bg-white p-6 text-center shadow-sm">
          <h2 className="mb-2 text-lg font-semibold text-red-600">{t('connection.failed')}</h2>
          <p className="mb-4 text-sm text-neutral-600">{error}</p>
          <button
            type="button"
            onClick={handleRetry}
            disabled={isRetrying}
            className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
          >
            {isRetrying ? t('connection.retrying') : t('connection.retry')}
          </button>
        </div>
      </div>
    )
  }

  return (
    <>
      {!status.isConnected && (
        <div className="bg-amber-50 px-4 py-2 text-center text-sm text-amber-800">
          {t('connection.reconnecting')}
        </div>
      )}
      <AppRoutes />
    </>
  )
}
