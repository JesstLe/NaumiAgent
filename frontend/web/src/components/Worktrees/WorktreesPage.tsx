import { useTranslation } from 'react-i18next'
import { useSessionStore } from '@/stores/sessionStore'
import { GitBranch, Trash2 } from 'lucide-react'

export function WorktreesPage() {
  const { t } = useTranslation()
  const worktrees = useSessionStore((state) => state.snapshot?.worktrees ?? [])

  return (
    <div className="p-6 h-full overflow-y-auto">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-xl font-semibold text-text">{t('nav.worktrees')}</h1>
        <span className="text-sm text-text-secondary">{worktrees.length} worktrees</span>
      </div>

      {worktrees.length === 0 ? (
        <div className="text-text-secondary">{t('worktrees.empty')}</div>
      ) : (
        <div className="space-y-3">
          {worktrees.map((worktree) => (
            <div key={worktree.name} className="rounded-lg border border-border bg-panel p-4 shadow-sm">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <GitBranch className="w-4 h-4 text-text-secondary" />
                  <span className="font-medium text-text">{worktree.name}</span>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-xs px-2 py-0.5 rounded-full bg-bg-tertiary text-text-secondary capitalize">
                    {worktree.status}
                  </span>
                  {worktree.removable && (
                    <button
                      type="button"
                      className="text-text-secondary hover:text-danger"
                      title={t('worktrees.delete')}
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  )}
                </div>
              </div>
              <div className="mt-2 text-sm text-text-secondary">{worktree.branch}</div>
              {worktree.dirty_files > 0 && (
                <div className="mt-1 text-xs text-danger">{t('worktrees.dirtyFiles', { count: worktree.dirty_files })}</div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
