import { invoke } from '@tauri-apps/api/core'
import { BrowserPlatformAdapter } from './BrowserPlatformAdapter'
import type { DaemonLaunchConfig, DaemonStatus, PlatformAdapter } from './PlatformAdapter'

// Rust serializes camelCase via serde rename, but the Tauri commands use plain
// snake_case struct fields. We pass camelCase objects that mirror DaemonLaunchConfig
// and the Rust side accepts the matching serde rename_all = "camelCase".
export class TauriPlatformAdapter extends BrowserPlatformAdapter implements PlatformAdapter {
  readonly supportsDaemon: boolean = true
  readonly supportsShell: boolean = true

  async getToken(): Promise<string | null> {
    try {
      return await invoke<string | null>('get_token')
    } catch {
      return super.getToken()
    }
  }

  async setToken(token: string): Promise<void> {
    try {
      await invoke('set_token', { token })
    } catch {
      await super.setToken(token)
    }
  }

  async removeToken(): Promise<void> {
    try {
      await invoke('remove_token')
    } catch {
      await super.removeToken()
    }
  }

  async getSetting(key: string): Promise<string | null> {
    try {
      return await invoke<string | null>('get_setting', { key })
    } catch {
      return super.getSetting(key)
    }
  }

  async setSetting(key: string, value: string): Promise<void> {
    try {
      await invoke('set_setting', { key, value })
    } catch {
      await super.setSetting(key, value)
    }
  }

  async getDaemonLaunchConfig(): Promise<DaemonLaunchConfig | null> {
    return invoke<DaemonLaunchConfig | null>('get_daemon_launch_config')
  }

  async setDaemonLaunchConfig(config: DaemonLaunchConfig): Promise<void> {
    await invoke('set_daemon_launch_config', { config })
  }

  async startDaemon(config: DaemonLaunchConfig): Promise<DaemonStatus> {
    return invoke<DaemonStatus>('start_daemon', { config })
  }

  async stopDaemon(): Promise<DaemonStatus> {
    return invoke<DaemonStatus>('stop_daemon')
  }

  async getDaemonStatus(): Promise<DaemonStatus> {
    return invoke<DaemonStatus>('get_daemon_status')
  }

  async getDaemonLogs(limit: number): Promise<string[]> {
    return invoke<string[]>('get_daemon_logs', { limit })
  }

  async openInExplorer(path: string): Promise<void> {
    await invoke('open_in_explorer', { path })
  }

  async openInTerminal(path: string): Promise<void> {
    await invoke('open_in_terminal', { path })
  }
}
