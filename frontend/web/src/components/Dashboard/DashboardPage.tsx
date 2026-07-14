import { useTranslation } from 'react-i18next'

export function DashboardPage() {
  const { t } = useTranslation()
  return (
    <div className="p-6">
      <h1 className="text-xl font-semibold mb-4">{t('nav.dashboard')}</h1>
      <p className="text-text-secondary">Dashboard 占位页面</p>
    </div>
  )
}
