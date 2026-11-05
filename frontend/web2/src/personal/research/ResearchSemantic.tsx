import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { AlertTriangle, ArrowRight, CheckCheck, ChevronRight, ClipboardCheck, ExternalLink, FileText, Loader2, RefreshCw, ShieldAlert, Square, X } from 'lucide-react'
import type { PersonalAgent } from '@naumi/shared/api/WorkbenchRuntimeClient'
import type { ResearchSemanticApi, ResearchSemanticPreview, ResearchSemanticReview, ResearchSemanticStatus } from '@naumi/shared/api/personalResearchSemantic'
import { semanticActive, useResearchSemantic, type ResearchSemanticTarget } from './useResearchSemantic'
import './research-semantic.css'

const statuses: Record<ResearchSemanticStatus, string> = { proposed: '准备中', admitted: '已入队', running: '执行中', completed: '已完成', failed: '执行失败', unknown: '结果待核对', cancelled: '已停止' }
const verdicts = { supported: '来源支持', contradicted: '发现矛盾', insufficient: '证据不足' }
const blockMessage = (code: string) => ({ missing_owner_grant: '原资料需要其所有者授权', source_unavailable: '原来源不可用', source_stale: '来源已变化', reviewer_unavailable: '所选 Agent 暂不可用', agent_inactive: '所选 Agent 已停用', binding_conflict: '原对话身份不一致', execution_session_readonly: '原对话为只读记录', no_claims: '暂无待复核声明', input_limit: '本次证据超过容量' }[code] || '原依据或执行条件尚待核对')

export function ResearchSemantic({ api, namespace, target, agents, agentsLoading = false, agentsError = false, connected, writable = true, onOpenExecution }: {
  api: ResearchSemanticApi; namespace: string; target: ResearchSemanticTarget; agents: PersonalAgent[]; agentsLoading?: boolean; agentsError?: boolean
  connected: boolean; writable?: boolean; onOpenExecution?: (sessionId: string, runId: string) => void
}) {
  const eligible = agents.filter(row => row.status === 'active' && row.kind === 'specialist' && row.id !== target.authorId)
  const [reviewerId, setReviewerId] = useState(''), [composer, setComposer] = useState(false), [confirmation, setConfirmation] = useState<ResearchSemanticPreview | null>(null)
  const [stopping, setStopping] = useState<ResearchSemanticReview | null>(null)
  const reviewer = eligible.find(row => row.id === reviewerId) || null
  const allowed = writable && !agentsLoading && !agentsError
  const state = useResearchSemantic({ api, namespace, target, reviewer, connected, writable: allowed })
  const view = state.selectedReview
  const modal = useRef<HTMLElement>(null)
  const confirming = confirmation && state.canStart && state.preview?.input_digest === confirmation.input_digest && reviewer?.id === confirmation.reviewer.id && reviewer.version === confirmation.reviewer.version ? confirmation : null
  useEffect(() => { setComposer(false); setConfirmation(null); setStopping(null); setReviewerId('') }, [api, namespace, target.workspaceKey, target.authorId, target.notebookId, target.revision, target.sourceSessionId])
  useEffect(() => { setConfirmation(null) }, [reviewer?.id, reviewer?.version, allowed, connected])
  useEffect(() => {
    if (!composer && !stopping) return
    const previous = document.activeElement as HTMLElement | null
    modal.current?.querySelector<HTMLElement>('button:not(:disabled)')?.focus()
    return () => { if (previous?.isConnected) previous.focus() }
  }, [composer, stopping])
  const close = () => { if (!state.busy) { setComposer(false); setConfirmation(null); setStopping(null) } }
  const keydown = (event: KeyboardEvent<HTMLElement>) => {
    event.stopPropagation()
    if (event.key === 'Escape' && !state.busy) { event.preventDefault(); if (confirming) setConfirmation(null); else close() }
    if (event.key === 'Tab') {
      const fields = [...(modal.current?.querySelectorAll<HTMLElement>('button:not(:disabled),select:not(:disabled),a[href],[tabindex="0"]') || [])].filter(row => row.tabIndex >= 0)
      if (fields.length && (event.shiftKey ? document.activeElement === fields[0] : document.activeElement === fields.at(-1))) { event.preventDefault(); (event.shiftKey ? fields.at(-1) : fields[0])?.focus() }
    }
  }
  return <section className="pres-semantic" aria-label="Agent 语义复核">
    <header className="pres-semantic-heading"><div><ClipboardCheck size={16} /><h3>Agent 复核</h3></div><div><button aria-label="刷新 Agent 复核" disabled={!state.canRead} onClick={() => void state.refresh()}><RefreshCw size={15} className={state.loading ? 'pres-semantic-spin' : ''} /></button><button className="pres-semantic-start" disabled={!connected || !allowed || !target.sourceSessionId || state.busy || Boolean(state.pending) || Boolean(state.identityError)} onClick={() => { setComposer(true); setConfirmation(null) }}>发起复核<ChevronRight size={14} /></button></div></header>
    {(state.error || state.identityError) && <div className="pres-semantic-error" role="alert"><ShieldAlert size={15} /><span>{state.identityError || state.error}</span></div>}
    {state.pending && <div className="pres-semantic-pending"><strong>原复核请求待核对</strong><code>{state.pending.request_id}</code><button disabled={!state.canRead} onClick={state.reconcileRequest}><RefreshCw size={14} />核对原请求</button></div>}
    {state.loading && !state.reviews.length ? <div className="pres-semantic-empty" role="status"><Loader2 size={22} className="pres-semantic-spin" />读取复核</div>
      : !state.reviews.length ? <div className="pres-semantic-empty"><ClipboardCheck size={24} /><strong>暂无 Agent 复核</strong></div>
        : <>
          <nav className="pres-semantic-history" aria-label="Agent 复核记录">{state.reviews.map(row => <button key={row.id} aria-current={view?.id === row.id ? 'true' : undefined} disabled={state.busy} onClick={() => state.select(row.id)}><strong>{row.reviewer.name}</strong><span>研究 v{row.notebook_revision}<i>·</i>{statuses[row.execution_status]}</span></button>)}</nav>
          {view && <div className="pres-semantic-result" aria-label="原 Agent 复核结果"><header><div><strong>{view.reviewer.name}</strong><span>Agent v{view.reviewer.version} · 研究 v{view.notebook_revision}</span></div><span className={`pres-semantic-status is-${view.execution_status}`}>{statuses[view.execution_status]}</span></header>
            {view.basis_status !== 'current' && <div className="pres-semantic-basis" role="status"><AlertTriangle size={15} />{view.basis_status === 'stale' ? '原依据已变化' : '原依据不可用'}</div>}
            <div className="pres-semantic-execution">{view.session_id && view.run_id && onOpenExecution && <button onClick={() => onOpenExecution(view.session_id, view.run_id)}><ExternalLink size={13} />查看原执行</button>}{semanticActive(view) && <button disabled={!state.canMutate} onClick={() => setStopping(view)}><Square size={12} />停止复核</button>}{view.execution_status === 'unknown' && <button disabled={!state.canMutate} onClick={() => void state.reconcile(view)}><RefreshCw size={13} />核对原执行</button>}</div>
            {view.basis_status !== 'unavailable' && view.result && <><div className="pres-semantic-scope"><CheckCheck size={14} /><span>给定来源复核</span></div>{view.result.summary && <p className="pres-semantic-summary">{view.result.summary}</p>}<div className="pres-semantic-verdicts">{view.result.verdicts.map(row => <article key={row.claim_id} className={`pres-semantic-verdict is-${row.verdict}`}><header><code>{row.claim_id}</code><strong>{verdicts[row.verdict]}</strong></header><p>{row.reason}</p>{row.evidence.map((anchor, index) => <div className="pres-semantic-evidence" key={`${anchor.source_id}:${anchor.source_revision}:${anchor.start_byte}:${index}`}><span><FileText size={13} /><code>{anchor.source_id}</code><strong>v{anchor.source_revision}</strong></span><blockquote>{anchor.quote}</blockquote><div><code>字符 {anchor.start}–{anchor.end}</code><code>字节 {anchor.start_byte}–{anchor.end_byte}</code></div><code title={anchor.body_sha256}>{anchor.body_sha256}</code></div>)}</article>)}</div></>}
            <details className="pres-semantic-provenance"><summary>原执行依据</summary><dl><dt>复核</dt><dd>{view.id}</dd><dt>Agent</dt><dd>{view.reviewer.id} · v{view.reviewer.version}</dd><dt>协作</dt><dd>{view.collaboration_id || '待核对'}</dd><dt>Job</dt><dd>{view.job_id || '待核对'}</dd><dt>Session</dt><dd>{view.session_id || '待核对'}</dd><dt>Run</dt><dd>{view.run_id || '待核对'}</dd><dt>输入</dt><dd>{view.input_digest}</dd>{view.result && <><dt>结果</dt><dd>{view.result.digest}</dd></>}</dl></details>
          </div>}
        </>}
    {(composer || stopping) && <div className="pres-semantic-backdrop"><section ref={modal} className="pres-semantic-dialog" role="dialog" aria-modal="true" aria-labelledby="pres-semantic-dialog-title" onKeyDown={keydown}>
      <header><div><ClipboardCheck size={20} /><h2 id="pres-semantic-dialog-title">{stopping ? '停止这次复核？' : '独立 Agent 复核'}</h2></div><button aria-label="关闭独立复核" disabled={state.busy} onClick={close}><X size={18} /></button></header>
      {stopping ? <><div className="pres-semantic-dialog-body"><strong>{stopping.reviewer.name}</strong><span>研究 v{stopping.notebook_revision}</span><code>{stopping.id}</code></div><footer><button disabled={state.busy} onClick={close}>保留执行</button><button className="pres-semantic-danger" disabled={!state.canMutate} onClick={async () => { const original = stopping; setStopping(null); await state.cancel(original) }}>停止复核</button></footer></>
        : <><div className="pres-semantic-dialog-body"><div className="pres-semantic-composer-version"><strong>研究 v{target.revision}</strong><code>{target.notebookId}</code></div><label>Agent<select aria-label="独立复核 Agent" disabled={state.busy || !allowed} value={reviewerId} onChange={event => { setReviewerId(event.target.value); setConfirmation(null) }}><option value="">{agentsLoading ? '读取 Agent' : eligible.length ? '选择 Agent' : '暂无可用 Agent'}</option>{eligible.map(row => <option key={row.id} value={row.id}>{row.name} · v{row.version}</option>)}</select></label>{agentsError && <div className="pres-semantic-error" role="alert"><ShieldAlert size={15} />Agent 目录待核对</div>}
          {state.previewLoading && <div className="pres-semantic-empty" role="status"><Loader2 size={22} className="pres-semantic-spin" />核对原证据</div>}
          {state.preview && <><div className="pres-semantic-recipient"><span>接收 Agent</span><strong>{state.preview.reviewer.name} · v{state.preview.reviewer.version}</strong></div><div className="pres-semantic-packet-heading"><h3>确切来源</h3><span>{state.preview.sources.length}</span></div><div className="pres-semantic-packet">{state.preview.sources.map(row => <article key={`${row.id}:${row.revision}`}><FileText size={16} /><div><strong>{row.title || row.id}</strong><span>v{row.revision} · {row.bytes} 字节</span><code>{row.body_sha256}</code></div></article>)}</div>{state.preview.materials.length > 0 && <><div className="pres-semantic-packet-heading"><h3>资料访问</h3><span>{state.preview.materials.length}</span></div><div className="pres-semantic-sharing">{state.preview.materials.map(row => <article key={`${row.artifact_id}:${row.version}`}><div><strong>{row.title || row.artifact_id}</strong><code>{row.artifact_id} · v{row.version}</code></div><span className={`is-${row.sharing}`}>{row.sharing === 'existing' ? '已获授权' : row.sharing === 'requires_owner_confirmation' ? '确认后共享' : '需所有者授权'}</span></article>)}</div></>}{state.preview.memory_count > 0 && <div className="pres-semantic-memory"><span>原记忆依据</span><strong>{state.preview.memory_count}</strong></div>}{!state.preview.can_start && <div className="pres-semantic-blocked" role="status">{[...new Set(state.preview.blocking_codes.map(blockMessage))].map(message => <span key={message}><ShieldAlert size={14} />{message}</span>)}</div>}<details className="pres-semantic-provenance"><summary>输入摘要</summary><code>{state.preview.input_digest}</code></details></>}
          {confirming && <div className="pres-semantic-confirm" role="group" aria-label="确认独立 Agent 复核"><strong>将本次确切证据交给 {confirming.reviewer.name}？</strong><span>研究 v{confirming.notebook_revision} <ArrowRight size={13} />Agent v{confirming.reviewer.version}</span>{confirming.materials.some(row => row.sharing === 'requires_owner_confirmation') && <p>仅共享上列由当前作者持有的确切资料版本。</p>}</div>}
          {(state.error || state.identityError) && <div className="pres-semantic-error" role="alert"><ShieldAlert size={15} /><span>{state.identityError || state.error}</span></div>}
        </div><footer><button disabled={!state.canRead || state.busy || !reviewer || Boolean(state.pending)} onClick={() => { setConfirmation(null); void state.refreshPreview() }}><RefreshCw size={14} />重新核对</button><div>{confirming ? <><button disabled={state.busy} onClick={() => setConfirmation(null)}>取消</button><button className="pres-semantic-primary" disabled={!state.canStart} onClick={async () => { const original = confirming; setConfirmation(null); if (await state.start(original)) setComposer(false) }}>确认复核</button></> : <button className="pres-semantic-primary" disabled={!state.canStart} onClick={() => state.preview && setConfirmation(state.preview)}>{state.busy ? <Loader2 size={15} className="pres-semantic-spin" /> : <ClipboardCheck size={15} />}发起复核</button>}</div></footer></>}
    </section></div>}
  </section>
}
