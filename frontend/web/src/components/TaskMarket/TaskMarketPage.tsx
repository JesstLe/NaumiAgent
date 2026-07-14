import { useTranslation } from 'react-i18next'
import { useSessionStore } from '@/stores/sessionStore'

export function TaskMarketPage() {
  const { t } = useTranslation()
  const issues = useSessionStore((state) => state.snapshot?.issues ?? [])

  return (
    <div className="p-6 h-full overflow-y-auto">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-xl font-semibold text-text">{t('nav.taskMarket')}</h1>
        <span className="text-sm text-text-secondary">{issues.length} issues</span>
      </div>

      {issues.length === 0 ? (
        <div className="text-text-secondary">{t('taskMarket.empty')}</div>
      ) : (
        <div className="space-y-3">
          {issues.map((issue) => (
            <div key={issue.task_id} className="rounded-lg border border-border bg-panel p-4 shadow-sm">
              <div className="flex items-center justify-between">
                <div className="font-medium text-text">{issue.task?.subject || issue.task_id}</div>
                <div className="flex items-center gap-2">
                  <span className="text-xs px-2 py-0.5 rounded-full bg-bg-tertiary text-text-secondary capitalize">
                    {issue.risk_level}
                  </span>
                  <span className="text-xs px-2 py-0.5 rounded-full bg-bg-tertiary text-text-secondary capitalize">
                    {issue.parallel_mode}
                  </span>
                </div>
              </div>
              {issue.task?.description && (
                <p className="mt-2 text-sm text-text-secondary line-clamp-2">{issue.task.description}</p>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
