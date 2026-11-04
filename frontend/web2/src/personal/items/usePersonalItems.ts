import { useCallback, useEffect, useRef, useState } from 'react'
import type { WorkbenchRuntimeClient } from '@naumi/shared/api/WorkbenchRuntimeClient'
import type {
  PersonalItem, PersonalItemAction, PersonalItemControl, PersonalItemInput, PersonalItemReadInput,
  PersonalItemResult, PersonalItemUpdate,
} from '@naumi/shared/api/personalItems'
import { validateArtifactRef } from '../library/useArtifactLibrary'

export type PersonalItemsApi = Pick<WorkbenchRuntimeClient,
  'personalItems' | 'personalItem' | 'createPersonalItem' | 'updatePersonalItem' | 'controlPersonalItem'
  | 'personalItemHistory' | 'personalItemOccurrences' | 'personalItemResults' | 'personalItemResult'
  | 'readPersonalItemResult' | 'personalArtifacts' | 'personalArtifact' | 'runs'>
export type ItemPending =
  | { operation: 'create'; body: PersonalItemInput }
  | { operation: 'update'; item_id: string; body: PersonalItemUpdate }
  | { operation: 'control'; item_id: string; action: PersonalItemAction; body: PersonalItemControl }
  | { operation: 'read'; occurrence_id: string; body: PersonalItemReadInput }
export const itemError = (error: unknown) => error instanceof Error ? error.message : '操作未完成，请重试'
export const itemPendingKey = (namespace: string) => `naumi:items:request:${encodeURIComponent(namespace)}`
const idPattern = /^pi_[a-f0-9]{32}$/
const occurrencePattern = /^pio_[a-f0-9]{32}$/
const hashPattern = /^[a-f0-9]{64}$/
export const itemRequestId = () => crypto.randomUUID().replaceAll('-', '')
export function validateItem(item: PersonalItem) {
  if (!item || !idPattern.test(item.item_id) || !Number.isSafeInteger(item.revision) || item.revision < 1
    || !['active', 'paused', 'cancelled', 'closed'].includes(item.state) || !item.definition
    || typeof item.definition.title !== 'string' || typeof item.definition.instructions !== 'string'
    || !item.definition.assignee || !/^(main|pa_[a-f0-9]{32})$/.test(item.definition.assignee.id)
    || !item.definition.schedule || !item.definition.inputs || !Array.isArray(item.definition.inputs.artifact_refs)
    || !hashPattern.test(item.definition_sha256)) throw new Error('事项记录不完整，请核对原请求')
  item.definition.inputs.artifact_refs.forEach(validateArtifactRef)
  if (item.occurrence) validateExecution(item.occurrence)
  return item
}
export function validateExecution<T extends { occurrence_id: string; status: string; artifacts: unknown[] }>(execution: T): T {
  if (!execution || !occurrencePattern.test(execution.occurrence_id)
    || !['prepared', 'queued', 'running', 'completed', 'error', 'timeout', 'max_turns', 'cancelled', 'unknown', 'blocked', 'skipped'].includes(execution.status)
    || !Array.isArray(execution.artifacts)) throw new Error('执行记录不完整，请重新加载')
  execution.artifacts.forEach(value => validateArtifactRef(value as Parameters<typeof validateArtifactRef>[0]))
  return execution
}
export function validateResult(result: PersonalItemResult) {
  validateExecution(result)
  if (!idPattern.test(result.item_id) || typeof result.title !== 'string'
    || !/^(main|pa_[a-f0-9]{32})$/.test(result.assignee_id) || !result.delivery_id || !hashPattern.test(result.delivery_sha256 || ''))
    throw new Error('结果投递凭据不完整，请重新加载')
  return result
}
export function parseItemPending(raw: string): ItemPending {
  const request = JSON.parse(raw) as ItemPending
  if (!request || !request.body || !/^[a-f0-9]{32}$/.test(request.body.request_id)
    || !['create', 'update', 'control', 'read'].includes(request.operation)) throw new Error('原事项请求记录不完整，请先核对原记录')
  if (request.operation === 'create' || request.operation === 'update') {
    const b = request.body
    if (typeof b.title !== 'string' || typeof b.instructions !== 'string' || !b.schedule || !Array.isArray(b.input_refs)
      || !Array.isArray(b.directories) || !Array.isArray(b.tags) || typeof b.assignee_id !== 'string'
      || !Number.isSafeInteger(b.assignee_version)) throw new Error('原事项请求正文不完整，请先核对原记录')
    b.input_refs.forEach(validateArtifactRef)
  }
  if (request.operation === 'update' || request.operation === 'control') {
    if (!idPattern.test(request.item_id) || !Number.isSafeInteger(request.body.expected_revision) || request.body.expected_revision < 1)
      throw new Error('原事项修订身份不完整，请先核对原记录')
  }
  if (request.operation === 'control' && !['pause', 'resume', 'cancel', 'close', 'run-now', 'restart'].includes(request.action)) throw new Error('原事项操作不完整')
  if (request.operation === 'read' && (!occurrencePattern.test(request.occurrence_id) || !request.body.delivery_id
    || !hashPattern.test(request.body.expected_delivery_sha256))) throw new Error('原已读请求凭据不完整')
  return request
}

export function usePersonalItems({ api, namespace, connected }: { api: PersonalItemsApi; namespace: string; connected: boolean }) {
  const [items, setItems] = useState<PersonalItem[]>([]), [results, setResults] = useState<PersonalItemResult[]>([])
  const [filter, setFilter] = useState<'active' | 'paused' | ''>('active'), [query, setQuery] = useState('')
  const [unread, setUnread] = useState(true), [tab, setTab] = useState<'items' | 'results'>('items')
  const [offset, setOffset] = useState(0), [nextOffset, setNextOffset] = useState<number | null>(null)
  const [loading, setLoading] = useState(false), [posting, setPosting] = useState(false), [error, setError] = useState('')
  const [writeError, setWriteError] = useState('')
  const [pending, setPending] = useState<ItemPending | null>(null), [identityError, setIdentityError] = useState('')
  const [refreshToken, setRefreshToken] = useState(0)
  const writeLock = useRef(false), alive = useRef(true), sequence = useRef(0)
  const key = itemPendingKey(namespace)
  useEffect(() => {
    alive.current = true
    const reload = () => {
      try { const raw = localStorage.getItem(key); setPending(raw ? parseItemPending(raw) : null); setIdentityError('') }
      catch (cause) { setIdentityError(itemError(cause)) }
    }
    reload(); window.addEventListener('storage', reload)
    return () => { alive.current = false; sequence.current++; window.removeEventListener('storage', reload) }
  }, [key])
  const refresh = useCallback(() => setRefreshToken(value => value + 1), [])
  useEffect(() => {
    if (!connected) { setItems([]); setResults([]); setLoading(false); return }
    const controller = new AbortController(), token = ++sequence.current
    setLoading(true)
    const timer = setTimeout(() => {
      const run = async () => {
        try {
          const page = tab === 'items'
            ? await api.personalItems({ state: filter, query: query.trim(), limit: 30, offset }, controller.signal)
            : await api.personalItemResults({ unread, limit: 30, offset }, controller.signal)
          if (controller.signal.aborted || token !== sequence.current) return
          if ('items' in page) {
            if (!Array.isArray(page.items)) throw new Error('事项目录不完整，请重试')
            setItems(page.items.map(validateItem))
          } else {
            if (!Array.isArray(page.results)) throw new Error('结果目录不完整，请重试')
            setResults(page.results.map(validateResult))
          }
          setNextOffset(page.next_offset ?? null); setError('')
        } catch (cause) { if (!controller.signal.aborted && token === sequence.current) setError(itemError(cause)) }
        finally { if (!controller.signal.aborted && token === sequence.current) setLoading(false) }
      }
      void run()
    }, query ? 180 : 0)
    return () => { clearTimeout(timer); controller.abort() }
  }, [api, connected, filter, query, unread, tab, offset, refreshToken])
  useEffect(() => {
    const focus = () => refresh()
    window.addEventListener('focus', focus)
    const timer = connected ? setInterval(refresh, 12000) : undefined
    return () => { window.removeEventListener('focus', focus); clearInterval(timer) }
  }, [connected, refresh])
  const execute = async (request: ItemPending) => {
    if (writeLock.current || !connected) return null
    writeLock.current = true; setPosting(true); setWriteError('')
    try {
      if (localStorage.getItem(key) !== JSON.stringify(request)) throw new Error('原请求记录已改变，请核对原请求')
      let accepted: PersonalItem | PersonalItemResult | Record<string, unknown>
      if (request.operation === 'create') accepted = validateItem(await api.createPersonalItem(request.body))
      else if (request.operation === 'update') {
        accepted = validateItem(await api.updatePersonalItem(request.item_id, request.body))
        if (accepted.item_id !== request.item_id) throw new Error('事项修订回执身份不一致，请核对原请求')
      } else if (request.operation === 'control') {
        accepted = validateItem(await api.controlPersonalItem(request.item_id, request.action, request.body))
        if (accepted.item_id !== request.item_id) throw new Error('事项操作回执身份不一致，请核对原请求')
      } else {
        await api.readPersonalItemResult(request.occurrence_id, request.body)
        accepted = validateResult(await api.personalItemResult(request.occurrence_id))
        if (accepted.occurrence_id !== request.occurrence_id || accepted.delivery_id !== request.body.delivery_id
          || accepted.delivery_sha256 !== request.body.expected_delivery_sha256 || !accepted.read_receipt)
          throw new Error('已读凭据尚待核对，原请求已保留')
      }
      if (localStorage.getItem(key) !== JSON.stringify(request)) throw new Error('原请求记录在提交期间改变，请核对已受理结果')
      localStorage.removeItem(key)
      if (alive.current) { setPending(null); refresh() }
      return accepted
    } catch (cause) { if (alive.current) setWriteError(`${itemError(cause)}；原请求已保留`) ; return null }
    finally { writeLock.current = false; if (alive.current) setPosting(false) }
  }
  const submit = async (request: ItemPending) => {
    if (writeLock.current || pending || identityError || !connected) return null
    try {
      if (localStorage.getItem(key)) throw new Error('存在待核对的原请求，请原样重试')
      const frozen = parseItemPending(JSON.stringify(request))
      localStorage.setItem(key, JSON.stringify(frozen)); setPending(frozen)
      return await execute(frozen)
    } catch (cause) { setWriteError(itemError(cause)); return null }
  }
  const discard = () => {
    if (writeLock.current) return
    try { localStorage.removeItem(key); setPending(null); setIdentityError(''); setError(''); setWriteError('') }
    catch (cause) { setError(itemError(cause)) }
  }
  const navigateTab = (value: 'items' | 'results') => { setTab(value); setOffset(0) }
  return {
    items, results, filter, query, unread, tab, offset, nextOffset, loading, posting, error: writeError || error, pending, identityError, refreshToken,
    setFilter: (value: typeof filter) => { setFilter(value); setOffset(0) },
    setQuery: (value: string) => { setQuery(value); setOffset(0) },
    setUnread: (value: boolean) => { setUnread(value); setOffset(0) }, setTab: navigateTab,
    setOffset, refresh, submit, retry: () => pending ? execute(pending) : Promise.resolve(null), discard,
    locked: posting || Boolean(pending) || Boolean(identityError) || !connected,
  }
}
export type PersonalItemsState = ReturnType<typeof usePersonalItems>
