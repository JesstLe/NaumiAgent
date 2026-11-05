import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Copy, Download, X } from 'lucide-react'
import type { WorkspaceRecipient, WorkspaceTicket } from '@naumi/shared/api/personalWorkspaces'
import { artifactNavigationEvent, requestArtifactNavigation, type ArtifactNavigation } from '../library/navigation'
import { parseWorkspaceJson, validateRecipient, validateTicket, validateWorkspaceEndpoint, workspaceCanonical } from './workspaceValidation'
import { workspaceError, type WorkspaceState } from './useWorkspaceOverview'

export const workspaceRequestId = () => `workspace_${crypto.randomUUID().replaceAll('-', '')}`
export function WorkspaceDialog({ title, dirty, busy, onClose, children }: { title: string; dirty: boolean; busy: boolean; onClose: () => void; children: ReactNode }) {
  const dialog = useRef<HTMLElement>(null), released = useRef(false), [leave, setLeave] = useState<ArtifactNavigation | null>(null)
  const close = useRef(onClose); close.current = onClose
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    const target = dialog.current; target?.querySelector<HTMLElement>('input,textarea,select,button')?.focus()
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); requestArtifactNavigation(() => close.current()) }
      if (event.key !== 'Tab' || !target) return
      const nodes = [...target.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex="0"]')]
      const first = nodes[0], last = nodes.at(-1)
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
    }
    target?.addEventListener('keydown', key)
    return () => { target?.removeEventListener('keydown', key); previous?.focus() }
  }, [])
  useEffect(() => {
    const guard = (event: Event) => { if (released.current || !dirty && !busy) return; event.preventDefault(); setLeave((event as CustomEvent<ArtifactNavigation>).detail) }
    const unload = (event: BeforeUnloadEvent) => { if (dirty || busy) { event.preventDefault(); event.returnValue = '' } }
    window.addEventListener(artifactNavigationEvent, guard); window.addEventListener('beforeunload', unload)
    return () => { window.removeEventListener(artifactNavigationEvent, guard); window.removeEventListener('beforeunload', unload) }
  }, [dirty, busy])
  return <div className="pw-backdrop"><section ref={dialog} className="pw-dialog" role="dialog" aria-modal="true" aria-label={title}>
    <header><h2>{title}</h2><button aria-label="关闭对话框" disabled={busy} onClick={() => requestArtifactNavigation(onClose)}><X size={18} /></button></header>
    {leave && <div className="pw-notice" role="alertdialog" aria-label="确认离开工作区表单"><strong>{busy ? '正在核对请求，请等待结果' : '放弃未保存的资料？'}</strong><div className="pw-actions"><button onClick={() => { leave.cancel?.(); setLeave(null) }}>继续编辑</button>{!busy && <button onClick={() => { released.current = true; const next = leave; setLeave(null); onClose(); next.proceed() }}>放弃并离开</button>}</div></div>}
    {children}
  </section></div>
}
function TicketActions({ ticket }: { ticket: WorkspaceTicket }) {
  const [message, setMessage] = useState(''), text = JSON.stringify(ticket, null, 2)
  return <><textarea aria-label="一次性连接凭据" readOnly rows={12} value={text} spellCheck={false} /><div className="pw-actions"><button onClick={() => { void navigator.clipboard.writeText(text).then(() => setMessage('已复制'), () => setMessage('复制未完成，请选择凭据正文复制')) }}><Copy size={15} />复制凭据</button><button onClick={() => { const url = URL.createObjectURL(new Blob([text], { type: 'application/json' })), anchor = document.createElement('a'); anchor.href = url; anchor.download = 'workspace-ticket.json'; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); setMessage('已生成下载') }}><Download size={15} />下载凭据</button></div><p className="pw-status" role="status">{message || '凭据仅在本次窗口显示'}</p></>
}
export function SourceGrantDialog({ state, onClose }: { state: WorkspaceState; onClose: () => void }) {
  const [raw, setRaw] = useState(''), [actorId, setActorId] = useState(state.context?.actors[0]?.id || 'main'), [results, setResults] = useState(true)
  const [expiry, setExpiry] = useState(() => { const date = new Date(Date.now() + 86400000); return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16) })
  const [ticket, setTicket] = useState<WorkspaceTicket | null>(null), [error, setError] = useState('')
  const request = useRef<string | null>(null), close = useRef(onClose); close.current = onClose
  useEffect(() => {
    if (state.completedRequest?.recovered && state.completedRequest.request_id === request.current && state.completedRequest.operation === 'issue') { setRaw(''); close.current() }
    if (ticket && state.grants.some(row => row.id === ticket.grant_id && row.status === 'revoked')) { setTicket(null); close.current() }
  }, [state.completedRequest, state.grants, ticket])
  const initial = useRef({ actorId, results, expiry })
  const dirty = Boolean(raw || ticket || actorId !== initial.current.actorId || results !== initial.current.results || expiry !== initial.current.expiry)
  const issue = async (event: React.FormEvent) => {
    event.preventDefault(); setError('')
    try {
      const card = validateRecipient(parseWorkspaceJson(raw) as WorkspaceRecipient), actor = state.context?.actors.find(row => row.id === actorId)
      if (!actor) throw new Error('请选择当前源 Agent')
      const date = new Date(expiry)
      if (!Number.isFinite(date.getTime()) || date.getTime() <= Date.now()) throw new Error('请选择未来的到期时间')
      request.current = workspaceRequestId()
      const receipt = await state.submit('issue', { payload: { request_id: request.current, recipient: card, source_actor_id: actor.id, source_actor_version: actor.version, scopes: results ? ['items', 'results'] : ['items'], expires_at: date.toISOString() } })
      if (receipt) { setRaw(''); if ('ticket' in receipt) setTicket(receipt.ticket); else onClose() }
    } catch (cause) { setError(workspaceError(cause)) }
  }
  return <WorkspaceDialog title={ticket ? '连接凭据' : '签发只读授权'} dirty={dirty} busy={state.posting} onClose={onClose}>
    {ticket ? <TicketActions ticket={ticket} /> : <form onSubmit={event => void issue(event)}><fieldset disabled={state.locked}>
      <label>接收卡<textarea aria-label="接收卡 JSON" required rows={7} spellCheck={false} value={raw} onChange={event => setRaw(event.target.value)} /></label>
      <div className="pw-form-row"><label>源 Agent<select aria-label="源 Agent" value={actorId} onChange={event => setActorId(event.target.value)}>{state.context?.actors.map(actor => <option key={actor.id} value={actor.id}>{actor.name} · v{actor.version}</option>)}</select></label><label>到期时间<input aria-label="授权到期时间" type="datetime-local" required value={expiry} onChange={event => setExpiry(event.target.value)} /></label></div>
      <label className="pw-checkbox"><input type="checkbox" checked={results} onChange={event => setResults(event.target.checked)} />允许读取执行结果和材料</label>
      <div className="pw-actions"><button className="pw-primary" type="submit">{state.posting ? '正在签发…' : '签发授权'}</button></div>
    </fieldset></form>}{error && <div className="pw-error" role="alert">{error}</div>}
  </WorkspaceDialog>
}
export function ConnectWorkspaceDialog({ state, onClose }: { state: WorkspaceState; onClose: () => void }) {
  const [endpoint, setEndpoint] = useState(''), [label, setLabel] = useState(''), [raw, setRaw] = useState(''), [error, setError] = useState('')
  const request = useRef<string | null>(null), close = useRef(onClose); close.current = onClose
  useEffect(() => { if (state.completedRequest?.recovered && state.completedRequest.request_id === request.current && state.completedRequest.operation === 'connect') { setRaw(''); setEndpoint(''); setLabel(''); close.current() } }, [state.completedRequest])
  const connect = async (event: React.FormEvent) => {
    event.preventDefault(); setError('')
    try {
      const ticket = validateTicket(parseWorkspaceJson(raw) as WorkspaceTicket), ctx = state.context
      if (!ctx || ticket.recipient.workspace_key !== ctx.workspace_key || ticket.recipient.owner_key !== ctx.owner_key || !ctx.actors.some(actor => actor.id === ticket.recipient.actor_id && actor.version === ticket.recipient.actor_version)) throw new Error('凭据接收方与当前工作区不一致')
      if (!label.trim() || label.trim().length > 80) throw new Error('请输入 1 至 80 字的来源名称')
      if (Date.parse(ticket.expires_at) <= Date.now()) throw new Error('连接凭据已到期')
      request.current = workspaceRequestId()
      const receipt = await state.submit('connect', { payload: { request_id: request.current, endpoint: validateWorkspaceEndpoint(endpoint), label: label.trim(), ticket } })
      if (receipt) { setRaw(''); setEndpoint(''); setLabel(''); onClose() }
    } catch (cause) { setError(workspaceError(cause)) }
  }
  return <WorkspaceDialog title="连接工作区" dirty={Boolean(raw || endpoint || label)} busy={state.posting} onClose={onClose}><form onSubmit={event => void connect(event)}><fieldset disabled={state.locked}>
    <div className="pw-form-row"><label>来源名称<input aria-label="来源名称" required maxLength={80} value={label} onChange={event => setLabel(event.target.value)} /></label><label>工作区地址<input aria-label="工作区地址" required inputMode="url" value={endpoint} onChange={event => setEndpoint(event.target.value)} /></label></div>
    <label>连接凭据<textarea aria-label="连接凭据 JSON" required rows={10} spellCheck={false} autoComplete="off" value={raw} onChange={event => setRaw(event.target.value)} /></label>
    <div className="pw-actions"><button className="pw-primary" type="submit">{state.posting ? '正在连接…' : '核对并连接'}</button></div>
  </fieldset></form>{error && <div className="pw-error" role="alert">{error}</div>}</WorkspaceDialog>
}
export function RecipientCard({ state }: { state: WorkspaceState }) {
  const [actor, setActor] = useState(state.context?.actors[0]?.id || 'main'), [card, setCard] = useState<WorkspaceRecipient | null>(null), [error, setError] = useState(''), [loading, setLoading] = useState(false), run = useRef(0)
  useEffect(() => () => { run.current++ }, [])
  const actorIdentity = state.context?.actors.map(row => `${row.id}:${row.version}`).join('|')
  useEffect(() => { run.current++; setCard(null); setLoading(false) }, [actorIdentity])
  const create = async () => { const seq = ++run.current; setLoading(true); setError(''); setCard(null); try { const value = await state.recipient(actor); if (seq === run.current) setCard(value) } catch (cause) { if (seq === run.current) setError(workspaceError(cause)) } finally { if (seq === run.current) setLoading(false) } }
  return <section className="pw-recipient"><div className="pw-form-row"><label>接收 Agent<select aria-label="接收 Agent" value={actor} onChange={event => { run.current++; setActor(event.target.value); setCard(null); setLoading(false); setError('') }}>{state.context?.actors.map(row => <option key={row.id} value={row.id}>{row.name} · v{row.version}</option>)}</select></label><button disabled={state.locked || loading} onClick={() => void create()}>生成接收卡</button></div>{card && <><textarea readOnly rows={6} aria-label="公开接收卡" value={JSON.stringify(card, null, 2)} /><button onClick={() => { void navigator.clipboard.writeText(workspaceCanonical(card)).then(() => setError('已复制接收卡'), () => setError('复制未完成，请选择接收卡正文复制')) }}><Copy size={15} />复制接收卡</button></>}{error && <p role="status">{error}</p>}</section>
}
