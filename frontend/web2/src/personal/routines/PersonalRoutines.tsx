import { useEffect, useState } from 'react'
import { CalendarClock, ChevronRight, History, Loader2, Pause, Pencil, Play, Plus, RefreshCw, Search, Square, X } from 'lucide-react'
import type { PersonalAgent } from '@naumi/shared/api/WorkbenchRuntimeClient'
import type { PersonalArtifactRef } from '@naumi/shared/api/personalArtifacts'
import type { PersonalRoutine, PersonalRoutineAction, PersonalRoutineDefinition, PersonalRoutineOccurrence, RoutineStatus } from '@naumi/shared/api/personalRoutines'
import { ExecutionCard, itemStatusNames } from '../items/PersonalItems'
import { requestArtifactNavigation } from '../library/navigation'
import { RoutineEditor, routineDate, routineMoney } from './RoutineEditor'
import { routineError, routineRequestId, usePersonalRoutines, validateRoutine, validateRoutineOccurrence, type PersonalRoutinesApi, type PersonalRoutinesState } from './usePersonalRoutines'
import '../items/items.css'
import './routines.css'

const statuses: Record<RoutineStatus, string> = { active: '已启用', paused: '已暂停', cancelled: '已取消', closed: '已结束' }
const occurrenceStatuses: Record<PersonalRoutineOccurrence['status'], string> = { reserved: '已预留', linked: '已创建事项', skipped: '已跳过', blocked: '受阻', settled: '已结算', unknown: '结果待核对' }
type Editor = { definition?: PersonalRoutineDefinition; id?: string; revision?: number }
const decisionText = (value: PersonalRoutineOccurrence['decision']) => typeof value === 'string' ? value : value ? JSON.stringify(value, null, 2) : ''

export function PersonalRoutines({ api, namespace, agents, connected, onClose, onOpenArtifact }: {
  api: PersonalRoutinesApi; namespace: string; agents: PersonalAgent[]; connected: boolean; onClose: () => void
  onOpenArtifact: (actor: string, ref: PersonalArtifactRef) => void
}) {
  const state = usePersonalRoutines({ api, namespace, connected })
  const [selected, setSelected] = useState<string | null>(null), [editor, setEditor] = useState<Editor | null>(null), [abandon, setAbandon] = useState(false)
  useEffect(() => {
    if (state.preview && !editor) setEditor({ definition: state.preview.definition, ...(state.preview.base_routine_id ? { id: state.preview.base_routine_id, revision: state.preview.base_revision! } : {}) })
  }, [state.preview, editor])
  useEffect(() => {
    if (state.accepted) { setSelected(state.accepted.id); setEditor(null) }
  }, [state.accepted])
  const closeEditor = () => { state.clearPreview(); setEditor(null) }
  return <section className="pr-workspace" aria-label="周期计划">
    <header className="pr-heading"><h1><CalendarClock size={23} />周期计划</h1><div><button aria-label="刷新周期计划" disabled={state.loading || !connected} onClick={state.refresh}><RefreshCw size={16} className={state.loading ? 'pa-spin' : ''} /></button><button aria-label="关闭周期计划" onClick={() => requestArtifactNavigation(onClose)}><X size={19} /></button></div></header>
    {(state.pending || state.identityError) && <div className="pr-pending" role="status"><div><strong>原请求待核对</strong>{state.pending && <code>{state.pending.request_id}</code>}{state.identityError && <span>{state.identityError}</span>}</div><div><button disabled={!state.pending || state.posting || !connected} onClick={() => void state.reconcile()}>核对原请求</button><button disabled={state.posting} onClick={() => setAbandon(true)}>停止核对</button></div></div>}
    {abandon && <div className="pr-confirm" role="alertdialog" aria-label="停止核对计划请求"><strong>停止核对原请求？</strong><p>这不会撤销已受理的计划或执行。</p><div><button onClick={() => setAbandon(false)}>继续核对</button><button onClick={() => { state.discard(); setAbandon(false) }}>确认停止核对</button></div></div>}
    {state.error && <div role="alert" className="pr-error">{state.error}</div>}
    {!connected && <div role="status" className="pr-empty">连接本地服务后查看周期计划</div>}
    {editor && state.context ? <RoutineEditor key={editor.id || 'create'} api={api} agents={agents} state={state} definition={editor.definition} routineId={editor.id} revision={editor.revision} onClose={closeEditor} onSaved={id => { setSelected(id); closeEditor() }} />
      : <><div className="pr-toolbar"><div className="pr-filters" role="group" aria-label="计划状态">{([['', '全部'], ['active', '已启用'], ['paused', '已暂停'], ['cancelled', '已取消'], ['closed', '已结束']] as const).map(([status, name]) => <button key={status} aria-pressed={state.status === status} onClick={() => { state.setStatus(status); setSelected(null) }}>{name}</button>)}</div><button className="pr-primary" disabled={state.locked || !state.context} onClick={() => { state.clearPreview(); setEditor({}) }}><Plus size={16} />新建计划</button></div>
        <div className="pr-body"><aside className="pr-list-pane"><label className="pr-search"><Search size={16} /><input aria-label="搜索周期计划" value={state.query} onChange={event => state.setQuery(event.target.value)} /></label>{state.loading && <div role="status" className="pr-loading"><Loader2 size={15} className="pa-spin" />正在加载…</div>}<div className="pr-list">{state.routines.map(routine => <button key={routine.id} className={`pr-list-card${selected === routine.id ? ' selected' : ''}`} onClick={() => setSelected(routine.id)}><div><strong>{routine.definition.title}</strong><ChevronRight size={15} /></div><div><span>{routine.definition.assignee.name}</span><span className={`pr-status pr-status-${routine.status}`}>{statuses[routine.status]}</span></div><small>{routineDate(routine.next_due_at, routine.definition.schedule.timezone)}</small>{routine.attention_code && <span className="pr-attention">需要核对</span>}</button>)}{!state.loading && !state.routines.length && <div className="pr-empty">暂无周期计划</div>}</div><div className="pr-pagination"><button disabled={!state.after || state.loading} onClick={() => state.setAfter('')}>首页</button><button disabled={!state.nextAfter || state.loading} onClick={() => { state.setAfter(state.nextAfter!); setSelected(null) }}>下一页</button></div></aside>
          <div className="pr-detail-pane">{selected ? <RoutineDetail key={selected} api={api} id={selected} state={state} onEdit={routine => { state.clearPreview(); setEditor({ id: routine.id, revision: routine.revision, definition: routine.definition }) }} onOpenArtifact={onOpenArtifact} /> : <div className="pr-detail-empty"><CalendarClock size={32} /><span>选择计划</span></div>}</div></div></>}
  </section>
}

function RoutineDetail({ api, id, state, onEdit, onOpenArtifact }: {
  api: PersonalRoutinesApi; id: string; state: PersonalRoutinesState; onEdit: (routine: PersonalRoutine) => void
  onOpenArtifact: (actor: string, ref: PersonalArtifactRef) => void
}) {
  const [routine, setRoutine] = useState<PersonalRoutine | null>(null), [error, setError] = useState('')
  const [occurrences, setOccurrences] = useState<PersonalRoutineOccurrence[]>([]), [nextAfter, setNextAfter] = useState<string | null>(null)
  const [history, setHistory] = useState<PersonalRoutine[]>([]), [historyAfter, setHistoryAfter] = useState<number | null>(null), [historyOpen, setHistoryOpen] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false), [confirm, setConfirm] = useState<PersonalRoutineAction | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    void Promise.all([api.personalRoutine(id, controller.signal), api.personalRoutineOccurrences(id, { limit: 20 }, controller.signal)])
      .then(([value, page]) => {
        if (controller.signal.aborted) return
        const valid = validateRoutine(value)
        if (valid.id !== id) throw new Error('计划身份不一致')
        const records = page.occurrences.map(validateRoutineOccurrence)
        if (records.some(record => record.routine_id !== id)) throw new Error('执行历史与原计划不一致')
        setRoutine(valid); setOccurrences(records); setNextAfter(page.next_after); setError('')
      }).catch(cause => { if (!controller.signal.aborted) setError(routineError(cause)) })
    return () => controller.abort()
  }, [api, id, state.refreshToken])
  useEffect(() => {
    if (!historyOpen) return
    const controller = new AbortController()
    void api.personalRoutineHistory(id, { limit: 20 }, controller.signal).then(page => {
      if (controller.signal.aborted) return
      const records = page.history.map(validateRoutine)
      if (records.some(value => value.id !== id)) throw new Error('修订记录身份不一致')
      setHistory(records); setHistoryAfter(page.next_after)
    }).catch(cause => { if (!controller.signal.aborted) setError(routineError(cause)) })
    return () => controller.abort()
  }, [api, id, historyOpen, state.refreshToken])
  const more = async (kind: 'occurrences' | 'history') => {
    setLoadingMore(true)
    try {
      if (kind === 'occurrences' && nextAfter) {
        const page = await api.personalRoutineOccurrences(id, { limit: 20, after: nextAfter })
        const values = page.occurrences.map(validateRoutineOccurrence)
        if (values.some(value => value.routine_id !== id)) throw new Error('执行历史与原计划不一致')
        setOccurrences(previous => [...previous, ...values.filter(value => !previous.some(existing => existing.id === value.id))]); setNextAfter(page.next_after)
      } else if (kind === 'history' && historyAfter !== null) {
        const page = await api.personalRoutineHistory(id, { limit: 20, after: historyAfter })
        const values = page.history.map(validateRoutine)
        if (values.some(value => value.id !== id)) throw new Error('修订记录身份不一致')
        setHistory(previous => [...previous, ...values.filter(value => !previous.some(existing => existing.revision === value.revision))]); setHistoryAfter(page.next_after)
      }
    } catch (cause) { setError(routineError(cause)) }
    finally { setLoadingMore(false) }
  }
  if (!routine) return <div className={error ? 'pr-error' : 'pr-empty'} role={error ? 'alert' : 'status'}>{error || '正在加载计划…'}</div>
  const definition = routine.definition, budget = routine.budget
  const control = async (action: PersonalRoutineAction) => {
    const result = await state.submit({ operation: action, routine_id: routine.id, body: { request_id: routineRequestId(), expected_revision: routine.revision } })
    if (result) { if ('budget' in result) setRoutine(result); setConfirm(null) }
  }
  const confirmText: Record<PersonalRoutineAction, string> = { pause: '暂停后续触发', resume: '恢复后续触发', cancel: '取消后续计划', 'extra-run': '追加一次独立执行' }
  return <article className="pr-detail"><header><div className="pr-detail-meta"><span className={`pr-status pr-status-${routine.status}`}>{statuses[routine.status]}</span><span>修订 {routine.revision}</span></div><h2>{definition.title}</h2><div className="pr-detail-meta"><span>{definition.assignee.name} · v{definition.assignee.version}</span><span>{definition.schedule.timezone}</span></div></header>
    <div className="pr-detail-actions">{['active', 'paused'].includes(routine.status) && <><button disabled={state.locked} onClick={() => onEdit(routine)}><Pencil size={14} />修改计划</button>{routine.status === 'active' ? <button disabled={state.locked} onClick={() => setConfirm('pause')}><Pause size={14} />暂停后续</button> : <button disabled={state.locked} onClick={() => setConfirm('resume')}><Play size={14} />恢复</button>}<button disabled={state.locked} onClick={() => setConfirm('extra-run')}><Plus size={14} />额外执行</button><button disabled={state.locked} onClick={() => setConfirm('cancel')}><Square size={13} />取消计划</button></>}<button aria-expanded={historyOpen} onClick={() => setHistoryOpen(value => !value)}><History size={14} />修订记录</button></div>
    {confirm && <div className="pr-confirm" role="alertdialog" aria-label="确认周期计划操作"><strong>{confirmText[confirm]}《{definition.title}》？</strong><p>{confirm === 'pause' || confirm === 'cancel' ? '已受理执行保持原状态，不会因此停止。' : confirm === 'extra-run' ? '新增独立执行并占用次数与额度，不重放原执行。' : '按当前恢复策略处理错过的触发。'}</p><div><button onClick={() => setConfirm(null)}>返回</button><button className="pr-primary" disabled={state.locked} onClick={() => void control(confirm)}>确认{confirmText[confirm]}</button></div></div>}
    {error && <div className="pr-error" role="alert">{error}</div>}{routine.attention_code && <div className="pr-attention" role="status">需要核对 · <code>{routine.attention_code}</code></div>}
    <section><h3>计划</h3><dl><dt>已创建</dt><dd>{routineDate(routine.created_at)}</dd><dt>规则</dt><dd>{definition.schedule.kind === 'cron' ? <code>{definition.schedule.expression}</code> : definition.schedule.kind === 'trading_day' ? `${definition.schedule.calendar_id} · ${definition.schedule.at === 'open' ? '开市' : definition.schedule.at === 'close' ? '收市' : definition.schedule.local_time}` : definition.schedule.local_time}</dd><dt>下次触发</dt><dd>{routineDate(routine.next_due_at, definition.schedule.timezone)}</dd><dt>执行要求</dt><dd className="pr-instructions">{definition.instructions}</dd></dl></section>
    <section aria-label="计划次数与费用"><h3>次数与估算额度</h3><div className="pr-budget-grid"><div><span>已开始</span><strong>{budget.started_executions}<small> / {definition.limits.max_executions}</small></strong></div><div><span>已预留次数</span><strong>{budget.reserved_slots}</strong></div><div><span>剩余次数</span><strong>{budget.remaining_slots}</strong></div><div><span>已结算估算</span><strong>{routineMoney(budget.settled_cost_microusd)}</strong></div><div><span>已预留额度</span><strong>{routineMoney(budget.reserved_cost_microusd)}</strong></div><div><span>剩余额度</span><strong>{routineMoney(budget.remaining_cost_microusd)}</strong></div></div>{budget.unknown_count > 0 && <div className="pr-attention" role="status">{budget.unknown_count} 次费用待核对 · 原预留额度保留</div>}<p className="pr-accounting">本地单价估算上限，非服务商最终账单。</p></section>
    {historyOpen && <section className="pr-history" aria-label="计划修订记录"><h3>修订记录</h3>{history.map(value => <div key={value.revision}><strong>修订 {value.revision}</strong><span>{statuses[value.status]}</span><time>{routineDate(value.updated_at)}</time><details><summary>原定义</summary><pre>{JSON.stringify(value.definition, null, 2)}</pre></details></div>)}{historyAfter !== null && <button disabled={loadingMore} onClick={() => void more('history')}>更多修订</button>}</section>}
    <section className="pr-occurrences" aria-label="周期执行记录"><h3>执行记录</h3>{occurrences.map(value => <OccurrenceCard key={value.id} occurrence={value} api={api} onOpenArtifact={onOpenArtifact} />)}{!occurrences.length && <div className="pr-empty">尚未触发</div>}{nextAfter && <button disabled={loadingMore} onClick={() => void more('occurrences')}>更多执行</button>}</section>
    <details className="pr-identities"><summary>计划身份</summary><dl><dt>计划 ID</dt><dd><code>{routine.id}</code></dd><dt>执行批次</dt><dd><code>{routine.epoch}</code></dd><dt>定义摘要</dt><dd><code>{routine.definition_sha256}</code></dd></dl></details>
  </article>
}

function OccurrenceCard({ occurrence, api, onOpenArtifact }: { occurrence: PersonalRoutineOccurrence; api: PersonalRoutinesApi; onOpenArtifact: (actor: string, ref: PersonalArtifactRef) => void }) {
  const execution = occurrence.child?.occurrence, decision = decisionText(occurrence.decision)
  const delivered = Boolean(execution?.delivery_id && execution.delivery_sha256)
  return <article className="pr-occurrence"><header><time>{routineDate(occurrence.nominal_due_at, occurrence.child?.definition.schedule.timezone)}</time><span className={`pr-status pr-status-${occurrence.status}`}>{occurrenceStatuses[occurrence.status]}</span></header><dl><dt>已触发</dt><dd>{occurrence.local_time}{occurrence.fold === 1 ? ' · 第二次' : ''}</dd><dt>执行</dt><dd>{execution ? itemStatusNames[execution.status] || '状态待核对' : occurrence.item_id ? '原事项待核对' : '尚未受理'}</dd><dt>送达</dt><dd>{delivered ? '已送达收件箱' : '尚无送达回执'}</dd><dt>本次估算费用</dt><dd>{occurrence.status === 'unknown' ? '待核对' : routineMoney(occurrence.settled_cost_microusd)}</dd></dl>{decision && <pre className="pr-decision">{decision}</pre>}{execution && <div className="pi-workspace pr-child-execution"><ExecutionCard execution={execution} actor={occurrence.child!.definition.assignee.id} api={api} onOpenArtifact={onOpenArtifact} /></div>}<details><summary>执行身份</summary><dl><dt>执行 ID</dt><dd><code>{occurrence.id}</code></dd><dt>事项</dt><dd><code>{occurrence.item_id || '未创建'}</code></dd><dt>定义修订</dt><dd>{occurrence.definition_revision}</dd></dl></details></article>
}
