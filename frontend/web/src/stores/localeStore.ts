import { create } from 'zustand'
import i18n from '@/i18n'
import type { Locale } from '@/i18n'

export type { Locale }

interface LocaleState {
  locale: Locale
  setLocale: (locale: Locale) => void
  initialize: (savedLocale?: string | null) => void
}

export const useLocaleStore = create<LocaleState>((set) => ({
  locale: 'zh-CN',

  setLocale: (locale) => {
    i18n.changeLanguage(locale)
    set({ locale })
  },

  initialize: (savedLocale) => {
    const locale: Locale = savedLocale === 'zh-CN' || savedLocale === 'en-US' ? savedLocale : 'zh-CN'
    i18n.changeLanguage(locale)
    set({ locale })
  },
}))
