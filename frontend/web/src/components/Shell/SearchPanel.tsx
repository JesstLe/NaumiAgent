import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Search } from 'lucide-react'
import { useSessionStore } from '@/stores/sessionStore'

export function SearchPanel() {
  const { t } = useTranslation()
  const [query, setQuery] = useState('')
  const sessions = useSessionStore((state) => state.sessions)
  const issues = useSessionStore((state) => state.snapshot?.issues ?? [])
  const agents = useSessionStore((state) => state.snapshot?.agent_profiles ?? [])

  const results = useMemo(() => {
    if (!query.trim()) return null
    const q = query.toLowerCase()
    return {
      sessions: sessions.filter((s) => (s.title || s.id).toLowerCase().includes(q)),
      issues: issues.filter((i) =>
        (i.task?.subject || i.task_id).toLowerCase().includes(q) ||
        (i.task?.description || '').toLowerCase().includes(q),
      ),
      agents: agents.filter((a) => a.name.toLowerCase().includes(q) || a.role.toLowerCase().includes(q)),
    }
  }, [query, sessions, issues, agents])

  return (
    <div className="flex flex-col h-full">
      <div className="px-4 py-3 border-b border-border font-medium text-sm">{t('sideTool.search')}</div>
      <div className="p-3 border-b border-border">
        <div className="relative">
          <Search className="absolute left-2.5 top-2 h-4 w-4 text-text-secondary" />
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t('search.placeholder')}
            className="w-full rounded-md border border-border bg-bg pl-9 pr-3 py-1.5 text-sm text-text focus:outline-none focus:ring-2 focus:ring-accent/40"
          />
        </div>
      </div>
      <div className="flex-1 overflow-y-auto p-3">
        {!results ? (
          <div className="text-sm text-text-secondary">{t('search.hint')}</div>
        ) : (
          <div className="space-y-4">
            <ResultSection title={t('search.projects')} items={results.sessions} label={(s) => s.title || s.id} />
            <ResultSection title={t('search.issues')} items={results.issues} label={(i) => i.task?.subject || i.task_id} />
            <ResultSection title={t('search.agents')} items={results.agents} label={(a) => a.name} />
            {results.sessions.length === 0 && results.issues.length === 0 && results.agents.length === 0 && (
              <div className="text-sm text-text-secondary">{t('search.noResults')}</div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function ResultSection<T>({
  title,
  items,
  label,
}: {
  title: string
  items: T[]
  label: (item: T) => string
}) {
  if (items.length === 0) return null
  return (
    <div>
      <div className="text-xs text-text-secondary uppercase tracking-wider mb-1">{title}</div>
      <ul className="space-y-0.5">
        {items.map((item, idx) => {
          const id = (item as { id?: string }).id ?? `${title}-${idx}`
          return (
            <li key={id} className="text-sm text-text truncate px-2 py-1 rounded hover:bg-bg-tertiary">
              {label(item)}
            </li>
          )
        })}
      </ul>
    </div>
  )
}
