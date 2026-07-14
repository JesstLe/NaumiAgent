import { useTranslation } from 'react-i18next'

export function SettingsPage() {
  const { t } = useTranslation()
  return (
    <div className="p-6">
      <h1 className="text-xl font-semibold mb-4">{t('nav.settings')}</h1>
      <p className="text-text-secondary">Settings 占位页面</p>
    </div>
  )
}
