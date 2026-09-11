// Adapted from Beautiful UI SelectionActions, MIT (see LICENSE).
// Native selection feeds the shared composer; no simulated edit result.
import { useEffect, useState } from 'react'
import { Copy, MessageSquareText, Scissors, Sparkles, X } from 'lucide-react'
import { useWorkspace } from '@naumi/shared/hooks/WorkspaceProvider'
import { selectionPrompt } from '@naumi/shared/api/selection'

export function SelectionActions({ focusComposer }: { focusComposer: () => void }) {
  const w = useWorkspace()
  const [selection, setSelection] = useState<{ text: string; x: number; y: number } | null>(null)
  useEffect(() => {
    const update = () => {
      if (document.activeElement?.closest('.bui-selection')) return
      const chosen = window.getSelection()
      if (!chosen?.rangeCount || chosen.isCollapsed) { setSelection(null); return }
      const range = chosen.getRangeAt(0)
      const element = (node: Node) => node.nodeType === Node.ELEMENT_NODE ? node as Element : node.parentElement
      const start = element(range.startContainer)?.closest('.w2-message.assistant')
      const end = element(range.endContainer)?.closest('.w2-message.assistant')
      if (!start || start !== end || !element(range.commonAncestorContainer)?.closest('.w2-message.assistant')) { setSelection(null); return }
      const text = chosen.toString().trim()
      const rect = range.getBoundingClientRect()
      if (!text || rect.bottom < 0 || rect.top > innerHeight) { setSelection(null); return }
      setSelection({ text, x: Math.max(8, Math.min(rect.left, innerWidth - 340)), y: Math.max(8, Math.min(rect.top - 48, innerHeight - 56)) })
    }
    const clear = () => setSelection(null)
    const key = (event: KeyboardEvent) => { if (event.key === 'Escape') { clear(); window.getSelection()?.removeAllRanges() } }
    document.addEventListener('selectionchange', update)
    window.addEventListener('resize', clear)
    document.addEventListener('scroll', clear, true)
    document.addEventListener('keydown', key)
    return () => { document.removeEventListener('selectionchange', update); window.removeEventListener('resize', clear); document.removeEventListener('scroll', clear, true); document.removeEventListener('keydown', key) }
  }, [])
  useEffect(() => setSelection(null), [w.sessionId])
  if (!selection) return null
  const prepare = (action: 'explain' | 'improve' | 'shorten') => {
    w.setDraft(selectionPrompt(action, selection.text, w.draft))
    window.getSelection()?.removeAllRanges()
    setSelection(null); focusComposer()
  }
  return <div className="bui-selection" role="toolbar" aria-label="选中文字操作" style={{ left: selection.x, top: selection.y }} onPointerDown={event => event.preventDefault()}>
    <button disabled={w.busy || w.connecting || w.loading} onClick={() => prepare('explain')}><MessageSquareText size={13} />解释</button>
    <button disabled={w.busy || w.connecting || w.loading} onClick={() => prepare('improve')}><Sparkles size={13} />改写</button>
    <button disabled={w.busy || w.connecting || w.loading} onClick={() => prepare('shorten')}><Scissors size={13} />精简</button>
    <button aria-label="复制选中文字" onClick={() => { void navigator.clipboard.writeText(selection.text).then(() => setSelection(null)).catch(() => w.setError('复制失败，请按 Ctrl+C 复制')) }}><Copy size={13} /></button>
    <button aria-label="关闭文字操作" onClick={() => { setSelection(null); window.getSelection()?.removeAllRanges() }}><X size={13} /></button>
  </div>
}
