import { useEffect, useState, useMemo, useCallback } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import {
  Folder,
  Terminal,
  GitBranch,
  CheckSquare,
  ChevronDown,
  Plus,
  RefreshCw,
  FileQuestion,
} from 'lucide-react'
import { useWorkbenchConnection } from '@/hooks/useWorkbenchConnection'
import { useSessionStore } from '@/stores/sessionStore'
import { usePlatform } from '@/platform/PlatformContext'
import { isApiException } from '@/api/ApiException'
import { WindowControls } from './WindowControls'
import type { GitDiffResponse, GitDiffFile } from '@/api/types'

type DiffFilter = 'all' | 'staged' | 'unstaged'

const STATUS_LABEL: Record<string, string> = {
  M: '修改',
  A: '新增',
  D: '删除',
  R: '重命名',
  C: '复制',
  U: '更新',
  '?': '未跟踪',
}

export function ReviewPanel() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const { client, currentSessionId, status } = useWorkbenchConnection()
  const platform = usePlatform()
  const sessions = useSessionStore((state) => state.sessions)
  const currentSession = useMemo(
    () => sessions.find((s) => s.id === currentSessionId),
    [sessions, currentSessionId],
  )

  const [diff, setDiff] = useState<GitDiffResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState<DiffFilter>('unstaged')
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const workspaceRoot = status.daemon?.workspace_root

  const loadDiff = useCallback(async () => {
    if (!client || !currentSessionId) {
      setDiff(null)
      setError(null)
      return
    }
    setLoading(true)
    setError(null)
    try {
      const next = await client.fetchGitDiff(currentSessionId)
      setDiff(next)
      if (next.error) {
        setError(next.error)
      }
    } catch (err) {
      setError(isApiException(err) ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [client, currentSessionId])

  useEffect(() => {
    void loadDiff()
  }, [loadDiff])

  const filteredFiles = useMemo(() => {
    if (!diff) return []
    if (filter === 'all') return diff.files
    return diff.files.filter((f) => f.stage === filter)
  }, [diff, filter])

  const toggleFile = useCallback((path: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })
  }, [])

  const openExplorer = useCallback(() => {
    if (workspaceRoot && platform.openInExplorer) {
      void platform.openInExplorer(workspaceRoot)
    }
  }, [platform, workspaceRoot])

  const openTerminal = useCallback(() => {
    if (workspaceRoot && platform.openInTerminal) {
      void platform.openInTerminal(workspaceRoot)
    }
  }, [platform, workspaceRoot])

  const isTauri = platform.supportsDaemon

  return (
    <div className="flex flex-col h-full bg-panel">
      <div className="px-3 py-2 border-b border-border flex items-center justify-between">
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={openExplorer}
            disabled={!workspaceRoot || !platform.openInExplorer}
            title={t('worktrees.openExplorer')}
            className="rounded-md p-1.5 text-text-secondary hover:text-text hover:bg-bg-tertiary disabled:opacity-40 transition-colors"
          >
            <Folder className="w-4 h-4" />
          </button>
          <button
            type="button"
            onClick={openTerminal}
            disabled={!workspaceRoot || !platform.openInTerminal}
            title={t('worktrees.openTerminal')}
            className="rounded-md p-1.5 text-text-secondary hover:text-text hover:bg-bg-tertiary disabled:opacity-40 transition-colors"
          >
            <Terminal className="w-4 h-4" />
          </button>
          <button
            type="button"
            title={t('panel.review')}
            className="rounded-md p-1.5 text-text-secondary hover:text-text hover:bg-bg-tertiary transition-colors"
          >
            <GitBranch className="w-4 h-4" />
          </button>
          <button
            type="button"
            onClick={() => navigate('/reviews')}
            title={t('action.review')}
            className="rounded-md p-1.5 text-text-secondary hover:text-text hover:bg-bg-tertiary transition-colors"
          >
            <CheckSquare className="w-4 h-4" />
          </button>
          <button
            type="button"
            className="rounded-md p-1.5 text-text-secondary hover:text-text hover:bg-bg-tertiary transition-colors"
            aria-label={t('action.more')}
          >
            <ChevronDown className="w-4 h-4" />
          </button>
        </div>
        {isTauri && <WindowControls />}
      </div>

      <div className="px-3 py-2 border-b border-border flex items-center justify-between gap-2">
        <div className="relative">
          <select
            value={filter}
            onChange={(e) => setFilter(e.target.value as DiffFilter)}
            className="appearance-none rounded-md border border-border bg-bg px-3 py-1.5 pr-7 text-xs text-text focus:outline-none focus:ring-2 focus:ring-accent/40"
          >
            <option value="unstaged">{t('review.unstaged')}</option>
            <option value="staged">{t('review.staged')}</option>
            <option value="all">{t('review.allChanges')}</option>
          </select>
          <ChevronDown className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-text-secondary" />
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => navigate('/reviews')}
            className="rounded-md border border-border bg-bg px-2.5 py-1.5 text-xs font-medium text-text hover:bg-bg-tertiary transition-colors"
          >
            {t('action.review')}
          </button>
          <button
            type="button"
            onClick={() => navigate('/chat')}
            className="rounded-md border border-border bg-bg px-2.5 py-1.5 text-xs font-medium text-text hover:bg-bg-tertiary transition-colors"
          >
            <Plus className="w-3.5 h-3.5" />
          </button>
          <button
            type="button"
            onClick={() => void loadDiff()}
            disabled={loading}
            title={t('action.refresh')}
            className="rounded-md p-1.5 text-text-secondary hover:text-text hover:bg-bg-tertiary disabled:opacity-50 transition-colors"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-3">
        {!currentSessionId ? (
          <div className="text-sm text-text-secondary text-center py-8">
            {t('review.emptySession')}
          </div>
        ) : loading ? (
          <div className="text-sm text-text-secondary text-center py-8">
            {t('review.loading')}
          </div>
        ) : error ? (
          <div className="flex flex-col items-center justify-center py-10 text-center">
            <FileQuestion className="w-10 h-10 text-text-secondary mb-3" />
            <div className="text-sm font-medium text-text mb-1">
              {t('review.loadError')}
            </div>
            <div className="text-xs text-text-secondary max-w-[200px] break-words">
              {error}
            </div>
          </div>
        ) : !diff?.available ? (
          <div className="flex flex-col items-center justify-center py-10 text-center">
            <FileQuestion className="w-10 h-10 text-text-secondary mb-3" />
            <div className="text-sm text-text-secondary">{t('review.noGit')}</div>
          </div>
        ) : filteredFiles.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-10 text-center">
            <FileQuestion className="w-10 h-10 text-text-secondary mb-3" />
            <div className="text-sm text-text-secondary">{t('review.noChanges')}</div>
          </div>
        ) : (
          <div className="space-y-2">
            {currentSession && (
              <div className="text-xs text-text-secondary mb-2">
                {currentSession.title}
                {diff.branch && (
                  <span className="ml-2 rounded-md bg-bg px-1.5 py-0.5 border border-border">
                    {diff.branch}
                  </span>
                )}
              </div>
            )}
            {filteredFiles.map((file) => (
              <DiffFileItem
                key={`${file.stage}:${file.path}`}
                file={file}
                expanded={expanded.has(file.path)}
                onToggle={() => toggleFile(file.path)}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function DiffFileItem({
  file,
  expanded,
  onToggle,
}: {
  file: GitDiffFile
  expanded: boolean
  onToggle: () => void
}) {
  const status = STATUS_LABEL[file.status] ?? file.status
  return (
    <div className="rounded-md border border-border bg-bg overflow-hidden">
      <button
        type="button"
        onClick={onToggle}
        className="w-full flex items-center justify-between px-3 py-2 text-left hover:bg-bg-tertiary transition-colors"
      >
        <div className="flex items-center gap-2 min-w-0">
          <span className="rounded px-1.5 py-0.5 text-[10px] font-medium bg-accent/10 text-accent shrink-0">
            {status}
          </span>
          <span className="text-xs text-text truncate">{file.path}</span>
        </div>
        <div className="flex items-center gap-2 shrink-0 text-xs text-text-secondary">
          {file.additions > 0 && (
            <span className="text-success">+{file.additions}</span>
          )}
          {file.deletions > 0 && (
            <span className="text-danger">-{file.deletions}</span>
          )}
        </div>
      </button>
      {expanded && file.patch && (
        <pre className="px-3 py-2 text-[10px] leading-4 text-text-secondary bg-bg-tertiary overflow-x-auto border-t border-border">
          {file.patch}
        </pre>
      )}
    </div>
  )
}
