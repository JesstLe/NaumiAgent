// Adapted from Beautiful UI SelectionActions, MIT (see LICENSE).
// Native selection feeds the shared composer; no simulated edit result.
import { useEffect, useState } from 'react'
import { ChatBubbleQuestion, Scissor, Spark, EmojiSatisfied, TextBox } from 'iconoir-react'
import { useWorkspace } from '@naumi/shared/hooks/WorkspaceProvider'
import { selectionPrompt } from '@naumi/shared/api/selection'
import Primitive from './upstream/components/primitives/SelectionActions'

export function SelectionActions({ focusComposer }: { focusComposer: () => void }) {
  const w = useWorkspace()
  const [selection, setSelection] = useState<{ text: string; x: number; y: number } | null>(null)
  useEffect(() => {
    const update = () => {
      if (document.activeElement?.closest('.bui-selection-anchor')) return
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
      setSelection({ text, x: Math.max(8, Math.min(rect.left, innerWidth - 460)), y: Math.max(8, Math.min(rect.bottom + 8, innerHeight - 56)) })
    }
    const clear = () => setSelection(null)
    const onScroll = (event: Event) => { if (!(event.target instanceof Element && event.target.closest('.bui-selection-anchor'))) clear() }
    const key = (event: KeyboardEvent) => { if (event.key === 'Escape') { clear(); window.getSelection()?.removeAllRanges() } }
    document.addEventListener('selectionchange', update)
    window.addEventListener('resize', clear)
    document.addEventListener('scroll', onScroll, true)
    document.addEventListener('keydown', key)
    return () => { document.removeEventListener('selectionchange', update); window.removeEventListener('resize', clear); document.removeEventListener('scroll', onScroll, true); document.removeEventListener('keydown', key) }
  }, [])
  useEffect(() => setSelection(null), [w.sessionId])
  if (!selection) return null
  const prepare = (action: string) => {
    const known: Record<string, 'explain' | 'improve' | 'shorten'> = { '解释': 'explain', '改写': 'improve', '精简': 'shorten' }
    w.setDraft(known[action] ? selectionPrompt(known[action], selection.text, w.draft) : `${w.draft.trim() ? w.draft.trimEnd() + '\n\n' : ''}请按以下要求处理选中文字：${action}\n\n${selection.text.split('\n').map(line => `> ${line}`).join('\n')}`)
    window.getSelection()?.removeAllRanges()
    setSelection(null); focusComposer()
  }
  return <div className="bui-root bui-selection-anchor" role="toolbar" aria-label="选中文字操作" style={{ left: selection.x, top: selection.y }} onPointerDown={event => { if (!(event.target instanceof HTMLInputElement)) event.preventDefault() }}>
    <Primitive externalAnchor={{ x: Math.min(230, (innerWidth - 16) / 2), y: 0 }} text={{ lead: '', original: selection.text, rewrite: '' }} labels={{ placeholder: '告诉 Agent 如何修改…', keep: '保留', discard: '放弃' }} actions={{
      primary: [{ id: '解释', action: '解释', icon: <ChatBubbleQuestion width={14} height={14} /> }, { id: '改写', action: '改写', icon: <Spark width={14} height={14} /> }],
      more: [{ id: '精简', action: '精简', icon: <Scissor width={14} height={14} /> }, { id: '语气', action: '调整语气', icon: <EmojiSatisfied width={14} height={14} /> }, { id: '语法', action: '纠正语法', icon: <TextBox width={14} height={14} /> }],
    }} onAction={prepare} />
  </div>
}
