import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { GitBranch, Trash2, FolderOpen, Terminal, Loader2, Check } from 'lucide-react'
import { useWorkbenchConnection } from '@/hooks/useWorkbenchConnection'
import { useSessionStore } from '@/stores/sessionStore'
import { usePlatform } from '@/platform'
import { isApiException } from '@/api/ApiException'
import { formatDate } from '@/utils/formatDate'
import type { Worktree, WorktreeStatus } from '@/api/types'

const STATUS_COLOR: Record<WorktreeStatus, string> = {
  clean: 'bg-success/15 text-success',
  dirty: 'bg-warning/15 text-warning',
  missing: 'bg-danger/15 text-danger',
  kept: 'bg-info/15 text-info',
}

export function WorktreesPage() {
  const { t } = useTranslation()
  const { client, currentSessionId, snapshot } = useWorkbenchConnection()
  const platform = usePlatform()
  const setError = useSessionStore((state) => state.setError)
  const [worktrees, setWorktrees] = useState<Worktree[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const [actionName, setActionName] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  // Seed from snapshot, then refresh from the API.
  useEffect(() => {
    setWorktrees(snapshot?.worktrees ?? [])
  }, [snapshot])

  useEffect(() => {
    if (!client || !currentSessionId) {
      setWorktrees([])
      return
    }
    let cancelled = false
    setIsLoading(true)
    client
      .fetchWorktrees(currentSessionId, { limit: 100 })
      .then((resp) => {
        if (!cancelled) setWorktrees(resp.worktrees)
      })
      .catch((err) => {
        if (!cancelled) setError(isApiException(err) ? err.message : String(err))
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [client, currentSessionId, setError])

  const handleDelete = async (worktree: Worktree) => {
    if (!client || !currentSessionId) return
    const confirmed = window.confirm(t('worktrees.deleteConfirm', { name: worktree.name }))
    if (!confirmed) return
    setActionName(worktree.name)
    setNotice(null)
    try {
      await client.deleteWorktree(currentSessionId, worktree.name)
      setWorktrees((prev) => prev.filter((w) => w.name !== worktree.name))
      setNotice(t('worktrees.deleteSuccess'))
    } catch (err) {
      setError(isApiException(err) ? err.message : String(err))
    } finally {
      setActionName(null)
    }
  }

  const handleKeep = async (worktree: Worktree) => {
    if (!client || !currentSessionId) return
    setActionName(worktree.name)
    setNotice(null)
    try {
      const updated = await client.keepWorktree(currentSessionId, worktree.name)
      setWorktrees((prev) => prev.map((w) => (w.name === updated.name ? updated : w)))
      setNotice(t('worktrees.keepSuccess'))
    } catch (err) {
      setError(isApiException(err) ? err.message : String(err))
    } finally {
      setActionName(null)
    }
  }

  const handleOpenExplorer = async (worktree: Worktree) => {
    if (!platform.openInExplorer) {
      setError(t('worktrees.notSupported'))
      return
    }
    try {
      await platform.openInExplorer(worktree.path)
    } catch (err) {
      setError(isApiException(err) ? err.message : String(err))
    }
  }

  const handleOpenTerminal = async (worktree: Worktree) => {
    if (!platform.openInTerminal) {
      setError(t('worktrees.notSupported'))
      return
    }
    try {
      await platform.openInTerminal(worktree.path)
    } catch (err) {
      setError(isApiException(err) ? err.message : String(err))
    }
  }

  if (!currentSessionId) {
    return (
      <div className="p-6 h-full overflow-y-auto">
        <h1 className="text-xl font-semibold text-text mb-4">{t('nav.worktrees')}</h1>
        <p className="text-text-secondary">{t('worktrees.empty')}</p>
      </div>
    )
  }

  return (
    <div className="p-6 h-full overflow-y-auto">
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-2">
          <h1 className="text-xl font-semibold text-text">{t('nav.worktrees')}</h1>
          {isLoading && <Loader2 className="w-4 h-4 animate-spin text-text-secondary" />}
        </div>
        <span className="text-sm text-text-secondary">{worktrees.length} worktrees</span>
      </div>

      {notice && (
        <div className="mb-4 rounded-md border border-success/30 bg-success/10 px-4 py-2 text-sm text-success">
          {notice}
        </div>
      )}

      {worktrees.length === 0 ? (
        <div className="text-text-secondary">{t('worktrees.empty')}</div>
      ) : (
        <div className="space-y-3">
          {worktrees.map((worktree) => {
            const isBusy = actionName === worktree.name
            return (
              <div key={worktree.name} className="rounded-lg border border-border bg-panel p-4 shadow-sm">
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <GitBranch className="w-4 h-4 text-text-secondary" />
                      <span className="font-medium text-text">{worktree.name}</span>
                      <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_COLOR[worktree.status]}`}>
                        {worktree.status}
                      </span>
                    </div>
                    <div className="mt-1 text-sm text-text-secondary">{worktree.branch}</div>
                    <div className="mt-1 text-xs text-text-secondary font-mono break-all">{worktree.path}</div>
                    <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-xs text-text-secondary">
                      <span>{formatDate(worktree.created_at)}</span>
                      {worktree.commits_ahead > 0 && (
                        <span className="text-info">+{worktree.commits_ahead} commits</span>
                      )}
                    </div>
                    {worktree.dirty_files > 0 && (
                      <div className="mt-1 text-xs text-danger">
                        {t('worktrees.dirtyFiles', { count: worktree.dirty_files })}
                      </div>
                    )}
                    {worktree.kept_reason && (
                      <div className="mt-1 rounded bg-bg-tertiary px-2 py-1 text-xs text-text-secondary">
                        {worktree.kept_reason}
                      </div>
                    )}
                  </div>
                  <div className="flex flex-col items-end gap-2">
                    {/* Shell actions (native only) */}
                    <div className="flex gap-1">
                      {platform.supportsShell && (
                        <>
                          <button
                            type="button"
                            onClick={() => handleOpenExplorer(worktree)}
                            title={t('worktrees.openExplorer')}
                            className="rounded-md border border-border p-1.5 text-text-secondary hover:text-accent hover:bg-bg-tertiary"
                          >
                            <FolderOpen className="w-4 h-4" />
                          </button>
                          <button
                            type="button"
                            onClick={() => handleOpenTerminal(worktree)}
                            title={t('worktrees.openTerminal')}
                            className="rounded-md border border-border p-1.5 text-text-secondary hover:text-accent hover:bg-bg-tertiary"
                          >
                            <Terminal className="w-4 h-4" />
                          </button>
                        </>
                      )}
                    </div>
                    {/* Keep / Remove */}
                    <div className="flex gap-2">
                      {worktree.removable && (
                        <button
                          type="button"
                          onClick={() => handleKeep(worktree)}
                          disabled={isBusy}
                          title={t('worktrees.keep')}
                          className="flex items-center gap-1 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium text-text hover:bg-bg-tertiary disabled:opacity-50"
                        >
                          {isBusy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Check className="w-3.5 h-3.5" />}
                          {t('worktrees.keep')}
                        </button>
                      )}
                      {worktree.removable && (
                        <button
                          type="button"
                          onClick={() => handleDelete(worktree)}
                          disabled={isBusy}
                          title={t('worktrees.delete')}
                          className="rounded-md border border-border p-1.5 text-text-secondary hover:text-danger hover:border-danger disabled:opacity-50"
                        >
                          {isBusy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Trash2 className="w-4 h-4" />}
                        </button>
                      )}
                    </div>
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
