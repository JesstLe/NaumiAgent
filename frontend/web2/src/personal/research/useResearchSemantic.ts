import { useEffect, useRef, useState } from 'react'
import type { ResearchSemanticApi, ResearchSemanticMutation, ResearchSemanticOperation, ResearchSemanticPending, ResearchSemanticPreview, ResearchSemanticReview, ResearchSemanticReviewer, ResearchSemanticStart } from '@naumi/shared/api/personalResearchSemantic'

const hash = /^[a-f0-9]{64}$/
const actor = /^(main|pa_[a-f0-9]{32})$/
const leaf = /^pa_[a-f0-9]{32}$/
const notebook = /^rq_[a-f0-9]{32}$/
const reviewId = /^rsv_[a-f0-9]{32}$/
const opaque = /^[A-Za-z0-9_.:/-]{1,128}$/
const requestId = /^[a-f0-9]{32}$/
const positive = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) > 0
const nonnegative = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) >= 0
const text = (value: unknown, limit: number, empty = false): value is string => typeof value === 'string' && value.length <= limit && (empty || value.trim().length > 0)
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T
class SemanticStateError extends Error {}
export interface ResearchSemanticTarget { workspaceKey: string; authorId: string; notebookId: string; revision: number; sourceSessionId: string; claimIds: string[] }
export const researchSemanticPendingKey = (namespace: string, target: ResearchSemanticTarget) => `naumi:research:semantic-pending:v1:${[namespace, target.workspaceKey, target.authorId, target.notebookId, String(target.revision), target.sourceSessionId].map(encodeURIComponent).join(':')}`
const targetValid = (target: ResearchSemanticTarget) => hash.test(target.workspaceKey) && actor.test(target.authorId) && notebook.test(target.notebookId) && positive(target.revision) && (!target.sourceSessionId || opaque.test(target.sourceSessionId)) && new Set(target.claimIds).size === target.claimIds.length
const scopeMatches = (row: { workspace_key: string; author_id: string; notebook_id: string; notebook_revision: number }, target: ResearchSemanticTarget) => row.workspace_key === target.workspaceKey && row.author_id === target.authorId && row.notebook_id === target.notebookId && row.notebook_revision === target.revision
const reviewerValid = (value: ResearchSemanticReviewer) => Boolean(value && leaf.test(value.id) && positive(value.version) && text(value.name, 256))
export const semanticActive = (value: ResearchSemanticReview | null) => Boolean(value && ['proposed', 'admitted', 'running'].includes(value.execution_status))

export function parseResearchSemanticPending(raw: string): ResearchSemanticPending {
  let value: unknown
  try { value = JSON.parse(raw) } catch { throw new SemanticStateError('原复核请求身份待核对') }
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new SemanticStateError('原复核请求身份待核对')
  const row = value as ResearchSemanticPending
  if (Object.keys(row).sort().join(',') !== 'author_id,notebook_id,notebook_revision,operation,request_id,review_id,reviewer_id,reviewer_version,source_session_id,version,workspace_key'
      || row.version !== 1 || !['semantic_start', 'semantic_reconcile', 'semantic_cancel'].includes(row.operation) || !requestId.test(row.request_id)
      || !hash.test(row.workspace_key) || !actor.test(row.author_id) || !notebook.test(row.notebook_id) || !positive(row.notebook_revision)
      || !opaque.test(row.source_session_id) || !leaf.test(row.reviewer_id) || !positive(row.reviewer_version)
      || (row.review_id !== '' && !reviewId.test(row.review_id)) || (row.operation !== 'semantic_start' && !reviewId.test(row.review_id))) throw new SemanticStateError('原复核请求身份待核对')
  return clone(row)
}
export function validateResearchSemanticPreview(value: ResearchSemanticPreview, target: ResearchSemanticTarget, reviewer: ResearchSemanticReviewer): ResearchSemanticPreview {
  if (!value || !scopeMatches(value, target) || value.source_session_id !== target.sourceSessionId || !reviewerValid(value.reviewer)
      || value.reviewer.id !== reviewer.id || value.reviewer.version !== reviewer.version || value.reviewer.id === target.authorId || !hash.test(value.input_digest)
      || !Array.isArray(value.sources) || value.sources.length > 256 || !Array.isArray(value.materials) || value.materials.length > 4096
      || !nonnegative(value.memory_count) || typeof value.can_start !== 'boolean' || !Array.isArray(value.blocking_codes)
      || value.blocking_codes.some(code => !text(code, 100))) throw new SemanticStateError('复核预览与原研究或 Agent 不一致')
  const sources = new Set<string>()
  for (const source of value.sources) {
    const key = `${source.id}:${source.revision}`
    if (!/^rs_[a-f0-9]{32}$/.test(source.id) || !positive(source.revision) || !text(source.title, 1000, true) || !hash.test(source.body_sha256) || !nonnegative(source.bytes) || sources.has(key)) throw new SemanticStateError('原复核来源版本待核对')
    sources.add(key)
  }
  const materials = new Set<string>()
  for (const material of value.materials) {
    const key = `${material.artifact_id}:${material.version}`
    if (!/^art_[a-f0-9]{32}$/.test(material.artifact_id) || !positive(material.version) || !text(material.title, 1000, true) || !actor.test(material.owner_id)
        || !['existing', 'requires_owner_confirmation', 'missing_owner_grant'].includes(material.sharing) || materials.has(key)) throw new SemanticStateError('原资料共享范围待核对')
    materials.add(key)
  }
  if (value.can_start && (value.blocking_codes.length || value.materials.some(row => row.sharing === 'missing_owner_grant'))) throw new SemanticStateError('原资料授权状态待核对')
  return clone(value)
}
export function validateResearchSemanticReview(value: ResearchSemanticReview, target: ResearchSemanticTarget, expectedId?: string, expectedReviewer?: ResearchSemanticReviewer, expectedDigest?: string, original?: ResearchSemanticReview): ResearchSemanticReview {
  if (!value || !reviewId.test(value.id) || expectedId && value.id !== expectedId || !scopeMatches(value, target) || !requestId.test(value.request_id)
      || !hash.test(value.input_digest) || expectedDigest && value.input_digest !== expectedDigest || !reviewerValid(value.reviewer) || value.reviewer.id === target.authorId
      || expectedReviewer && (value.reviewer.id !== expectedReviewer.id || value.reviewer.version !== expectedReviewer.version)
      || value.step_id !== 'review' || !['proposed', 'admitted', 'running', 'completed', 'failed', 'unknown', 'cancelled'].includes(value.execution_status)
      || !['current', 'stale', 'unavailable'].includes(value.basis_status)
      || ['collaboration_id', 'job_id', 'session_id', 'run_id'].some(key => { const id = value[key as keyof ResearchSemanticReview]; return typeof id !== 'string' || id !== '' && !opaque.test(id) })) throw new SemanticStateError('复核执行与原研究不一致')
  if (original && (value.id !== original.id || value.request_id !== original.request_id || value.input_digest !== original.input_digest
      || value.reviewer.id !== original.reviewer.id || value.reviewer.version !== original.reviewer.version
      || (['collaboration_id', 'job_id', 'session_id', 'run_id'] as const).some(key => Boolean(original[key]) && value[key] !== original[key])
      || original.result && value.result && original.result.digest !== value.result.digest)) throw new SemanticStateError('原复核执行身份已变化')
  if (value.basis_status === 'unavailable') { const result = clone(value); delete result.result; return result }
  if (value.execution_status === 'completed' && !value.result) throw new SemanticStateError('原复核结果尚未完整读回')
  if (value.result) {
    const result = value.result
    if (value.execution_status !== 'completed' || !hash.test(result.digest) || result.verification_scope !== 'provided_sources' || result.independently_reviewed !== true
        || !text(result.summary, 12000, true) || !Array.isArray(result.verdicts) || result.verdicts.length !== target.claimIds.length) throw new SemanticStateError('逐条复核结果待核对')
    const claims = new Set<string>()
    for (const verdict of result.verdicts) {
      if (!target.claimIds.includes(verdict.claim_id) || claims.has(verdict.claim_id) || !['supported', 'contradicted', 'insufficient'].includes(verdict.verdict)
          || !text(verdict.reason, 12000) || !Array.isArray(verdict.evidence) || verdict.evidence.length > 256 || verdict.verdict !== 'insufficient' && !verdict.evidence.length) throw new SemanticStateError('声明复核覆盖不完整')
      claims.add(verdict.claim_id)
      for (const anchor of verdict.evidence) {
        if (!/^rs_[a-f0-9]{32}$/.test(anchor.source_id) || !positive(anchor.source_revision) || !hash.test(anchor.body_sha256) || !text(anchor.quote, 12000)
            || !nonnegative(anchor.start) || !positive(anchor.end) || anchor.end <= anchor.start || !nonnegative(anchor.start_byte) || !positive(anchor.end_byte) || anchor.end_byte <= anchor.start_byte
            || new TextEncoder().encode(anchor.quote).length !== anchor.end_byte - anchor.start_byte || Array.from(anchor.quote).length !== anchor.end - anchor.start) throw new SemanticStateError('复核引文与确切位置不一致')
      }
    }
    if (result.artifact_ref) {
      const ref = result.artifact_ref
      if (!/^art_[a-f0-9]{32}$/.test(ref.artifact_id) || !positive(ref.version) || !hash.test(ref.sha256) || !/^(|asg_[a-f0-9]{32})$/.test(ref.grant_id) || !nonnegative(ref.grant_revision) || Boolean(ref.grant_id) !== (ref.grant_revision > 0)) throw new SemanticStateError('原复核产物引用待核对')
    }
  }
  if (value.execution_status === 'completed' && (!value.collaboration_id || !value.job_id || !value.session_id || !value.run_id)) throw new SemanticStateError('原复核执行收据不完整')
  return clone(value)
}
const status = (cause: unknown) => cause && typeof cause === 'object' && 'status' in cause ? Number(cause.status) : 0
const accepted = (cause: unknown) => {
  if (!cause || typeof cause !== 'object') return false
  if ('accepted' in cause && cause.accepted === true) return true
  const receipt = 'receipt' in cause ? cause.receipt : null
  return Boolean(receipt && typeof receipt === 'object' && 'accepted' in receipt && receipt.accepted === true)
}
const definitive = (cause: unknown) => !accepted(cause) && status(cause) >= 400 && status(cause) < 500 && ![408, 425, 429].includes(status(cause))
export function researchSemanticError(cause: unknown) {
  if (cause instanceof SemanticStateError) return cause.message
  const code = cause && typeof cause === 'object' && 'code' in cause ? String(cause.code) : ''
  if (['source_stale', 'version_conflict', 'idempotency_conflict'].includes(code)) return '研究或 Agent 已变化，请重新核对原记录'
  if (['source_unavailable', 'artifact_access_revoked', 'scope_denied', 'not_found', 'agent_inactive', 'binding_conflict', 'execution_session_readonly'].includes(code) || [401, 403, 404].includes(status(cause))) return '原研究或 Agent 依据不可用'
  if (code === 'research_busy') return '原对话正在处理其他操作，请先核对原执行'
  if (status(cause) === 422 || code === 'invalid_input') return '原复核请求格式待核对'
  return '复核结果待核对，请查询原请求'
}

export function useResearchSemantic({ api, namespace, target, reviewer, connected, writable = true }: {
  api: ResearchSemanticApi; namespace: string; target: ResearchSemanticTarget; reviewer: ResearchSemanticReviewer | null; connected: boolean; writable?: boolean
}) {
  const key = researchSemanticPendingKey(namespace, target), enabled = connected && targetValid(target)
  const [preview, setPreview] = useState<ResearchSemanticPreview | null>(null), [reviews, setReviews] = useState<ResearchSemanticReview[]>([])
  const [selected, setSelected] = useState(''), [pending, setPending] = useState<ResearchSemanticPending | null>(null)
  const [loading, setLoading] = useState(false), [previewLoading, setPreviewLoading] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState(''), [identityError, setIdentityError] = useState('')
  const [loaded, setLoaded] = useState<{ key: string; api: ResearchSemanticApi; reviewerId: string; reviewerVersion: number } | null>(null)
  const [readScope, setReadScope] = useState<{ key: string; api: ResearchSemanticApi } | null>(null)
  const alive = useRef(true), epoch = useRef(0), active = useRef({ api, key, enabled }), readSequence = useRef(0), previewSequence = useRef(0)
  const reads = useRef(new Set<AbortController>()), posting = useRef<symbol | null>(null), pendingRef = useRef<ResearchSemanticPending | null>(null), previewRef = useRef(preview)
  const pendingScope = useRef<{ api: ResearchSemanticApi; key: string } | null>(null), selectedRef = useRef(selected), reviewerRef = useRef(reviewer), writableRef = useRef(writable)
  const controlIdentity = `${writable}:${reviewer?.id || ''}:${reviewer?.version || 0}`
  const controls = useRef({ identity: controlIdentity, generation: 0 })
  if (controls.current.identity !== controlIdentity) controls.current = { identity: controlIdentity, generation: controls.current.generation + 1 }
  active.current = { api, key, enabled }; previewRef.current = preview; selectedRef.current = selected; reviewerRef.current = reviewer; writableRef.current = writable
  const valid = (origin: typeof active.current, generation: number, signal?: AbortSignal) => alive.current && active.current.api === origin.api && active.current.key === origin.key && active.current.enabled && epoch.current === generation && !signal?.aborted
  const controller = () => { const read = new AbortController(); reads.current.add(read); return read }
  const savePending = (value: ResearchSemanticPending | null) => { pendingScope.current = { api, key }; pendingRef.current = value; setPending(value) }
  const clearPending = (saved: ResearchSemanticPending) => {
    const raw = localStorage.getItem(key)
    if (!raw || JSON.stringify(parseResearchSemanticPending(raw)) !== JSON.stringify(saved)) throw new SemanticStateError('原复核请求身份已变化')
    localStorage.removeItem(key); savePending(null)
  }
  const purge = () => { setPreview(null); previewRef.current = null; setLoaded(null); setReadScope(null); setReviews([]) }
  const apply = (value: ResearchSemanticReview, expectedId?: string, expectedReviewer?: ResearchSemanticReviewer, expectedDigest?: string) => {
    const next = validateResearchSemanticReview(value, target, expectedId, expectedReviewer, expectedDigest)
    setReadScope({ api, key }); setReviews(previous => [next, ...previous.filter(row => row.id !== next.id)]); setSelected(next.id); selectedRef.current = next.id
    return next
  }
  const refresh = async () => {
    if (!active.current.enabled || posting.current) return
    const origin = active.current, generation = epoch.current, sequence = ++readSequence.current, read = controller()
    setLoading(true); setError('')
    try {
      const result = await api.listPersonalResearchSemanticReviews(target.notebookId, read.signal)
      if (!valid(origin, generation, read.signal) || sequence !== readSequence.current) return
      if (!result || !Array.isArray(result.reviews) || result.reviews.length > 256 || result.reviews.some(row => !row || row.workspace_key !== target.workspaceKey || row.author_id !== target.authorId || row.notebook_id !== target.notebookId)) throw new SemanticStateError('复核目录与原研究不一致')
      const values = result.reviews.filter(row => row.notebook_revision === target.revision).map(row => validateResearchSemanticReview(row, target, undefined, undefined, undefined, reviews.find(original => original.id === row.id)))
      if (new Set(values.map(row => row.id)).size !== values.length) throw new SemanticStateError('原复核目录身份重复')
      setReadScope({ api, key }); setReviews(values); setSelected(previous => values.some(row => row.id === previous) ? previous : values[0]?.id || '')
    } catch (cause) { if (valid(origin, generation, read.signal) && sequence === readSequence.current) { setReviews([]); setError(researchSemanticError(cause)) } }
    finally { reads.current.delete(read); if (valid(origin, generation, read.signal) && sequence === readSequence.current) setLoading(false) }
  }
  const readReview = async (id: string) => {
    if (!active.current.enabled || posting.current || !reviewId.test(id)) return
    const origin = active.current, generation = epoch.current, sequence = ++readSequence.current, read = controller()
    try {
      const result = await api.getPersonalResearchSemanticReview(id, read.signal)
      if (!valid(origin, generation, read.signal) || sequence !== readSequence.current || selectedRef.current !== id) return
      const next = validateResearchSemanticReview(result, target, id, undefined, undefined, reviews.find(row => row.id === id))
      apply(next, id); setError('')
    } catch (cause) { if (valid(origin, generation, read.signal) && sequence === readSequence.current) { setReviews(previous => previous.filter(row => row.id !== id)); setError(researchSemanticError(cause)) } }
    finally { reads.current.delete(read) }
  }
  const loadPreview = async () => {
    const current = reviewerRef.current
    if (!active.current.enabled || posting.current || !writableRef.current || !target.sourceSessionId || !current || !reviewerValid(current)) return
    const origin = active.current, generation = epoch.current, sequence = ++previewSequence.current, read = controller()
    setPreviewLoading(true); setPreview(null); previewRef.current = null; setLoaded(null); setError('')
    try {
      const value = await api.getPersonalResearchSemanticContext(target.notebookId, target.sourceSessionId, current.id, read.signal)
      if (!valid(origin, generation, read.signal) || sequence !== previewSequence.current || reviewerRef.current?.id !== current.id || reviewerRef.current.version !== current.version || !writableRef.current) return
      const next = validateResearchSemanticPreview(value, target, current)
      setPreview(next); previewRef.current = next; setLoaded({ api, key, reviewerId: current.id, reviewerVersion: current.version })
    } catch (cause) { if (valid(origin, generation, read.signal) && sequence === previewSequence.current) { setPreview(null); setLoaded(null); setError(researchSemanticError(cause)) } }
    finally { reads.current.delete(read); if (valid(origin, generation, read.signal) && sequence === previewSequence.current) setPreviewLoading(false) }
  }
  const reconcileRequest = async (initial?: ResearchSemanticPending) => {
    const saved = initial || pendingRef.current
    if (!active.current.enabled || posting.current || !saved || !scopeMatches(saved, target) || saved.source_session_id !== target.sourceSessionId || pendingScope.current?.api !== api || pendingScope.current.key !== key) return
    const origin = active.current, generation = epoch.current, read = controller(), token = Symbol('semantic-original-request')
    posting.current = token; setBusy(true); setError(''); purge()
    try {
      const result = await api.getPersonalResearchSemanticRequest(saved.request_id, read.signal)
      if (!valid(origin, generation, read.signal)) return
      if (!result || result.request_id !== saved.request_id || !['unknown', 'pending', 'completed'].includes(result.status)) throw new SemanticStateError('原复核请求回执不一致')
      if (result.status === 'completed' && result.review) {
        const value = validateResearchSemanticReview(result.review, target, saved.review_id || undefined, { id: saved.reviewer_id, version: saved.reviewer_version, name: result.review.reviewer?.name || '' })
        if (saved.operation === 'semantic_start' && value.request_id !== saved.request_id) throw new SemanticStateError('原复核启动收据不一致')
        clearPending(saved); apply(value)
      } else setError(result.status === 'pending' ? '原复核请求尚待核对' : '原复核结果待核对')
    } catch (cause) { if (valid(origin, generation, read.signal)) { purge(); setError(researchSemanticError(cause)) } }
    finally { reads.current.delete(read); if (posting.current === token) { posting.current = null; if (valid(origin, generation)) setBusy(false) } }
  }
  const mutate = async (operation: ResearchSemanticOperation, frozen?: ResearchSemanticPreview, expectedReview?: ResearchSemanticReview) => {
    const current = reviewerRef.current
    if (!active.current.enabled || !writableRef.current || !target.sourceSessionId || posting.current || pendingRef.current || identityError) return false
    let expected: ResearchSemanticReviewer, body: ResearchSemanticStart | ResearchSemanticMutation
    const id = crypto.randomUUID().replaceAll('-', '')
    if (operation === 'semantic_start') {
      const value = previewRef.current
      if (!current || !frozen || !value || !value.can_start || loaded?.api !== api || loaded.key !== key || loaded.reviewerId !== current.id || loaded.reviewerVersion !== current.version
          || value.input_digest !== frozen.input_digest || value.reviewer.id !== current.id || value.reviewer.version !== current.version) return false
      expected = current
      body = { id: target.notebookId, source_session_id: target.sourceSessionId, reviewer_agent_id: current.id, expected_reviewer_version: current.version,
        expected_revision: target.revision, expected_input_digest: value.input_digest, request_id: id }
    } else {
      if (!expectedReview || expectedReview.id !== selectedRef.current || expectedReview.basis_status === 'unavailable') return false
      if (operation === 'semantic_cancel' && !semanticActive(expectedReview) || operation === 'semantic_reconcile' && expectedReview.execution_status !== 'unknown') return false
      expected = expectedReview.reviewer; body = { id: expectedReview.id, request_id: id }
    }
    const saved: ResearchSemanticPending = { version: 1, operation, request_id: id, workspace_key: target.workspaceKey, author_id: target.authorId, notebook_id: target.notebookId,
      notebook_revision: target.revision, source_session_id: target.sourceSessionId, reviewer_id: expected.id, reviewer_version: expected.version, review_id: expectedReview?.id || '' }
    const origin = active.current, generation = epoch.current, controlGeneration = controls.current.generation, token = Symbol('semantic-write')
    const mutationValid = () => valid(origin, generation) && writableRef.current && controls.current.generation === controlGeneration
    posting.current = token; setBusy(true); setError(''); readSequence.current++; previewSequence.current++
    let sent = false
    try {
      const raw = localStorage.getItem(key)
      if (raw) throw new SemanticStateError('原复核请求尚待核对')
      localStorage.setItem(key, JSON.stringify(saved)); savePending(saved); sent = true
      const result = operation === 'semantic_start' ? await api.startPersonalResearchSemanticReview(clone(body as ResearchSemanticStart)) : await api.operatePersonalResearchSemanticReview(operation, clone(body as ResearchSemanticMutation))
      if (!mutationValid()) return false
      const value = validateResearchSemanticReview(result, target, expectedReview?.id, expected, frozen?.input_digest, expectedReview)
      if (operation === 'semantic_start' && value.request_id !== id) throw new SemanticStateError('原复核启动收据不一致')
      clearPending(saved); apply(value, expectedReview?.id, expected, frozen?.input_digest); setPreview(null); previewRef.current = null; setLoaded(null)
      return true
    } catch (cause) {
      if (!mutationValid()) return false
      if (!sent) setIdentityError('原请求状态保存失败，请核对浏览器存储')
      else if (definitive(cause)) {
        try { clearPending(saved) } catch { setIdentityError('原复核请求身份已变化'); return false }
      }
      setPreview(null); previewRef.current = null; setLoaded(null); setError(researchSemanticError(cause)); return false
    } finally { if (posting.current === token) { posting.current = null; if (valid(origin, generation)) setBusy(false) } }
  }
  useEffect(() => { alive.current = true; return () => { alive.current = false; for (const read of reads.current) read.abort(); reads.current.clear() } }, [])
  useEffect(() => {
    epoch.current++; readSequence.current++; previewSequence.current++; for (const read of reads.current) read.abort(); reads.current.clear()
    posting.current = null; savePending(null); purge(); setSelected(''); setBusy(false); setLoading(false); setPreviewLoading(false); setError(''); setIdentityError('')
    let saved: ResearchSemanticPending | null = null
    try {
      const raw = localStorage.getItem(key)
      if (raw) { saved = parseResearchSemanticPending(raw); if (!scopeMatches(saved, target) || saved.source_session_id !== target.sourceSessionId) throw new SemanticStateError('原复核请求与当前研究不一致'); savePending(saved) }
    } catch (cause) { setIdentityError(researchSemanticError(cause)); return }
    if (enabled) { if (saved) void reconcileRequest(saved); else void refresh() }
    return () => { epoch.current++; for (const read of reads.current) read.abort(); reads.current.clear() }
  }, [api, key, enabled])
  useEffect(() => {
    previewSequence.current++; setPreview(null); previewRef.current = null; setLoaded(null); setPreviewLoading(false)
    if (enabled && writable && target.sourceSessionId && reviewer && !pendingRef.current) void loadPreview()
  }, [api, key, enabled, writable, reviewer?.id, reviewer?.version])
  const readable = enabled && readScope?.key === key && readScope.api === api
  const selectedReview = readable ? reviews.find(row => row.id === selected) || null : null
  useEffect(() => {
    if (!enabled || pending || !semanticActive(selectedReview) || !selectedReview) return
    let stopped = false, timer: ReturnType<typeof setTimeout> | undefined
    const schedule = () => { if (!stopped && document.visibilityState !== 'hidden') timer = setTimeout(() => { void readReview(selectedReview.id) }, 1800) }
    const visible = () => { clearTimeout(timer); if (document.visibilityState !== 'hidden') void readReview(selectedReview.id) }
    schedule(); document.addEventListener('visibilitychange', visible)
    return () => { stopped = true; clearTimeout(timer); document.removeEventListener('visibilitychange', visible) }
  }, [api, key, enabled, pending, selectedReview])
  const previewReady = enabled && writable && loaded?.api === api && loaded.key === key && loaded.reviewerId === reviewer?.id && loaded.reviewerVersion === reviewer?.version
  const scopedPending = pendingScope.current?.key === key && pendingScope.current.api === api ? pending : null
  return { preview: previewReady ? preview : null, reviews: readable ? reviews : [], selectedReview, loading, previewLoading, busy, error, identityError, pending: scopedPending,
    refresh, refreshPreview: loadPreview, reconcileRequest: () => void reconcileRequest(), select: (id: string) => { if (!posting.current) { setSelected(id); selectedRef.current = id; void readReview(id) } },
    start: (value: ResearchSemanticPreview) => mutate('semantic_start', value), reconcile: (value: ResearchSemanticReview) => mutate('semantic_reconcile', undefined, value), cancel: (value: ResearchSemanticReview) => mutate('semantic_cancel', undefined, value),
    canStart: Boolean(previewReady && preview?.can_start && !scopedPending && !busy && !loading && !previewLoading && !identityError),
    canRead: enabled && !busy && !loading && !identityError, canMutate: enabled && writable && Boolean(target.sourceSessionId) && !scopedPending && !busy && !identityError }
}
