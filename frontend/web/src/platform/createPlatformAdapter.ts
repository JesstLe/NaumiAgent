import { BrowserPlatformAdapter } from './BrowserPlatformAdapter'
import { TauriPlatformAdapter } from './TauriPlatformAdapter'
import type { PlatformAdapter } from './PlatformAdapter'

export function createPlatformAdapter(): PlatformAdapter {
  if (typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window) {
    return new TauriPlatformAdapter()
  }
  return new BrowserPlatformAdapter()
}
