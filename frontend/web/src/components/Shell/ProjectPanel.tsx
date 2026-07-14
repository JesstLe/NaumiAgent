import { useState, useMemo, useCallback } from 'react'
import { useTranslation } from 'react-i18next'
import {
  Folder,
  Hash,
  Search,
  Filter,
  Trash2,
  Plus,
  ChevronDown,
  ChevronRight,
} from 'lucide-react'
import { useSessionStore } from '@/stores/sessionStore'
import { useWorkbenchConnection } from '@/hooks/useWorkbenchConnection'
import { isApiException } from '@/api/ApiException'
import { formatRelativeTime } from '@/utils/formatDate'
import { GroupPanel } from './GroupPanel'
import type { Session } from '@/api/types'

type ProjectView = 'projects' | 'groups'

interface ProjectGroup {
  name: string
  sessions: Session[]
}

export function ProjectPanel() {
  const { t } = useTranslation()
  const { client, selectSession, status } = useWorkbenchConnection()
  const sessions = useSessionStore((state) => state.sessions)
  const currentSessionId = useSessionStore((state) => state.currentSessionId)
  const setSessions = useSessionStore((state) => state.setSessions)
  const setCurrentSessionId = useSessionStore((state) => state.setCurrentSessionId)
  const setSessionError = useSessionStore((state) => state.setError)

  const [view, setView] = useState<ProjectView>('projects')
  const [query, setQuery] = useState('')
  const [showSearch, setShowSearch] = useState(false)
  const [creating, setCreating] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const workspaceName = status.daemon?.workspace_name ?? t('project.unknown')

  const projects = useMemo<ProjectGroup[]>(() => {
    const projectName = workspaceName
    const map = new Map<string, Session[]>()
    for (const session of sessions) {
      const list = map.get(projectName) ?? []
      list.push(session)
      map.set(projectName, list)
    }
    for (const list of map.values()) {
      list.sort(
        (a, b) =>
          new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime(),
      )
    }
    return Array.from(map.entries()).map(([name, sessions]) => ({ name, sessions }))
  }, [sessions, workspaceName])

  const filteredProjects = useMemo<ProjectGroup[]>(() => {
    if (!query.trim()) return projects
    const q = query.toLowerCase()
    return projects
      .map((project) => ({
        ...project,
        sessions: project.sessions.filter((s) =>
          (s.title || s.id).toLowerCase().includes(q),
        ),
      }))
      .filter((project) => project.sessions.length > 0)
  }, [projects, query])

  const [expanded, setExpanded] = useState<Set<string>>(new Set([workspaceName]))
  const toggleProject = useCallback((name: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(name)) {
        next.delete(name)
      } else {
        next.add(name)
      }
      return next
    })
  }, [])

  const handleCreateSession = useCallback(async () => {
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
  }, [client, sessions, selectSession, setSessions, setCurrentSessionId, setSessionError, t])

  const handleSelectSession = useCallback(
    async (sessionId: string) => {
      try {
        await selectSession(sessionId)
        setCurrentSessionId(sessionId)
      } catch (error) {
        setSessionError(isApiException(error) ? error.message : String(error))
      }
    },
    [selectSession, setCurrentSessionId, setSessionError],
  )

  const handleDeleteSession = useCallback(async () => {
    if (!client || !currentSessionId) return
    const session = sessions.find((s) => s.id === currentSessionId)
    const title = session?.title || currentSessionId
    if (!window.confirm(t('project.deleteConfirm', { title }))) return
    setDeleting(true)
    try {
      await client.deleteSession(currentSessionId)
      setSessions(sessions.filter((s) => s.id !== currentSessionId))
      setCurrentSessionId(null)
    } catch (error) {
      setSessionError(isApiException(error) ? error.message : String(error))
    } finally {
      setDeleting(false)
    }
  }, [client, currentSessionId, sessions, setSessions, setCurrentSessionId, setSessionError, t])

  const TabButton = ({
    active,
    onClick,
    icon: Icon,
    label,
  }: {
    active: boolean
    onClick: () => void
    icon: typeof Folder
    label: string
  }) => (
    <button
      type="button"
      onClick={onClick}
      className={`flex items-center gap-1 rounded-md px-2.5 py-1.5 text-xs transition-colors ${
        active
          ? 'bg-panel text-text font-medium shadow-sm'
          : 'text-text-secondary hover:text-text hover:bg-bg-tertiary'
      }`}
    >
      <Icon className="w-3.5 h-3.5" />
      {label}
    </button>
  )

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between px-3 py-2 border-b border-border">
        <div className="flex items-center gap-1 rounded-lg border border-border bg-bg p-0.5">
          <TabButton
            active={view === 'groups'}
            onClick={() => setView('groups')}
            icon={Hash}
            label={t('project.group')}
          />
          <TabButton
            active={view === 'projects'}
            onClick={() => setView('projects')}
            icon={Folder}
            label={t('project.project')}
          />
        </div>
        <div className="flex items-center gap-0.5">
          <button
            type="button"
            onClick={() => setShowSearch((s) => !s)}
            title={t('action.search')}
            className={`rounded-md p-1.5 transition-colors ${
              showSearch
                ? 'bg-accent/10 text-accent'
                : 'text-text-secondary hover:text-text hover:bg-bg-tertiary'
            }`}
          >
            <Filter className="w-4 h-4" />
          </button>
          <button
            type="button"
            onClick={() => void handleDeleteSession()}
            disabled={!currentSessionId || deleting}
            title={t('action.delete')}
            className="rounded-md p-1.5 text-text-secondary hover:text-danger hover:bg-danger/10 disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-text-secondary transition-colors"
          >
            <Trash2 className="w-4 h-4" />
          </button>
        </div>
      </div>

      {showSearch && (
        <div className="p-2 border-b border-border">
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
      )}

      <button
        type="button"
        onClick={() => void handleCreateSession()}
        disabled={!client || creating}
        className="mx-2 mt-2 flex items-center gap-2 rounded-md px-3 py-2 text-sm text-accent hover:bg-accent/10 disabled:opacity-50 text-left transition-colors"
      >
        <Plus className="w-4 h-4" />
        {t('session.new')}
      </button>

      <div className="flex-1 overflow-y-auto mt-1">
        {view === 'groups' ? (
          <GroupPanel showHeader={false} />
        ) : filteredProjects.length === 0 ? (
          <div className="px-3 py-6 text-center text-sm text-text-secondary">
            {t('project.empty')}
          </div>
        ) : (
          filteredProjects.map((project) => {
            const isExpanded = expanded.has(project.name)
            return (
              <div key={project.name} className="border-b border-border last:border-b-0">
                <button
                  type="button"
                  onClick={() => toggleProject(project.name)}
                  className="w-full flex items-center gap-2 px-3 py-2 text-sm text-text hover:bg-bg-tertiary transition-colors"
                >
                  {isExpanded ? (
                    <ChevronDown className="w-4 h-4 text-text-secondary" />
                  ) : (
                    <ChevronRight className="w-4 h-4 text-text-secondary" />
                  )}
                  <Folder className="w-4 h-4 text-text-secondary" />
                  <span className="truncate font-medium">{project.name}</span>
                </button>
                {isExpanded && (
                  <ul className="pb-1">
                    {project.sessions.map((session) => {
                      const active = session.id === currentSessionId
                      return (
                        <li key={session.id}>
                          <button
                            type="button"
                            onClick={() => void handleSelectSession(session.id)}
                            className={`w-full flex items-center justify-between px-3 py-2 pl-9 text-sm transition-colors ${
                              active
                                ? 'bg-accent/10 text-accent'
                                : 'text-text-secondary hover:text-text hover:bg-bg-tertiary'
                            }`}
                          >
                            <span className="truncate">{session.title || session.id}</span>
                            <span className="text-xs text-text-secondary shrink-0 ml-2">
                              {formatRelativeTime(session.updated_at)}
                            </span>
                          </button>
                        </li>
                      )
                    })}
                    {project.sessions.length === 0 && (
                      <li className="px-3 py-2 pl-9 text-xs text-text-secondary">
                        {t('project.noSessions')}
                      </li>
                    )}
                  </ul>
                )}
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}
