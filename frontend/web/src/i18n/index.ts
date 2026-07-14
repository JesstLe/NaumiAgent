import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'
import zhCN from './zh-CN.json'
import enUS from './en-US.json'

const resources = {
  'zh-CN': { translation: zhCN },
  'en-US': { translation: enUS },
}

i18n.use(initReactI18next).init({
  resources,
  lng: 'zh-CN',
  fallbackLng: 'en-US',
  interpolation: {
    escapeValue: false,
  },
})

export default i18n
export type Locale = 'zh-CN' | 'en-US'
