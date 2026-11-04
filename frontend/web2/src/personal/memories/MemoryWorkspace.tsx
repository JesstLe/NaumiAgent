import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { ArrowLeft, BrainCircuit, Check, ChevronRight, Download, History, Link2, Loader2, Pencil, Plus, RefreshCw, Search, Share2, Trash2, X } from 'lucide-react'
import type { PersonalAgent } from '@naumi/shared/api/WorkbenchRuntimeClient'
import type { PersonalItem } from '@naumi/shared/api/personalItems'
import type { PersonalLegacyMemory, PersonalMemory, PersonalMemoryFilter, PersonalMemoryGrant, PersonalMemoryHistoryEntry, PersonalMemoryMutationResult, PersonalMemoryOperation, PersonalMemoryPayload, PersonalMemoryRef, PersonalMemoryScopeKind } from '@naumi/shared/api/personalMemories'
import { requestArtifactNavigation } from '../library/navigation'
import { MemoryEditor, memoryCategoryNames } from './MemoryEditor'
import { memoryError, usePersonalMemories, validateMemory, validateMemoryGrant, type PersonalMemoriesApi, type PersonalMemoriesState } from './usePersonalMemories'
import './memory.css'

const stateNames: Record<string, string> = { active: '已确认', proposed: '待确认', superseded: '已替换', rejected: '已拒绝', deleted: '已删除', conflict: '冲突' }
const scopeNames: Record<string, string> = { all: '全部范围', personal: '个人', workspace: '当前工作区', item: '事项' }
const authorityNames: Record<string, string> = { user: '用户', assistant: 'Agent 建议', external: '外部资料', unknown: '未核实来源' }
const sourceNames: Record<string, string> = { manual: '手动输入', message: '对话消息', artifact: '资料', legacy: '旧记忆', compaction: '会话整理' }
const operationNames: Record<string, string> = { create: '保存', propose: '建议', confirm: '确认', revise: '纠正', delete: '删除', reject: '拒绝', share: '分享', revoke: '撤销', import_legacy: '导入' }
const date = (value?: string) => value && Number.isFinite(Date.parse(value)) ? new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)) : '—'
const conflicting = (memory: PersonalMemory) => memory.state === 'proposed' && Boolean(memory.active_ref && memory.active_ref.memory_id !== memory.memory_id)
const memoryTitle = (memory: PersonalMemory) => memory.state === 'deleted' ? '已删除的记忆' : memory.topic || '记忆'
const sameRef = (a: PersonalMemoryRef | null, b: PersonalMemoryRef | null) => a && b && a.memory_id === b.memory_id && a.revision === b.revision && a.content_sha256 === b.content_sha256 && a.grant_id === b.grant_id && a.grant_revision === b.grant_revision

export interface MemoryRoute { actorId: string; scope: PersonalMemoryScopeKind | 'all'; itemId: string; memoryId: string; error: string }
export function readMemoryLocation(search: string, fallback = 'main'): MemoryRoute {
  const params = new URLSearchParams(search), actor = params.get('memory_actor') || fallback
  const scope = params.get('memory_scope') || 'all', itemId = params.get('memory_item') || '', memoryId = params.get('memory_id') || ''
  const valid = /^(main|pa_[a-f0-9]{32})$/.test(actor) && ['all', 'personal', 'workspace', 'item'].includes(scope)
    && (!itemId || /^pi_[a-f0-9]{32}$/.test(itemId)) && (!memoryId || /^pm_[a-f0-9]{32}$/.test(memoryId))
  return { actorId: actor, scope: scope as MemoryRoute['scope'], itemId, memoryId, error: valid ? '' : '记忆地址不完整，请重新选择' }
}
export function writeMemoryLocation(route: Omit<MemoryRoute, 'error'>) {
  const url = new URL(location.href)
  url.searchParams.set('memories', '1'); url.searchParams.set('memory_actor', route.actorId); url.searchParams.set('memory_scope', route.scope)
  for (const [key, value] of [['memory_item', route.itemId], ['memory_id', route.memoryId]]) value ? url.searchParams.set(key, value) : url.searchParams.delete(key)
  history.pushState({}, '', url)
}
export function clearMemoryLocation() {
  const url = new URL(location.href)
  for (const key of ['memories', 'memory_actor', 'memory_scope', 'memory_item', 'memory_id']) url.searchParams.delete(key)
  history.pushState({}, '', url)
}
function Status({ memory }: { memory: PersonalMemory }) {
  return <span className={`pm-status pm-status-${conflicting(memory) ? 'conflict' : memory.state}`}>{conflicting(memory) ? '冲突' : stateNames[memory.state]}</span>
}
function useMemoryModal(onClose: () => void, blocked = false) {
  const ref = useRef<HTMLDivElement>(null), close = useRef(onClose), disabled = useRef(blocked)
  close.current = onClose; disabled.current = blocked
  const controls = () => Array.from(ref.current?.querySelectorAll<HTMLElement>('button,select,input,textarea,a[href],[tabindex]') || [])
    .filter(element => element.tabIndex >= 0 && !(element as HTMLButtonElement).disabled && !element.closest('[hidden],fieldset[disabled]'))
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    controls()[0]?.focus()
    return () => previous?.focus()
  }, [])
  return { ref, onKeyDown: (event: KeyboardEvent<HTMLElement>) => {
    if (event.key === 'Escape' && !disabled.current) { event.preventDefault(); close.current() }
    if (event.key === 'Tab') {
      const elements = controls()
      if (elements.length && (event.shiftKey ? document.activeElement === elements[0] : document.activeElement === elements.at(-1))) { event.preventDefault(); (event.shiftKey ? elements.at(-1) : elements[0])?.focus() }
    }
  } }
}

export function MemoryWorkspace({ api, namespace, initialActorId, agents, connected, onClose }: {
  api: PersonalMemoriesApi; namespace: string; initialActorId: string; agents: PersonalAgent[]; connected: boolean; onClose: () => void
}) {
  const [route, setRoute] = useState(() => readMemoryLocation(location.search, initialActorId))
  useEffect(() => {
    const update = () => setRoute(readMemoryLocation(location.search, initialActorId))
    window.addEventListener('popstate', update)
    return () => window.removeEventListener('popstate', update)
  }, [initialActorId])
  const navigate = (next: Omit<MemoryRoute, 'error'>) => requestArtifactNavigation(() => { writeMemoryLocation(next); setRoute({ ...next, error: '' }) })
  const acceptNavigate = (next: Omit<MemoryRoute, 'error'>) => { writeMemoryLocation(next); setRoute({ ...next, error: '' }) }
  if (route.error) return <section className="pm-workspace" aria-label="记忆"><header className="pm-heading"><h1>记忆</h1><button aria-label="关闭记忆" onClick={onClose}><X size={18} /></button></header><div className="pm-error" role="alert">{route.error}</div></section>
  return <MemoryActorWorkspace key={`${namespace}:${route.actorId}`} api={api} namespace={namespace} route={route} agents={agents} connected={connected} onClose={onClose} onNavigate={navigate} onAcceptedNavigate={acceptNavigate} />
}

function MemoryActorWorkspace({ api, namespace, route, agents, connected, onClose, onNavigate, onAcceptedNavigate }: {
  api: PersonalMemoriesApi; namespace: string; route: MemoryRoute; agents: PersonalAgent[]; connected: boolean
  onClose: () => void; onNavigate: (route: Omit<MemoryRoute, 'error'>) => void
  onAcceptedNavigate: (route: Omit<MemoryRoute, 'error'>) => void
}) {
  const state = usePersonalMemories({ api, namespace, actorId: route.actorId, scope: route.scope, itemId: route.itemId, connected })
  const [items, setItems] = useState<PersonalItem[]>([]), [itemsError, setItemsError] = useState('')
  const [editor, setEditor] = useState<PersonalMemory | 'create' | null>(null)
  const [legacy, setLegacy] = useState(false), [abandon, setAbandon] = useState(false), [actionError, setActionError] = useState('')
  const actor = agents.find(a => a.id === route.actorId), writeActive = route.actorId === 'main' || actor?.status === 'active'
  const alive = useRef(true), currentRoute = useRef(route)
  const currentRefresh = useRef(state.refreshToken)
  currentRefresh.current = state.refreshToken
  currentRoute.current = route
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  useEffect(() => { setEditor(null) }, [route.scope, route.itemId])
  useEffect(() => {
    setItems([]); setItemsError('')
    if (!connected) return
    const controller = new AbortController()
    void (async () => {
      const all: PersonalItem[] = []; let offset = 0
      while (!controller.signal.aborted) {
        const page = await api.personalItems({ limit: 100, offset }, controller.signal)
        if (!Array.isArray(page.items)) throw new Error('事项目录不完整')
        all.push(...page.items)
        if (page.next_offset === null) break
        if (!Number.isSafeInteger(page.next_offset) || page.next_offset <= offset) throw new Error('事项目录分页未前进')
        offset = page.next_offset
      }
      if (!controller.signal.aborted) setItems(all)
    })().catch(cause => { if (!controller.signal.aborted) setItemsError(memoryError(cause)) })
    return () => controller.abort()
  }, [api, namespace, connected])
  useEffect(() => {
    if (!editor || editor === 'create') return
    const controller = new AbortController()
    void api.personalMemory(editor.memory_id, route.actorId, controller.signal).then(current => {
      validateMemory(current, route.actorId)
      if (!controller.signal.aborted && (!current.source_available || current.state === 'deleted')) {
        setEditor(null); setActionError('原记忆已删除或来源已撤销，编辑内容已清除')
      }
    }).catch(cause => { if (!controller.signal.aborted) { setEditor(null); setActionError(memoryError(cause)) } })
    return () => controller.abort()
  }, [api, route.actorId, editor, state.refreshToken])
  const accepted = (value: PersonalMemoryMutationResult | null) => {
    const current = currentRoute.current
    if (!alive.current || current.actorId !== route.actorId || current.scope !== route.scope || current.itemId !== route.itemId || current.memoryId !== route.memoryId) return false
    if (value && 'memory_id' in value && !('grant_id' in value)) {
      setEditor(null); onAcceptedNavigate({ ...route, memoryId: value.memory_id })
    }
    return Boolean(value)
  }
  const exportMemory = async () => {
    setActionError('')
    try {
      const originalRoute = currentRoute.current, originalRefresh = currentRefresh.current
      const value = await api.exportPersonalMemories({ actor_id: route.actorId, scope: route.scope, ...(route.scope === 'item' ? { item_id: route.itemId } : {}) })
      if (!alive.current || currentRoute.current !== originalRoute || currentRefresh.current !== originalRefresh) throw new Error('记忆上下文已更新，请重新导出当前内容')
      if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('导出内容不完整')
      const blob = new Blob([JSON.stringify(value, null, 2) + '\n'], { type: 'application/json;charset=utf-8' })
      const url = URL.createObjectURL(blob), anchor = document.createElement('a')
      anchor.href = url; anchor.download = `NaumiAgent-记忆-${new Date().toISOString().slice(0, 10)}.json`
      document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (cause) { setActionError(memoryError(cause)) }
  }
  return <section className="pm-workspace" aria-label="记忆">
    <header className="pm-heading"><div><h1><BrainCircuit size={23} />记忆</h1><select aria-label="记忆所属 Agent" value={route.actorId} disabled={state.posting} onChange={event => onNavigate({ actorId: event.target.value, scope: 'all', itemId: '', memoryId: '' })}>
      <option value="main">主 Agent</option>{agents.map(agent => <option key={agent.id} value={agent.id}>{agent.name}{agent.status === 'active' ? '' : agent.status === 'paused' ? ' · 已暂停' : ' · 已归档'}</option>)}
      {route.actorId !== 'main' && !actor && <option value={route.actorId}>{route.actorId}</option>}
    </select></div><div><button aria-label="刷新记忆" disabled={!connected || state.loading || state.posting} onClick={() => state.refresh()}><RefreshCw size={17} className={state.loading ? 'pa-spin' : ''} /></button><button aria-label="关闭记忆" onClick={() => requestArtifactNavigation(onClose)}><X size={19} /></button></div></header>
    {(state.pending || state.error) && <>{(state.pending || state.pendingError) && <div className="pm-pending" role="status"><strong>原记忆请求待核对</strong>{state.pending && <code>{state.pending.request_id}</code>}<div><button disabled={!state.pending || state.posting || !connected} onClick={() => { void state.retry().then(accepted) }}>原样核对</button><button disabled={state.posting || (Boolean(state.pending) && !connected)} onClick={() => setAbandon(true)}>{state.pending ? '清理草稿' : '清理本地记录'}</button></div></div>}{state.error && <div className="pm-error" role="alert">{state.error}</div>}</>}
    {state.notice && <div className="pm-notice" role="status">{state.notice}</div>}
    {actionError && <div className="pm-error" role="alert">{actionError}</div>}
    {!connected && <div className="pm-error" role="status">连接本地服务后查看记忆</div>}
    {abandon && <Confirm title={state.pending ? '清理原记忆草稿？' : '清理损坏的本地记录？'} body={state.pending ? '仅清理未提交的草稿。已经提交的操作会恢复当前结果，不会删除记忆。' : '仅移除本地损坏记录，不改变服务端操作状态。'} confirm={state.pending ? '确认清理草稿' : '清理本地记录'} locked={state.posting} cancelLocked={state.posting} onCancel={() => setAbandon(false)} onConfirm={() => {
      if (!state.pending) { state.discardCorrupt(); setAbandon(false); return }
      void state.discard().then(value => { if (value && alive.current) { setAbandon(false); accepted(value.result) } })
    }} />}
    <div className="pm-toolbar"><div className="pm-scopes" role="group" aria-label="记忆范围">{(['all', 'personal', 'workspace', 'item'] as const).map(scope => <button key={scope} aria-pressed={route.scope === scope} onClick={() => onNavigate({ ...route, scope, itemId: '', memoryId: '' })}>{scopeNames[scope]}</button>)}</div><div className="pm-tools"><button disabled={state.locked || !writeActive} onClick={() => requestArtifactNavigation(() => setLegacy(true))}><History size={15} />旧记忆</button><button disabled={!connected || !state.context || state.posting || (route.scope === 'item' && !route.itemId)} onClick={() => void exportMemory()}><Download size={15} />导出</button><button className="pm-primary" disabled={state.locked || !writeActive} onClick={() => requestArtifactNavigation(() => setEditor('create'))}><Plus size={16} />新建记忆</button></div></div>
    {route.scope === 'item' && <div className="pm-item-context"><select aria-label="记忆所属事项" value={route.itemId} onChange={event => onNavigate({ ...route, itemId: event.target.value, memoryId: '' })}><option value="">选择事项</option>{items.map(item => <option key={item.item_id} value={item.item_id}>{item.definition.title} · {item.definition.assignee.name}</option>)}</select>{itemsError && <span role="alert">{itemsError}</span>}</div>}
    {editor ? <MemoryEditor key={editor === 'create' ? 'create' : `${editor.memory_id}:${editor.revision}`} memory={editor === 'create' ? undefined : editor} initialScope={route.scope} initialItemId={route.itemId} items={items} locked={state.locked || !writeActive} onClose={() => setEditor(null)} onSave={async fields => accepted(await state.submit(editor === 'create' ? 'create' : 'revise', fields))} /> : <div className="pm-body"><div className="pm-list-pane">
      <div className="pm-filters" role="group" aria-label="记忆状态">{(['active', 'proposed', 'conflict', ''] as PersonalMemoryFilter[]).map(filter => <button key={filter || 'all'} aria-pressed={state.filter === filter} onClick={() => state.setFilter(filter)}>{filter ? stateNames[filter] : '全部'}</button>)}</div>
      <label className="pm-search"><Search size={16} /><input aria-label="搜索记忆" placeholder="搜索记忆" maxLength={200} value={state.query} onChange={event => state.setQuery(event.target.value)} /></label>
      {state.loading && <div className="pm-loading" role="status"><Loader2 size={15} className="pa-spin" />正在加载…</div>}
      <div className="pm-list" aria-label="记忆列表">{state.memories.map(memory => <button key={`${memory.memory_id}:${memory.ref?.grant_id || ''}`} className={`pm-card${memory.memory_id === route.memoryId ? ' selected' : ''}`} onClick={() => onNavigate({ ...route, memoryId: memory.memory_id })}><div><strong>{memoryTitle(memory)}</strong><ChevronRight size={15} /></div><p>{memory.source_available ? memory.content : memory.error || (memory.state === 'deleted' ? '正文已删除' : '来源暂不可查看')}</p><div><span>{scopeNames[memory.scope.kind]}{memory.access === 'shared' ? ' · 已共享给我' : ''}</span><Status memory={memory} /></div><small>{date(memory.updated_at)}</small></button>)}{!state.loading && !state.memories.length && <div className="pm-empty">{route.scope === 'item' && !route.itemId ? '选择事项' : state.query ? '没有匹配结果' : state.filter === 'proposed' ? '暂无待确认记忆' : state.filter === 'conflict' ? '暂无冲突' : '暂无记忆'}</div>}</div>
      {state.diagnostics && <details className="pm-diagnostics"><summary>检索依据</summary><pre>{JSON.stringify(state.diagnostics, null, 2)}</pre></details>}
      <footer className="pm-pagination"><button disabled={state.offset === 0 || state.loading || Boolean(state.query && state.filter === 'active')} onClick={() => state.setOffset(Math.max(0, state.offset - 30))}>上一页</button><span>{Math.floor(state.offset / 30) + 1}</span><button disabled={state.nextOffset === null || state.loading} onClick={() => state.setOffset(state.nextOffset!)}>下一页</button></footer>
    </div><div className={`pm-detail-pane${route.memoryId ? ' has-selection' : ''}`}>{route.memoryId ? <><button className="pm-mobile-back" onClick={() => onNavigate({ ...route, memoryId: '' })}><ArrowLeft size={15} />返回列表</button><MemoryDetail key={`${route.actorId}:${route.memoryId}`} api={api} memoryId={route.memoryId} actorId={route.actorId} agents={agents} state={state} writeActive={writeActive} onEdit={memory => setEditor(memory)} /></> : <div className="pm-empty pm-detail-empty"><BrainCircuit size={29} />选择记忆</div>}</div></div>}
    {legacy && <LegacyMemories api={api} actorId={route.actorId} state={state} items={items} initialScope={route.scope} itemId={route.itemId} onClose={() => setLegacy(false)} onAccepted={value => { setLegacy(false); accepted(value) }} />}
  </section>
}

function MemoryDetail({ api, memoryId, actorId, agents, state, writeActive, onEdit }: {
  api: PersonalMemoriesApi; memoryId: string; actorId: string; agents: PersonalAgent[]; state: PersonalMemoriesState
  writeActive: boolean; onEdit: (memory: PersonalMemory) => void
}) {
  const [memory, setMemory] = useState<PersonalMemory | null>(null), [active, setActive] = useState<PersonalMemory | null>(null)
  const [historyRows, setHistoryRows] = useState<PersonalMemoryHistoryEntry[] | null>(null), [grants, setGrants] = useState<PersonalMemoryGrant[] | null>(null)
  const [error, setError] = useState(''), [busy, setBusy] = useState(false), [share, setShare] = useState(false)
  const [action, setAction] = useState<{ operation: PersonalMemoryOperation; title: string; button: string; payload: Omit<PersonalMemoryPayload, 'request_id'> } | null>(null)
  useEffect(() => {
    setMemory(null); setActive(null); setHistoryRows(null); setGrants(null); setError(''); setShare(false)
    const controller = new AbortController()
    if (state.posting) return () => controller.abort()
    void api.personalMemory(memoryId, actorId, controller.signal).then(async value => {
      validateMemory(value, actorId)
      if (value.memory_id !== memoryId) throw new Error('记忆身份不一致，请重新加载')
      if (controller.signal.aborted) return
      setMemory(value)
      if (conflicting(value) && value.active_ref) {
        const current = validateMemory(await api.personalMemory(value.active_ref.memory_id, actorId, controller.signal), actorId)
        if (!sameRef(current.ref, value.active_ref)) throw new Error('当前记忆已变化，请刷新后确认冲突')
        if (!controller.signal.aborted) setActive(current)
      }
    }).catch(cause => { if (!controller.signal.aborted) { setMemory(null); setActive(null); setError(memoryError(cause)) } })
    return () => controller.abort()
  }, [api, memoryId, actorId, state.refreshToken, state.posting])
  const history = async () => {
    setBusy(true); setError('')
    try { const value = await api.personalMemoryHistory(memoryId, actorId); if (!Array.isArray(value.history)) throw new Error('修订记录不完整'); setHistoryRows(value.history) }
    catch (cause) { setError(memoryError(cause)) } finally { setBusy(false) }
  }
  const loadGrants = async () => {
    setBusy(true); setError('')
    try { const value = await api.personalMemoryGrants(memoryId, actorId); if (!Array.isArray(value.grants)) throw new Error('分享记录不完整'); setGrants(value.grants.map(validateMemoryGrant)) }
    catch (cause) { setError(memoryError(cause)) } finally { setBusy(false) }
  }
  const readonly = !memory || memory.access !== 'owned' || !writeActive || !memory.source_available || ['deleted', 'superseded', 'rejected'].includes(memory.state)
  if (state.posting || (!memory && !error)) return <div className="pm-loading" role="status"><Loader2 size={17} className="pa-spin" />正在核对记忆…</div>
  if (!memory) return <div className="pm-error" role="alert">{error}</div>
  const source = memory.source
  const queue = (operation: PersonalMemoryOperation, title: string, button: string, extra: Partial<PersonalMemoryPayload> = {}) => setAction({ operation, title, button, payload: { memory_id: memory.memory_id, expected_revision: memory.revision, ...extra } })
  return <article className="pm-detail">
    <header><div className="pm-detail-meta"><Status memory={memory} /><span>{scopeNames[memory.scope.kind]}</span><span>{memoryCategoryNames[memory.category]}</span>{memory.access === 'shared' && <span><Share2 size={12} />已共享给我</span>}</div><h2>{memoryTitle(memory)}</h2><time>{date(memory.updated_at)}</time></header>
    {error && <div className="pm-error" role="alert">{error}</div>}
    {memory.error && <div className="pm-error" role="status">{memory.error}</div>}
    {memory.content !== null && <section className="pm-content" aria-label="记忆内容"><p>{memory.content}</p></section>}
    {source && memory.source_available && <section className="pm-source" aria-label="记忆来源"><h3>来源</h3><div><span>{sourceNames[source.kind]}</span><span>{authorityNames[source.authority]}</span>{memory.confirmation && <span><Check size={12} />人工确认</span>}</div>{source.excerpt && <blockquote>{source.excerpt}</blockquote>}<details><summary>原始依据</summary><dl><dt>来源时间</dt><dd>{date(source.captured_at)}</dd><dt>来源摘要</dt><dd><code>{memory.source_sha256}</code></dd><dt>原始对象</dt><dd><pre>{JSON.stringify(source.locator, null, 2)}</pre></dd>{source.artifact_refs.length > 0 && <><dt>确切资料版本</dt><dd>{source.artifact_refs.map(ref => <p key={`${ref.artifact_id}:${ref.version}:${ref.grant_id}`}><code>{ref.artifact_id} · v{ref.version}<br />{ref.sha256}</code></p>)}</dd></>}</dl></details></section>}
    {conflicting(memory) && <section className="pm-conflict" aria-label="记忆冲突"><h3>当前记忆</h3>{active ? <><strong>{memoryTitle(active)}</strong><p>{active.content || active.error || '当前正文不可查看'}</p><small>修订 {active.revision}</small></> : <div className="pm-loading"><Loader2 size={14} className="pa-spin" />正在核对当前修订…</div>}</section>}
    <div className="pm-detail-actions">
      {memory.access === 'owned' && memory.state === 'proposed' && <><button className="pm-primary" disabled={readonly || state.locked || (conflicting(memory) && !active)} onClick={() => queue('confirm', conflicting(memory) ? '替换当前记忆？' : '确认保存这条记忆？', conflicting(memory) ? '确认替换' : '确认保存', { expected_content_sha256: memory.content_sha256!, expected_active_ref: memory.active_ref })}><Check size={15} />{conflicting(memory) ? '替换为此条' : '确认保存'}</button><button disabled={readonly || state.locked} onClick={() => queue('reject', '拒绝这条待确认记忆？', '确认拒绝')}>拒绝</button></>}
      {memory.access === 'owned' && memory.state === 'active' && <button disabled={readonly || state.locked} onClick={() => onEdit(memory)}><Pencil size={15} />纠正</button>}
      {memory.access === 'owned' && memory.state === 'active' && <button disabled={readonly || state.locked} onClick={() => setShare(true)}><Share2 size={15} />分享</button>}
      {!['deleted', 'superseded', 'rejected'].includes(memory.state) && memory.access === 'owned' && <button className="pm-danger" disabled={state.locked || !writeActive} onClick={() => queue('delete', '删除这条记忆？', '确认删除')}><Trash2 size={15} />删除</button>}
      <button disabled={busy || state.posting} onClick={() => void history()}><History size={15} />修订记录</button>
      {memory.access === 'owned' && <button disabled={busy || state.posting} onClick={() => void loadGrants()}><Link2 size={15} />分享记录</button>}
    </div>
    {historyRows && <section className="pm-history" aria-label="记忆修订记录"><h3>修订记录</h3>{historyRows.length ? historyRows.map((row, index) => <div key={`${row.revision || index}:${row.operation || ''}`}><strong>{row.revision ? `修订 ${row.revision}` : '操作记录'}</strong><span>{stateNames[row.state || ''] || operationNames[row.operation || ''] || '已记录'}</span><time>{date(row.occurred_at || row.updated_at || row.created_at)}</time>{row.content_sha256 && <code>{row.content_sha256}</code>}</div>) : <p>暂无修订记录</p>}</section>}
    {grants && <section className="pm-grants" aria-label="记忆分享记录"><h3>分享记录</h3>{grants.length ? grants.map(grant => <div key={grant.grant_id}><div><strong>{grant.recipient_actor_id === 'main' ? '主 Agent' : agents.find(agent => agent.id === grant.recipient_actor_id)?.name || grant.recipient_actor_id}</strong><span>{grant.state === 'active' ? '已分享' : '已撤销'}</span><small>记忆修订 {grant.memory_revision}</small></div>{grant.state === 'active' && <button disabled={state.locked || !writeActive} onClick={() => setAction({ operation: 'revoke', title: '撤销这份记忆分享？', button: '确认撤销', payload: { grant_id: grant.grant_id, expected_revision: grant.revision } })}>撤销</button>}</div>) : <p>暂无分享记录</p>}</section>}
    <details className="pm-identities"><summary>记忆身份</summary><dl><dt>ID</dt><dd><code>{memory.memory_id}</code></dd><dt>修订</dt><dd>{memory.revision}</dd><dt>正文摘要</dt><dd><code>{memory.content_sha256 || '已清除'}</code></dd>{memory.ref?.grant_id && <><dt>分享凭据</dt><dd><code>{memory.ref.grant_id} · {memory.ref.grant_revision}</code></dd></>}</dl></details>
    {action && <Confirm title={action.title} confirm={action.button} locked={state.locked} cancelLocked={state.posting} onCancel={() => setAction(null)} onConfirm={() => { const original = action; void state.submit(original.operation, original.payload).then(value => { if (value) setAction(null) }) }} />}
    {share && <ShareMemory memory={memory} actorId={actorId} agents={agents} state={state} onClose={() => setShare(false)} />}
  </article>
}

function Confirm({ title, body, confirm, locked = false, cancelLocked = false, onCancel, onConfirm }: { title: string; body?: string; confirm: string; locked?: boolean; cancelLocked?: boolean; onCancel: () => void; onConfirm: () => void }) {
  const modal = useMemoryModal(onCancel, cancelLocked)
  return <div className="pm-modal-backdrop"><div {...modal} className="pm-confirm" role="alertdialog" aria-label={title} aria-modal="true"><strong>{title}</strong>{body && <p>{body}</p>}<div><button disabled={cancelLocked} onClick={onCancel}>取消</button><button className="pm-primary" disabled={locked} onClick={onConfirm}>{cancelLocked && <Loader2 size={14} className="pa-spin" />}{confirm}</button></div></div></div>
}

function ShareMemory({ memory, actorId, agents, state, onClose }: { memory: PersonalMemory; actorId: string; agents: PersonalAgent[]; state: PersonalMemoriesState; onClose: () => void }) {
  const [recipient, setRecipient] = useState('')
  const modal = useMemoryModal(onClose, state.posting)
  const available = [{ id: 'main', name: '主 Agent', version: 0, status: 'active' }, ...agents].filter(agent => agent.id !== actorId && agent.status === 'active')
  const selected = available.find(agent => agent.id === recipient)
  return <div className="pm-modal-backdrop"><div {...modal} className="pm-share" role="dialog" aria-modal="true" aria-label="分享记忆"><header><h2>分享记忆</h2><button aria-label="关闭分享记忆" onClick={onClose} disabled={state.posting}><X size={18} /></button></header><strong>{memoryTitle(memory)}</strong><span>修订 {memory.revision}</span><label>接收 Agent<select aria-label="记忆接收 Agent" value={recipient} disabled={state.locked} onChange={event => setRecipient(event.target.value)}><option value="">选择 Agent</option>{available.map(agent => <option key={agent.id} value={agent.id}>{agent.name}</option>)}</select></label><footer><button disabled={state.posting} onClick={onClose}>取消</button><button className="pm-primary" disabled={!selected || state.locked} onClick={() => { if (selected) void state.submit('share', { memory_id: memory.memory_id, expected_revision: memory.revision, expected_content_sha256: memory.content_sha256!, recipient_actor_id: selected.id, recipient_version: selected.version }).then(value => { if (value) onClose() }) }}>确认分享</button></footer></div></div>
}

function LegacyMemories({ api, actorId, state, items, initialScope, itemId, onClose, onAccepted }: {
  api: PersonalMemoriesApi; actorId: string; state: PersonalMemoriesState; items: PersonalItem[]; initialScope: MemoryRoute['scope']; itemId: string
  onClose: () => void; onAccepted: (result: PersonalMemoryMutationResult) => void
}) {
  const [rows, setRows] = useState<PersonalLegacyMemory[]>([]), [offset, setOffset] = useState(0), [next, setNext] = useState<number | null>(null)
  const [error, setError] = useState(''), [loading, setLoading] = useState(false), [selection, setSelection] = useState<PersonalLegacyMemory | null>(null)
  const [topic, setTopic] = useState(''), [scope, setScope] = useState<'workspace' | 'item'>(initialScope === 'item' ? 'item' : 'workspace'), [selectedItem, setSelectedItem] = useState(itemId)
  const modal = useMemoryModal(onClose, state.posting)
  useEffect(() => {
    const controller = new AbortController(); setLoading(true); setRows([])
    void api.legacyPersonalMemories(actorId, 30, offset, controller.signal).then(page => {
      if (controller.signal.aborted) return
      if (!Array.isArray(page.memories) || page.memories.some(row => !row.legacy_id || typeof row.content !== 'string' || !/^[a-f0-9]{64}$/.test(row.content_sha256))) throw new Error('旧记忆记录不完整')
      setRows(page.memories); setNext(page.next_offset); setError('')
    }).catch(cause => { if (!controller.signal.aborted) setError(memoryError(cause)) }).finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [api, actorId, offset])
  return <div className="pm-modal-backdrop"><div {...modal} className="pm-legacy" role="dialog" aria-modal="true" aria-label="旧记忆"><header><h2>旧记忆</h2><button aria-label="关闭旧记忆" onClick={onClose} disabled={state.posting}><X size={18} /></button></header>{error && <div className="pm-error" role="alert">{error}</div>}{selection ? <><button onClick={() => setSelection(null)} disabled={state.posting}><ArrowLeft size={15} />返回旧记忆</button><label>主题<input aria-label="导入记忆主题" maxLength={120} value={topic} disabled={state.locked} onChange={event => setTopic(event.target.value)} /></label><div className="pm-source"><span>旧记忆 · 待确认</span><pre>{selection.content}</pre></div><label>导入范围<select aria-label="旧记忆导入范围" value={scope} disabled={state.locked} onChange={event => setScope(event.target.value as 'workspace' | 'item')}><option value="workspace">当前工作区</option><option value="item">事项</option></select></label>{scope === 'item' && <label>事项<select aria-label="旧记忆导入事项" value={selectedItem} disabled={state.locked} onChange={event => setSelectedItem(event.target.value)}><option value="">选择事项</option>{items.map(item => <option key={item.item_id} value={item.item_id}>{item.definition.title}</option>)}</select></label>}<footer><button disabled={state.posting} onClick={onClose}>取消</button><button className="pm-primary" disabled={state.locked || !topic.trim() || (scope === 'item' && !selectedItem)} onClick={() => void state.submit('import_legacy', { legacy_id: selection.legacy_id, expected_content_sha256: selection.content_sha256, topic: topic.trim(), scope, ...(scope === 'item' ? { item_id: selectedItem } : {}) }).then(value => { if (value) onAccepted(value) })}>导入待确认</button></footer></> : <>{loading ? <div className="pm-loading"><Loader2 size={16} className="pa-spin" />正在读取…</div> : rows.map(row => <button key={row.legacy_id} className="pm-legacy-row" onClick={() => { setSelection(row); setTopic((row.topic || row.content).slice(0, 120)) }}><strong>{row.topic || '旧记忆'}</strong><p>{row.content}</p><ChevronRight size={16} /></button>)}{!loading && !rows.length && <div className="pm-empty">暂无旧记忆</div>}<footer><button disabled={offset === 0 || loading} onClick={() => setOffset(Math.max(0, offset - 30))}>上一页</button><span>{Math.floor(offset / 30) + 1}</span><button disabled={next === null || loading} onClick={() => setOffset(next!)}>下一页</button></footer></>}</div></div>
}
