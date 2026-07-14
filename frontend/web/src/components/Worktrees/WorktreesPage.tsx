import { useTranslation } from 'react-i18next'

export function WorktreesPage() {
  const { t } = useTranslation()
  return (
    <div className="p-6">
      <h1 className="text-xl font-semibold mb-4">{t('nav.worktrees')}</h1>
      <p className="text-text-secondary">Worktrees 占位页面</p>
    </div>
  )
}
