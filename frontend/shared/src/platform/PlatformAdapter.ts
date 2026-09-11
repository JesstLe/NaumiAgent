// Platform adapter interface.
// BrowserPlatformAdapter and TauriPlatformAdapter implement this.

/** Daemon launch configuration mirrored from the Rust DaemonLaunchConfig. */
export interface DaemonLaunchConfig {
  executable?: string | null
  args?: string[]
  working_dir?: string | null
  port?: number | null
  env_vars?: Record<string, string>
}

/** Status of the local daemon process. */
export interface DaemonStatus {
  running: boolean
  pid: number | null
  port: number | null
  url: string | null
  executable: string | null
  last_error: string | null
}

export interface PlatformAdapter {
  getSetting(key: string): Promise<string | null>
  setSetting(key: string, value: string): Promise<void>
  getToken(): Promise<string | null>
  setToken(token: string): Promise<void>
  removeToken(): Promise<void>
  log(level: 'info' | 'warn' | 'error', message: string): Promise<void>

  // --- Native-only capabilities (Tauri). Browser returns false / throws. ---

  /** Whether this adapter can manage a local daemon process. */
  readonly supportsDaemon: boolean
  /** Whether this adapter can open paths in Explorer / Git Bash. */
  readonly supportsShell: boolean

  getDaemonLaunchConfig?(): Promise<DaemonLaunchConfig | null>
  setDaemonLaunchConfig?(config: DaemonLaunchConfig): Promise<void>
  startDaemon?(config: DaemonLaunchConfig): Promise<DaemonStatus>
  stopDaemon?(): Promise<DaemonStatus>
  getDaemonStatus?(): Promise<DaemonStatus>
  getDaemonLogs?(limit: number): Promise<string[]>

  openInExplorer?(path: string): Promise<void>
  openInTerminal?(path: string): Promise<void>
}
