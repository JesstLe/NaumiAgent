import { useCallback, useEffect, useRef, useState } from 'react'
import type { WorkbenchRuntimeClient } from '@naumi/shared/api/WorkbenchRuntimeClient'
import type { CalendarAction, CalendarOAuthIntent, PersonalConnection, PersonalConnectionContext, PersonalConnectionRequest } from '@naumi/shared/api/personalConnections'
import type { PersonalItem } from '@naumi/shared/api/personalItems'

export type PersonalConnectionsApi = Pick<WorkbenchRuntimeClient,
  'personalConnectionContext' | 'personalConnections' | 'personalConnectionRequest' |
  'configurePersonalCalendarClient' | 'beginPersonalCalendarOAuth' | 'personalCalendarOAuth' |
  'cancelPersonalCalendarOAuth' | 'syncPersonalCalendar' | 'disconnectPersonalCalendar' |
  'personalCalendarEvents' | 'createPersonalCalendarItem' | 'grantPersonalCalendar' |
  'revokePersonalCalendarGrant' | 'personalCalendarActions' | 'personalCalendarAction' |
  'createPersonalCalendarAction' | 'revisePersonalCalendarAction' | 'approvePersonalCalendarAction' |
  'rejectPersonalCalendarAction' | 'reconcilePersonalCalendarAction' | 'personalItem'>

export interface ConnectionRecovery {
  request_id: string; operation: string; owner_key: string; workspace_key: string
  resource_id?: string; intent_id?: string
}
type Receipt = PersonalConnectionContext | PersonalConnection | CalendarOAuthIntent | CalendarAction | PersonalItem
const opaque = /^[A-Za-z0-9_.:/-]{1,200}$/
export const connectionRecoveryKey = (namespace: string) => `naumi:calendar-recovery:v1:${encodeURIComponent(namespace)}`
export const connectionRequestId = () => crypto.randomUUID().replaceAll('-', '')
export function calendarError(cause: unknown): string {
  if (cause instanceof RangeError) return '时区或日期格式不正确，请填写有效的 IANA 时区'
  const value = cause instanceof Error ? cause.message : '连接资料暂未读到，请刷新'
  if (/failed to fetch|networkerror|fetch failed|network request/i.test(value)) return '本地服务暂未连接，请保留原请求编号'
  return /client_secret|access_token|refresh_token|password|client_json/i.test(value) ? '连接资料暂未读到，请按原编号核对' : value.slice(0, 240)
}
export function validateConnection(value: PersonalConnection): PersonalConnection {
  if (!value || !/^pc_[a-f0-9]{32}$/.test(value.id) || !Number.isSafeInteger(value.revision) || value.revision < 1 || !Number.isSafeInteger(value.auth_epoch) || !Array.isArray(value.scopes) || !Array.isArray(value.grants)) throw new Error('连接记录不完整，请刷新原连接')
  return value
}
export function validateAction(value: CalendarAction): CalendarAction {
  if (!value || !/^ca_[a-f0-9]{32}$/.test(value.id) || !Number.isSafeInteger(value.revision) || value.revision < 1 || !/^[a-f0-9]{64}$/.test(value.digest) || !value.body || !Array.isArray(value.source_refs)) throw new Error('动作记录不完整，请按原动作编号核对')
  return value
}
function readRecovery(key: string): { value: ConnectionRecovery | null; error: string } {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return { value: null, error: '' }
    const value = JSON.parse(raw) as ConnectionRecovery
    const fields = ['request_id', 'operation', 'owner_key', 'workspace_key', 'resource_id', 'intent_id']
    if (!value || Object.keys(value).some(field => !fields.includes(field)) || ![value.request_id, value.operation, value.owner_key, value.workspace_key].every(item => typeof item === 'string' && opaque.test(item)) || [value.resource_id, value.intent_id].some(item => item !== undefined && (typeof item !== 'string' || !opaque.test(item)))) throw new Error('待核对请求记录不完整，请保留原记录')
    return { value, error: '' }
  } catch { return { value: null, error: '原连接请求记录未能读取，请保留本地记录后核对' } }
}

export function usePersonalConnections({ api, namespace, connected }: { api: PersonalConnectionsApi; namespace: string; connected: boolean }) {
  const key = connectionRecoveryKey(namespace)
  const [context, setContext] = useState<PersonalConnectionContext | null>(null)
  const [connections, setConnections] = useState<PersonalConnection[]>([]), [actions, setActions] = useState<CalendarAction[]>([])
  const [pending, setPending] = useState(() => readRecovery(key).value)
  const recovery = useRef(pending)
  const [identityError, setIdentityError] = useState(() => readRecovery(key).error)
  const [oauth, setOauth] = useState<CalendarOAuthIntent | null>(null)
  const [lastResult, setLastResult] = useState<Receipt | null>(null)
  const [loading, setLoading] = useState(false), [busy, setBusy] = useState(false)
  const [error, setError] = useState(''), [notice, setNotice] = useState(''), [refreshToken, setRefreshToken] = useState(0)
  const alive = useRef(true), posting = useRef(false), active = useRef({ api, namespace, connected })
  active.current = { api, namespace, connected }
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  useEffect(() => { const saved = readRecovery(key); recovery.current = saved.value; setPending(saved.value); setIdentityError(saved.error); setContext(null); setConnections([]); setActions([]); setOauth(null); setBusy(false) }, [key])
  const refresh = useCallback(() => setRefreshToken(value => value + 1), [])
  useEffect(() => {
    if (!connected) { setLoading(false); return }
    const controller = new AbortController()
    setLoading(true)
    void Promise.all([api.personalConnectionContext(controller.signal), api.personalConnections(controller.signal), api.personalCalendarActions(controller.signal)]).then(([current, accountPage, actionPage]) => {
      if (controller.signal.aborted) return
      if (!current?.owner_key || !current.workspace_key || current.provider !== 'google_calendar' || !Array.isArray(accountPage.connections) || !Array.isArray(actionPage.actions)) throw new Error('连接工作区记录不完整')
      const accounts = accountPage.connections.map(validateConnection), drafts = actionPage.actions.map(validateAction)
      setContext(current); setConnections(accounts); setActions(drafts); setError('')
    }).catch(cause => { if (!controller.signal.aborted) setError(calendarError(cause)) }).finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [api, namespace, connected, refreshToken])
  const persist = (value: ConnectionRecovery | null) => {
    if (value) localStorage.setItem(key, JSON.stringify(value))
    else localStorage.removeItem(key)
    recovery.current = value
    setPending(value)
  }
  const apply = (value: Receipt) => {
    if ('intent_id' in value) {
      if (!opaque.test(value.intent_id)) throw new Error('授权记录编号不完整')
      setOauth(value)
    } else if ('id' in value && value.id.startsWith('pc_')) {
      const row = validateConnection(value as PersonalConnection)
      setConnections(previous => [...previous.filter(item => item.id !== row.id), row])
    } else if ('id' in value && value.id.startsWith('ca_')) {
      const row = validateAction(value as CalendarAction)
      setActions(previous => [row, ...previous.filter(item => item.id !== row.id)])
    } else if ('client_configured' in value) setContext(value)
    else if (!('item_id' in value)) throw new Error('操作回执不完整，请按原请求编号核对')
    setLastResult(value)
  }
  const observeAction = (value: CalendarAction) => {
    const row = validateAction(value)
    setActions(previous => [row, ...previous.filter(item => item.id !== row.id)])
  }
  const same = (origin: typeof active.current) => alive.current && active.current.api === origin.api && active.current.namespace === origin.namespace
  const settleOAuth = (value: CalendarOAuthIntent, record: ConnectionRecovery) => {
    if (value.status === 'connected' && !value.connection_id) {
      if (!opaque.test(value.intent_id)) throw new Error('授权记录编号不完整')
      persist({ ...record, intent_id: value.intent_id })
      throw new Error('授权回执缺少原连接编号，请核对原授权')
    }
    apply(value)
    if (value.status === 'connected' && value.connection_id) { persist(null); setNotice('Google 日历已连接'); refresh() }
    else if (['denied', 'expired', 'cancelled', 'interrupted'].includes(value.status)) { persist(null); setNotice(({ denied: '授权已拒绝', expired: '授权已过期', cancelled: '授权已取消', interrupted: '授权已中断' } as Record<string, string>)[value.status]); refresh() }
    else persist({ ...record, intent_id: value.intent_id })
  }
  const perform = async (operation: string, resourceId: string | undefined, call: (requestId: string) => Promise<Receipt>) => {
    if (posting.current || recovery.current || identityError || !context || !connected) return null
    const origin = active.current
    const record: ConnectionRecovery = { request_id: connectionRequestId(), operation, owner_key: context.owner_key, workspace_key: context.workspace_key, ...(resourceId ? { resource_id: resourceId } : {}) }
    posting.current = true; setBusy(true); setError(''); setNotice('')
    try {
      persist(record)
      const result = await call(record.request_id)
      if (!same(origin)) return null
      if ('intent_id' in result) settleOAuth(result, record)
      else { apply(result); persist(null); refresh() }
      return result
    } catch {
      if (same(origin)) setError('操作结果尚未确认，请核对原请求编号')
      return null
    } finally { posting.current = false; if (same(origin)) setBusy(false) }
  }
  const checkPending = async () => {
    const pending = recovery.current
    if (!pending || !connected || posting.current || !context) return
    if (context.owner_key !== pending.owner_key || context.workspace_key !== pending.workspace_key) { setIdentityError('原请求属于另一工作区，请返回原工作区核对'); return }
    const origin = active.current
    posting.current = true; setBusy(true); setError('')
    try {
      if (pending.intent_id) {
        const value = await api.personalCalendarOAuth(pending.intent_id)
        if (!same(origin)) return
        if (value.intent_id !== pending.intent_id) throw new Error('授权编号不一致')
        settleOAuth(value, pending)
      } else {
        const result: PersonalConnectionRequest = await api.personalConnectionRequest(pending.request_id)
        if (!same(origin)) return
        if (result.request_id !== pending.request_id) throw new Error('原请求编号不一致')
        if (result.status !== 'applied') { setNotice('原请求仍待核对'); return }
        let value = result.result
        if (!value) {
          if (result.result_kind === 'action' && result.result_id) value = await api.personalCalendarAction(result.result_id)
          else if (result.result_kind === 'oauth' && result.result_id) value = await api.personalCalendarOAuth(result.result_id)
          else if (result.result_kind === 'item' && result.result_id) value = await api.personalItem(result.result_id)
          else if (result.result_kind === 'context') value = await api.personalConnectionContext()
          else if (result.result_kind === 'connection' && result.result_id) value = (await api.personalConnections()).connections.find(connection => connection.id === result.result_id)
        }
        if (!same(origin)) return
        if (!value) { setNotice('原请求已受理，结果暂未读到'); return }
        const returnedId = 'id' in value ? value.id : 'item_id' in value ? value.item_id : 'intent_id' in value ? value.intent_id : null
        if (result.result_id !== returnedId) throw new Error('原请求结果身份不一致，请保留原编号')
        if ('intent_id' in value) settleOAuth(value, pending)
        else { apply(value); persist(null); setNotice('已恢复原请求结果'); refresh() }
      }
    } catch (cause) { if (same(origin)) setError(calendarError(cause)) }
    finally { posting.current = false; if (same(origin)) setBusy(false) }
  }
  const cancelOAuth = async () => {
    const pending = recovery.current
    if (!pending?.intent_id || !connected || posting.current || pending.operation === 'oauth_cancel') return
    const origin = active.current
    posting.current = true; setBusy(true)
    try {
      const record = { ...pending, request_id: connectionRequestId(), operation: 'oauth_cancel' }
      persist(record)
      const value = await api.cancelPersonalCalendarOAuth(pending.intent_id, { request_id: record.request_id })
      if (same(origin)) settleOAuth(value, record)
    } catch { if (same(origin)) setError('取消结果尚未确认，请继续核对原授权编号') }
    finally { posting.current = false; if (same(origin)) setBusy(false) }
  }
  useEffect(() => {
    if (!pending?.intent_id || !connected) return
    const timer = window.setInterval(() => { void checkPending() }, 2500)
    return () => window.clearInterval(timer)
  })
  const locked = !connected || !context || busy || Boolean(pending || identityError)
  return { context, connections, actions, pending, identityError, oauth, lastResult, loading, busy, error, notice, refreshToken, refresh, perform, checkPending, cancelOAuth, observeAction, locked }
}
