import { useEffect, useId, useMemo, useRef, useState, type FormEvent } from 'react'
import { Archive, Bot, Check, ChevronDown, History, Loader2, Pause, Pencil, Play, Plus, RefreshCw, Search, Users, X } from 'lucide-react'
import type { PersonalAgent, PersonalAgentInput, PersonalTool } from '@naumi/shared/api/WorkbenchRuntimeClient'
import { personalError, type PersonalAgentsApi, type PersonalAgentsState } from './usePersonalAgents'
import './personal.css'

const stateLabels = { active: '启用', paused: '已暂停', archived: '已归档' } as const
const tierLabels = { fast: '快速', capable: '标准', reasoning: '深度思考' } as const

export function PersonalAgents({ api, state, busy, connected }: {
  api: PersonalAgentsApi
  state: PersonalAgentsState
  busy: boolean
  connected: boolean
}) {
  const [editor, setEditor] = useState<PersonalAgent | 'create' | null>(null)
  const [archived, setArchived] = useState(false)
  const visible = state.agents.filter(agent => archived || agent.status !== 'archived')
  const locked = busy || Boolean(state.opening) || !connected

  useEffect(() => { setEditor(null); setArchived(false) }, [api])
  useEffect(() => { if (!connected) setEditor(null) }, [connected])

  return <section className="pa-directory" aria-label="我的助理">
    <header className="pa-directory-heading">
      <strong>我的助理</strong>
      <div>
        <button type="button" aria-label="刷新助理目录" title="刷新" disabled={!connected || state.loading} onClick={() => void state.refresh()}><RefreshCw size={13} className={state.loading ? 'pa-spin' : ''} /></button>
        <button type="button" aria-label="创建助理" title="创建助理" disabled={locked} onClick={() => setEditor('create')}><Plus size={16} /></button>
      </div>
    </header>
    <div className="pa-agent-list">
      <button type="button" className={`pa-main-agent${!state.current && !state.currentPending && !state.currentError ? ' selected' : ''}`} disabled={locked} onClick={() => void state.select(null)}>
        <span className="pa-avatar pa-main-avatar"><Bot size={15} /></span><span>主助理</span>
        {state.opening === 'main' && <Loader2 size={13} className="pa-spin" />}
      </button>
      {state.loading && !visible.length && <div role="status" className="pa-directory-status">正在加载…</div>}
      {state.error && <div className="pa-directory-error" role="alert"><span>{state.error}</span><button type="button" disabled={!connected || state.loading} onClick={() => void state.refresh()}>重试</button></div>}
      {visible.map(agent => <div key={agent.id} className={`pa-agent-row${state.current?.id === agent.id ? ' selected' : ''}`}>
        <button type="button" className="pa-open-agent" aria-label={`与${agent.name}对话`} aria-current={state.current?.id === agent.id ? 'page' : undefined}
          disabled={locked || agent.status !== 'active'} onClick={() => void state.select(agent)} title={agent.description || agent.name}>
          <span className="pa-avatar">{agent.kind === 'team' ? <Users size={15} /> : <span>{Array.from(agent.name)[0]}</span>}</span>
          <span className="pa-agent-name">{agent.name}{agent.status !== 'active' && <small>{stateLabels[agent.status]}</small>}</span>
          {state.opening === agent.id && <Loader2 size={13} className="pa-spin" />}
        </button>
        <button type="button" className="pa-edit-agent" aria-label={`编辑${agent.name}`} disabled={locked} onClick={() => setEditor(agent)}><Pencil size={12} /></button>
      </div>)}
    </div>
    {state.agents.some(agent => agent.status === 'archived') && <button type="button" className="pa-archive-toggle" onClick={() => setArchived(value => !value)} aria-expanded={archived}><Archive size={12} />{archived ? '隐藏已归档' : '已归档'}<ChevronDown size={12} /></button>}
    {editor && <PersonalAgentEditor key={editor === 'create' ? 'create' : editor.id}
      api={api} agent={editor === 'create' ? null : editor} agents={state.agents} busy={locked}
      onClose={() => { setEditor(null); void state.refresh() }} onSaved={async () => { await state.refresh(); setEditor(null) }} />}
  </section>
}

export function PersonalAgentEditor({ api, agent, agents, busy, onClose, onSaved }: {
  api: PersonalAgentsApi
  agent: PersonalAgent | null
  agents: PersonalAgent[]
  busy: boolean
  onClose: () => void
  onSaved: () => Promise<unknown>
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const headingId = useId()
  const [name, setName] = useState(agent?.name || '')
  const [description, setDescription] = useState(agent?.description || '')
  const [instructions, setInstructions] = useState(agent?.instructions || '')
  const [kind, setKind] = useState<PersonalAgent['kind']>(agent?.kind || 'specialist')
  const [tier, setTier] = useState<PersonalAgent['model_tier']>(agent?.model_tier || 'capable')
  const [selectedTools, setSelectedTools] = useState<string[]>(agent?.tool_names || [])
  const [members, setMembers] = useState<string[]>(agent?.member_ids || [])
  const [tools, setTools] = useState<PersonalTool[]>([])
  const [toolsLoading, setToolsLoading] = useState(true)
  const [toolsError, setToolsError] = useState('')
  const [toolsAttempt, setToolsAttempt] = useState(0)
  const [toolQuery, setToolQuery] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [confirmation, setConfirmation] = useState<PersonalAgent['status'] | null>(null)
  const [revisions, setRevisions] = useState<PersonalAgent[] | null>(null)
  const [historyLoading, setHistoryLoading] = useState(false)
  const actionInFlight = useRef(false)
  const alive = useRef(true)
  const creationIdentity = useRef('')
  const locked = saving || busy
  const filteredTools = useMemo(() => tools.filter(tool =>
    `${tool.name} ${tool.description}`.toLowerCase().includes(toolQuery.trim().toLowerCase()),
  ), [tools, toolQuery])
  const memberOptions = agents.filter(candidate => candidate.id !== agent?.id
    && (candidate.status === 'active' || members.includes(candidate.id)))

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
    let cancelled = false
    setToolsLoading(true)
    setToolsError('')
    void api.personalTools().then(result => {
      if (!Array.isArray(result.tools)) throw new Error('工具目录返回不完整，请重试')
      if (!cancelled) setTools(result.tools)
    }).catch(cause => { if (!cancelled) setToolsError(personalError(cause)) })
      .finally(() => { if (!cancelled) setToolsLoading(false) })
    return () => { cancelled = true }
  }, [api, toolsAttempt])

  const apply = async (operation: () => Promise<unknown>) => {
    if (actionInFlight.current || busy) return
    actionInFlight.current = true
    setSaving(true)
    setError('')
    try { await operation(); if (alive.current) await onSaved() }
    catch (cause) { if (alive.current) setError(personalError(cause)) }
    finally { if (alive.current) setSaving(false); actionInFlight.current = false }
  }
  const save = (event: FormEvent) => {
    event.preventDefault()
    if (locked || toolsLoading || toolsError || !name.trim() || !instructions.trim()) return
    const body: PersonalAgentInput = {
      name: name.trim(), description: description.trim(), instructions: instructions.trim(),
      kind, model_tier: tier, tool_names: [...selectedTools].sort(),
      member_ids: kind === 'team' ? [...members].sort() : [],
    }
    if (agent) {
      const { kind: _kind, ...changes } = body
      void apply(() => api.updatePersonalAgent(agent.id, { ...changes, expected_version: agent.version }))
    } else {
      // Preserve one operation identity even after edits: an unknown earlier success
      // must produce a payload conflict rather than silently creating a duplicate.
      if (!creationIdentity.current) creationIdentity.current = crypto.randomUUID()
      void apply(() => api.createPersonalAgent({ ...body, request_id: creationIdentity.current }))
    }
  }
  const setStatus = () => {
    if (!agent || !confirmation) return
    void apply(() => api.updatePersonalAgent(agent.id, { status: confirmation, expected_version: agent.version }))
  }
  const history = async () => {
    if (!agent || historyLoading) return
    if (revisions) { setRevisions(null); return }
    setHistoryLoading(true); setError('')
    try {
      const result = await api.personalAgentHistory(agent.id)
      if (!Array.isArray(result.revisions)) throw new Error('版本记录返回不完整，请重试')
      if (alive.current) setRevisions(result.revisions)
    } catch (cause) { if (alive.current) setError(personalError(cause)) }
    finally { if (alive.current) setHistoryLoading(false) }
  }
  const toggleTool = (tool: string) => setSelectedTools(value => value.includes(tool) ? value.filter(name => name !== tool) : [...value, tool])
  const unavailableTools = selectedTools.filter(name => !tools.some(tool => tool.name === name))

  return <dialog ref={dialog} className="pa-editor" aria-labelledby={headingId} onCancel={event => { event.preventDefault(); if (!locked) onClose() }}>
    <header className="pa-editor-heading"><div><span className="pa-avatar">{kind === 'team' ? <Users size={18} /> : <Bot size={18} />}</span><h2 id={headingId}>{agent ? '编辑助理' : '创建助理'}</h2>{agent && <span className="pa-version">v{agent.version}</span>}</div><button type="button" aria-label="关闭助理设置" disabled={locked} onClick={onClose}><X size={18} /></button></header>
    <form onSubmit={save}>
      <div className="pa-editor-body">
        {error && <div className="pa-form-error" role="alert">{error}</div>}
        <fieldset disabled={locked || agent?.status === 'archived'} className="pa-fields">
          <div className="pa-field-row"><label>名称<input aria-label="名称" autoFocus required maxLength={80} value={name} onChange={event => setName(event.target.value)} /></label><label>类型<select aria-label="类型" value={kind} disabled={Boolean(agent)} onChange={event => setKind(event.target.value as PersonalAgent['kind'])}><option value="specialist">专员</option><option value="team">团队</option></select></label></div>
          <label>简介<input aria-label="简介" maxLength={500} value={description} onChange={event => setDescription(event.target.value)} /></label>
          <label>职责与工作方式<textarea aria-label="职责与工作方式" required rows={5} maxLength={12000} value={instructions} onChange={event => setInstructions(event.target.value)} /></label>
          <label>模型能力<select aria-label="模型能力" value={tier} onChange={event => setTier(event.target.value as PersonalAgent['model_tier'])}>{Object.entries(tierLabels).map(([value, text]) => <option key={value} value={value}>{text}</option>)}</select></label>
          {kind === 'team' && <fieldset className="pa-members"><legend>团队成员</legend>{memberOptions.length ? memberOptions.map(candidate => <label key={candidate.id}><input type="checkbox" checked={members.includes(candidate.id)} onChange={event => setMembers(value => event.target.checked ? [...value, candidate.id] : value.filter(id => id !== candidate.id))} />{candidate.name}{candidate.status !== 'active' && ` · ${stateLabels[candidate.status]}`}</label>) : <span className="pa-field-empty">尚无可加入的助理</span>}</fieldset>}
          <fieldset className="pa-tools"><legend>工具权限 · {selectedTools.length ? `已选 ${selectedTools.length}` : '仅对话'}</legend>
            <div className="pa-tool-search"><Search size={14} /><input aria-label="搜索可用工具" value={toolQuery} onChange={event => setToolQuery(event.target.value)} /></div>
            {toolsLoading ? <span role="status" className="pa-field-empty">正在加载工具…</span> : toolsError ? <div className="pa-form-error" role="alert">{toolsError}<button type="button" onClick={() => setToolsAttempt(value => value + 1)}>重试加载工具</button></div> : <>
              <div className="pa-tool-list">{filteredTools.map(tool => <label key={tool.name} title={tool.description}><input type="checkbox" checked={selectedTools.includes(tool.name)} onChange={() => toggleTool(tool.name)} /><span><strong>{tool.name}</strong><small>{tool.description}</small></span></label>)}{!filteredTools.length && <span className="pa-field-empty">{toolQuery ? '没有匹配的工具' : '暂无可授权工具'}</span>}</div>
              {!!unavailableTools.length && <div className="pa-unavailable-tools">{unavailableTools.map(name => <button key={name} type="button" onClick={() => toggleTool(name)} title="移除已失效工具">{name} · 已失效<X size={12} /></button>)}</div>}
            </>}
          </fieldset>
        </fieldset>
        {agent && <section className="pa-lifecycle" aria-label="助理状态管理">
          <div><span className={`pa-state pa-state-${agent.status}`}>{stateLabels[agent.status]}</span><button type="button" disabled={locked || historyLoading} onClick={() => void history()}><History size={14} />{revisions ? '收起版本记录' : historyLoading ? '加载版本…' : '版本记录'}</button></div>
          {revisions && <ol className="pa-history" aria-label="助理版本记录">{revisions.map(revision => <li key={revision.version}><strong>v{revision.version} · {revision.name}</strong><span>{stateLabels[revision.status]} · {tierLabels[revision.model_tier]}</span><time>{new Date(revision.updated_at).toLocaleString('zh-CN')}</time><p>{revision.instructions}</p></li>)}</ol>}
          <div className="pa-state-actions">{agent.status === 'active' ? <button type="button" disabled={locked} onClick={() => setConfirmation('paused')}><Pause size={14} />暂停助理</button> : <button type="button" disabled={locked} onClick={() => setConfirmation('active')}><Play size={14} />{agent.status === 'archived' ? '恢复助理' : '启用助理'}</button>}{agent.status !== 'archived' && <button type="button" className="pa-danger" disabled={locked} onClick={() => setConfirmation('archived')}><Archive size={14} />归档助理</button>}</div>
          {confirmation && <div className="pa-confirm" role="group" aria-label="确认状态变更"><strong>{confirmation === 'archived' ? `归档「${agent.name}」？` : confirmation === 'paused' ? `暂停「${agent.name}」？` : `启用「${agent.name}」？`}</strong>{confirmation !== 'active' && <p>保留会话和版本记录，停止接受新任务。</p>}<div><button type="button" disabled={locked} onClick={() => setConfirmation(null)}>取消变更</button><button type="button" className="pa-confirm-action" disabled={locked} onClick={setStatus}>确认{confirmation === 'archived' ? '归档' : confirmation === 'paused' ? '暂停' : '启用'}</button></div></div>}
        </section>}
      </div>
      <footer className="pa-editor-footer"><button type="button" disabled={locked} onClick={onClose}>取消</button><button type="submit" className="pa-primary" disabled={locked || toolsLoading || Boolean(toolsError) || !name.trim() || !instructions.trim() || agent?.status === 'archived'}>{saving ? <Loader2 size={14} className="pa-spin" /> : <Check size={14} />}{saving ? '正在保存…' : agent ? '保存修改' : '创建'}</button></footer>
    </form>
  </dialog>
}
