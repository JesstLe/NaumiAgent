import { useEffect, useRef, useState } from 'react'
import { ArrowRight, CheckCheck, FileText, History, Loader2, RefreshCw, ShieldAlert, X } from 'lucide-react'
import type { ResearchSessionContextApi, ResearchSessionContextPreview } from '@naumi/shared/api/personalResearchContext'
import { useResearchContext, type ResearchContextTarget } from './useResearchContext'
import './research-context.css'

export function ResearchContext({ api, namespace, target, connected, writable = true, onClose }: {
  api: ResearchSessionContextApi; namespace: string; target: ResearchContextTarget; connected: boolean; writable?: boolean; onClose: () => void
}) {
  const state = useResearchContext({ api, namespace, target, connected, writable })
  const [confirmation, setConfirmation] = useState<ResearchSessionContextPreview | null>(null)
  const dialog = useRef<HTMLElement>(null)
  const view = state.view
  const confirming = confirmation && state.canUpdate && view?.plan_digest === confirmation.plan_digest && view.context_revision === confirmation.context_revision ? confirmation : null
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    dialog.current?.querySelector<HTMLElement>('button:not(:disabled)')?.focus()
    return () => { if (previous?.isConnected) previous.focus() }
  }, [])
  useEffect(() => { setConfirmation(null) }, [api, namespace, target.workspaceKey, target.actorId, target.sessionId, connected, writable])
  return <div className="pres-context-backdrop"><section className="pres-context-dialog" ref={dialog} role="dialog" aria-modal="true" aria-labelledby="pres-context-title" onKeyDown={event => {
    event.stopPropagation()
    if (event.key === 'Escape' && !state.busy) { event.preventDefault(); if (confirming) setConfirmation(null); else onClose() }
    if (event.key === 'Tab') {
      const fields = [...(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled),a[href],[tabindex="0"]') || [])].filter(item => item.tabIndex >= 0)
      if (fields.length && (event.shiftKey ? document.activeElement === fields[0] : document.activeElement === fields.at(-1))) { event.preventDefault(); (event.shiftKey ? fields.at(-1) : fields[0])?.focus() }
    }
  }}>
    <header className="pres-context-heading"><div><History size={20} /><h2 id="pres-context-title">对话依据</h2></div><button aria-label="关闭对话依据" disabled={state.busy} onClick={onClose}><X size={18} /></button></header>
    {(state.error || state.identityError) && <div className="pres-context-alert" role="alert"><ShieldAlert size={16} /><span>{state.identityError || state.error}</span></div>}
    {state.notice && <div className="pres-context-notice" role="status"><CheckCheck size={16} /><span>{state.notice}</span></div>}
    {state.pending && <div className="pres-context-pending"><div><strong>原请求待核对</strong><code>{state.pending.request_id}</code></div><button disabled={!state.canRead} onClick={state.reconcile}>{state.busy ? <Loader2 size={15} className="pres-context-spin" /> : <RefreshCw size={15} />}核对原请求</button></div>}
    <div className="pres-context-body">
      {!connected || !writable ? <div className="pres-context-empty"><ShieldAlert size={28} /><strong>{!connected ? '连接已断开' : '当前对话暂不可更新'}</strong></div>
        : state.loading ? <div className="pres-context-empty" role="status"><Loader2 size={25} className="pres-context-spin" /><strong>读取对话依据</strong></div>
          : !view ? <div className="pres-context-empty"><History size={28} /><strong>{state.pending ? '结果待核对' : '依据待核对'}</strong></div>
            : <>
              <div className="pres-context-owner"><span>{view.actor_name}</span><h3>{view.session_title || '未命名对话'}</h3><code>{view.session_id}</code></div>
              {view.status === 'blocked' ? <div className="pres-context-empty"><ShieldAlert size={28} /><strong>原依据不可用</strong></div>
                : view.status === 'current' ? <div className="pres-context-current"><CheckCheck size={21} /><strong>无需更新</strong></div>
                  : <>
                    <div className="pres-context-section-title"><h3>保留原文的研究材料</h3><span>{view.materials.length}</span></div>
                    <div className="pres-context-materials" aria-label="待更新研究材料">{view.materials.map((row, index) => <article key={`${row.original_ref.artifact_id}:${row.original_ref.version}:${index}`}><FileText size={17} /><div><strong>{row.title || '研究材料'}</strong><span>v{row.original_ref.version} · {row.read_count} 份研究凭据</span><code>{row.body_sha256}</code></div></article>)}</div>
                    {view.changes.length > 0 && <><div className="pres-context-section-title"><h3>来源变化</h3><span>{view.changes.length}</span></div><div className="pres-context-changes" aria-label="来源变化">{view.changes.map((row, index) => <article key={`${row.notebook_id}:${row.source_id}:${index}`}><code>{row.source_id}</code><div><span>v{row.from_revision}{row.from_expired ? ' · 已过期' : ''}</span><ArrowRight size={13} /><span>v{row.to_revision}{row.to_expired ? ' · 已过期' : ''}</span></div></article>)}</div></>}
                  </>}
              {view.status !== 'blocked' && view.historical_notice && <p className="pres-context-historical">{view.historical_notice}</p>}
            </>}
      {confirming && <div className="pres-context-confirm" role="group" aria-label="确认更新对话依据"><strong>更新这段对话的研究依据？</strong><span>{confirming.actor_name} · {confirming.session_title || confirming.session_id}</span><p>原对话和原文保持不变，不代表旧结论已重新核实。</p></div>}
    </div>
    <footer className="pres-context-actions"><button disabled={!state.canRead || Boolean(state.pending)} onClick={() => { setConfirmation(null); void state.refresh() }}><RefreshCw size={14} />重新核对</button><div>{confirming ? <><button disabled={state.busy} onClick={() => setConfirmation(null)}>取消</button><button className="pres-context-primary" disabled={!state.canUpdate} onClick={() => { const original = confirming; setConfirmation(null); void state.update(original) }}>确认更新</button></> : <button className="pres-context-primary" disabled={!state.canUpdate} onClick={() => view && setConfirmation(view)}>{state.busy ? <Loader2 size={15} className="pres-context-spin" /> : <History size={15} />}更新依据</button>}</div></footer>
  </section></div>
}
