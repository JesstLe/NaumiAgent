import { invoke } from '@tauri-apps/api/core'
import { BrowserPlatformAdapter } from './BrowserPlatformAdapter'
import type { PlatformAdapter } from './PlatformAdapter'

export class TauriPlatformAdapter extends BrowserPlatformAdapter implements PlatformAdapter {
  async getToken(): Promise<string | null> {
    try {
      return await invoke<string | null>('plugin:secure-storage|get_token')
    } catch {
      return super.getToken()
    }
  }

  async setToken(token: string): Promise<void> {
    try {
      await invoke('plugin:secure-storage|set_token', { token })
    } catch {
      await super.setToken(token)
    }
  }

  async removeToken(): Promise<void> {
    try {
      await invoke('plugin:secure-storage|remove_token')
    } catch {
      await super.removeToken()
    }
  }
}
