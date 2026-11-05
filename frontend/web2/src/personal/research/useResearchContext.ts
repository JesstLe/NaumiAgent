import { useEffect, useRef, useState } from 'react'
import type { ResearchSessionContextApi, ResearchSessionContextPending, ResearchSessionContextPreview, ResearchSessionContextReceipt, ResearchSessionContextWrite } from '@naumi/shared/api/personalResearchContext'

const sha = /^[a-f0-9]{64}$/
const actor = /^(main|pa_[a-f0-9]{32})$/
const session = /^[A-Za-z0-9_-]{1,128}$/
const request = /^[a-f0-9]{32}$/
const nonnegative = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) >= 0
const positive = (value: unknown): value is number => nonnegative(value) && value > 0
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T
class ContextStateError extends Error {}
export type ResearchContextTarget = { workspaceKey: string; sessionId: string; actorId: string }
export const researchContextPendingKey = (namespace: string, target: ResearchContextTarget) => `naumi:research:context-pending:v1:${[namespace, target.workspaceKey, target.actorId, target.sessionId].map(encodeURIComponent).join(':')}`
export const researchContextTargetValid = (target: ResearchContextTarget) => sha.test(target.workspaceKey) && actor.test(target.actorId) && session.test(target.sessionId)

export function parseResearchContextPending(raw: string): ResearchSessionContextPending {
  let value: unknown
  try { value = JSON.parse(raw) } catch { throw new ContextStateError('原对话依据请求状态待核对') }
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ContextStateError('原对话依据请求状态待核对')
  const row = value as Record<string, unknown>
  if (Object.keys(row).sort().join(',') !== 'actor_id,operation,request_id,session_id,version,workspace_key' || row.version !== 1
      || row.operation !== 'context_rollforward' || typeof row.request_id !== 'string' || !request.test(row.request_id)
      || typeof row.workspace_key !== 'string' || !sha.test(row.workspace_key) || typeof row.actor_id !== 'string' || !actor.test(row.actor_id)
      || typeof row.session_id !== 'string' || !session.test(row.session_id)) throw new ContextStateError('原对话依据请求身份待核对')
  return row as unknown as ResearchSessionContextPending
}
const matches = (row: { workspace_key: string; session_id: string; actor_id: string }, target: ResearchContextTarget) => row.workspace_key === target.workspaceKey && row.session_id === target.sessionId && row.actor_id === target.actorId

export function validateResearchContext(value: ResearchSessionContextPreview, target: ResearchContextTarget): ResearchSessionContextPreview {
  if (!value || !matches(value, target) || !nonnegative(value.context_revision) || !nonnegative(value.actor_version)
      || typeof value.actor_name !== 'string' || !value.actor_name || value.actor_name.length > 256 || typeof value.session_title !== 'string' || value.session_title.length > 1000
      || !sha.test(value.plan_digest) || !['ready', 'current', 'blocked'].includes(value.status)
      || !Array.isArray(value.materials) || value.materials.length > 4096 || !Array.isArray(value.changes) || value.changes.length > 4096
      || typeof value.historical_notice !== 'string' || value.historical_notice.length > 2000) throw new ContextStateError('对话依据身份或版本待核对')
  if (value.status === 'blocked') return { ...value, materials: [], changes: [], historical_notice: '' }
  for (const row of value.materials) {
    const ref = row?.original_ref
    if (!ref || !/^art_[a-f0-9]{32}$/.test(ref.artifact_id) || !positive(ref.version) || !sha.test(ref.sha256)
        || !/^(|asg_[a-f0-9]{32})$/.test(ref.grant_id) || !nonnegative(ref.grant_revision) || Boolean(ref.grant_id) !== (ref.grant_revision > 0)
        || typeof row.title !== 'string' || row.title.length > 1000 || !sha.test(row.body_sha256) || row.body_sha256 !== ref.sha256 || !positive(row.read_count)) throw new ContextStateError('原研究材料或正文摘要待核对')
  }
  for (const row of value.changes) if (!row || !/^rq_[a-f0-9]{32}$/.test(row.notebook_id) || !/^rs_[a-f0-9]{32}$/.test(row.source_id)
      || !positive(row.from_revision) || !positive(row.to_revision) || row.to_revision < row.from_revision || typeof row.from_expired !== 'boolean' || typeof row.to_expired !== 'boolean') throw new ContextStateError('原来源变化记录待核对')
  if (value.status === 'ready' && !value.materials.length) throw new ContextStateError('待更新的原研究材料不完整')
  return clone(value)
}
export function validateResearchContextReceipt(value: ResearchSessionContextReceipt, saved: ResearchSessionContextPending, target: ResearchContextTarget): ResearchSessionContextReceipt {
  if (!value || value.status !== 'completed' || !matches(value, target) || value.request_id !== saved.request_id || value.session_id !== saved.session_id || value.actor_id !== saved.actor_id
      || !nonnegative(value.context_revision) || !nonnegative(value.updated_count) || value.updated_count > 4096) throw new ContextStateError('原对话依据请求回执不一致')
  const view = validateResearchContext(value.view, target)
  if (view.context_revision < value.context_revision) throw new ContextStateError('原对话依据更新版本待核对')
  return { ...clone(value), view }
}
const status = (cause: unknown) => cause && typeof cause === 'object' && 'status' in cause ? Number(cause.status) : 0
export function researchContextError(cause: unknown): string {
  if (cause instanceof ContextStateError) return cause.message
  const code = cause && typeof cause === 'object' && 'code' in cause ? String(cause.code) : ''
  if (['source_unavailable', 'artifact_access_revoked', 'scope_denied', 'not_found', 'agent_inactive', 'execution_session_readonly', 'binding_conflict'].includes(code) || [401, 403, 404].includes(status(cause))) return '原对话或依据不可用，请核对当前身份'
  if (['source_stale', 'version_conflict', 'idempotency_conflict'].includes(code) || status(cause) === 409) return '对话依据已变化，请重新核对原记录'
  if (status(cause) === 422 || code === 'invalid_input') return '对话依据请求格式待核对'
  return '对话依据结果待核对，请检查原请求'
}
const accepted = (cause: unknown) => {
  if (!cause || typeof cause !== 'object') return false
  if ('accepted' in cause && cause.accepted === true) return true
  const receipt = 'receipt' in cause ? cause.receipt : null
  return Boolean(receipt && typeof receipt === 'object' && 'accepted' in receipt && receipt.accepted === true)
}
const definitive = (cause: unknown) => !accepted(cause) && status(cause) >= 400 && status(cause) < 500 && ![408, 425, 429].includes(status(cause))

export function useResearchContext({ api, namespace, target, connected, writable = true }: {
  api: ResearchSessionContextApi; namespace: string; target: ResearchContextTarget; connected: boolean; writable?: boolean
}) {
  const key = researchContextPendingKey(namespace, target), enabled = connected && writable && researchContextTargetValid(target)
  const [view, setView] = useState<ResearchSessionContextPreview | null>(null), [pending, setPending] = useState<ResearchSessionContextPending | null>(null)
  const [loading, setLoading] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState(''), [identityError, setIdentityError] = useState('')
  const [loaded, setLoaded] = useState<{ key: string; api: ResearchSessionContextApi } | null>(null)
  const alive = useRef(true), epoch = useRef(0), active = useRef({ api, key, enabled }), sequence = useRef(0)
  const reads = useRef(new Set<AbortController>()), posting = useRef<symbol | null>(null)
  const pendingRef = useRef<ResearchSessionContextPending | null>(null), viewRef = useRef<ResearchSessionContextPreview | null>(null)
  const pendingScope = useRef<{ key: string; api: ResearchSessionContextApi } | null>(null)
  active.current = { api, key, enabled }; viewRef.current = view
  const valid = (origin: typeof active.current, generation: number, signal?: AbortSignal) => alive.current && active.current.api === origin.api && active.current.key === origin.key && active.current.enabled && epoch.current === generation && !signal?.aborted
  const controller = () => { const value = new AbortController(); reads.current.add(value); return value }
  const savePending = (value: ResearchSessionContextPending | null) => { pendingScope.current = { key, api }; pendingRef.current = value; setPending(value) }
  const purge = () => { setView(null); viewRef.current = null; setLoaded(null) }
  const apply = (value: ResearchSessionContextPreview) => {
    const next = validateResearchContext(value, target)
    setView(next); viewRef.current = next; setLoaded({ key, api }); return next
  }
  const clear = (saved: ResearchSessionContextPending) => {
    const raw = localStorage.getItem(key)
    if (!raw || JSON.stringify(parseResearchContextPending(raw)) !== JSON.stringify(saved)) throw new ContextStateError('原对话依据请求编号已变化')
    localStorage.removeItem(key); savePending(null)
  }
  const refresh = async () => {
    if (!active.current.enabled || posting.current) return
    const origin = active.current, generation = epoch.current, serial = ++sequence.current, read = controller()
    setLoading(true); setError(''); setNotice(''); purge()
    try {
      const value = await api.getPersonalResearchSessionContext(target.sessionId, target.actorId, read.signal)
      if (!valid(origin, generation, read.signal) || serial !== sequence.current) return
      apply(value)
    } catch (cause) { if (valid(origin, generation, read.signal) && serial === sequence.current) { purge(); setError(researchContextError(cause)) } }
    finally { reads.current.delete(read); if (valid(origin, generation, read.signal) && serial === sequence.current) setLoading(false) }
  }
  const reconcile = async (initial?: ResearchSessionContextPending) => {
    const saved = initial || pendingRef.current
    if (!saved || !matches(saved, target) || !active.current.enabled || posting.current || pendingScope.current?.key !== key || pendingScope.current.api !== api) return
    const origin = active.current, generation = epoch.current, read = controller(), token = Symbol('context-readback')
    sequence.current += 1; posting.current = token; setBusy(true); setLoading(false); setError(''); purge()
    try {
      const value = await api.getPersonalResearchContextRequest(saved.request_id, read.signal)
      if (!valid(origin, generation, read.signal)) return
      if (!value || value.request_id !== saved.request_id || !['unknown', 'pending', 'completed', 'failed'].includes(value.status)) throw new ContextStateError('原对话依据请求回执不一致')
      if (value.status === 'completed') {
        const receipt = validateResearchContextReceipt(value.receipt!, saved, target)
        apply(receipt.view); clear(saved); setNotice(receipt.view.status === 'blocked' ? '原请求已完成，当前依据不可用' : '原请求已读回'); return
      }
      setNotice(value.status === 'failed' ? '原请求已失败，请核对原记录' : '结果待核对，未重新提交')
    } catch (cause) { if (valid(origin, generation, read.signal)) { purge(); setError(researchContextError(cause)) } }
    finally { reads.current.delete(read); if (posting.current === token) { posting.current = null; if (valid(origin, generation)) setBusy(false) } }
  }
  const update = async (preview: ResearchSessionContextPreview) => {
    if (!active.current.enabled || posting.current || pendingRef.current || identityError || loaded?.key !== key || loaded.api !== api) return false
    const current = viewRef.current
    if (!current || current.status !== 'ready' || current.plan_digest !== preview.plan_digest || current.context_revision !== preview.context_revision) return false
    const origin = active.current, generation = epoch.current, token = Symbol('context-write')
    const saved: ResearchSessionContextPending = { version: 1, operation: 'context_rollforward', request_id: crypto.randomUUID().replaceAll('-', ''), workspace_key: target.workspaceKey, session_id: target.sessionId, actor_id: target.actorId }
    const body: ResearchSessionContextWrite = { session_id: target.sessionId, actor_id: target.actorId, request_id: saved.request_id, expected_context_revision: current.context_revision, expected_plan_digest: current.plan_digest }
    let sent = false
    sequence.current += 1; posting.current = token; setBusy(true); setLoading(false); setError(''); setNotice('')
    try {
      validateResearchContext(preview, target)
      if (localStorage.getItem(key)) throw new ContextStateError('请先核对原对话依据请求')
      try { localStorage.setItem(key, JSON.stringify(saved)) } catch { throw new ContextStateError('请求状态保存失败，请重新核对后再提交') }
      savePending(saved); sent = true
      const value = await api.rollforwardPersonalResearchContext(clone(body))
      if (!valid(origin, generation)) return false
      const receipt = validateResearchContextReceipt(value, saved, target)
      if (receipt.context_revision < body.expected_context_revision) throw new ContextStateError('原对话依据更新版本待核对')
      apply(receipt.view); clear(saved); setNotice(receipt.view.status === 'blocked' ? '原请求已完成，当前依据不可用' : receipt.updated_count ? '已更新' : '无需更新'); return true
    } catch (cause) {
      if (!valid(origin, generation)) return false
      purge()
      if (sent && definitive(cause)) {
        try { clear(saved) } catch (identity) { setIdentityError(researchContextError(identity)); return false }
      }
      setError(researchContextError(cause)); return false
    } finally { if (posting.current === token) { posting.current = null; if (valid(origin, generation)) setBusy(false) } }
  }
  useEffect(() => { alive.current = true; return () => { alive.current = false; for (const read of reads.current) read.abort(); reads.current.clear() } }, [])
  useEffect(() => {
    epoch.current += 1; sequence.current += 1
    for (const read of reads.current) read.abort(); reads.current.clear()
    posting.current = null; purge(); savePending(null); setBusy(false); setLoading(false); setError(''); setNotice(''); setIdentityError('')
    let saved: ResearchSessionContextPending | null = null
    try {
      const raw = localStorage.getItem(key)
      if (raw) { saved = parseResearchContextPending(raw); if (!matches(saved, target)) throw new ContextStateError('原请求与当前对话身份不一致'); savePending(saved) }
    } catch (cause) { setIdentityError(researchContextError(cause)); return }
    if (enabled) { if (saved) void reconcile(saved); else void refresh() }
  }, [api, key, enabled])
  const scopeReady = enabled && loaded?.key === key && loaded.api === api
  return { view: scopeReady ? view : null, loading, busy, error, notice, identityError, pending: pendingScope.current?.key === key && pendingScope.current.api === api ? pending : null,
    refresh, reconcile: () => void reconcile(), update,
    canUpdate: Boolean(scopeReady && view?.status === 'ready' && !pending && !busy && !loading && !identityError),
    canRead: enabled && !busy && !loading && !identityError }
}
