import { useEffect, useRef, useState, type FormEvent } from 'react'
import type { CalendarActionBody } from '@naumi/shared/api/personalConnections'
import type { FollowupAcceptTarget, FollowupProposal } from '@naumi/shared/api/personalFollowups'
import { calendarInstant, calendarWallTime } from '../connections/calendarTime'
import { todayDate } from '../today/todayLocation'
import { validateTodayView } from '../today/usePersonalToday'
import { requestArtifactNavigation } from '../library/navigation'
import { FollowupDialog } from './FollowupDialog'
import { FollowupSourceCard } from './FollowupSourceCard'
import { followupCanonical, validateFollowupProposal } from './followupValidation'
import { followupError, followupRequestId, type FollowupApi, type FollowupState } from './useFollowups'
export function FollowupAcceptDialog({ api, state, proposal, defaultDate, defaultTimezone, onClose, onAccepted }: { api: FollowupApi; state: FollowupState; proposal: FollowupProposal; defaultDate?: string; defaultTimezone?: string; onClose: () => void; onAccepted: () => void }) {
  const timezone = defaultTimezone || Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai', now = state.context?.server_now || new Date().toISOString()
  const after = proposal.source?.kind === 'calendar_change' ? proposal.source.after : null, activeEvent = after && after.status !== 'cancelled' && after.available !== false ? after : null
  const [date, setDate] = useState(defaultDate || todayDate(new Date(now), timezone)), [zone, setZone] = useState(activeEvent?.start?.timeZone || timezone)
  const [summary, setSummary] = useState(activeEvent?.summary ? `跟进：${activeEvent.summary}` : proposal.source?.kind === 'calendar_change' && !activeEvent ? '核对日程变更' : proposal.title || ''), [description, setDescription] = useState('')
  const [start, setStart] = useState(calendarWallTime(activeEvent?.end?.dateTime || new Date(Date.parse(now) + 3600000).toISOString(), activeEvent?.start?.timeZone || timezone))
  const [end, setEnd] = useState(calendarWallTime(new Date(Date.parse(activeEvent?.end?.dateTime || now) + (activeEvent?.end?.dateTime ? 1800000 : 7200000)).toISOString(), activeEvent?.start?.timeZone || timezone))
  const [fold, setFold] = useState<0 | 1>(0), [expiry, setExpiry] = useState(calendarWallTime(new Date(Date.parse(now) + 3600000).toISOString(), timezone)), [error, setError] = useState(''), [preparing, setPreparing] = useState(false)
  const form = followupCanonical({ date, zone, summary, description, start, end, fold, expiry }), initial = useRef(form), alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const accept = async (event: FormEvent) => {
    event.preventDefault(); if (preparing || state.locked) return
    setPreparing(true); setError('')
    try {
      if (!state.context) throw new Error('工作区已变化')
      const current = validateFollowupProposal(await api.executePersonalFollowupOperation('get', { proposal_id: proposal.id }), state.context.workspace_key)
      if (!alive.current) return
      state.observe(current)
      if (current.id !== proposal.id || current.availability !== 'available' || current.decision.status !== 'pending' || current.decision.revision !== proposal.decision.revision || followupCanonical(current.proposal_ref) !== followupCanonical(proposal.proposal_ref) || followupCanonical(current.source) !== followupCanonical(proposal.source) || !current.source) throw new Error('原建议或来源已变化，请刷新后重新核对')
      let target: FollowupAcceptTarget
      if (current.effect_kind === 'today_place' && current.source.kind === 'deadline') {
        const [day, metadata] = await Promise.all([api.getPersonalToday({ date, timezone: zone, planner_id: current.actor_id, limit: 50 }), api.getPersonalTodayMetadata(current.source.item_ref.item_id)])
        if (!alive.current) return
        validateTodayView(day, state.context.workspace_key, { date, timezone: zone, planner_id: current.actor_id })
        if (metadata.metadata_id !== current.source.metadata_ref.metadata_id || metadata.revision !== current.source.current_metadata_revision || metadata.entry.availability !== 'available' || followupCanonical(metadata.entry.item_ref) !== followupCanonical(current.source.item_ref)) throw new Error('原截止资料已变化，请刷新建议')
        target = { kind: 'today_place', date, timezone: zone, expected_day_revision: day.day.revision, expected_metadata_revision: metadata.revision }
      } else if (current.effect_kind === 'calendar_draft') {
        const began = calendarInstant(start, zone, fold), finished = calendarInstant(end, zone, fold), expires = calendarInstant(expiry, timezone)
        if (!summary.trim()) throw new Error('请填写日程草稿名称')
        if (Date.parse(finished) <= Date.parse(began)) throw new Error('结束时间必须晚于开始时间')
        const serverNow = Date.parse(state.context.server_now)
        if (Date.parse(expires) <= serverNow || Date.parse(expires) > serverNow + 86400000) throw new Error('审批截止需在未来 24 小时内')
        const body: CalendarActionBody = { summary: summary.trim(), description, start: { dateTime: began, timeZone: zone }, end: { dateTime: finished, timeZone: zone } }
        target = { kind: 'calendar_draft', body, expires_at: expires }
      } else throw new Error('原建议目标类型待核对')
      const receipt = await state.submit('accept', { payload: { request_id: followupRequestId(), proposal_ref: current.proposal_ref, expected_decision_revision: current.decision.revision, target } })
      if (receipt && alive.current) { onAccepted(); onClose(); void state.verifyTarget(current.id) }
    } catch (cause) { if (alive.current) setError(followupError(cause)) }
    finally { if (alive.current) setPreparing(false) }
  }
  return <FollowupDialog title={proposal.effect_kind === 'today_place' ? '采纳到安排' : '采纳为日程草稿'} dirty={form !== initial.current} busy={preparing || state.posting} onStopWaiting={state.posting ? state.stopWaiting : undefined} onClose={onClose}><FollowupSourceCard proposal={proposal} /><form onSubmit={event => void accept(event)}><fieldset disabled={preparing || state.locked}>
    <div className="pf-actor">{state.context?.actors.find(row => row.id === proposal.actor_id)?.name || '原 Agent'} · {proposal.effect_kind === 'today_place' ? '规划' : '提出草稿'}</div>
    {proposal.effect_kind === 'today_place' ? <div className="pf-form-row"><label>安排日期<input aria-label="采纳安排日期" type="date" required value={date} onChange={event => setDate(event.target.value)} /></label><label>安排时区<input aria-label="采纳安排时区" required value={zone} onChange={event => setZone(event.target.value)} /></label></div> : <><label>日程名称<input required aria-label="跟进日程名称" maxLength={200} value={summary} onChange={event => setSummary(event.target.value)} /></label><div className="pf-form-row"><label>开始<input aria-label="跟进日程开始" type="datetime-local" required value={start} onChange={event => setStart(event.target.value)} /></label><label>结束<input aria-label="跟进日程结束" type="datetime-local" required value={end} onChange={event => setEnd(event.target.value)} /></label></div><div className="pf-form-row"><label>时区<input aria-label="跟进日程时区" required value={zone} onChange={event => setZone(event.target.value)} /></label><label>夏令时重复时刻<select aria-label="跟进日程重复时刻" value={fold} onChange={event => setFold(Number(event.target.value) as 0 | 1)}><option value={0}>第一次</option><option value={1}>第二次</option></select></label></div><label>描述<textarea aria-label="跟进日程描述" rows={4} maxLength={16000} value={description} onChange={event => setDescription(event.target.value)} /></label><label>审批截止<input aria-label="跟进草稿审批截止" type="datetime-local" required value={expiry} onChange={event => setExpiry(event.target.value)} /></label></>}
  </fieldset>{error && <div className="pf-error" role="alert">{error}</div>}<footer className="pf-actions"><button type="button" onClick={() => requestArtifactNavigation(onClose)}>取消</button><button type="submit" className="pf-primary" disabled={preparing || state.locked}>{preparing || state.posting ? '正在核对…' : proposal.effect_kind === 'today_place' ? '采纳并安排' : '建立待审批草稿'}</button></footer></form></FollowupDialog>
}
