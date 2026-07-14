import { useState, useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { Folder, FolderOpen, Search } from 'lucide-react'
import { useSessionStore } from '@/stores/sessionStore'
import { useWorkbenchConnection } from '@/hooks/useWorkbenchConnection'
import { isApiException } from '@/api/ApiException'

export function ProjectPanel() {
  const { t } = useTranslation()
  const { client, selectSession } = useWorkbenchConnection()
  const sessions = useSessionStore((state) => state.sessions)
  const currentSessionId = useSessionStore((state) => state.currentSessionId)
  const setSessions = useSessionStore((state) => state.setSessions)
  const setCurrentSessionId = useSessionStore((state) => state.setCurrentSessionId)
  const setSessionError = useSessionStore((state) => state.setError)
  const [query, setQuery] = useState('')
  const [creating, setCreating] = useState(false)

  const filtered = useMemo(() => {
    if (!query.trim()) return sessions
    const q = query.toLowerCase()
    return sessions.filter((s) => (s.title || s.id).toLowerCase().includes(q))
  }, [sessions, query])

  const handleCreateSession = async () => {
    if (!client) return
    setCreating(true)
    try {
      const result = await client.createSession(t('session.newTitle'))
      setSessions([result.sessions[0], ...sessions])
      const sessionId = result.selected_session_id ?? result.sessions[0].id
      await selectSession(sessionId)
      setCurrentSessionId(sessionId)
    } catch (error) {
      setSessionError(isApiException(error) ? error.message : String(error))
    } finally {
      setCreating(false)
    }
  }

  const handleSelectSession = async (sessionId: string) => {
    try {
      await selectSession(sessionId)
      setCurrentSessionId(sessionId)
    } catch (error) {
      setSessionError(isApiException(error) ? error.message : String(error))
    }
  }

  return (
    <div className="flex flex-col h-full">
      <div className="px-4 py-3 border-b border-border font-medium text-sm">{t('sideTool.projects')}</div>
      <div className="p-3 border-b border-border">
        <div className="relative">
          <Search className="absolute left-2.5 top-2 h-4 w-4 text-text-secondary" />
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t('action.search')}
            className="w-full rounded-md border border-border bg-bg pl-9 pr-3 py-1.5 text-sm text-text focus:outline-none focus:ring-2 focus:ring-accent/40"
          />
        </div>
      </div>
      <div className="flex-1 overflow-y-auto p-2">
        <button
          type="button"
          onClick={() => void handleCreateSession()}
          disabled={!client || creating}
          className="w-full flex items-center gap-2 rounded-md px-3 py-2 text-sm text-accent hover:bg-accent/10 disabled:opacity-50 text-left"
        >
          <span className="text-lg leading-none">+</span> {t('action.newMission')}
        </button>
        <ul className="mt-2 space-y-0.5">
          {filtered.map((session) => {
            const active = session.id === currentSessionId
            return (
              <li key={session.id}>
                <button
                  type="button"
                  onClick={() => void handleSelectSession(session.id)}
                  className={`w-full flex items-center gap-2 text-left px-3 py-2 rounded-md text-sm truncate transition-colors ${
                    active
                      ? 'bg-accent/10 text-accent font-medium'
                      : 'text-text-secondary hover:bg-bg-tertiary hover:text-text'
                  }`}
                >
                  {active ? (
                    <FolderOpen className="w-4 h-4 shrink-0" />
                  ) : (
                    <Folder className="w-4 h-4 shrink-0" />
                  )}
                  <span className="truncate">{session.title || session.id}</span>
                </button>
              </li>
            )
          })}
        </ul>
      </div>
    </div>
  )
}
