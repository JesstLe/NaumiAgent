import { actionErrorMessage } from '../../../web2/src/personal/connections/actionErrors'
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { WorkbenchRuntimeClient, type PersonalAgent } from '@naumi/shared/api/WorkbenchRuntimeClient'
import type { CalendarAction, CalendarEvent, CalendarOAuthIntent, PersonalConnection, PersonalConnectionRequest } from '@naumi/shared/api/personalConnections'
import type { PersonalItem } from '@naumi/shared/api/personalItems'
import { ConnectionWorkspace, googleAuthorizationUrl } from '../../../web2/src/personal/connections/ConnectionWorkspace'
import { connectionRecoveryKey, usePersonalConnections, type PersonalConnectionsApi } from '../../../web2/src/personal/connections/usePersonalConnections'
import { calendarInstant, calendarWallTime } from '../../../web2/src/personal/connections/calendarTime'
import { ActionSources } from '../../../web2/src/personal/connections/ActionSources'

const connectionId = `pc_${'1'.repeat(32)}`, actionId = `ca_${'2'.repeat(32)}`, actorId = `pa_${'3'.repeat(32)}`, sha = '4'.repeat(64)
const context = { owner_key: 'a'.repeat(64), workspace_key: 'b'.repeat(64), provider: 'google_calendar' as const, client_configured: true }
const agent: PersonalAgent = { id: actorId, name: 'Mira', description: '', instructions: '核对日程', version: 3, kind: 'specialist', status: 'active', model_tier: 'capable', tool_names: [], member_ids: [], created_at: '', updated_at: '' }
const connection = (patch: Partial<PersonalConnection> = {}): PersonalConnection => ({ id: connectionId, revision: 1, auth_epoch: 2, provider: 'google_calendar', email: 'calendar@example.test', email_verified: true, can_write: false, scopes: ['https://www.googleapis.com/auth/calendar.events.owned.readonly'], status: 'connected', sync_status: 'ready', last_sync_at: '2026-11-05T08:00:00Z', last_error_code: null, revoke_status: null, updated_at: '2026-11-05T08:00:00Z', grants: [], ...patch })
const event = (patch: Partial<CalendarEvent> = {}): CalendarEvent => ({ connection_id: connectionId, event_id: 'event/exact?occurrence=1', occurrence_key: 'occurrence-original', revision: 4, etag: '"etag-original"', status: 'confirmed', summary: '季度项目评审', description: '核对议程与原始材料', start: { dateTime: '2099-11-06T01:00:00Z', timeZone: 'Asia/Shanghai' }, end: { dateTime: '2099-11-06T02:00:00Z', timeZone: 'Asia/Shanghai' }, html_link: 'https://calendar.google.com/calendar/event?eid=original', recurring_event_id: 'series-original', original_start_time: { dateTime: '2099-11-06T01:00:00Z' }, fetched_at: '2026-11-05T08:00:00Z', source_ref: { connection_id: connectionId, auth_epoch: 2, event_id: 'event/exact?occurrence=1', revision: 4, payload_sha256: sha, occurrence_key: 'occurrence-original', grant_id: 'cgrant_original', grant_revision: 7 }, ...patch })
const action = (patch: Partial<CalendarAction> = {}): CalendarAction => ({ id: actionId, revision: 1, status: 'draft', digest: sha, connection_id: connectionId, auth_epoch: 2, actor_id: 'main', account_email: 'calendar@example.test', operation: 'insert', event_id: null, base_etag: null, body: { summary: '团队方案讨论', description: '确认待写入的会议内容', start: { dateTime: '2099-11-06T03:00:00Z', timeZone: 'Asia/Shanghai' }, end: { dateTime: '2099-11-06T04:00:00Z', timeZone: 'Asia/Shanghai' } }, created_at: '2026-11-05T08:00:00Z', updated_at: '2026-11-05T08:00:00Z', expires_at: '2099-11-06T02:00:00Z', source_refs: [], artifact_refs: [], memory_refs: [], memory_item_id: null, approval: null, result: null, job_id: null, run_id: null, ...patch })
const oauth = (patch: Partial<CalendarOAuthIntent> = {}): CalendarOAuthIntent => ({ intent_id: 'oauth_original', status: 'pending', authorization_url: 'https://accounts.google.com/o/oauth2/v2/auth?state=opaque', expires_at: '2099-11-05T09:00:00Z', ...patch })
const createdItem = { item_id: `pi_${'8'.repeat(32)}`, revision: 1, state: 'active' } as PersonalItem
function apiMock(account = connection(), draft = action()) {
  return {
    personalConnectionContext: vi.fn().mockResolvedValue(context), personalConnections: vi.fn().mockResolvedValue({ connections: [account] }),
    personalConnectionRequest: vi.fn().mockImplementation(async request_id => ({ request_id, operation: 'create_action', status: 'unknown', result_kind: null, result_id: null, fingerprint: null } satisfies PersonalConnectionRequest)),
    configurePersonalCalendarClient: vi.fn().mockResolvedValue(context), beginPersonalCalendarOAuth: vi.fn().mockResolvedValue(oauth()), personalCalendarOAuth: vi.fn().mockResolvedValue(oauth()), cancelPersonalCalendarOAuth: vi.fn().mockResolvedValue(oauth({ status: 'cancelled' })),
    syncPersonalCalendar: vi.fn().mockResolvedValue(account), disconnectPersonalCalendar: vi.fn().mockResolvedValue(connection({ status: 'disconnected', revision: 2 })),
    personalCalendarEvents: vi.fn().mockResolvedValue({ events: [event()], limit: 30, offset: 0, next_offset: null }), createPersonalCalendarItem: vi.fn().mockResolvedValue(createdItem),
    personalItem: vi.fn().mockResolvedValue(createdItem),
    grantPersonalCalendar: vi.fn().mockResolvedValue(account), revokePersonalCalendarGrant: vi.fn().mockResolvedValue(account),
    personalCalendarActions: vi.fn().mockResolvedValue({ actions: [draft] }), personalCalendarAction: vi.fn().mockResolvedValue(draft),
    createPersonalCalendarAction: vi.fn().mockResolvedValue(draft), revisePersonalCalendarAction: vi.fn().mockResolvedValue(draft),
    approvePersonalCalendarAction: vi.fn().mockResolvedValue(action({ status: 'approved', revision: 2, approval: { revision: 1, digest: sha, expires_at: draft.expires_at, receipt_id: 'approval_exact' } })),
    rejectPersonalCalendarAction: vi.fn().mockResolvedValue(action({ status: 'rejected', revision: 2 })), reconcilePersonalCalendarAction: vi.fn().mockResolvedValue(draft),
  } satisfies PersonalConnectionsApi
}
function mount(api = apiMock(), connected = true) { return { api, ...render(<ConnectionWorkspace api={api} namespace="calendar-A" agents={[agent, { ...agent, id: `pa_${'9'.repeat(32)}`, name: '团队', kind: 'team' }]} connected={connected} onClose={vi.fn()} onOpenItems={vi.fn()} />) } }
async function selectAction() { fireEvent.click(screen.getByRole('tab', { name: /动作审批/ })); fireEvent.click(await screen.findByRole('button', { name: /团队方案讨论/ })); await screen.findByRole('button', { name: '批准并执行' }) }
beforeEach(() => { localStorage.clear(); sessionStorage.clear(); history.replaceState({}, '', '/web2?connections=1') })
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('calendar request identity recovery', () => {
  it('retains opaque IDs only after response loss and never treats an empty directory as rejection', async () => {
    const api = apiMock(); api.createPersonalCalendarAction.mockRejectedValue(new Error('503 UNKNOWN'))
    const first = renderHook(() => usePersonalConnections({ api, namespace: 'calendar-A', connected: true }))
    await waitFor(() => expect(first.result.current.context).not.toBeNull())
    await act(async () => { await first.result.current.perform('create_action', connectionId, request_id => api.createPersonalCalendarAction({ request_id, connection_id: connectionId, actor_id: 'main', source_refs: [], operation: 'insert', body: action().body, expires_at: action().expires_at })) })
    const raw = localStorage.getItem(connectionRecoveryKey('calendar-A'))!, record = JSON.parse(raw)
    expect(record.request_id).toMatch(/^[a-f0-9]{32}$/)
    expect(Object.keys(record).sort()).toEqual(['operation', 'owner_key', 'request_id', 'resource_id', 'workspace_key'])
    expect(raw).not.toContain(action().body.summary); expect(raw).not.toContain(action().body.description); expect(raw).not.toContain('client_secret')
    first.unmount(); api.personalCalendarActions.mockResolvedValue({ actions: [] })
    const reopened = renderHook(() => usePersonalConnections({ api, namespace: 'calendar-A', connected: true }))
    await waitFor(() => expect(reopened.result.current.context).not.toBeNull())
    await act(async () => { await reopened.result.current.checkPending(); await reopened.result.current.perform('create_action', connectionId, api.createPersonalCalendarAction) })
    expect(reopened.result.current.pending?.request_id).toBe(record.request_id)
    expect(api.personalConnectionRequest).toHaveBeenCalledWith(record.request_id)
    expect(api.createPersonalCalendarAction).toHaveBeenCalledTimes(1)
    api.personalConnectionRequest.mockResolvedValue({ request_id: record.request_id, operation: 'create_action', status: 'applied', result_kind: 'action', result_id: actionId, result: action(), fingerprint: sha })
    await act(async () => { await reopened.result.current.checkPending() })
    expect(reopened.result.current.pending).toBeNull(); expect(localStorage.getItem(connectionRecoveryKey('calendar-A'))).toBeNull()
  })
  it('does not transport when durable identity storage fails', async () => {
    const api = apiMock(), hook = renderHook(() => usePersonalConnections({ api, namespace: 'calendar-A', connected: true }))
    await waitFor(() => expect(hook.result.current.context).not.toBeNull())
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('storage full') })
    await act(async () => { await hook.result.current.perform('sync', connectionId, id => api.syncPersonalCalendar(connectionId, { request_id: id, expected_revision: 1 })) })
    expect(api.syncPersonalCalendar).not.toHaveBeenCalled()
  })
  it('blocks a second fresh POST even before React has committed the first failed write state', async () => {
    const api = apiMock(), hook = renderHook(() => usePersonalConnections({ api, namespace: 'calendar-A', connected: true }))
    api.syncPersonalCalendar.mockRejectedValue(new Error('response lost'))
    await waitFor(() => expect(hook.result.current.context).not.toBeNull())
    const submit = hook.result.current.perform
    await act(async () => {
      await submit('sync', connectionId, request_id => api.syncPersonalCalendar(connectionId, { request_id, expected_revision: 1 }))
      await submit('sync', connectionId, request_id => api.syncPersonalCalendar(connectionId, { request_id, expected_revision: 1 }))
    })
    expect(api.syncPersonalCalendar).toHaveBeenCalledTimes(1)
  })
  it('preserves corrupted recovery records and blocks all fresh mutations', async () => {
    localStorage.setItem(connectionRecoveryKey('calendar-A'), '{broken')
    const api = apiMock(), hook = renderHook(() => usePersonalConnections({ api, namespace: 'calendar-A', connected: true }))
    await waitFor(() => expect(hook.result.current.context).not.toBeNull())
    await act(async () => { await hook.result.current.perform('sync', connectionId, api.syncPersonalCalendar) })
    expect(api.syncPersonalCalendar).not.toHaveBeenCalled(); expect(hook.result.current.identityError).toBeTruthy()
    expect(localStorage.getItem(connectionRecoveryKey('calendar-A'))).toBe('{broken')
  })
  it('keeps pending OAuth identity without persisting authorization URL and accepts only connected with connection_id', async () => {
    const api = apiMock(), hook = renderHook(() => usePersonalConnections({ api, namespace: 'calendar-A', connected: true }))
    await waitFor(() => expect(hook.result.current.context).not.toBeNull())
    await act(async () => { await hook.result.current.perform('oauth', undefined, request_id => api.beginPersonalCalendarOAuth({ request_id, write: false })) })
    expect(hook.result.current.pending?.intent_id).toBe('oauth_original')
    expect(localStorage.getItem(connectionRecoveryKey('calendar-A'))).not.toContain('accounts.google.com')
    api.personalCalendarOAuth.mockResolvedValue(oauth({ status: 'exchange_unknown' }))
    await act(async () => { await hook.result.current.checkPending() })
    expect(hook.result.current.pending).not.toBeNull()
    api.personalCalendarOAuth.mockResolvedValue(oauth({ status: 'connected' }))
    await act(async () => { await hook.result.current.checkPending() })
    expect(hook.result.current.pending).not.toBeNull()
    api.personalCalendarOAuth.mockResolvedValue(oauth({ status: 'connected', connection_id: connectionId }))
    await act(async () => { await hook.result.current.checkPending() })
    expect(hook.result.current.pending).toBeNull(); expect(api.beginPersonalCalendarOAuth).toHaveBeenCalledTimes(1)
  })
  it('persists original OAuth cancellation request before transport and never issues a new cancellation after response loss', async () => {
    const api = apiMock(), hook = renderHook(() => usePersonalConnections({ api, namespace: 'calendar-A', connected: true }))
    await waitFor(() => expect(hook.result.current.context).not.toBeNull())
    await act(async () => { await hook.result.current.perform('oauth', undefined, request_id => api.beginPersonalCalendarOAuth({ request_id, write: false })) })
    api.cancelPersonalCalendarOAuth.mockImplementation(async (_id, body) => { expect(JSON.parse(localStorage.getItem(connectionRecoveryKey('calendar-A'))!).request_id).toBe(body.request_id); throw new Error('response lost') })
    await act(async () => { await hook.result.current.cancelOAuth() })
    const requestId = hook.result.current.pending?.request_id
    await act(async () => { await hook.result.current.cancelOAuth(); await hook.result.current.checkPending() })
    expect(api.cancelPersonalCalendarOAuth).toHaveBeenCalledTimes(1); expect(hook.result.current.pending?.request_id).toBe(requestId)
  })
})

describe('remote revocation state feedback', () => {
  it.each([
    ['confirmed', '已撤销'],
    ['error', '撤销失败'],
    ['unknown', '结果待核对'],
    ['not_requested', '未请求'],
    ['revoked', '已撤销'],
    ['failed', '撤销失败'],
    ['requested', '已请求'],
    ['none', '未请求'],
    ['future_revoke_state', '待核对'],
  ])('renders %s as %s without issuing calendar writes', async (revoke_status, label) => {
    const api = apiMock(connection({ status: 'disconnected', revoke_status }))
    mount(api)
    expect(await screen.findByText(`远端撤销：${label}`)).toBeInTheDocument()
    for (const mutation of ['disconnectPersonalCalendar', 'beginPersonalCalendarOAuth', 'syncPersonalCalendar',
      'grantPersonalCalendar', 'revokePersonalCalendarGrant', 'createPersonalCalendarAction',
      'approvePersonalCalendarAction', 'reconcilePersonalCalendarAction'] as const)
      expect(api[mutation]).not.toHaveBeenCalled()
  })
  it.each([null, undefined, ''])('does not invent remote revocation feedback for an absent status (%s)', async revoke_status => {
    const api = apiMock(connection({ status: 'disconnected', revoke_status }))
    mount(api)
    await screen.findByRole('button', { name: /calendar@example\.test/ })
    expect(screen.queryByText(/远端撤销：/)).not.toBeInTheDocument()
    expect(api.disconnectPersonalCalendar).not.toHaveBeenCalled()
    expect(api.beginPersonalCalendarOAuth).not.toHaveBeenCalled()
  })
})

describe('operative light calendar workspace', () => {
  it('starts a new read-only account connection without rebinding the selected existing account', async () => {
    const { api } = mount(); const button = await screen.findByRole('button', { name: '连接 Google 日历' })
    await waitFor(() => expect(button).not.toBeDisabled()); fireEvent.click(button)
    await waitFor(() => expect(api.beginPersonalCalendarOAuth).toHaveBeenCalledTimes(1))
    expect(api.beginPersonalCalendarOAuth.mock.calls[0][0]).toEqual({ request_id: expect.stringMatching(/^[a-f0-9]{32}$/), write: false })
  })
  it('shows primary, actual scopes and an independent explicit write upgrade', async () => {
    const { api } = mount(); await screen.findByRole('button', { name: '启用日历写入' })
    expect(screen.getByText('primary')).toBeInTheDocument()
    expect(screen.getByText(connection().scopes[0])).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '启用日历写入' }))
    expect(api.beginPersonalCalendarOAuth).not.toHaveBeenCalled()
    fireEvent.click(within(screen.getByRole('dialog', { name: '启用日历写入？' })).getByRole('button', { name: '前往 Google 授权' }))
    await waitFor(() => expect(api.beginPersonalCalendarOAuth).toHaveBeenCalledWith(expect.objectContaining({ connection_id: connectionId, expected_revision: 1, write: true })))
    expect(api.approvePersonalCalendarAction).not.toHaveBeenCalled()
    expect(await screen.findByRole('link', { name: /前往 Google 授权/ })).toHaveAttribute('href', oauth().authorization_url)
  })
  it('reads client JSON once without storing its secret in browser storage', async () => {
    const api = apiMock(); api.personalConnectionContext.mockResolvedValue({ ...context, client_configured: false }); mount(api)
    await screen.findByRole('button', { name: '选择客户端 JSON' })
    const secret = 'PRIVATE_CLIENT_SECRET_DO_NOT_PERSIST', client = JSON.stringify({ installed: { client_id: 'desktop-client', client_secret: secret } })
    fireEvent.change(screen.getByLabelText('Google 客户端 JSON'), { target: { files: [new File([client], 'client.json', { type: 'application/json' })] } })
    await waitFor(() => expect(api.configurePersonalCalendarClient).toHaveBeenCalledWith(expect.objectContaining({ client_json: client })))
    expect(JSON.stringify(localStorage)).not.toContain(secret); expect(JSON.stringify(sessionStorage)).not.toContain(secret)
  })
  it('renders event text literally and rejects executable external URLs', async () => {
    const api = apiMock(); api.personalCalendarEvents.mockResolvedValue({ events: [event({ description: '<img src=x onerror="window.bad=1">', html_link: 'javascript:alert(1)' })], limit: 30, offset: 0, next_offset: null }); mount(api)
    fireEvent.click(await screen.findByRole('button', { name: /季度项目评审/ }))
    expect(screen.getByText('<img src=x onerror="window.bad=1">')).toBeInTheDocument()
    expect(document.querySelector('img')).toBeNull(); expect(screen.queryByRole('link', { name: '在 Google 查看' })).not.toBeInTheDocument()
    expect(googleAuthorizationUrl('https://accounts.google.com.evil.test/oauth')).toBeNull()
    expect(googleAuthorizationUrl('javascript:alert(1)')).toBeNull()
  })
  it('converts the exact event revision into an existing item with the chosen actor', async () => {
    const { api } = mount(); fireEvent.click(await screen.findByRole('button', { name: /季度项目评审/ })); fireEvent.click(screen.getByRole('button', { name: '转为事项' }))
    fireEvent.change(screen.getByLabelText('日历事项执行要求'), { target: { value: '按议程核对我的资料' } }); fireEvent.change(screen.getByLabelText('日历事项 Agent'), { target: { value: actorId } })
    fireEvent.click(screen.getByRole('button', { name: '创建事项' }))
    await waitFor(() => expect(api.createPersonalCalendarItem).toHaveBeenCalledWith(connectionId, event().event_id, expect.objectContaining({ event_revision: 4, actor_id: actorId, instructions: '按议程核对我的资料', schedule: expect.objectContaining({ mode: 'now', misfire_policy: 'run_once', grace_seconds: 3600 }) })))
    expect(await screen.findByRole('button', { name: /查看事项/ })).toBeInTheDocument()
  })
  it('shares only the exact active specialist version, not a team or an unversioned actor', async () => {
    const { api } = mount(); fireEvent.click(await screen.findByRole('button', { name: '只读分享' }))
    const select = screen.getByLabelText('日历分享 Agent 版本')
    expect(within(select).getByRole('option', { name: 'Mira · v3' })).toBeInTheDocument(); expect(within(select).queryByText('团队')).not.toBeInTheDocument()
    fireEvent.change(select, { target: { value: actorId } }); fireEvent.click(screen.getByRole('button', { name: '分享给此版本' }))
    await waitFor(() => expect(api.grantPersonalCalendar).toHaveBeenCalledWith(connectionId, expect.objectContaining({ actor_id: actorId, actor_version: 3, expected_revision: 1 })))
  })
  it.each([false, true])('keeps local disconnect and remote revocation distinct (remote=%s)', async remote => {
    const { api } = mount(); fireEvent.click(await screen.findByRole('button', { name: '断开连接' }))
    if (remote) fireEvent.click(screen.getByRole('checkbox', { name: '同时请求 Google 撤销授权' }))
    fireEvent.click(screen.getByRole('button', { name: remote ? '断开并请求撤销' : '仅断开本地连接' }))
    await waitFor(() => expect(api.disconnectPersonalCalendar).toHaveBeenCalledWith(connectionId, expect.objectContaining({ expected_revision: 1, remote_revoke: remote })))
  })
  it('approves once with the exact reviewed digest and never calls another provider write', async () => {
    const { api } = mount(apiMock(connection({ can_write: true }))); await selectAction()
    fireEvent.click(screen.getByRole('button', { name: '批准并执行' })); const confirm = screen.getByRole('button', { name: '确认批准并执行' })
    fireEvent.click(confirm); fireEvent.click(confirm)
    await waitFor(() => expect(api.approvePersonalCalendarAction).toHaveBeenCalledTimes(1))
    expect(api.approvePersonalCalendarAction).toHaveBeenCalledWith(actionId, expect.objectContaining({ expected_revision: 1, expected_digest: sha }))
    expect(api.createPersonalCalendarAction).not.toHaveBeenCalled(); expect(api.reconcilePersonalCalendarAction).not.toHaveBeenCalled()
  })
  it('invalidates the approval confirmation when the reviewed revision or digest changes', async () => {
    const api = apiMock(connection({ can_write: true })); mount(api); await selectAction(); fireEvent.click(screen.getByRole('button', { name: '批准并执行' }))
    const changed = action({ revision: 2, digest: 'f'.repeat(64) })
    api.personalCalendarActions.mockResolvedValue({ actions: [changed] }); api.personalCalendarAction.mockResolvedValue(changed)
    fireEvent.click(screen.getByRole('button', { name: '刷新连接' }))
    await screen.findByText('动作已更新，请关闭后重新审阅')
    expect(screen.getByRole('button', { name: '确认批准并执行' })).toBeDisabled(); expect(api.approvePersonalCalendarAction).not.toHaveBeenCalled()
  })
  it('keeps edits through blocked navigation and exposes a real discard decision', async () => {
    mount(apiMock(connection({ can_write: true }))); fireEvent.click(await screen.findByRole('button', { name: '新建日程草稿' }))
    fireEvent.change(screen.getByLabelText('日程名称'), { target: { value: '尚未保存的会议' } }); fireEvent.click(screen.getByRole('tab', { name: /动作审批/ }))
    expect(screen.getByRole('dialog', { name: '放弃未保存修改？' })).toBeInTheDocument(); fireEvent.click(screen.getByRole('button', { name: '继续编辑' }))
    expect(screen.getByLabelText('日程名称')).toHaveValue('尚未保存的会议')
    fireEvent.click(screen.getByRole('tab', { name: /动作审批/ })); fireEvent.click(screen.getByRole('button', { name: '放弃修改' }))
    expect(screen.queryByLabelText('日程名称')).not.toBeInTheDocument()
  })
  it('preserves the complete event source ref including the original grant version in update proposals', async () => {
    const { api } = mount(apiMock(connection({ can_write: true }))); fireEvent.click(await screen.findByRole('button', { name: /季度项目评审/ })); fireEvent.click(screen.getByRole('button', { name: '拟定日程修改' }))
    fireEvent.click(screen.getByRole('button', { name: '保存草稿' }))
    await waitFor(() => expect(api.createPersonalCalendarAction).toHaveBeenCalledWith(expect.objectContaining({ operation: 'update', target_event_id: event().event_id, base_etag: event().etag, source_refs: [event().source_ref] })))
  })
  it('reconciles an unknown original action without creating or re-approving an action', async () => {
    const api = apiMock(connection({ can_write: true }), action({ status: 'unknown' })); mount(api)
    fireEvent.click(screen.getByRole('tab', { name: /动作审批/ })); fireEvent.click(await screen.findByRole('button', { name: /团队方案讨论/ })); fireEvent.click(screen.getByRole('button', { name: '核对原动作' }))
    await waitFor(() => expect(api.reconcilePersonalCalendarAction).toHaveBeenCalledWith(actionId, expect.objectContaining({ expected_revision: 1 })))
    expect(api.createPersonalCalendarAction).not.toHaveBeenCalled(); expect(api.approvePersonalCalendarAction).not.toHaveBeenCalled()
  })
  it('retains opaque recovery IDs across a disconnected render and disables mutations', async () => {
    localStorage.setItem(connectionRecoveryKey('calendar-A'), JSON.stringify({ request_id: 'request_original', operation: 'sync', owner_key: context.owner_key, workspace_key: context.workspace_key, resource_id: connectionId }))
    const { api } = mount(apiMock(), false)
    expect(screen.getByText('request_original')).toBeInTheDocument(); expect(screen.getByRole('button', { name: '核对原请求' })).toBeDisabled()
    expect(api.personalConnections).not.toHaveBeenCalled(); expect(api.syncPersonalCalendar).not.toHaveBeenCalled()
  })
})

describe('calendar timezone semantics', () => {
  it('rejects DST gaps and maps repeated local times to two distinct explicit instants', () => {
    expect(() => calendarInstant('2026-03-08T02:30', 'America/New_York')).toThrow('夏令时')
    const first = calendarInstant('2026-11-01T01:30', 'America/New_York', 0), second = calendarInstant('2026-11-01T01:30', 'America/New_York', 1)
    expect(Date.parse(second) - Date.parse(first)).toBe(3600000); expect(calendarWallTime(first, 'America/New_York')).toBe('2026-11-01T01:30')
    expect(calendarInstant('2026-11-05T09:00', 'Asia/Shanghai')).toBe('2026-11-05T01:00:00.000Z')
  })
})

describe('read-only exact action sources', () => {
  it('links exact artifact version and original memory reference under the action actor without replacing grants', () => {
    const artifact = { artifact_id: `art_${'5'.repeat(32)}`, version: 2, sha256: sha, grant_id: 'artifact_grant_original', grant_revision: 4 }
    const memory = { memory_id: `pm_${'6'.repeat(32)}`, revision: 3, content_sha256: sha, grant_id: 'memory_grant_original', grant_revision: 7 }
    const itemId = `pi_${'7'.repeat(32)}`, openArtifact = vi.fn(), openMemory = vi.fn()
    render(<ActionSources action={action({ actor_id: actorId, artifact_refs: [artifact], memory_refs: [memory], memory_item_id: itemId })} onOpenArtifact={openArtifact} onOpenMemory={openMemory} />)
    const artifactLink = screen.getByRole('link', { name: `查看来源资料 ${artifact.artifact_id} 版本 2` })
    const memoryLink = screen.getByRole('link', { name: `查看记忆记录 ${memory.memory_id} 原引用修订 3` })
    const artifactUrl = new URL(artifactLink.getAttribute('href')!, location.origin), memoryUrl = new URL(memoryLink.getAttribute('href')!, location.origin)
    expect(artifactUrl.searchParams.get('actor')).toBe(actorId); expect(artifactUrl.searchParams.get('version')).toBe('2')
    expect(memoryUrl.searchParams.get('memory_actor')).toBe(actorId); expect(memoryUrl.searchParams.get('memory_id')).toBe(memory.memory_id); expect(memoryUrl.searchParams.get('memory_item')).toBe(itemId)
    expect(screen.getByText('引用修订 3')).toBeInTheDocument(); expect(memoryLink).toHaveAttribute('title', expect.stringContaining('本动作仍引用修订 3'))
    fireEvent.click(artifactLink); fireEvent.click(memoryLink)
    expect(openArtifact).toHaveBeenCalledWith(actorId, artifact); expect(openMemory).toHaveBeenCalledWith(actorId, memory, itemId)
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument(); expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
  })
  it('keeps empty internal source collections invisible rather than adding helper text', () => {
    render(<ActionSources action={action()} />)
    expect(screen.queryByRole('region', { name: '动作原始来源' })).not.toBeInTheDocument()
  })
})

describe('typed calendar transport', () => {
  it('uses authenticated runtime endpoints, encoded event IDs, exact bodies and GET-only original request lookup', async () => {
    const requests: { path: string; method: string; body: unknown; authorization: string | null }[] = []
    const server = setupServer(http.all('http://calendar.local/*', async ({ request }) => { const url = new URL(request.url); requests.push({ path: url.pathname + url.search, method: request.method, body: request.method === 'GET' ? null : await request.json(), authorization: request.headers.get('authorization') }); return HttpResponse.json({}) }))
    server.listen({ onUnhandledRequest: 'error' })
    try {
      const api = new WorkbenchRuntimeClient('http://calendar.local', async () => 'local-runtime-token')
      await api.personalConnectionRequest('request/original'); await api.createPersonalCalendarItem(connectionId, 'event/with?query', { request_id: 'original', event_revision: 4, actor_id: 'main', title: '核对日程', instructions: '核对材料', schedule: { mode: 'now', timezone: 'Asia/Shanghai', misfire_policy: 'run_once', grace_seconds: 3600 } })
      await api.approvePersonalCalendarAction(actionId, { request_id: 'approval_original', expected_revision: 3, expected_digest: sha })
      await api.beginPersonalCalendarOAuth({ request_id: 'oauth_original', connection_id: connectionId, expected_revision: 1, write: true })
      expect(requests[0]).toMatchObject({ path: '/personal/connections/requests/request%2Foriginal', method: 'GET', body: null })
      expect(requests[1].path).toContain('/events/event%2Fwith%3Fquery/items'); expect(requests[1].body).toMatchObject({ request_id: 'original', event_revision: 4 })
      expect(requests[2]).toMatchObject({ path: `/personal/actions/${actionId}/approve`, method: 'POST', body: { request_id: 'approval_original', expected_revision: 3, expected_digest: sha } })
      expect(requests.every(request => request.authorization === 'Bearer local-runtime-token')).toBe(true)
      expect(requests.some(request => request.path.includes('googleapis.com'))).toBe(false)
    } finally { server.close() }
  })
})


describe('calendar action evidence errors', () => {
  it.each([
    ['not_found', '结果未知，请按原动作编号对账'],
    ['job_terminal_unknown', '原受理或执行证据待恢复，请勿另发写入请求'],
    ['memory_access_revoked', '原始来源或授权已变化，请重新核对'],
    ['preflight_unsupported_target', '原日程包含本次未批准的参与者或扩展字段，未修改日程'],
    ['preflight_etag_conflict', '原日程已更新，未修改日程，请读取新版本后提出草稿'],
    ['preflight_read_failed', '原日程预检未完成，本次未进入修改，请核对原行动'],
    ['constructor', '行动证据待核对'],
  ])('maps %s without exposing raw codes or claiming an unaccepted write', (code, message) => {
    expect(actionErrorMessage(code)).toBe(message)
  })
  it('shows original unknown-action evidence and keeps only original reconciliation available', async () => {
    const api = apiMock(connection({ can_write: true }), action({ status: 'unknown', last_error_code: 'not_found' })); mount(api)
    fireEvent.click(screen.getByRole('tab', { name: /动作审批/ })); fireEvent.click(await screen.findByRole('button', { name: /团队方案讨论/ }))
    expect(await screen.findByText('结果未知，请按原动作编号对账')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '核对原动作' })).toBeEnabled()
    expect(screen.queryByRole('button', { name: '批准并执行' })).not.toBeInTheDocument()
    expect(screen.queryByText('not_found')).not.toBeInTheDocument()
    expect(api.createPersonalCalendarAction).not.toHaveBeenCalled(); expect(api.approvePersonalCalendarAction).not.toHaveBeenCalled()
  })
})


describe('read-only calendar drafting and explicit write upgrade', () => {
  const noAutomaticExecution = (api: ReturnType<typeof apiMock>) => {
    expect(api.beginPersonalCalendarOAuth).not.toHaveBeenCalled()
    expect(api.approvePersonalCalendarAction).not.toHaveBeenCalled()
    expect(api.reconcilePersonalCalendarAction).not.toHaveBeenCalled()
  }
  it.each([
    ['ready', '已同步'], ['not_synced', '尚未同步'], ['syncing', '正在同步'],
    ['error', '同步失败'], ['unknown', '结果待核对'], ['future_status', '待核对'],
  ])('shows real sync status %s without confusing success with unknown', async (sync_status, label) => {
    mount(apiMock(connection({ sync_status })))
    const heading = await screen.findByText('同步状态')
    expect(heading.nextElementSibling).toHaveTextContent(label)
  })
  it('creates a draft on a connected read-only account without an automatic upgrade or approval', async () => {
    const { api } = mount()
    const create = await screen.findByRole('button', { name: '新建日程草稿' })
    await waitFor(() => expect(create).toBeEnabled()); fireEvent.click(create)
    expect(screen.getByLabelText('日程名称')).toBeEnabled()
    fireEvent.change(screen.getByLabelText('日程名称'), { target: { value: '只读账户拟定会议' } })
    fireEvent.click(screen.getByRole('button', { name: '保存草稿' }))
    await waitFor(() => expect(api.createPersonalCalendarAction).toHaveBeenCalledTimes(1))
    expect(api.createPersonalCalendarAction).toHaveBeenCalledWith(expect.objectContaining({ connection_id: connectionId, actor_id: 'main', operation: 'insert', source_refs: [], body: expect.objectContaining({ summary: '只读账户拟定会议' }) }))
    noAutomaticExecution(api)
  })
  it('revises an existing draft on a read-only account while approval stays unavailable', async () => {
    const { api } = mount(); await selectAction()
    expect(screen.getByRole('button', { name: '批准并执行' })).toBeDisabled()
    expect(screen.getByText('批准前需启用日历写入')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '修改草稿' }))
    expect(screen.getByLabelText('日程名称')).toBeEnabled()
    fireEvent.change(screen.getByLabelText('日程名称'), { target: { value: '只读修订后的会议' } })
    fireEvent.click(screen.getByRole('button', { name: '保存草稿' }))
    await waitFor(() => expect(api.revisePersonalCalendarAction).toHaveBeenCalledTimes(1))
    expect(api.revisePersonalCalendarAction).toHaveBeenCalledWith(actionId, expect.objectContaining({ expected_revision: 1, body: expect.objectContaining({ summary: '只读修订后的会议' }) }))
    expect(api.createPersonalCalendarAction).not.toHaveBeenCalled(); noAutomaticExecution(api)
  })
  it('proposes a read-only event update with its exact original source and ETag, without automatic writes', async () => {
    const { api } = mount(); fireEvent.click(await screen.findByRole('button', { name: /季度项目评审/ }))
    expect(screen.getByRole('button', { name: '拟定日程修改' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: '拟定日程修改' }))
    fireEvent.change(screen.getByLabelText('日程名称'), { target: { value: '只读拟定的日程修改' } })
    fireEvent.click(screen.getByRole('button', { name: '保存草稿' }))
    await waitFor(() => expect(api.createPersonalCalendarAction).toHaveBeenCalledTimes(1))
    expect(api.createPersonalCalendarAction).toHaveBeenCalledWith(expect.objectContaining({ operation: 'update', target_event_id: event().event_id, base_etag: event().etag, source_refs: [event().source_ref] }))
    noAutomaticExecution(api)
  })
  it('upgrades the exact reviewed draft account only after confirmation and still requires a separate approval', async () => {
    const other = connection({ id: `pc_${'9'.repeat(32)}`, email: 'other@example.test', revision: 5 })
    const api = apiMock(); api.personalConnections.mockResolvedValue({ connections: [other, connection()] }); mount(api)
    await selectAction()
    fireEvent.click(screen.getByRole('button', { name: '启用写入权限' }))
    expect(api.beginPersonalCalendarOAuth).not.toHaveBeenCalled()
    const dialog = screen.getByRole('dialog', { name: '启用日历写入？' })
    expect(within(dialog).getByText('calendar@example.test')).toBeInTheDocument()
    fireEvent.click(within(dialog).getByRole('button', { name: '前往 Google 授权' }))
    await waitFor(() => expect(api.beginPersonalCalendarOAuth).toHaveBeenCalledWith(expect.objectContaining({ connection_id: connectionId, expected_revision: 1, write: true })))
    expect(api.beginPersonalCalendarOAuth).toHaveBeenCalledTimes(1)
    expect(api.approvePersonalCalendarAction).not.toHaveBeenCalled()
    api.personalConnections.mockResolvedValue({ connections: [other, connection({ can_write: true, revision: 2 })] })
    api.personalCalendarOAuth.mockResolvedValue(oauth({ status: 'connected', connection_id: connectionId }))
    fireEvent.click(screen.getByRole('button', { name: '核对原请求' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '批准并执行' })).toBeEnabled())
    expect(api.approvePersonalCalendarAction).not.toHaveBeenCalled()
    expect(api.createPersonalCalendarAction).not.toHaveBeenCalled(); expect(api.revisePersonalCalendarAction).not.toHaveBeenCalled()
  })
  it('invalidates an upgrade confirmation when that draft account revision changes', async () => {
    const api = apiMock(); mount(api); await selectAction()
    fireEvent.click(screen.getByRole('button', { name: '启用写入权限' }))
    api.personalConnections.mockResolvedValue({ connections: [connection({ revision: 2 })] })
    fireEvent.click(screen.getByRole('button', { name: '刷新连接' }))
    await waitFor(() => expect(within(screen.getByRole('dialog', { name: '启用日历写入？' })).getByRole('button', { name: '前往 Google 授权' })).toBeDisabled())
    expect(api.beginPersonalCalendarOAuth).not.toHaveBeenCalled(); expect(api.approvePersonalCalendarAction).not.toHaveBeenCalled()
  })
  it.each(['disconnected', 'reauth_required', 'refresh_unknown'] as const)('keeps drafting locked when the original account is %s', async status => {
    const { api } = mount(apiMock(connection({ status })))
    const create = await screen.findByRole('button', { name: '新建日程草稿' })
    expect(create).toBeDisabled()
    fireEvent.click(await screen.findByRole('button', { name: /季度项目评审/ }))
    expect(screen.getByRole('button', { name: '拟定日程修改' })).toBeDisabled()
    fireEvent.click(screen.getByRole('tab', { name: /动作审批/ })); fireEvent.click(await screen.findByRole('button', { name: /团队方案讨论/ }))
    expect(screen.getByRole('button', { name: '批准并执行' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '修改草稿' })).toBeDisabled()
    expect(screen.queryByRole('button', { name: '启用写入权限' })).not.toBeInTheDocument()
    expect(screen.getByText('原账号或授权批次已变化，请重新核验')).toBeInTheDocument(); noAutomaticExecution(api)
  })
  it('keeps unknown original requests locked across all draft and upgrade entry points', async () => {
    localStorage.setItem(connectionRecoveryKey('calendar-A'), JSON.stringify({ request_id: 'readonly_request_unknown', operation: 'create_action', owner_key: context.owner_key, workspace_key: context.workspace_key, resource_id: connectionId }))
    const { api } = mount()
    await screen.findByText('primary'); expect(screen.getByRole('button', { name: '新建日程草稿' })).toBeDisabled()
    fireEvent.click(await screen.findByRole('button', { name: /季度项目评审/ })); expect(screen.getByRole('button', { name: '拟定日程修改' })).toBeDisabled()
    await selectAction(); expect(screen.getByRole('button', { name: '修改草稿' })).toBeDisabled(); expect(screen.getByRole('button', { name: '启用写入权限' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '批准并执行' })).toBeDisabled()
    expect(api.createPersonalCalendarAction).not.toHaveBeenCalled(); expect(api.revisePersonalCalendarAction).not.toHaveBeenCalled(); noAutomaticExecution(api)
  })
  it('locks an already-open read-only draft immediately when the runtime disconnects', async () => {
    const api = apiMock(), view = mount(api)
    const create = await screen.findByRole('button', { name: '新建日程草稿' }); await waitFor(() => expect(create).toBeEnabled()); fireEvent.click(create)
    fireEvent.change(screen.getByLabelText('日程名称'), { target: { value: '离线前草稿' } })
    view.rerender(<ConnectionWorkspace api={api} namespace="calendar-A" agents={[agent]} connected={false} onClose={vi.fn()} />)
    expect(screen.getByLabelText('日程名称')).toBeDisabled(); expect(screen.getByRole('button', { name: '保存草稿' })).toBeDisabled()
    expect(api.createPersonalCalendarAction).not.toHaveBeenCalled(); noAutomaticExecution(api)
  })
  it('keeps a fresh write-enabled epoch from approving the old draft or masquerading as a pure scope upgrade', async () => {
    const api = apiMock(connection({ can_write: true, auth_epoch: 3 })); mount(api); await selectAction()
    expect(screen.getByRole('button', { name: '批准并执行' })).toBeDisabled()
    expect(screen.queryByRole('button', { name: '启用写入权限' })).not.toBeInTheDocument()
    expect(screen.getByText('原账号或授权批次已变化，请重新核验')).toBeInTheDocument(); noAutomaticExecution(api)
  })
})
