import { useEffect, useRef, useState, type FormEvent } from 'react'
import { ArrowRight, Check, ChevronDown, ExternalLink, Loader2, Mail, RefreshCw, Send } from 'lucide-react'
import type {
  PersonalAgent, PersonalAgentMessage, PersonalCollaboration, PersonalMessageInput, PersonalMessageState,
  PersonalPeerGrant, PersonalTool, WorkbenchRuntimeClient,
} from '@naumi/shared/api/WorkbenchRuntimeClient'
import { MessageContent } from '../rich/MessageContent'
import { personalError } from './usePersonalAgents'
import './peer-messages.css'

export type PersonalMessagesApi = Pick<WorkbenchRuntimeClient,
  'personalMessages' | 'personalMessage' | 'acknowledgePersonalMessage' | 'createPersonalMessage' | 'personalTools'
>
const messageLabels: Record<PersonalMessageState, string> = {
  queued: '等待派送', dispatching: '派送中', processing: '对端处理中', responded: '结果已返回',
  acknowledged: '已确认', blocked: '派送受阻', unknown: '结果待核实', cancelled: '已停止',
  error: '执行失败', timeout: '已超时', max_turns: '达到轮次上限',
}
const activeMessageStates = new Set(['queued', 'dispatching', 'processing'])
const receiptLabels: Record<string, string> = {
  requested: '发送请求', peer_requested: '发送请求', dispatch_started: '开始派送',
  peer_dispatch_started: '开始派送', received: '对端已接收', peer_received: '对端已接收',
  result: '结果已保存', result_available: '结果已保存', peer_result_available: '结果已保存',
  acknowledged: '确认收件', peer_acknowledged: '确认收件',
}
const stamp = (value: string) => {
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? '时间待核实' : date.toLocaleString('zh-CN', { hour12: false })
}

export function validatePersonalMessage(value: PersonalAgentMessage, expectedId?: string, full = false): PersonalAgentMessage {
  const fields: (keyof PersonalAgentMessage)[] = ['id', 'collaboration_id', 'step_id', 'source_session_id',
    'source_run_id', 'sender_agent_id', 'sender_name', 'recipient_agent_id', 'recipient_name',
    'receiver_session_id', 'receiver_run_id', 'root_job_id', 'content', 'context', 'response', 'error',
    'outcome', 'state', 'created_at', 'acknowledged_at', 'response_sha256', 'result_sha256']
  const identities: (keyof PersonalAgentMessage)[] = ['id', 'collaboration_id', 'step_id', 'source_session_id',
    'sender_agent_id', 'recipient_agent_id', 'receiver_session_id', 'created_at']
  if (!value || fields.some(field => typeof value[field] !== 'string')
    || identities.some(field => !(value[field] as string).trim())
    || (expectedId && value.id !== expectedId) || !Object.hasOwn(messageLabels, value.state)
    || !Number.isInteger(value.sender_version) || value.sender_version < 1
    || !Number.isInteger(value.recipient_version) || value.recipient_version < 1
    || [value.response_sha256, value.result_sha256].some(digest => digest && !/^[a-f0-9]{64}$/.test(digest))
    || (value.acknowledged_by !== undefined && !['', 'agent', 'user'].includes(value.acknowledged_by))
    || (value.state === 'acknowledged' && (!value.acknowledged_at || !value.response_sha256 || value.outcome !== 'completed'))
    || (full && (value.content_truncated || value.context_truncated || value.response_truncated))
    || (value.receipts !== undefined && (!Array.isArray(value.receipts) || value.receipts.some((receipt, index) => !receipt
      || !Number.isInteger(receipt.sequence) || receipt.sequence !== index + 1 || typeof receipt.kind !== 'string'
      || typeof receipt.created_at !== 'string' || typeof receipt.receipt_sha256 !== 'string'
      || !/^[a-f0-9]{64}$/.test(receipt.receipt_sha256)
      || !receipt.data || typeof receipt.data !== 'object' || Array.isArray(receipt.data)))))
    throw new Error(full ? '完整消息返回不完整，请重新加载对话' : 'Agent 消息返回不完整，请刷新后重试')
  return value
}

function retainMessageFacts(previous: PersonalAgentMessage | undefined, next: PersonalAgentMessage, full: boolean): PersonalAgentMessage {
  if (!previous) return next
  const identities: (keyof PersonalAgentMessage)[] = ['id', 'collaboration_id', 'step_id', 'source_session_id',
    'sender_agent_id', 'sender_version', 'sender_name', 'recipient_agent_id', 'recipient_version', 'recipient_name',
    'receiver_session_id', 'created_at', ...(full ? ['content', 'context'] as const : [])]
  const bindings = ['source_run_id', 'receiver_run_id', 'root_job_id', 'response_sha256', 'result_sha256'] as const
  if (identities.some(field => previous[field] !== next[field])
    || bindings.some(field => previous[field] && next[field] && previous[field] !== next[field])
    || (full && previous.response_sha256 && next.response_sha256 && (previous.response !== next.response
      || previous.outcome !== next.outcome || previous.error !== next.error))
    || (previous.acknowledged_at && next.acknowledged_at && (previous.acknowledged_at !== next.acknowledged_at
      || (previous.acknowledged_by && next.acknowledged_by && previous.acknowledged_by !== next.acknowledged_by)))
    || next.receipts?.some(receipt => previous.receipts?.some(known => known.sequence === receipt.sequence
      && known.receipt_sha256 !== receipt.receipt_sha256)))
    throw new Error('消息身份或结果摘要发生冲突，请核对原执行记录')
  if (bindings.some(field => previous[field] && !next[field])) return previous
  return { ...next, ...(previous.acknowledged_at ? {
    state: 'acknowledged', acknowledged_at: previous.acknowledged_at, acknowledged_by: previous.acknowledged_by,
  } : {}), ...((previous.receipts?.length || 0) > (next.receipts?.length || 0) ? { receipts: previous.receipts } : {}) }
}

export function defaultPeerTools(agent: PersonalAgent, tools: PersonalTool[]): string[] {
  return agent.tool_names.filter(name => tools.some(tool => tool.name === name
    && (tool.read_only === true || name === 'personal_message'))).sort()
}

export function usePeerTools(api: Pick<PersonalMessagesApi, 'personalTools'>) {
  const [tools, setTools] = useState<PersonalTool[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    let stopped = false
    setLoading(true); setError('')
    void api.personalTools().then(result => {
      if (!Array.isArray(result.tools) || result.tools.some(tool => !tool || typeof tool.name !== 'string'
        || typeof tool.description !== 'string' || (tool.read_only !== undefined && typeof tool.read_only !== 'boolean')))
        throw new Error('工具权限返回不完整，请重试')
      if (!stopped) setTools(result.tools)
    }).catch(cause => { if (!stopped) setError(personalError(cause)) })
      .finally(() => { if (!stopped) setLoading(false) })
    return () => { stopped = true }
  }, [api, revision])
  return { tools, loading, error, retry: () => setRevision(value => value + 1) }
}

export function PeerScopeEditor({ agents, tools, value, onChange, disabled = false, legend = '互询范围', selectionVerb = '询问' }: {
  agents: PersonalAgent[]; tools: PersonalTool[]; value: PersonalPeerGrant[]
  onChange: (grants: PersonalPeerGrant[]) => void; disabled?: boolean
  legend?: string; selectionVerb?: '询问' | '分配'
}) {
  const available = agents.filter(agent => agent.kind === 'specialist' && agent.status === 'active')
  const candidates = [...available, ...agents.filter(agent => value.some(grant => grant.agent_id === agent.id)
    && !available.some(candidate => candidate.id === agent.id))]
  const setGrant = (agent: PersonalAgent, checked: boolean) => onChange(checked
    ? [...value, { agent_id: agent.id, agent_version: agent.version, tool_names: defaultPeerTools(agent, tools) }]
    : value.filter(grant => grant.agent_id !== agent.id))
  return <fieldset className="pm-scope" disabled={disabled}>
    <legend>{legend}</legend>
    {!candidates.length && <span className="pm-empty">尚无可选择的 Agent</span>}
    {candidates.map(agent => {
      const grant = value.find(candidate => candidate.agent_id === agent.id)
      const selectedTools = grant?.tool_names || []
      const stale = grant && grant.agent_version !== agent.version
      return <div className={`pm-grant${grant ? ' selected' : ''}`} key={agent.id}>
        <label className="pm-grant-role"><input type="checkbox" aria-label={selectionVerb === '分配' ? `允许分配给${agent.name}` : `允许向${agent.name}询问`}
          checked={Boolean(grant)} disabled={!grant && (agent.status !== 'active' || value.length >= 128)}
          onChange={event => setGrant(agent, event.target.checked)} /><strong>{agent.name}</strong>
          <code>v{grant?.agent_version || agent.version}</code>{agent.status !== 'active' && <span>已停用</span>}</label>
        {grant && <>
          {stale && <div className="pm-stale" role="alert">配置已更新
            <button type="button" onClick={() => onChange(value.map(candidate => candidate.agent_id === agent.id
              ? { ...candidate, agent_version: agent.version, tool_names: selectedTools.filter(name => agent.tool_names.includes(name) && tools.some(tool => tool.name === name)) } : candidate))}>更新授权版本</button></div>}
          <details className="pm-grant-tools"><summary>工具范围 · {selectedTools.length ? `${selectedTools.length} 项` : '仅对话'}<ChevronDown size={12} /></summary>
            <div>{agent.tool_names.map(name => {
              const tool = tools.find(tool => tool.name === name)
              return <label key={name}><input type="checkbox" aria-label={`${agent.name}的${name}权限`}
                disabled={!tool || agent.status !== 'active'} checked={selectedTools.includes(name)}
                onChange={event => onChange(value.map(candidate => candidate.agent_id === agent.id
                  ? { ...candidate, tool_names: (event.target.checked ? [...selectedTools, name] : selectedTools.filter(value => value !== name)).sort() } : candidate))} />
                <span>{tool?.description || name}{!tool && ' · 已失效'}</span><code>{name}</code></label>
            })}{!agent.tool_names.length && <span className="pm-empty">仅对话</span>}</div>
          </details>
        </>}
      </div>
    })}
    {value.filter(grant => !candidates.some(agent => agent.id === grant.agent_id)).map(grant => <div key={grant.agent_id} className="pm-stale" role="alert">
      <span>Agent 资料暂不可用 · {grant.agent_id}</span><button type="button" onClick={() => onChange(value.filter(candidate => candidate.agent_id !== grant.agent_id))}>移除授权</button>
    </div>)}
  </fieldset>
}

export function ManualMessageComposer({ api, agents, sourceSessionId, onBusy, onCreated }: {
  api: Pick<PersonalMessagesApi, 'createPersonalMessage' | 'personalTools'>
  agents: PersonalAgent[]; sourceSessionId: string | null
  onBusy: (busy: boolean) => void; onCreated: (value: PersonalCollaboration) => void
}) {
  const [sender, setSender] = useState<{ id: string; version: number } | null>(null)
  const [recipient, setRecipient] = useState<{ id: string; version: number } | null>(null)
  const [content, setContent] = useState('')
  const [context, setContext] = useState('')
  const [grants, setGrants] = useState<PersonalPeerGrant[]>([])
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const requestId = useRef(''), requestSource = useRef<string | null>(null)
  const submitted = useRef<PersonalMessageInput | null>(null)
  const inFlight = useRef(false), alive = useRef(true)
  const permissions = usePeerTools(api)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const available = agents.filter(agent => agent.status === 'active' && agent.kind === 'specialist')
  const senders = available.filter(agent => agent.tool_names.includes('personal_message'))
  const selectedSender = available.find(agent => agent.id === sender?.id)
  const selectedRecipient = available.find(agent => agent.id === recipient?.id)
  const stale = Boolean(sender && (!selectedSender || selectedSender.version !== sender.version)
    || recipient && (!selectedRecipient || selectedRecipient.version !== recipient.version)
    || grants.some(grant => !available.some(agent => agent.id === grant.agent_id && agent.version === grant.agent_version)))
  const grant = recipient && grants.find(grant => grant.agent_id === recipient.id && grant.agent_version === recipient.version)
  const canSend = Boolean(sender && recipient && sender.id !== recipient.id && content.trim()
    && selectedSender?.tool_names.includes('personal_message') && grant && !stale && !permissions.loading && !permissions.error)
  const selectRecipient = (id: string) => {
    const agent = available.find(agent => agent.id === id)
    setRecipient(agent ? { id: agent.id, version: agent.version } : null)
    setGrants(values => {
      const filtered = values.filter(value => value.agent_id !== recipient?.id)
      return agent ? [...filtered.filter(value => value.agent_id !== agent.id), {
        agent_id: agent.id, agent_version: agent.version, tool_names: defaultPeerTools(agent, permissions.tools),
      }] : filtered
    })
  }
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (inFlight.current || (!submitted.current && !canSend) || !sender || !recipient) return
    if (!sourceSessionId && !requestSource.current) { setError('请先打开一个已有对话，再发送Agent 消息'); return }
    if (!requestId.current) { requestId.current = crypto.randomUUID(); requestSource.current = sourceSessionId }
    inFlight.current = true; setSaving(true); setError(''); onBusy(true)
    const body: PersonalMessageInput = submitted.current || {
      source_session_id: requestSource.current!, request_id: requestId.current,
      sender_agent_id: sender.id, sender_agent_version: sender.version,
      recipient_agent_id: recipient.id, recipient_agent_version: recipient.version,
      content: content.trim(), context: context.trim(), peer_agents: grants,
    }
    submitted.current = body
    try {
      const value = await api.createPersonalMessage(body)
      if (!value || typeof value.id !== 'string' || value.message_request_id !== body.request_id
        || value.source_session_id !== body.source_session_id || !Array.isArray(value.steps))
        throw new Error('返回的消息任务与本次请求不一致，请刷新记录后核对')
      if (alive.current) onCreated(value)
    } catch (cause) { if (alive.current) setError(personalError(cause)) }
    finally { inFlight.current = false; if (alive.current) setSaving(false); onBusy(false) }
  }
  return <form className="pm-composer pc-composer" onSubmit={submit}>
    <header><h3>发送Agent 消息</h3><Mail size={18} /></header>
    {error && <div className="pc-error" role="alert">{error}</div>}
    {stale && <div className="pc-error" role="alert">Agent 配置已更新，请重新选择发送方、接收方或更新授权版本</div>}
    {permissions.error && <div className="pc-error" role="alert">{permissions.error}<button type="button" onClick={permissions.retry}>重试加载工具</button></div>}
    <fieldset disabled={saving || permissions.loading || Boolean(submitted.current)} className="pc-compose-fields">
      <div className="pm-actors"><label>发送方<select aria-label="消息发送方" value={sender?.id || ''} required onChange={event => {
        const agent = senders.find(agent => agent.id === event.target.value)
        setSender(agent ? { id: agent.id, version: agent.version } : null)
      }}><option value="">选择发送方</option>{senders.map(agent => <option key={agent.id} value={agent.id}>{agent.name} · v{agent.version}</option>)}</select></label>
        <ArrowRight size={17} /><label>接收方<select aria-label="消息接收方" value={recipient?.id || ''} required onChange={event => selectRecipient(event.target.value)}>
          <option value="">选择接收方</option>{available.filter(agent => agent.id !== sender?.id).map(agent => <option key={agent.id} value={agent.id}>{agent.name} · v{agent.version}</option>)}</select></label></div>
      {!senders.length && <div className="pc-error" role="status">请先给发送方配置“Agent 互询”工具</div>}
      <label>消息<textarea aria-label="Agent 消息内容" rows={5} required maxLength={16000} value={content} onChange={event => setContent(event.target.value)} /></label>
      <label>共享上下文<textarea aria-label="消息共享上下文" rows={3} maxLength={32000} value={context} onChange={event => setContext(event.target.value)} /></label>
      {sender && <label className="pm-return-scope"><input type="checkbox" aria-label="允许对端继续询问发送方" checked={grants.some(grant => grant.agent_id === sender.id)}
        onChange={event => {
          if (event.target.checked && selectedSender) setGrants(values => [...values.filter(grant => grant.agent_id !== sender.id), { agent_id: sender.id, agent_version: sender.version, tool_names: defaultPeerTools(selectedSender, permissions.tools) }])
          else setGrants(values => values.filter(grant => grant.agent_id !== sender.id))
        }} />允许对端继续询问发送方</label>}
      <PeerScopeEditor agents={agents} tools={permissions.tools} value={grants} onChange={setGrants} disabled={saving || permissions.loading} />
    </fieldset>
    <footer><button type="submit" className="pc-primary" disabled={saving || (!submitted.current && !canSend)}>{saving ? <Loader2 className="pa-spin" size={15} /> : <Send size={15} />}{saving ? '正在发送…' : '发送消息'}</button></footer>
  </form>
}

export function PeerConversations({ api, collaboration, busy = false, onOpenSession }: {
  api: Pick<PersonalMessagesApi, 'personalMessages' | 'personalMessage' | 'acknowledgePersonalMessage'>
  collaboration: PersonalCollaboration; busy?: boolean
  onOpenSession?: (sessionId: string) => Promise<unknown>
}) {
  const [messages, setMessages] = useState<PersonalAgentMessage[]>(collaboration.messages || [])
  const [listError, setListError] = useState('')
  const [loading, setLoading] = useState(false)
  const [selectedId, setSelectedId] = useState('')
  const [detail, setDetail] = useState<PersonalAgentMessage | null>(null)
  const [detailError, setDetailError] = useState('')
  const [detailLoading, setDetailLoading] = useState(false)
  const [actionError, setActionError] = useState('')
  const [acking, setAcking] = useState(false)
  const [opening, setOpening] = useState(false)
  const [revision, setRevision] = useState(0)
  const alive = useRef(true), ackInFlight = useRef(false)
  const knownFull = useRef(new Map<string, PersonalAgentMessage>())
  const collaborationActive = ['admitted', 'claimed', 'running', 'waiting'].includes(collaboration.status)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  useEffect(() => { knownFull.current.clear(); setMessages(collaboration.messages || []); setSelectedId(''); setDetail(null); setActionError('') }, [api, collaboration.id])
  useEffect(() => {
    let stopped = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const load = async () => {
      setLoading(true)
      try {
        const result = await api.personalMessages(collaboration.id)
        if (!Array.isArray(result.messages)) throw new Error('Agent 消息列表返回不完整，请重试')
        const values = result.messages.map(value => validatePersonalMessage(value))
        if (values.some(value => value.collaboration_id !== collaboration.id)) throw new Error('消息与当前协作不一致，请重新加载')
        if (stopped) return
        setMessages(values.map(value => retainMessageFacts(knownFull.current.get(value.id), value, false))); setListError('')
        if (collaborationActive || values.some(value => activeMessageStates.has(value.state))) timer = setTimeout(() => void load(), 1800)
      } catch (cause) { if (!stopped) setListError(personalError(cause)) }
      finally { if (!stopped) setLoading(false) }
    }
    void load()
    return () => { stopped = true; clearTimeout(timer) }
  }, [api, collaboration.id, collaborationActive, revision])
  const selectedSummary = messages.find(message => message.id === selectedId)
  const detailRevision = selectedSummary ? `${selectedSummary.state}:${selectedSummary.response_sha256}:${selectedSummary.acknowledged_at}` : ''
  useEffect(() => {
    let stopped = false
    let timer: ReturnType<typeof setTimeout> | undefined
    if (!selectedId) { setDetail(null); setDetailError(''); return }
    const load = async () => {
      setDetailLoading(true)
      try {
        const value = validatePersonalMessage(await api.personalMessage(selectedId), selectedId, true)
        if (value.collaboration_id !== collaboration.id) throw new Error('消息与当前协作不一致，请重新加载')
        if (stopped) return
        const merged = retainMessageFacts(knownFull.current.get(value.id), value, true)
        knownFull.current.set(value.id, merged)
        setDetail(merged); setDetailError('')
        if (activeMessageStates.has(value.state)) timer = setTimeout(() => void load(), 1500)
      } catch (cause) { if (!stopped) setDetailError(personalError(cause)) }
      finally { if (!stopped) setDetailLoading(false) }
    }
    void load()
    return () => { stopped = true; clearTimeout(timer) }
  }, [api, collaboration.id, selectedId, detailRevision, revision])
  const selected = detail?.id === selectedId ? detail : null
  const acknowledge = async () => {
    if (!selected || ackInFlight.current || !/^[a-f0-9]{64}$/.test(selected.response_sha256)) return
    ackInFlight.current = true; setAcking(true); setActionError('')
    try {
      const value = validatePersonalMessage(await api.acknowledgePersonalMessage(selected.id, { expected_response_sha256: selected.response_sha256 }), selected.id, true)
      if (value.collaboration_id !== collaboration.id || value.response_sha256 !== selected.response_sha256
        || !value.acknowledged_at || value.state !== 'acknowledged') throw new Error('收件确认状态未核实，请重新加载对话')
      if (alive.current) {
        const merged = retainMessageFacts(knownFull.current.get(value.id), value, true)
        knownFull.current.set(value.id, merged)
        setDetail(merged); setMessages(values => values.map(message => message.id === value.id ? merged : message))
      }
    } catch (cause) { if (alive.current) setActionError(personalError(cause)) }
    finally { ackInFlight.current = false; if (alive.current) setAcking(false) }
  }
  const open = async (sessionId: string) => {
    if (!onOpenSession || opening || busy || acking) return
    setOpening(true); setActionError('')
    try { await onOpenSession(sessionId) }
    catch (cause) { if (alive.current) setActionError(personalError(cause)) }
    finally { if (alive.current) setOpening(false) }
  }
  return <section className="pm-conversations" aria-label="Agent 双方对话">
    <header><h4><Mail size={15} />Agent 消息 <span>{messages.length}</span></h4><button type="button" className="pc-icon" aria-label="刷新Agent 消息" disabled={acking || opening || loading} onClick={() => setRevision(value => value + 1)}><RefreshCw size={13} className={loading ? 'pa-spin' : ''} /></button></header>
    {listError && <div className="pc-error" role="alert">{listError}<button type="button" onClick={() => setRevision(value => value + 1)}>重试加载消息</button></div>}
    {!messages.length && !listError && <div className="pm-empty" role="status">{loading ? '正在加载Agent 消息…' : '尚无Agent 消息'}</div>}
    <div className="pm-message-list">{messages.map(message => <button type="button" key={message.id} className={`pm-summary${message.id === selectedId ? ' selected' : ''}`}
      aria-label={`查看${message.sender_name}与${message.recipient_name}的对话`} aria-expanded={message.id === selectedId} disabled={acking || opening}
      onClick={() => { setActionError(''); setSelectedId(value => value === message.id ? '' : message.id) }}>
      <header><strong>{message.sender_name}<ArrowRight size={12} />{message.recipient_name}</strong><span className={`pm-state pm-state-${message.state}`}>{messageLabels[message.state]}</span></header>
      <p>{message.content.slice(0, 220)}{(message.content.length > 220 || message.content_truncated) && '…'}</p><time>{stamp(message.created_at)}</time>
    </button>)}</div>
    {selectedId && <div className="pm-thread" aria-label="Agent 完整对话">
      {detailError && <div className="pc-error" role="alert">{detailError}<button type="button" onClick={() => setRevision(value => value + 1)}>重试加载对话</button></div>}
      {actionError && <div className="pc-error" role="alert">{actionError}</div>}
      {detailLoading && !selected && <div className="pm-empty" role="status">正在打开双方对话…</div>}
      {selected && <>
        <header className="pm-thread-heading"><strong>{selected.sender_name}<ArrowRight size={14} />{selected.recipient_name}</strong><span className={`pm-state pm-state-${selected.state}`}>{messageLabels[selected.state]}</span></header>
        <article className="pm-bubble pm-request" aria-label="发送方原始请求"><header><strong>{selected.sender_name}</strong><code>v{selected.sender_version}</code></header><p>{selected.content}</p>
          {selected.context && <details><summary>共享上下文</summary><pre>{selected.context}</pre></details>}
          {onOpenSession && selected.source_run_id && <button type="button" disabled={busy || acking || opening} onClick={() => void open(selected.source_session_id)}><ExternalLink size={12} />查看发送方执行</button>}
        </article>
        {(selected.response || selected.error || selected.receiver_run_id) && <article className="pm-bubble pm-response" aria-label="接收方原始回应"><header><strong>{selected.recipient_name}</strong><code>v{selected.recipient_version}</code></header>
          {selected.response && <MessageContent content={selected.response} />}{selected.error && <div className="pc-error" role="alert">{selected.error}</div>}
          {onOpenSession && selected.receiver_run_id && <button type="button" disabled={busy || acking || opening} onClick={() => void open(selected.receiver_session_id)}><ExternalLink size={12} />查看接收方执行</button>}
        </article>}
        <ol className="pm-receipts" aria-label="消息交付回执">{selected.receipts?.map(receipt => <li key={receipt.sequence}><Check size={12} /><strong>{['acknowledged', 'peer_acknowledged'].includes(receipt.kind)
          ? receipt.data.actor_kind === 'user' ? '用户确认收件' : receipt.data.actor_kind === 'agent' ? '发送方确认收件' : '确认收件'
          : receiptLabels[receipt.kind] || '交付记录'}</strong><time>{stamp(receipt.created_at)}</time></li>)}</ol>
        {selected.state === 'responded' && selected.outcome === 'completed' && !selected.acknowledged_at && <button type="button" className="pc-primary pm-ack" disabled={acking || opening || !/^[a-f0-9]{64}$/.test(selected.response_sha256)} onClick={() => void acknowledge()}>{acking ? <Loader2 size={13} className="pa-spin" /> : <Check size={13} />}{acking ? '正在确认…' : '确认收到'}</button>}
        <details className="pm-evidence"><summary>消息与执行标识</summary><dl><dt>消息</dt><dd>{selected.id}</dd><dt>根执行</dt><dd>{selected.root_job_id}</dd><dt>发送方 Run</dt><dd>{selected.source_run_id || '尚未开始'}</dd><dt>接收方 Run</dt><dd>{selected.receiver_run_id || '尚未开始'}</dd><dt>结果摘要</dt><dd>{selected.response_sha256 || '尚无结果'}</dd></dl></details>
      </>}
    </div>}
  </section>
}
