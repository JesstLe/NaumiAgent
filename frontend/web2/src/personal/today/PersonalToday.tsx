import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowDown, ArrowRight, ArrowUp, CalendarDays, CheckCheck, ChevronLeft, ChevronRight, Clock3, GripVertical, History, Inbox, ListTodo, Plus, RefreshCw, X } from 'lucide-react'
import type { TodayEntry, TodayQuery } from '@naumi/shared/api/personalToday'
import { artifactNavigationEvent, requestArtifactNavigation, type ArtifactNavigation } from '../library/navigation'
import { itemStatusNames } from '../items/PersonalItems'
import { TodayEditor } from './TodayEditor'
import { TodaySourcePicker } from './TodaySourcePicker'
import { todayDate, todayRequestId, writeTodayLocation } from './todayLocation'
import { todayError, usePersonalToday, type PersonalTodayApi } from './usePersonalToday'
import { Followups, FollowupRuleLauncher, readFollowupLocation } from '../followups/Followups'
import type { FollowupApi } from '../followups/useFollowups'
import type { FollowupRuleSeed } from '../followups/FollowupRuleEditor'
import './personal-today.css'

const time = (value: string | null | undefined, timezone: string) => {
  if (!value) return '—'
  try { return new Intl.DateTimeFormat('zh-CN', { timeZone: timezone, month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(value)) } catch { return '时间待核对' }
}
const attention = new Set(['unknown', 'blocked', 'error', 'timeout', 'max_turns'])
const priorityNames = { high: '高', normal: '普通', low: '低' }

export function PersonalToday({ api, namespace, connected, onClose, onOpenItem, onOpenResult, onOpenRoutine, onOpenAction, initialFollowupId }: {
  api: PersonalTodayApi & Partial<FollowupApi>; namespace: string; connected: boolean; onClose: () => void
  onOpenItem: (itemId: string) => void; onOpenResult: (occurrenceId: string) => void; onOpenRoutine: (routineId: string) => void; onOpenAction?: (actionId: string) => void; initialFollowupId?: string | null
}) {
  const state = usePersonalToday({ api, namespace, connected })
  const hasFollowups = typeof api.getPersonalFollowupContext === 'function' && typeof api.executePersonalFollowupOperation === 'function'
  const [followupsOpen, setFollowupsOpen] = useState(() => new URLSearchParams(location.search).get('today_view') === 'followups' || Boolean(initialFollowupId))
  const [followupSeed, setFollowupSeed] = useState<FollowupRuleSeed | null>(null)
  const followupNavigation = useRef(namespace); followupNavigation.current = namespace
  useEffect(() => () => { followupNavigation.current = '' }, [namespace])
  useEffect(() => { if (initialFollowupId) setFollowupsOpen(true) }, [initialFollowupId])
  const switchView = (followups: boolean) => requestArtifactNavigation(() => {
    setEditor(null); setPicker(false); setFollowupSeed(null); setFollowupsOpen(followups)
    const url = new URL(location.href); if (followups) url.searchParams.set('today_view', 'followups'); else { url.searchParams.delete('today_view'); url.searchParams.delete('followup_id') }; window.history.replaceState({}, '', url)
  })
  const [picker, setPicker] = useState(false), [editor, setEditor] = useState<TodayEntry | null>(null)
  const [editorVersion, setEditorVersion] = useState(0)
  const [editorDirty, setEditorDirty] = useState(false), [order, setOrder] = useState<string[] | null>(null)
  const [leave, setLeave] = useState<ArtifactNavigation | null>(null), [notice, setNotice] = useState('')
  const [zone, setZone] = useState(state.query.timezone), [history, setHistory] = useState<Record<string, unknown>[] | null>(null)
  const [historyAfter, setHistoryAfter] = useState<number | null>(null), [historyLoading, setHistoryLoading] = useState(false)
  const historyIdentity = useRef('')
  historyIdentity.current = `${namespace}:${state.view?.day.id || ''}`
  const [historyOpen, setHistoryOpen] = useState(false)
  const dragged = useRef<string | null>(null)
  const dirty = editorDirty || Boolean(order)
  useEffect(() => {
    const guard = (event: Event) => { if (dirty) { event.preventDefault(); setLeave((event as CustomEvent<ArtifactNavigation>).detail) } }
    const unload = (event: BeforeUnloadEvent) => { if (dirty) { event.preventDefault(); event.returnValue = '' } }
    window.addEventListener(artifactNavigationEvent, guard); window.addEventListener('beforeunload', unload)
    return () => { window.removeEventListener(artifactNavigationEvent, guard); window.removeEventListener('beforeunload', unload) }
  }, [dirty])
  useEffect(() => { setZone(state.query.timezone) }, [state.query.timezone])
  useEffect(() => {
    if (!historyOpen || !state.view) return
    const controller = new AbortController(); setHistory(null); setHistoryAfter(null)
    void api.getPersonalTodayHistory('day', state.view.day.id, { limit: 20 }, controller.signal).then(page => { if (!controller.signal.aborted) { setHistory(page.history); setHistoryAfter(page.next_after_revision ?? null) } })
      .catch(cause => { if (!controller.signal.aborted) setNotice(todayError(cause)) })
    return () => controller.abort()
  }, [api, namespace, historyOpen, state.view?.day.id, state.view?.day.revision])
  const moreHistory = async () => {
    if (historyAfter === null || historyLoading || !state.view) return
    const key = historyIdentity.current
    setHistoryLoading(true)
    try {
      const page = await api.getPersonalTodayHistory('day', state.view.day.id, { limit: 20, after_revision: historyAfter })
      if (historyIdentity.current !== key) return
      setHistory(previous => [...(previous || []), ...page.history.filter(record => !previous?.some(old => old.revision === record.revision))])
      setHistoryAfter(page.next_after_revision ?? null)
    } catch (cause) { if (historyIdentity.current === key) { setHistory(null); setNotice(todayError(cause)) } }
    finally { if (historyIdentity.current === key) setHistoryLoading(false) }
  }
  const navigate = (proceed: () => void) => requestArtifactNavigation(proceed)
  const changeQuery = (query: TodayQuery) => navigate(() => { setEditor(null); setOrder(null); setEditorDirty(false); state.changeQuery(query); writeTodayLocation(query) })
  const changeDate = (offset: number) => {
    const date = new Date(`${state.query.date}T12:00:00Z`); date.setUTCDate(date.getUTCDate() + offset)
    changeQuery({ ...state.query, date: date.toISOString().slice(0, 10) })
  }
  const included = useMemo(() => state.view?.day.placements.filter(value => value.disposition === 'include').map(value => value.id) || [], [state.view])
  const arrange = (from: string, target: string) => {
    const ids = [...(order || included)], source = ids.indexOf(from), destination = ids.indexOf(target)
    if (source < 0 || destination < 0 || source === destination) return
    ids.splice(source, 1); ids.splice(destination, 0, from); setOrder(ids); setNotice('顺序尚未保存')
  }
  const arranged = [...(state.view?.entries || [])].filter(entry => entry.planned && entry.disposition === 'include')
    .sort((a, b) => (order || included).indexOf(a.placement_id || '') - (order || included).indexOf(b.placement_id || ''))
  const pending = state.view?.entries.filter(entry => entry.disposition !== 'exclude' && entry.metadata_status !== 'archived'
    && (!entry.planned || attention.has(entry.execution_status || '') || entry.deadline?.status === 'timezone_data_changed')) || []
  const place = (entry: TodayEntry) => state.view ? state.submit({ operation: 'place', body: { request_id: todayRequestId(), date: state.query.date, timezone: state.query.timezone, planner_id: state.query.planner_id,
    expected_day_revision: state.view.day.revision, expected_metadata_revision: entry.metadata_revision, source_ref: entry.item_ref } }) : Promise.resolve(false)
  const card = (entry: TodayEntry, sortable = false) => {
    const available = entry.availability === 'available', title = available ? entry.title : entry.availability === 'stale' ? '事项已更新' : '来源暂不可用'
    const status = entry.execution_status || entry.item_status || ''
    const index = (order || included).indexOf(entry.placement_id || '')
    return <article key={entry.metadata_id} className={`pt-card ${entry.availability}${attention.has(status) || entry.deadline?.status === 'timezone_data_changed' ? ' needs-attention' : ''}`}
      onDragOver={event => { if (sortable && dragged.current) event.preventDefault() }} onDrop={event => { event.preventDefault(); if (sortable && entry.placement_id && dragged.current) arrange(dragged.current, entry.placement_id); dragged.current = null }}>
      <div className="pt-card-top">{sortable && entry.placement_id && <button className="pt-handle" aria-label={`排序${title}`} title="方向键调整顺序" draggable={!state.locked}
        onDragStart={event => { dragged.current = entry.placement_id; event.dataTransfer.setData('text/plain', entry.placement_id!); event.dataTransfer.effectAllowed = 'move' }} onDragEnd={() => { dragged.current = null }}
        onKeyDown={event => { if (['ArrowUp', 'ArrowDown'].includes(event.key)) { event.preventDefault(); const target = (order || included)[index + (event.key === 'ArrowUp' ? -1 : 1)]; if (target && !state.locked) arrange(entry.placement_id!, target) } }} disabled={state.locked}><GripVertical size={17} /></button>}
        <input type="checkbox" aria-label={`人工勾选${title}`} checked={entry.human_check?.checked === true} disabled={state.locked || !available || Boolean(order) || editorDirty} onChange={event => {
          void state.submit({ operation: 'check', body: { request_id: todayRequestId(), metadata_id: entry.metadata_id, expected_revision: entry.metadata_revision, source_ref: entry.item_ref, checked: event.target.checked } })
        }} /><button className="pt-card-title" onClick={() => navigate(() => setEditor(entry))}>{title}</button><span className={`pt-priority pt-priority-${entry.priority}`}>{priorityNames[entry.priority]}</span></div>
      <div className="pt-card-facts">{available && <span>{entry.assignee?.name}</span>}{status && <span className={attention.has(status) ? 'pt-attention' : ''}>{itemStatusNames[status] || '状态待核对'}</span>}{entry.human_check?.checked && <span className="pt-manual">人工已勾选</span>}</div>
      {entry.deadline && <time className={entry.deadline.status === 'timezone_data_changed' ? 'pt-deadline-unconfirmed' : entry.deadline.status === 'overdue' || !entry.deadline.status && Date.parse(entry.deadline.boundary_at || entry.deadline.resolved_at || '') <= Date.now() ? 'pt-overdue' : ''}>{entry.deadline.status === 'timezone_data_changed' ? '原截止' : '截止'} {entry.deadline.kind === 'date' ? entry.deadline.local_date : entry.deadline.local_time.replace('T', ' ')} <span>{entry.deadline.timezone}</span>{entry.deadline.status === 'timezone_data_changed' && <strong>时区规则待确认</strong>}</time>}
      <div className="pt-card-actions"><button onClick={() => navigate(() => setEditor(entry))}>{entry.availability === 'stale' ? '核对版本' : '设置'}</button>
        {available && entry.deadline && hasFollowups && <button onClick={() => navigate(() => setFollowupSeed({ kind: 'deadline', item_id: entry.item_ref.item_id, actor_id: entry.item_ref.assignee_id }))}>跟进</button>}
        {available && <button onClick={() => navigate(() => onOpenItem(entry.item_ref.item_id))}>原事项<ArrowRight size={12} /></button>}
        {entry.report && available && <button onClick={() => navigate(() => onOpenResult(entry.report!.occurrence_id))}>报告</button>}
        {!entry.planned && available && <button disabled={state.locked || Boolean(order) || editorDirty} onClick={() => void place(entry)}>安排<Plus size={13} /></button>}
        {entry.placement_id && <button disabled={state.locked || Boolean(order) || editorDirty} onClick={() => { if (state.view) void state.submit({ operation: 'remove', body: { request_id: todayRequestId(), day_id: state.view.day.id, expected_revision: state.view.day.revision, placement_id: entry.placement_id! } }) }}>移出今日</button>}
        {sortable && <div className="pt-order-actions"><button aria-label={`上移${title}`} disabled={state.locked || index <= 0} onClick={() => arrange(entry.placement_id!, (order || included)[index - 1])}><ArrowUp size={13} /></button><button aria-label={`下移${title}`} disabled={state.locked || index < 0 || index >= included.length - 1} onClick={() => arrange(entry.placement_id!, (order || included)[index + 1])}><ArrowDown size={13} /></button></div>}
      </div>
    </article>
  }
  return <section className="pt-workspace" aria-label="今日安排"><header className="pt-heading"><div className="pt-title"><CalendarDays size={25} /><h1>今日</h1></div><div><button aria-label="刷新今日安排" disabled={state.loading || !connected} onClick={() => navigate(() => { state.refresh(); setOrder(null) })}><RefreshCw size={16} className={state.loading ? 'pa-spin' : ''} /></button><button aria-label="关闭今日安排" onClick={() => navigate(onClose)}><X size={19} /></button></div></header>
    {hasFollowups && <div className="pt-followup-tabs" role="tablist" aria-label="今日视图"><button role="tab" aria-selected={!followupsOpen} onClick={() => switchView(false)}>安排</button><button role="tab" aria-selected={followupsOpen} onClick={() => switchView(true)}>跟进</button></div>}
    {followupsOpen && hasFollowups ? <Followups api={api as FollowupApi} namespace={namespace} connected={connected} initialProposalId={initialFollowupId || readFollowupLocation()} defaultDate={state.query.date} defaultTimezone={state.query.timezone} onOpenAction={onOpenAction} onAccepted={state.refresh} onOpenToday={id => {
      void api.getPersonalTodayDay(id).then(value => { if (followupNavigation.current !== namespace) return; navigate(() => { state.changeQuery({ date: value.day.date, timezone: value.day.timezone, planner_id: value.day.planner_id }); writeTodayLocation({ date: value.day.date, timezone: value.day.timezone, planner_id: value.day.planner_id }); setFollowupsOpen(false); const url = new URL(location.href); url.searchParams.delete('today_view'); url.searchParams.delete('followup_id'); window.history.replaceState({}, '', url) }) }).catch(cause => { if (followupNavigation.current === namespace) setNotice(todayError(cause)) })
    }} /> : <>
    <div className="pt-toolbar"><div className="pt-date"><button aria-label="前一天" onClick={() => changeDate(-1)}><ChevronLeft size={17} /></button><input aria-label="安排日期" type="date" value={state.query.date} onChange={event => { if (event.target.value) changeQuery({ ...state.query, date: event.target.value }) }} /><button aria-label="后一天" onClick={() => changeDate(1)}><ChevronRight size={17} /></button><button onClick={() => changeQuery({ ...state.query, date: todayDate(new Date(), state.query.timezone) })}>今天</button></div>
      <label className="pt-zone"><Clock3 size={14} /><input aria-label="安排时区" list="pt-timezones" value={zone} onChange={event => setZone(event.target.value)} /><datalist id="pt-timezones">{['Asia/Shanghai', 'Asia/Tokyo', 'America/New_York', 'Europe/London', 'UTC'].map(value => <option key={value} value={value} />)}</datalist>{zone !== state.query.timezone && <button onClick={() => { try { new Intl.DateTimeFormat('zh-CN', { timeZone: zone }).format(); changeQuery({ ...state.query, timezone: zone }) } catch { setNotice('请选择有效的 IANA 时区') } }}>切换</button>}</label>
      <select aria-label="规划 Agent" value={state.query.planner_id || ''} disabled={!state.context} onChange={event => changeQuery({ ...state.query, planner_id: event.target.value })}>{state.context?.planners.map(planner => <option key={planner.id} value={planner.id}>{planner.name}</option>)}</select>
      <div className="pt-toolbar-end"><button aria-expanded={historyOpen} onClick={() => setHistoryOpen(value => !value)}><History size={15} />历史</button><button className="pt-primary" disabled={state.locked} onClick={() => navigate(() => setPicker(true))}><Plus size={15} />加入安排</button></div>
    </div>
    {state.pending && <div className="pt-pending" role="status"><strong>原请求待核对</strong><code>{state.pending.request_id}</code><button disabled={state.posting || !connected} onClick={() => void state.reconcile()}>核对原请求</button></div>}
    {(state.error || state.identityError) && <div className="pt-alert" role="alert">{state.identityError || state.error}</div>}
    {notice && <div className="pt-notice" role="status">{notice}<button aria-label="关闭提示" onClick={() => setNotice('')}><X size={14} /></button></div>}
    {!connected && <div className="pt-empty" role="status">连接本地服务后查看今日安排</div>}
    {historyOpen && <section className="pt-day-history" aria-label="日计划历史">{history?.map((record, index) => <div key={String(record.revision ?? index)}><strong>修订 {String(record.revision ?? '—')}</strong><time>{String(record.updated_at ?? '')}</time></div>)}{history && !history.length && <span>尚无日计划修订</span>}{historyAfter !== null && <button disabled={historyLoading} onClick={() => void moreHistory()}>更多日计划修订</button>}</section>}
    {state.loading && !state.view && <div className="pt-loading" role="status">正在核对今日安排…</div>}
    {state.view && <><div className="pt-board"><section className="pt-column" aria-label="待处理"><header><ListTodo size={17} /><h2>待处理</h2><span>{pending.length}</span></header><div className="pt-cards">{pending.map(entry => card(entry))}{!pending.length && <div className="pt-empty">暂无待处理事项</div>}</div></section>
      <section className="pt-column pt-planned" aria-label="安排"><header><CalendarDays size={17} /><h2>安排</h2><span>{arranged.length}</span>{(order || state.view.projection_unavailable) && <button className="pt-primary" disabled={state.locked || editorDirty} onClick={() => {
        if (state.view) void state.submit({ operation: 'reorder', body: { request_id: todayRequestId(), day_id: state.view.day.id, expected_revision: state.view.day.revision, placement_ids: order || included } }).then(saved => { if (saved) { setOrder(null); setNotice('') } })
      }}>{state.view.projection_unavailable ? '确认当前顺序' : '保存顺序'}</button>}</header><div className="pt-cards">{arranged.map(entry => card(entry, true))}{!arranged.length && <button className="pt-add-empty" disabled={state.locked} onClick={() => setPicker(true)}><Plus size={20} />加入事项</button>}
        {state.view.routines.map(routine => <button className="pt-routine" key={routine.id} disabled={routine.availability !== 'available'} onClick={() => navigate(() => onOpenRoutine(routine.id))}><Clock3 size={16} /><div><strong>{routine.availability === 'available' ? routine.title : '计划来源暂不可用'}</strong><time>{time(routine.next_due_at, state.query.timezone)}</time>{routine.attention_code && <span className="pt-attention">需要核对</span>}</div><ArrowRight size={15} /></button>)}</div></section>
      <section className="pt-column" aria-label="已交付"><header><Inbox size={17} /><h2>已交付</h2><span>{state.view.sources.deliveries.error ? '待核对' : `${state.view.deliveries.length}${state.view.sources.deliveries.partial ? '+' : ''}`}</span></header><div className="pt-cards">{state.view.deliveries.map(delivery => <button className="pt-delivery" key={delivery.delivery_id} onClick={() => navigate(() => onOpenResult(delivery.occurrence_id))}><div className="pt-delivery-icon"><CheckCheck size={21} /></div><div><strong>{delivery.title || '原交付'}</strong><time>{time(delivery.delivered_at, state.query.timezone)}</time><span className={delivery.read_receipt ? '' : 'pt-unread'}>{delivery.read_receipt ? '已读' : '未读'}</span></div><ArrowRight size={15} /></button>)}{!state.view.deliveries.length && <div className="pt-empty">{state.view.sources.deliveries.error ? '交付记录待核对' : state.view.sources.deliveries.partial ? '交付记录尚未读完' : '暂无交付'}</div>}</div></section></div>
      <div className="pt-source-states" aria-label="来源读取状态">{([['items', '事项'], ['routines', '周期计划'], ['deliveries', '交付']] as const).map(([kind, name]) => {
        const source = state.view!.sources[kind]
        return source.error || source.partial ? <div key={kind} className={source.error ? 'pt-alert' : 'pt-source-partial'} role={source.error ? 'alert' : 'status'} aria-label={`${name}来源状态`}><span>{name}来源{source.error ? '暂不可用' : '尚未读完'}</span>{source.error && <code>{source.error}</code>}</div> : null
      })}</div>
      {state.view.next_cursor && <footer className="pt-more"><button disabled={state.loading} onClick={() => void state.more()}>加载更多</button></footer>}
    </>}
    {picker && state.context && <TodaySourcePicker api={api} workspaceKey={state.context.workspace_key} namespace={namespace} included={state.view?.entries.filter(entry => entry.planned).map(entry => entry.metadata_id) || []} locked={state.locked} onClose={() => setPicker(false)} onSelect={place} />}
    {editor && <TodayEditor key={`${namespace}:${editor.metadata_id}:${editorVersion}`} api={api} namespace={namespace} state={state} initial={editor} entry={state.view?.entries.find(entry => entry.metadata_id === editor.metadata_id) || null} onDirty={setEditorDirty} onClose={() => setEditor(null)} />}
    </>}
    {followupSeed && hasFollowups && <FollowupRuleLauncher api={api as FollowupApi} namespace={namespace} connected={connected} seed={followupSeed} onClose={() => setFollowupSeed(null)} />}
    {leave && <div className="pt-overlay"><section className="pt-leave" role="alertdialog" aria-modal="true" aria-label="保留未保存安排"><h2>保留未保存的修改？</h2><div><button onClick={() => { leave.cancel?.(); setLeave(null) }}>继续编辑</button><button className="pt-danger" onClick={() => { const next = leave; setLeave(null); setEditorDirty(false); setOrder(null); setEditor(null); setEditorVersion(value => value + 1); next.proceed() }}>放弃修改并继续</button></div></section></div>}
  </section>
}
