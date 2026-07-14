import { create } from 'zustand'
import type { Locale } from '@/i18n'

interface LocaleState {
  locale: Locale
  setLocale: (locale: Locale) => void
}

export const useLocaleStore = create<LocaleState>((set) => ({
  locale: 'zh-CN',
  setLocale: (locale) => set({ locale }),
}))
