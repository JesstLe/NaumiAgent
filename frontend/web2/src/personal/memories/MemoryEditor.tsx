import { useEffect, useRef, useState, type FormEvent } from 'react'
import { ArrowLeft, Loader2 } from 'lucide-react'
import type { PersonalMemory, PersonalMemoryCategory, PersonalMemoryPayload, PersonalMemoryScopeKind } from '@naumi/shared/api/personalMemories'
import type { PersonalItem } from '@naumi/shared/api/personalItems'
import { artifactNavigationEvent, requestArtifactNavigation, type ArtifactNavigation } from '../library/navigation'

export const memoryCategoryNames: Record<PersonalMemoryCategory, string> = {
  preference: '偏好', fact: '事实', decision: '决定', experience: '经验', plan_template: '计划模板',
}
export function MemoryEditor({ memory, initialScope, initialItemId, items, locked, onClose, onSave }: {
  memory?: PersonalMemory; initialScope: PersonalMemoryScopeKind | 'all'; initialItemId: string
  items: PersonalItem[]; locked: boolean; onClose: () => void
  onSave: (payload: Omit<PersonalMemoryPayload, 'request_id'>) => Promise<boolean>
}) {
  const [topic, setTopic] = useState(memory?.topic || ''), [content, setContent] = useState(memory?.content || '')
  const [category, setCategory] = useState<PersonalMemoryCategory>(memory?.category || 'preference')
  const [scope, setScope] = useState<PersonalMemoryScopeKind>(memory?.scope.kind || (initialScope === 'all' ? 'workspace' : initialScope))
  const [itemId, setItemId] = useState(memory?.scope.item_id || initialItemId)
  const [error, setError] = useState(''), [leaving, setLeaving] = useState<ArtifactNavigation | null>(null)
  const dialog = useRef<HTMLDivElement>(null)
  const saved = useRef(false), original = useRef(JSON.stringify({ topic, content, category, scope, itemId }))
  const dirty = JSON.stringify({ topic, content, category, scope, itemId }) !== original.current
  useEffect(() => {
    const guard = (event: Event) => {
      if (!dirty || saved.current) return
      event.preventDefault(); setLeaving((event as CustomEvent<ArtifactNavigation>).detail)
    }
    const unload = (event: BeforeUnloadEvent) => { if (dirty && !saved.current) { event.preventDefault(); event.returnValue = '' } }
    window.addEventListener(artifactNavigationEvent, guard); window.addEventListener('beforeunload', unload)
    return () => { window.removeEventListener(artifactNavigationEvent, guard); window.removeEventListener('beforeunload', unload) }
  }, [dirty])
  useEffect(() => {
    if (!leaving) return
    const previous = document.activeElement as HTMLElement | null
    dialog.current?.querySelector<HTMLButtonElement>('button')?.focus()
    return () => previous?.focus()
  }, [leaving])
  const save = async (event: FormEvent) => {
    event.preventDefault(); setError('')
    if (!content.trim() || content.length > 8000 || (!memory && (!topic.trim() || topic.length > 120))) {
      setError('请填写完整主题和正文'); return
    }
    if (scope === 'item' && !itemId) { setError('请选择实际事项'); return }
    const fields: Omit<PersonalMemoryPayload, 'request_id'> = memory
      ? { memory_id: memory.memory_id, expected_revision: memory.revision, expected_content_sha256: memory.content_sha256!, content }
      : { topic: topic.trim(), category, content, scope, ...(scope === 'item' ? { item_id: itemId } : {}) }
    if (await onSave(fields)) saved.current = true
  }
  return <div className="pm-editor">
    <header><button aria-label="返回记忆" onClick={() => requestArtifactNavigation(onClose)}><ArrowLeft size={18} /></button><h2>{memory ? '纠正记忆' : '新建记忆'}</h2></header>
    <form onSubmit={event => void save(event)}>
      <fieldset disabled={locked}>
        <div className="pm-form-grid">
          <label>主题<input aria-label="记忆主题" autoFocus required maxLength={120} value={topic} disabled={Boolean(memory)} onChange={event => setTopic(event.target.value)} /></label>
          <label>类别<select aria-label="记忆类别" value={category} disabled={Boolean(memory)} onChange={event => setCategory(event.target.value as PersonalMemoryCategory)}>{Object.entries(memoryCategoryNames).map(([value, name]) => <option key={value} value={value}>{name}</option>)}</select></label>
          <label>范围<select aria-label="保存记忆范围" value={scope} disabled={Boolean(memory)} onChange={event => { setScope(event.target.value as PersonalMemoryScopeKind); setItemId('') }}><option value="personal">个人</option><option value="workspace">当前工作区</option><option value="item">事项</option></select></label>
          {scope === 'item' && <label>事项<select aria-label="保存记忆事项" required value={itemId} disabled={Boolean(memory)} onChange={event => setItemId(event.target.value)}><option value="">选择事项</option>{items.map(item => <option key={item.item_id} value={item.item_id}>{item.definition.title}</option>)}</select></label>}
        </div>
        <label>正文<textarea aria-label="记忆正文" required maxLength={8000} rows={10} value={content} onChange={event => setContent(event.target.value)} /></label>
      </fieldset>
      {error && <div className="pm-error" role="alert">{error}</div>}
      <footer><button type="button" onClick={() => requestArtifactNavigation(onClose)}>取消</button><button className="pm-primary" disabled={locked || !content.trim() || (!memory && !topic.trim())}>{locked && <Loader2 size={14} className="pa-spin" />}{memory ? '保存纠正' : '保存记忆'}</button></footer>
    </form>
    {leaving && <div className="pm-modal-backdrop"><div ref={dialog} className="pm-confirm" role="alertdialog" aria-label="确认离开记忆编辑" aria-modal="true" onKeyDown={event => {
      if (event.key === 'Escape') { event.preventDefault(); leaving.cancel?.(); setLeaving(null) }
      if (event.key === 'Tab') {
        const buttons = Array.from(dialog.current?.querySelectorAll<HTMLButtonElement>('button') || [])
        if (buttons.length && (event.shiftKey ? document.activeElement === buttons[0] : document.activeElement === buttons.at(-1))) { event.preventDefault(); (event.shiftKey ? buttons.at(-1) : buttons[0])?.focus() }
      }
    }}><strong>放弃未保存的修改？</strong><div><button onClick={() => { leaving.cancel?.(); setLeaving(null) }}>继续编辑</button><button onClick={() => { const target = leaving; saved.current = true; setLeaving(null); target.proceed() }}>放弃修改</button></div></div></div>}
  </div>
}
