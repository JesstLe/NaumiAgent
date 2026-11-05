import { useEffect, useRef, useState, type FormEvent } from 'react'
import type { FollowupChangeField, FollowupChangeKind, FollowupRule, FollowupRuleBody } from '@naumi/shared/api/personalFollowups'
import type { CalendarEvent } from '@naumi/shared/api/personalConnections'
import type { TodayEntry } from '@naumi/shared/api/personalToday'
import { validateTodayEntry } from '../today/usePersonalToday'
import { FollowupDialog } from './FollowupDialog'
import { requestArtifactNavigation } from '../library/navigation'
import { followupError, followupRequestId, type FollowupApi, type FollowupState } from './useFollowups'
import { followupCanonical, validateFollowupRuleBody } from './followupValidation'
export type FollowupRuleSeed = { kind: 'deadline'; item_id: string; actor_id?: string } | { kind: 'calendar_change'; connection_id: string; event_id?: string }
const kinds: Record<FollowupChangeKind, string> = { created: '新增', updated: '修改', cancelled: '取消', unavailable: '不可用', restored: '恢复' }
const fields: Record<FollowupChangeField, string> = { summary: '名称', description: '描述', start: '开始时间', end: '结束时间', status: '状态', available: '可用性' }
export function FollowupRuleEditor({ api, state, rule, seed, onClose }: { api: FollowupApi; state: FollowupState; rule?: FollowupRule; seed?: FollowupRuleSeed; onClose: () => void }) {
  const [kind, setKind] = useState<'deadline' | 'calendar_change'>(rule?.body.trigger.kind || seed?.kind || 'deadline')
  const [name, setName] = useState(rule?.body.name || ''), [actorId, setActorId] = useState(rule?.body.actor_id || (seed?.kind === 'deadline' ? seed.actor_id : '') || 'main')
  const [timezone, setTimezone] = useState(rule?.body.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai'), [status, setStatus] = useState<'active' | 'paused'>(rule?.body.status === 'paused' ? 'paused' : 'active')
  const deadline = rule?.body.trigger.kind === 'deadline' ? rule.body.trigger : null, calendar = rule?.body.trigger.kind === 'calendar_change' ? rule.body.trigger : null
  const [allItems, setAllItems] = useState(deadline ? deadline.item_ids.length === 0 : false), [itemIds, setItemIds] = useState<string[]>(deadline?.item_ids || (seed?.kind === 'deadline' ? [seed.item_id] : []))
  const [offsets, setOffsets] = useState(deadline?.offsets_minutes.join(', ') || '1440, 0'), [connectionId, setConnectionId] = useState(calendar?.connection_id || (seed?.kind === 'calendar_change' ? seed.connection_id : '') || state.context?.connections.find(row => row.status === 'connected')?.id || '')
  const [eventId, setEventId] = useState(calendar?.event_id || (seed?.kind === 'calendar_change' ? seed.event_id || '' : '')), [allEvents, setAllEvents] = useState(calendar ? calendar.event_id === null : false)
  const [changeKinds, setChangeKinds] = useState<FollowupChangeKind[]>(calendar?.change_kinds || ['updated', 'cancelled']), [changeFields, setChangeFields] = useState<FollowupChangeField[]>(calendar?.fields || ['start', 'end', 'status'])
  const [includeExisting, setIncludeExisting] = useState(rule?.body.include_existing || false), [lateness, setLateness] = useState(rule?.body.max_lateness_minutes || 1440), [freshness, setFreshness] = useState((rule?.body.freshness_seconds || 3600) / 60), [misfire, setMisfire] = useState<'latest' | 'skip'>(rule?.body.misfire_policy || 'latest')
  const [items, setItems] = useState<TodayEntry[]>([]), [events, setEvents] = useState<CalendarEvent[]>([]), [nextItem, setNextItem] = useState<string | null>(null), [nextEvent, setNextEvent] = useState<number | null>(null), [sourceLoading, setSourceLoading] = useState(false), [error, setError] = useState('')
  const [moreLoading, setMoreLoading] = useState(false), sourceSeq = useRef(0)
  const form = followupCanonical({ name, actorId, timezone, status, kind, allItems, itemIds, offsets, connectionId, eventId, allEvents, changeKinds, changeFields, includeExisting, lateness, freshness, misfire }), initial = useRef(form)
  useEffect(() => {
    const controller = new AbortController(), seq = ++sourceSeq.current
    setItems([]); setEvents([]); setNextItem(null); setNextEvent(null); setMoreLoading(false); setError(''); setSourceLoading(true)
    const load = async () => {
      try {
        if (!state.context) return
        if (kind === 'deadline') { const page = await api.getPersonalTodaySources({ limit: 50 }, controller.signal); const rows = page.entries.map(row => validateTodayEntry(row, state.context!.workspace_key)); if (!controller.signal.aborted && seq === sourceSeq.current) { setItems(rows.filter(row => row.availability === 'available')); setNextItem(page.next_after) } }
        else if (connectionId) { const page = await api.personalCalendarEvents(connectionId, { limit: 50, offset: 0 }, controller.signal); if (page.events.some(row => row.connection_id !== connectionId || row.source_ref?.connection_id !== connectionId)) throw new Error('原日历来源不一致'); if (!controller.signal.aborted && seq === sourceSeq.current) { setEvents(page.events); setNextEvent(page.next_offset) } }
      } catch (cause) { if (!controller.signal.aborted && seq === sourceSeq.current) setError(followupError(cause)) }
      finally { if (!controller.signal.aborted && seq === sourceSeq.current) setSourceLoading(false) }
    }
    void load(); return () => { controller.abort(); sourceSeq.current++ }
  }, [api, kind, connectionId, state.context])
  const more = async () => {
    if (moreLoading || sourceLoading || !state.context) return
    const seq = sourceSeq.current; setMoreLoading(true)
    try {
      if (kind === 'deadline' && nextItem) { const page = await api.getPersonalTodaySources({ limit: 50, after: nextItem }); const rows = page.entries.map(row => validateTodayEntry(row, state.context!.workspace_key)); if (seq !== sourceSeq.current) return; setItems(old => [...old, ...rows.filter(row => row.availability === 'available' && !old.some(value => value.metadata_id === row.metadata_id))]); setNextItem(page.next_after) }
      else if (kind === 'calendar_change' && nextEvent !== null) { const page = await api.personalCalendarEvents(connectionId, { limit: 50, offset: nextEvent }); if (seq !== sourceSeq.current) return; if (page.events.some(row => row.connection_id !== connectionId || row.source_ref?.connection_id !== connectionId)) throw new Error('原日历分页身份不一致'); setEvents(old => [...old, ...page.events.filter(row => !old.some(value => value.occurrence_key === row.occurrence_key))]); setNextEvent(page.next_offset) }
    } catch (cause) { if (seq === sourceSeq.current) { setItems([]); setEvents([]); setError(followupError(cause)) } }
    finally { if (seq === sourceSeq.current) setMoreLoading(false) }
  }
  const save = async (event: FormEvent) => {
    event.preventDefault(); setError('')
    try {
      const actor = state.context?.actors.find(row => row.id === actorId); if (!actor) throw new Error('所选 Agent 已变化，请重新选择')
      if (kind === 'deadline' && !allItems && !itemIds.length) throw new Error('请选择事项，或明确启用该 Agent 的全部截止事项')
      if (kind === 'calendar_change' && !allEvents && !eventId) throw new Error('请选择原事件，或明确启用该连接的全部事件')
      const parsedOffsets = offsets.split(/[,，]/).map(value => Number(value.trim())); if (offsets.split(/[,，]/).some(value => !value.trim())) throw new Error('请输入有效跟进时点，单位为分钟')
      const body = validateFollowupRuleBody({ name: name.trim(), actor_id: actor.id, actor_version: actor.version, status, timezone: timezone.trim(), include_existing: includeExisting, max_lateness_minutes: lateness, freshness_seconds: freshness * 60, misfire_policy: misfire, trigger: kind === 'deadline' ? { kind, item_ids: allItems ? [] : itemIds, offsets_minutes: parsedOffsets } : { kind, connection_id: connectionId, event_id: allEvents ? null : eventId, change_kinds: changeKinds, fields: changeFields } } satisfies FollowupRuleBody)
      const receipt = rule ? await state.submit('update_rule', { payload: { request_id: followupRequestId(), rule_id: rule.id, expected_revision: rule.revision, rule: body } }) : await state.submit('create_rule', { payload: { request_id: followupRequestId(), rule: body } })
      if (receipt) onClose()
    } catch (cause) { setError(followupError(cause)) }
  }
  const toggle = <T,>(values: T[], value: T) => values.includes(value) ? values.filter(row => row !== value) : [...values, value]
  const eligible = items.filter(row => row.deadline && (actorId === 'main' || row.item_ref.assignee_id === actorId))
  return <FollowupDialog title={rule ? '编辑跟进规则' : '新建跟进规则'} dirty={form !== initial.current} busy={state.posting} onStopWaiting={state.stopWaiting} onClose={onClose}><form onSubmit={event => void save(event)}><fieldset disabled={state.locked}>
    <label>规则名称<input autoFocus required maxLength={80} aria-label="跟进规则名称" value={name} onChange={event => setName(event.target.value)} /></label>
    <div className="pf-form-row"><label>Agent<select aria-label="跟进 Agent" disabled={Boolean(rule)} value={actorId} onChange={event => { setActorId(event.target.value); setItemIds([]) }}>{state.context?.actors.map(actor => <option key={actor.id} value={actor.id}>{actor.name} · v{actor.version}</option>)}</select></label><label>来源<select aria-label="跟进来源类型" value={kind} onChange={event => setKind(event.target.value as typeof kind)}><option value="deadline">事项截止</option><option value="calendar_change">日历变更</option></select></label></div>
    {kind === 'deadline' ? <><label className="pf-check"><input type="checkbox" checked={allItems} onChange={event => setAllItems(event.target.checked)} />该 Agent 的全部有截止事项</label>{!allItems && <div className="pf-source-picker" aria-label="跟进事项范围">{eligible.map(row => <label className="pf-check" key={row.metadata_id}><input type="checkbox" checked={itemIds.includes(row.item_ref.item_id)} onChange={() => setItemIds(toggle(itemIds, row.item_ref.item_id))} />{row.title}</label>)}{!sourceLoading && !eligible.length && <span>暂无可选截止事项</span>}{itemIds.filter(id => !eligible.some(row => row.item_ref.item_id === id)).map(id => <label className="pf-check" key={id}><input type="checkbox" checked onChange={() => setItemIds(itemIds.filter(value => value !== id))} /><code>{id}</code><span>来源待核对</span></label>)}</div>}<label>跟进时点（分钟）<input aria-label="跟进时点分钟" required value={offsets} onChange={event => setOffsets(event.target.value)} /></label><div className="pf-inline-options"><button type="button" onClick={() => setOffsets('1440, 0')}>提前一天、截止时</button><button type="button" onClick={() => setOffsets('60, 0, -60')}>前后一小时</button></div></> : <><label>日历连接<select aria-label="跟进日历连接" value={connectionId} onChange={event => { setConnectionId(event.target.value); setEventId('') }}><option value="">选择连接</option>{state.context?.connections.map(row => <option key={row.id} value={row.id} disabled={row.status !== 'connected'}>{row.email} · {row.status === 'connected' ? '已连接' : '不可用'}</option>)}</select></label><label className="pf-check"><input type="checkbox" checked={allEvents} onChange={event => setAllEvents(event.target.checked)} />该连接的全部事件</label>{!allEvents && <label>原事件<select aria-label="跟进原事件" value={eventId} onChange={event => setEventId(event.target.value)}><option value="">选择事件</option>{events.map(row => <option key={row.occurrence_key} value={row.event_id}>{row.summary || '无标题事件'} · v{row.revision}</option>)}{eventId && !events.some(row => row.event_id === eventId) && <option value={eventId}>{eventId} · 来源待核对</option>}</select></label>}<section><h3>变更类型</h3><div className="pf-check-grid">{Object.entries(kinds).map(([id, name]) => <label className="pf-check" key={id}><input type="checkbox" checked={changeKinds.includes(id as FollowupChangeKind)} onChange={() => setChangeKinds(toggle(changeKinds, id as FollowupChangeKind))} />{name}</label>)}</div><h3>关注字段</h3><div className="pf-check-grid">{Object.entries(fields).map(([id, name]) => <label className="pf-check" key={id}><input type="checkbox" checked={changeFields.includes(id as FollowupChangeField)} onChange={() => setChangeFields(toggle(changeFields, id as FollowupChangeField))} />{name}</label>)}</div></section></>}
    {sourceLoading && <p role="status">正在核对来源…</p>}{(kind === 'deadline' ? nextItem : nextEvent !== null) && <button type="button" disabled={sourceLoading || moreLoading} onClick={() => void more()}>更多原来源</button>}
    <label className="pf-check"><input type="checkbox" checked={includeExisting} onChange={event => setIncludeExisting(event.target.checked)} />纳入当前已存在的来源</label>
    <details className="pf-rule-advanced"><summary>时间与补发</summary><div className="pf-form-row"><label>规则时区<input required aria-label="跟进规则时区" value={timezone} onChange={event => setTimezone(event.target.value)} /></label><label>状态<select aria-label="规则状态" value={status} onChange={event => setStatus(event.target.value as typeof status)}><option value="active">有效</option><option value="paused">暂停</option></select></label></div><div className="pf-form-row"><label>错过触发<select aria-label="错过跟进处理" value={misfire} onChange={event => setMisfire(event.target.value as typeof misfire)}><option value="latest">仅保留最近阶段</option><option value="skip">跳过错过阶段</option></select></label><label>最多补发延迟（分钟）<input aria-label="跟进补发窗口" type="number" min={1} max={10080} value={lateness} onChange={event => setLateness(Number(event.target.value))} /></label></div><label>日历同步有效期（分钟）<input aria-label="日历同步有效期" type="number" min={1} max={10080} value={freshness} onChange={event => setFreshness(Number(event.target.value))} /></label></details>
  </fieldset>{error && <div className="pf-error" role="alert">{error}</div>}<footer className="pf-actions"><button type="button" onClick={() => requestArtifactNavigation(onClose)}>取消</button><button type="submit" className="pf-primary" disabled={state.locked || sourceLoading}>{state.posting ? '正在保存…' : '保存规则'}</button></footer></form></FollowupDialog>
}
