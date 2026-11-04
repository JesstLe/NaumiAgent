import { useEffect, useRef, useState } from 'react'
import { ArrowDownToLine, ChevronLeft, ChevronRight, File, FileText, FolderOpen, Grid2X2, Library, Loader2, Plus, RefreshCw, Search, Share2, Upload, X } from 'lucide-react'
import type { PersonalAgent } from '@naumi/shared/api/WorkbenchRuntimeClient'
import type { PersonalArtifactGrant, PersonalArtifactRef, PersonalArtifactSnapshot } from '@naumi/shared/api/personalArtifacts'
import { ArtifactPage, actorName, type ArtifactSelection } from './ArtifactPage'
import { ArtifactEditor, type ArtifactEditorValue } from './ArtifactEditor'
import { artifactError, useArtifactLibrary, validateArtifact, validateArtifactRef, type ArtifactApi } from './useArtifactLibrary'
import { requestArtifactNavigation } from './navigation'
import './library.css'

type LocationState = { actorId: string; selection: ArtifactSelection | null; error: string }
export function readArtifactLocation(search: string, defaultActor = 'main'): LocationState {
  const params = new URLSearchParams(search), actorId = params.get('actor') || defaultActor
  const id = params.get('artifact'), rawVersion = params.get('version'), version = rawVersion === null ? undefined : Number(rawVersion)
  if (!/^(main|pa_[a-f0-9]{32})$/.test(actorId) || id !== null && !/^art_[a-f0-9]{32}$/.test(id)
    || rawVersion !== null && (!id || !/^[1-9][0-9]*$/.test(rawVersion) || !Number.isSafeInteger(version)))
    return { actorId, selection: null, error: '资料链接的身份或版本不完整，请核对原链接' }
  return { actorId, selection: id ? { artifact_id: id, version } : null, error: '' }
}

export function writeArtifactLocation(actorId: string, selection: ArtifactSelection | null, replace = false) {
  const url = new URL(location.href)
  url.searchParams.set('library', '1'); url.searchParams.set('actor', actorId)
  if (selection) {
    url.searchParams.set('artifact', selection.artifact_id)
    if (selection.version !== undefined) url.searchParams.set('version', String(selection.version))
    else url.searchParams.delete('version')
  } else { url.searchParams.delete('artifact'); url.searchParams.delete('version') }
  history[replace ? 'replaceState' : 'pushState']({}, '', url)
}

export function clearArtifactLocation() {
  const url = new URL(location.href)
  for (const key of ['library', 'actor', 'artifact', 'version']) url.searchParams.delete(key)
  history.pushState({}, '', url)
}

export function LibraryWorkspace({ api, namespace, initialActorId, currentActorId, sessionId, agents, connected, onClose, onConversationRefresh }: {
  api: ArtifactApi; namespace: string; initialActorId: string; currentActorId: string; sessionId?: string
  agents: PersonalAgent[]; connected: boolean; onClose: () => void; onConversationRefresh?: () => void
}) {
  const [route, setRoute] = useState(() => readArtifactLocation(location.search, initialActorId))
  useEffect(() => {
    const update = () => setRoute(readArtifactLocation(location.search, initialActorId))
    window.addEventListener('popstate', update)
    return () => window.removeEventListener('popstate', update)
  }, [initialActorId])
  const acceptedRoute = (actorId: string, selection: ArtifactSelection | null, replace = false) => {
    writeArtifactLocation(actorId, selection, replace)
    setRoute({ actorId, selection, error: '' })
  }
  const navigate = (actorId: string, selection: ArtifactSelection | null, replace = false) => requestArtifactNavigation(() => acceptedRoute(actorId, selection, replace))
  if (route.error) return <section className="al-workspace" aria-label="资料库"><header className="al-library-heading"><h1><Library size={21} />资料库</h1><button onClick={onClose}><X size={17} />返回对话</button></header><div className="al-error" role="alert">{route.error}</div></section>
  return <section className="al-workspace" aria-label="资料库">
    <header className="al-library-heading"><div><Library size={21} /><h1>资料库</h1><select aria-label="资料库所属 Agent" value={route.actorId} onChange={event => navigate(event.target.value, null)}>
      <option value="main">主 Agent</option>{agents.map(agent => <option key={agent.id} value={agent.id}>{agent.name}{agent.status === 'active' ? '' : agent.status === 'paused' ? ' · 已暂停' : ' · 已归档'}</option>)}
      {route.actorId !== 'main' && !agents.some(agent => agent.id === route.actorId) && <option value={route.actorId}>{route.actorId}</option>}
    </select></div><button aria-label="关闭资料库" onClick={() => requestArtifactNavigation(onClose)}><X size={18} /></button></header>
    <LibraryActorWorkspace key={`${namespace}:${route.actorId}`} api={api} namespace={namespace} actorId={route.actorId} selection={route.selection}
      agents={agents} connected={connected} onOpen={value => navigate(route.actorId, value)} onAcceptedOpen={value => acceptedRoute(route.actorId, value)} onBack={() => navigate(route.actorId, null)}
      sessionId={route.actorId === currentActorId ? sessionId : undefined} onConversationRefresh={onConversationRefresh} />
  </section>
}

function LibraryActorWorkspace({ api, namespace, actorId, selection, agents, connected, sessionId, onOpen, onAcceptedOpen, onBack, onConversationRefresh }: {
  api: ArtifactApi; namespace: string; actorId: string; selection: ArtifactSelection | null; agents: PersonalAgent[]
  connected: boolean; sessionId?: string; onOpen: (value: ArtifactSelection) => void; onAcceptedOpen: (value: ArtifactSelection) => void; onBack: () => void; onConversationRefresh?: () => void
}) {
  const [editor, setEditor] = useState<ArtifactEditorValue | null>(null), [refreshToken, setRefreshToken] = useState(0), [abandon, setAbandon] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null), retryFile = useRef<HTMLInputElement>(null)
  const writable = actorId === 'main' || agents.some(agent => agent.id === actorId && agent.status === 'active')
  const state = useArtifactLibrary({ api, namespace, actorId, connected, onAccepted: value => {
    setRefreshToken(count => count + 1)
    if ('kind' in value) { setEditor(null); onAcceptedOpen(value) }
    if (sessionId) onConversationRefresh?.()
  } })
  useEffect(() => { setEditor(null); setAbandon(false) }, [selection?.artifact_id, selection?.version])
  const pendingFile = state.pending?.operation === 'import' || state.pending?.operation === 'import_version'
  const retry = () => { if (pendingFile) retryFile.current?.click(); else void state.retry() }
  const scopedState = { ...state, locked: state.locked || !writable }
  return <div className="al-actor-workspace">
    {(state.pending || state.identityError) && <div className="al-pending" role="status"><div><Loader2 size={15} /><strong>保留原请求</strong>{state.pending && <code>{state.pending.body.request_id}</code>}</div>
      {state.identityError && <span>{state.identityError}</span>}
      {pendingFile && state.pending && 'file' in state.pending && <span>{state.pending.file.name} · {state.pending.file.bytes.toLocaleString()} B</span>}
      <div><button disabled={state.posting || !state.pending || !connected} onClick={retry}>{pendingFile ? '选择原文件重试' : '原样重试'}</button>
        {state.pending?.receipt && <button onClick={() => onOpen(state.pending!.receipt!)}>查看已受理版本</button>}
        <button disabled={state.posting} onClick={() => setAbandon(true)}>放弃重试</button></div>
    </div>}
    {abandon && <div className="al-confirm" role="group" aria-label="确认放弃资料请求重试"><strong>放弃原请求重试？</strong><p>这不会撤销已经创建的版本或分享。先核对资料和分享记录。</p><div><button onClick={() => setAbandon(false)}>保留原请求</button><button disabled={state.posting} onClick={() => { state.abandon(); setAbandon(false) }}>确认放弃重试</button></div></div>}
    {state.error && <div className="al-error" role="alert">{state.error}<button disabled={state.loading} onClick={state.refresh}>核对资料目录</button></div>}
    {!connected && <div className="al-error" role="status">连接本地服务后查看资料</div>}
    {editor ? <ArtifactEditor key={editor.item ? `${editor.item.artifact_id}:${editor.item.version}` : 'new'} value={editor} api={api} actorId={actorId}
      state={scopedState} sessionId={sessionId} onClose={() => setEditor(null)} />
      : selection ? <ArtifactPage key={`${selection.artifact_id}:${selection.version ?? 'latest'}`} api={api} actorId={actorId} selection={selection} agents={agents}
        state={scopedState} refreshToken={refreshToken} sessionId={sessionId} onOpen={onOpen} onBack={onBack} onEdit={(item, content) => setEditor({ item, content })} />
        : <>
          <div className="al-list-toolbar"><div className="al-filters" role="group" aria-label="资料分类">{(['', 'material', 'output'] as const).map(kind => <button key={kind || 'all'} aria-pressed={state.kind === kind} onClick={() => state.setKind(kind)}>{kind === 'material' ? '材料' : kind === 'output' ? '产物' : '全部'}</button>)}</div>
            <div className="al-list-actions"><button disabled={scopedState.locked} onClick={() => fileInput.current?.click()}><Upload size={15} />上传资料</button><button className="al-primary" disabled={scopedState.locked} onClick={() => setEditor({ content: '' })}><Plus size={15} />新建文档</button></div>
          </div>
          <div className="al-search-row"><label className="al-search"><Search size={16} /><input aria-label="搜索资料库" placeholder="搜索资料" value={state.query} onChange={event => state.setQuery(event.target.value)} /></label><button aria-label="刷新资料目录" disabled={state.loading} onClick={state.refresh}><RefreshCw size={15} className={state.loading ? 'pa-spin' : ''} /></button></div>
          {state.loading && <div className="al-loading" role="status"><Loader2 size={20} className="pa-spin" />正在读取资料…</div>}
          {!state.loading && !state.error && !state.artifacts.length && <div className="al-empty"><FolderOpen size={32} /><strong>{state.query || state.kind ? '没有匹配的资料' : '暂无资料'}</strong></div>}
          {!state.loading && <div className="al-artifact-grid" aria-label="资料清单">{state.artifacts.map(item => <ArtifactTile key={item.artifact_id} item={item} actorId={actorId} agents={agents} onOpen={() => onOpen(item)} />)}</div>}
          {(state.offset > 0 || state.hasNext) && <nav className="al-list-pagination" aria-label="资料分页"><button disabled={!state.offset || state.loading} onClick={state.previous}><ChevronLeft size={15} />上一页</button><span>第 {state.page} 页</span><button disabled={!state.hasNext || state.loading} onClick={state.next}>下一页<ChevronRight size={15} /></button></nav>}
        </>}
    <input ref={fileInput} className="al-hidden" type="file" aria-label="选择上传资料" onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; if (file) void state.importFile(file, sessionId) }} />
    <input ref={retryFile} className="al-hidden" type="file" aria-label="选择原请求文件" onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; if (file) void state.retry(file) }} />
  </div>
}

function ArtifactTile({ item, actorId, agents, onOpen }: { item: PersonalArtifactSnapshot; actorId: string; agents: PersonalAgent[]; onOpen: () => void }) {
  const icon = item.suffix.replace(/^\./, '').toLowerCase()
  return <button className="al-artifact-tile" onClick={onOpen} aria-label={`打开${item.title} v${item.version}`}>
    <div className={`al-tile-preview al-tile-${icon}`}><span>{icon === 'csv' ? <Grid2X2 size={30} /> : ['md', 'txt', 'json'].includes(icon) ? <FileText size={30} /> : <File size={30} />}</span><code>{icon.toUpperCase()}</code></div>
    <div className="al-tile-heading"><strong>{item.title}</strong><code>v{item.version}</code></div>
    <div className="al-tile-meta"><span>{item.kind === 'material' ? '材料' : '产物'}</span><span>{item.bytes < 1024 ? `${item.bytes} B` : item.bytes < 1024 * 1024 ? `${(item.bytes / 1024).toFixed(1)} KB` : `${(item.bytes / (1024 * 1024)).toFixed(1)} MB`}</span><time>{new Date(item.created_at).toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' })}</time></div>
    {item.owner_id !== actorId && <span className="al-shared-owner"><Share2 size={12} />{actorName(item.owner_id, agents)}</span>}
  </button>
}

export function messageArtifactRefs(metadata: Record<string, unknown>): { refs: PersonalArtifactRef[]; error: string } {
  if (metadata.artifact_refs === undefined) return { refs: [], error: '' }
  try {
    if (!Array.isArray(metadata.artifact_refs) || metadata.artifact_refs.length > 32) throw new Error('产物引用尚未完整')
    return { refs: metadata.artifact_refs.map(value => validateArtifactRef(value)), error: '' }
  } catch { return { refs: [], error: '产物引用尚未完整，请核对原执行记录' } }
}

function MessageArtifactCard({ reference, api, actorId, onOpen }: { reference: PersonalArtifactRef; api: ArtifactApi; actorId: string; onOpen: (value: ArtifactSelection) => void }) {
  const [item, setItem] = useState<PersonalArtifactSnapshot | null>(null), [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    setItem(null); setError('')
    void api.personalArtifact(reference.artifact_id, actorId, reference.version, controller.signal).then(value => {
      validateArtifact(value, { id: reference.artifact_id, version: reference.version })
      if (value.sha256 !== reference.sha256) throw new Error('产物摘要与原消息不一致')
      if (!controller.signal.aborted) setItem(value)
    }).catch(cause => { if (!controller.signal.aborted) setError(artifactError(cause)) })
    return () => controller.abort()
  }, [api, actorId, reference.artifact_id, reference.version, reference.sha256])
  return <button className="al-chat-card" onClick={() => onOpen(reference)}><FileText size={20} /><span><strong>{item?.title || (error ? '产物访问待核对' : '正在读取产物…')}</strong><small>v{reference.version}{error ? ` · ${error}` : item ? ` · ${item.bytes.toLocaleString()} B` : ''}</small></span><ArrowDownToLine size={16} /></button>
}

export function MessageArtifactCards({ metadata, api, actorId, onOpen }: { metadata: Record<string, unknown>; api: ArtifactApi; actorId: string; onOpen: (value: ArtifactSelection) => void }) {
  const value = messageArtifactRefs(metadata)
  if (value.error) return <div className="al-error" role="status">{value.error}</div>
  return value.refs.length ? <div className="al-chat-artifacts" aria-label="消息产物">{value.refs.map((reference, index) => <MessageArtifactCard key={`${reference.artifact_id}:${reference.version}:${index}`} reference={reference} api={api} actorId={actorId} onOpen={onOpen} />)}</div> : null
}

export type ArtifactMutationResult = PersonalArtifactSnapshot | PersonalArtifactGrant
