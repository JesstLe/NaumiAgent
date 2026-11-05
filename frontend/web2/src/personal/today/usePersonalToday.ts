import { useCallback, useEffect, useRef, useState } from 'react'
import { WorkbenchRequestError, type WorkbenchRuntimeClient } from '@naumi/shared/api/WorkbenchRuntimeClient'
import type { TodayContext, TodayDayView, TodayDelivery, TodayEntry, TodayItemRef, TodayMutation, TodayQuery, TodayWrite } from '@naumi/shared/api/personalToday'
import { parseTodayPending, readTodayLocation, todayPendingKey, type TodayPending } from './todayLocation'

export type PersonalTodayApi = Pick<WorkbenchRuntimeClient, 'getPersonalTodayContext' | 'getPersonalToday' | 'getPersonalTodaySources'
  | 'getPersonalTodayMetadata' | 'getPersonalTodayDay' | 'getPersonalTodayHistory' | 'getPersonalTodayRequest' | 'mutatePersonalToday'
  | 'personalItem' | 'personalItemResult' | 'readPersonalItemResult'>
export const todayError = (cause: unknown) => cause instanceof Error ? cause.message : '操作未完成，请核对原记录'
const hash = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
const integer = (value: unknown) => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
export function validateTodayRef(value: TodayItemRef, workspace?: string): TodayItemRef {
  if (!value || value.kind !== 'item' || !hash(value.workspace_key) || workspace && value.workspace_key !== workspace
    || !/^pi_[a-f0-9]{32}$/.test(value.item_id) || !integer(value.item_revision) || value.item_revision < 1
    || !/^[a-f0-9]{32}$/.test(value.once_key) || !hash(value.definition_sha256) || !integer(value.assignee_version)
    || !/^(main|pa_[a-f0-9]{32})$/.test(value.assignee_id)) throw new Error('事项来源身份待核对')
  return value
}
export function validateTodayDelivery(value: TodayDelivery): TodayDelivery {
  if (!value || !/^pi_[a-f0-9]{32}$/.test(value.item_id) || !integer(value.item_revision) || !value.occurrence_id
    || !value.delivery_id || !hash(value.delivery_sha256) || !Number.isFinite(Date.parse(value.delivered_at))
    || !Array.isArray(value.artifacts)) throw new Error('原交付回执待核对')
  return value
}
export function validateTodayEntry(value: TodayEntry, workspace?: string): TodayEntry {
  validateTodayRef(value?.item_ref, workspace)
  if (!value || !/^tm_[a-f0-9]{32}$/.test(value.metadata_id) || !integer(value.metadata_revision)
    || !['available', 'stale', 'source_unavailable'].includes(value.availability)
    || !['high', 'normal', 'low'].includes(value.priority) || !['active', 'archived'].includes(value.metadata_status)
    || !['include', 'exclude'].includes(value.disposition) || typeof value.planned !== 'boolean'
    || value.placement_id !== null && !/^tp_[a-f0-9]{32}$/.test(value.placement_id)) throw new Error('安排记录待核对')
  if (value.availability !== 'available') return { ...value, title: null, assignee: null, report: null }
  if (typeof value.title !== 'string' || !value.assignee || typeof value.assignee.name !== 'string') throw new Error('事项来源尚不可用')
  if (value.report) validateTodayDelivery(value.report)
  return value
}
export function validateTodayView(value: TodayDayView, workspace: string, query: TodayQuery): TodayDayView {
  if (!value || value.workspace_key !== workspace || !value.day || !/^td_[a-f0-9]{32}$/.test(value.day.id)
    || !integer(value.day.revision) || value.day.date !== query.date || value.day.timezone !== query.timezone
    || query.planner_id && value.day.planner_id !== query.planner_id || !Array.isArray(value.day.placements)
    || !Array.isArray(value.entries) || !Array.isArray(value.routines) || !Array.isArray(value.deliveries)
    || !value.sources?.items || !value.sources.routines || !value.sources.deliveries || value.consistency !== 'live_keyset'
    || value.projection_unavailable !== undefined && typeof value.projection_unavailable !== 'boolean') throw new Error('今日安排与当前工作区不一致')
  for (const source of [value.sources.items, value.sources.routines, value.sources.deliveries])
    if (typeof source.partial !== 'boolean' || source.error !== undefined && typeof source.error !== 'string')
      throw new Error('来源读取状态待核对')
  const entries = value.entries.map(entry => validateTodayEntry(entry, workspace))
  if (new Set(entries.map(entry => entry.metadata_id)).size !== entries.length) throw new Error('安排记录存在重复身份')
  for (const placement of value.day.placements) validateTodayRef(placement.source_ref, workspace)
  return { ...value, entries, routines: value.routines.map(routine => routine.availability === 'available' ? routine : { ...routine, title: null }), deliveries: value.deliveries.map(validateTodayDelivery) }
}

export function usePersonalToday({ api, namespace, connected, readActive = true }: { api: PersonalTodayApi; namespace: string; connected: boolean; readActive?: boolean }) {
  const [query, setQuery] = useState<TodayQuery>(readTodayLocation)
  const [contextSnapshot, setContext] = useState<TodayContext | null>(null), [contextKey, setContextKey] = useState(''), [view, setView] = useState<TodayDayView | null>(null)
  const [contextLoading, setContextLoading] = useState(false)
  const [loading, setLoading] = useState(false), [posting, setPosting] = useState(false), [error, setError] = useState('')
  const [writeError, setWriteError] = useState('')
  const [pending, setPending] = useState<TodayPending | null>(null), [identityError, setIdentityError] = useState('')
  const [token, setToken] = useState(0)
  const active = useRef(''), sequence = useRef(0), writeLock = useRef(false)
  const moreController = useRef<AbortController | null>(null), reading = useRef(false)
  const storageKey = todayPendingKey(namespace)
  const foreground = connected && readActive
  active.current = storageKey; reading.current = foreground
  const context = foreground && contextKey === storageKey ? contextSnapshot : null
  const refresh = useCallback(() => setToken(value => value + 1), [])
  useEffect(() => {
    const key = storageKey
    setContext(null); setContextKey(''); setView(null); setError(''); setWriteError(''); setPosting(false); writeLock.current = false
    const reload = () => {
      if (active.current !== key) return
      try { const raw = localStorage.getItem(key); setPending(raw ? parseTodayPending(raw) : null); setIdentityError('') }
      catch { setIdentityError('原请求身份存储待核对'); setPending(null) }
    }
    reload(); window.addEventListener('storage', reload)
    return () => { sequence.current++; window.removeEventListener('storage', reload) }
  }, [storageKey])
  useEffect(() => {
    const controller = new AbortController(), key = storageKey
    setContext(null); setContextKey(''); setView(null); setError(''); setContextLoading(foreground)
    if (foreground) void api.getPersonalTodayContext(controller.signal).then(value => {
      if (controller.signal.aborted || active.current !== key || !reading.current) return
      if (!value || !hash(value.workspace_key) || !Array.isArray(value.planners) || typeof value.default_planner !== 'string') throw new Error('工作区身份待核对')
      setContext(value); setContextKey(key)
      setQuery(previous => ({ ...previous, planner_id: previous.planner_id && value.planners.some(planner => planner.id === previous.planner_id) ? previous.planner_id : value.default_planner }))
    }).catch(cause => { if (!controller.signal.aborted && active.current === key && reading.current) setError(todayError(cause)) })
      .finally(() => { if (!controller.signal.aborted && active.current === key && reading.current) setContextLoading(false) })
    return () => { controller.abort(); moreController.current?.abort(); moreController.current = null }
  }, [api, foreground, storageKey])
  useEffect(() => {
    const controller = new AbortController(), run = ++sequence.current, key = storageKey
    moreController.current?.abort(); moreController.current = null
    setView(null)
    if (!foreground || !context || !query.planner_id) { setLoading(false); return () => controller.abort() }
    setLoading(true)
    void api.getPersonalToday({ ...query, limit: 50 }, controller.signal).then(value => {
      if (controller.signal.aborted || run !== sequence.current || active.current !== key || !reading.current) return
      setView(validateTodayView(value, context.workspace_key, query)); setError('')
    }).catch(cause => { if (!controller.signal.aborted && run === sequence.current && active.current === key && reading.current) { setView(null); setError(todayError(cause)) } })
      .finally(() => { if (!controller.signal.aborted && run === sequence.current && active.current === key && reading.current) setLoading(false) })
    return () => { controller.abort(); moreController.current?.abort(); moreController.current = null }
  }, [api, foreground, context, query, token, storageKey])
  const changeQuery = (value: TodayQuery) => { setView(null); setQuery(value) }
  const accept = (value: TodayMutation, identity: TodayPending, key: string): boolean => {
    if (active.current !== key) return false
    if (!value || value.accepted !== true || value.request_id !== identity.request_id || value.operation.replaceAll('_', '-') !== identity.operation
      || !value.identity || !Array.isArray(value.identity.metadata_ids) || !/^tc_[a-f0-9]{32}$/.test(value.identity.change_id)
      || identity.metadata_id && !value.identity.metadata_ids.includes(identity.metadata_id)) throw new Error('原变更回执身份不一致')
    if (identity.operation === 'move') {
      if (!identity.day_id || value.identity.source_day_id !== identity.day_id
        || typeof value.identity.day_id !== 'string' || !/^td_[a-f0-9]{32}$/.test(value.identity.day_id)
        || value.identity.day_id === identity.day_id || !integer(value.identity.day_revision) || value.identity.day_revision! < 1
        || value.identity.source_day_revision !== undefined && (!integer(value.identity.source_day_revision) || value.identity.source_day_revision < 1))
        throw new Error('原移动回执身份不一致')
    } else if (identity.operation === 'rebase') {
      if (value.identity.day_id === undefined) {
        if (!identity.metadata_id || value.identity.day_revision !== undefined
          || !value.identity.metadata_ids.includes(identity.metadata_id)) throw new Error('原重新绑定回执身份不一致')
      } else if (!identity.day_id || value.identity.day_id !== identity.day_id
        || !integer(value.identity.day_revision) || value.identity.day_revision! < 1) throw new Error('原重新绑定回执身份不一致')
    } else if (identity.day_id && value.identity.day_id !== identity.day_id) throw new Error('原变更回执身份不一致')
    localStorage.removeItem(key); setPending(null); setIdentityError(''); setWriteError(value.projection_available === false ? '变更已受理，来源暂不可用' : '')
    setView(null); refresh(); return true
  }
  const submit = async (write: TodayWrite): Promise<boolean> => {
    if (writeLock.current || pending || identityError || !connected || !context || !reading.current) return false
    const key = storageKey, scope = context.workspace_key
    writeLock.current = true; setPosting(true); setWriteError('')
    try {
      const identity = parseTodayPending(JSON.stringify({ version: 1, request_id: write.body.request_id, operation: write.operation, workspace_key: scope,
      ...('day_id' in write.body ? { day_id: write.body.day_id } : {}), ...('metadata_id' in write.body ? { metadata_id: write.body.metadata_id } : {}) }))
      if (localStorage.getItem(key)) throw new Error('请先核对原请求')
      localStorage.setItem(key, JSON.stringify(identity)); setPending(identity)
      const value = await api.mutatePersonalToday(write.operation, write.body)
      return accept(value, identity, key)
    } catch (cause) {
      if (active.current !== key) return false
      if (cause instanceof WorkbenchRequestError && cause.status >= 400 && cause.status < 500 && ![408, 425, 429].includes(cause.status)) {
        localStorage.removeItem(key); setPending(null)
        if ([401, 403, 404, 409].includes(cause.status)) { setView(null); refresh() }
      }
      setWriteError(todayError(cause)); return false
    } finally { if (active.current === key) { writeLock.current = false; setPosting(false) } }
  }
  const reconcile = async () => {
    if (!pending || !context || writeLock.current || !connected || !reading.current) return false
    if (pending.workspace_key !== context.workspace_key) { setIdentityError('原请求属于其他工作区'); return false }
    const key = storageKey, identity = pending
    writeLock.current = true; setPosting(true)
    try {
      const value = await api.getPersonalTodayRequest(identity.request_id)
      if (active.current !== key) return false
      if (value.request_id !== identity.request_id || value.status !== 'accepted') throw new Error('原请求尚待核对，未重新提交')
      return accept({ ...value, accepted: true } as TodayMutation, identity, key)
    } catch (cause) { if (active.current === key) setWriteError(todayError(cause)); return false }
    finally { if (active.current === key) { writeLock.current = false; setPosting(false) } }
  }
  const more = async () => {
    if (!reading.current || !view?.next_cursor || !context || loading || contextLoading || moreController.current) return
    const run = sequence.current, key = storageKey
    const controller = new AbortController(); moreController.current = controller
    setLoading(true)
    try {
      const page = validateTodayView(await api.getPersonalToday({ ...query, limit: 50, cursor: view.next_cursor }, controller.signal), context.workspace_key, query)
      if (controller.signal.aborted || run !== sequence.current || active.current !== key || !reading.current) return
      setView(previous => previous ? { ...page, entries: [...previous.entries, ...page.entries.filter(entry => !previous.entries.some(old => old.metadata_id === entry.metadata_id))],
        routines: [...previous.routines, ...page.routines.filter(entry => !previous.routines.some(old => old.id === entry.id))],
        deliveries: [...previous.deliveries, ...page.deliveries.filter(entry => !previous.deliveries.some(old => old.delivery_id === entry.delivery_id))] } : null)
    } catch (cause) { if (!controller.signal.aborted && run === sequence.current && active.current === key && reading.current) { setView(null); setError(todayError(cause)) } }
    finally { if (moreController.current === controller) moreController.current = null; if (!controller.signal.aborted && run === sequence.current && active.current === key && reading.current) setLoading(false) }
  }
  return { query, context, view: foreground && context ? view : null, loading: foreground && (contextLoading || loading), posting, pending, identityError, error: writeError || error, refresh, changeQuery, submit, reconcile, more,
    locked: posting || Boolean(pending) || Boolean(identityError) || !foreground || !view }
}
export type PersonalTodayState = ReturnType<typeof usePersonalToday>
