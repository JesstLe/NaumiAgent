import { useEffect, type ReactNode } from 'react'
import { NavLink, Outlet } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import {
  LayoutDashboard,
  MessageSquare,
  ClipboardList,
  GitBranch,
  Eye,
  Clock,
  Settings,
  Plus,
  Search,
  Wrench,
  LayoutGrid,
  Folder,
} from 'lucide-react'
import { useAppStore, type AppRoute } from '@/stores/appStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useWorkbenchConnection } from '@/hooks/useWorkbenchConnection'
import { ProjectPanel } from './ProjectPanel'
import { GroupPanel } from './GroupPanel'
import { SkillPanel } from './SkillPanel'
import { SearchPanel } from './SearchPanel'
import { GoalPanel } from './GoalPanel'
import { TopBar } from './TopBar'
import { isApiException } from '@/api/ApiException'
import type { ElementType } from 'react'

type NavItem = { route: AppRoute; path: string; labelKey: string; icon: ElementType }

const navItems: NavItem[] = [
  { route: 'dashboard', path: '/dashboard', labelKey: 'nav.dashboard', icon: LayoutDashboard },
  { route: 'chat', path: '/chat', labelKey: 'nav.chat', icon: MessageSquare },
  { route: 'taskMarket', path: '/task-market', labelKey: 'nav.taskMarket', icon: ClipboardList },
  { route: 'worktrees', path: '/worktrees', labelKey: 'nav.worktrees', icon: GitBranch },
  { route: 'reviews', path: '/reviews', labelKey: 'nav.reviews', icon: Eye },
  { route: 'timeline', path: '/timeline', labelKey: 'nav.timeline', icon: Clock },
]

type ToolItem = { id: 'projects' | 'groups' | 'skills' | 'search'; labelKey: string; icon: ElementType }

const toolItems: ToolItem[] = [
  { id: 'projects', labelKey: 'sideTool.projects', icon: Folder },
  { id: 'groups', labelKey: 'sideTool.groups', icon: LayoutGrid },
  { id: 'skills', labelKey: 'sideTool.skills', icon: Wrench },
  { id: 'search', labelKey: 'sideTool.search', icon: Search },
]

export function MainLayout() {
  const { t } = useTranslation()
  const { activeSideTool, setCurrentRoute, setActiveSideTool } = useAppStore()
  const setSessions = useSessionStore((state) => state.setSessions)
  const setSessionError = useSessionStore((state) => state.setError)
  const { client } = useWorkbenchConnection()

  useEffect(() => {
    if (!client) {
      setSessions([])
      return
    }
    let cancelled = false
    client
      .fetchSessions()
      .then((response) => {
        if (!cancelled) setSessions(response.sessions)
      })
      .catch((error) => {
        if (!cancelled) setSessionError(isApiException(error) ? error.message : String(error))
      })
    return () => {
      cancelled = true
    }
  }, [client, setSessions, setSessionError])

  const sidePanel: Record<typeof activeSideTool, ReactNode> = {
    projects: <ProjectPanel />,
    groups: <GroupPanel />,
    skills: <SkillPanel />,
    search: <SearchPanel />,
  }

  return (
    <div className="flex h-full w-full bg-bg">
      {/* Far-left icon rail (matches the UX in the screenshot) */}
      <nav className="flex flex-col w-14 border-r border-border bg-sidebar shrink-0">
        <div className="flex items-center justify-center h-12 border-b border-border">
          <div className="font-semibold text-xs text-accent">NA</div>
        </div>

        <div className="flex-1 overflow-y-auto py-2 space-y-1 px-1">
          <button
            type="button"
            className="w-full flex items-center justify-center rounded-md py-2 text-accent hover:bg-accent/10"
            title={t('action.newTask')}
          >
            <Plus className="w-5 h-5" />
          </button>
          {toolItems.map((tool) => {
            const Icon = tool.icon
            const active = activeSideTool === tool.id
            return (
              <button
                key={tool.id}
                type="button"
                onClick={() => setActiveSideTool(tool.id)}
                title={t(tool.labelKey)}
                className={`w-full flex flex-col items-center justify-center gap-0.5 rounded-md py-2 text-xs transition-colors ${
                  active
                    ? 'bg-accent/10 text-accent font-medium'
                    : 'text-text-secondary hover:bg-bg-tertiary hover:text-text'
                }`}
              >
                <Icon className="w-5 h-5" />
                <span className="scale-90">{t(tool.labelKey)}</span>
              </button>
            )
          })}

          <div className="my-2 border-t border-border" />

          {navItems.map((item) => {
            const Icon = item.icon
            return (
              <NavLink
                key={item.route}
                to={item.path}
                onClick={() => setCurrentRoute(item.route)}
                title={t(item.labelKey)}
                className={({ isActive }) =>
                  `w-full flex flex-col items-center justify-center gap-0.5 rounded-md py-2 text-xs transition-colors ${
                    isActive
                      ? 'bg-accent/10 text-accent font-medium'
                      : 'text-text-secondary hover:bg-bg-tertiary hover:text-text'
                  }`
                }
              >
                <Icon className="w-5 h-5" />
                <span className="scale-90">{t(item.labelKey)}</span>
              </NavLink>
            )
          })}
        </div>

        <div className="p-1 border-t border-border">
          <NavLink
            to="/settings"
            onClick={() => setCurrentRoute('settings')}
            title={t('nav.settings')}
            className={({ isActive }) =>
              `w-full flex flex-col items-center justify-center gap-0.5 rounded-md py-2 text-xs transition-colors ${
                isActive
                  ? 'bg-accent/10 text-accent font-medium'
                  : 'text-text-secondary hover:bg-bg-tertiary hover:text-text'
              }`
            }
          >
            <Settings className="w-5 h-5" />
            <span className="scale-90">{t('nav.settings')}</span>
          </NavLink>
        </div>
      </nav>

      {/* Left side panel for the selected tool */}
      <aside className="w-64 border-r border-border bg-sidebar shrink-0 flex flex-col">
        {sidePanel[activeSideTool]}
      </aside>

      {/* Main content area */}
      <main className="flex-1 min-w-0 flex flex-col bg-bg">
        <TopBar />
        <Outlet />
      </main>

      {/* Right goal panel */}
      <aside className="w-80 border-l border-border bg-panel shrink-0 flex flex-col">
        <GoalPanel />
      </aside>
    </div>
  )
}
