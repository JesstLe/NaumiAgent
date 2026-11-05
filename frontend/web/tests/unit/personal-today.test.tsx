import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { WorkbenchRequestError, WorkbenchRuntimeClient } from '@naumi/shared/api/WorkbenchRuntimeClient'
import type { TodayContext, TodayDayView, TodayDelivery, TodayEntry, TodayMutation, TodayQuery } from '@naumi/shared/api/personalToday'
import { PersonalToday } from '../../../web2/src/personal/today/PersonalToday'
import { parseTodayPending, todayPendingKey } from '../../../web2/src/personal/today/todayLocation'
import { usePersonalToday, validateTodayEntry, validateTodayView, type PersonalTodayApi } from '../../../web2/src/personal/today/usePersonalToday'
import { requestArtifactNavigation } from '../../../web2/src/personal/library/navigation'
import { PersonalItems } from '../../../web2/src/personal/items/PersonalItems'
import type { PersonalItemsApi } from '../../../web2/src/personal/items/usePersonalItems'

const workspace = 'a'.repeat(64), digest = 'b'.repeat(64), dayId = `td_${'c'.repeat(32)}`, metadataId = `tm_${'d'.repeat(32)}`
const itemId = `pi_${'e'.repeat(32)}`, placementId = `tp_${'f'.repeat(32)}`, occurrenceId = `pio_${'1'.repeat(32)}`
const context: TodayContext = { workspace_key: workspace, default_planner: 'main', planners: [{ id: 'main', name: '主 Agent', version: 0 }, { id: `pa_${'2'.repeat(32)}`, name: 'Kai', version: 3 }], limits: {}, server_now: '2026-11-05T01:00:00Z' }
const query: TodayQuery = { date: '2026-11-05', timezone: 'Asia/Shanghai', planner_id: 'main' }
const source = { kind: 'item' as const, workspace_key: workspace, item_id: itemId, item_revision: 1, once_key: '3'.repeat(32), definition_sha256: digest, assignee_id: 'main', assignee_version: 0 }
const entry = (changes: Partial<TodayEntry> = {}): TodayEntry => ({ id: metadataId, metadata_id: metadataId, metadata_revision: 1, placement_id: placementId, item_ref: source,
  title: '核对项目资料', availability: 'available', assignee: { id: 'main', name: '主 Agent', version: 0 }, item_status: 'active', execution_status: 'unknown',
  due_at: '2026-11-05T01:00:00Z', deadline: null, priority: 'normal', human_check: null, metadata_status: 'active', planned: true, disposition: 'include', report: null, ...changes })
const delivery = (): TodayDelivery => ({ item_id: itemId, item_revision: 1, occurrence_id: occurrenceId, job_id: 'job-original', run_id: 'run-original', publication_id: 'pub-original', delivery_id: 'delivery-original', delivery_sha256: digest, delivered_at: '2026-11-05T02:00:00Z', read_receipt: null, artifacts: [], title: '原始核对报告', assignee_id: 'main' })
const day = (changes: Partial<TodayDayView> = {}): TodayDayView => ({ workspace_key: workspace, day: { id: dayId, revision: 2, digest, ...query, planner_id: 'main', timezone_sha256: digest, placements: [{ id: placementId, metadata_id: metadataId, source_ref: source, disposition: 'include' }] }, window: { start_at: '2026-11-04T16:00:00Z', end_at: '2026-11-05T16:00:00Z' },
  entries: [entry()], routines: [{ id: `pr_${'4'.repeat(32)}`, title: '每日检查', availability: 'available', status: 'active', attention_code: null, next_due_at: '2026-11-05T04:00:00Z', assignee_id: 'main', plan_ref: {} }], deliveries: [delivery()], sources: { items: { partial: false }, routines: { partial: false }, deliveries: { partial: false } }, next_cursor: null, observed_at: '2026-11-05T03:00:00Z', consistency: 'live_keyset', ...changes })
const receipt = (requestId: string, operation: string, changes: Partial<TodayMutation> = {}): TodayMutation => ({ accepted: true, request_id: requestId, operation, identity: { day_id: dayId, day_revision: 3, metadata_ids: [metadataId], change_id: `tc_${'5'.repeat(32)}` }, projection_available: true, ...changes })
function apiMock(view = day()) {
  return {
    getPersonalTodayContext: vi.fn().mockResolvedValue(context), getPersonalToday: vi.fn().mockImplementation(async (q: TodayQuery) => ({ ...view, day: { ...view.day, date: q.date, timezone: q.timezone, planner_id: q.planner_id || 'main' } })),
    getPersonalTodaySources: vi.fn().mockResolvedValue({ entries: [entry({ planned: false, placement_id: null, metadata_revision: 0 })], next_after: null, partial: false, consistency: 'live_keyset' }),
    getPersonalTodayMetadata: vi.fn(), getPersonalTodayDay: vi.fn().mockResolvedValue(view), getPersonalTodayHistory: vi.fn().mockResolvedValue({ history: [], next_after_revision: null }),
    getPersonalTodayRequest: vi.fn().mockResolvedValue({ status: 'unknown', request_id: '6'.repeat(32) }),
    mutatePersonalToday: vi.fn().mockImplementation(async (operation: string, body: { request_id: string }) => receipt(body.request_id, operation)),
    personalItem: vi.fn(), personalItemResult: vi.fn(), readPersonalItemResult: vi.fn(),
  } satisfies PersonalTodayApi
}
function renderToday(api = apiMock()) {
  const onClose = vi.fn(), onOpenItem = vi.fn(), onOpenResult = vi.fn(), onOpenRoutine = vi.fn()
  return { api, onClose, onOpenItem, onOpenResult, onOpenRoutine, ...render(<PersonalToday api={api} namespace="workspace-A" connected onClose={onClose} onOpenItem={onOpenItem} onOpenResult={onOpenResult} onOpenRoutine={onOpenRoutine} />) }
}
beforeEach(() => { localStorage.clear(); history.replaceState({}, '', '/web2?today=1&today_date=2026-11-05&today_timezone=Asia%2FShanghai') })
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('Today exact identity and request state', () => {
  it('rejects private body fields in pending storage and clears revoked source text before rendering', () => {
    expect(() => parseTodayPending(JSON.stringify({ version: 1, request_id: '6'.repeat(32), operation: 'place', workspace_key: workspace, title: 'private' }))).toThrow()
    const value = validateTodayEntry(entry({ availability: 'source_unavailable', title: 'MUST_NOT_RENDER', report: delivery() }), workspace)
    expect(value.title).toBeNull(); expect(value.assignee).toBeNull(); expect(value.report).toBeNull()
    expect(() => validateTodayView({ ...day(), projection_unavailable: 'true' } as unknown as TodayDayView, workspace, query)).toThrow()
  })
  it('keeps 503 unknown opaque, reconciles original identity after remount, and never repeats POST', async () => {
    const api = apiMock(); api.mutatePersonalToday.mockRejectedValueOnce(new WorkbenchRequestError('503 UNKNOWN', 503))
    const first = renderHook(() => usePersonalToday({ api, namespace: 'workspace-A', connected: true }))
    await waitFor(() => expect(first.result.current.view).not.toBeNull())
    const requestId = '6'.repeat(32)
    await act(async () => { await first.result.current.submit({ operation: 'set-metadata', body: { request_id: requestId, source_ref: source, expected_revision: 1, priority: 'high', deadline: { kind: 'date', local_date: '2026-11-06', timezone: 'Asia/Shanghai' }, status: 'active' } }) })
    const stored = localStorage.getItem(todayPendingKey('workspace-A'))!
    expect(stored).not.toContain('local_date'); expect(stored).not.toContain(itemId); expect(stored).not.toContain('source_ref'); expect(stored).not.toContain('priority')
    first.unmount()
    api.getPersonalTodayRequest.mockResolvedValueOnce({ status: 'accepted', ...receipt(requestId, 'set_metadata') })
    const second = renderHook(() => usePersonalToday({ api, namespace: 'workspace-A', connected: true }))
    await waitFor(() => expect(second.result.current.pending?.request_id).toBe(requestId))
    await waitFor(() => expect(second.result.current.context).not.toBeNull())
    await act(async () => { expect(await second.result.current.reconcile()).toBe(true) })
    expect(api.mutatePersonalToday).toHaveBeenCalledTimes(1)
    expect(api.getPersonalTodayRequest).toHaveBeenCalledWith(requestId)
    expect(localStorage.getItem(todayPendingKey('workspace-A'))).toBeNull()
  })
  it('drops delayed responses from a prior namespace even if transport ignores AbortSignal', async () => {
    const api = apiMock(); let release!: (value: TodayDayView) => void
    api.getPersonalToday.mockImplementationOnce(() => new Promise<TodayDayView>(resolve => { release = resolve }))
    const hook = renderHook(({ namespace }) => usePersonalToday({ api, namespace, connected: true }), { initialProps: { namespace: 'workspace-A' } })
    await waitFor(() => expect(api.getPersonalToday).toHaveBeenCalledTimes(1))
    hook.rerender({ namespace: 'workspace-B' })
    await waitFor(() => expect(hook.result.current.view?.entries[0].title).toBe('核对项目资料'))
    await act(async () => { release(day({ entries: [entry({ title: 'OLD_NAMESPACE_SECRET' })] })); await Promise.resolve() })
    expect(hook.result.current.view?.entries[0].title).not.toBe('OLD_NAMESPACE_SECRET')
  })
})

describe('Today foreground read subscription', () => {
  it('restores opaque pending state while inactive and defers refresh/query reads until foreground returns', async () => {
    const requestId = '6'.repeat(32), saved = { version: 1, request_id: requestId, operation: 'check', workspace_key: workspace, metadata_id: metadataId }
    localStorage.setItem(todayPendingKey('workspace-A'), JSON.stringify(saved))
    const api = apiMock(), ui = renderHook(({ readActive }) => usePersonalToday({ api, namespace: 'workspace-A', connected: true, readActive }), { initialProps: { readActive: false } })
    await waitFor(() => expect(ui.result.current.pending?.request_id).toBe(requestId))
    act(() => { ui.result.current.refresh(); ui.result.current.changeQuery({ ...query, date: '2026-11-06' }) })
    expect(api.getPersonalTodayContext).not.toHaveBeenCalled(); expect(api.getPersonalToday).not.toHaveBeenCalled(); expect(ui.result.current.context).toBeNull(); expect(ui.result.current.view).toBeNull()
    ui.rerender({ readActive: true })
    await waitFor(() => expect(ui.result.current.view?.day.date).toBe('2026-11-06'))
    expect(api.getPersonalTodayContext).toHaveBeenCalledTimes(1); expect(api.getPersonalToday).toHaveBeenCalledTimes(1)
    expect(ui.result.current.pending?.request_id).toBe(requestId); expect(localStorage.getItem(todayPendingKey('workspace-A'))).toBe(JSON.stringify(saved))
  })
  it('aborts a hidden context read and ignores its late response across a namespace change', async () => {
    const api = apiMock(); let release!: (value: TodayContext) => void, originalSignal!: AbortSignal
    api.getPersonalTodayContext.mockImplementationOnce((signal?: AbortSignal) => { originalSignal = signal!; return new Promise<TodayContext>(resolve => { release = resolve }) })
    const ui = renderHook(({ namespace, readActive }) => usePersonalToday({ api, namespace, connected: true, readActive }), { initialProps: { namespace: 'workspace-A', readActive: true } })
    await waitFor(() => expect(api.getPersonalTodayContext).toHaveBeenCalledTimes(1))
    ui.rerender({ namespace: 'workspace-B', readActive: false }); expect(originalSignal.aborted).toBe(true)
    await act(async () => { release({ ...context, default_planner: 'OLD_PRIVATE_PLANNER' }); await Promise.resolve() })
    expect(api.getPersonalToday).not.toHaveBeenCalled(); expect(ui.result.current.context).toBeNull()
    ui.rerender({ namespace: 'workspace-B', readActive: true })
    await waitFor(() => expect(ui.result.current.view).not.toBeNull())
    expect(api.getPersonalTodayContext).toHaveBeenCalledTimes(2); expect(api.getPersonalToday).toHaveBeenCalledTimes(1)
    expect(ui.result.current.query.planner_id).toBe('main')
  })
  it('aborts and fences a hidden day read even when its transport resolves after reactivation', async () => {
    const api = apiMock(); let release!: (value: TodayDayView) => void, signal!: AbortSignal
    api.getPersonalToday.mockImplementationOnce((_q: TodayQuery, passed?: AbortSignal) => { signal = passed!; return new Promise<TodayDayView>(resolve => { release = resolve }) })
    const ui = renderHook(({ readActive }) => usePersonalToday({ api, namespace: 'workspace-A', connected: true, readActive }), { initialProps: { readActive: true } })
    await waitFor(() => expect(api.getPersonalToday).toHaveBeenCalledTimes(1)); ui.rerender({ readActive: false })
    expect(signal.aborted).toBe(true); expect(ui.result.current.view).toBeNull(); expect(ui.result.current.loading).toBe(false)
    ui.rerender({ readActive: true }); await waitFor(() => expect(ui.result.current.view?.entries[0].title).toBe('核对项目资料'))
    await act(async () => { release(day({ entries: [entry({ title: 'LATE_HIDDEN_SOURCE' })] })); await Promise.resolve() })
    expect(ui.result.current.view?.entries[0].title).toBe('核对项目资料'); expect(api.getPersonalTodayContext).toHaveBeenCalledTimes(2)
  })
  it('keeps an in-flight original human write and its lock while pausing only reads', async () => {
    const api = apiMock(); let release!: (value: TodayMutation) => void
    api.mutatePersonalToday.mockImplementationOnce(() => new Promise<TodayMutation>(resolve => { release = resolve }))
    const ui = renderHook(({ readActive }) => usePersonalToday({ api, namespace: 'workspace-A', connected: true, readActive }), { initialProps: { readActive: true } })
    await waitFor(() => expect(ui.result.current.view).not.toBeNull())
    const originalSubmit = ui.result.current.submit, requestId = '6'.repeat(32), write = { operation: 'check' as const, body: { request_id: requestId, metadata_id: metadataId, expected_revision: 1, source_ref: source, checked: true } }
    let submitted!: Promise<boolean>; act(() => { submitted = originalSubmit(write) })
    ui.rerender({ readActive: false }); expect(ui.result.current.posting).toBe(true); expect(ui.result.current.pending?.request_id).toBe(requestId)
    await act(async () => { expect(await originalSubmit({ ...write, body: { ...write.body, request_id: '7'.repeat(32) } })).toBe(false) })
    await act(async () => { release(receipt(requestId, 'check')); expect(await submitted).toBe(true) })
    expect(ui.result.current.pending).toBeNull(); expect(ui.result.current.posting).toBe(false); expect(api.mutatePersonalToday).toHaveBeenCalledTimes(1)
    expect(api.getPersonalTodayContext).toHaveBeenCalledTimes(1); expect(api.getPersonalToday).toHaveBeenCalledTimes(1)
    ui.rerender({ readActive: true }); await waitFor(() => expect(api.getPersonalToday).toHaveBeenCalledTimes(2)); expect(api.getPersonalTodayContext).toHaveBeenCalledTimes(2)
  })
  it('retains UNKNOWN from a paused original write and reconciles only its original request after return', async () => {
    const api = apiMock(); let fail!: (cause: Error) => void
    api.mutatePersonalToday.mockImplementationOnce(() => new Promise<TodayMutation>((_resolve, reject) => { fail = reject }))
    const ui = renderHook(({ readActive }) => usePersonalToday({ api, namespace: 'workspace-A', connected: true, readActive }), { initialProps: { readActive: true } })
    await waitFor(() => expect(ui.result.current.view).not.toBeNull()); const requestId = '6'.repeat(32)
    let submitted!: Promise<boolean>; act(() => { submitted = ui.result.current.submit({ operation: 'check', body: { request_id: requestId, metadata_id: metadataId, expected_revision: 1, source_ref: source, checked: true } }) })
    ui.rerender({ readActive: false }); await act(async () => { fail(new WorkbenchRequestError('original response UNKNOWN', 503)); expect(await submitted).toBe(false) })
    expect(ui.result.current.pending?.request_id).toBe(requestId); expect(api.getPersonalTodayRequest).not.toHaveBeenCalled(); expect(api.getPersonalToday).toHaveBeenCalledTimes(1)
    api.getPersonalTodayRequest.mockResolvedValueOnce({ status: 'accepted', ...receipt(requestId, 'check') })
    ui.rerender({ readActive: true }); await waitFor(() => expect(ui.result.current.context).not.toBeNull())
    await act(async () => { expect(await ui.result.current.reconcile()).toBe(true) })
    expect(api.getPersonalTodayRequest).toHaveBeenCalledWith(requestId); expect(api.mutatePersonalToday).toHaveBeenCalledTimes(1); expect(ui.result.current.pending).toBeNull()
  })
  it('aborts pagination and rejects old more callbacks while hidden without merging a late page', async () => {
    const api = apiMock(); let release!: (value: TodayDayView) => void, pagingSignal!: AbortSignal
    api.getPersonalToday.mockImplementation((q: TodayQuery, signal?: AbortSignal) => q.cursor ? new Promise<TodayDayView>(resolve => { pagingSignal = signal!; release = resolve }) : Promise.resolve(day({ next_cursor: 'original-cursor' })))
    const ui = renderHook(({ readActive }) => usePersonalToday({ api, namespace: 'workspace-A', connected: true, readActive }), { initialProps: { readActive: true } })
    await waitFor(() => expect(ui.result.current.view?.next_cursor).toBe('original-cursor'))
    const oldMore = ui.result.current.more; let paging!: Promise<void>; act(() => { paging = oldMore() })
    await waitFor(() => expect(pagingSignal).toBeDefined()); ui.rerender({ readActive: false }); expect(pagingSignal.aborted).toBe(true)
    await act(async () => { await oldMore() }); expect(api.getPersonalToday).toHaveBeenCalledTimes(2)
    ui.rerender({ readActive: true }); await waitFor(() => expect(ui.result.current.view).not.toBeNull())
    await act(async () => { release(day({ entries: [entry({ metadata_id: `tm_${'8'.repeat(32)}`, title: 'LATE_PRIVATE_PAGE' })] })); await paging })
    expect(ui.result.current.view?.entries.some(value => value.title === 'LATE_PRIVATE_PAGE')).toBe(false); expect(ui.result.current.loading).toBe(false)
  })
  it('keeps real disconnect distinct from read pause and reacquires the original context on reconnect', async () => {
    const api = apiMock(), ui = renderHook(({ connected, readActive }) => usePersonalToday({ api, namespace: 'workspace-A', connected, readActive }), { initialProps: { connected: true, readActive: true } })
    await waitFor(() => expect(ui.result.current.view).not.toBeNull()); ui.rerender({ connected: false, readActive: true })
    act(() => ui.result.current.refresh()); expect(ui.result.current.view).toBeNull(); expect(ui.result.current.context).toBeNull(); expect(api.getPersonalTodayContext).toHaveBeenCalledTimes(1)
    ui.rerender({ connected: true, readActive: false }); expect(api.getPersonalTodayContext).toHaveBeenCalledTimes(1)
    ui.rerender({ connected: true, readActive: true }); await waitFor(() => expect(ui.result.current.view).not.toBeNull()); expect(api.getPersonalTodayContext).toHaveBeenCalledTimes(2); expect(api.getPersonalToday).toHaveBeenCalledTimes(2)
  })
  it('does not reuse a pre-pause private projection when fresh context authorization fails', async () => {
    const api = apiMock(), ui = renderHook(({ readActive }) => usePersonalToday({ api, namespace: 'workspace-A', connected: true, readActive }), { initialProps: { readActive: true } })
    await waitFor(() => expect(ui.result.current.view).not.toBeNull()); ui.rerender({ readActive: false }); api.getPersonalTodayContext.mockRejectedValueOnce(new WorkbenchRequestError('原工作区权限已变化', 403)); ui.rerender({ readActive: true })
    await waitFor(() => expect(ui.result.current.error).toBe('原工作区权限已变化')); expect(ui.result.current.view).toBeNull(); expect(ui.result.current.context).toBeNull(); expect(api.getPersonalToday).toHaveBeenCalledTimes(1)
  })
})

describe('Today operative light workspace', () => {
  it('requires a human click to confirm an empty opaque day with the full empty placement CAS set', async () => {
    let confirmed = false
    const api = apiMock()
    api.getPersonalToday.mockImplementation(async (q: TodayQuery) => day({
      day: { ...day().day, date: q.date, timezone: q.timezone, planner_id: q.planner_id || 'main',
        revision: confirmed ? 3 : 2, placements: [] },
      entries: [], routines: [], deliveries: [], projection_unavailable: !confirmed,
    }))
    api.mutatePersonalToday.mockImplementation(async (operation: string, body: { request_id: string }) => {
      expect(operation).toBe('reorder')
      expect(body).toMatchObject({ day_id: dayId, expected_revision: 2, placement_ids: [] })
      confirmed = true
      return receipt(body.request_id, operation, { identity: {
        day_id: dayId, day_revision: 3, metadata_ids: [], change_id: `tc_${'5'.repeat(32)}`,
      } })
    })
    renderToday(api)
    const confirm = await screen.findByRole('button', { name: '确认当前顺序' })
    await waitFor(() => expect(confirm).toBeEnabled())
    expect(api.mutatePersonalToday).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: '保存顺序' })).toBeNull()
    fireEvent.click(confirm)
    await waitFor(() => expect(api.mutatePersonalToday).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(localStorage.getItem(todayPendingKey('workspace-A'))).toBeNull())
    await waitFor(() => expect(api.getPersonalToday.mock.calls.length).toBeGreaterThan(1))
    await waitFor(() => expect(screen.getByRole('button', { name: '加入安排' })).toBeEnabled())
    expect(screen.queryByRole('button', { name: '确认当前顺序' })).toBeNull()
    expect(screen.queryByText('原请求待核对')).toBeNull()
    expect(api.getPersonalTodayRequest).not.toHaveBeenCalled()
  })
  it('accepts a move receipt bound to its original source day and refreshes after target CAS', async () => {
    const targetDayId = `td_${'6'.repeat(32)}`, targetDate = '2026-11-06'
    let moved = false
    const api = apiMock(day({ entries: [entry({ execution_status: 'running' })] }))
    api.getPersonalToday.mockImplementation(async (q: TodayQuery) => {
      if (q.date === targetDate) return day({
        day: { ...day().day, id: targetDayId, revision: 0, date: targetDate, placements: [] },
        window: { start_at: '2026-11-05T16:00:00Z', end_at: '2026-11-06T16:00:00Z' },
        entries: [], routines: [], deliveries: [],
      })
      return moved ? day({ day: { ...day().day, revision: 3, placements: [] }, entries: [] })
        : day({ entries: [entry({ execution_status: 'running' })] })
    })
    api.mutatePersonalToday.mockImplementation(async (operation: string, body: { request_id: string }) => {
      expect(operation).toBe('move')
      expect(body).toMatchObject({ day_id: dayId, expected_revision: 2, placement_id: placementId,
        target_date: targetDate, target_timezone: query.timezone, target_planner_id: 'main', expected_target_revision: 0 })
      moved = true
      return receipt(body.request_id, operation, { identity: {
        day_id: targetDayId, day_revision: 1, source_day_id: dayId, source_day_revision: 3,
        metadata_ids: [metadataId], change_id: `tc_${'5'.repeat(32)}`,
      } })
    })
    renderToday(api)
    const planned = await screen.findByRole('region', { name: '安排' })
    fireEvent.click(within(planned).getByRole('button', { name: '设置' }))
    fireEvent.change(screen.getByLabelText('移到日期'), { target: { value: targetDate } })
    fireEvent.click(screen.getByRole('button', { name: '移动安排' }))
    await waitFor(() => expect(api.mutatePersonalToday).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(localStorage.getItem(todayPendingKey('workspace-A'))).toBeNull())
    await waitFor(() => expect(api.getPersonalToday.mock.calls.filter(([q]) => q.date === query.date).length).toBeGreaterThan(1))
    await waitFor(() => expect(screen.queryByRole('complementary', { name: '编辑安排' })).toBeNull())
    expect(screen.queryByText('原请求待核对')).toBeNull()
    expect(screen.queryByText('原移动回执身份不一致')).toBeNull()
    expect(within(screen.getByRole('region', { name: '安排' })).queryByText('核对项目资料')).toBeNull()
  })
  it('accepts metadata-only rebase without inventing a saved day and refreshes the current source', async () => {
    let rebased = false
    const latestRef = { ...source, item_revision: 2, once_key: '7'.repeat(32), definition_sha256: '8'.repeat(64) }
    const stale = entry({ planned: false, placement_id: null, availability: 'stale', execution_status: null })
    const api = apiMock()
    api.getPersonalToday.mockImplementation(async (q: TodayQuery) => day({
      day: { ...day().day, date: q.date, timezone: q.timezone, planner_id: q.planner_id || 'main', revision: 0, placements: [] },
      entries: [rebased ? entry({ planned: false, placement_id: null, item_ref: latestRef,
        metadata_revision: 2, title: '核对新项目资料', execution_status: null }) : stale],
      routines: [], deliveries: [],
    }))
    api.personalItem.mockResolvedValue({ item_id: itemId, revision: 2, once_key: latestRef.once_key,
      state: 'active', definition_sha256: latestRef.definition_sha256,
      definition: { title: '核对新项目资料', instructions: '采用用户已确认的新版要求', tags: [],
        assignee: { id: 'main', name: '主 Agent', version: 0, kind: 'main' },
        schedule: { mode: 'once', timezone: query.timezone, due_at: '2026-11-05T01:00:00Z' },
        inputs: { artifact_refs: [], directories: [] } },
    })
    api.mutatePersonalToday.mockImplementation(async (operation: string, body: { request_id: string }) => {
      expect(operation).toBe('rebase')
      expect(body).toMatchObject({ day_id: dayId, expected_day_revision: 0, metadata_id: metadataId,
        expected_metadata_revision: 1, old_source_ref: source, new_source_ref: latestRef,
        preserve_deadline: true, preserve_priority: true })
      rebased = true
      return receipt(body.request_id, operation, { identity: {
        metadata_ids: [metadataId], change_id: `tc_${'5'.repeat(32)}`,
      } })
    })
    renderToday(api)
    fireEvent.click(await screen.findByRole('button', { name: '核对版本' }))
    const confirm = await screen.findByRole('button', { name: '确认采用新版本' })
    await waitFor(() => expect(confirm).toBeEnabled())
    fireEvent.click(confirm)
    await waitFor(() => expect(api.mutatePersonalToday).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(localStorage.getItem(todayPendingKey('workspace-A'))).toBeNull())
    await waitFor(() => expect(api.getPersonalToday.mock.calls.length).toBeGreaterThan(1))
    await waitFor(() => expect(screen.queryByRole('complementary', { name: '编辑安排' })).toBeNull())
    expect(await screen.findByRole('button', { name: '核对新项目资料' })).toBeInTheDocument()
    expect(screen.queryByText('原请求待核对')).toBeNull()
    expect(screen.queryByText('原重新绑定回执身份不一致')).toBeNull()
  })
  it('keeps all three source states independent and never turns a delivery read failure into an empty inbox', async () => {
    const api = apiMock(day({ deliveries: [], sources: {
      items: { partial: true, error: 'storage_error' }, routines: { partial: true, next_after: 'routine-cursor' },
      deliveries: { partial: true, error: 'delivery_receipt_unavailable' },
    } }))
    renderToday(api)
    const deliveryState = await screen.findByRole('alert', { name: '交付来源状态' })
    expect(deliveryState).toHaveTextContent('交付来源暂不可用')
    expect(deliveryState).toHaveTextContent('delivery_receipt_unavailable')
    expect(screen.getByRole('alert', { name: '事项来源状态' })).toHaveTextContent('storage_error')
    expect(screen.getByRole('status', { name: '周期计划来源状态' })).toHaveTextContent('周期计划来源尚未读完')
    expect(screen.queryByText('暂无交付')).toBeNull()
    expect(within(screen.getByRole('region', { name: '已交付' })).getByText('交付记录待核对')).toBeInTheDocument()
  })
  it('shows a partial delivery page as incomplete even when item and routine sources finished', async () => {
    renderToday(apiMock(day({ deliveries: [], sources: { items: { partial: false }, routines: { partial: false }, deliveries: { partial: true, next_after: 'delivery-cursor' } } })))
    expect(await screen.findByRole('status', { name: '交付来源状态' })).toHaveTextContent('交付来源尚未读完')
    expect(screen.queryByText('暂无交付')).toBeNull()
    expect(screen.queryByLabelText('事项来源状态')).toBeNull()
    expect(screen.queryByLabelText('周期计划来源状态')).toBeNull()
  })
  it('preserves the old resolved deadline until a human confirms the current timezone rules', async () => {
    const deadline = { kind: 'instant' as const, local_time: '2026-11-05T17:00:00', timezone: 'Asia/Shanghai', fold: 0 as const,
      resolved_at: '2026-11-05T09:00:00Z', timezone_sha256: digest, status: 'timezone_data_changed' as const }
    const api = apiMock(day({ entries: [entry({ execution_status: 'running', deadline })] })); renderToday(api)
    const planned = await screen.findByRole('region', { name: '安排' })
    expect(planned).toHaveTextContent('时区规则待确认')
    expect(screen.getByRole('region', { name: '待处理' })).toHaveTextContent('核对项目资料')
    expect(api.mutatePersonalToday).not.toHaveBeenCalled()
    fireEvent.click(within(planned).getByRole('button', { name: '设置' }))
    expect(screen.getByRole('group', { name: '截止时区规则待确认' })).toHaveTextContent('2026-11-05T09:00:00Z')
    expect(screen.getByLabelText('截止时刻')).toHaveValue('2026-11-05T17:00')
    fireEvent.change(screen.getByLabelText('安排优先级'), { target: { value: 'high' } })
    expect(screen.getByRole('button', { name: '保存设置' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '保存设置' }))
    expect(api.mutatePersonalToday).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('checkbox', { name: '确认采用当前时区规则' }))
    expect(screen.getByRole('button', { name: '保存设置' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: '保存设置' }))
    await waitFor(() => expect(api.mutatePersonalToday).toHaveBeenCalledWith('set-metadata', expect.objectContaining({
      expected_revision: 1, source_ref: source, priority: 'high', deadline: { kind: 'instant', local_time: '2026-11-05T17:00:00', timezone: 'Asia/Shanghai', fold: 0 },
    })))
    expect(api.mutatePersonalToday.mock.calls[0][1].deadline).not.toHaveProperty('resolved_at')
    expect(api.mutatePersonalToday.mock.calls[0][1].deadline).not.toHaveProperty('timezone_sha256')
  })
  it('invalidates timezone confirmation when the deadline changes and retains the draft after a server gap rejection', async () => {
    const deadline = { kind: 'instant' as const, local_time: '2026-03-08T02:30:00', timezone: 'America/New_York', fold: 0 as const,
      resolved_at: '2026-03-08T07:30:00Z', timezone_sha256: digest, status: 'timezone_data_changed' as const }
    const api = apiMock(day({ entries: [entry({ deadline })] }));
    api.mutatePersonalToday.mockRejectedValueOnce(new WorkbenchRequestError('当前时区规则下该时刻不存在', 422))
    renderToday(api)
    const planned = await screen.findByRole('region', { name: '安排' })
    fireEvent.click(within(planned).getByRole('button', { name: '设置' }))
    fireEvent.click(screen.getByRole('checkbox', { name: '确认采用当前时区规则' }))
    fireEvent.click(screen.getByRole('button', { name: '保存设置' }))
    await waitFor(() => expect(api.mutatePersonalToday).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByRole('button', { name: '保存设置' })).toBeEnabled())
    expect(screen.getByLabelText('截止时刻')).toHaveValue('2026-03-08T02:30')
    expect(screen.getByRole('group', { name: '截止时区规则待确认' })).toHaveTextContent('2026-03-08T07:30:00Z')
    fireEvent.change(screen.getByLabelText('截止时刻'), { target: { value: '2026-03-08T03:30' } })
    expect(screen.getByRole('checkbox', { name: '确认采用当前时区规则' })).not.toBeChecked()
    expect(screen.getByRole('button', { name: '保存设置' })).toBeDisabled()
    expect(api.mutatePersonalToday).toHaveBeenCalledTimes(1)
  })
  it('renders three real groups, preserves UNKNOWN after a human check and opens exact original targets', async () => {
    const result = renderToday()
    const planned = await screen.findByRole('region', { name: '安排' })
    expect(screen.getByRole('region', { name: '待处理' })).toHaveTextContent('结果未知')
    expect(screen.getByRole('region', { name: '已交付' })).toHaveTextContent('原始核对报告')
    fireEvent.click(within(planned).getByRole('checkbox', { name: '人工勾选核对项目资料' }))
    await waitFor(() => expect(result.api.mutatePersonalToday).toHaveBeenCalledWith('check', expect.objectContaining({ metadata_id: metadataId, expected_revision: 1, source_ref: source, checked: true })))
    await waitFor(() => expect(screen.getByRole('region', { name: '待处理' })).toHaveTextContent('结果未知'))
    fireEvent.click(within(screen.getByRole('region', { name: '安排' })).getByRole('button', { name: '原事项' }))
    expect(result.onOpenItem).toHaveBeenCalledWith(itemId)
    fireEvent.click(screen.getByRole('button', { name: /原始核对报告/ })); expect(result.onOpenResult).toHaveBeenCalledWith(occurrenceId)
    fireEvent.click(screen.getByRole('button', { name: /每日检查/ })); expect(result.onOpenRoutine).toHaveBeenCalledWith(`pr_${'4'.repeat(32)}`)
    expect(result.api.readPersonalItemResult).not.toHaveBeenCalled()
  })
  it('picks only real sources and includes source and both CAS revisions when placing', async () => {
    const api = apiMock(day({ entries: [], day: { ...day().day, placements: [] } })); renderToday(api)
    await waitFor(() => expect(screen.getByRole('button', { name: '加入安排' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '加入安排' }))
    const dialog = await screen.findByRole('dialog', { name: '选择真实事项' })
    fireEvent.click(await within(dialog).findByRole('button', { name: /核对项目资料/ }))
    await waitFor(() => expect(api.mutatePersonalToday).toHaveBeenCalledWith('place', expect.objectContaining({ source_ref: source, expected_day_revision: 2, expected_metadata_revision: 0, date: query.date, timezone: query.timezone, planner_id: 'main' })))
  })
  it('CAS conflicts keep the editor draft and navigation is guarded until explicitly abandoned', async () => {
    const api = apiMock(); api.mutatePersonalToday.mockRejectedValueOnce(new WorkbenchRequestError('安排已更新', 409, 'version_conflict'))
    const rendered = renderToday(api)
    const planned = await screen.findByRole('region', { name: '安排' })
    fireEvent.click(within(planned).getByRole('button', { name: '设置' }))
    fireEvent.change(screen.getByLabelText('安排优先级'), { target: { value: 'high' } })
    fireEvent.change(screen.getByLabelText('截止类型'), { target: { value: 'date' } })
    fireEvent.change(screen.getByLabelText('截止日期'), { target: { value: '2026-11-09' } })
    fireEvent.click(screen.getByRole('button', { name: '保存设置' }))
    await waitFor(() => expect(api.mutatePersonalToday).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByLabelText('安排优先级')).toHaveValue('high'))
    expect(screen.getByLabelText('截止日期')).toHaveValue('2026-11-09')
    expect(localStorage.getItem(todayPendingKey('workspace-A'))).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '关闭今日安排' }))
    expect(rendered.onClose).not.toHaveBeenCalled()
    expect(screen.getByRole('alertdialog', { name: '保留未保存安排' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '继续编辑' }))
    const navigate = vi.fn(); act(() => requestArtifactNavigation(navigate))
    expect(navigate).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '放弃修改并继续' })); expect(navigate).toHaveBeenCalledOnce()
  })
  it('keyboard ordering uses a full exact placement set and removes only from the day', async () => {
    const other = entry({ id: `tm_${'7'.repeat(32)}`, metadata_id: `tm_${'7'.repeat(32)}`, placement_id: `tp_${'8'.repeat(32)}`, title: '第二事项', item_ref: { ...source, item_id: `pi_${'9'.repeat(32)}` } })
    const api = apiMock(day({ entries: [entry(), other], day: { ...day().day, placements: [...day().day.placements, { id: other.placement_id!, metadata_id: other.metadata_id, source_ref: other.item_ref, disposition: 'include' }] } })); renderToday(api)
    const handle = await screen.findByRole('button', { name: '排序第二事项' })
    fireEvent.keyDown(handle, { key: 'ArrowUp' }); fireEvent.click(screen.getByRole('button', { name: '保存顺序' }))
    await waitFor(() => expect(api.mutatePersonalToday).toHaveBeenCalledWith('reorder', expect.objectContaining({ day_id: dayId, expected_revision: 2, placement_ids: [other.placement_id, placementId] })))
    await waitFor(() => expect(api.getPersonalToday.mock.calls.length).toBeGreaterThan(1))
    const planned = await screen.findByRole('region', { name: '安排' })
    await waitFor(() => expect(within(planned).getAllByRole('button', { name: '移出今日' })[0]).toBeEnabled())
    fireEvent.click(within(planned).getAllByRole('button', { name: '移出今日' })[0])
    await waitFor(() => expect(api.mutatePersonalToday).toHaveBeenCalledWith('remove', expect.objectContaining({ day_id: dayId, expected_revision: 2, placement_id: placementId })))
    expect(api.mutatePersonalToday.mock.calls.every(call => !JSON.stringify(call).includes('cancel'))).toBe(true)
  })
  it('dragging by the handle stages a complete order without writing until confirmed', async () => {
    const other = entry({ id: `tm_${'7'.repeat(32)}`, metadata_id: `tm_${'7'.repeat(32)}`, placement_id: `tp_${'8'.repeat(32)}`, title: '第二事项', execution_status: 'queued', item_ref: { ...source, item_id: `pi_${'9'.repeat(32)}` } })
    const api = apiMock(day({ entries: [entry(), other], day: { ...day().day, placements: [...day().day.placements, { id: other.placement_id!, metadata_id: other.metadata_id, source_ref: other.item_ref, disposition: 'include' }] } })); renderToday(api)
    const handle = await screen.findByRole('button', { name: '排序第二事项' })
    const firstHandle = screen.getByRole('button', { name: '排序核对项目资料' })
    const dataTransfer = { setData: vi.fn(), effectAllowed: '' }
    fireEvent.dragStart(handle, { dataTransfer }); fireEvent.dragOver(firstHandle.closest('article')!); fireEvent.drop(firstHandle.closest('article')!)
    expect(dataTransfer.setData).toHaveBeenCalledWith('text/plain', other.placement_id)
    expect(api.mutatePersonalToday).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '保存顺序' }))
    await waitFor(() => expect(api.mutatePersonalToday).toHaveBeenCalledWith('reorder', expect.objectContaining({ placement_ids: [other.placement_id, placementId] })))
  })
  it('keeps an invalid DST deadline draft and sends an explicitly selected fold without browser UTC conversion', async () => {
    const api = apiMock(); api.mutatePersonalToday.mockRejectedValueOnce(new WorkbenchRequestError('该时刻不存在或需要选择重复时刻', 422))
    renderToday(api)
    const planned = await screen.findByRole('region', { name: '安排' })
    fireEvent.click(within(planned).getByRole('button', { name: '设置' }))
    fireEvent.change(screen.getByLabelText('截止类型'), { target: { value: 'instant' } })
    fireEvent.change(screen.getByLabelText('截止时刻'), { target: { value: '2026-11-01T01:30' } })
    fireEvent.change(screen.getByLabelText('截止时区'), { target: { value: 'America/New_York' } })
    fireEvent.click(screen.getByRole('button', { name: '保存设置' }))
    await waitFor(() => expect(api.mutatePersonalToday).toHaveBeenCalledTimes(1))
    expect(api.mutatePersonalToday.mock.calls[0][1]).toMatchObject({ deadline: { kind: 'instant', local_time: '2026-11-01T01:30:00', timezone: 'America/New_York' } })
    expect(screen.getByLabelText('截止时刻')).toHaveValue('2026-11-01T01:30')
    await waitFor(() => expect(screen.getByRole('button', { name: '保存设置' })).toBeEnabled())
    fireEvent.change(screen.getByLabelText('重复时刻选择'), { target: { value: '1' } })
    fireEvent.click(screen.getByRole('button', { name: '保存设置' }))
    await waitFor(() => expect(api.mutatePersonalToday.mock.calls[1][1]).toMatchObject({ deadline: { local_time: '2026-11-01T01:30:00', fold: 1 } }))
  })
  it('switches date, timezone and planner and clears source text when reauthorization fails', async () => {
    const api = apiMock(), rendered = renderToday(api)
    await screen.findByRole('region', { name: '安排' })
    fireEvent.change(screen.getByLabelText('安排日期'), { target: { value: '2026-11-06' } })
    await waitFor(() => expect(api.getPersonalToday).toHaveBeenLastCalledWith(expect.objectContaining({ date: '2026-11-06' }), expect.any(AbortSignal)))
    fireEvent.change(screen.getByLabelText('安排时区'), { target: { value: 'America/New_York' } }); fireEvent.click(screen.getByRole('button', { name: '切换' }))
    await waitFor(() => expect(api.getPersonalToday).toHaveBeenLastCalledWith(expect.objectContaining({ timezone: 'America/New_York' }), expect.any(AbortSignal)))
    fireEvent.change(screen.getByLabelText('规划 Agent'), { target: { value: context.planners[1].id } })
    await waitFor(() => expect(api.getPersonalToday).toHaveBeenLastCalledWith(expect.objectContaining({ planner_id: context.planners[1].id }), expect.any(AbortSignal)))
    api.getPersonalToday.mockRejectedValueOnce(new WorkbenchRequestError('来源已撤销', 403))
    fireEvent.click(screen.getByRole('button', { name: '刷新今日安排' }))
    await waitFor(() => expect(screen.queryByText('原始核对报告')).toBeNull())
    expect(screen.queryByText('核对项目资料')).toBeNull()
    rendered.unmount()
  })
  it('stale source needs an explicit old/new rebase and never substitutes the latest reference silently', async () => {
    const api = apiMock(day({ entries: [entry({ availability: 'stale', title: 'SECRET_OLD_TITLE' })] }));
    api.personalItem.mockResolvedValue({ item_id: itemId, revision: 2, once_key: '0'.repeat(32), state: 'active', definition_sha256: '1'.repeat(64), definition: { title: 'new title', instructions: 'private', assignee: { id: 'main', version: 0 }, schedule: {}, inputs: { artifact_refs: [] } } })
    renderToday(api)
    const planned = await screen.findByRole('region', { name: '安排' })
    expect(screen.queryByText('SECRET_OLD_TITLE')).toBeNull()
    fireEvent.click(within(planned).getByRole('button', { name: '核对版本' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '确认采用新版本' })).toBeEnabled())
    expect(api.mutatePersonalToday).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '确认采用新版本' }))
    await waitFor(() => expect(api.mutatePersonalToday).toHaveBeenCalledWith('rebase', expect.objectContaining({ old_source_ref: source, new_source_ref: expect.objectContaining({ item_revision: 2, once_key: '0'.repeat(32) }), expected_day_revision: 2, expected_metadata_revision: 1, preserve_deadline: true, preserve_priority: true })))
  })
})

describe('original result deep selection', () => {
  it('loads exact occurrence even outside the first inbox page and marks read only on a human click', async () => {
    const result = { ...delivery(), status: 'completed', artifacts: [], execution_session_id: 'session-original', title: '精确原报告', assignee_name: '主 Agent', created_at: '2026-11-01T01:00:00Z', answer: '原始答案' }
    const api = {
      personalItems: vi.fn().mockResolvedValue({ items: [], next_offset: null }), personalItem: vi.fn(), createPersonalItem: vi.fn(), updatePersonalItem: vi.fn(), controlPersonalItem: vi.fn(), personalItemHistory: vi.fn(), personalItemOccurrences: vi.fn(),
      personalItemResults: vi.fn().mockResolvedValue({ results: [], next_offset: null }), personalItemResult: vi.fn().mockResolvedValue(result), readPersonalItemResult: vi.fn().mockResolvedValue({ ...result, read_receipt: { read_at: '2026-11-05T03:00:00Z' } }), personalArtifacts: vi.fn(), personalArtifact: vi.fn(), runs: vi.fn().mockResolvedValue({ runs: [] }),
    } satisfies PersonalItemsApi
    render(<PersonalItems api={api} namespace="workspace-A" agents={[]} connected initialSelection={{ kind: 'result', id: occurrenceId }} onClose={vi.fn()} onOpenArtifact={vi.fn()} />)
    expect(await screen.findByText('精确原报告')).toBeInTheDocument()
    expect(api.personalItemResult).toHaveBeenCalledWith(occurrenceId, expect.any(AbortSignal))
    expect(api.readPersonalItemResult).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '标记已读' }))
    await waitFor(() => expect(api.readPersonalItemResult).toHaveBeenCalledWith(occurrenceId, expect.objectContaining({ delivery_id: 'delivery-original', expected_delivery_sha256: digest })))
  })
})

describe('Today authenticated client paths', () => {
  it('uses header auth, no-store GET and exact original mutation/request JSON', async () => {
    const calls: { path: string; auth: string | null; body?: unknown }[] = []
    const server = setupServer(http.get('http://today.local/personal/today', ({ request }) => { calls.push({ path: new URL(request.url).pathname + new URL(request.url).search, auth: request.headers.get('authorization') }); return HttpResponse.json(day()) }),
      http.post('http://today.local/personal/today/check', async ({ request }) => { const body = await request.json(); calls.push({ path: new URL(request.url).pathname, auth: request.headers.get('authorization'), body }); return HttpResponse.json(receipt('6'.repeat(32), 'check')) }),
      http.get('http://today.local/personal/today/requests/:requestId', ({ request }) => { calls.push({ path: new URL(request.url).pathname, auth: request.headers.get('authorization') }); return HttpResponse.json({ status: 'unknown', request_id: '6'.repeat(32) }) }))
    server.listen({ onUnhandledRequest: 'error' })
    try {
      const api = new WorkbenchRuntimeClient('http://today.local', async () => 'header-token')
      await api.getPersonalToday(query)
      const body = { request_id: '6'.repeat(32), metadata_id: metadataId, expected_revision: 1, source_ref: source, checked: true }
      await api.mutatePersonalToday('check', body); await api.getPersonalTodayRequest(body.request_id)
      expect(calls[0].path).toContain('date=2026-11-05'); expect(calls[0].path).not.toContain('token')
      expect(calls.every(call => call.auth === 'Bearer header-token')).toBe(true)
      expect(calls[1]).toMatchObject({ path: '/personal/today/check', body })
      expect(calls[2].path).toBe(`/personal/today/requests/${body.request_id}`)
    } finally { server.close() }
  })
})
