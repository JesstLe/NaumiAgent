import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { Eye, Plus, X } from 'lucide-react'
import { useAppStore } from '@/stores/appStore'

export function TopBar() {
  const { t } = useTranslation()
  const { currentRoute, openIssues } = useAppStore()
  const navigate = useNavigate()

  return (
    <div className="h-12 border-b border-border bg-bg flex items-center justify-between px-4 shrink-0">
      <div className="flex items-center gap-4 overflow-hidden">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-text truncate">{t(`nav.${currentRoute}`)}</span>
          <span className="text-xs text-text-secondary">/ NaumiAgent Workspace</span>
        </div>
      </div>

      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => navigate('/reviews')}
          className="flex items-center gap-1 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium text-text hover:bg-bg-tertiary"
        >
          <Eye className="w-3.5 h-3.5" />
          {t('action.review')}
          {openIssues > 0 && (
            <span className="ml-1 rounded-full bg-accent px-1.5 py-0 text-[10px] text-white">
              {openIssues}
            </span>
          )}
        </button>
        <button
          type="button"
          onClick={() => navigate('/chat')}
          className="flex items-center gap-1 rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-white hover:bg-accent-hover"
        >
          <Plus className="w-3.5 h-3.5" />
          {t('action.newMission')}
        </button>
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
          <X className="w-3 h-3" />
        </button>
      </div>
    </div>
  )
}
