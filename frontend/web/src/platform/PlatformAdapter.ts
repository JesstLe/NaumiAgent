// Platform adapter interface.
// BrowserPlatformAdapter and TauriPlatformAdapter implement this.
export interface PlatformAdapter {
  getSetting(key: string): Promise<string | null>
  setSetting(key: string, value: string): Promise<void>
  getToken(): Promise<string | null>
  setToken(token: string): Promise<void>
  removeToken(): Promise<void>
  log(level: 'info' | 'warn' | 'error', message: string): Promise<void>
}
