import { BrowserPlatformAdapter } from './BrowserPlatformAdapter'
import type { PlatformAdapter } from './PlatformAdapter'

export class TauriPlatformAdapter extends BrowserPlatformAdapter implements PlatformAdapter {
  // TODO: bridge to Tauri Rust commands for daemon management, secure storage, and shell ops.
}
