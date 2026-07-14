import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { LayoutGrid } from 'lucide-react'
import { useSessionStore } from '@/stores/sessionStore'

export interface GroupPanelProps {
  showHeader?: boolean
}

export function GroupPanel({ showHeader = true }: GroupPanelProps) {
  const { t } = useTranslation()
  const snapshot = useSessionStore((state) => state.snapshot)
  const missions = snapshot?.missions ?? []
  const issues = snapshot?.issues ?? []
  const grouped = useMemo(() => {
    const map = new Map<string, typeof issues>()
    for (const mission of missions) {
      map.set(mission.id, issues.filter((i) => i.mission_id === mission.id))
    }
    // Issues without a matching mission
    const known = new Set(missions.map((m) => m.id))
    const orphan = issues.filter((i) => !known.has(i.mission_id))
    return { map, orphan }
  }, [missions, issues])

  return (
    <div className="flex flex-col h-full">
      {showHeader && (
        <div className="px-4 py-3 border-b border-border font-medium text-sm">{t('sideTool.groups')}</div>
      )}
      <div className="flex-1 overflow-y-auto p-3 space-y-4">
        {missions.map((mission) => (
          <div key={mission.id} className="rounded-md border border-border bg-bg p-2">
            <div className="flex items-center gap-2 text-sm font-medium text-text">
              <LayoutGrid className="w-4 h-4 text-text-secondary" />
              {mission.title}
            </div>
            <ul className="mt-1 space-y-0.5">
              {grouped.map.get(mission.id)?.map((issue) => (
                <li key={issue.task_id} className="text-xs text-text-secondary truncate">
                  {issue.task?.subject || issue.task_id}
                </li>
              ))}
            </ul>
            {(grouped.map.get(mission.id)?.length ?? 0) === 0 && (
              <div className="text-xs text-text-secondary">{t('group.empty')}</div>
            )}
          </div>
        ))}
        {grouped.orphan.length > 0 && (
          <div className="rounded-md border border-border bg-bg p-2">
            <div className="text-sm font-medium text-text">{t('group.orphan')}</div>
            <ul className="mt-1 space-y-0.5">
              {grouped.orphan.map((issue) => (
                <li key={issue.task_id} className="text-xs text-text-secondary truncate">
                  {issue.task?.subject || issue.task_id}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  )
}
