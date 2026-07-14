import { useTranslation } from 'react-i18next'
import { useSessionStore } from '@/stores/sessionStore'
import { formatDate } from '@/utils/formatDate'

export function GoalPanel() {
  const { t } = useTranslation()
  const snapshot = useSessionStore((state) => state.snapshot)
  const currentSessionId = useSessionStore((state) => state.currentSessionId)
  const missions = snapshot?.missions ?? []
  const currentMission = missions[0]
  const issues = snapshot?.issues ?? []
  const pendingApprovals = (snapshot?.approvals ?? []).filter((a) => a.state === 'waiting')
  const openIssues = issues.filter((i) => i.task?.status === 'pending' || i.task?.status === 'in_progress')
  const blockedIssues = issues.filter((i) => i.task?.status === 'blocked')
  const failedValidations = (snapshot?.validation_runs ?? []).filter((v) => v.status === 'failed')

  const todos = [
    ...(pendingApprovals.length > 0 ? [{ label: t('reviews.approvals'), count: pendingApprovals.length, tone: 'amber' as const }] : []),
    ...(openIssues.length > 0 ? [{ label: t('dashboard.openIssues'), count: openIssues.length, tone: 'blue' as const }] : []),
    ...(blockedIssues.length > 0 ? [{ label: t('dashboard.blockedIssues'), count: blockedIssues.length, tone: 'warning' as const }] : []),
    ...(failedValidations.length > 0 ? [{ label: t('dashboard.failedValidations'), count: failedValidations.length, tone: 'danger' as const }] : []),
  ]

  const completedCount = issues.length - openIssues.length - blockedIssues.length
  const total = Math.max(issues.length, 1)
  const progress = Math.round((completedCount / total) * 100)

  return (
    <div className="flex flex-col h-full">
      <div className="px-4 py-3 border-b border-border font-medium text-sm">{t('panel.goal')}</div>
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {currentSessionId ? (
          <>
            <div className="rounded-lg border border-border bg-bg p-3">
              <div className="text-xs text-text-secondary uppercase tracking-wider">{t('goal.currentMission')}</div>
              <div className="mt-1 font-medium text-text">
                {currentMission?.title || snapshot?.summary.current_mission_title || t('goal.noMission')}
              </div>
              {currentMission && (
                <div className="mt-1 text-xs text-text-secondary">{formatDate(currentMission.created_at)}</div>
              )}
            </div>

            <div className="rounded-lg border border-border bg-bg p-3">
              <div className="flex items-center justify-between mb-2">
                <span className="text-xs text-text-secondary uppercase tracking-wider">{t('goal.progress')}</span>
                <span className="text-sm font-medium text-text">{progress}%</span>
              </div>
              <div className="h-2 rounded-full bg-bg-tertiary overflow-hidden">
                <div
                  className="h-full rounded-full bg-accent transition-all"
                  style={{ width: `${progress}%` }}
                />
              </div>
              <div className="mt-2 text-xs text-text-secondary">
                {completedCount}/{total} {t('goal.completed')}
              </div>
            </div>

            {todos.length > 0 ? (
              <div className="space-y-2">
                <div className="text-xs text-text-secondary uppercase tracking-wider">{t('goal.todo')}</div>
                {todos.map((todo, idx) => (
                  <div
                    key={idx}
                    className="flex items-center justify-between rounded-md bg-bg px-3 py-2 text-sm border border-border"
                  >
                    <span className="text-text">{todo.label}</span>
                    <span
                      className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                        todo.tone === 'amber'
                          ? 'bg-warning/15 text-warning'
                          : todo.tone === 'warning'
                            ? 'bg-amber-500/15 text-amber-600'
                            : todo.tone === 'danger'
                              ? 'bg-danger/15 text-danger'
                              : 'bg-accent/15 text-accent'
                      }`}
                    >
                      {todo.count}
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-sm text-text-secondary">{t('goal.allClear')}</div>
            )}

            <div className="rounded-lg border border-border bg-bg p-3">
              <div className="text-xs text-text-secondary uppercase tracking-wider">{t('goal.agents')}</div>
              <div className="mt-2 flex items-center gap-2">
                <div className="h-2 w-2 rounded-full bg-success" />
                <span className="text-sm text-text">
                  {snapshot?.summary.active_agents ?? 0} {t('dashboard.activeAgents')}
                </span>
              </div>
            </div>
          </>
        ) : (
          <div className="text-sm text-text-secondary">{t('goal.empty')}</div>
        )}
      </div>
    </div>
  )
}
