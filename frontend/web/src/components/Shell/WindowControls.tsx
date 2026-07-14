import { getCurrentWindow } from '@tauri-apps/api/window'
import { Minus, Square, X } from 'lucide-react'

export function WindowControls() {
  return (
    <div className="flex items-center">
      <button
        type="button"
        onClick={() => void getCurrentWindow().minimize()}
        className="p-1.5 text-text-secondary hover:text-text transition-colors"
        aria-label="最小化"
      >
        <Minus className="w-4 h-4" />
      </button>
      <button
        type="button"
        onClick={() => void getCurrentWindow().toggleMaximize()}
        className="p-1.5 text-text-secondary hover:text-text transition-colors"
        aria-label="最大化"
      >
        <Square className="w-4 h-4" />
      </button>
      <button
        type="button"
        onClick={() => void getCurrentWindow().close()}
        className="p-1.5 text-text-secondary hover:text-danger transition-colors"
        aria-label="关闭"
      >
        <X className="w-4 h-4" />
      </button>
    </div>
  )
}
