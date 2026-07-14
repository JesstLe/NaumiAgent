import { useTranslation } from 'react-i18next'
import { Wrench } from 'lucide-react'
import { useSessionStore } from '@/stores/sessionStore'

export function SkillPanel() {
  const { t } = useTranslation()
  const agents = useSessionStore((state) => state.snapshot?.agent_profiles ?? [])

  return (
    <div className="flex flex-col h-full">
      <div className="px-4 py-3 border-b border-border font-medium text-sm">{t('sideTool.skills')}</div>
      <div className="flex-1 overflow-y-auto p-3 space-y-3">
        {agents.length === 0 ? (
          <div className="text-sm text-text-secondary">{t('skill.empty')}</div>
        ) : (
          agents.map((agent) => (
            <div key={agent.id} className="rounded-md border border-border bg-bg p-3">
              <div className="flex items-center gap-2 text-sm font-medium text-text">
                <div
                  className={`h-2 w-2 rounded-full ${
                    agent.status === 'idle'
                      ? 'bg-success'
                      : agent.status === 'busy'
                        ? 'bg-accent'
                        : agent.status === 'stale'
                          ? 'bg-warning'
                          : 'bg-text-secondary'
                  }`}
                />
                {agent.name}
              </div>
              <div className="text-xs text-text-secondary">{agent.role}</div>
              {agent.capabilities.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1">
                  {agent.capabilities.map((cap) => (
                    <span
                      key={cap}
                      className="inline-flex items-center gap-1 rounded-full bg-bg-tertiary px-2 py-0.5 text-xs text-text-secondary"
                    >
                      <Wrench className="w-3 h-3" />
                      {cap}
                    </span>
                  ))}
                </div>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  )
}
