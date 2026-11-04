import { useCallback, useEffect, useRef, useState } from 'react'
import type { WorkbenchRuntimeClient } from '@naumi/shared/api/WorkbenchRuntimeClient'
import type {
  PersonalRoutine, PersonalRoutineAction, PersonalRoutineConfirm, PersonalRoutineContext, PersonalRoutineControl,
  PersonalRoutineOccurrence, PersonalRoutinePreview, PersonalRoutinePreviewInput, PersonalRoutineRequestReceipt,
  PersonalRoutineRevise, RoutineStatus,
} from '@naumi/shared/api/personalRoutines'
import { validateArtifactRef } from '../library/useArtifactLibrary'
import { validateItem } from '../items/usePersonalItems'

export type PersonalRoutinesApi = Pick<WorkbenchRuntimeClient,
  'personalRoutineContext' | 'personalRoutines' | 'personalRoutine' | 'previewPersonalRoutine' | 'createPersonalRoutine'
  | 'revisePersonalRoutine' | 'controlPersonalRoutine' | 'personalRoutineHistory' | 'personalRoutineOccurrences'
  | 'personalRoutineRequest' | 'personalArtifacts' | 'personalArtifact' | 'runs'>
export type RoutineWrite =
  | { operation: 'preview'; body: PersonalRoutinePreviewInput }
  | { operation: 'create'; body: PersonalRoutineConfirm }
  | { operation: 'revise'; routine_id: string; body: PersonalRoutineRevise }
  | { operation: PersonalRoutineAction; routine_id: string; body: PersonalRoutineControl }
// Only reconciliation identities survive reload. Instructions, paths and material references never enter storage.
export interface RoutinePending {
  version: 1
  operation: RoutineWrite['operation']
  request_id: string
  routine_id?: string
  preview_id?: string
  expected_revision?: number
  expected_digest?: string
}
export const routineRequestId = () => crypto.randomUUID().replaceAll('-', '')
export const routinePendingKey = (namespace: string) => `naumi:routines:request:${encodeURIComponent(namespace)}`
export const routineError = (cause: unknown) => cause instanceof Error ? cause.message : '操作未完成，请核对原请求'
const opaque = (value: unknown): value is string => typeof value === 'string' && value.length > 0 && value.length <= 512 && !/[\x00-\x1f\x7f]/.test(value)
const digest = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
const integer = (value: unknown) => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
const operations = ['preview', 'create', 'revise', 'pause', 'resume', 'cancel', 'extra-run']

function validateDefinition(value: PersonalRoutine['definition']) {
  if (!value || value.schema_version !== 1 || typeof value.title !== 'string' || typeof value.instructions !== 'string'
    || !value.assignee || !opaque(value.assignee.id) || typeof value.assignee.name !== 'string' || !integer(value.assignee.version)
    || !Array.isArray(value.tags) || !value.inputs || !Array.isArray(value.inputs.artifact_refs) || !Array.isArray(value.inputs.directories)
    || !value.schedule || !['once', 'cron', 'trading_day'].includes(value.schedule.kind)
    || typeof value.schedule.timezone !== 'string' || !value.policy || !value.limits || !value.recovery)
    throw new Error('计划定义不完整，请核对原记录')
  value.inputs.artifact_refs.forEach(validateArtifactRef)
}
export function validateRoutine(value: PersonalRoutine): PersonalRoutine {
  if (!value || !opaque(value.id) || !integer(value.revision) || value.revision < 1 || !/^[a-f0-9]{32}$/.test(value.epoch)
    || !['active', 'paused', 'cancelled', 'closed'].includes(value.status) || !digest(value.definition_sha256) || !value.budget)
    throw new Error('计划回执不完整，请核对原记录')
  validateDefinition(value.definition)
  for (const key of ['reserved_slots', 'started_executions', 'settled_cost_microusd', 'reserved_cost_microusd', 'unknown_count', 'remaining_cost_microusd', 'remaining_slots'] as const)
    if (!integer(value.budget[key])) throw new Error('计划额度回执不完整，请核对原记录')
  if (value.budget.accounting !== 'rate_card_estimate_not_provider_billing') throw new Error('费用核算来源待核对')
  return value
}
export function validateRoutinePreview(value: PersonalRoutinePreview): PersonalRoutinePreview {
  if (!value || !opaque(value.preview_id) || !digest(value.definition_digest) || !Number.isFinite(Date.parse(value.expires_at))
    || !Array.isArray(value.schedule_preview) || !value.schedule_preview.length
    || !(value.base_routine_id === null || opaque(value.base_routine_id))
    || !(value.base_revision === null || integer(value.base_revision) && value.base_revision > 0))
    throw new Error('预览回执不完整，请核对原请求')
  validateDefinition(value.definition)
  for (const entry of value.schedule_preview)
    if (!Number.isFinite(Date.parse(entry.due_at)) || typeof entry.local_time !== 'string' || ![0, 1].includes(entry.fold))
      throw new Error('预览时间缺少确切依据')
  return value
}
export function validateRoutineOccurrence(value: PersonalRoutineOccurrence): PersonalRoutineOccurrence {
  if (!value || !opaque(value.id) || !opaque(value.routine_id) || !/^[a-f0-9]{32}$/.test(value.epoch) || !integer(value.definition_revision)
    || !['reserved', 'linked', 'skipped', 'blocked', 'settled', 'unknown'].includes(value.status)
    || !Number.isFinite(Date.parse(value.nominal_due_at)) || !integer(value.reserved_cost_microusd)
    || !(value.settled_cost_microusd === undefined || value.settled_cost_microusd === null || integer(value.settled_cost_microusd)))
    throw new Error('执行记录不完整，请核对原记录')
  if (value.child) {
    validateItem(value.child)
    if (value.child.item_id !== value.item_id) throw new Error('执行记录与原事项身份不一致')
  }
  return value
}
export function parseRoutinePending(raw: string): RoutinePending {
  const value = JSON.parse(raw) as RoutinePending
  const keys = new Set(['version', 'operation', 'request_id', 'routine_id', 'preview_id', 'expected_revision', 'expected_digest'])
  if (!value || typeof value !== 'object' || Object.keys(value).some(key => !keys.has(key)) || value.version !== 1
    || !operations.includes(value.operation) || !/^[a-f0-9]{32}$/.test(value.request_id)
    || value.routine_id !== undefined && !opaque(value.routine_id)
    || value.preview_id !== undefined && !opaque(value.preview_id)
    || value.expected_digest !== undefined && !digest(value.expected_digest)
    || value.expected_revision !== undefined && (!integer(value.expected_revision) || value.expected_revision < 1))
    throw new Error('原请求身份记录不完整，请先核对记录')
  if (!['preview', 'create'].includes(value.operation) && !value.routine_id) throw new Error('原请求缺少计划身份')
  if (['create', 'revise'].includes(value.operation) && (!value.preview_id || !value.expected_digest)) throw new Error('原请求缺少预览身份')
  return value
}
function pendingFor(write: RoutineWrite): RoutinePending {
  const value: RoutinePending = { version: 1, operation: write.operation, request_id: write.body.request_id }
  if ('routine_id' in write) value.routine_id = write.routine_id
  if ('routine_id' in write.body && write.body.routine_id) value.routine_id = write.body.routine_id
  if ('preview_id' in write.body) { value.preview_id = write.body.preview_id; value.expected_digest = write.body.expected_digest }
  if ('expected_revision' in write.body) value.expected_revision = write.body.expected_revision
  return parseRoutinePending(JSON.stringify(value))
}
type WriteResult = PersonalRoutine | PersonalRoutinePreview | PersonalRoutineOccurrence
function validateResult(pending: RoutinePending, raw: WriteResult): WriteResult {
  if (pending.operation === 'preview') {
    const value = validateRoutinePreview(raw as PersonalRoutinePreview)
    if (value.base_routine_id !== (pending.routine_id ?? null) || value.base_revision !== (pending.expected_revision ?? null))
      throw new Error('预览不对应原计划修订，请保留原请求')
    return value
  }
  if (pending.operation === 'extra-run') {
    const value = validateRoutineOccurrence(raw as PersonalRoutineOccurrence)
    if (value.routine_id !== pending.routine_id) throw new Error('额外执行回执身份不一致')
    return value
  }
  const value = validateRoutine(raw as PersonalRoutine)
  if (pending.routine_id && value.id !== pending.routine_id) throw new Error('计划回执身份不一致')
  if (pending.expected_digest && value.definition_sha256 !== pending.expected_digest) throw new Error('计划定义与原确认摘要不一致')
  return value
}

export function usePersonalRoutines({ api, namespace, connected }: { api: PersonalRoutinesApi; namespace: string; connected: boolean }) {
  const [routines, setRoutines] = useState<PersonalRoutine[]>([]), [context, setContext] = useState<PersonalRoutineContext | null>(null)
  const [status, setStatus] = useState<RoutineStatus | ''>(''), [query, setQuery] = useState(''), [after, setAfter] = useState('')
  const [nextAfter, setNextAfter] = useState<string | null>(null), [loading, setLoading] = useState(false), [posting, setPosting] = useState(false)
  const [writeError, setWriteError] = useState('')
  const [error, setError] = useState(''), [identityError, setIdentityError] = useState(''), [pending, setPending] = useState<RoutinePending | null>(null)
  const [preview, setPreview] = useState<PersonalRoutinePreview | null>(null), [accepted, setAccepted] = useState<PersonalRoutine | null>(null)
  const [refreshToken, setRefreshToken] = useState(0)
  const alive = useRef(true), lock = useRef(false), activeKey = useRef('')
  const key = routinePendingKey(namespace)
  activeKey.current = key
  const refresh = useCallback(() => setRefreshToken(value => value + 1), [])
  useEffect(() => {
    alive.current = true; setPreview(null); setAccepted(null); setContext(null); setRoutines([]); setAfter(''); setWriteError('')
    const reload = () => {
      try { const raw = localStorage.getItem(key); setPending(raw ? parseRoutinePending(raw) : null); setIdentityError('') }
      catch (cause) { setIdentityError(routineError(cause)); setPending(null) }
    }
    reload(); window.addEventListener('storage', reload)
    return () => { alive.current = false; window.removeEventListener('storage', reload) }
  }, [key])
  useEffect(() => {
    if (!connected) { setRoutines([]); setContext(null); setLoading(false); return }
    const controller = new AbortController(); setLoading(true)
    const timer = setTimeout(() => {
      void Promise.all([api.personalRoutines({ ...(status ? { status } : {}), query: query.trim(), limit: 30, ...(after ? { after } : {}) }, controller.signal), api.personalRoutineContext(controller.signal)])
        .then(([page, ctx]) => {
          if (controller.signal.aborted) return
          if (!Array.isArray(page.routines) || !ctx || !Array.isArray(ctx.calendars) || !ctx.default_policy) throw new Error('计划上下文不完整，请重新加载')
          setRoutines(page.routines.map(validateRoutine)); setNextAfter(page.next_after ?? null); setContext(ctx); setError('')
        }).catch(cause => { if (!controller.signal.aborted) setError(routineError(cause)) })
        .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    }, query ? 180 : 0)
    return () => { clearTimeout(timer); controller.abort() }
  }, [api, connected, key, status, query, after, refreshToken])
  useEffect(() => {
    const focus = () => refresh()
    window.addEventListener('focus', focus)
    const timer = connected ? setInterval(refresh, 12000) : undefined
    return () => { window.removeEventListener('focus', focus); clearInterval(timer) }
  }, [connected, refresh])
  const accept = (identity: RoutinePending, value: WriteResult, storageKey: string) => {
    if (!alive.current || activeKey.current !== storageKey) return null
    if (localStorage.getItem(storageKey) !== JSON.stringify(identity)) throw new Error('原请求身份已改变，请核对受理记录')
    const valid = validateResult(identity, value)
    localStorage.removeItem(storageKey); setPending(null); setWriteError('')
    if (identity.operation === 'preview') setPreview(valid as PersonalRoutinePreview)
    else if (identity.operation !== 'extra-run') { setAccepted(valid as PersonalRoutine); setPreview(null) }
    refresh()
    return valid
  }
  const submit = async (write: RoutineWrite): Promise<WriteResult | null> => {
    if (lock.current || pending || identityError || !connected) return null
    const storageKey = key
    lock.current = true; setPosting(true); setWriteError('')
    try {
      if (localStorage.getItem(storageKey)) throw new Error('存在待核对原请求，请先核对')
      const identity = pendingFor(write)
      localStorage.setItem(storageKey, JSON.stringify(identity)); setPending(identity)
      let result: WriteResult
      if (write.operation === 'preview') result = await api.previewPersonalRoutine(write.body)
      else if (write.operation === 'create') result = await api.createPersonalRoutine(write.body)
      else if (write.operation === 'revise') result = await api.revisePersonalRoutine(write.routine_id, write.body)
      else result = await api.controlPersonalRoutine(write.routine_id, write.operation, write.body)
      return accept(identity, result, storageKey)
    } catch (cause) { if (alive.current && activeKey.current === storageKey) setWriteError(`${routineError(cause)}；原请求身份已保留`) ; return null }
    finally { lock.current = false; if (alive.current && activeKey.current === storageKey) setPosting(false) }
  }
  const reconcile = async () => {
    if (!pending || lock.current || !connected) return null
    const storageKey = key, identity = pending
    lock.current = true; setPosting(true); setWriteError('')
    try {
      const receipt: PersonalRoutineRequestReceipt = await api.personalRoutineRequest(identity.request_id)
      if (receipt.request_id !== identity.request_id) throw new Error('原请求回执身份不一致')
      if (receipt.status !== 'applied' || !receipt.result) throw new Error('原请求仍待核对，未创建新执行')
      if (receipt.operation !== identity.operation) throw new Error('原请求回执操作不一致')
      const expectedKind = identity.operation === 'preview' ? 'preview' : identity.operation === 'extra-run' ? 'occurrence' : 'routine'
      if (receipt.result_kind !== expectedKind) throw new Error('原请求回执类型不一致')
      const result = receipt.result as WriteResult
      const resultId = 'preview_id' in result ? result.preview_id : 'id' in result ? result.id : null
      if (!receipt.result_id || receipt.result_id !== resultId) throw new Error('原请求结果身份不一致')
      return accept(identity, result, storageKey)
    } catch (cause) { if (alive.current && activeKey.current === storageKey) setWriteError(routineError(cause)); return null }
    finally { lock.current = false; if (alive.current && activeKey.current === storageKey) setPosting(false) }
  }
  const discard = () => {
    if (lock.current) return
    try { localStorage.removeItem(key); setPending(null); setIdentityError(''); setError(''); setWriteError('') }
    catch (cause) { setError(routineError(cause)) }
  }
  return {
    routines, context, status, query, after, nextAfter, loading, posting, pending, preview, accepted, identityError, error: writeError || error, refreshToken,
    setStatus: (value: RoutineStatus | '') => { setStatus(value); setAfter('') }, setQuery: (value: string) => { setQuery(value); setAfter('') },
    setAfter, refresh, submit, reconcile, discard, clearPreview: () => setPreview(null),
    locked: posting || Boolean(pending) || Boolean(identityError) || !connected,
  }
}
export type PersonalRoutinesState = ReturnType<typeof usePersonalRoutines>
