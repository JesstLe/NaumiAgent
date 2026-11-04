import { useMemo, useRef, useState } from 'react'
import { ArrowRight, ChevronDown, CircleStop, Clock3, ExternalLink, GitBranch, Loader2, RefreshCw, ShieldCheck, Users } from 'lucide-react'
import type { PersonalAgent, PersonalTeamTurn, PersonalTeamTurnStatus } from '@naumi/shared/api/WorkbenchRuntimeClient'
import { PersonalCollaborations, type PersonalCollaborationsApi } from './Collaborations'
import { PeerScopeEditor } from './PeerMessages'
import { activeTeamTurnStates, type TeamTurnsState } from './useTeamTurns'
import { personalError } from './usePersonalAgents'
import './team-conversation.css'

export const teamTurnLabels: Record<PersonalTeamTurnStatus, string> = {
  queued: '等待执行', planning: '协调中', working: '成员执行中', summarizing: '汇总中',
  completed: '已完成', cancelled: '已停止', error: '执行失败', unknown: '结果待核实',
  blocked: '执行受阻', delivery_pending: '结果待写入对话',
}
const phaseLabels = { planning: '协调', members: '成员协作', summary: '汇总' }
const stamp = (value: string) => {
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? '' : date.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false })
}

export function TeamComposerControls({ team, state, disabled = false }: {
  team: PersonalAgent; state: TeamTurnsState; disabled?: boolean
}) {
  const [abandon, setAbandon] = useState(false)
  const pending = Boolean(state.pending || state.identityError)
  return <section className="pt-compose-controls" aria-label="团队任务设置">
    {(state.error || state.historyError) && <div className="pt-error" role="alert">
      <span>{state.error || state.historyError}</span><button type="button" disabled={state.posting} onClick={state.refresh}>核对任务记录</button>
    </div>}
    {pending && <div className="pt-pending" role="status">
      <span><Clock3 size={13} />保留原请求{state.pending && <><code>{state.pending.request_id}</code>
        {!!state.pending.source_ids?.length && <small>资料 {state.pending.source_ids.length}</small>}</>}</span>
      <div><button type="button" disabled={state.posting} onClick={() => void state.submit('')}>重试原请求</button>
        <button type="button" disabled={state.posting} onClick={() => setAbandon(true)}>放弃重试</button></div>
    </div>}
    {abandon && <div className="pt-confirm" role="group" aria-label="确认放弃原请求重试">
      <strong>放弃原请求重试？</strong><p>这不会取消已受理的任务。原任务仍保留在团队执行记录中。</p>
      <div><button type="button" onClick={() => setAbandon(false)}>保留原请求</button>
        <button type="button" onClick={() => { state.abandonPending(); setAbandon(false) }}>确认放弃重试</button></div>
    </div>}
    <details className="pt-settings"><summary><Users size={13} />成员 {state.roster.members.length}<span>v{team.version}</span><ChevronDown size={12} /></summary>
      <div className="pt-member-roster" aria-label="当前团队成员">
        {state.roster.teams.filter(member => member.id !== team.id).map(member => <span className="pt-roster-team" key={member.id}><Users size={12} />{member.name}<code>v{member.version}</code></span>)}
        {state.roster.members.map(member => <span key={member.id}>{member.name}<code>v{member.version}</code>{member.status !== 'active' && <em>{member.status === 'paused' ? '已暂停' : '已归档'}</em>}</span>)}
        {!state.roster.members.length && <span className="pt-muted">尚无成员</span>}
      </div>
      {state.coordination && <>
        {state.roster.errors.map(error => <div key={error} className="pt-error" role="alert">{error}</div>)}
        {state.toolsError && <div className="pt-error" role="alert">{state.toolsError}<button type="button" onClick={state.refresh}>重新加载权限</button></div>}
        {state.toolsLoading ? <div className="pt-muted" role="status">正在加载成员权限…</div> : <PeerScopeEditor agents={state.roster.members} tools={state.tools} legend="本次成员与权限" selectionVerb="分配"
          value={state.pending?.member_grants || state.grants} onChange={state.setGrants} disabled={disabled || pending || state.posting || Boolean(state.toolsError)} />}
      </>}
      <label className="pt-context-label">共享资料<textarea aria-label="团队共享资料" rows={3} maxLength={32000}
        disabled={disabled || pending || state.posting} value={state.pending?.context ?? state.context} onChange={event => state.setContext(event.target.value)} /></label>
    </details>
  </section>
}

export function TeamTurnCard({ turn, state, api, agents, busy = false, onOpenSession }: {
  turn: PersonalTeamTurn; state: Pick<TeamTurnsState, 'cancel' | 'refresh'>
  api: PersonalCollaborationsApi; agents: PersonalAgent[]; busy?: boolean
  onOpenSession?: (sessionId: string) => Promise<unknown>
}) {
  const [details, setDetails] = useState<string | null>(null)
  const [confirm, setConfirm] = useState(false)
  const [stopping, setStopping] = useState(false)
  const cancelInFlight = useRef(false)
  const [error, setError] = useState('')
  const phases = useMemo(() => turn.phases.map(phase => phase.collaboration), [turn.phases])
  const members = turn.phases.find(phase => phase.phase === 'members')?.collaboration.steps || []
  const active = activeTeamTurnStates.has(turn.status)
  const interactions = turn.phases.flatMap(phase => {
    const events = phase.collaboration.events || []
    return events.filter(event => (event.kind === 'question_requested' && !events.some(answer =>
      ['question_answered', 'question_expired'].includes(answer.kind) && answer.data.request_id === event.data.request_id))
      || (event.kind === 'permission_requested' && !events.some(answer => answer.kind === 'permission_resolved'
        && answer.step_id === event.step_id && answer.data.session_id === event.data.session_id && answer.data.call_id === event.data.call_id)))
      .map(event => ({ phase, event }))
  })
  const stop = async () => {
    if (cancelInFlight.current) return
    cancelInFlight.current = true
    setStopping(true); setError('')
    try { await state.cancel(turn); setConfirm(false) }
    catch (cause) { setError(personalError(cause)) }
    finally { cancelInFlight.current = false; setStopping(false) }
  }
  return <section className={`pt-turn pt-turn-${turn.status}`} aria-label={`团队任务 ${turn.request_id}`}>
    <header><div><GitBranch size={14} /><strong>{turn.team_name}</strong><code>v{turn.team_version}</code></div>
      <span className={`pt-status pt-status-${turn.status}`} role="status">{active && <Loader2 size={12} className="pa-spin" />}{turn.cancel_requested && active ? '正在停止' : teamTurnLabels[turn.status]}</span></header>
    <div className="pt-phases" aria-label="团队执行阶段">{turn.phases.map((phase, index) => <span key={phase.phase}>
      {index > 0 && <ArrowRight size={11} />}<button type="button" disabled={busy} onClick={() => setDetails(phase.collaboration_id)}>{phaseLabels[phase.phase]}<ExternalLink size={11} /></button>
    </span>)}{!turn.phases.length && <span className="pt-muted">等待协调执行</span>}</div>
    {!!members.length && <ol className="pt-member-steps" aria-label="团队成员执行">{members.map(step => <li key={step.id}>
      <span>{step.agent_name || agents.find(agent => agent.id === step.agent_id)?.name || step.agent_id}<code>v{step.agent_version}</code></span>
      <span>{({ admitted: '排队中', claimed: '准备中', running: '执行中', completed: '已完成', error: '执行失败', timeout: '已超时', cancelled: '已停止', unknown: '结果待核实', blocked: '已阻塞', waiting: '等待执行', max_turns: '达到轮次上限' })[step.status]}</span>
    </li>)}</ol>}
    {active && !!interactions.length && <div className="pt-decisions" aria-label="团队待处理决定">{interactions.map(({ phase, event }) => <button type="button" key={`${phase.collaboration_id}:${event.key}`}
      onClick={() => setDetails(phase.collaboration_id)}><ShieldCheck size={13} /><strong>{typeof event.data.actor_name === 'string' && event.data.actor_name || '成员'}</strong>
      {event.kind === 'question_requested' ? '待回答' : '待授权'}<ArrowRight size={12} /></button>)}</div>}
    {(turn.error || error) && <div className="pt-error" role="alert">{turn.error || error}</div>}
    <footer><time>{stamp(turn.created_at)}</time><span>{turn.usage.job_count} 次执行{turn.usage.total_tokens > 0 && ` · ${turn.usage.total_tokens.toLocaleString()} tokens`}</span>
      <button type="button" aria-label="刷新团队任务" disabled={stopping} onClick={state.refresh}><RefreshCw size={12} /></button>
      {active && !turn.cancel_requested && <button type="button" className="pt-stop" disabled={stopping} onClick={() => setConfirm(true)}><CircleStop size={12} />停止</button>}
    </footer>
    {confirm && <div className="pt-confirm" role="group" aria-label="确认停止团队任务"><strong>停止这次团队任务？</strong><div>
      <button type="button" disabled={stopping} onClick={() => setConfirm(false)}>继续执行</button>
      <button type="button" disabled={stopping} onClick={() => void stop()}>{stopping ? '正在停止…' : '确认停止'}</button></div></div>}
    <details className="pt-identity"><summary>任务标识</summary><dl><dt>任务</dt><dd>{turn.id}</dd><dt>请求</dt><dd>{turn.request_id}</dd><dt>结果摘要</dt><dd>{turn.result_sha256 || '尚无结果'}</dd></dl></details>
    {details && <PersonalCollaborations api={api} agents={agents} sourceSessionId={turn.team_session_id} busy={busy}
      scopedCollaborations={phases} initialId={details} allowCancel={false} onClose={() => setDetails(null)} onOpenSession={onOpenSession} />}
  </section>
}

export function TeamConversation({ state, api, agents, visibleMessageIds, busy, onOpenSession }: {
  state: TeamTurnsState; api: PersonalCollaborationsApi; agents: PersonalAgent[]; visibleMessageIds: string[]
  busy: boolean; onOpenSession?: (sessionId: string) => Promise<unknown>
}) {
  const unanchored = state.turns.filter(turn => !visibleMessageIds.includes(turn.source_user_message_id))
  if (!unanchored.length && !state.loading && !state.historyError) return null
  return <section className="pt-turn-list" aria-label="团队任务记录">
    {state.loading && !state.turns.length && <span className="pt-muted" role="status">正在加载团队任务…</span>}
    {unanchored.map(turn => <div key={turn.id}><p className="pt-unanchored-content">{turn.content}</p><TeamTurnCard turn={turn} state={state} api={api} agents={agents} busy={busy} onOpenSession={onOpenSession} /></div>)}
  </section>
}
