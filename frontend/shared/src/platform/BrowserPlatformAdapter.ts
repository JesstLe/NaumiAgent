import type { PlatformAdapter } from './PlatformAdapter'

const KEY_PREFIX = 'naumi:'

function resolveKey(key: string): string {
  return `${KEY_PREFIX}${key}`
}

export class BrowserPlatformAdapter implements PlatformAdapter {
  readonly supportsDaemon: boolean = false
  readonly supportsShell: boolean = false

  async getSetting(key: string): Promise<string | null> {
    return localStorage.getItem(resolveKey(key))
  }

  async setSetting(key: string, value: string): Promise<void> {
    localStorage.setItem(resolveKey(key), value)
  }

  async getToken(): Promise<string | null> {
    return localStorage.getItem(resolveKey('token'))
  }

  async setToken(token: string): Promise<void> {
    localStorage.setItem(resolveKey('token'), token)
  }

  async removeToken(): Promise<void> {
    localStorage.removeItem(resolveKey('token'))
  }

  async log(level: 'info' | 'warn' | 'error', message: string): Promise<void> {
    console[level](message)
  }
}
