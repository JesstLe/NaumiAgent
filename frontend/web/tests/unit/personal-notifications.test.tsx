import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { WorkbenchRequestError, WorkbenchRuntimeClient } from '@naumi/shared/api/WorkbenchRuntimeClient'
import type { NotificationBatch, NotificationContext, NotificationMutation, NotificationPage, NotificationPreferenceWrite, NotificationPreferencesView, NotificationQuietState, NotificationRead, PersonalNotificationEntry } from '@naumi/shared/api/personalNotifications'
import { PersonalNotifications } from '../../../web2/src/personal/notifications/PersonalNotifications'
import { NotificationSettings, notificationPreferenceInput } from '../../../web2/src/personal/notifications/NotificationSettings'
import { ConnectionWorkspace } from '../../../web2/src/personal/connections/ConnectionWorkspace'
import type { PersonalConnectionsApi } from '../../../web2/src/personal/connections/usePersonalConnections'
import type { CalendarAction } from '@naumi/shared/api/personalConnections'
import { notificationPendingKey, parseNotificationPending, usePersonalNotifications, validateNotificationBatch, validateNotificationEntry, validateNotificationPreferences, type PersonalNotificationsApi } from '../../../web2/src/personal/notifications/usePersonalNotifications'

const workspace = 'a'.repeat(64), digest = 'b'.repeat(64), eventId = `ne_${'c'.repeat(32)}`, batchId = `nb_${'d'.repeat(32)}`
const itemId = `pi_${'e'.repeat(32)}`, occurrenceId = `pio_${'f'.repeat(32)}`, actionId = `ca_${'1'.repeat(32)}`
const quiet = (patch: Partial<NotificationQuietState> = {}): NotificationQuietState => ({ quiet: false, next_release: null, timezone_sha256: digest, timezone_data_changed: false, windows: [], clock_rollback: false, hold_reason: null, ...patch })
const preferences = (patch: Partial<NotificationPreferencesView['preferences']> = {}): NotificationPreferencesView => {
  const value = { id: `np_${'2'.repeat(32)}`, workspace_key: workspace, revision: 1, enabled: false, timezone: 'Asia/Shanghai', timezone_sha256: digest, quiet_hours: [], muted_actor_ids: [], debounce_seconds: 60, max_delay_seconds: 300, bypass_kinds: [], include_existing: false, cutover_at: '2026-11-05T01:00:00Z', created_at: '2026-11-05T01:00:00Z', updated_at: '2026-11-05T01:00:00Z', ...patch }
  return { preferences: value, revision: value.revision, digest, quiet: quiet() }
}
const context = (prefs = preferences()): NotificationContext => ({ workspace_key: workspace, owner_key: '3'.repeat(64), configured: true, preferences: prefs, actors: [{ id: 'main', name: '主 Agent', version: 0 }], unread_count: 1, attention_count: 0, partial: false, worker: { last_error_code: null, last_tick_at: '2026-11-05T02:00:00Z' }, quiet: quiet() })
const entry = (patch: Partial<PersonalNotificationEntry> = {}): PersonalNotificationEntry => ({ id: eventId, event_digest: digest, kind: 'item_result', actor_id: 'main', actor_name: '主 Agent', title: '项目资料已核对', occurred_at: '2026-11-05T02:00:00Z', availability: 'available', priority: 'normal', unread: true, notice_read: null, source_ref: { occurrence_id: occurrenceId }, jump: { kind: 'result', id: occurrenceId }, batch_id: batchId, ...patch })
const page = (entries = [entry()]): NotificationPage => ({ entries, next_after: null, partial: false, consistency: 'live_keyset', unread_count: entries.filter(value => value.unread).length, attention_count: entries.filter(value => value.kind !== 'item_result').length })
const batch = (entries = [entry()]): NotificationBatch => ({ id: batchId, digest, workspace_key: workspace, actor_id: 'main', kind: entries[0].kind, member_refs: entries.map(value => ({ event_id: value.id, event_digest: value.event_digest })), entries, created_at: '2026-11-05T02:00:00Z', presentation_status: 'ready', quiet: quiet(), prompt_available: true })
const receipt = (requestId: string, operation: string, identity: NotificationMutation['identity']): NotificationMutation => ({ accepted: true, request_id: requestId, operation, identity, projection_available: true })
function apiMock(rows = [entry()], enabled = false) {
  const ctx = context(preferences({ enabled })), offered = batch(rows)
  const api = {
    getPersonalNotificationContext: vi.fn().mockImplementation(async () => ctx), getPersonalNotifications: vi.fn().mockResolvedValue(page(rows)),
    getPersonalNotificationPreferences: vi.fn().mockResolvedValue(ctx.preferences), getPersonalNotificationBatch: vi.fn().mockResolvedValue(offered),
    getPersonalNotificationRequest: vi.fn().mockImplementation(async request_id => ({ status: 'unknown', request_id })),
    getPersonalNotificationPrompt: vi.fn().mockResolvedValue({ batch: null, quiet: quiet(), partial: false }),
    setPersonalNotificationPreferences: vi.fn().mockImplementation(async (body: NotificationPreferenceWrite) => {
      ctx.preferences = preferences({ ...body, revision: body.expected_revision + 1 }); return receipt(body.request_id, 'set_preferences', { preferences_id: ctx.preferences.preferences.id, preferences_revision: body.expected_revision + 1 })
    }),
    claimPersonalNotification: vi.fn().mockImplementation(async body => {
      api.getPersonalNotificationPrompt.mockResolvedValue({ batch: null, quiet: quiet(), partial: false })
      return receipt(body.request_id, 'claim', { batch_id: body.batch_id, batch_digest: body.expected_digest, client_id: body.client_id, claim_id: `no_${'4'.repeat(32)}`, event_refs: offered.member_refs })
    }),
    presentPersonalNotification: vi.fn().mockImplementation(async body => receipt(body.request_id, 'presentation', { ...body, presentation_id: `ns_${'5'.repeat(32)}` })),
    readPersonalNotifications: vi.fn().mockImplementation(async (body: NotificationRead) => receipt(body.request_id, 'notice_read', { event_refs: body.event_refs, notice_read_ids: body.event_refs.map((_, index) => `nr_${String(index + 6).repeat(32).slice(0, 32)}`) })),
  } satisfies PersonalNotificationsApi
  return api
}
function mount(api = apiMock(), open = true) {
  const onClose = vi.fn(), onToggle = vi.fn(), onOpenResult = vi.fn(), onOpenItem = vi.fn(), onOpenAction = vi.fn()
  return { api, onClose, onToggle, onOpenResult, onOpenItem, onOpenAction, ...render(<PersonalNotifications api={api} namespace="notice-A" connected agents={[]} open={open} onClose={onClose} onToggle={onToggle} onOpenResult={onOpenResult} onOpenItem={onOpenItem} onOpenAction={onOpenAction} />) }
}
const rect = (top = 10, bottom = 30): DOMRect => ({ top, bottom, left: 10, right: 380, width: 370, height: bottom - top, x: 10, y: top, toJSON: () => ({}) })
function visibleGeometry() {
  vi.spyOn(HTMLElement.prototype, 'getClientRects').mockImplementation(() => [rect()] as unknown as DOMRectList)
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) { return this.classList.contains('pn-toast-members') ? rect(0, 150) : rect() })
  vi.spyOn(document, 'elementFromPoint').mockImplementation((x, y) => [...document.querySelectorAll<HTMLElement>('[data-notification-member]')].find(element => { const box = element.getBoundingClientRect(); return x >= box.left && x <= box.right && y >= box.top && y <= box.bottom }) || null)
}
beforeEach(() => { localStorage.clear(); sessionStorage.clear(); history.replaceState({}, '', '/web2'); Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' }); Object.defineProperty(document, 'elementFromPoint', { configurable: true, writable: true, value: () => null }) })
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers() })

describe('notification immutable identities', () => {
  it('stores strictly opaque request identities, rejecting bodies, wrong phases and malformed clients', () => {
    const saved = { version: 1, request_id: 'request-1', operation: 'claim', workspace_key: workspace, phase: 'sent', client_id: `nc_${'7'.repeat(32)}`, batch_id: batchId }
    expect(parseNotificationPending(JSON.stringify(saved))).toEqual(saved)
    for (const patch of [{ title: 'PRIVATE' }, { preferences: {} }, { phase: 'presented' }, { client_id: 'main' }, { workspace_key: 'wrong' }]) expect(() => parseNotificationPending(JSON.stringify({ ...saved, ...patch }))).toThrow()
  })
  it('strips revoked source text, actor names, navigation and source fields before rendering', () => {
    const value = validateNotificationEntry(entry({ availability: 'source_unavailable', title: 'PRIVATE', actor_name: 'PRIVATE AGENT', source_ref: { path: 'PRIVATE PATH' } }))
    expect(value).toMatchObject({ title: null, actor_name: null, jump: null, source_ref: {} })
  })
  it('rejects mismatched complete batch members, duplicate references and cross-workspace batches', () => {
    expect(() => validateNotificationBatch({ ...batch(), member_refs: [] }, workspace)).toThrow()
    expect(() => validateNotificationBatch({ ...batch(), member_refs: [...batch().member_refs, ...batch().member_refs] }, workspace)).toThrow()
    expect(() => validateNotificationBatch({ ...batch(), entries: [entry({ event_digest: '8'.repeat(64) })] }, workspace)).toThrow()
    expect(() => validateNotificationBatch(batch(), '9'.repeat(64))).toThrow()
  })
  it('validates full preference identity and genuine timezone, preserving overnight windows', () => {
    expect(validateNotificationPreferences(preferences({ quiet_hours: [{ weekdays: [0, 6], start: '22:00', end: '07:00', full_day: false }] }), workspace).revision).toBe(1)
    for (const patch of [{ max_delay_seconds: 10 }, { timezone: 'Invalid/Zone' }, { quiet_hours: [{ weekdays: [0], start: '22:00', end: '22:00', full_day: false }] }]) expect(() => validateNotificationPreferences(preferences(patch), workspace)).toThrow()
  })
})

describe('notification prompt and physical presentation', () => {
  it('keeps the closed bell mounted with a badge without claiming, presenting or marking anything read', async () => {
    const { api } = mount(apiMock(), false)
    expect(await screen.findByRole('button', { name: '通知，1 条未读' })).toBeVisible()
    expect(screen.queryByRole('dialog')).toBeNull(); expect(api.getPersonalNotifications).not.toHaveBeenCalled()
    expect(api.claimPersonalNotification).not.toHaveBeenCalled(); expect(api.presentPersonalNotification).not.toHaveBeenCalled(); expect(api.readPersonalNotifications).not.toHaveBeenCalled()
  })
  it('claims an offered batch while the drawer is closed and reports only its visible exact members once', async () => {
    visibleGeometry(); const api = apiMock(undefined, true); api.getPersonalNotificationPrompt.mockResolvedValueOnce({ batch: batch(), quiet: quiet(), partial: false })
    mount(api, false)
    expect(await screen.findByRole('status', { name: '新通知' })).toBeVisible()
    await waitFor(() => expect(api.presentPersonalNotification).toHaveBeenCalledTimes(1))
    expect(api.claimPersonalNotification).toHaveBeenCalledTimes(1)
    const claimBody = api.claimPersonalNotification.mock.calls[0][0], presentBody = api.presentPersonalNotification.mock.calls[0][0]
    expect(presentBody).toMatchObject({ batch_id: batchId, batch_digest: digest, client_id: claimBody.client_id, claim_id: `no_${'4'.repeat(32)}`, event_refs: batch().member_refs })
    expect(presentBody.request_id).not.toBe(claimBody.request_id); expect(api.readPersonalNotifications).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '关闭新通知' })); expect(screen.queryByRole('status', { name: '新通知' })).toBeNull()
  })
  it('does not invent presentation for a DOM member with no visible rectangle', async () => {
    const api = apiMock(undefined, true); api.getPersonalNotificationPrompt.mockResolvedValueOnce({ batch: batch(), quiet: quiet(), partial: false }); mount(api, false)
    await screen.findByRole('status', { name: '新通知' }); await new Promise(resolve => setTimeout(resolve, 45))
    expect(api.presentPersonalNotification).not.toHaveBeenCalled(); expect(api.readPersonalNotifications).not.toHaveBeenCalled()
  })
  it('waits until every batch member has been physically visible, rather than treating a summary count as presentation', async () => {
    let scrolled = false
    const rows = [entry(), entry({ id: `ne_${'8'.repeat(32)}`, title: '第二条原结果' })], api = apiMock(rows, true)
    vi.spyOn(HTMLElement.prototype, 'getClientRects').mockImplementation(() => [rect()] as unknown as DOMRectList)
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      if (this.classList.contains('pn-toast-members')) return rect(0, 100)
      return this.dataset.notificationMember === rows[1].id ? scrolled ? rect(40, 60) : rect(120, 140) : rect()
    })
    vi.spyOn(document, 'elementFromPoint').mockImplementation((x, y) => [...document.querySelectorAll<HTMLElement>('[data-notification-member]')].find(element => { const box = element.getBoundingClientRect(); return x >= box.left && x <= box.right && y >= box.top && y <= box.bottom }) || null)
    api.getPersonalNotificationPrompt.mockResolvedValueOnce({ batch: batch(rows), quiet: quiet(), partial: false }); mount(api, false)
    const toast = await screen.findByRole('status', { name: '新通知' }); await new Promise(resolve => setTimeout(resolve, 45))
    expect(api.presentPersonalNotification).not.toHaveBeenCalled(); expect(within(toast).getByText('第二条原结果')).toBeInTheDocument()
    scrolled = true; fireEvent.scroll(toast.querySelector('.pn-toast-members')!)
    await waitFor(() => expect(api.presentPersonalNotification).toHaveBeenCalledTimes(1))
    expect(api.presentPersonalNotification.mock.calls[0][0].event_refs).toEqual(batch(rows).member_refs)
  })
  it('does not claim physical presentation for a toast covered by another dialog', async () => {
    visibleGeometry(); vi.mocked(document.elementFromPoint).mockReturnValue(document.body)
    const api = apiMock(undefined, true); api.getPersonalNotificationPrompt.mockResolvedValueOnce({ batch: batch(), quiet: quiet(), partial: false }); mount(api, false)
    await screen.findByRole('status', { name: '新通知' }); await new Promise(resolve => setTimeout(resolve, 45))
    expect(api.presentPersonalNotification).not.toHaveBeenCalled()
  })
  it('does not claim while hidden, and does not treat notification viewing as human read', async () => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' })
    const api = apiMock(undefined, true); api.getPersonalNotificationPrompt.mockResolvedValue({ batch: batch(), quiet: quiet(), partial: false }); mount(api)
    await screen.findByText('项目资料已核对'); expect(api.claimPersonalNotification).not.toHaveBeenCalled(); expect(api.readPersonalNotifications).not.toHaveBeenCalled()
  })
  it('reauthorizes after claim and suppresses a newly revoked source before rendering a toast', async () => {
    const api = apiMock(undefined, true); api.getPersonalNotificationPrompt.mockResolvedValueOnce({ batch: batch(), quiet: quiet(), partial: false })
    api.getPersonalNotificationBatch.mockResolvedValue(batch([entry({ availability: 'source_unavailable', title: 'PRIVATE' })])); mount(api, false)
    await waitFor(() => expect(api.getPersonalNotificationBatch).toHaveBeenCalled())
    expect(screen.queryByText('PRIVATE')).toBeNull(); expect(screen.queryByRole('status', { name: '新通知' })).toBeNull(); expect(api.presentPersonalNotification).not.toHaveBeenCalled()
  })
  it.each([
    { label: 'normal batch entering DND', kind: 'item_result' as const, bypass: false, muted: false, clock: false, drift: false, shown: false },
    { label: 'explicit failure bypass', kind: 'item_failure' as const, bypass: true, muted: false, clock: false, drift: false, shown: true },
    { label: 'mute overriding failure bypass', kind: 'item_failure' as const, bypass: true, muted: true, clock: false, drift: false, shown: false },
    { label: 'clock rollback overriding bypass', kind: 'item_failure' as const, bypass: true, muted: false, clock: true, drift: false, shown: false },
    { label: 'timezone drift overriding bypass', kind: 'calendar_approval' as const, bypass: true, muted: false, clock: false, drift: true, shown: false },
  ])('rechecks $label after claim, before rendering or presenting', async ({ kind, bypass, muted, clock, drift, shown }) => {
    visibleGeometry(); const rows = [entry({ kind })], api = apiMock(rows, true), prefs = preferences({ enabled: true, bypass_kinds: bypass && kind !== 'item_result' ? [kind] : [], muted_actor_ids: muted ? ['main'] : [] })
    api.getPersonalNotificationContext.mockResolvedValue(context(prefs)); api.getPersonalNotificationPrompt.mockResolvedValueOnce({ batch: batch(rows), quiet: quiet(), partial: false })
    api.getPersonalNotificationBatch.mockResolvedValue({ ...batch(rows), quiet: quiet({ quiet: true, clock_rollback: clock, timezone_data_changed: drift, hold_reason: clock ? 'clock_rollback' : drift ? 'timezone_data_changed' : null }) })
    mount(api, false); await waitFor(() => expect(api.getPersonalNotificationBatch).toHaveBeenCalled())
    if (shown) { await screen.findByRole('status', { name: '新通知' }); await waitFor(() => expect(api.presentPersonalNotification).toHaveBeenCalledTimes(1)) }
    else { await act(async () => { await Promise.resolve() }); expect(screen.queryByRole('status', { name: '新通知' })).toBeNull(); expect(api.presentPersonalNotification).not.toHaveBeenCalled() }
    expect(api.claimPersonalNotification).toHaveBeenCalledTimes(1)
  })
})

describe('notification unknown recovery and namespace fences', () => {
  it('preserves the latest presentation phase and never resurrects a dismissed toast from a late batch reauthorization', async () => {
    const api = apiMock(undefined, true); api.getPersonalNotificationPrompt.mockResolvedValueOnce({ batch: batch(), quiet: quiet(), partial: false })
    let finishPresentation!: () => void
    api.presentPersonalNotification.mockImplementationOnce(body => new Promise<NotificationMutation>(resolve => { finishPresentation = () => resolve(receipt(body.request_id, 'presentation', { ...body, presentation_id: `ns_${'5'.repeat(32)}` })) }))
    const hook = renderHook(() => usePersonalNotifications({ api, namespace: 'notice-A', connected: true, open: false }))
    await waitFor(() => expect(hook.result.current.toast?.phase).toBe('claimed')); await waitFor(() => expect(hook.result.current.loading).toBe(false))
    let releasePhase!: (value: NotificationBatch) => void
    api.getPersonalNotificationBatch.mockImplementationOnce(() => new Promise<NotificationBatch>(resolve => { releasePhase = resolve }))
    await act(async () => { window.dispatchEvent(new Event('focus')) }); await waitFor(() => expect(releasePhase).toBeTypeOf('function'))
    let reporting!: Promise<void>
    await act(async () => { reporting = hook.result.current.reportVisible(batchId); await Promise.resolve() })
    expect(hook.result.current.toast?.phase).toBe('reported'); expect(api.presentPersonalNotification).toHaveBeenCalledTimes(1)
    await act(async () => { releasePhase({ ...batch(), presentation_status: 'claimed' }); await Promise.resolve() })
    await waitFor(() => expect(hook.result.current.loading).toBe(false)); expect(hook.result.current.toast?.phase).toBe('reported')
    let releaseDismissed!: (value: NotificationBatch) => void
    api.getPersonalNotificationBatch.mockImplementationOnce(() => new Promise<NotificationBatch>(resolve => { releaseDismissed = resolve }))
    await act(async () => { window.dispatchEvent(new Event('focus')) }); await waitFor(() => expect(releaseDismissed).toBeTypeOf('function'))
    await act(async () => { hook.result.current.dismissToast(); releaseDismissed(batch()); await Promise.resolve() })
    expect(hook.result.current.toast).toBeNull()
    await act(async () => { finishPresentation(); await reporting })
    expect(hook.result.current.toast).toBeNull(); expect(api.claimPersonalNotification).toHaveBeenCalledTimes(1); expect(api.presentPersonalNotification).toHaveBeenCalledTimes(1)
    expect(localStorage.getItem(notificationPendingKey('notice-A'))).toBeNull()
  })
  it('lets an authorized same-query list finish beyond multiple poll intervals instead of canceling it into starvation', async () => {
    vi.useFakeTimers(); const api = apiMock(); let release!: (value: NotificationPage) => void
    api.getPersonalNotifications.mockImplementationOnce(() => new Promise<NotificationPage>(resolve => { release = resolve }))
    const hook = renderHook(() => usePersonalNotifications({ api, namespace: 'notice-A', connected: true, open: true }))
    await act(async () => { await vi.advanceTimersByTimeAsync(0) }); expect(api.getPersonalNotifications).toHaveBeenCalledTimes(1)
    const signal = api.getPersonalNotifications.mock.calls[0][1] as AbortSignal
    await act(async () => { await vi.advanceTimersByTimeAsync(45000) })
    expect(api.getPersonalNotifications).toHaveBeenCalledTimes(1); expect(api.getPersonalNotificationContext).toHaveBeenCalledTimes(1); expect(signal.aborted).toBe(false)
    await act(async () => { release(page()); await vi.advanceTimersByTimeAsync(0) })
    expect(hook.result.current.page?.entries[0].title).toBe('项目资料已核对'); expect(api.getPersonalNotificationContext).toHaveBeenCalledTimes(2)
    expect(api.getPersonalNotifications).toHaveBeenCalledTimes(2); expect(signal.aborted).toBe(false)
  })
  it('does not repeat context authorization when opening, closing or changing list filters and actor', async () => {
    const api = apiMock(), hook = renderHook(({ open }) => usePersonalNotifications({ api, namespace: 'notice-A', connected: true, open }), { initialProps: { open: false } })
    await waitFor(() => expect(hook.result.current.context).not.toBeNull()); expect(api.getPersonalNotificationContext).toHaveBeenCalledTimes(1); expect(api.getPersonalNotifications).not.toHaveBeenCalled()
    hook.rerender({ open: true }); await waitFor(() => expect(hook.result.current.page).not.toBeNull())
    await act(async () => { hook.result.current.changeFilter('all') }); await waitFor(() => expect(api.getPersonalNotifications.mock.lastCall?.[0]).toMatchObject({ filter: 'all' }))
    await act(async () => { hook.result.current.changeActor('main') }); await waitFor(() => expect(api.getPersonalNotifications.mock.lastCall?.[0]).toMatchObject({ filter: 'all', actor_id: 'main' }))
    hook.rerender({ open: false }); await waitFor(() => expect(hook.result.current.page).toBeNull())
    expect(api.getPersonalNotificationContext).toHaveBeenCalledTimes(1)
  })
  it('makes one new context read for a fast close, namespace switch and reopen, without old body reuse', async () => {
    const api = apiMock(); let release!: (value: NotificationContext) => void
    api.getPersonalNotificationContext.mockResolvedValueOnce(context()).mockImplementationOnce(() => new Promise<NotificationContext>(resolve => { release = resolve }))
    api.getPersonalNotifications.mockResolvedValueOnce(page([entry({ title: 'OLD_NAMESPACE_BODY' })])).mockResolvedValue(page([entry({ title: 'NEW_NAMESPACE_BODY' })]))
    const hook = renderHook(({ namespace, open }) => usePersonalNotifications({ api, namespace, connected: true, open }), { initialProps: { namespace: 'notice-A', open: true } })
    await waitFor(() => expect(hook.result.current.page?.entries[0].title).toBe('OLD_NAMESPACE_BODY'))
    hook.rerender({ namespace: 'notice-A', open: false }); hook.rerender({ namespace: 'notice-B', open: false }); hook.rerender({ namespace: 'notice-B', open: true })
    await waitFor(() => expect(api.getPersonalNotificationContext).toHaveBeenCalledTimes(2)); expect(hook.result.current.page).toBeNull(); expect(hook.result.current.context).toBeNull()
    await act(async () => { release(context()); await Promise.resolve() }); await waitFor(() => expect(hook.result.current.page?.entries[0].title).toBe('NEW_NAMESPACE_BODY'))
    expect(api.getPersonalNotificationContext).toHaveBeenCalledTimes(2)
  })
  it('coordinates in-flight context polling across focus and list changes without authorization fan-out', async () => {
    const api = apiMock(); let release!: (value: NotificationContext) => void
    api.getPersonalNotificationContext.mockImplementationOnce(() => new Promise<NotificationContext>(resolve => { release = resolve }))
    const hook = renderHook(({ open }) => usePersonalNotifications({ api, namespace: 'notice-A', connected: true, open }), { initialProps: { open: false } })
    await waitFor(() => expect(api.getPersonalNotificationContext).toHaveBeenCalledTimes(1))
    hook.rerender({ open: true }); await act(async () => { hook.result.current.changeFilter('all'); window.dispatchEvent(new Event('focus')); window.dispatchEvent(new Event('focus')) })
    expect(api.getPersonalNotificationContext).toHaveBeenCalledTimes(1)
    await act(async () => { release(context()); await Promise.resolve() }); await waitFor(() => expect(hook.result.current.page).not.toBeNull())
    expect(api.getPersonalNotifications.mock.lastCall?.[0]).toMatchObject({ filter: 'all' }); expect(api.getPersonalNotificationContext).toHaveBeenCalledTimes(1)
  })
  it('fences a same-microtask late list response when current context authorization fails', async () => {
    const api = apiMock(); let release!: (value: NotificationPage) => void, rejectContext!: (cause: Error) => void
    api.getPersonalNotifications.mockImplementationOnce(() => new Promise<NotificationPage>(resolve => { release = resolve }))
    api.getPersonalNotificationContext.mockResolvedValueOnce(context()).mockImplementationOnce(() => new Promise<NotificationContext>((_, reject) => { rejectContext = reject }))
    const hook = renderHook(() => usePersonalNotifications({ api, namespace: 'notice-A', connected: true, open: true }))
    await waitFor(() => expect(api.getPersonalNotifications).toHaveBeenCalledTimes(1)); await act(async () => { window.dispatchEvent(new Event('focus')) })
    await waitFor(() => expect(api.getPersonalNotificationContext).toHaveBeenCalledTimes(2))
    await act(async () => { rejectContext(new WorkbenchRequestError('当前工作区身份已变化', 403)); release(page([entry({ title: 'MUST_NOT_RENDER' })])); await Promise.resolve() })
    expect(hook.result.current.context).toBeNull()
    expect(hook.result.current.page).toBeNull(); expect(hook.result.current.error).toContain('身份已变化')
  })
  it('reconciles an unknown claim with GET only after remount and never replays its toast or POST', async () => {
    const api = apiMock(undefined, true); api.getPersonalNotificationPrompt.mockResolvedValue({ batch: batch(), quiet: quiet(), partial: false }); api.claimPersonalNotification.mockRejectedValue(new WorkbenchRequestError('503 UNKNOWN', 503))
    const first = mount(api, false); await waitFor(() => expect(api.claimPersonalNotification).toHaveBeenCalledTimes(1))
    const saved = parseNotificationPending(localStorage.getItem(notificationPendingKey('notice-A'))!)
    expect(JSON.stringify(saved)).not.toContain('项目资料'); expect(JSON.stringify(saved)).not.toContain('source_ref'); expect(JSON.stringify(saved)).not.toContain('event_refs')
    first.unmount(); api.getPersonalNotificationRequest.mockResolvedValue({ status: 'accepted', request_id: saved.request_id, operation: 'claim', identity: { batch_id: batchId, batch_digest: digest, client_id: saved.client_id, claim_id: `no_${'4'.repeat(32)}`, event_refs: batch().member_refs }, projection_available: true })
    mount(api); fireEvent.click(await screen.findByRole('button', { name: '核对原请求' }))
    await waitFor(() => expect(localStorage.getItem(notificationPendingKey('notice-A'))).toBeNull())
    expect(api.getPersonalNotificationRequest).toHaveBeenCalledWith(saved.request_id); expect(api.claimPersonalNotification).toHaveBeenCalledTimes(1)
    expect(api.presentPersonalNotification).not.toHaveBeenCalled(); expect(screen.queryByRole('status', { name: '新通知' })).toBeNull()
  })
  it('retains an unknown presentation receipt and never sends a fresh presentation after reload', async () => {
    visibleGeometry(); const api = apiMock(undefined, true); api.getPersonalNotificationPrompt.mockResolvedValueOnce({ batch: batch(), quiet: quiet(), partial: false }); api.presentPersonalNotification.mockRejectedValue(new Error('response lost'))
    const first = mount(api, false); await waitFor(() => expect(api.presentPersonalNotification).toHaveBeenCalledTimes(1))
    const saved = parseNotificationPending(localStorage.getItem(notificationPendingKey('notice-A'))!); expect(saved.operation).toBe('present'); first.unmount()
    api.getPersonalNotificationRequest.mockResolvedValue({ status: 'accepted', request_id: saved.request_id, operation: 'presentation', identity: { batch_id: batchId, batch_digest: digest, client_id: saved.client_id, claim_id: `no_${'4'.repeat(32)}`, presentation_id: `ns_${'5'.repeat(32)}`, event_refs: batch().member_refs }, projection_available: true })
    mount(api); fireEvent.click(await screen.findByRole('button', { name: '核对原请求' })); await waitFor(() => expect(localStorage.getItem(notificationPendingKey('notice-A'))).toBeNull())
    expect(api.presentPersonalNotification).toHaveBeenCalledTimes(1); expect(api.claimPersonalNotification).toHaveBeenCalledTimes(1); expect(screen.queryByRole('status', { name: '新通知' })).toBeNull()
  })
  it('keeps unknown preferences body out of storage, blocks fresh writes and preserves an unknown GET outcome', async () => {
    const api = apiMock(); api.setPersonalNotificationPreferences.mockRejectedValue(new Error('lost'))
    const hook = renderHook(() => usePersonalNotifications({ api, namespace: 'notice-A', connected: true, open: true }))
    await waitFor(() => expect(hook.result.current.context).not.toBeNull())
    await act(async () => { await hook.result.current.savePreferences({ ...notificationPreferenceInput(preferences()), timezone: 'Europe/London' }, 1); await hook.result.current.savePreferences(notificationPreferenceInput(preferences()), 1) })
    const raw = localStorage.getItem(notificationPendingKey('notice-A'))!
    expect(raw).not.toContain('Europe/London'); expect(raw).not.toContain('timezone'); expect(raw).not.toContain('quiet_hours'); expect(api.setPersonalNotificationPreferences).toHaveBeenCalledTimes(1)
    await act(async () => { expect(await hook.result.current.reconcile()).toBe(false) }); expect(localStorage.getItem(notificationPendingKey('notice-A'))).toBe(raw)
  })
  it('holds mismatched receipt identities instead of clearing the original request', async () => {
    const api = apiMock(); api.readPersonalNotifications.mockImplementationOnce(async body => receipt('wrong-request', 'read', { event_refs: body.event_refs, notice_read_ids: [`nr_${'6'.repeat(32)}`] }))
    const hook = renderHook(() => usePersonalNotifications({ api, namespace: 'notice-A', connected: true, open: true })); await waitFor(() => expect(hook.result.current.context).not.toBeNull())
    await act(async () => { expect(await hook.result.current.read([entry()])).toBe(false) }); expect(hook.result.current.pending?.operation).toBe('read'); expect(hook.result.current.error).toContain('回执身份')
  })
  it('does not transport if opaque request storage fails', async () => {
    const api = apiMock(), hook = renderHook(() => usePersonalNotifications({ api, namespace: 'notice-A', connected: true, open: true })); await waitFor(() => expect(hook.result.current.context).not.toBeNull())
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('storage full') })
    await act(async () => { await hook.result.current.read([entry()]) }); expect(api.readPersonalNotifications).not.toHaveBeenCalled()
  })
  it('fails closed for a tampered pending body', async () => {
    localStorage.setItem(notificationPendingKey('notice-A'), JSON.stringify({ title: 'PRIVATE' })); const { api } = mount()
    expect(await screen.findByText('原通知请求身份待核对')).toBeInTheDocument(); expect(await screen.findByRole('button', { name: '标记通知已读：项目资料已核对' })).toBeDisabled(); expect(api.readPersonalNotifications).not.toHaveBeenCalled()
  })
  it('ignores an old delayed page after namespace change even when the transport ignores abort', async () => {
    const api = apiMock(); let release!: (value: NotificationPage) => void
    api.getPersonalNotifications.mockImplementationOnce(() => new Promise<NotificationPage>(resolve => { release = resolve }))
    const hook = renderHook(({ namespace }) => usePersonalNotifications({ api, namespace, connected: true, open: true }), { initialProps: { namespace: 'notice-A' } })
    await waitFor(() => expect(api.getPersonalNotifications).toHaveBeenCalledTimes(1)); hook.rerender({ namespace: 'notice-B' }); await waitFor(() => expect(hook.result.current.page).not.toBeNull())
    await act(async () => { release(page([entry({ title: 'OLD_NAMESPACE_PRIVATE' })])); await Promise.resolve() })
    expect(hook.result.current.page?.entries[0].title).toBe('项目资料已核对'); expect(hook.result.current.pending).toBeNull()
  })
  it('clears source text and toast immediately when disconnected', async () => {
    const api = apiMock(), hook = renderHook(({ connected }) => usePersonalNotifications({ api, namespace: 'notice-A', connected, open: true }), { initialProps: { connected: true } })
    await waitFor(() => expect(hook.result.current.page).not.toBeNull()); hook.rerender({ connected: false }); expect(hook.result.current.page).toBeNull(); expect(hook.result.current.context).toBeNull(); expect(hook.result.current.toast).toBeNull()
  })
})

describe('operative light notification workspace', () => {
  it('opens the exact original result without marking either the notification or original result read', async () => {
    const state = mount(); fireEvent.click(await screen.findByRole('button', { name: '打开原结果：项目资料已核对' }))
    expect(state.onOpenResult).toHaveBeenCalledWith(occurrenceId); expect(state.onClose).toHaveBeenCalled(); expect(state.api.readPersonalNotifications).not.toHaveBeenCalled()
  })
  it('routes an exact calendar action and item independently', async () => {
    const rows = [entry({ jump: { kind: 'calendar_action', id: actionId }, title: '待审批日程', kind: 'calendar_approval' }), entry({ id: `ne_${'8'.repeat(32)}`, jump: { kind: 'item', id: itemId }, title: '原事项' })]
    const state = mount(apiMock(rows)); fireEvent.click(await screen.findByRole('button', { name: '打开原动作：待审批日程' })); fireEvent.click(screen.getByRole('button', { name: '打开原事项：原事项' }))
    expect(state.onOpenAction).toHaveBeenCalledWith(actionId); expect(state.onOpenItem).toHaveBeenCalledWith(itemId); expect(state.api.readPersonalNotifications).not.toHaveBeenCalled()
  })
  it('marks only explicitly clicked notification refs read, including opaque unavailable entries', async () => {
    const api = apiMock([entry({ availability: 'source_unavailable', title: 'PRIVATE' })]); mount(api)
    expect(await screen.findByText('原来源已不可用')).toBeInTheDocument(); expect(screen.queryByText('PRIVATE')).toBeNull(); expect(screen.queryByRole('button', { name: /打开原结果/ })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '标记通知已读：来源不可用' })); await waitFor(() => expect(api.readPersonalNotifications).toHaveBeenCalledTimes(1))
    expect(api.readPersonalNotifications.mock.calls[0][0].event_refs).toEqual([{ event_id: eventId, event_digest: digest }])
    expect(api.claimPersonalNotification).not.toHaveBeenCalled(); expect(api.presentPersonalNotification).not.toHaveBeenCalled()
  })
  it('changes read-only filters and fairly paginates without creating read receipts', async () => {
    const api = apiMock(); api.getPersonalNotifications.mockResolvedValueOnce({ ...page(), next_after: eventId }).mockResolvedValueOnce(page([entry({ id: `ne_${'8'.repeat(32)}`, title: '另一条通知' })]))
    mount(api); fireEvent.click(await screen.findByRole('button', { name: '加载更多' })); await screen.findByText('另一条通知'); expect(api.getPersonalNotifications.mock.calls[1][0]).toMatchObject({ after: eventId, filter: 'unread' })
    fireEvent.click(screen.getByRole('tab', { name: '需处理' })); await waitFor(() => expect(api.getPersonalNotifications.mock.lastCall?.[0]).toMatchObject({ filter: 'attention' })); expect(api.readPersonalNotifications).not.toHaveBeenCalled()
  })
  it('saves complete DND, merging, bypass and mute settings with the exact observed CAS revision', async () => {
    const api = apiMock(); mount(api); fireEvent.click(await screen.findByRole('button', { name: '通知设置' }))
    fireEvent.click(screen.getByRole('checkbox', { name: '主动提醒' })); fireEvent.click(screen.getByRole('button', { name: '添加' })); fireEvent.click(screen.getByRole('checkbox', { name: '执行失败' })); fireEvent.click(screen.getByRole('checkbox', { name: '主 Agent' })); fireEvent.click(screen.getByRole('checkbox', { name: '包含现有结果与待审批动作' })); fireEvent.click(screen.getByRole('button', { name: '保存设置' }))
    await waitFor(() => expect(api.setPersonalNotificationPreferences).toHaveBeenCalledTimes(1))
    expect(api.setPersonalNotificationPreferences.mock.calls[0][0]).toMatchObject({ expected_revision: 1, enabled: true, timezone: 'Asia/Shanghai', quiet_hours: [{ weekdays: [0, 1, 2, 3, 4, 5, 6], start: '22:00', end: '07:00', full_day: false }], muted_actor_ids: ['main'], bypass_kinds: ['item_failure'], include_existing: true, debounce_seconds: 60, max_delay_seconds: 300 })
  })
  it('preserves a locally edited settings draft after an external revision, and explicitly loads the new version', async () => {
    const save = vi.fn().mockResolvedValue(false), view = render(<NotificationSettings preferences={preferences()} actors={[{ id: 'main', name: '主 Agent' }]} locked={false} onSave={save} />)
    fireEvent.change(screen.getByRole('combobox', { name: '通知时区' }), { target: { value: 'Europe/London' } })
    view.rerender(<NotificationSettings preferences={preferences({ revision: 2 })} actors={[{ id: 'main', name: '主 Agent' }]} locked={false} onSave={save} />)
    expect(screen.getByRole('combobox', { name: '通知时区' })).toHaveValue('Europe/London'); expect(screen.getByRole('button', { name: '保存设置' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '载入新版本' })); expect(screen.getByRole('combobox', { name: '通知时区' })).toHaveValue('Asia/Shanghai')
  })
  it('validates equal-time windows as explicitly full-day and rejects incomplete weekdays before saving', async () => {
    const save = vi.fn().mockResolvedValue(true); render(<NotificationSettings preferences={preferences()} actors={[]} locked={false} onSave={save} />)
    fireEvent.click(screen.getByRole('button', { name: '添加' })); fireEvent.change(screen.getByLabelText('时段 1 结束'), { target: { value: '22:00' } }); fireEvent.click(screen.getByRole('button', { name: '保存设置' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('相同起止时间需选择全天'); expect(save).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('checkbox', { name: '时段 1 全天' })); fireEvent.click(screen.getByRole('button', { name: '保存设置' })); await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(save.mock.calls[0][0].quiet_hours[0]).toMatchObject({ start: '22:00', end: '22:00', full_day: true })
  })
  it('shows truthful clock rollback and timezone-data holds without active prompts', async () => {
    const api = apiMock(undefined, true); api.getPersonalNotificationContext.mockResolvedValue({ ...context(preferences({ enabled: true })), quiet: quiet({ hold_reason: 'clock_rollback', clock_rollback: true }) }); mount(api)
    expect(await screen.findByText('时钟已回退，主动提醒暂停')).toBeInTheDocument(); expect(api.getPersonalNotificationPrompt).not.toHaveBeenCalled()
  })
  it('opens an exact deep-linked Calendar action even when the paged action list completes later without it', async () => {
    let release!: (page: { actions: CalendarAction[] }) => void
    const action: CalendarAction = { id: actionId, revision: 1, digest, connection_id: `pc_${'9'.repeat(32)}`, auth_epoch: 1, actor_id: 'main', account_email: 'calendar@example.test', operation: 'insert', status: 'draft', event_id: null, base_etag: null,
      body: { summary: '精确日历草稿', description: '', start: { dateTime: '2099-11-06T01:00:00Z', timeZone: 'Asia/Shanghai' }, end: { dateTime: '2099-11-06T02:00:00Z', timeZone: 'Asia/Shanghai' } },
      created_at: '2026-11-05T02:00:00Z', updated_at: '2026-11-05T02:00:00Z', expires_at: '2099-11-05T02:00:00Z', source_refs: [], artifact_refs: [], memory_refs: [], memory_item_id: null, approval: null, result: null, job_id: null, run_id: null }
    const api = { personalConnectionContext: vi.fn().mockResolvedValue({ owner_key: digest, workspace_key: workspace, provider: 'google_calendar', client_configured: true }), personalConnections: vi.fn().mockResolvedValue({ connections: [] }), personalCalendarActions: vi.fn().mockImplementation(() => new Promise<{ actions: CalendarAction[] }>(resolve => { release = resolve })), personalCalendarAction: vi.fn().mockResolvedValue(action) } as unknown as PersonalConnectionsApi
    render(<ConnectionWorkspace api={api} namespace="calendar-A" agents={[]} connected initialActionId={actionId} onClose={vi.fn()} />)
    expect(await screen.findByRole('heading', { name: '精确日历草稿', level: 2 })).toBeInTheDocument()
    expect(api.personalCalendarAction).toHaveBeenCalledWith(actionId, expect.any(AbortSignal))
    await act(async () => { release({ actions: [] }); await Promise.resolve() })
    expect(screen.getByRole('heading', { name: '精确日历草稿', level: 2 })).toBeInTheDocument(); expect(screen.getByRole('tab', { name: /动作审批/ })).toHaveAttribute('aria-selected', 'true')
  })
})

describe('notification authenticated HTTP client', () => {
  it('uses all ten dedicated endpoints, bearer headers, no-store reads and encoded exact request IDs', async () => {
    const observed: { method: string; path: string; token: string | null; body: unknown }[] = []
    const server = setupServer(http.all('http://notice.local/personal/notifications*', async ({ request }) => {
      const path = new URL(request.url).pathname
      observed.push({ method: request.method, path, token: request.headers.get('Authorization'), body: request.method === 'POST' ? await request.json() : null })
      return HttpResponse.json({ ok: true })
    }))
    server.listen({ onUnhandledRequest: 'error' })
    const spy = vi.spyOn(globalThis, 'fetch')
    try {
      const client = new WorkbenchRuntimeClient('http://notice.local', async () => 'local-token')
      await client.getPersonalNotificationContext(); await client.getPersonalNotifications({ filter: 'attention', actor_id: 'main', limit: 10, after: eventId }); await client.getPersonalNotificationPreferences(); await client.getPersonalNotificationBatch(batchId); await client.getPersonalNotificationRequest('opaque/request:1'); await client.getPersonalNotificationPrompt()
      await client.setPersonalNotificationPreferences({ ...notificationPreferenceInput(preferences()), request_id: 'prefs', expected_revision: 1 }); await client.claimPersonalNotification({ request_id: 'claim', batch_id: batchId, expected_digest: digest, client_id: `nc_${'7'.repeat(32)}` }); await client.presentPersonalNotification({ request_id: 'present', batch_id: batchId, batch_digest: digest, client_id: `nc_${'7'.repeat(32)}`, claim_id: `no_${'4'.repeat(32)}`, event_refs: batch().member_refs }); await client.readPersonalNotifications({ request_id: 'read', event_refs: batch().member_refs })
      expect(observed).toHaveLength(10); expect(observed.every(value => value.token === 'Bearer local-token')).toBe(true)
      expect(observed[4].path).toBe('/personal/notifications/requests/opaque%2Frequest%3A1'); expect(observed.slice(6).map(value => value.path)).toEqual(['/personal/notifications/preferences', '/personal/notifications/claim', '/personal/notifications/presentation', '/personal/notifications/notice-read'])
      expect(spy.mock.calls.every(([, init]) => init?.cache === 'no-store')).toBe(true)
    } finally { server.close() }
  })
})
