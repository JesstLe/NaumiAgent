import { useEffect, useRef, useState, type ReactNode } from 'react'
import { ArrowUpRight, Check, CheckCheck, ChevronRight, Clock3, FileText, Globe2, Inbox, Link2, Loader2, RefreshCw, Search, ShieldCheck, ShieldX, X } from 'lucide-react'
import type { ResearchEgressApi, ResearchEgressArtifactTarget, ResearchEgressDecision, ResearchEgressProposal, ResearchEgressStatus, ResearchEgressView } from '@naumi/shared/api/personalResearchEgress'
import type { ResearchResult, ResearchView } from '@naumi/shared/api/personalResearch'
import { useResearchEgress } from './useResearchEgress'
import './research-egress.css'

export const researchEgressStatuses: Record<ResearchEgressStatus, string> = {
  proposed: '待审批', pending: '待审批', approved: '已批准', started: '结果待核对', completed: '已完成', rejected: '已拒绝',
  revoked: '已撤销', expired: '已过期', stale: '依据已变化', unavailable: '依据不可用', unknown: '结果待核对',
}
const actionNames = { search: '外部检索', capture: '读取网页', recheck: '复查网页' }
const decisionNames = { approve: '批准外发', reject: '拒绝外发', revoke: '撤销批准', execute: '执行一次' }
const date = (value?: string) => { if (!value) return '—'; const time = new Date(value); return Number.isNaN(time.getTime()) ? '时间待核对' : time.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) }
const json = (value: unknown) => JSON.stringify(value, null, 2)
const isOriginalResult = (value: ResearchResult): value is ResearchView => 'notebook' in value

export function researchEgressArtifacts(view: ResearchEgressView): ResearchEgressArtifactTarget[] {
  if (view.availability !== 'available' || !view.proposal.actor_id) return []
  const exact = view.proposal.execution?.result
  const receiptRef = exact?.source_ref || exact?.artifact_ref
  if (receiptRef && /^art_[a-f0-9]{32}$/.test(receiptRef.artifact_id) && Number.isSafeInteger(receiptRef.version) && receiptRef.version > 0 && /^[a-f0-9]{64}$/.test(receiptRef.sha256)) return [{ actor_id: view.proposal.actor_id, ref: receiptRef }]
  if (!view.result) return []
  const result = isOriginalResult(view.result) ? view.result : view.result.view
  if (!result || result.availability === 'source_unavailable') return []
  const sourceId = view.proposal.execution?.result?.source_id
  const sourceRevision = view.proposal.execution?.result?.source_revision
  const targets: ResearchEgressArtifactTarget[] = []
  for (const source of result.sources || []) {
    if (sourceId && (source.id !== sourceId || sourceRevision !== undefined && source.revision !== sourceRevision) || !source.artifact_ref || source.status !== 'available') continue
    const ref = source.artifact_ref
    if (/^art_[a-f0-9]{32}$/.test(ref.artifact_id) && Number.isSafeInteger(ref.version) && ref.version > 0 && /^[a-f0-9]{64}$/.test(ref.sha256)) targets.push({ actor_id: view.proposal.actor_id, ref })
  }
  return targets
}

function EgressDialog({ title, children, onClose, busy = false, className = '' }: { title: string; children: ReactNode; onClose: () => void; busy?: boolean; className?: string }) {
  const ref = useRef<HTMLElement>(null)
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    ref.current?.querySelector<HTMLElement>('button:not(:disabled),[tabindex="0"]')?.focus()
    return () => previous?.focus()
  }, [])
  return <div className="pres-egress-backdrop"><section className={`pres-egress-dialog ${className}`} role="dialog" aria-modal="true" aria-label={title} ref={ref} onKeyDown={event => {
    event.stopPropagation()
    if (event.key === 'Escape' && !busy) { event.preventDefault(); onClose() }
    if (event.key === 'Tab') {
      const fields = [...(ref.current?.querySelectorAll<HTMLElement>('button,a[href],input,select,textarea,[tabindex="0"]') || [])].filter(item => !(item as HTMLButtonElement).disabled && item.tabIndex >= 0)
      if (fields.length && (event.shiftKey ? document.activeElement === fields[0] : document.activeElement === fields.at(-1))) { event.preventDefault(); (event.shiftKey ? fields.at(-1) : fields[0])?.focus() }
    }
  }}><header className="pres-egress-heading"><div><ShieldCheck size={20} /><h2>{title}</h2></div><button aria-label={`关闭${title}`} disabled={busy} onClick={onClose}><X size={19} /></button></header>{children}</section></div>
}

function Status({ value }: { value: ResearchEgressStatus }) { return <span className={`pres-egress-status is-${value}`}>{value === 'completed' ? <CheckCheck size={13} /> : value === 'approved' ? <Check size={13} /> : value === 'started' ? <Clock3 size={13} /> : null}{researchEgressStatuses[value]}</span> }
function Payload({ proposal }: { proposal: ResearchEgressProposal }) {
  const payload = proposal.payload
  const content = payload && ('query' in payload ? payload.query : payload.url)
  return <section className="pres-egress-section"><h3>外发内容</h3><pre className="pres-egress-payload" aria-label={proposal.action === 'search' ? '确切外发检索词' : '确切外发网址'} tabIndex={0}>{content || '—'}</pre></section>
}
function Destination({ proposal }: { proposal: ResearchEgressProposal }) {
  const plan = proposal.destination_plan
  if (!plan) return null
  return <section className="pres-egress-section"><h3>接收方</h3><div className="pres-egress-destination"><span className="pres-egress-method">{plan.method}</span><code aria-label="确切接收地址">{plan.endpoint}</code></div><dl className="pres-egress-facts"><dt>服务</dt><dd>{({ duckduckgo: 'DuckDuckGo', brave: 'Brave Search', direct: '目标网站' })[plan.provider]}</dd><dt>重定向</dt><dd>不跟随</dd></dl>{Object.keys(plan.parameters).length > 0 && <div className="pres-egress-parameters"><strong>请求参数</strong><pre aria-label="确切外发参数">{json(plan.parameters)}</pre></div>}<details className="pres-egress-transport"><summary>传输依据<ChevronRight size={13} /></summary><dl><dt>请求编码</dt><dd>{plan.request_encoding}</dd><dt>传输</dt><dd>{plan.endpoint.startsWith('https:') ? 'HTTPS · 证书校验' : 'HTTP'}</dd><dt>环境代理</dt><dd>不使用</dd></dl>{Object.keys(plan.headers).length > 0 && <pre aria-label="公开请求头">{json(plan.headers)}</pre>}<code>{plan.routing_digest}</code></details></section>
}

export function ResearchEgress({ api, namespace, workspaceKey, serverNow, connected, onClose, onOpenScopedArtifact }: {
  api: ResearchEgressApi; namespace: string; workspaceKey: string; serverNow?: string; connected: boolean; onClose: () => void
  onOpenScopedArtifact?: (actorId: string, id: string, version: number) => void
}) {
  const state = useResearchEgress({ api, namespace, workspaceKey, connected })
  const [confirm, setConfirm] = useState<{ action: ResearchEgressDecision | 'execute'; snapshot: ResearchEgressView } | null>(null)
  const [localNotice, setLocalNotice] = useState(''), [filter, setFilter] = useState('all')
  const [now, setNow] = useState(() => serverNow ? Date.parse(serverNow) : Date.now())
  useEffect(() => {
    const start = performance.now(), clock = serverNow && Number.isFinite(Date.parse(serverNow)) ? Date.parse(serverNow) : Date.now()
    setNow(clock)
    const timer = window.setInterval(() => setNow(clock + performance.now() - start), 1000)
    return () => window.clearInterval(timer)
  }, [serverNow])
  useEffect(() => { setConfirm(null); setLocalNotice(''); setFilter('all') }, [api, namespace, workspaceKey, connected])
  useEffect(() => {
    if (!confirm) return
    const current = state.detail, original = confirm.snapshot.proposal
    if (!current || current.availability !== 'available' || current.proposal.id !== original.id || current.proposal.revision !== original.revision || current.proposal.digest !== original.digest) {
      setConfirm(null); setLocalNotice('外发记录已变化，请重新审阅')
    }
  }, [state.detail, state.revisionToken, confirm])
  const detail = state.detail, proposal = detail?.proposal
  const available = detail?.availability === 'available' && Boolean(proposal?.payload && proposal.destination_plan)
  const expired = Boolean(proposal?.expires_at && Date.parse(proposal.expires_at) <= now)
  const visibleStatus = proposal && expired && ['proposed', 'pending', 'approved'].includes(proposal.status) ? 'expired' : proposal?.status
  const canDecide = available && !expired && Boolean(proposal && ['proposed', 'pending'].includes(proposal.status))
  const canExecute = available && !expired && proposal?.status === 'approved' && proposal.approval?.digest === proposal.digest
  const beforeSend = Boolean(proposal && !['started', 'completed', 'unknown', 'rejected', 'revoked', 'unavailable'].includes(proposal.status) && !proposal.execution)
  const canReject = available && beforeSend && !proposal?.approval
  const canRevoke = available && beforeSend && Boolean(proposal?.approval)
  const targets = detail ? researchEgressArtifacts(detail) : []
  const listed = state.rows.filter(row => filter === 'all' || (filter === 'review' ? ['proposed', 'pending', 'approved'].includes(row.proposal.status) : ['started', 'unknown'].includes(row.proposal.status)))
  const confirmExpired = Boolean(confirm?.snapshot.proposal.expires_at && Date.parse(confirm.snapshot.proposal.expires_at) <= now)
  const queue = <EgressDialog title="外发审批" onClose={onClose} busy={state.busy} className="pres-egress-workspace">
    {!connected && <div className="pres-egress-message" role="status">已断开</div>}
    {(state.error || state.identityError) && <div className="pres-egress-message is-error" role="alert"><ShieldX size={16} />{state.identityError || state.error}</div>}
    {(state.notice || localNotice) && <div className="pres-egress-message" role="status">{localNotice || state.notice}</div>}
    {state.pending && <div className="pres-egress-pending" role="status"><Clock3 size={17} /><span><strong>原请求待核对</strong><code>{state.pending.request_id}</code></span><button disabled={state.busy || !connected || Boolean(state.identityError)} onClick={() => void state.reconcile()}>核对原外发请求</button>{state.canResumeCaptured && <button className="pres-egress-primary" disabled={state.busy || !connected} onClick={() => void state.resumeCaptured()}>继续保存原结果</button>}{state.canRetry && <button disabled={state.busy || !connected} onClick={state.retry}>重试原审批请求</button>}</div>}
    <div className="pres-egress-layout"><aside className="pres-egress-list"><div className="pres-egress-list-toolbar"><select aria-label="外发状态筛选" value={filter} onChange={event => setFilter(event.target.value)}><option value="all">全部请求</option><option value="review">待处理</option><option value="execution">执行中与待核对</option></select><button aria-label="刷新外发队列" disabled={!connected || state.loading || state.busy} onClick={state.refresh}><RefreshCw size={15} /></button></div><nav aria-label="外发审批队列">{listed.map(row => { const item = row.proposal; return <button key={item.id} className={`pres-egress-card${state.selectedId === item.id ? ' selected' : ''}`} disabled={!connected || state.busy} onClick={() => { setConfirm(null); setLocalNotice(''); void state.select(item.id) }}><span className="pres-egress-card-top">{item.action === 'search' ? <Search size={17} /> : <Globe2 size={17} />}<Status value={item.status} /></span><strong>{row.availability === 'source_unavailable' ? '原依据不可用' : item.notebook_title || actionNames[item.action || 'search']}</strong><span>{row.availability === 'source_unavailable' ? item.id : `${item.actor_name || item.actor_id || 'Agent'} · ${actionNames[item.action || 'search']}`}</span>{item.notebook_revision && row.availability === 'available' && <small>研究 v{item.notebook_revision} · {date(item.created_at)}</small>}</button> })}</nav>{state.loading && <div className="pres-egress-empty" role="status"><Loader2 size={20} className="pres-egress-spin" /><span>读取外发队列</span></div>}{!state.loading && !listed.length && <div className="pres-egress-empty"><Inbox size={29} /><strong>暂无外发请求</strong></div>}{state.nextAfter && <button className="pres-egress-more" disabled={state.loading || !connected || state.busy} onClick={state.more}>更多请求</button>}</aside>
      <main className="pres-egress-detail" aria-label="外发请求详情">{state.detailLoading ? <div className="pres-egress-empty" role="status"><Loader2 size={24} className="pres-egress-spin" /><span>读取原外发记录</span></div> : !detail ? <div className="pres-egress-empty"><ShieldCheck size={35} /><strong>选择外发请求</strong></div> : detail.availability !== 'available' ? <div className="pres-egress-empty is-unavailable"><ShieldX size={34} /><strong>原外发依据不可用</strong><code>{detail.proposal.id}</code></div> : proposal && <>
        <div className="pres-egress-detail-heading"><span className="pres-egress-kind">{proposal.action === 'search' ? <Search size={17} /> : <Link2 size={17} />}{actionNames[proposal.action || 'search']}</span><Status value={visibleStatus!} /></div>
        <h2>{proposal.notebook_title}</h2><div className="pres-egress-owner"><span>{proposal.actor_name || proposal.actor_id}</span><span>研究 v{proposal.notebook_revision}</span><span><Clock3 size={13} />{date(proposal.expires_at)}</span></div>
        <Payload proposal={proposal} /><Destination proposal={proposal} />
        {proposal.reason && <section className="pres-egress-section"><h3>请求原因</h3><p className="pres-egress-reason">{proposal.reason}</p></section>}
        {proposal.dependencies && <section className="pres-egress-section"><h3>原依据</h3><div className="pres-egress-dependencies"><span>资料 {proposal.dependencies.artifact_count}</span><span>记忆 {proposal.dependencies.memory_count}</span><span>来源 {proposal.dependencies.source_count}</span></div></section>}
        <div className="pres-egress-actions">{canDecide && <button className="pres-egress-primary" disabled={state.locked} onClick={() => setConfirm({ action: 'approve', snapshot: detail })}><Check size={15} />批准</button>}{canReject && <button disabled={state.locked} onClick={() => setConfirm({ action: 'reject', snapshot: detail })}>拒绝</button>}{canExecute && <button className="pres-egress-primary" disabled={state.locked} onClick={() => setConfirm({ action: 'execute', snapshot: detail })}><ArrowUpRight size={16} />执行一次</button>}{canRevoke && <button disabled={state.locked} onClick={() => setConfirm({ action: 'revoke', snapshot: detail })}>撤销批准</button>}{['started', 'unknown'].includes(proposal.status) && <button disabled={state.busy || !connected} onClick={() => void state.select(proposal.id)}><RefreshCw size={14} />读取原执行状态</button>}</div>
        {proposal.execution && <section className="pres-egress-section pres-egress-result"><h3>执行结果</h3><Status value={proposal.execution.status === 'completed' ? 'completed' : proposal.status === 'unknown' ? 'unknown' : 'started'} /><time>{date(proposal.execution.started_at)}</time>{proposal.execution.result && <dl className="pres-egress-facts">{proposal.execution.result.source_id && <><dt>来源</dt><dd><code>{proposal.execution.result.source_id} · v{proposal.execution.result.source_revision}</code></dd></>}{proposal.execution.result.discovery_id && <><dt>检索结果</dt><dd><code>{proposal.execution.result.discovery_id}</code></dd></>}</dl>}{targets.map(target => <button key={`${target.ref.artifact_id}:${target.ref.version}`} disabled={!onOpenScopedArtifact || !connected} onClick={() => onOpenScopedArtifact?.(target.actor_id, target.ref.artifact_id, target.ref.version)}><FileText size={15} />打开原来源资料<span>v{target.ref.version}</span><ArrowUpRight size={13} /></button>)}{detail.result && !isOriginalResult(detail.result) && <div className="pres-egress-discoveries">{detail.result.discovery.items.map((item, index) => <article key={`${item.url}:${index}`}><strong>{item.title}</strong><code>{item.url}</code>{item.snippet && <p>{item.snippet}</p>}</article>)}</div>}</section>}
        <details className="pres-egress-identities"><summary>请求与审批依据<ChevronRight size={13} /></summary><dl><dt>原请求</dt><dd>{proposal.id}</dd><dt>修订</dt><dd>{proposal.revision}</dd><dt>内容摘要</dt><dd>{proposal.digest}</dd><dt>研究</dt><dd>{proposal.notebook_id}</dd><dt>原会话</dt><dd>{proposal.session_id}</dd>{proposal.approval && <><dt>审批凭据</dt><dd>{proposal.approval.decision_id}</dd><dt>审批时间</dt><dd>{date(proposal.approval.approved_at)}</dd></>}{proposal.execution && <><dt>执行请求</dt><dd>{proposal.execution.request_id}</dd></>}</dl></details>
      </>}</main></div>
  </EgressDialog>
  return <div className="pres-egress">{queue}{available && confirm && <EgressDialog title={decisionNames[confirm.action]} onClose={() => setConfirm(null)} busy={state.busy} className="pres-egress-confirm"><div className="pres-egress-confirm-body"><div className="pres-egress-confirm-owner"><strong>{confirm.snapshot.proposal.actor_name || confirm.snapshot.proposal.actor_id}</strong><span>{confirm.snapshot.proposal.notebook_title} · v{confirm.snapshot.proposal.notebook_revision}</span></div><Payload proposal={confirm.snapshot.proposal} /><Destination proposal={confirm.snapshot.proposal} />{confirmExpired && ['approve', 'execute'].includes(confirm.action) && <div className="pres-egress-message is-error" role="alert">原外发请求已过期</div>}</div><footer><button disabled={state.busy} onClick={() => setConfirm(null)}>取消</button><button className={confirm.action === 'reject' || confirm.action === 'revoke' ? 'pres-egress-danger' : 'pres-egress-primary'} disabled={state.locked || confirmExpired && ['approve', 'execute'].includes(confirm.action)} onClick={async () => {
      const original = confirm
      const success = await state.mutate(original.action === 'execute' ? 'egress_execute' : 'egress_decide', original.snapshot.proposal, original.action === 'execute' ? undefined : original.action)
      if (success) setConfirm(null)
    }}>{state.busy ? '正在提交' : `确认${decisionNames[confirm.action]}`}</button></footer></EgressDialog>}</div>
}
