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
  Pause,
  RefreshCw,
  Search,
  Circle,
} from 'lucide-react'
import { useAppStore, type AppRoute } from '@/stores/appStore'
import type { ElementType } from 'react'

const navItems: { route: AppRoute; path: string; labelKey: string; icon: ElementType }[] = [
  { route: 'dashboard', path: '/dashboard', labelKey: 'nav.dashboard', icon: LayoutDashboard },
  { route: 'chat', path: '/chat', labelKey: 'nav.chat', icon: MessageSquare },
  { route: 'taskMarket', path: '/task-market', labelKey: 'nav.taskMarket', icon: ClipboardList },
  { route: 'worktrees', path: '/worktrees', labelKey: 'nav.worktrees', icon: GitBranch },
  { route: 'reviews', path: '/reviews', labelKey: 'nav.reviews', icon: Eye },
  { route: 'timeline', path: '/timeline', labelKey: 'nav.timeline', icon: Clock },
  { route: 'settings', path: '/settings', labelKey: 'nav.settings', icon: Settings },
]

function StatusDot({ status }: { status: string }) {
  const color =
    status === 'online'
      ? 'text-green-500'
      : status === 'offline'
      ? 'text-red-500'
      : status === 'starting'
      ? 'text-amber-500'
      : 'text-gray-400'
  return <Circle className={`w-2 h-2 fill-current ${color}`} />
}

export function MainLayout() {
  const { t } = useTranslation()
  const { currentRoute, connectionStatus, openIssues, activeAgents, setCurrentRoute } = useAppStore()

  return (
    <div className="flex h-full w-full bg-bg">
      {/* Left sidebar */}
      <aside className="flex flex-col w-64 border-r border-border bg-sidebar shrink-0">
        {/* App title */}
        <div className="px-4 py-3 border-b border-border">
          <div className="font-semibold text-text truncate">{t('appTitle')}</div>
          <div className="text-xs text-text-secondary truncate">NaumiAgent Workspace</div>
        </div>

        {/* Main navigation */}
        <nav className="flex-1 overflow-y-auto py-2">
          <ul className="space-y-0.5 px-2">
            {navItems.map((item) => {
              const Icon = item.icon
              const active = currentRoute === item.route
              return (
                <li key={item.route}>
                  <NavLink
                    to={item.path}
                    onClick={() => setCurrentRoute(item.route)}
                    className={`flex items-center gap-3 px-3 py-2 rounded-md text-sm transition-colors ${
                      active
                        ? 'bg-accent/10 text-accent font-medium'
                        : 'text-text-secondary hover:bg-bg-tertiary hover:text-text'
                    }`}
                  >
                    <Icon className="w-4 h-4" />
                    {t(item.labelKey)}
                  </NavLink>
                </li>
              )
            })}
          </ul>

          {/* Workspace / session list placeholder */}
          <div className="mt-6 px-4 text-xs font-semibold text-text-secondary uppercase tracking-wider">
            Sessions
          </div>
          <ul className="mt-2 px-2">
            <li className="px-3 py-2 rounded-md text-sm text-text-secondary truncate">
              default-session
            </li>
          </ul>

          {/* Global actions */}
          <div className="mt-6 px-2 space-y-1">
            <button className="w-full flex items-center gap-2 px-3 py-2 rounded-md text-sm text-text-secondary hover:bg-bg-tertiary">
              <Plus className="w-4 h-4" /> {t('action.newMission')}
            </button>
            <button className="w-full flex items-center gap-2 px-3 py-2 rounded-md text-sm text-text-secondary hover:bg-bg-tertiary">
              <Pause className="w-4 h-4" /> {t('action.pauseAgents')}
            </button>
            <button className="w-full flex items-center gap-2 px-3 py-2 rounded-md text-sm text-text-secondary hover:bg-bg-tertiary">
              <RefreshCw className="w-4 h-4" /> {t('action.syncContext')}
            </button>
            <button className="w-full flex items-center gap-2 px-3 py-2 rounded-md text-sm text-text-secondary hover:bg-bg-tertiary">
              <Search className="w-4 h-4" /> {t('action.search')}
            </button>
          </div>
        </nav>

        {/* Bottom status */}
        <div className="px-4 py-3 border-t border-border text-xs">
          <div className="flex items-center gap-2 text-text-secondary">
            <StatusDot status={connectionStatus} />
            {t(`status.${connectionStatus}`)}
          </div>
          <div className="mt-1 text-text-secondary">
            Agents: {activeAgents} · Issues: {openIssues}
          </div>
        </div>
      </aside>

      {/* Center content */}
      <main className="flex-1 min-w-0 flex flex-col bg-bg">
        <Outlet />
      </main>

      {/* Right context panel */}
      <aside className="w-80 border-l border-border bg-panel shrink-0 flex flex-col">
        <div className="px-4 py-3 border-b border-border font-medium text-sm">{t('panel.context')}</div>
        <div className="flex-1 p-4 text-sm text-text-secondary">
          当前页面：{t(`nav.${currentRoute}`)}
        </div>
      </aside>
    </div>
  )
}
