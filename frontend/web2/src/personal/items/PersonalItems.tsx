import { useEffect, useState } from 'react'
import { ArrowLeft, ArrowUpRight, CalendarClock, Check, CheckCheck, ChevronRight, FileText, History, Inbox, ListTodo, Loader2, Pause, Pencil, Play, Plus, RefreshCw, Search, Square, X } from 'lucide-react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { PersonalAgent, Run } from '@naumi/shared/api/WorkbenchRuntimeClient'
import type { PersonalArtifactRef, PersonalArtifactSnapshot } from '@naumi/shared/api/personalArtifacts'
import type { PersonalItem, PersonalItemAction, PersonalItemExecution, PersonalItemResult } from '@naumi/shared/api/personalItems'
import { requestArtifactNavigation } from '../library/navigation'
import { validateArtifact } from '../library/useArtifactLibrary'
import { ItemEditor } from './ItemEditor'
import { itemError, itemRequestId, usePersonalItems, validateExecution, validateItem, validateResult, type PersonalItemsApi, type PersonalItemsState } from './usePersonalItems'
import './items.css'

export const itemStatusNames: Record<string, string> = { active: '待执行', paused: '已暂停', cancelled: '已取消', closed: '已结束', prepared: '准备中', queued: '排队中', running: '执行中', completed: '已完成', error: '执行出错', timeout: '已超时', max_turns: '已达轮数上限', unknown: '结果未知', blocked: '受阻', skipped: '已跳过' }
export const knownTerminal = (status?: string) => Boolean(status && ['completed', 'error', 'timeout', 'max_turns', 'cancelled', 'skipped'].includes(status))
const date = (value?: string) => value ? new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)) : '—'
const executionError = (value: PersonalItemExecution['error']) => typeof value === 'string' ? value : value ? JSON.stringify(value, null, 2) : ''
function Status({ status }: { status: string }) { return <span className={`pi-status pi-status-${status}`}>{status === 'running' && <Loader2 size={12} className="pa-spin" />}{itemStatusNames[status] || status}</span> }

export function PersonalItems({ api, namespace, agents, connected, originSessionId, onClose, onOpenArtifact }: {
  api: PersonalItemsApi; namespace: string; agents: PersonalAgent[]; connected: boolean; originSessionId?: string
  onClose: () => void; onOpenArtifact: (actorId: string, ref: PersonalArtifactRef) => void
}) {
  const state = usePersonalItems({ api, namespace, connected })
  const [selection, setSelection] = useState<{ kind: 'item' | 'result'; id: string } | null>(null)
  const [editor, setEditor] = useState<PersonalItem | 'create' | null>(null), [abandon, setAbandon] = useState(false)
  const [detailToken, setDetailToken] = useState(0)
  const navigate = (proceed: () => void) => requestArtifactNavigation(proceed)
  const accepted = (value: unknown) => {
    if (value && typeof value === 'object' && 'item_id' in value && !('occurrence_id' in value)) {
      setSelection({ kind: 'item', id: String(value.item_id) }); setEditor(null); setDetailToken(n => n + 1)
    }
  }
  return <section className="pi-workspace" aria-label="事项">
    <header className="pi-heading"><h1><ListTodo size={23} />事项</h1><div><button aria-label="刷新事项" disabled={state.loading || !connected} onClick={() => { state.refresh(); setDetailToken(n => n + 1) }}><RefreshCw size={16} className={state.loading ? 'pa-spin' : ''} /></button><button aria-label="关闭事项" onClick={() => navigate(onClose)}><X size={19} /></button></div></header>
    {(state.pending || state.identityError) && <div className="pi-pending" role="status"><div><strong>原请求待核对</strong>{state.pending && <code>{state.pending.body.request_id}</code>}</div>{state.identityError && <span>{state.identityError}</span>}<div><button disabled={!state.pending || state.posting || !connected} onClick={() => { void state.retry().then(accepted) }}>原样重试</button><button disabled={state.posting} onClick={() => setAbandon(true)}>放弃重试</button><details><summary>请求记录</summary><pre>{JSON.stringify(state.pending, null, 2)}</pre></details></div></div>}
    {abandon && <div className="pi-confirm" role="alertdialog" aria-label="确认放弃事项请求重试"><strong>放弃原请求重试？</strong><p>这不会撤销已受理的事项、执行或已读记录。</p><div><button onClick={() => setAbandon(false)}>保留原请求</button><button onClick={() => { state.discard(); setAbandon(false) }}>确认放弃重试</button></div></div>}
    {state.error && <div className="pi-error" role="alert">{state.error}</div>}
    {!connected && <div className="pi-error" role="status">连接本地服务后查看事项</div>}
    {editor ? <ItemEditor key={editor === 'create' ? 'create' : `${editor.item_id}:${editor.revision}`} item={editor === 'create' ? undefined : editor} agents={agents} api={api} state={state}
      onClose={() => setEditor(null)} onSave={async body => {
        const value = editor === 'create'
          ? await state.submit({ operation: 'create', body: { ...body, request_id: itemRequestId(), ...(originSessionId ? { origin_session_id: originSessionId } : {}) } })
          : await state.submit({ operation: 'update', item_id: editor.item_id, body: { ...body, expected_revision: editor.revision, request_id: itemRequestId() } })
        if (value) accepted(value)
        return Boolean(value)
      }} /> : <>
      <div className="pi-tabs"><div role="tablist" aria-label="事项视图"><button role="tab" aria-selected={state.tab === 'items'} onClick={() => { state.setTab('items'); setSelection(null) }}><ListTodo size={16} />我的事项</button><button role="tab" aria-selected={state.tab === 'results'} onClick={() => { state.setTab('results'); setSelection(null) }}><Inbox size={16} />结果收件箱</button></div><button className="pi-primary" disabled={state.locked} onClick={() => setEditor('create')}><Plus size={16} />新建事项</button></div>
      <div className="pi-body"><div className="pi-list-pane">
        <div className="pi-list-toolbar"><div className="pi-segment" role="group" aria-label={state.tab === 'items' ? '事项状态' : '结果筛选'}>{state.tab === 'items'
          ? (['active', 'paused', ''] as const).map(value => <button key={value || 'all'} aria-pressed={state.filter === value} onClick={() => state.setFilter(value)}>{value === 'active' ? '进行中' : value === 'paused' ? '已暂停' : '全部'}</button>)
          : [true, false].map(value => <button key={String(value)} aria-pressed={state.unread === value} onClick={() => state.setUnread(value)}>{value ? '未读' : '全部'}</button>)}</div></div>
        {state.tab === 'items' && <label className="pi-search"><Search size={16} /><input aria-label="搜索事项" placeholder="搜索事项" value={state.query} onChange={event => state.setQuery(event.target.value)} /></label>}
        {state.loading && <div className="pi-list-loading" role="status"><Loader2 className="pa-spin" size={15} />正在加载…</div>}
        <div className="pi-list" aria-label={state.tab === 'items' ? '事项列表' : '结果列表'}>{state.tab === 'items' ? state.items.map(item => <button key={item.item_id} className={`pi-list-card${selection?.id === item.item_id ? ' selected' : ''}`} onClick={() => setSelection({ kind: 'item', id: item.item_id })}>
          <div><strong>{item.definition.title}</strong><ChevronRight size={15} /></div><div><span>{item.definition.assignee.name}</span><Status status={item.occurrence?.status || item.state} /></div><small><CalendarClock size={12} />{date(item.definition.schedule.due_at)}<span>{item.definition.tags.join(' · ')}</span></small></button>)
          : state.results.map(result => <button key={result.occurrence_id} className={`pi-list-card${selection?.id === result.occurrence_id ? ' selected' : ''}`} onClick={() => setSelection({ kind: 'result', id: result.occurrence_id })}><div><strong>{!result.read_receipt && <span className="pi-unread-dot" aria-label="未读" />}{result.title}</strong><ChevronRight size={15} /></div><div><span>{result.assignee_name}</span><Status status={result.status} /></div><small>{date(result.created_at)}{result.read_receipt && <CheckCheck size={13} />}</small></button>)}
          {!state.loading && !(state.tab === 'items' ? state.items.length : state.results.length) && <div className="pi-empty">{state.tab === 'items' ? '暂无事项' : state.unread ? '暂无未读结果' : '暂无结果'}</div>}
        </div><footer className="pi-pagination"><button disabled={!state.offset || state.loading} onClick={() => state.setOffset(Math.max(0, state.offset - 30))}>上一页</button><span>{Math.floor(state.offset / 30) + 1}</span><button disabled={state.nextOffset === null || state.loading} onClick={() => state.setOffset(state.nextOffset!)}>下一页</button></footer>
      </div><div className={`pi-detail-pane${selection ? ' has-selection' : ''}`}>{selection ? <><button className="pi-mobile-back" onClick={() => setSelection(null)}><ArrowLeft size={15} />返回列表</button>{selection.kind === 'item'
        ? <ItemDetail key={`item:${selection.id}`} api={api} itemId={selection.id} state={state} refreshToken={detailToken} onEdit={setEditor} onOpenArtifact={onOpenArtifact} />
        : <ResultDetail key={`result:${selection.id}`} api={api} occurrenceId={selection.id} state={state} refreshToken={detailToken} onOpenArtifact={onOpenArtifact} />}</> : <div className="pi-detail-empty"><ListTodo size={34} /><span>{state.tab === 'items' ? '选择事项' : '选择结果'}</span></div>}</div></div>
    </>}
  </section>
}

function ItemDetail({ api, itemId, state, refreshToken, onEdit, onOpenArtifact }: { api: PersonalItemsApi; itemId: string; state: PersonalItemsState; refreshToken: number; onEdit: (item: PersonalItem) => void; onOpenArtifact: (actor: string, ref: PersonalArtifactRef) => void }) {
  const [item, setItem] = useState<PersonalItem | null>(null), [error, setError] = useState('')
  const [history, setHistory] = useState<PersonalItem[] | null>(null), [occurrences, setOccurrences] = useState<PersonalItemExecution[] | null>(null)
  const [confirm, setConfirm] = useState<PersonalItemAction | null>(null), [historyOpen, setHistoryOpen] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    void api.personalItem(itemId, controller.signal).then(value => { if (!controller.signal.aborted) { const valid = validateItem(value); if (valid.item_id !== itemId) throw new Error('事项身份不一致'); setItem(valid); setError('') } }).catch(cause => { if (!controller.signal.aborted) setError(itemError(cause)) })
    return () => controller.abort()
  }, [api, itemId, state.refreshToken, refreshToken])
  useEffect(() => {
    if (!historyOpen) return
    const controller = new AbortController()
    void Promise.all([api.personalItemHistory(itemId, controller.signal), api.personalItemOccurrences(itemId, controller.signal)])
      .then(([snapshots, records]) => { if (!controller.signal.aborted) { setHistory(snapshots.history.map(validateItem)); setOccurrences(records.occurrences.map(validateExecution)); setError('') } })
      .catch(cause => { if (!controller.signal.aborted) setError(itemError(cause)) })
    return () => controller.abort()
  }, [api, itemId, historyOpen, state.refreshToken, refreshToken])
  if (!item) return <div className={error ? 'pi-error' : 'pi-empty'} role={error ? 'alert' : 'status'}>{error || '正在加载事项…'}</div>
  const execution = item.occurrence, terminal = knownTerminal(execution?.status) || (!execution && ['cancelled', 'closed'].includes(item.state)), prepared = Boolean(execution)
  const runAction = async (action: PersonalItemAction) => {
    const result = await state.submit({ operation: 'control', item_id: item.item_id, action, body: { expected_revision: item.revision, request_id: itemRequestId(), ...(action === 'restart' ? { schedule: { mode: 'now' as const, timezone: item.definition.schedule.timezone, misfire_policy: 'run_once' as const, grace_seconds: item.definition.schedule.grace_seconds } } : {}) } })
    if (result && 'definition' in result) setItem(validateItem(result as PersonalItem))
    setConfirm(null)
  }
  return <article className="pi-item-detail"><header><div><Status status={execution?.status || item.state} /><span className="pi-revision">修订 {item.revision}</span></div><h2>{item.definition.title}</h2><div className="pi-detail-meta"><span>{item.definition.assignee.name} · v{item.definition.assignee.version}</span><span>{date(item.definition.schedule.due_at)}</span><span>{item.definition.schedule.timezone}{item.definition.schedule.fold !== null && item.definition.schedule.fold !== undefined ? ` · fold ${item.definition.schedule.fold}` : ''}</span></div></header>
    <div className="pi-detail-actions">{!prepared && ['active', 'paused'].includes(item.state) && <button disabled={state.locked} onClick={() => onEdit(item)}><Pencil size={14} />编辑</button>}
      {item.state === 'active' && !terminal && execution?.status !== 'running' && execution?.status !== 'unknown' && <button disabled={state.locked} onClick={() => void runAction('pause')}><Pause size={14} />暂停</button>}
      {item.state === 'paused' && <button disabled={state.locked} onClick={() => void runAction('resume')}><Play size={14} />恢复</button>}
      {item.state === 'active' && !prepared && <button disabled={state.locked} onClick={() => setConfirm('run-now')}><Play size={14} />立即运行</button>}
      {['active', 'paused'].includes(item.state) && !terminal && <button disabled={state.locked} onClick={() => setConfirm('cancel')}><Square size={13} />取消事项</button>}
      {terminal && <button disabled={state.locked} onClick={() => setConfirm('restart')}><RefreshCw size={14} />再次执行</button>}
      <button aria-expanded={historyOpen} onClick={() => setHistoryOpen(value => !value)}><History size={14} />记录</button>
    </div>
    {confirm && <div className="pi-confirm" role="alertdialog" aria-label="确认事项操作"><strong>{confirm === 'cancel' ? '取消' : confirm === 'restart' ? '立即再次执行' : '立即运行'}《{item.definition.title}》？</strong>{confirm === 'restart' && <p>这将创建独立的新执行记录。</p>}<div><button onClick={() => setConfirm(null)}>返回</button><button className={confirm === 'cancel' ? 'pi-danger' : 'pi-primary'} disabled={state.locked} onClick={() => void runAction(confirm)}>{confirm === 'cancel' ? '确认取消' : confirm === 'restart' ? '确认再次执行' : '确认运行'}</button></div></div>}
    {error && <div role="alert" className="pi-error">{error}</div>}
    <section className="pi-instructions"><h3>执行要求</h3><div>{item.definition.instructions}</div></section>
    {!!item.definition.tags.length && <div className="pi-tags">{item.definition.tags.map((tag, index) => <span key={`${tag}:${index}`}>{tag}</span>)}</div>}
    {!!item.definition.inputs.artifact_refs.length && <section><h3>指定材料</h3><ReportCards refs={item.definition.inputs.artifact_refs} actor={item.definition.assignee.id} api={api} onOpenArtifact={onOpenArtifact} /></section>}
    {!!item.definition.inputs.directories.length && <section><h3>资料目录</h3>{item.definition.inputs.directories.map(value => <div className="pi-directory" key={value.path}><code>{value.path}</code><span>{value.pattern}</span></div>)}</section>}
    {execution && <ExecutionCard execution={execution} actor={item.definition.assignee.id} api={api} onOpenArtifact={onOpenArtifact} />}
    {historyOpen && <section className="pi-history"><h3>事项记录</h3>{history === null && <span role="status">正在加载记录…</span>}{history?.map(snapshot => <div key={snapshot.revision}><strong>修订 {snapshot.revision}</strong><Status status={snapshot.state} /><span>{date(snapshot.updated_at)}</span><details><summary>定义</summary><pre>{JSON.stringify(snapshot.definition, null, 2)}</pre></details></div>)}<h3>执行记录</h3>{occurrences?.map(record => <ExecutionCard key={record.occurrence_id} execution={record} actor={item.definition.assignee.id} api={api} onOpenArtifact={onOpenArtifact} />)}{occurrences?.length === 0 && <div className="pi-empty">暂无执行记录</div>}</section>}
  </article>
}

function ResultDetail({ api, occurrenceId, state, refreshToken, onOpenArtifact }: { api: PersonalItemsApi; occurrenceId: string; state: PersonalItemsState; refreshToken: number; onOpenArtifact: (actor: string, ref: PersonalArtifactRef) => void }) {
  const [result, setResult] = useState<PersonalItemResult | null>(null), [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    void api.personalItemResult(occurrenceId, controller.signal).then(value => { if (!controller.signal.aborted) { const valid = validateResult(value); if (valid.occurrence_id !== occurrenceId) throw new Error('结果身份不一致'); setResult(valid); setError('') } }).catch(cause => { if (!controller.signal.aborted) setError(itemError(cause)) })
    return () => controller.abort()
  }, [api, occurrenceId, state.refreshToken, refreshToken])
  if (!result) return <div className={error ? 'pi-error' : 'pi-empty'} role={error ? 'alert' : 'status'}>{error || '正在加载结果…'}</div>
  return <article className="pi-item-detail"><header><div><Status status={result.status} />{result.read_receipt ? <span className="pi-read"><CheckCheck size={14} />已读</span> : <span className="pi-read pi-unread">未读</span>}</div><h2>{result.title}</h2><div className="pi-detail-meta"><span>{result.assignee_name}</span><span>{date(result.created_at)}</span></div></header>
    <div className="pi-detail-actions">{!result.read_receipt && <button className="pi-primary" disabled={state.locked} onClick={() => { void state.submit({ operation: 'read', occurrence_id: result.occurrence_id, body: { request_id: itemRequestId(), delivery_id: result.delivery_id!, expected_delivery_sha256: result.delivery_sha256! } }) }}><Check size={15} />标记已读</button>}</div>{error && <div role="alert" className="pi-error">{error}</div>}
    <ExecutionCard execution={result} actor={result.assignee_id} api={api} onOpenArtifact={onOpenArtifact} />
    <details className="pi-receipt"><summary>投递凭据</summary><dl><dt>投递 ID</dt><dd>{result.delivery_id}</dd><dt>SHA-256</dt><dd>{result.delivery_sha256}</dd></dl>{result.read_receipt && <pre>{JSON.stringify(result.read_receipt, null, 2)}</pre>}</details>
  </article>
}

export function ExecutionCard({ execution, actor, api, onOpenArtifact }: { execution: PersonalItemExecution; actor: string; api: Pick<PersonalItemsApi, 'personalArtifact' | 'runs'>; onOpenArtifact: (actor: string, ref: PersonalArtifactRef) => void }) {
  const error = executionError(execution.error)
  return <section className="pi-execution"><div className="pi-section-title"><h3>执行结果</h3><Status status={execution.status} /></div>{execution.status === 'unknown' && <div className="pi-unknown" role="status">执行结果未知 · 保留原执行身份</div>}{error && <pre className="pi-error">{error}</pre>}
    {execution.answer && <div className="pi-answer"><Markdown remarkPlugins={[remarkGfm]}>{execution.answer}</Markdown></div>}
    <ReportCards refs={execution.artifacts} actor={actor} api={api} onOpenArtifact={onOpenArtifact} />
    <dl className="pi-execution-ids"><dt>执行 ID</dt><dd>{execution.occurrence_id}</dd>{execution.job_id && <><dt>Job</dt><dd>{execution.job_id}</dd></>}{execution.run_id && <><dt>Run</dt><dd>{execution.run_id}</dd></>}</dl>
    {execution.run_id && execution.execution_session_id && <RunRecord api={api} execution={execution} />}
  </section>
}
function ReportCards({ refs, actor, api, onOpenArtifact }: { refs: PersonalArtifactRef[]; actor: string; api: Pick<PersonalItemsApi, 'personalArtifact'>; onOpenArtifact: (actor: string, ref: PersonalArtifactRef) => void }) {
  return <div className="pi-reports">{refs.map(ref => <ReportCard key={`${actor}:${ref.artifact_id}:${ref.version}:${ref.sha256}`} artifactRef={ref} actor={actor} api={api} onOpenArtifact={onOpenArtifact} />)}</div>
}
function ReportCard({ artifactRef: ref, actor, api, onOpenArtifact }: { artifactRef: PersonalArtifactRef; actor: string; api: Pick<PersonalItemsApi, 'personalArtifact'>; onOpenArtifact: (actor: string, ref: PersonalArtifactRef) => void }) {
  const [artifact, setArtifact] = useState<PersonalArtifactSnapshot | null>(null), [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    void api.personalArtifact(ref.artifact_id, actor, ref.version, controller.signal).then(value => {
      const snapshot = validateArtifact(value, { id: ref.artifact_id, version: ref.version })
      if (snapshot.sha256 !== ref.sha256 || snapshot.grant_id !== ref.grant_id || snapshot.grant_revision !== ref.grant_revision) throw new Error('材料授权或内容校验已变化，请核对原版本')
      if (!controller.signal.aborted) { setArtifact(snapshot); setError('') }
    }).catch(cause => { if (!controller.signal.aborted) setError(itemError(cause)) })
    return () => controller.abort()
  }, [api, actor, ref.artifact_id, ref.version, ref.sha256, ref.grant_id, ref.grant_revision])
  return <div className="pi-report-card"><FileText size={23} /><div><strong>{artifact?.title || '正在核对材料…'}</strong><small>v{ref.version}{artifact ? ` · ${artifact.bytes.toLocaleString()} B` : ''}</small>{error && <span className="pi-report-error" role="alert">{error}</span>}</div><button disabled={!artifact || Boolean(error)} aria-label={`打开${artifact?.title || '材料'}`} onClick={() => requestArtifactNavigation(() => onOpenArtifact(actor, ref))}><ArrowUpRight size={17} /></button></div>
}
function RunRecord({ api, execution }: { api: Pick<PersonalItemsApi, 'runs'>; execution: PersonalItemExecution }) {
  const [open, setOpen] = useState(false), [run, setRun] = useState<Run | null>(null), [error, setError] = useState('')
  useEffect(() => {
    if (!open) return
    let active = true
    if (execution.steps) { setRun({ id: execution.run_id, status: execution.status, started_at: '', steps: execution.steps }); return }
    void api.runs(execution.execution_session_id).then(page => {
      const exact = page.runs.find(value => value.id === execution.run_id)
      if (!exact) throw new Error('原 Run 记录尚待核对')
      if (active) { setRun(exact); setError('') }
    }).catch(cause => { if (active) setError(itemError(cause)) })
    return () => { active = false }
  }, [api, open, execution.run_id, execution.execution_session_id, execution.status, execution.steps])
  return <div className="pi-run-record"><button aria-expanded={open} onClick={() => setOpen(value => !value)}><History size={14} />Run 记录</button>{open && <div>{error && <div className="pi-error" role="alert">{error}</div>}{!run && !error && <div role="status">正在加载 Run…</div>}{run?.steps.map(step => <div className="pi-run-step" key={step.sequence}><span>{step.sequence}</span><div><strong>{step.summary || step.stage}</strong><small>{step.stage} · {step.status}</small>{step.detail && <details><summary>详情</summary><pre>{step.detail}</pre></details>}</div></div>)}{run && !run.steps.length && <div className="pi-empty">Run 暂无步骤记录</div>}</div>}</div>
}
