import { useTranslation } from 'react-i18next'
import { useSessionStore } from '@/stores/sessionStore'
import { Users, CircleAlert, ShieldAlert, ClipboardCheck, XCircle } from 'lucide-react'

export function DashboardPage() {
  const { t } = useTranslation()
  const snapshot = useSessionStore((state) => state.snapshot)
  const summary = snapshot?.summary

  const cards = [
    { label: t('dashboard.activeAgents'), value: summary?.active_agents ?? 0, icon: Users, color: 'text-blue-600', bg: 'bg-blue-50' },
    { label: t('dashboard.openIssues'), value: summary?.open_issues ?? 0, icon: CircleAlert, color: 'text-amber-600', bg: 'bg-amber-50' },
    { label: t('dashboard.blockedIssues'), value: summary?.blocked_issues ?? 0, icon: ShieldAlert, color: 'text-red-600', bg: 'bg-red-50' },
    { label: t('dashboard.pendingApprovals'), value: summary?.pending_approvals ?? 0, icon: ClipboardCheck, color: 'text-purple-600', bg: 'bg-purple-50' },
    { label: t('dashboard.failedValidations'), value: summary?.failed_validations ?? 0, icon: XCircle, color: 'text-rose-600', bg: 'bg-rose-50' },
  ]

  return (
    <div className="p-6 h-full overflow-y-auto">
      <h1 className="text-xl font-semibold mb-6 text-text">{t('nav.dashboard')}</h1>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
        {cards.map((card) => {
          const Icon = card.icon
          return (
            <div key={card.label} className="rounded-lg border border-border bg-panel p-4 shadow-sm">
              <div className="flex items-center justify-between">
                <div className={`p-2 rounded-md ${card.bg}`}>
                  <Icon className={`w-5 h-5 ${card.color}`} />
                </div>
                <div className="text-2xl font-semibold text-text">{card.value}</div>
              </div>
              <div className="mt-2 text-sm text-text-secondary">{card.label}</div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
