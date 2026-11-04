import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { ArrowDown, ChevronRight, CircleStop, ExternalLink, GitBranch, Loader2, Mail, Plus, RefreshCw, Trash2, X } from 'lucide-react'
import type {
  PersonalAgent, PersonalCollaboration, PersonalCollaborationEvent, PersonalCollaborationInput,
  PersonalCollaborationReply, PersonalCollaborationStatus, PersonalCollaborationStepInput, PersonalPeerGrant, WorkbenchRuntimeClient,
} from '@naumi/shared/api/WorkbenchRuntimeClient'
import { personalError } from './usePersonalAgents'
import { MessageContent } from '../rich/MessageContent'
import { ManualMessageComposer, PeerConversations, PeerScopeEditor, usePeerTools, validatePersonalMessage } from './PeerMessages'
import './collaboration.css'

export type PersonalCollaborationsApi = Pick<WorkbenchRuntimeClient,
  'personalCollaborations' | 'createPersonalCollaboration' | 'personalCollaboration' |
  'cancelPersonalCollaboration' | 'replyPersonalCollaboration' | 'resolvePermission' | 'personalTools' |
  'personalMessages' | 'personalMessage' | 'acknowledgePersonalMessage' | 'createPersonalMessage'
>

const activeStatuses = new Set<string>(['admitted', 'claimed', 'running', 'waiting'])
const statusLabels: Record<PersonalCollaborationStatus, string> = {
  admitted: '排队中', claimed: '准备中', running: '执行中', completed: '已完成',
  error: '执行失败', timeout: '已超时', max_turns: '达到轮次上限', cancelled: '已停止',
  unknown: '结果待核实', blocked: '已阻塞', waiting: '等待执行',
}
const eventLabels: Record<string, string> = {
  created: '创建协作', admitted: '任务入队', claimed: '领取任务', running: '开始执行',
  handoff: '交接上下文', result: '提交结果', completed: '完成任务', error: '执行失败',
  cancelled: '停止执行', cancel_requested: '请求停止', blocked: '依赖阻塞',
  unknown: '结果待核实', tool_start: '调用工具', tool_end: '工具返回', tool_error: '工具失败',
  permission_requested: '等待授权', permission_resolved: '授权已处理',
  recovery_error: '恢复失败', job_admitted: '任务入队', dispatch_prepared: '准备交接',
  recovery_unknown: '中断执行待核实',
  peer_requested: '专员发出请求', peer_dispatch_started: '向专员派送', peer_received: '对端接收请求',
  peer_result_available: '专员返回结果', peer_acknowledged: '确认收件',
  question_requested: '专员提问', question_answered: '用户已回答', question_expired: '问题已过期',
}
const stateLabel = (status: string) => statusLabels[status as PersonalCollaborationStatus] || '状态待核实'
const eventLabel = (kind: string) => eventLabels[kind] || '执行记录'
const timestamp = (value: string) => {
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? '时间待核实' : date.toLocaleString('zh-CN', { hour12: false })
}

function validateCollaboration(value: PersonalCollaboration, expectedId?: string) {
  if (!value || typeof value.id !== 'string' || !Array.isArray(value.steps)
    || typeof value.status !== 'string' || (expectedId && value.id !== expectedId)
    || typeof value.cancel_requested !== 'boolean' || typeof value.created_at !== 'string'
    || typeof value.context !== 'string' || typeof value.title !== 'string'
    || value.steps.some(step => !step || typeof step.id !== 'string' || typeof step.agent_id !== 'string'
      || !Number.isInteger(step.agent_version) || step.agent_version < 1 || typeof step.task !== 'string'
      || typeof step.status !== 'string' || typeof step.job_id !== 'string' || typeof step.session_id !== 'string'
      || typeof step.response !== 'string' || typeof step.error !== 'string' || typeof step.run_id !== 'string'
      || !Array.isArray(step.depends_on) || step.depends_on.some(id => typeof id !== 'string'))
    || (value.events !== undefined && (!Array.isArray(value.events) || value.events.some(event => !event
      || !Number.isInteger(event.sequence) || typeof event.key !== 'string' || typeof event.kind !== 'string'
      || typeof event.step_id !== 'string' || typeof event.created_at !== 'string'
      || !event.data || typeof event.data !== 'object' || Array.isArray(event.data)))))
    throw new Error('协作记录返回不完整，请刷新后重试')
  if (value.messages !== undefined && (!Array.isArray(value.messages)
    || value.messages.some(message => validatePersonalMessage(message).collaboration_id !== value.id)))
    throw new Error('专员消息与当前协作不一致，请重新加载')
  if (value.peer_sessions !== undefined && (!Array.isArray(value.peer_sessions) || value.peer_sessions.some(session => !session
    || typeof session.message_id !== 'string' || typeof session.session_id !== 'string'
    || typeof session.agent_id !== 'string' || !Number.isInteger(session.agent_version) || session.agent_version < 1
    || typeof session.step_id !== 'string' || !session.step_id.trim()
    || typeof session.state !== 'string'))) throw new Error('专员执行归属返回不完整，请重新加载')
  return value
}

export function matchesCollaborationExecution(value: PersonalCollaboration, event: PersonalCollaborationEvent, running = false) {
  const root = value.steps.find(step => step.id === event.step_id)
  if (!root || (running && root.status !== 'running')) return false
  if (root.session_id === event.data.session_id && !event.data.peer_message_id) return true
  if (typeof event.data.peer_message_id !== 'string') return false
  return Boolean(value.peer_sessions?.some(session => session.message_id === event.data.peer_message_id
    && session.session_id === event.data.session_id
    && session.step_id === event.step_id
    && !value.messages?.some(message => message.id === session.message_id && message.step_id !== event.step_id)
    && (!running || ['processing', 'dispatching'].includes(session.state))))
}

function Status({ status, stopping = false }: { status: string; stopping?: boolean }) {
  return <span className={`pc-status pc-status-${status}`} role="status">
    {activeStatuses.has(status) && <span className="pc-status-dot" />}
    {stopping && activeStatuses.has(status) ? '停止中' : stateLabel(status)}
  </span>
}

export function PersonalCollaborations({ api, agents, sourceSessionId, busy, onClose, onOpenSession }: {
  api: PersonalCollaborationsApi
  agents: PersonalAgent[]
  sourceSessionId: string | null
  busy: boolean
  onClose: () => void
  onOpenSession?: (sessionId: string) => Promise<unknown>
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const headingId = useId()
  const alive = useRef(true)
  const cancelInFlight = useRef(false)
  const [items, setItems] = useState<PersonalCollaboration[]>([])
  const [loading, setLoading] = useState(true)
  const [listError, setListError] = useState('')
  const [detailError, setDetailError] = useState('')
  const [actionError, setActionError] = useState('')
  const [selectedId, setSelectedId] = useState('')
  const [detail, setDetail] = useState<PersonalCollaboration | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [composing, setComposing] = useState(false)
  const [composerKey, setComposerKey] = useState(0)
  const [messageComposerKey, setMessageComposerKey] = useState(0)
  const [composeMode, setComposeMode] = useState<'workflow' | 'message'>('workflow')
  const [revision, setRevision] = useState(0)
  const [submitting, setSubmitting] = useState(false)
  const [cancelConfirm, setCancelConfirm] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [opening, setOpening] = useState(false)
  const permissionInFlight = useRef(new Set<string>())
  const [permissionSent, setPermissionSent] = useState<Record<string, boolean>>({})
  const [permissionBusy, setPermissionBusy] = useState<Record<string, boolean>>({})
  const questionInFlight = useRef(new Set<string>())
  const [questionSent, setQuestionSent] = useState<Record<string, boolean>>({})
  const [questionBusy, setQuestionBusy] = useState<Record<string, boolean>>({})
  const [customAnswers, setCustomAnswers] = useState<Record<string, string>>({})

  useEffect(() => {
    alive.current = true
    const element = dialog.current
    if (element && !element.open) {
      if (element.showModal) element.showModal()
      else element.setAttribute('open', '')
    }
    return () => { alive.current = false; if (element?.open) element.close() }
  }, [])

  useEffect(() => {
    let stopped = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const refresh = async () => {
      setLoading(true)
      try {
        const result = await api.personalCollaborations()
        if (!Array.isArray(result.collaborations)) throw new Error('协作列表返回不完整，请重试')
        const values = result.collaborations.map(value => validateCollaboration(value))
        if (stopped) return
        setItems(values); setListError('')
        setSelectedId(value => value || values[0]?.id || '')
        if (values.some(value => activeStatuses.has(value.status))) timer = setTimeout(() => void refresh(), 2000)
      } catch (cause) {
        if (!stopped) setListError(personalError(cause))
      } finally { if (!stopped) setLoading(false) }
    }
    void refresh()
    return () => { stopped = true; clearTimeout(timer) }
  }, [api, revision])

  useEffect(() => {
    let stopped = false
    let timer: ReturnType<typeof setTimeout> | undefined
    setCancelConfirm(false); setActionError(''); setDetailError('')
    if (!selectedId || composing) { setDetail(null); setDetailLoading(false); return }
    setDetail(null)
    const refresh = async () => {
      setDetailLoading(true)
      try {
        const value = validateCollaboration(await api.personalCollaboration(selectedId), selectedId)
        if (stopped) return
        setDetail(value); setDetailError('')
        if (activeStatuses.has(value.status)) timer = setTimeout(() => void refresh(), 1600)
      } catch (cause) {
        if (!stopped) setDetailError(personalError(cause))
      } finally { if (!stopped) setDetailLoading(false) }
    }
    void refresh()
    return () => { stopped = true; clearTimeout(timer) }
  }, [api, selectedId, composing, revision])

  const requestClose = () => { if (!submitting && !cancelling && !opening) onClose() }
  const selected = detail?.id === selectedId ? detail : null
  const locked = submitting || cancelling || opening
  const refresh = () => setRevision(value => value + 1)
  const acceptCreated = (input: PersonalCollaboration, origin: 'workflow' | 'message') => {
    const value = validateCollaboration(input)
    setItems(items => [value, ...items.filter(item => item.id !== value.id)])
    setComposing(false)
    if (origin === 'workflow') setComposerKey(value => value + 1)
    else setMessageComposerKey(value => value + 1)
    setSelectedId(value.id); setDetail(value); refresh()
  }
  const select = (id: string) => { if (!locked) { setComposing(false); setSelectedId(id) } }

  const cancel = async () => {
    if (!selected || cancelInFlight.current || !cancelConfirm) return
    cancelInFlight.current = true; setCancelling(true); setActionError('')
    try {
      const value = validateCollaboration(await api.cancelPersonalCollaboration(selected.id), selected.id)
      if (alive.current) { setDetail(value); setCancelConfirm(false); refresh() }
    } catch (cause) { if (alive.current) setActionError(personalError(cause)) }
    finally { if (alive.current) setCancelling(false); cancelInFlight.current = false }
  }
  const openSession = async (sessionId: string) => {
    if (!onOpenSession || locked || busy) return
    setOpening(true); setActionError('')
    try { await onOpenSession(sessionId); if (alive.current) onClose() }
    catch (cause) { if (alive.current) setActionError(personalError(cause)) }
    finally { if (alive.current) setOpening(false) }
  }
  const resolve = async (event: PersonalCollaborationEvent, decision: 'allow' | 'deny') => {
    const sessionId = event.data.session_id, callId = event.data.call_id
    if (typeof sessionId !== 'string' || typeof callId !== 'string') return
    const key = `${sessionId}:${callId}`
    if (permissionInFlight.current.has(key) || permissionSent[key]) return
    permissionInFlight.current.add(key)
    setPermissionBusy(value => ({ ...value, [key]: true })); setActionError('')
    try {
      await api.resolvePermission(sessionId, callId, { decision })
      if (alive.current) { setPermissionSent(value => ({ ...value, [key]: true })); refresh() }
    } catch (cause) { if (alive.current) setActionError(personalError(cause)) }
    finally {
      permissionInFlight.current.delete(key)
      if (alive.current) setPermissionBusy(value => ({ ...value, [key]: false }))
    }
  }
  const resolved = new Set((selected?.events || []).filter(event => event.kind === 'permission_resolved')
    .map(event => `${event.step_id}:${String(event.data.call_id || '')}`))
  const permissions = (selected && activeStatuses.has(selected.status) && !selected.cancel_requested ? selected.events || [] : []).filter(event => event.kind === 'permission_requested'
    && typeof event.data.call_id === 'string' && typeof event.data.session_id === 'string'
    && !resolved.has(`${event.step_id}:${event.data.call_id}`)
    && selected && matchesCollaborationExecution(selected, event))
  const answered = new Set((selected?.events || []).filter(event => ['question_answered', 'question_expired'].includes(event.kind))
    .map(event => String(event.data.request_id || '')))
  const questions = (selected && activeStatuses.has(selected.status) && !selected.cancel_requested ? selected.events || [] : [])
    .filter(event => event.kind === 'question_requested' && typeof event.data.request_id === 'string'
      && !answered.has(event.data.request_id) && selected && matchesCollaborationExecution(selected, event, true))
  const reply = async (event: PersonalCollaborationEvent, body: PersonalCollaborationReply) => {
    if (!selected || typeof event.data.request_id !== 'string') return
    const requestId = event.data.request_id, key = `${selected.id}:${requestId}`
    if (questionInFlight.current.has(key) || questionSent[key]) return
    questionInFlight.current.add(key); setQuestionBusy(value => ({ ...value, [key]: true })); setActionError('')
    try {
      const result = await api.replyPersonalCollaboration(selected.id, requestId, body)
      if (result.status !== 'answered' || result.request_id !== requestId)
        throw new Error('回答提交状态未确认，请刷新协作记录后核对')
      if (alive.current) { setQuestionSent(value => ({ ...value, [key]: true })); refresh() }
    } catch (cause) { if (alive.current) setActionError(personalError(cause)) }
    finally {
      questionInFlight.current.delete(key)
      if (alive.current) setQuestionBusy(value => ({ ...value, [key]: false }))
    }
  }

  return <dialog ref={dialog} className="pc-dialog" aria-labelledby={headingId}
    onCancel={event => { event.preventDefault(); requestClose() }}>
    <header className="pc-heading"><div><GitBranch size={20} /><h2 id={headingId}>协作</h2></div>
      <div><button type="button" className="pc-icon" aria-label="刷新协作记录" disabled={locked || loading} onClick={refresh}><RefreshCw size={16} className={loading ? 'pa-spin' : ''} /></button>
        <button type="button" className="pc-icon" aria-label="关闭协作" disabled={locked} onClick={requestClose}><X size={19} /></button></div>
    </header>
    <div className="pc-workbench">
      <aside className="pc-sidebar" aria-label="协作任务列表">
        <button type="button" className={`pc-new${composing && composeMode === 'workflow' ? ' selected' : ''}`} disabled={locked} onClick={() => { setComposeMode('workflow'); setComposing(true) }}><Plus size={16} />发起协作</button>
        <button type="button" className={`pc-new${composing && composeMode === 'message' ? ' selected' : ''}`} disabled={locked} onClick={() => { setComposeMode('message'); setComposing(true) }}><Mail size={16} />专员消息</button>
        {listError && <div className="pc-error" role="alert">{listError}<button type="button" onClick={refresh}>重试加载列表</button></div>}
        {loading && !items.length && <div className="pc-empty" role="status">正在加载协作…</div>}
        {!loading && !listError && !items.length && <div className="pc-empty">尚无协作任务</div>}
        <div className="pc-list">{items.map(item => <button type="button" key={item.id} disabled={locked}
          className={`pc-list-item${selectedId === item.id && !composing ? ' selected' : ''}`}
          aria-current={selectedId === item.id && !composing ? 'true' : undefined} onClick={() => select(item.id)}>
          <strong>{item.title || '协作任务'}</strong><span><Status status={item.status} stopping={item.cancel_requested} /><time>{new Date(item.created_at).toLocaleDateString('zh-CN')}</time></span>
        </button>)}</div>
      </aside>
      <main className="pc-content">
        <div hidden={!composing || composeMode !== 'workflow'}><CollaborationComposer key={composerKey} api={api} agents={agents} sourceSessionId={sourceSessionId}
          onBusy={setSubmitting} onCreated={value => acceptCreated(value, 'workflow')} /></div>
        <div hidden={!composing || composeMode !== 'message'}><ManualMessageComposer key={messageComposerKey} api={api} agents={agents} sourceSessionId={sourceSessionId}
          onBusy={setSubmitting} onCreated={value => acceptCreated(value, 'message')} /></div>
        {!composing && <>
          {detailError && <div className="pc-error" role="alert">{detailError}<button type="button" onClick={refresh}>重试加载任务</button></div>}
          {actionError && <div className="pc-error" role="alert">{actionError}</div>}
          {detailLoading && !selected && <div className="pc-empty" role="status">正在加载任务…</div>}
          {!selected && !detailLoading && !detailError && <div className="pc-blank"><GitBranch size={32} /><span>{items.length ? '选择协作任务' : '还没有协作记录'}</span></div>}
          {selected && <>
            <header className="pc-detail-heading"><div><h3>{selected.title || '协作任务'}</h3><time>{timestamp(selected.created_at)}</time></div><Status status={selected.status} stopping={selected.cancel_requested} /></header>
            {selected.context && <details className="pc-context"><summary>共享上下文</summary><pre>{selected.context}</pre></details>}
            {!!questions.length && <section className="pc-questions" aria-label="协作待回答问题">{questions.map(event => {
              const requestId = String(event.data.request_id), key = `${selected.id}:${requestId}`
              const options = Array.isArray(event.data.options) ? event.data.options.filter((option): option is { value: string; label: string; description?: string } => Boolean(option) && typeof option === 'object' && typeof option.value === 'string' && typeof option.label === 'string') : []
              const expired = typeof event.data.expires_at === 'string' && new Date(event.data.expires_at).valueOf() <= Date.now()
              return <div className="pc-question" key={event.key}>
                <header><strong>{typeof event.data.header === 'string' && event.data.header || '专员提问'}</strong><span>{typeof event.data.actor_name === 'string' && event.data.actor_name || selected.steps.find(step => step.id === event.step_id)?.agent_name || agents.find(agent => agent.id === selected.steps.find(step => step.id === event.step_id)?.agent_id)?.name || '专员'}</span></header>
                <p>{typeof event.data.question === 'string' ? event.data.question : '问题内容暂不可用，请刷新记录'}</p>
                {questionSent[key] ? <span role="status">回答已提交</span> : expired ? <span role="status">问题已过期</span> : <>
                  {!!options.length && <div className="pc-question-options">{options.map(option => <button type="button" key={option.value} disabled={questionBusy[key]} onClick={() => void reply(event, { kind: 'option', value: option.value })}>
                    <strong>{option.label}</strong>{typeof option.description === 'string' && option.description && <span>{option.description}</span>}
                  </button>)}</div>}
                  {event.data.allow_custom === true && <form className="pc-question-custom" onSubmit={submit => {
                    submit.preventDefault()
                    const text = customAnswers[key]?.trim()
                    if (text) void reply(event, { kind: 'custom', custom_text: text })
                  }}><label>{typeof event.data.custom_label === 'string' && event.data.custom_label || '自定义回答'}<textarea aria-label={`问题 ${requestId} 自定义回答`} maxLength={4000} rows={3} disabled={questionBusy[key]} value={customAnswers[key] || ''} onChange={change => setCustomAnswers(value => ({ ...value, [key]: change.target.value }))} /></label>
                    <button type="submit" className="pc-primary" disabled={questionBusy[key] || !customAnswers[key]?.trim()}>{questionBusy[key] ? '正在提交…' : '提交回答'}</button></form>}
                </>}
              </div>
            })}</section>}
            {!!permissions.length && <section className="pc-permissions" aria-label="协作待授权操作">{permissions.map(event => {
              const key = `${String(event.data.session_id)}:${String(event.data.call_id)}`
              return <div className="pc-permission" key={event.key}><header><strong>{typeof event.data.actor_name === 'string' && event.data.actor_name || selected.steps.find(step => step.id === event.step_id)?.agent_name || agents.find(agent => agent.id === selected.steps.find(step => step.id === event.step_id)?.agent_id)?.name || '专员'} · 等待授权</strong><code>{String(event.data.tool_name || '工具操作')}</code></header>
                {typeof event.data.reason === 'string' && <p>{event.data.reason}</p>}
                {event.data.arguments !== undefined && <pre>{typeof event.data.arguments === 'string' ? event.data.arguments : JSON.stringify(event.data.arguments, null, 2)}</pre>}
                {permissionSent[key] ? <span role="status">授权决定已提交</span> : <div><button type="button" disabled={permissionBusy[key]} onClick={() => void resolve(event, 'deny')}>拒绝</button><button type="button" className="pc-primary" disabled={permissionBusy[key]} onClick={() => void resolve(event, 'allow')}>{permissionBusy[key] ? '正在提交…' : '允许本次操作'}</button></div>}
              </div>
            })}</section>}
            <ol className="pc-steps" aria-label="协作执行步骤">{selected.steps.map((step, index) => <li className="pc-step" key={step.id}>
              <header><span className="pc-step-number">{index + 1}</span><strong>{step.agent_name || agents.find(agent => agent.id === step.agent_id)?.name || step.agent_id}</strong><span className="pc-version">v{step.agent_version}</span><Status status={step.status} /></header>
              {!!step.depends_on.length && <div className="pc-dependencies"><GitBranch size={12} />{step.depends_on.map(id => {
                const previous = selected.steps.find(value => value.id === id)
                return previous ? previous.agent_name || agents.find(agent => agent.id === previous.agent_id)?.name || previous.id : id
              }).join('、')}</div>}
              <p className="pc-task">{step.task}</p>
              {step.error && <div className="pc-error" role="alert">{step.error}</div>}
              {step.response && <div className="pc-response" aria-label={`步骤 ${index + 1} 的结果`}><MessageContent content={step.response} /></div>}
              <footer><code>{step.job_id}</code>{step.run_id && <code title="执行记录">{step.run_id}</code>}
                {onOpenSession && step.session_id && <button type="button" disabled={busy || locked || ['waiting', 'admitted', 'claimed'].includes(step.status)} onClick={() => void openSession(step.session_id)}><ExternalLink size={13} />查看执行记录</button>}</footer>
            </li>)}</ol>
            <PeerConversations api={api} collaboration={selected} busy={busy || locked} onOpenSession={onOpenSession ? openSession : undefined} />
            <details className="pc-transcript" open><summary>交接与执行记录 <span>{selected.events?.length || 0}</span></summary>
              {selected.events?.length ? <ol>{selected.events.map(event => <li key={event.sequence}><span className="pc-event-dot" /><div><header><strong>{eventLabel(event.kind)}</strong><time>{timestamp(event.created_at)}</time></header>
                {event.step_id && <span className="pc-event-agent">{typeof event.data.actor_name === 'string' && event.data.actor_name || selected.steps.find(step => step.id === event.step_id)?.agent_name || agents.find(agent => agent.id === selected.steps.find(step => step.id === event.step_id)?.agent_id)?.name || event.step_id}</span>}
                <EventData event={event} />
              </div></li>)}</ol> : <div className="pc-empty">尚无执行记录</div>}
            </details>
            {activeStatuses.has(selected.status) && !selected.cancel_requested && <div className="pc-stop">
              {cancelConfirm ? <div className="pc-stop-confirm" role="group" aria-label="确认停止协作"><strong>停止这次协作？</strong><div><button type="button" disabled={cancelling} onClick={() => setCancelConfirm(false)}>继续执行</button><button type="button" className="pc-danger" disabled={cancelling} onClick={() => void cancel()}>{cancelling ? '正在停止…' : '确认停止'}</button></div></div>
                : <button type="button" className="pc-danger" disabled={locked} onClick={() => setCancelConfirm(true)}><CircleStop size={14} />停止协作</button>}
            </div>}
          </>}
        </>}
      </main>
    </div>
  </dialog>
}

function EventData({ event }: { event: PersonalCollaborationEvent }) {
  const text = [event.data.summary, event.data.message, event.data.tool_name, event.data.name]
    .find(value => typeof value === 'string' && value)
  return <>{typeof text === 'string' && <p>{text}</p>}
    {!!Object.keys(event.data).length && <details><summary>查看原始记录</summary><pre>{JSON.stringify(event.data, null, 2)}</pre></details>}</>
}

export function CollaborationComposer({ api, agents, sourceSessionId, onBusy, onCreated }: {
  api: Pick<PersonalCollaborationsApi, 'createPersonalCollaboration' | 'personalTools'>
  agents: PersonalAgent[]
  sourceSessionId: string | null
  onBusy: (value: boolean) => void
  onCreated: (value: PersonalCollaboration) => void
}) {
  const [title, setTitle] = useState('')
  const [context, setContext] = useState('')
  const [mode, setMode] = useState<'sequential' | 'parallel'>('sequential')
  const [rows, setRows] = useState<PersonalCollaborationStepInput[]>(() => [newStep()])
  const [peerAgents, setPeerAgents] = useState<PersonalPeerGrant[]>([])
  const permissions = usePeerTools(api)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const requestId = useRef('')
  const requestSource = useRef<string | null>(null)
  const submitted = useRef<PersonalCollaborationInput | null>(null)
  const inFlight = useRef(false)
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const available = agents.filter(agent => agent.kind === 'specialist' && agent.status === 'active')
  const canSubmit = rows.length > 0 && rows.every(row => row.agent_id && row.agent_version > 0 && row.task.trim()
    && available.some(agent => agent.id === row.agent_id))
    && peerAgents.every(grant => available.some(agent => agent.id === grant.agent_id && agent.version === grant.agent_version))
    && (!peerAgents.length || (!permissions.loading && !permissions.error))
  const update = (id: string, fields: Partial<PersonalCollaborationStepInput>) => setRows(values => values.map(row => row.id === id ? { ...row, ...fields } : row))
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (inFlight.current || (!submitted.current && !canSubmit)) return
    if (!sourceSessionId && !requestSource.current) { setError('请先打开一个已有对话，再发起协作'); return }
    if (!requestId.current) { requestId.current = crypto.randomUUID(); requestSource.current = sourceSessionId }
    inFlight.current = true; setSaving(true); onBusy(true); setError('')
    const body: PersonalCollaborationInput = submitted.current || {
      source_session_id: requestSource.current!, request_id: requestId.current,
      title: title.trim(), context: context.trim(), ...(peerAgents.length ? { peer_agents: peerAgents } : {}), steps: rows.map((row, index) => ({
        ...row, task: row.task.trim(), depends_on: mode === 'sequential' && index > 0 ? [rows[index - 1].id] : [],
      })),
    }
    submitted.current = body
    try {
      const result = validateCollaboration(await api.createPersonalCollaboration(body))
      if (result.request_id !== body.request_id || result.source_session_id !== body.source_session_id)
        throw new Error('返回的协作与本次请求不一致，请刷新记录后核对')
      if (alive.current) onCreated(result)
    } catch (cause) { if (alive.current) setError(personalError(cause)) }
    finally { if (alive.current) setSaving(false); inFlight.current = false; onBusy(false) }
  }
  return <form className="pc-composer" onSubmit={submit}>
    <header><h3>发起协作</h3><span>{rows.length} 个步骤</span></header>
    {error && <div className="pc-error" role="alert">{error}</div>}
    <fieldset disabled={saving || Boolean(submitted.current)} className="pc-compose-fields">
      <label>任务名称<input aria-label="协作任务名称" maxLength={120} value={title} onChange={event => setTitle(event.target.value)} /></label>
      <fieldset className="pc-mode"><legend>执行方式</legend>
        <label><input type="radio" name="collaboration-mode" checked={mode === 'sequential'} onChange={() => setMode('sequential')} /><ArrowDown size={14} />依次接力</label>
        <label><input type="radio" name="collaboration-mode" checked={mode === 'parallel'} onChange={() => setMode('parallel')} /><GitBranch size={14} />并行执行</label>
      </fieldset>
      <ol className="pc-compose-steps">{rows.map((row, index) => <li key={row.id}>
        {index > 0 && mode === 'sequential' && <div className="pc-link"><ArrowDown size={14} /></div>}
        <div className="pc-compose-step"><header><span className="pc-step-number">{index + 1}</span>
          <select aria-label={`步骤 ${index + 1} 专员`} required value={row.agent_id} onChange={event => {
            const profile = available.find(agent => agent.id === event.target.value)
            update(row.id, { agent_id: profile?.id || '', agent_version: profile?.version || 0 })
          }}><option value="">选择专员</option>{available.map(agent => <option key={agent.id} value={agent.id}>{agent.name}</option>)}
            {row.agent_id && !available.some(agent => agent.id === row.agent_id) && <option value={row.agent_id}>所选专员已停用</option>}</select>
          {row.agent_version > 0 && <span className="pc-version">v{row.agent_version}</span>}
          <button type="button" className="pc-icon" aria-label={`删除步骤 ${index + 1}`} disabled={rows.length === 1} onClick={() => setRows(values => values.filter(value => value.id !== row.id))}><Trash2 size={14} /></button></header>
          <label>任务<textarea aria-label={`步骤 ${index + 1} 任务`} required maxLength={16000} rows={3} value={row.task} onChange={event => update(row.id, { task: event.target.value })} /></label>
        </div>
      </li>)}</ol>
      {!available.length && <div className="pc-empty">尚无可协作的专员</div>}
      <button type="button" className="pc-add-step" disabled={rows.length >= 12} onClick={() => setRows(values => [...values, newStep()])}><Plus size={14} />添加步骤</button>
      {permissions.error && <div className="pc-error" role="alert">{permissions.error}<button type="button" onClick={permissions.retry}>重试加载互询权限</button></div>}
      <PeerScopeEditor agents={agents} tools={permissions.tools} value={peerAgents} onChange={setPeerAgents} disabled={saving || permissions.loading || Boolean(permissions.error)} />
      <label>共享上下文<textarea aria-label="协作共享上下文" maxLength={32000} rows={4} value={context} onChange={event => setContext(event.target.value)} /></label>
    </fieldset>
    <footer><button type="submit" className="pc-primary" disabled={saving || (!submitted.current && !canSubmit)}>{saving ? <Loader2 size={15} className="pa-spin" /> : <ChevronRight size={15} />}{saving ? '正在提交…' : '开始协作'}</button></footer>
  </form>
}

function newStep(): PersonalCollaborationStepInput {
  return { id: `step_${crypto.randomUUID().replaceAll('-', '').slice(0, 16)}`, agent_id: '', agent_version: 0, task: '', depends_on: [] }
}
