import { useTranslation } from 'react-i18next'
import { usePlatform } from '@/platform'
import { useLocaleStore, type Locale } from '@/stores/localeStore'

export function SettingsPage() {
  const { t, i18n } = useTranslation()
  const platform = usePlatform()
  const { locale, setLocale } = useLocaleStore()

  async function handleLanguageChange(next: Locale) {
    setLocale(next)
    await platform.setSetting('locale', next)
  }

  return (
    <div className="p-6">
      <h1 className="text-xl font-semibold mb-4">{t('nav.settings')}</h1>

      <section className="space-y-3">
        <h2 className="text-sm font-medium text-text-secondary uppercase tracking-wider">Language / 语言</h2>
        <div className="flex gap-2">
          <button
            onClick={() => handleLanguageChange('zh-CN')}
            className={`px-3 py-1.5 rounded-md text-sm border ${
              locale === 'zh-CN'
                ? 'bg-accent text-white border-accent'
                : 'bg-bg border-border hover:bg-bg-tertiary'
            }`}
          >
            简体中文
          </button>
          <button
            onClick={() => handleLanguageChange('en-US')}
            className={`px-3 py-1.5 rounded-md text-sm border ${
              locale === 'en-US'
                ? 'bg-accent text-white border-accent'
                : 'bg-bg border-border hover:bg-bg-tertiary'
            }`}
          >
            English
          </button>
        </div>
        <div className="text-xs text-text-secondary">Current i18n language: {i18n.language}</div>
      </section>
    </div>
  )
}
