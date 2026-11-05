import { useCallback, useEffect, useRef, useState } from 'react'
import { WorkbenchRequestError, type WorkbenchRuntimeClient } from '@naumi/shared/api/WorkbenchRuntimeClient'
import type { NotificationBatch, NotificationContext, NotificationEventRef, NotificationFilter, NotificationMutation, NotificationMutationIdentity, NotificationOperation, NotificationPage, NotificationPreferenceInput, NotificationPreferencesView, NotificationQuietState, PersonalNotificationEntry } from '@naumi/shared/api/personalNotifications'

export type PersonalNotificationsApi = Pick<WorkbenchRuntimeClient, 'getPersonalNotificationContext' | 'getPersonalNotifications' | 'getPersonalNotificationPreferences' | 'getPersonalNotificationBatch' | 'getPersonalNotificationRequest' | 'getPersonalNotificationPrompt' | 'setPersonalNotificationPreferences' | 'claimPersonalNotification' | 'presentPersonalNotification' | 'readPersonalNotifications'>
export interface NotificationPending {
  version: 1; request_id: string; operation: NotificationOperation; workspace_key: string; phase: 'sent'
  client_id?: string; batch_id?: string
}
interface Toast { batch: NotificationBatch; claim: NotificationMutationIdentity; phase: 'claimed' | 'reported' }
const hash = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
const id = (value: unknown, prefix: string): value is string => typeof value === 'string' && new RegExp(`^${prefix}_[a-f0-9]{32}$`).test(value)
const integer = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
const actor = (value: unknown): value is string => value === 'main' || id(value, 'pa')
const foreground = () => document.visibilityState !== 'hidden'
export const notificationError = (cause: unknown) => cause instanceof Error ? cause.message : '通知记录待核对'
export const notificationRequestId = () => crypto.randomUUID().replaceAll('-', '')
export const notificationPendingKey = (namespace: string) => `naumi:notification-request:v1:${encodeURIComponent(namespace)}`
const clientKey = (namespace: string) => `naumi:notification-client:v1:${encodeURIComponent(namespace)}`
export function parseNotificationPending(raw: string): NotificationPending {
  const value = JSON.parse(raw) as NotificationPending
  const allowed = ['version', 'request_id', 'operation', 'workspace_key', 'phase', 'client_id', 'batch_id']
  if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).some(key => !allowed.includes(key))
    || value.version !== 1 || typeof value.request_id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value.request_id)
    || !['preferences', 'claim', 'present', 'read'].includes(value.operation) || !hash(value.workspace_key) || value.phase !== 'sent'
    || value.client_id !== undefined && !id(value.client_id, 'nc') || value.batch_id !== undefined && !id(value.batch_id, 'nb')
    || ['claim', 'present'].includes(value.operation) && (!value.client_id || !value.batch_id)) throw new Error('原通知请求身份待核对')
  return value
}
function getClient(namespace: string) {
  const key = clientKey(namespace), existing = sessionStorage.getItem(key)
  if (existing) { if (!id(existing, 'nc')) throw new Error('通知客户端身份待核对'); return existing }
  const value = `nc_${notificationRequestId()}`; sessionStorage.setItem(key, value); return value
}
export function validateNotificationRef(value: NotificationEventRef): NotificationEventRef {
  if (!value || !id(value.event_id, 'ne') || !hash(value.event_digest)) throw new Error('通知成员身份待核对')
  return { event_id: value.event_id, event_digest: value.event_digest }
}
const refsEqual = (left: NotificationEventRef[], right: NotificationEventRef[]) => {
  const sorted = (refs: NotificationEventRef[]) => refs.map(ref => `${ref.event_id}:${ref.event_digest}`).sort()
  const canonical = sorted(right)
  return left.length === right.length && sorted(left).every((value, index) => value === canonical[index])
}
export function validateNotificationEntry(value: PersonalNotificationEntry): PersonalNotificationEntry {
  if (!value || !id(value.id, 'ne') || !hash(value.event_digest) || !actor(value.actor_id)
    || !['item_result', 'item_failure', 'calendar_approval'].includes(value.kind) || !['available', 'resolved', 'source_unavailable'].includes(value.availability)
    || !['normal', 'high'].includes(value.priority) || typeof value.unread !== 'boolean' || !Number.isFinite(Date.parse(value.occurred_at))
    || value.batch_id !== null && !id(value.batch_id, 'nb') || !value.source_ref || typeof value.source_ref !== 'object' || Array.isArray(value.source_ref)
    || value.notice_read !== null && (typeof value.notice_read !== 'object' || Array.isArray(value.notice_read))) throw new Error('通知记录待核对')
  if (value.availability === 'source_unavailable') return { ...value, title: null, actor_name: null, jump: null, source_ref: {} }
  if (value.title !== null && typeof value.title !== 'string' || value.actor_name !== null && typeof value.actor_name !== 'string'
    || value.jump && (!['result', 'item', 'calendar_action'].includes(value.jump.kind) || typeof value.jump.id !== 'string' || !value.jump.id || value.jump.id.length > 256)) throw new Error('通知来源待核对')
  return value
}
export function validateNotificationQuiet(value: NotificationQuietState): NotificationQuietState {
  if (!value || typeof value.quiet !== 'boolean' || typeof value.timezone_data_changed !== 'boolean' || !hash(value.timezone_sha256)
    || value.next_release !== null && !Number.isFinite(Date.parse(value.next_release)) || !Array.isArray(value.windows)
    || value.clock_rollback !== undefined && typeof value.clock_rollback !== 'boolean'
    || value.hold_reason !== undefined && value.hold_reason !== null && !['clock_rollback', 'timezone_data_changed'].includes(value.hold_reason)
    || value.windows.some(window => !Number.isFinite(Date.parse(window.start_at)) || !Number.isFinite(Date.parse(window.end_at)) || Date.parse(window.end_at) <= Date.parse(window.start_at))) throw new Error('勿扰时间状态待核对')
  return value
}
export function validateNotificationPreferences(value: NotificationPreferencesView, workspace: string): NotificationPreferencesView {
  const prefs = value?.preferences
  if (!prefs || !id(prefs.id, 'np') || prefs.workspace_key !== workspace || !integer(prefs.revision) || value.revision !== prefs.revision || !hash(value.digest)
    || typeof prefs.enabled !== 'boolean' || typeof prefs.timezone !== 'string' || !hash(prefs.timezone_sha256) || typeof prefs.include_existing !== 'boolean'
    || !integer(prefs.debounce_seconds) || prefs.debounce_seconds > 300 || !integer(prefs.max_delay_seconds) || prefs.max_delay_seconds < 1 || prefs.max_delay_seconds > 3600 || prefs.max_delay_seconds < prefs.debounce_seconds
    || !Array.isArray(prefs.quiet_hours) || prefs.quiet_hours.length > 14 || !Array.isArray(prefs.muted_actor_ids) || prefs.muted_actor_ids.length > 1000 || !prefs.muted_actor_ids.every(actor)
    || new Set(prefs.muted_actor_ids).size !== prefs.muted_actor_ids.length || !Array.isArray(prefs.bypass_kinds) || prefs.bypass_kinds.some(kind => !['item_failure', 'calendar_approval'].includes(kind))) throw new Error('通知设置身份待核对')
  try { new Intl.DateTimeFormat('zh-CN', { timeZone: prefs.timezone }) } catch { throw new Error('通知时区待核对') }
  for (const window of prefs.quiet_hours) if (!window || !Array.isArray(window.weekdays) || !window.weekdays.length || window.weekdays.some(day => !integer(day) || day > 6)
    || new Set(window.weekdays).size !== window.weekdays.length || !/^([01]\d|2[0-3]):[0-5]\d$/.test(window.start) || !/^([01]\d|2[0-3]):[0-5]\d$/.test(window.end)
    || typeof window.full_day !== 'boolean' || window.start === window.end && !window.full_day) throw new Error('勿扰时段待核对')
  validateNotificationQuiet(value.quiet); return value
}
export function validateNotificationBatch(value: NotificationBatch, workspace: string): NotificationBatch {
  if (!value || !id(value.id, 'nb') || !hash(value.digest) || value.workspace_key !== workspace || !actor(value.actor_id)
    || !['item_result', 'item_failure', 'calendar_approval'].includes(value.kind) || !['ready', 'claimed', 'presented'].includes(value.presentation_status)
    || typeof value.prompt_available !== 'boolean' || !Array.isArray(value.member_refs) || value.member_refs.length < 1 || value.member_refs.length > 100
    || !Array.isArray(value.entries) || !Number.isFinite(Date.parse(value.created_at))) throw new Error('通知批次身份待核对')
  const refs = value.member_refs.map(validateNotificationRef), entries = value.entries.map(validateNotificationEntry)
  if (new Set(refs.map(ref => ref.event_id)).size !== refs.length || new Set(entries.map(entry => entry.id)).size !== entries.length || entries.length !== refs.length || entries.some(entry => entry.actor_id !== value.actor_id || entry.kind !== value.kind || !refs.some(ref => ref.event_id === entry.id && ref.event_digest === entry.event_digest))) throw new Error('通知批次成员待核对')
  validateNotificationQuiet(value.quiet); return { ...value, member_refs: refs, entries }
}
function validateContext(value: NotificationContext): NotificationContext {
  if (!value || !hash(value.workspace_key) || typeof value.owner_key !== 'string' || !value.owner_key || typeof value.configured !== 'boolean'
    || !integer(value.unread_count) || !integer(value.attention_count) || typeof value.partial !== 'boolean' || !Array.isArray(value.actors)
    || value.actors.some(row => !actor(row.id) || typeof row.name !== 'string' || !integer(row.version)) || !value.worker) throw new Error('通知工作区身份待核对')
  validateNotificationPreferences(value.preferences, value.workspace_key); validateNotificationQuiet(value.quiet); return value
}
function validatePage(value: NotificationPage) {
  if (!value || !Array.isArray(value.entries) || value.consistency !== 'live_keyset' || typeof value.partial !== 'boolean' || !integer(value.unread_count) || !integer(value.attention_count)
    || value.next_after !== null && typeof value.next_after !== 'string') throw new Error('通知分页待核对')
  const entries = value.entries.map(validateNotificationEntry)
  if (new Set(entries.map(row => row.id)).size !== entries.length) throw new Error('通知分页存在重复身份')
  return { ...value, entries }
}
function operationName(value: string) { return ({ set_preferences: 'preferences', presentation: 'present', notice_read: 'read', mark_read: 'read' } as Record<string, string>)[value.replaceAll('-', '_')] || value }
function mayShow(batch: NotificationBatch, context: NotificationContext | null) {
  const prefs = context?.preferences.preferences, quiet = batch.quiet
  return Boolean(prefs && context?.workspace_key === batch.workspace_key && prefs.enabled && !prefs.muted_actor_ids.includes(batch.actor_id)
    && !quiet.clock_rollback && !quiet.timezone_data_changed && !quiet.hold_reason
    && (!quiet.quiet || batch.kind !== 'item_result' && prefs.bypass_kinds.includes(batch.kind))
    && (!context?.quiet.quiet || batch.kind !== 'item_result' && prefs.bypass_kinds.includes(batch.kind))
    && !context?.quiet.clock_rollback && !context?.quiet.timezone_data_changed && !context?.quiet.hold_reason)
}

export function usePersonalNotifications({ api, namespace, connected, open }: { api: PersonalNotificationsApi; namespace: string; connected: boolean; open: boolean }) {
  const [scope, setScope] = useState(namespace), [context, setContext] = useState<NotificationContext | null>(null), [page, setPage] = useState<NotificationPage | null>(null)
  const [filter, setFilter] = useState<NotificationFilter>('unread'), [actorId, setActorId] = useState('')
  const [pending, setPending] = useState<NotificationPending | null>(null), [identityError, setIdentityError] = useState(''), [error, setError] = useState('')
  const [toast, setToast] = useState<Toast | null>(null), [posting, setPosting] = useState(false), [loading, setLoading] = useState(false), [token, setToken] = useState(0)
  const [contextLoading, setContextLoading] = useState(false)
  const active = useRef(namespace), writeLock = useRef(false), pendingRef = useRef<NotificationPending | null>(null), toastRef = useRef<Toast | null>(null), sequence = useRef(0)
  const contextRef = useRef<NotificationContext | null>(null), offered = useRef(new Set<string>())
  const contextSequence = useRef(0)
  const pageFlight = useRef<symbol | null>(null), contextQueued = useRef(false)
  const refreshPage = useRef<(() => void) | null>(null), refreshContext = useRef<(() => void) | null>(null)
  active.current = namespace; pendingRef.current = scope === namespace ? pending : null; toastRef.current = scope === namespace ? toast : null
  const key = notificationPendingKey(namespace), refresh = useCallback(() => setToken(value => value + 1), [])
  useEffect(() => {
    setScope(namespace); setContext(null); setPage(null); setToast(null); toastRef.current = null; setPending(null); pendingRef.current = null
    contextRef.current = null; offered.current = new Set()
    contextQueued.current = false
    setError(''); setIdentityError(''); setPosting(false); writeLock.current = false; setFilter('unread'); setActorId('')
    const load = () => { if (active.current !== namespace) return; try { const raw = localStorage.getItem(key); const value = raw ? parseNotificationPending(raw) : null; if (value?.batch_id) offered.current.add(value.batch_id); pendingRef.current = value; setPending(value); setIdentityError(''); getClient(namespace) } catch (cause) { setIdentityError(notificationError(cause)) } }
    load(); const changed = (event: StorageEvent) => { if (event.key === key || event.key === null) load() }; window.addEventListener('storage', changed)
    return () => { sequence.current++; window.removeEventListener('storage', changed) }
  }, [namespace, key])
  const clearPending = (saved: NotificationPending) => {
    if (active.current !== namespace) return
    const raw = localStorage.getItem(key)
    if (!raw || parseNotificationPending(raw).request_id !== saved.request_id) throw new Error('原通知请求已变化')
    localStorage.removeItem(key); pendingRef.current = null; setPending(null)
  }
  const accept = (value: NotificationMutation, saved: NotificationPending) => {
    if (!value || value.accepted !== true || value.request_id !== saved.request_id || operationName(value.operation) !== saved.operation || !value.identity || typeof value.projection_available !== 'boolean') throw new Error('通知回执身份不一致')
    const identity = value.identity
    if (saved.operation === 'preferences' && (!id(identity.preferences_id, 'np') || !integer(identity.preferences_revision) || identity.preferences_revision < 1)) throw new Error('通知设置回执待核对')
    if (saved.operation === 'claim' || saved.operation === 'present') {
      if (identity.batch_id !== saved.batch_id || identity.client_id !== saved.client_id || !hash(identity.batch_digest) || !id(identity.claim_id, 'no') || !Array.isArray(identity.event_refs) || !identity.event_refs.length || identity.event_refs.length > 100) throw new Error('通知呈现回执待核对')
      identity.event_refs.forEach(validateNotificationRef)
      if (new Set(identity.event_refs.map(ref => ref.event_id)).size !== identity.event_refs.length || saved.operation === 'present' && !id(identity.presentation_id, 'ns')) throw new Error('通知呈现成员待核对')
    }
    if (saved.operation === 'read' && (!Array.isArray(identity.event_refs) || !identity.event_refs.length || identity.event_refs.length > 100 || !Array.isArray(identity.notice_read_ids) || identity.notice_read_ids.length !== identity.event_refs.length || !identity.notice_read_ids.every(value => id(value, 'nr')))) throw new Error('通知已读回执待核对')
    return identity
  }
  const submit = async (operation: NotificationOperation, body: { request_id: string }, invoke: () => Promise<NotificationMutation>, check?: (identity: NotificationMutationIdentity) => void, batchId?: string): Promise<NotificationMutation | null> => {
    if (!connected || !contextRef.current || writeLock.current || pendingRef.current || identityError) return null
    const saved: NotificationPending = { version: 1, request_id: body.request_id, operation, workspace_key: contextRef.current.workspace_key, phase: 'sent', ...(['claim', 'present'].includes(operation) ? { client_id: getClient(namespace), batch_id: batchId } : {}) }
    writeLock.current = true; setPosting(true); setError('')
    try {
      parseNotificationPending(JSON.stringify(saved)); if (localStorage.getItem(key)) throw new Error('请先核对原通知请求')
      localStorage.setItem(key, JSON.stringify(saved)); pendingRef.current = saved; setPending(saved)
      const value = await invoke(); if (active.current !== namespace) return null
      const identity = accept(value, saved); check?.(identity); clearPending(saved)
      if (!value.projection_available) setError('请求已受理，原来源暂不可用')
      refresh(); return value
    } catch (cause) {
      if (active.current !== namespace) return null
      if (cause instanceof WorkbenchRequestError && cause.status >= 400 && cause.status < 500 && ![408, 425, 429].includes(cause.status)) {
        clearPending(saved); if ([401, 403, 404, 409].includes(cause.status)) { setPage(null); setToast(null); toastRef.current = null; refresh() }
      }
      setError(notificationError(cause)); return null
    } finally { if (active.current === namespace) { writeLock.current = false; setPosting(false) } }
  }
  const claim = async (batch: NotificationBatch) => {
    if (toastRef.current || offered.current.has(batch.id) || document.visibilityState === 'hidden' || !batch.prompt_available) return
    offered.current.add(batch.id)
    const client_id = getClient(namespace), request_id = notificationRequestId()
    const value = await submit('claim', { request_id }, () => api.claimPersonalNotification({ request_id, batch_id: batch.id, expected_digest: batch.digest, client_id }), identity => {
      if (identity.batch_digest !== batch.digest || !refsEqual(identity.event_refs!, batch.member_refs)) throw new Error('通知领取成员不一致')
    }, batch.id)
    if (!value || active.current !== namespace || !value.projection_available || !foreground()) return
    // Reauthorize the exact immutable batch after the reservation, before rendering any source text.
    try {
      const current = validateNotificationBatch(await api.getPersonalNotificationBatch(batch.id), batch.workspace_key)
      if (active.current !== namespace || !foreground() || current.digest !== batch.digest || !refsEqual(current.member_refs, batch.member_refs)) return
      if (current.entries.some(entry => entry.availability !== 'available') || !mayShow(current, contextRef.current)) return
      const next: Toast = { batch: current, claim: value.identity, phase: 'claimed' }; toastRef.current = next; setToast(next)
    } catch (cause) { if (active.current === namespace) setError(notificationError(cause)) }
  }
  const actions = useRef({ claim }); actions.current = { claim }
  useEffect(() => {
    const controller = new AbortController(), run = ++contextSequence.current; let running = false
    const current = () => !controller.signal.aborted && active.current === namespace && run === contextSequence.current
    const load = async (force = false) => {
      if (!connected || !current() || running) return
      if (pageFlight.current && !force) { contextQueued.current = true; return }
      contextQueued.current = false; running = true; setContextLoading(true)
      try {
        const next = validateContext(await api.getPersonalNotificationContext(controller.signal)); if (!current()) return
        contextRef.current = next; setContext(next); refreshPage.current?.()
        if (pendingRef.current && pendingRef.current.workspace_key !== next.workspace_key) { setIdentityError('原通知请求属于其他工作区'); return }
        const showing = toastRef.current
        if (showing) {
          const batch = validateNotificationBatch(await api.getPersonalNotificationBatch(showing.batch.id, controller.signal), next.workspace_key); if (!current()) return
          const live = toastRef.current
          if (live && live.batch.id === showing.batch.id && live.batch.digest === showing.batch.digest
            && live.claim.claim_id === showing.claim.claim_id && live.claim.client_id === showing.claim.client_id) {
            if (batch.digest !== live.batch.digest || !refsEqual(batch.member_refs, live.batch.member_refs) || batch.entries.some(entry => entry.availability !== 'available') || !mayShow(batch, next)) { toastRef.current = null; setToast(null) }
            else { const fresh = { ...live, batch }; toastRef.current = fresh; setToast(fresh) }
          }
        }
        if (document.visibilityState === 'hidden' || pendingRef.current || toastRef.current || !next.configured || !next.preferences.preferences.enabled || next.quiet.clock_rollback || next.quiet.timezone_data_changed) return
        const prompt = await api.getPersonalNotificationPrompt(controller.signal); if (!current()) return
        validateNotificationQuiet(prompt.quiet)
        if (prompt.batch) await actions.current.claim(validateNotificationBatch(prompt.batch, next.workspace_key))
      } catch (cause) { if (current()) { setError(notificationError(cause)); contextRef.current = null; setContext(null); setPage(null); setToast(null); toastRef.current = null } }
      finally { running = false; if (current()) setContextLoading(false) }
    }
    const scheduled = () => { void load() }; refreshContext.current = scheduled
    if (!connected) { contextRef.current = null; setContext(null); setPage(null); setToast(null); toastRef.current = null; setContextLoading(false) }
    else void load(true)
    const timer = window.setInterval(() => { if (document.visibilityState !== 'hidden') void load() }, 15000)
    const foreground = () => { if (document.visibilityState !== 'hidden') void load(true); else { toastRef.current = null; setToast(null) } }; document.addEventListener('visibilitychange', foreground); window.addEventListener('focus', foreground)
    return () => { controller.abort(); if (refreshContext.current === scheduled) refreshContext.current = null; window.clearInterval(timer); document.removeEventListener('visibilitychange', foreground); window.removeEventListener('focus', foreground) }
  }, [api, namespace, connected, token])
  useEffect(() => {
    const controller = new AbortController(), run = ++sequence.current
    const workspace = context?.workspace_key
    const current = () => !controller.signal.aborted && active.current === namespace && run === sequence.current && Boolean(workspace) && contextRef.current?.workspace_key === workspace
    if (!connected || !open || !context || scope !== namespace) { setPage(null); setLoading(false); return () => controller.abort() }
    let running = false, ticket: symbol | null = null
    const load = async () => {
      if (!current() || running) return
      running = true; ticket = Symbol('notification-list'); pageFlight.current = ticket; setLoading(true)
      try { const value = await api.getPersonalNotifications({ filter, ...(actorId ? { actor_id: actorId } : {}), limit: 50 }, controller.signal); if (current()) setPage(validatePage(value)) }
      catch (cause) { if (current()) { setPage(null); setError(notificationError(cause)) } }
      finally {
        running = false
        if (pageFlight.current === ticket) { pageFlight.current = null; if (contextQueued.current) refreshContext.current?.() }
        if (current()) setLoading(false)
      }
    }
    const scheduled = () => { void load() }; refreshPage.current = scheduled; scheduled()
    return () => { controller.abort(); if (refreshPage.current === scheduled) refreshPage.current = null; if (pageFlight.current === ticket) pageFlight.current = null }
  }, [api, namespace, connected, open, filter, actorId, context?.workspace_key, scope])
  const reconcile = async () => {
    const saved = pendingRef.current
    if (!saved || !context || !connected || writeLock.current || saved.workspace_key !== context.workspace_key) return false
    writeLock.current = true; setPosting(true)
    try {
      const value = await api.getPersonalNotificationRequest(saved.request_id)
      if (active.current !== namespace) return false
      if (value.request_id !== saved.request_id || value.status !== 'accepted') throw new Error('原请求尚待核对，未重新提交')
      accept({ ...value, accepted: true } as NotificationMutation, saved); clearPending(saved)
      // Recovery proves only the original logical request. It never recreates a toast or physical presentation.
      setToast(null); toastRef.current = null; setError(''); refresh(); return true
    } catch (cause) { if (active.current === namespace) setError(notificationError(cause)); return false }
    finally { if (active.current === namespace) { writeLock.current = false; setPosting(false) } }
  }
  const reportVisible = async (batchId: string) => {
    const value = toastRef.current
    if (!value || value.batch.id !== batchId || value.phase !== 'claimed' || document.visibilityState === 'hidden' || pendingRef.current || writeLock.current) return
    const reported: Toast = { ...value, phase: 'reported' }; toastRef.current = reported; setToast(reported)
    const request_id = notificationRequestId(), body = { request_id, batch_id: value.batch.id, batch_digest: value.batch.digest, client_id: value.claim.client_id!, claim_id: value.claim.claim_id!, event_refs: value.batch.member_refs }
    await submit('present', body, () => api.presentPersonalNotification(body), identity => {
      if (identity.batch_digest !== body.batch_digest || identity.claim_id !== body.claim_id || !refsEqual(identity.event_refs!, body.event_refs)) throw new Error('通知呈现回执成员不一致')
    }, batchId)
  }
  const savePreferences = async (input: NotificationPreferenceInput, expectedRevision: number) => {
    const request_id = notificationRequestId(), body = { ...input, expected_revision: expectedRevision, request_id }
    return Boolean(await submit('preferences', body, () => api.setPersonalNotificationPreferences(body), identity => {
      if (identity.preferences_revision !== expectedRevision + 1) throw new Error('通知设置版本不一致')
    }))
  }
  const read = async (entries: PersonalNotificationEntry[]) => {
    const event_refs = entries.filter(entry => entry.unread).slice(0, 100).map(entry => ({ event_id: entry.id, event_digest: entry.event_digest })).sort((left, right) => left.event_id.localeCompare(right.event_id))
    if (!event_refs.length) return false
    const request_id = notificationRequestId(), body = { request_id, event_refs }
    return Boolean(await submit('read', body, () => api.readPersonalNotifications(body), identity => {
      if (!refsEqual(identity.event_refs!, event_refs)) throw new Error('通知已读回执成员不一致')
    }))
  }
  const more = async () => {
    if (!page?.next_after || loading || !connected) return
    const run = sequence.current; setLoading(true)
    try { const next = validatePage(await api.getPersonalNotifications({ filter, ...(actorId ? { actor_id: actorId } : {}), limit: 50, after: page.next_after })); if (active.current === namespace && run === sequence.current) setPage(previous => previous ? { ...next, entries: [...previous.entries, ...next.entries.filter(entry => !previous.entries.some(old => old.id === entry.id))] } : null) }
    catch (cause) { if (active.current === namespace && run === sequence.current) { setPage(null); setError(notificationError(cause)) } }
    finally { if (active.current === namespace && run === sequence.current) setLoading(false) }
  }
  const dismissToast = () => { setToast(null); toastRef.current = null }
  const changeFilter = (next: NotificationFilter) => { setPage(null); setFilter(next) }
  const changeActor = (next: string) => { setPage(null); setActorId(next) }
  const same = scope === namespace
  const currentContext = same && connected && context && contextRef.current?.workspace_key === context.workspace_key
  return { context: currentContext ? context : null, page: currentContext ? page : null, toast: currentContext ? toast : null,
    pending: same ? pending : null, identityError: same ? identityError : '', error: same ? error : '', posting, loading: loading || contextLoading, filter, actorId,
    refresh, changeFilter, changeActor, reconcile, reportVisible, savePreferences, read, more, dismissToast,
    locked: !connected || posting || Boolean(pending) || Boolean(identityError) || !context }
}
export type PersonalNotificationsState = ReturnType<typeof usePersonalNotifications>
