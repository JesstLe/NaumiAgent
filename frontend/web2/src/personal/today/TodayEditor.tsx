import { useEffect, useRef, useState } from 'react'
import { ArrowLeft, History, RefreshCw } from 'lucide-react'
import type { TodayDeadlineInput, TodayEntry, TodayItemRef, TodayPriority } from '@naumi/shared/api/personalToday'
import { validateItem } from '../items/usePersonalItems'
import { requestArtifactNavigation } from '../library/navigation'
import { todayRequestId } from './todayLocation'
import { todayError, validateTodayRef, type PersonalTodayState, type PersonalTodayApi } from './usePersonalToday'

export function TodayEditor({ api, state, initial, entry, namespace, onDirty, onClose }: {
  api: PersonalTodayApi; state: PersonalTodayState; initial: TodayEntry; entry: TodayEntry | null; namespace: string
  onDirty: (value: boolean) => void; onClose: () => void
}) {
  const [priority, setPriority] = useState<TodayPriority>(initial.priority), [status, setStatus] = useState(initial.metadata_status)
  const [kind, setKind] = useState<'none' | 'date' | 'instant'>(initial.deadline?.kind || 'none')
  const [value, setValue] = useState(initial.deadline?.kind === 'date' ? initial.deadline.local_date : initial.deadline?.local_time || '')
  const [timezone, setTimezone] = useState(initial.deadline?.timezone || state.query.timezone)
  const [fold, setFold] = useState<string>(initial.deadline?.kind === 'instant' && initial.deadline.fold !== undefined ? String(initial.deadline.fold) : '')
  const [confirmTimezone, setConfirmTimezone] = useState(false)
  const [targetDate, setTargetDate] = useState(state.query.date), [moving, setMoving] = useState(false)
  const [latest, setLatest] = useState<TodayItemRef | null>(null), [error, setError] = useState('')
  const [keepDeadline, setKeepDeadline] = useState(true), [keepPriority, setKeepPriority] = useState(true)
  const [historyAfter, setHistoryAfter] = useState<number | null>(null), [historyLoading, setHistoryLoading] = useState(false)
  const [history, setHistory] = useState<Record<string, unknown>[] | null>(null), [historyOpen, setHistoryOpen] = useState(false)
  const timezoneChanged = initial.deadline?.status === 'timezone_data_changed' || entry?.deadline?.status === 'timezone_data_changed'
  const timezoneConfirmationRequired = timezoneChanged && kind !== 'none' && !confirmTimezone
  const form = JSON.stringify({ priority, status, kind, value, timezone, fold, confirmTimezone })
  const original = useRef(form), originalDate = useRef(state.query.date), alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [namespace])
  const dirty = form !== original.current || targetDate !== originalDate.current
  useEffect(() => { onDirty(dirty); return () => onDirty(false) }, [dirty, onDirty])
  useEffect(() => {
    const controller = new AbortController(); setLatest(null); setError('')
    if (entry?.availability === 'stale') void api.personalItem(initial.item_ref.item_id, controller.signal).then(raw => {
      if (controller.signal.aborted) return
      const item = validateItem(raw)
      if (item.item_id !== initial.item_ref.item_id) throw new Error('事项身份不一致')
      setLatest(validateTodayRef({ kind: 'item', workspace_key: initial.item_ref.workspace_key, item_id: item.item_id,
        item_revision: item.revision, once_key: item.once_key, definition_sha256: item.definition_sha256,
        assignee_id: item.definition.assignee.id, assignee_version: item.definition.assignee.version }, initial.item_ref.workspace_key))
    }).catch(cause => { if (!controller.signal.aborted) { setLatest(null); setError(todayError(cause)) } })
    return () => controller.abort()
  }, [api, namespace, initial.item_ref, entry?.availability])
  useEffect(() => {
    if (!historyOpen) return
    const controller = new AbortController(); setHistory(null); setHistoryAfter(null)
    void api.getPersonalTodayHistory('metadata', initial.metadata_id, { limit: 20 }, controller.signal).then(page => {
      if (!controller.signal.aborted) { setHistory(page.history); setHistoryAfter(page.next_after_revision ?? null) }
    }).catch(cause => { if (!controller.signal.aborted) setError(todayError(cause)) })
    return () => controller.abort()
  }, [api, namespace, initial.metadata_id, historyOpen])
  const moreHistory = async () => {
    if (historyAfter === null || historyLoading) return
    setHistoryLoading(true)
    try {
      const page = await api.getPersonalTodayHistory('metadata', initial.metadata_id, { limit: 20, after_revision: historyAfter })
      if (!alive.current) return
      setHistory(previous => [...(previous || []), ...page.history.filter(record => !previous?.some(old => old.revision === record.revision))])
      setHistoryAfter(page.next_after_revision ?? null)
    } catch (cause) { if (alive.current) { setHistory(null); setError(todayError(cause)) } }
    finally { if (alive.current) setHistoryLoading(false) }
  }
  const save = async () => {
    if (!entry || entry.availability !== 'available') return
    if (timezoneConfirmationRequired) { setError('请先确认采用当前时区规则'); return }
    let deadline: TodayDeadlineInput = null
    if (kind !== 'none') {
      if (!value || !timezone.trim()) { setError('请选择截止日期与时区'); return }
      deadline = kind === 'date' ? { kind, local_date: value, timezone: timezone.trim() }
        : { kind, local_time: value.length === 16 ? `${value}:00` : value, timezone: timezone.trim(), ...(fold !== '' ? { fold: Number(fold) as 0 | 1 } : {}) }
    }
    if (await state.submit({ operation: 'set-metadata', body: { request_id: todayRequestId(), expected_revision: initial.metadata_revision, source_ref: initial.item_ref, deadline, priority, status } })) {
      original.current = form; onDirty(false); onClose()
    }
  }
  const move = async () => {
    if (!entry?.placement_id || !state.view || !targetDate || targetDate === state.query.date) return
    setMoving(true); setError('')
    try {
      const target = await api.getPersonalToday({ ...state.query, date: targetDate })
      if (!alive.current) return
      if (target.workspace_key !== initial.item_ref.workspace_key || target.day.date !== targetDate || target.day.timezone !== state.query.timezone || target.day.planner_id !== state.query.planner_id) throw new Error('目标安排身份不一致')
      if (await state.submit({ operation: 'move', body: { request_id: todayRequestId(), day_id: state.view.day.id, expected_revision: state.view.day.revision,
        placement_id: entry.placement_id, target_date: targetDate, target_timezone: state.query.timezone, target_planner_id: state.query.planner_id, expected_target_revision: target.day.revision } })) { onDirty(false); onClose() }
    } catch (cause) { setError(todayError(cause)) }
    finally { setMoving(false) }
  }
  return <aside className="pt-editor" aria-label="编辑安排"><header><button aria-label="返回今日安排" onClick={() => requestArtifactNavigation(onClose)}><ArrowLeft size={17} /></button><h2>{entry?.availability === 'available' ? entry.title : '安排设置'}</h2></header>
    {error && <div className="pt-alert" role="alert">{error}</div>}
    {entry?.availability === 'stale' && <section className="pt-rebase" aria-label="确认事项新版本"><h3>事项已更新</h3><div className="pt-revision">原修订 {initial.item_ref.item_revision}{latest && <> → 修订 {latest.item_revision}</>}</div><label><input type="checkbox" checked={keepDeadline} onChange={event => setKeepDeadline(event.target.checked)} />保留原截止</label><label><input type="checkbox" checked={keepPriority} onChange={event => setKeepPriority(event.target.checked)} />保留原优先级</label><button className="pt-primary" disabled={!latest || state.locked || !state.view} onClick={() => {
      if (latest && state.view) void state.submit({ operation: 'rebase', body: { request_id: todayRequestId(), day_id: state.view.day.id, expected_day_revision: state.view.day.revision,
        metadata_id: initial.metadata_id, expected_metadata_revision: initial.metadata_revision, old_source_ref: initial.item_ref, new_source_ref: latest, preserve_deadline: keepDeadline, preserve_priority: keepPriority } }).then(saved => { if (saved) { onDirty(false); onClose() } })
    }}><RefreshCw size={14} />确认采用新版本</button></section>}
    {timezoneChanged && <section className="pt-timezone-change" role="group" aria-label="截止时区规则待确认"><h3>截止时区规则待确认</h3><dl><dt>原截止 UTC</dt><dd><time>{initial.deadline?.boundary_at || initial.deadline?.resolved_at || entry?.deadline?.boundary_at || entry?.deadline?.resolved_at}</time></dd></dl><label><input type="checkbox" aria-label="确认采用当前时区规则" checked={confirmTimezone} disabled={state.locked || moving || entry?.availability !== 'available'} onChange={event => setConfirmTimezone(event.target.checked)} />按当前规则重新确认截止</label></section>}
    <form onSubmit={event => { event.preventDefault(); void save() }}><fieldset disabled={state.locked || moving || entry?.availability !== 'available'}>
      <label>优先级<select aria-label="安排优先级" value={priority} onChange={event => setPriority(event.target.value as TodayPriority)}><option value="high">高</option><option value="normal">普通</option><option value="low">低</option></select></label>
      <label>截止<select aria-label="截止类型" value={kind} onChange={event => { setKind(event.target.value as typeof kind); setValue(''); setConfirmTimezone(false) }}><option value="none">不设截止</option><option value="date">日期</option><option value="instant">确切时刻</option></select></label>
      {kind !== 'none' && <><label>{kind === 'date' ? '截止日期' : '截止时刻'}<input aria-label={kind === 'date' ? '截止日期' : '截止时刻'} type={kind === 'date' ? 'date' : 'datetime-local'} value={value} onChange={event => { setValue(event.target.value); setConfirmTimezone(false) }} required /></label><label>截止时区<input aria-label="截止时区" value={timezone} onChange={event => { setTimezone(event.target.value); setConfirmTimezone(false) }} required /></label>{kind === 'instant' && <label>重复时刻<select aria-label="重复时刻选择" value={fold} onChange={event => { setFold(event.target.value); setConfirmTimezone(false) }}><option value="">未指定</option><option value="0">第一次</option><option value="1">第二次</option></select></label>}</>}
      <label>记录<select aria-label="安排记录状态" value={status} onChange={event => setStatus(event.target.value as typeof status)}><option value="active">有效</option><option value="archived">归档</option></select></label>
      <button className="pt-primary" disabled={moving || timezoneConfirmationRequired} type="submit">保存设置</button>
    </fieldset></form>
    {initial.placement_id && <section className="pt-move"><label>移到日期<input aria-label="移到日期" type="date" disabled={moving || state.posting} value={targetDate} onChange={event => setTargetDate(event.target.value)} /></label><button disabled={state.locked || moving || form !== original.current || targetDate === state.query.date || entry?.availability !== 'available'} onClick={() => void move()}>移动安排</button></section>}
    <button className="pt-history-toggle" aria-expanded={historyOpen} onClick={() => setHistoryOpen(value => !value)}><History size={14} />修改记录</button>
    {historyOpen && <section className="pt-history" aria-label="安排修改记录">{history?.map((record, index) => <div key={String(record.revision ?? index)}><strong>修订 {String(record.revision ?? '—')}</strong><time>{String(record.updated_at ?? '')}</time></div>)}{history && !history.length && <span>暂无修改记录</span>}{historyAfter !== null && <button disabled={historyLoading} onClick={() => void moreHistory()}>更多修改</button>}</section>}
  </aside>
}
