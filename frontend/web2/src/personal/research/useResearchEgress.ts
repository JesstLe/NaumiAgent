import { useEffect, useRef, useState } from 'react'
import type { ResearchEgressApi, ResearchEgressDecision, ResearchEgressMutation, ResearchEgressPage, ResearchEgressPending, ResearchEgressProposal, ResearchEgressRequest, ResearchEgressStatus, ResearchEgressView } from '@naumi/shared/api/personalResearchEgress'

const idPattern = /^rx_[a-f0-9]{32}$/
const digestPattern = /^[a-f0-9]{64}$/
const statuses = new Set<ResearchEgressStatus>(['proposed', 'pending', 'approved', 'started', 'completed', 'rejected', 'revoked', 'expired', 'stale', 'unavailable', 'unknown'])
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T
export const researchEgressPendingKey = (namespace: string) => `naumi:research:egress-pending:v1:${encodeURIComponent(namespace)}`
export const egressRequestId = () => crypto.randomUUID().replaceAll('-', '')

export function parseResearchEgressPending(raw: string): ResearchEgressPending {
  let value: unknown
  try { value = JSON.parse(raw) } catch { throw new Error('原外发请求状态待核对') }
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('原外发请求状态待核对')
  const row = value as Record<string, unknown>
  if (Object.keys(row).sort().join(',') !== 'operation,proposal_id,request_id,version,workspace_key' || row.version !== 1
      || typeof row.request_id !== 'string' || !/^[a-f0-9]{32}$/.test(row.request_id)
      || !['egress_decide', 'egress_execute'].includes(String(row.operation)) || typeof row.proposal_id !== 'string' || !idPattern.test(row.proposal_id)
      || typeof row.workspace_key !== 'string' || !digestPattern.test(row.workspace_key)) throw new Error('原外发请求状态待核对')
  return row as unknown as ResearchEgressPending
}

function positive(value: unknown) { return typeof value === 'number' && Number.isSafeInteger(value) && value > 0 }
function text(value: unknown, maximum: number) { return typeof value === 'string' && value.length <= maximum && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(value) }
function pairs(value: unknown) { return value !== null && typeof value === 'object' && !Array.isArray(value) && Object.entries(value).length <= 64 && Object.entries(value).every(([key, item]) => text(key, 256) && text(item, 8192)) }

export function validateResearchEgress(value: ResearchEgressView, expectedId?: string, detail = true): ResearchEgressView {
  const row = value?.proposal
  if (!row || !idPattern.test(row.id) || expectedId && row.id !== expectedId || !positive(row.revision)
      || !statuses.has(row.status) || !['available', 'source_unavailable'].includes(value.availability)) throw new Error('外发记录身份待核对')
  if (value.availability === 'source_unavailable' || row.status === 'unavailable') {
    return { availability: 'source_unavailable', proposal: { id: row.id, revision: row.revision, digest: '', status: 'unavailable' } }
  }
  if (!digestPattern.test(row.digest)) throw new Error('外发内容摘要待核对')
  if (row.actor_id !== undefined && !/^(main|pa_[a-f0-9]{32})$/.test(row.actor_id)
      || row.notebook_id !== undefined && !/^rq_[a-f0-9]{32}$/.test(row.notebook_id)
      || row.action !== undefined && !['search', 'capture', 'recheck'].includes(row.action)) throw new Error('外发记录作用范围待核对')
  if (!detail) {
    return { availability: 'available', proposal: { id: row.id, revision: row.revision, digest: row.digest, status: row.status,
      actor_id: row.actor_id, actor_name: typeof row.actor_name === 'string' ? row.actor_name : undefined,
      actor_version: row.actor_version, notebook_id: row.notebook_id, notebook_revision: row.notebook_revision,
      notebook_title: typeof row.notebook_title === 'string' ? row.notebook_title : undefined, action: row.action,
      created_at: row.created_at, expires_at: row.expires_at } }
  }
  const plan = row.destination_plan
  if (!row.actor_id || !text(row.actor_name, 160) || !row.notebook_id || !positive(row.notebook_revision)
      || !text(row.notebook_title, 160) || !text(row.session_id, 200) || !text(row.reason, 4000)
      || !row.action || !row.payload || !plan || !text(row.expires_at, 64) || !Number.isFinite(Date.parse(row.expires_at!))
      || !['search', 'url'].includes(plan.kind) || !['duckduckgo', 'brave', 'direct'].includes(plan.provider)
      || !text(plan.endpoint, 8192) || !['POST', 'GET'].includes(plan.method) || !pairs(plan.parameters) || !pairs(plan.headers)
      || plan.redirect_policy !== 'none' || plan.max_redirects !== 0 || plan.trust_env !== false || plan.tls_verify !== true
      || !['form-url-encoded', 'query', 'none'].includes(plan.request_encoding) || !digestPattern.test(plan.routing_digest)) throw new Error('外发内容或接收方待核对')
  if (row.action === 'search') {
    if (!('query' in row.payload) || !text(row.payload.query, 400) || !row.payload.query.trim() || plan.kind !== 'search') throw new Error('检索内容待核对')
  } else if (!('url' in row.payload) || !text(row.payload.url, 8192) || !row.payload.url || plan.kind !== 'url') throw new Error('网页请求待核对')
  if (row.approval && (!text(row.approval.decision_id, 200) || !digestPattern.test(row.approval.digest) || !positive(row.approval.proposal_revision)
      || !text(row.approval.approved_at, 64) || !Number.isFinite(Date.parse(row.approval.approved_at)) || !text(row.approval.channel, 32))) throw new Error('原外发审批凭据待核对')
  if (row.execution && (!text(row.execution.request_id, 200) || !['started', 'completed'].includes(row.execution.status)
      || !text(row.execution.started_at, 64) || !Number.isFinite(Date.parse(row.execution.started_at)))) throw new Error('原执行回执待核对')
  if (value.result) {
    const result = 'notebook' in value.result ? value.result : value.result.view
    if (!result?.notebook || result.notebook.id !== row.notebook_id || result.notebook.actor_id && result.notebook.actor_id !== row.actor_id
        || !Array.isArray(result.sources) || !('notebook' in value.result) && (!value.result.discovery || !Array.isArray(value.result.discovery.items) || value.result.discovery.items.length > 10
          || value.result.discovery.items.some(item => !text(item.title, 2000) || !text(item.url, 8192) || !text(item.snippet ?? '', 16000)))) throw new Error('原外发结果身份待核对')
  }
  try { const url = new URL(plan.endpoint); if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error() } catch { throw new Error('外发接收方待核对') }
  return clone(value)
}

export function researchEgressError(cause: unknown) {
  const value = cause && typeof cause === 'object' ? cause as { code?: string; error?: string; status?: number } : {}
  const code = value.code || value.error || ''
  const messages: Record<string, string> = {
    version_conflict: '原外发记录已变化，请重新审阅', idempotency_conflict: '原请求编号已绑定不同内容，请核对原请求',
    source_stale: '原外发依据已变化，请重新审阅', source_unavailable: '原外发依据暂不可读取',
    egress_expired: '外发审批已过期', egress_revoked: '原外发批准已撤销', egress_unapproved: '请先批准这条确切请求',
    egress_used: '原请求已经使用，请核对原执行结果', request_pending: '原外发请求仍待核对', storage_error: '外发记录校验未通过，请恢复原资料后核对',
    approval_required: '请先批准这条确切外发请求', approval_invalidated: '原批准的依据已变化，请重新审阅',
    action_expired: '原外发请求已过期', action_locked: '原外发请求已锁定，请核对原批准或执行结果',
    lease_lost: '原发送结果仍待核对，请保留原请求编号', artifact_access_revoked: '原资料授权已撤销，请重新核对',
    binding_conflict: '原外发记录的身份已变化，请重新核对', agent_inactive: '原 Agent 已暂停或归档',
    invalid_input: '外发参数不完整或格式不正确', scope_denied: '外发记录与当前入口不一致', not_found: '原外发记录暂不可读取',
  }
  if (messages[code]) return messages[code]
  if ([401, 403, 404].includes(Number(value.status))) return '原外发记录或授权暂不可用'
  return '外发操作结果待核对，请检查原请求'
}

type Frozen = { key: string; saved: ResearchEgressPending; body: Record<string, unknown> }
type CapturedResume = { key: string; saved: ResearchEgressPending; arguments: NonNullable<ResearchEgressRequest['resume']>['arguments'] }
export function validateCapturedEgressResume(receipt: ResearchEgressRequest, saved: ResearchEgressPending) {
  const resume = receipt.resume, row = receipt.view?.proposal, fields = resume?.arguments
  if (receipt.status !== 'pending' || saved.operation !== 'egress_execute' || receipt.request_id !== saved.request_id
      || receipt.view?.availability !== 'available' || row?.id !== saved.proposal_id || row.execution?.phase !== 'captured' || row.execution.saved_stage !== true
      || resume?.operation !== 'egress_execute' || !fields || Object.keys(fields).sort().join(',') !== 'expected_digest,expected_revision,id,request_id'
      || fields.id !== saved.proposal_id || fields.request_id !== saved.request_id || !positive(fields.expected_revision)
      || fields.expected_revision > row.revision || !digestPattern.test(fields.expected_digest) || fields.expected_digest !== row.digest) throw new Error('原已接收结果的保存凭据待核对')
  return clone(fields)
}
export function useResearchEgress({ api, namespace, workspaceKey, connected }: {
  api: ResearchEgressApi; namespace: string; workspaceKey: string; connected: boolean
}) {
  const key = researchEgressPendingKey(namespace)
  const scope = `${key}:${workspaceKey}`
  const [rows, setRows] = useState<ResearchEgressView[]>([]), [nextAfter, setNextAfter] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState(''), [detail, setDetail] = useState<ResearchEgressView | null>(null)
  const [loading, setLoading] = useState(false), [detailLoading, setDetailLoading] = useState(false), [busy, setBusy] = useState(false)
  const [error, setError] = useState(''), [notice, setNotice] = useState(''), [identityError, setIdentityError] = useState('')
  const [pending, setPending] = useState<ResearchEgressPending | null>(null)
  const [capturedResume, setCapturedResume] = useState<CapturedResume | null>(null)
  const [loadedScope, setLoadedScope] = useState<{ scope: string; api: ResearchEgressApi } | null>(null)
  const [revisionToken, setRevisionToken] = useState(0)
  const mounted = useRef(true), epoch = useRef(0), active = useRef({ api, scope, connected })
  const reads = useRef(new Set<AbortController>()), selected = useRef(''), detailSequence = useRef(0), listSequence = useRef(0)
  const currentDetail = useRef<ResearchEgressView | null>(null)
  const posting = useRef<symbol | null>(null), pendingRef = useRef<ResearchEgressPending | null>(null), frozen = useRef<Frozen | null>(null)
  const resumeRef = useRef<CapturedResume | null>(null)
  active.current = { api, scope, connected }; selected.current = selectedId; currentDetail.current = detail
  const valid = (origin: typeof active.current, generation: number, signal?: AbortSignal) => mounted.current && active.current.api === origin.api && active.current.scope === origin.scope && active.current.connected && epoch.current === generation && !signal?.aborted
  const controller = () => { const value = new AbortController(); reads.current.add(value); return value }
  const setPendingValue = (value: ResearchEgressPending | null) => { pendingRef.current = value; setPending(value) }
  const setResume = (value: CapturedResume | null) => { resumeRef.current = value; setCapturedResume(value) }
  const purge = (id?: string) => {
    detailSequence.current += 1; setDetail(null); setDetailLoading(false); frozen.current = null; setResume(null)
    if (!id) setRows([])
    else setRows(values => values.map(row => row.proposal.id === id ? { availability: 'source_unavailable', proposal: { id, revision: row.proposal.revision, digest: '', status: 'unavailable' } } : row))
    setRevisionToken(value => value + 1)
  }
  const apply = (value: ResearchEgressView, id: string) => {
    const next = validateResearchEgress(value, id)
    detailSequence.current += 1; listSequence.current += 1
    setLoading(false); setDetailLoading(false); setResume(null)
    if (next.availability === 'source_unavailable') { purge(id); setDetail(next) }
    else { setDetail(next); setRevisionToken(value => value + 1) }
    setSelectedId(id); selected.current = id
    setRows(values => [validateResearchEgress(next, id, false), ...values.filter(row => row.proposal.id !== id)])
    return next
  }
  const load = async (append = false) => {
    if (!active.current.connected || !digestPattern.test(workspaceKey)) return
    const origin = active.current, generation = epoch.current, sequence = ++listSequence.current, read = controller()
    setLoading(true); setError('')
    try {
      const page: ResearchEgressPage = await api.listPersonalResearchEgress({ limit: 30, ...(append && nextAfter ? { after: nextAfter } : {}) }, read.signal)
      if (!valid(origin, generation, read.signal) || sequence !== listSequence.current) return
      if (!Array.isArray(page?.proposals) || page.next_after !== null && !idPattern.test(page.next_after)) throw new Error('外发目录身份待核对')
      const values = page.proposals.map(row => validateResearchEgress(row, undefined, false))
      setLoadedScope({ scope, api })
      setRows(old => append ? [...old, ...values.filter(row => !old.some(item => item.proposal.id === row.proposal.id))] : values)
      setNextAfter(page.next_after)
      const denied = values.find(row => row.proposal.id === selected.current && row.availability === 'source_unavailable')
      if (denied) { purge(denied.proposal.id); setDetail(denied) }
      else {
        const updated = values.find(row => row.proposal.id === selected.current)
        const viewed = currentDetail.current
        if (updated && viewed && (updated.proposal.revision !== viewed.proposal.revision || updated.proposal.digest !== viewed.proposal.digest || updated.proposal.status !== viewed.proposal.status)) void select(updated.proposal.id)
      }
    } catch (cause) { if (valid(origin, generation, read.signal) && sequence === listSequence.current) { purge(); setError(researchEgressError(cause)) } }
    finally { reads.current.delete(read); if (valid(origin, generation, read.signal) && sequence === listSequence.current) setLoading(false) }
  }
  const select = async (id: string) => {
    if (!active.current.connected || !idPattern.test(id)) return
    const origin = active.current, generation = epoch.current, sequence = ++detailSequence.current, read = controller()
    setSelectedId(id); selected.current = id; setDetail(null); setDetailLoading(true); setError(''); setResume(null)
    try {
      const value = await api.getPersonalResearchEgress(id, read.signal)
      if (!valid(origin, generation, read.signal) || sequence !== detailSequence.current || selected.current !== id) return
      const next = validateResearchEgress(value, id)
      if (next.availability === 'source_unavailable') { purge(id); setDetail(next) } else setDetail(next)
      setRevisionToken(value => value + 1)
    } catch (cause) { if (valid(origin, generation, read.signal) && sequence === detailSequence.current) { purge(id); setError(researchEgressError(cause)) } }
    finally { reads.current.delete(read); if (valid(origin, generation, read.signal) && sequence === detailSequence.current) setDetailLoading(false) }
  }
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; for (const read of reads.current) read.abort(); reads.current.clear() } }, [])
  useEffect(() => {
    epoch.current += 1; detailSequence.current += 1; listSequence.current += 1
    for (const read of reads.current) read.abort(); reads.current.clear()
    setRows([]); setDetail(null); setSelectedId(''); selected.current = ''; setNextAfter(null); setError(''); setNotice(''); setIdentityError('')
    setLoadedScope(null); setBusy(false); setLoading(false); setDetailLoading(false); posting.current = null; frozen.current = null; setResume(null); setPendingValue(null)
    try {
      const raw = localStorage.getItem(key)
      if (raw) { const record = parseResearchEgressPending(raw); setPendingValue(record); if (record.workspace_key !== workspaceKey) setIdentityError('原外发请求属于另一工作区，请返回原工作区核对') }
    } catch { setIdentityError('原外发请求状态待核对，请保留原记录') }
    if (connected) void load()
  }, [api, namespace, workspaceKey, connected])
  const clear = (saved: ResearchEgressPending) => {
    const raw = localStorage.getItem(key)
    if (!raw || parseResearchEgressPending(raw).request_id !== saved.request_id) throw new Error('原外发请求编号已变化')
    localStorage.removeItem(key); setPendingValue(null); setResume(null)
    if (frozen.current?.saved.request_id === saved.request_id) frozen.current = null
  }
  const mutate = async (operation: ResearchEgressMutation, proposal: ResearchEgressProposal, decision?: ResearchEgressDecision, retry = false) => {
    if (posting.current || !connected || identityError || !digestPattern.test(workspaceKey) || pendingRef.current && !retry) return false
    if (!idPattern.test(proposal.id) || !positive(proposal.revision) || !digestPattern.test(proposal.digest)) { setError('原外发记录身份待核对'); return false }
    const origin = active.current, generation = epoch.current, token = Symbol('egress-write')
    const original = retry ? frozen.current : null
    if (retry && (!original || original.key !== scope || original.saved.operation !== 'egress_decide' || pendingRef.current?.request_id !== original.saved.request_id)) return false
    const saved: ResearchEgressPending = original?.saved || { version: 1, request_id: egressRequestId(), operation, proposal_id: proposal.id, workspace_key: workspaceKey }
    const body = original?.body || { request_id: saved.request_id, id: proposal.id, expected_revision: proposal.revision, expected_digest: proposal.digest, ...(decision ? { decision } : {}) }
    posting.current = token; setBusy(true); setError(''); setNotice('')
    let sent = false
    try {
      if (!retry) {
        if (localStorage.getItem(key)) throw new Error('Original receipt exists')
        parseResearchEgressPending(JSON.stringify(saved))
        localStorage.setItem(key, JSON.stringify(saved)); setPendingValue(saved)
        frozen.current = { key: scope, saved, body: clone(body) }
      }
      sent = true
      const result = await api.operatePersonalResearchEgress(saved.operation, clone(body))
      if (!valid(origin, generation)) return false
      const next = apply(result, saved.proposal_id)
      clear(saved)
      setNotice(next.availability === 'source_unavailable' ? '原请求已受理，原依据暂不可读取' : saved.operation === 'egress_execute' ? '原执行结果已读回' : decision === 'revoke' ? '批准已撤销' : decision === 'reject' ? '外发请求已拒绝' : '外发请求已批准')
      return true
    } catch (cause) {
      if (!valid(origin, generation)) return false
      const value = cause && typeof cause === 'object' ? cause as { status?: number; accepted?: boolean; code?: string; error?: string } : {}
      const code = value.code || value.error || ''
      if (['source_unavailable', 'artifact_access_revoked', 'scope_denied', 'binding_conflict', 'agent_inactive', 'approval_invalidated'].includes(code)) purge(proposal.id)
      if (sent && value.accepted !== true && Number(value.status) >= 400 && Number(value.status) < 500 && ![408, 425, 429].includes(Number(value.status)) && !['request_pending', 'lease_lost', 'action_locked'].includes(code)) {
        try { clear(saved) } catch { /* The original opaque receipt remains available for reconciliation. */ }
        if ([401, 403, 404].includes(Number(value.status))) purge(proposal.id)
        else void select(proposal.id)
      }
      setError(sent ? researchEgressError(cause) : '原请求状态保存失败，未发送外发操作')
      return false
    } finally { if (posting.current === token) { posting.current = null; if (valid(origin, generation)) setBusy(false) } }
  }
  const reconcile = async () => {
    const saved = pendingRef.current
    if (!saved || posting.current || !connected || identityError || saved.workspace_key !== workspaceKey) return
    const origin = active.current, generation = epoch.current, token = Symbol('egress-readback'), read = controller()
    posting.current = token; setBusy(true); setError('')
    try {
      const receipt = await api.getPersonalResearchEgressRequest(saved.request_id, read.signal)
      if (!valid(origin, generation, read.signal)) return
      if (receipt.request_id !== saved.request_id || !['completed', 'pending', 'unknown'].includes(receipt.status)) throw new Error('Original receipt differs')
      if (receipt.resume) {
        const arguments_ = validateCapturedEgressResume(receipt, saved)
        apply(receipt.view!, saved.proposal_id)
        setResume({ key: scope, saved, arguments: arguments_ }); setNotice('原结果已接收，等待保存'); return
      }
      if (receipt.status !== 'completed' || !receipt.view) { setResume(null); setNotice('原请求结果待核对，未重新提交'); return }
      apply(receipt.view, saved.proposal_id); clear(saved); setNotice('原请求已读回')
    } catch (cause) { if (valid(origin, generation, read.signal)) setError(researchEgressError(cause)) }
    finally { reads.current.delete(read); if (posting.current === token) { posting.current = null; if (valid(origin, generation)) setBusy(false) } }
  }
  const retry = () => {
    const original = frozen.current
    if (!original || original.key !== scope || original.saved.operation !== 'egress_decide') return
    const body = original.body
    void mutate('egress_decide', { id: String(body.id), revision: Number(body.expected_revision), digest: String(body.expected_digest), status: 'unknown' }, body.decision as ResearchEgressDecision, true)
  }
  const resumeCaptured = async () => {
    const original = resumeRef.current, saved = pendingRef.current
    if (!original || !saved || original.key !== scope || original.saved.request_id !== saved.request_id || posting.current || !connected || identityError) return
    const origin = active.current, generation = epoch.current, token = Symbol('egress-save')
    posting.current = token; setBusy(true); setError(''); setNotice(''); setResume(null)
    try {
      const result = await api.operatePersonalResearchEgress('egress_execute', clone(original.arguments))
      if (!valid(origin, generation)) return
      const next = apply(result, saved.proposal_id)
      clear(saved); setNotice(next.availability === 'source_unavailable' ? '原请求已受理，原依据暂不可读取' : '原结果已保存并读回')
    } catch (cause) { if (valid(origin, generation)) setError(researchEgressError(cause)) }
    finally { if (posting.current === token) { posting.current = null; if (valid(origin, generation)) setBusy(false) } }
  }
  const scopeReady = connected && loadedScope?.scope === scope && loadedScope.api === api
  return { rows: scopeReady ? rows : [], nextAfter: scopeReady ? nextAfter : null, selectedId: scopeReady ? selectedId : '', detail: scopeReady ? detail : null,
    loading, detailLoading, busy, pending, error, notice, identityError, revisionToken,
    refresh: () => void load(), more: () => void load(true), select, mutate, reconcile, retry, resumeCaptured,
    canResumeCaptured: Boolean(scopeReady && capturedResume?.key === scope && capturedResume.saved.request_id === pending?.request_id),
    canRetry: Boolean(frozen.current?.key === scope && frozen.current.saved.operation === 'egress_decide'),
    locked: busy || Boolean(pending) || Boolean(identityError) || !scopeReady }
}
