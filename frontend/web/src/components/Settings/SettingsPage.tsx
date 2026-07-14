import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Loader2, Play, Square, Save, KeyRound, Trash2 } from 'lucide-react'
import { usePlatform } from '@/platform'
import { useLocaleStore, type Locale } from '@/stores/localeStore'
import { useSessionStore } from '@/stores/sessionStore'
import { isApiException } from '@/api/ApiException'
import type { DaemonLaunchConfig, DaemonStatus } from '@/platform/PlatformAdapter'

export function SettingsPage() {
  const { t, i18n } = useTranslation()
  const platform = usePlatform()
  const { locale, setLocale } = useLocaleStore()
  const setError = useSessionStore((state) => state.setError)

  async function handleLanguageChange(next: Locale) {
    setLocale(next)
    await platform.setSetting('locale', next)
  }

  return (
    <div className="p-6 h-full overflow-y-auto space-y-8">
      <h1 className="text-xl font-semibold text-text">{t('nav.settings')}</h1>

      {/* Language */}
      <section className="space-y-3">
        <h2 className="text-sm font-medium text-text-secondary uppercase tracking-wider">
          {t('settings.language')} / Language
        </h2>
        <div className="flex gap-2">
          <button
            onClick={() => handleLanguageChange('zh-CN')}
            className={`px-3 py-1.5 rounded-md text-sm border ${
              locale === 'zh-CN'
                ? 'bg-accent text-white border-accent'
                : 'bg-bg border-border hover:bg-bg-tertiary'
            }`}
          >
            简体中文
          </button>
          <button
            onClick={() => handleLanguageChange('en-US')}
            className={`px-3 py-1.5 rounded-md text-sm border ${
              locale === 'en-US'
                ? 'bg-accent text-white border-accent'
                : 'bg-bg border-border hover:bg-bg-tertiary'
            }`}
          >
            English
          </button>
        </div>
        <div className="text-xs text-text-secondary">Current i18n language: {i18n.language}</div>
      </section>

      {/* API Token */}
      <TokenSection setError={setError} />

      {/* Daemon (native only) */}
      {platform.supportsDaemon && platform.getDaemonLaunchConfig && (
        <DaemonSection setError={setError} />
      )}
    </div>
  )
}

function TokenSection({ setError }: { setError: (msg: string | null) => void }) {
  const { t } = useTranslation()
  const platform = usePlatform()
  const [token, setToken] = useState('')
  const [hasToken, setHasToken] = useState(false)
  const [isSaving, setIsSaving] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)

  useEffect(() => {
    platform.getToken().then((existing) => {
      setHasToken(!!existing)
    })
  }, [platform])

  const handleSave = async () => {
    if (!token.trim()) return
    setIsSaving(true)
    setNotice(null)
    try {
      await platform.setToken(token.trim())
      setToken('')
      setHasToken(true)
      setNotice(t('settings.tokenSaved'))
    } catch (err) {
      setError(isApiException(err) ? err.message : String(err))
    } finally {
      setIsSaving(false)
    }
  }

  const handleRemove = async () => {
    setIsSaving(true)
    setNotice(null)
    try {
      await platform.removeToken()
      setHasToken(false)
      setNotice(t('settings.tokenRemoved'))
    } catch (err) {
      setError(isApiException(err) ? err.message : String(err))
    } finally {
      setIsSaving(false)
    }
  }

  return (
    <section className="space-y-3">
      <h2 className="flex items-center gap-2 text-sm font-medium text-text-secondary uppercase tracking-wider">
        <KeyRound className="w-4 h-4" />
        {t('settings.token')}
      </h2>
      {notice && (
        <div className="rounded-md border border-success/30 bg-success/10 px-4 py-2 text-sm text-success">
          {notice}
        </div>
      )}
      <div className="flex gap-2">
        <input
          type="password"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder={t('settings.tokenPlaceholder')}
          className="flex-1 rounded-md border border-border bg-bg px-3 py-1.5 text-sm text-text focus:outline-none focus:ring-2 focus:ring-accent/40"
        />
        <button
          type="button"
          onClick={handleSave}
          disabled={isSaving || !token.trim()}
          className="flex items-center gap-1 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white hover:bg-accent-hover disabled:opacity-50"
        >
          {isSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
          {t('settings.saveToken')}
        </button>
        {hasToken && (
          <button
            type="button"
            onClick={handleRemove}
            disabled={isSaving}
            className="flex items-center gap-1 rounded-md border border-danger px-3 py-1.5 text-sm font-medium text-danger hover:bg-danger/10 disabled:opacity-50"
          >
            <Trash2 className="w-4 h-4" />
            {t('settings.removeToken')}
          </button>
        )}
      </div>
      {hasToken && <div className="text-xs text-success">✓ {t('settings.tokenSaved')}</div>}
    </section>
  )
}

function DaemonSection({ setError }: { setError: (msg: string | null) => void }) {
  const { t } = useTranslation()
  const platform = usePlatform()
  const [config, setConfig] = useState<DaemonLaunchConfig>({
    executable: null,
    args: [],
    working_dir: null,
    port: null,
    env_vars: {},
  })
  const [status, setStatus] = useState<DaemonStatus | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [isStarting, setIsStarting] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)

  useEffect(() => {
    if (!platform.getDaemonLaunchConfig) return
    platform.getDaemonLaunchConfig().then((saved) => {
      if (saved) setConfig(saved)
    })
    if (platform.getDaemonStatus) {
      platform.getDaemonStatus().then(setStatus).catch(() => {})
    }
  }, [platform])

  const refreshStatus = async () => {
    if (!platform.getDaemonStatus) return
    try {
      const s = await platform.getDaemonStatus()
      setStatus(s)
    } catch {
      // ignore
    }
  }

  const handleSave = async () => {
    if (!platform.setDaemonLaunchConfig) return
    setIsLoading(true)
    setNotice(null)
    try {
      await platform.setDaemonLaunchConfig(config)
      setNotice(t('settings.daemonSaved'))
    } catch (err) {
      setError(isApiException(err) ? err.message : String(err))
    } finally {
      setIsLoading(false)
    }
  }

  const handleStart = async () => {
    if (!platform.startDaemon) return
    setIsStarting(true)
    setNotice(null)
    try {
      const s = await platform.startDaemon(config)
      setStatus(s)
      setNotice(s.running ? t('settings.daemonRunning') : t('settings.daemonStopped'))
    } catch (err) {
      setError(isApiException(err) ? err.message : String(err))
    } finally {
      setIsStarting(false)
    }
  }

  const handleStop = async () => {
    if (!platform.stopDaemon) return
    setIsStarting(true)
    setNotice(null)
    try {
      const s = await platform.stopDaemon()
      setStatus(s)
      setNotice(t('settings.daemonStopped'))
    } catch (err) {
      setError(isApiException(err) ? err.message : String(err))
    } finally {
      setIsStarting(false)
    }
  }

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-medium text-text-secondary uppercase tracking-wider">
          {t('settings.daemon')}
        </h2>
        {status && (
          <div className="flex items-center gap-2 text-xs">
            <span className="text-text-secondary">{t('settings.daemonStatus')}:</span>
            <span
              className={`flex items-center gap-1 font-medium ${
                status.running ? 'text-success' : 'text-text-secondary'
              }`}
            >
              <span
                className={`h-2 w-2 rounded-full ${status.running ? 'bg-success' : 'bg-text-secondary/40'}`}
              />
              {status.running ? t('settings.daemonRunning') : t('settings.daemonStopped')}
            </span>
            {status.pid != null && (
              <span className="text-text-secondary">
                {t('settings.daemonPid')}: {status.pid}
              </span>
            )}
            {status.port != null && <span className="text-text-secondary">:{status.port}</span>}
          </div>
        )}
      </div>

      {notice && (
        <div className="rounded-md border border-success/30 bg-success/10 px-4 py-2 text-sm text-success">
          {notice}
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <label className="block">
          <span className="text-xs text-text-secondary">{t('settings.daemonExecutable')}</span>
          <input
            value={config.executable ?? ''}
            onChange={(e) => setConfig({ ...config, executable: e.target.value || null })}
            placeholder="naumi / python"
            className="mt-1 w-full rounded-md border border-border bg-bg px-3 py-1.5 text-sm text-text focus:outline-none focus:ring-2 focus:ring-accent/40"
          />
        </label>
        <label className="block">
          <span className="text-xs text-text-secondary">{t('settings.daemonPort')}</span>
          <input
            type="number"
            value={config.port ?? ''}
            onChange={(e) =>
              setConfig({ ...config, port: e.target.value ? Number(e.target.value) : null })
            }
            placeholder="8765-8799"
            className="mt-1 w-full rounded-md border border-border bg-bg px-3 py-1.5 text-sm text-text focus:outline-none focus:ring-2 focus:ring-accent/40"
          />
        </label>
      </div>
      <label className="block">
        <span className="text-xs text-text-secondary">{t('settings.daemonArgs')}</span>
        <input
          value={config.args?.join(' ') ?? ''}
          onChange={(e) =>
            setConfig({ ...config, args: e.target.value ? e.target.value.split(/\s+/) : [] })
          }
          placeholder="serve --port {port}"
          className="mt-1 w-full rounded-md border border-border bg-bg px-3 py-1.5 text-sm text-text focus:outline-none focus:ring-2 focus:ring-accent/40"
        />
      </label>
      <label className="block">
        <span className="text-xs text-text-secondary">{t('settings.daemonWorkingDir')}</span>
        <input
          value={config.working_dir ?? ''}
          onChange={(e) => setConfig({ ...config, working_dir: e.target.value || null })}
          placeholder="C:\\projects\\naumiagent"
          className="mt-1 w-full rounded-md border border-border bg-bg px-3 py-1.5 text-sm text-text focus:outline-none focus:ring-2 focus:ring-accent/40"
        />
      </label>

      <div className="flex gap-2 pt-1">
        <button
          type="button"
          onClick={handleStart}
          disabled={isStarting || status?.running}
          className="flex items-center gap-1 rounded-md bg-success px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50"
        >
          {isStarting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
          {t('settings.startDaemon')}
        </button>
        <button
          type="button"
          onClick={handleStop}
          disabled={isStarting || !status?.running}
          className="flex items-center gap-1 rounded-md border border-danger px-3 py-1.5 text-sm font-medium text-danger hover:bg-danger/10 disabled:opacity-50"
        >
          <Square className="w-4 h-4" />
          {t('settings.stopDaemon')}
        </button>
        <button
          type="button"
          onClick={() => void refreshStatus()}
          className="rounded-md border border-border px-3 py-1.5 text-sm text-text hover:bg-bg-tertiary"
        >
          ↻
        </button>
        <button
          type="button"
          onClick={handleSave}
          disabled={isLoading}
          className="ml-auto flex items-center gap-1 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white hover:bg-accent-hover disabled:opacity-50"
        >
          {isLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
          {t('settings.saveDaemon')}
        </button>
      </div>
    </section>
  )
}
