import { useState, useMemo, useRef, useEffect } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import {
  Folder,
  Terminal,
  GitBranch,
  CheckSquare,
  ChevronDown,
  MoreHorizontal,
  Settings,
  History,
} from 'lucide-react'
import { useAppStore } from '@/stores/appStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useWorkbenchConnection } from '@/hooks/useWorkbenchConnection'
import { usePlatform } from '@/platform/PlatformContext'
import { WindowControls } from './WindowControls'

export function TopBar() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const { currentRoute } = useAppStore()
  const { status } = useWorkbenchConnection()
  const platform = usePlatform()
  const menuRef = useRef<HTMLDivElement>(null)
  const [menuOpen, setMenuOpen] = useState(false)

  const sessions = useSessionStore((state) => state.sessions)
  const currentSessionId = useSessionStore((state) => state.currentSessionId)
  const currentSession = useMemo(
    () => sessions.find((s) => s.id === currentSessionId),
    [sessions, currentSessionId],
  )

  const title = currentSession?.title ?? t(`nav.${currentRoute}`)
  const workspaceName = status.daemon?.workspace_name ?? 'NA'
  const workspaceRoot = status.daemon?.workspace_root
  const isTauri = platform.supportsDaemon

  useEffect(() => {
    function handleClick(event: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) {
        setMenuOpen(false)
      }
    }
    if (menuOpen) {
      document.addEventListener('mousedown', handleClick)
    }
    return () => {
      document.removeEventListener('mousedown', handleClick)
    }
  }, [menuOpen])

  const openExplorer = () => {
    if (workspaceRoot && platform.openInExplorer) {
      void platform.openInExplorer(workspaceRoot)
    }
  }

  const openTerminal = () => {
    if (workspaceRoot && platform.openInTerminal) {
      void platform.openInTerminal(workspaceRoot)
    }
  }

  return (
    <div className="h-12 border-b border-border bg-bg flex items-center justify-between px-3 shrink-0">
      <div className="flex items-center gap-3 overflow-hidden">
        <span className="text-sm font-medium text-text truncate">{title}</span>
        <button
          type="button"
          onClick={openExplorer}
          disabled={!workspaceRoot || !platform.openInExplorer}
          className="flex items-center gap-1.5 rounded-md border border-border bg-bg-tertiary px-2 py-1 text-xs text-text-secondary hover:text-text hover:bg-bg disabled:opacity-40 transition-colors"
        >
          <Folder className="w-3.5 h-3.5" />
          {workspaceName}
        </button>
        <div className="relative" ref={menuRef}>
          <button
            type="button"
            onClick={() => setMenuOpen((o) => !o)}
            className="rounded-md p-1.5 text-text-secondary hover:text-text hover:bg-bg-tertiary transition-colors"
            aria-label={t('action.more')}
          >
            <MoreHorizontal className="w-4 h-4" />
          </button>
          {menuOpen && (
            <div className="absolute left-0 top-full mt-1 w-44 rounded-md border border-border bg-panel shadow-lg z-50 py-1">
              <button
                type="button"
                onClick={() => {
                  setMenuOpen(false)
                  navigate('/reviews')
                }}
                className="w-full flex items-center gap-2 px-3 py-2 text-sm text-text hover:bg-bg-tertiary text-left"
              >
                <CheckSquare className="w-4 h-4" />
                {t('action.review')}
              </button>
              <button
                type="button"
                onClick={() => {
                  setMenuOpen(false)
                  navigate('/timeline')
                }}
                className="w-full flex items-center gap-2 px-3 py-2 text-sm text-text hover:bg-bg-tertiary text-left"
              >
                <History className="w-4 h-4" />
                {t('nav.timeline')}
              </button>
              <button
                type="button"
                onClick={() => {
                  setMenuOpen(false)
                  navigate('/settings')
                }}
                className="w-full flex items-center gap-2 px-3 py-2 text-sm text-text hover:bg-bg-tertiary text-left"
              >
                <Settings className="w-4 h-4" />
                {t('nav.settings')}
              </button>
            </div>
          )}
        </div>
      </div>

      <div className="flex items-center">
        <div className="flex items-center gap-1 pr-3 border-r border-border mr-3">
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
            onClick={() => navigate('/reviews')}
            title={t('action.review')}
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
    </div>
  )
}

export function TabBar() {
  const { t } = useTranslation()
  const { currentRoute } = useAppStore()

  // Simple tab representing the current page; future version could keep
  // multiple open tabs for sessions.
  return (
    <div className="h-8 bg-bg border-b border-border flex items-center px-2 gap-1">
      <div className="flex items-center gap-2 px-3 py-1 rounded-md bg-bg-tertiary text-xs text-text font-medium">
        {t(`nav.${currentRoute}`)}
        <button
          type="button"
          className="text-text-secondary hover:text-text"
          aria-label={t('action.close')}
        >
          <XIcon className="w-3 h-3" />
        </button>
      </div>
    </div>
  )
}

function XIcon({ className }: { className?: string }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
    >
      <path d="M18 6 6 18" />
      <path d="m6 6 12 12" />
    </svg>
  )
}
