import type { PlatformAdapter } from './PlatformAdapter'

export class BrowserPlatformAdapter implements PlatformAdapter {
  async getSetting(key: string): Promise<string | null> {
    return localStorage.getItem(key)
  }

  async setSetting(key: string, value: string): Promise<void> {
    localStorage.setItem(key, value)
  }

  async getToken(): Promise<string | null> {
    return localStorage.getItem('naumi_token')
  }

  async setToken(token: string): Promise<void> {
    localStorage.setItem('naumi_token', token)
  }

  async removeToken(): Promise<void> {
    localStorage.removeItem('naumi_token')
  }

  async log(level: 'info' | 'warn' | 'error', message: string): Promise<void> {
    console[level](message)
  }
}
