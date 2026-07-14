import { useTranslation } from 'react-i18next'

export function TimelinePage() {
  const { t } = useTranslation()
  return (
    <div className="p-6">
      <h1 className="text-xl font-semibold mb-4">{t('nav.timeline')}</h1>
      <p className="text-text-secondary">Timeline 占位页面</p>
    </div>
  )
}
