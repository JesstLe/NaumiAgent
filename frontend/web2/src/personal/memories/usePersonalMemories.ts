import { useCallback, useEffect, useRef, useState } from 'react'
import type { WorkbenchRuntimeClient } from '@naumi/shared/api/WorkbenchRuntimeClient'
import type {
  PersonalMemory, PersonalMemoryContext, PersonalMemoryFilter, PersonalMemoryGrant, PersonalMemoryMutationResult,
  PersonalMemoryOperation, PersonalMemoryPayload, PersonalMemoryRef, PersonalMemoryRequest, PersonalMemoryScopeKind,
} from '@naumi/shared/api/personalMemories'
import { validateArtifactRef } from '../library/useArtifactLibrary'

export type PersonalMemoriesApi = Pick<WorkbenchRuntimeClient,
  'personalMemoryContext' | 'personalMemories' | 'personalMemory' | 'searchPersonalMemories'
  | 'personalMemoryHistory' | 'personalMemoryGrants' | 'exportPersonalMemories' | 'legacyPersonalMemories'
  | 'preparePersonalMemoryRequest' | 'personalMemoryRequest' | 'applyPersonalMemoryRequest' | 'discardPersonalMemoryRequest' | 'personalItems'>
export const memoryError = (value: unknown) => value instanceof Error ? value.message : '操作未完成，请重试'
export const memoryRequestId = () => crypto.randomUUID().replaceAll('-', '')
const actorPattern = /^(main|pa_[a-f0-9]{32})$/
const idPattern = /^pm_[a-f0-9]{32}$/
const hashPattern = /^[a-f0-9]{64}$/
const operations: PersonalMemoryOperation[] = ['create', 'propose', 'confirm', 'revise', 'delete', 'reject', 'share', 'revoke', 'import_legacy']
export interface MemoryPending {
  version: 1; request_id: string; owner_key: string; workspace_key: string
  actor_id: string; operation: PersonalMemoryOperation; fingerprint: string
}
export const memoryPendingKey = (namespace: string, context: PersonalMemoryContext) =>
  `naumi:memory:request:v1:${encodeURIComponent(namespace)}:${context.owner_key}:${context.workspace_key}:${context.actor_id}`
export function parseMemoryPending(raw: string): MemoryPending {
  const value = JSON.parse(raw) as MemoryPending
  const allowed = ['version', 'request_id', 'owner_key', 'workspace_key', 'actor_id', 'operation', 'fingerprint']
  if (!value || Object.keys(value).some(k => !allowed.includes(k)) || value.version !== 1
    || !/^[a-f0-9]{32}$/.test(value.request_id) || !hashPattern.test(value.owner_key)
    || !hashPattern.test(value.workspace_key) || !actorPattern.test(value.actor_id)
    || !operations.includes(value.operation) || !hashPattern.test(value.fingerprint))
    throw new Error('原记忆请求身份不完整，请核对原记录')
  return value
}
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  if (value && typeof value === 'object') return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical((value as Record<string, unknown>)[key])}`).join(',')}}`
  return JSON.stringify(value)
}
export async function memoryFingerprint(actorId: string, workspaceKey: string, operation: PersonalMemoryOperation, payload: PersonalMemoryPayload) {
  const clean = JSON.parse(JSON.stringify({ actor_id: actorId, workspace_key: workspaceKey, operation, payload })) as unknown
  const bytes = new TextEncoder().encode(canonical(clean))
  return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))).map(n => n.toString(16).padStart(2, '0')).join('')
}
export function validateMemoryRef(ref: PersonalMemoryRef): PersonalMemoryRef {
  if (!ref || !idPattern.test(ref.memory_id) || !Number.isSafeInteger(ref.revision) || ref.revision < 1
    || !hashPattern.test(ref.content_sha256) || typeof ref.grant_id !== 'string'
    || (ref.grant_id ? !/^pmg_[a-f0-9]{32}$/.test(ref.grant_id) || !Number.isSafeInteger(ref.grant_revision) || ref.grant_revision < 1 : ref.grant_revision !== 0))
    throw new Error('记忆引用不完整，请重新核对')
  return ref
}
export function validateMemory(value: PersonalMemory, actorId?: string): PersonalMemory {
  if (!value || !idPattern.test(value.memory_id) || !actorPattern.test(value.actor_id)
    || !Number.isSafeInteger(value.revision) || value.revision < 1
    || !['proposed', 'active', 'superseded', 'rejected', 'deleted'].includes(value.state)
    || !value.scope || !['personal', 'workspace', 'item'].includes(value.scope.kind)
    || !['preference', 'fact', 'decision', 'experience', 'plan_template'].includes(value.category)
    || !['owned', 'shared'].includes(value.access) || typeof value.source_available !== 'boolean'
    || (value.content !== null && typeof value.content !== 'string')
    || (value.topic !== null && typeof value.topic !== 'string')) throw new Error('记忆记录不完整，请重新加载')
  if (actorId && value.access === 'owned' && value.actor_id !== actorId) throw new Error('记忆所属 Agent 与当前上下文不一致')
  if (value.access === 'shared' && !value.ref?.grant_id) throw new Error('记忆分享凭据尚未确认')
  if (value.ref) {
    validateMemoryRef(value.ref)
    if (value.ref.memory_id !== value.memory_id || value.ref.revision !== value.revision
      || (value.content_sha256 !== null && value.ref.content_sha256 !== value.content_sha256)) throw new Error('记忆引用与当前修订不一致')
  }
  if (value.active_ref) validateMemoryRef(value.active_ref)
  if (value.content_sha256 !== null && !hashPattern.test(value.content_sha256)) throw new Error('记忆摘要不完整')
  if (value.source) {
    const source = value.source
    if (!['manual', 'message', 'artifact', 'legacy', 'compaction'].includes(source.kind)
      || !['user', 'assistant', 'external', 'unknown'].includes(source.authority)
      || !Array.isArray(source.artifact_refs) || !hashPattern.test(source.excerpt_sha256)
      || (source.excerpt !== null && typeof source.excerpt !== 'string')) throw new Error('记忆来源记录不完整')
    source.artifact_refs.forEach(validateArtifactRef)
  }
  if (value.state === 'deleted' && (value.content !== null || value.source !== null)) throw new Error('已删除记忆仍含正文，请核对服务状态')
  if (!value.source_available && value.content !== null) throw new Error('来源失效的记忆仍含正文，请重新加载')
  return value
}
export function validateMemoryGrant(value: PersonalMemoryGrant): PersonalMemoryGrant {
  if (!value || !/^pmg_[a-f0-9]{32}$/.test(value.grant_id) || !idPattern.test(value.memory_id)
    || !actorPattern.test(value.recipient_actor_id) || !hashPattern.test(value.content_sha256)
    || !Number.isSafeInteger(value.revision) || value.revision < 1
    || !['active', 'revoked'].includes(value.state) || !Array.isArray(value.source_refs)) throw new Error('记忆分享凭据不完整')
  value.source_refs.forEach(validateArtifactRef)
  return value
}
function validateContext(value: PersonalMemoryContext, actor: string) {
  if (!value || value.actor_id !== actor || !hashPattern.test(value.owner_key) || !hashPattern.test(value.workspace_key)
    || !value.index || typeof value.index !== 'object') throw new Error('记忆上下文尚未确认，请重新连接')
  return value
}
function verifyRequest(meta: PersonalMemoryRequest, token: MemoryPending) {
  if (!meta || meta.request_id !== token.request_id || meta.actor_id !== token.actor_id
    || meta.workspace_key !== token.workspace_key || meta.operation !== token.operation
    || meta.fingerprint !== token.fingerprint || !['prepared', 'applied', 'erased'].includes(meta.state))
    throw new Error('原记忆请求与服务回执不一致，请核对原记录')
  return meta
}
function verifyResult(value: PersonalMemoryMutationResult, token: MemoryPending, payload?: PersonalMemoryPayload) {
  if ('grant_id' in value) {
    validateMemoryGrant(value)
    if (!['share', 'revoke'].includes(token.operation) || (payload?.grant_id && value.grant_id !== payload.grant_id)
      || (payload?.memory_id && value.memory_id !== payload.memory_id)) throw new Error('分享回执与原操作不一致')
  } else {
    validateMemory(value)
    if (value.actor_id !== token.actor_id || (payload?.memory_id && value.memory_id !== payload.memory_id)) throw new Error('记忆回执与原操作不一致')
    if (token.operation === 'delete' && value.state !== 'deleted') throw new Error('原删除尚未得到确认')
    if (token.operation === 'reject' && value.state !== 'rejected') throw new Error('原拒绝尚未得到确认')
  }
  return value
}

export function usePersonalMemories({ api, namespace, actorId, scope, itemId, connected }: {
  api: PersonalMemoriesApi; namespace: string; actorId: string; scope: PersonalMemoryScopeKind | 'all'; itemId: string; connected: boolean
}) {
  const [context, setContext] = useState<PersonalMemoryContext | null>(null), [contextError, setContextError] = useState('')
  const [contextAttempt, setContextAttempt] = useState(0)
  const [memories, setMemories] = useState<PersonalMemory[]>([]), [filter, setFilterState] = useState<PersonalMemoryFilter>('active')
  const [query, setQueryState] = useState(''), [offset, setOffset] = useState(0), [nextOffset, setNextOffset] = useState<number | null>(null)
  const [diagnostics, setDiagnostics] = useState<Record<string, unknown> | null>(null)
  const [loading, setLoading] = useState(false), [posting, setPosting] = useState(false), [error, setError] = useState('')
  const [readError, setReadError] = useState('')
  const [pending, setPending] = useState<MemoryPending | null>(null), [pendingError, setPendingError] = useState('')
  const [refreshToken, setRefreshToken] = useState(0), [notice, setNotice] = useState('')
  const alive = useRef(true), sequence = useRef(0), writeLock = useRef(false)
  const contextFailed = useRef(false)
  const currentContext = useRef(context); currentContext.current = context
  const currentIdentity = useRef({ namespace, actorId }); currentIdentity.current = { namespace, actorId }
  const key = context ? memoryPendingKey(namespace, context) : ''
  const refresh = useCallback(() => { setMemories([]); setDiagnostics(null); setRefreshToken(n => n + 1); if (contextFailed.current) setContextAttempt(n => n + 1) }, [])
  useEffect(() => {
    alive.current = true; setContext(null); setContextError(''); setMemories([]); setPending(null); setPendingError(''); setReadError('')
    const controller = new AbortController()
    if (connected) void api.personalMemoryContext(actorId, controller.signal).then(value => {
      if (!controller.signal.aborted) { setContext(validateContext(value, actorId)); contextFailed.current = false }
    }).catch(cause => { if (!controller.signal.aborted) { setContextError(memoryError(cause)); contextFailed.current = true } })
    return () => { alive.current = false; sequence.current++; controller.abort() }
  }, [api, namespace, actorId, connected, contextAttempt])
  useEffect(() => {
    if (!key || !context) return
    const reload = () => {
      try {
        const raw = localStorage.getItem(key), token = raw ? parseMemoryPending(raw) : null
        if (token && (token.actor_id !== context.actor_id || token.owner_key !== context.owner_key || token.workspace_key !== context.workspace_key)) throw new Error('原请求属于另一个记忆上下文')
        setPending(token); setPendingError('')
      } catch (cause) { setPendingError(memoryError(cause)) }
    }
    const changed = () => { reload(); refresh() }
    reload(); window.addEventListener('storage', changed)
    return () => window.removeEventListener('storage', changed)
  }, [key, context, refresh])
  useEffect(() => {
    setMemories([]); setDiagnostics(null)
    if (!connected || !context || (scope === 'item' && !itemId)) { setLoading(false); return }
    const controller = new AbortController(), token = ++sequence.current
    setLoading(true)
    const timer = setTimeout(() => {
      const args = { actor_id: actorId, scope, ...(scope === 'item' ? { item_id: itemId } : {}), query: query.trim() }
      const work = query.trim() && filter === 'active'
        ? api.searchPersonalMemories({ ...args, limit: 10 }, controller.signal)
        : api.personalMemories({ ...args, state: filter, limit: 30, offset }, controller.signal)
      void work.then(page => {
        if (controller.signal.aborted || token !== sequence.current) return
        if (!Array.isArray(page.memories)) throw new Error('记忆目录不完整，请重新加载')
        setMemories(page.memories.map(value => validateMemory(value, actorId))); setNextOffset('next_offset' in page ? page.next_offset : null)
        // A catalogue read cannot acknowledge or erase an independent mutation error.
        setDiagnostics('diagnostics' in page ? page.diagnostics : null); setReadError('')
      }).catch(cause => { if (!controller.signal.aborted && token === sequence.current) setReadError(memoryError(cause)) })
        .finally(() => { if (!controller.signal.aborted && token === sequence.current) setLoading(false) })
    }, query ? 180 : 0)
    return () => { clearTimeout(timer); controller.abort() }
  }, [api, actorId, context, scope, itemId, query, filter, offset, connected, refreshToken])
  useEffect(() => {
    const focus = () => refresh()
    window.addEventListener('focus', focus)
    const timer = connected ? setInterval(refresh, 12000) : undefined
    return () => { window.removeEventListener('focus', focus); clearInterval(timer) }
  }, [connected, refresh])
  const isCurrent = (token: MemoryPending) => alive.current && currentIdentity.current.namespace === namespace
    && currentIdentity.current.actorId === token.actor_id && currentContext.current?.actor_id === token.actor_id
    && currentContext.current?.owner_key === token.owner_key && currentContext.current?.workspace_key === token.workspace_key
  const finish = (token: MemoryPending) => {
    if (localStorage.getItem(key) !== JSON.stringify(token)) throw new Error('原请求记录在核对期间改变')
    localStorage.removeItem(key)
    if (isCurrent(token)) { setPending(null); refresh() }
  }
  const apply = async (token: MemoryPending, payload?: PersonalMemoryPayload) => {
    const meta = verifyRequest(await api.personalMemoryRequest(token.request_id, token.actor_id), token)
    if (meta.state === 'erased') {
      const value = meta.result ? verifyResult(meta.result, token, payload) : null
      finish(token); if (isCurrent(token)) setNotice('原请求正文已清除，此请求不会再次执行')
      return value
    }
    const value = meta.state === 'applied' ? meta.result : await api.applyPersonalMemoryRequest(token.request_id, token.actor_id, token.fingerprint)
    if (!value) throw new Error('原操作的当前结果尚待核对')
    verifyResult(value, token, payload)
    const checked = verifyRequest(await api.personalMemoryRequest(token.request_id, token.actor_id), token)
    if (checked.state === 'prepared') throw new Error('操作回执尚未持久确认，原请求已保留')
    if (checked.result) verifyResult(checked.result, token, payload)
    finish(token)
    return checked.result || value
  }
  const submit = async (operation: PersonalMemoryOperation, fields: Omit<PersonalMemoryPayload, 'request_id'>) => {
    if (!context || !key || writeLock.current || pending || pendingError || !connected) return null
    writeLock.current = true; setPosting(true); setError(''); setNotice(''); refresh()
    try {
      if (localStorage.getItem(key)) throw new Error('存在待核对的原请求，请先原样核对')
      const payload: PersonalMemoryPayload = { ...fields, request_id: memoryRequestId() }
      const fingerprint = await memoryFingerprint(actorId, context.workspace_key, operation, payload)
      if (!alive.current || currentContext.current !== context) throw new Error('记忆上下文已改变，本次请求未发送')
      const token: MemoryPending = { version: 1, request_id: payload.request_id, owner_key: context.owner_key, workspace_key: context.workspace_key, actor_id: actorId, operation, fingerprint }
      localStorage.setItem(key, JSON.stringify(token)); setPending(token)
      verifyRequest(await api.preparePersonalMemoryRequest({ request_id: payload.request_id, actor_id: actorId, operation, payload }), token)
      if (!isCurrent(token)) return null
      return await apply(token, payload)
    } catch (cause) { if (alive.current) setError(`${memoryError(cause)}；请核对原请求`); return null }
    finally { writeLock.current = false; if (alive.current) setPosting(false) }
  }
  const retry = async () => {
    if (!pending || writeLock.current || !connected || !context) return null
    writeLock.current = true; setPosting(true); setError(''); setNotice(''); refresh()
    try { return await apply(pending) }
    catch (cause) { if (alive.current) setError(`${memoryError(cause)}；原请求身份已保留`); return null }
    finally { writeLock.current = false; if (alive.current) setPosting(false) }
  }
  const discard = async () => {
    if (!key || !pending || writeLock.current || !connected || !context) return null
    const token = pending
    writeLock.current = true; setPosting(true); setError(''); setNotice(''); refresh()
    try {
      verifyRequest(await api.personalMemoryRequest(token.request_id, token.actor_id), token)
      const erased = verifyRequest(await api.discardPersonalMemoryRequest(token.request_id, token.actor_id, token.fingerprint), token)
      if (erased.state === 'prepared') throw new Error('原草稿的清理状态尚待核对，原请求身份已保留')
      const checked = verifyRequest(await api.personalMemoryRequest(token.request_id, token.actor_id), token)
      if (checked.state === 'prepared') throw new Error('清理回执尚未持久确认，原请求身份已保留')
      if (checked.state === 'applied' && !checked.result) throw new Error('原请求已提交，当前结果尚待核对')
      const value = checked.result ? verifyResult(checked.result, token) : null
      finish(token)
      if (isCurrent(token)) setNotice(checked.state === 'applied' ? '原请求已提交，已恢复当前结果；本次清理未撤销已提交的操作'
        : value ? '原请求正文已清除，已恢复当前结果' : '未提交的原记忆草稿已清理')
      return { result: value, state: checked.state }
    } catch (cause) { if (isCurrent(token)) setError(`${memoryError(cause)}；原请求身份已保留`); return null }
    finally { writeLock.current = false; if (isCurrent(token)) setPosting(false) }
  }
  const discardCorrupt = () => {
    if (!key || writeLock.current) return
    try { localStorage.removeItem(key); setPending(null); setPendingError(''); setError(''); setNotice(''); refresh() }
    catch (cause) { setError(memoryError(cause)) }
  }
  return {
    context, memories, filter, query, offset, nextOffset, diagnostics, loading, posting, pending, pendingError, notice,
    error: contextError || pendingError || error || readError, refreshToken, refresh, submit, retry, discard, discardCorrupt,
    setFilter: (value: PersonalMemoryFilter) => { setFilterState(value); setOffset(0); setQueryState('') },
    setQuery: (value: string) => { setQueryState(value); setOffset(0) }, setOffset,
    locked: posting || Boolean(pending) || Boolean(pendingError) || !connected || !context,
  }
}
export type PersonalMemoriesState = ReturnType<typeof usePersonalMemories>
