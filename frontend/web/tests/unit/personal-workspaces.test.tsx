import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { WorkbenchRequestError, WorkbenchRuntimeClient } from '@naumi/shared/api/WorkbenchRuntimeClient'
import type { WorkspaceConnection, WorkspaceContext, WorkspaceOverview, WorkspaceOverviewEntry, WorkspaceReadResult, WorkspaceRecipient, WorkspaceSourceIdentity, WorkspaceTicket } from '@naumi/shared/api/personalWorkspaces'
import { WorkspaceOverview as Overview } from '../../../web2/src/personal/workspaces/WorkspaceOverview'
import { parseWorkspacePending, useWorkspaceOverview, workspacePendingKey, type WorkspaceApi } from '../../../web2/src/personal/workspaces/useWorkspaceOverview'
import { parseWorkspaceJson, validateConnection, validateRecipient, validateTicket, validateWorkspaceEndpoint, validateWorkspaceOverview, validateWorkspaceResult } from '../../../web2/src/personal/workspaces/workspaceValidation'
import { requestArtifactNavigation } from '../../../web2/src/personal/library/navigation'

const workspace = 'a'.repeat(64), remote = 'b'.repeat(64), hash = 'c'.repeat(64), actorId = `pa_${'d'.repeat(32)}`, connectionId = `wc_${'e'.repeat(32)}`, itemId = `pi_${'f'.repeat(32)}`
const source: WorkspaceSourceIdentity = { version: 1, owner_key: hash, workspace_key: remote, session_db_key: hash, directory_instance_id: '1'.repeat(32), item_instance_id: '2'.repeat(32) }
const recipient: WorkspaceRecipient = { version: 1, owner_key: hash, workspace_key: workspace, session_db_key: hash, directory_instance_id: '3'.repeat(32), actor_id: 'main', actor_version: 1 }
const connection = (patch: Partial<WorkspaceConnection> = {}): WorkspaceConnection => ({ id: connectionId, revision: 1, status: 'active', label: '研究空间', endpoint: 'http://127.0.0.1:8766', source, recipient, source_actor_id: actorId, source_actor_version: 2, scopes: ['items', 'results'], expires_at: '2099-11-06T00:00:00Z', ...patch })
const context: WorkspaceContext = { workspace_key: workspace, owner_key: hash, configured: true, actors: [{ id: 'main', name: '主 Agent', version: 1 }], connection_count: 1, grant_count: 0 }
const entry = (patch: Partial<WorkspaceOverviewEntry> = {}): WorkspaceOverviewEntry => ({ item_id: itemId, item_revision: 2, once_key: '4'.repeat(32), definition_sha256: hash, title: '核验研究资料', state: 'active', assignee_id: actorId, assignee_name: '研究 Agent', schedule: { due_at: '2026-11-05T04:00:00Z', timezone: 'Asia/Shanghai' }, execution: { status: 'completed', occurrence_id: `pio_${'5'.repeat(32)}`, job_id: 'original-job', run_id: 'original-run' }, source_ref: { kind: 'item', workspace_key: remote, item_id: itemId, item_revision: 2, once_key: '4'.repeat(32), definition_sha256: hash, assignee_id: actorId, assignee_version: 2 }, ...patch })
const envelope = { connection_id: connectionId, connection_revision: 1, source, recipient }
const overview = (entries = [entry()], next: string | null = null): WorkspaceOverview => ({ sources: [{ ...envelope, label: '研究空间', availability: 'available', code: null, next_after: next, partial: Boolean(next), entries }], partial: Boolean(next), consistency: 'live_keyset' })
const readResult = (): WorkspaceReadResult => ({ ...envelope, availability: 'available', code: null, item: entry(), result: { ...entry().execution!, answer: '已核验 825 元，并保留原始证据。', artifacts: [{ artifact_id: `art_${'6'.repeat(32)}`, version: 2, sha256: hash, title: '核验报告', content_text: '发票合计 825 元，来源材料三份。' }], delivery_id: 'original-delivery', delivery_sha256: hash, read_receipt: null } })
const ticket: WorkspaceTicket = { version: 1, grant_id: `wg_${'7'.repeat(32)}`, grant_revision: 1, source, recipient, source_actor_id: actorId, source_actor_version: 2, scopes: ['items', 'results'], expires_at: '2099-11-06T00:00:00Z', secret: '8'.repeat(64) }
function apiMock() {
  const operation = vi.fn().mockImplementation(async (name: string) => {
    if (name === 'list') return { connections: [connection()] }
    if (name === 'grants') return { grants: [] }
    if (name === 'overview') return overview()
    if (name === 'result') return readResult()
    if (name === 'recipient') return { recipient }
    if (name === 'requests') return { status: 'missing', request_id: 'original' }
    throw new Error('unexpected operation ' + name)
  })
  return { getPersonalWorkspaceContext: vi.fn().mockResolvedValue(context), executePersonalWorkspaceOperation: operation } as unknown as WorkspaceApi & { executePersonalWorkspaceOperation: typeof operation }
}
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done }); return { promise, resolve } }
beforeEach(() => { localStorage.clear(); history.replaceState({}, '', '/web2'); Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn().mockResolvedValue(undefined) } }) })
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers() })

describe('workspace federation boundary validation', () => {
  it('accepts only literal loopback HTTP origins without credentials, paths, queries, fragments or normalized IP aliases', () => {
    expect(validateWorkspaceEndpoint('http://127.0.0.1:8766')).toBe('http://127.0.0.1:8766')
    expect(validateWorkspaceEndpoint('http://[::1]:8766')).toBe('http://[::1]:8766')
    for (const url of ['https://127.0.0.1:8766', 'http://localhost:8766', 'http://127.0.0.1:8766/api', 'http://user:password@127.0.0.1:8766', 'http://127.0.0.1:8766?token=private', 'http://127.0.0.1:8766#private', 'http://127.1:8766', 'http://2130706433:8766', 'http://0x7f000001:8766']) expect(() => validateWorkspaceEndpoint(url)).toThrow()
  })
  it('rejects duplicate JSON keys including escaped names, oversized and nonfinite data', () => {
    expect(parseWorkspaceJson('{"a":{"b":1},"c":["a",2]}')).toEqual({ a: { b: 1 }, c: ['a', 2] })
    for (const raw of ['{"secret":1,"secr\\u0065t":2}', '{"a":{"b":1,"b":2}}', '{"n":1e500}', '"' + 'x'.repeat(262145) + '"']) expect(() => parseWorkspaceJson(raw)).toThrow()
  })
  it('validates exact public card and secret ticket identities without legacy actor versions', () => {
    expect(validateTicket(ticket)).toBe(ticket)
    expect(() => validateRecipient({ ...recipient, secret: 'PRIVATE' } as WorkspaceRecipient)).toThrow()
    expect(() => validateRecipient({ ...recipient, actor_version: 0 })).toThrow()
    expect(() => validateTicket({ ...ticket, scopes: ['results'] })).toThrow()
    expect(() => validateTicket({ ...ticket, secret: 'invalid' })).toThrow()
  })
  it('rejects secret material in public connection views', () => {
    expect(validateConnection(connection(), workspace).id).toBe(connectionId)
    expect(() => validateConnection({ ...connection(), ticket } as WorkspaceConnection, workspace)).toThrow()
  })
  it('binds source envelopes, original item revisions and actor versions', () => {
    expect(validateWorkspaceOverview(overview(), [connection()]).sources[0].entries[0].source_ref.item_revision).toBe(2)
    const page = overview(); page.sources[0].connection_revision = 2
    expect(() => validateWorkspaceOverview(page, [connection()])).toThrow()
    const mismatch = overview(); mismatch.sources[0].entries[0].source_ref.item_revision = 1
    expect(() => validateWorkspaceOverview(mismatch, [connection()])).toThrow()
  })
  it('preserves original main ItemRef version zero and source main delegation to another original assignee', () => {
    const mainItem = entry({ assignee_id: 'main', source_ref: { ...entry().source_ref, assignee_id: 'main', assignee_version: 0 } })
    expect(validateWorkspaceOverview(overview([mainItem]), [connection({ source_actor_id: 'main', source_actor_version: 1 })]).sources[0].entries[0].source_ref.assignee_version).toBe(0)
    expect(validateWorkspaceOverview(overview(), [connection({ source_actor_id: 'main', source_actor_version: 1 })]).sources[0].entries[0].assignee_id).toBe(actorId)
    expect(() => validateWorkspaceOverview(overview([entry({ source_ref: { ...entry().source_ref, assignee_version: 0 } })]), [connection()])).toThrow()
  })
  it('strips unavailable source rows and result bodies even if the wire carries private text', () => {
    const page = overview(); page.sources[0].availability = 'unavailable'; page.sources[0].code = 'grant_revoked'; page.sources[0].next_after = 'private-cursor'
    expect(validateWorkspaceOverview(page, [connection()]).sources[0]).toMatchObject({ entries: [], next_after: null })
    expect(validateWorkspaceResult({ ...readResult(), availability: 'unavailable' }, connection(), itemId)).toMatchObject({ result: null, item: null })
  })
  it('retains partial-source truth when a healthy response omits inaccessible rows without a next cursor', () => {
    const page = overview([]); page.sources[0].partial = true; page.partial = true
    expect(validateWorkspaceOverview(page, [connection()]).sources[0].partial).toBe(true)
  })
  it('rejects substituted results, delivery digests and artifact references', () => {
    for (const mutate of [(value: WorkspaceReadResult) => { value.result!.run_id = 'other-run' }, (value: WorkspaceReadResult) => { value.result!.delivery_sha256 = 'invalid' }, (value: WorkspaceReadResult) => { value.result!.artifacts[0].version = 0 }, (value: WorkspaceReadResult) => { value.result!.read_receipt = {} as null }]) { const value = readResult(); mutate(value); expect(() => validateWorkspaceResult(value, connection(), itemId)).toThrow() }
  })
  it('persists only opaque request identities and disallows secret, payload, endpoint and body fields', () => {
    const value = { version: 1, operation: 'connect', request_id: 'original', workspace_key: workspace }
    expect(parseWorkspacePending(JSON.stringify(value))).toEqual(value)
    for (const field of ['ticket', 'secret', 'payload', 'recipient', 'endpoint', 'title']) expect(() => parseWorkspacePending(JSON.stringify({ ...value, [field]: 'PRIVATE' }))).toThrow()
    expect(() => parseWorkspacePending(JSON.stringify({ ...value, operation: 'disconnect' }))).toThrow()
  })
})

describe('workspace overview production components', () => {
  it('renders original rows with filters, only reads results on click and shows real answer plus artifact text', async () => {
    const api = apiMock()
    render(<Overview api={api} namespace="hub" connected onClose={vi.fn()} />)
    await screen.findByText('核验研究资料')
    expect(api.executePersonalWorkspaceOperation.mock.calls.some(([name]) => name === 'result')).toBe(false)
    fireEvent.change(screen.getByLabelText('搜索汇总事项'), { target: { value: '不存在' } })
    expect(screen.queryByText('核验研究资料')).toBeNull()
    fireEvent.change(screen.getByLabelText('搜索汇总事项'), { target: { value: '' } })
    fireEvent.click(screen.getByLabelText('读取结果 核验研究资料'))
    await screen.findByText('已核验 825 元，并保留原始证据。')
    expect(screen.getByText('发票合计 825 元，来源材料三份。')).toBeInTheDocument()
    expect(api.executePersonalWorkspaceOperation.mock.calls.filter(([name]) => !['list', 'grants', 'overview', 'result'].includes(name))).toHaveLength(0)
    expect(localStorage.length).toBe(0)
  })
  it('retains closed state as closed rather than completed', async () => {
    const api = apiMock(), original = api.executePersonalWorkspaceOperation.getMockImplementation()!
    api.executePersonalWorkspaceOperation.mockImplementation((name: string, ...args: unknown[]) => name === 'overview' ? Promise.resolve(overview([entry({ state: 'closed', execution: null })])) : original(name, ...args))
    render(<Overview api={api} namespace="hub" connected onClose={vi.fn()} />)
    await screen.findByText('核验研究资料')
    expect(screen.getAllByText('已关闭').length).toBeGreaterThan(0)
    expect(screen.getByLabelText('读取结果 核验研究资料')).toBeDisabled()
  })
  it('uses per-source keyset pagination and labels loaded-page counts truthfully', async () => {
    const api = apiMock(), original = api.executePersonalWorkspaceOperation.getMockImplementation()!
    api.executePersonalWorkspaceOperation.mockImplementation((name: string, args: Record<string, unknown>) => name === 'overview' ? Promise.resolve(args.after ? overview([entry({ item_id: `pi_${'9'.repeat(32)}`, title: '第二项', source_ref: { ...entry().source_ref, item_id: `pi_${'9'.repeat(32)}` } })]) : overview([entry()], 'opaque-cursor')) : original(name, args))
    render(<Overview api={api} namespace="hub" connected onClose={vi.fn()} />)
    await screen.findByText('已加载 1 项')
    fireEvent.click(screen.getByText('加载更多 · 研究空间'))
    await screen.findByText('第二项')
    expect(api.executePersonalWorkspaceOperation).toHaveBeenCalledWith('overview', { connection_id: connectionId, after: 'opaque-cursor', limit: 50 })
    expect(screen.getByText('已加载 2 项')).toBeInTheDocument()
  })
  it('clears source rows and an open result after source revocation', async () => {
    const api = apiMock(), original = api.executePersonalWorkspaceOperation.getMockImplementation()!, revoked = overview(); revoked.sources[0].availability = 'unavailable'; revoked.sources[0].code = 'grant_revoked'
    render(<Overview api={api} namespace="hub" connected onClose={vi.fn()} />)
    await screen.findByText('核验研究资料'); fireEvent.click(screen.getByLabelText('读取结果 核验研究资料')); await screen.findByText('发票合计 825 元，来源材料三份。')
    api.executePersonalWorkspaceOperation.mockImplementation((name: string, ...args: unknown[]) => name === 'overview' ? Promise.resolve(revoked) : original(name, ...args))
    fireEvent.click(screen.getByLabelText('刷新工作区汇总'))
    await waitFor(() => expect(screen.queryByText('发票合计 825 元，来源材料三份。')).toBeNull())
    expect(screen.queryByText('已核验 825 元，并保留原始证据。')).toBeNull()
  })
  it('never displays private foreign rows or result bodies after hub disconnect', async () => {
    const api = apiMock(), props = { api, namespace: 'hub', connected: true, onClose: vi.fn() }
    const ui = render(<Overview {...props} />)
    await screen.findByText('核验研究资料'); fireEvent.click(screen.getByLabelText('读取结果 核验研究资料')); await screen.findByText('发票合计 825 元，来源材料三份。')
    ui.rerender(<Overview {...props} connected={false} />)
    expect(screen.queryByText('发票合计 825 元，来源材料三份。')).toBeNull()
    expect(screen.getByText('当前工作区已断开')).toBeInTheDocument()
  })
  it('generates a public recipient card without storing it in browser storage', async () => {
    const api = apiMock(); render(<Overview api={api} namespace="hub" connected onClose={vi.fn()} />)
    await screen.findByText('核验研究资料'); fireEvent.click(screen.getByText('管理来源')); fireEvent.click(screen.getByText('生成接收卡'))
    const card = await screen.findByLabelText('公开接收卡')
    expect((card as HTMLTextAreaElement).value).toContain(workspace)
    fireEvent.click(screen.getByText('复制接收卡')); await screen.findByText('已复制接收卡')
    expect(localStorage.length).toBe(0)
  })
  it('guards pasted ticket edits against navigation and clears the secret after confirmed dismissal', async () => {
    const api = apiMock(), proceed = vi.fn(); render(<Overview api={api} namespace="hub" connected onClose={vi.fn()} />)
    await screen.findByText('核验研究资料'); fireEvent.click(screen.getByText('管理来源')); fireEvent.click(screen.getByText('连接工作区'))
    fireEvent.change(screen.getByLabelText('连接凭据 JSON'), { target: { value: JSON.stringify(ticket) } })
    act(() => requestArtifactNavigation(proceed))
    expect(proceed).not.toHaveBeenCalled(); expect(screen.getByRole('alertdialog', { name: '确认离开工作区表单' })).toBeInTheDocument()
    fireEvent.click(screen.getByText('继续编辑')); expect((screen.getByLabelText('连接凭据 JSON') as HTMLTextAreaElement).value).toContain(ticket.secret)
    act(() => requestArtifactNavigation(proceed)); fireEvent.click(screen.getByText('放弃并离开'))
    expect(proceed).toHaveBeenCalledTimes(1); expect(screen.queryByLabelText('连接凭据 JSON')).toBeNull(); expect(localStorage.length).toBe(0)
  })
  it('connects through a deliberate validated form submission, clears ticket input and never persists the secret', async () => {
    const api = apiMock(), original = api.executePersonalWorkspaceOperation.getMockImplementation()!
    api.executePersonalWorkspaceOperation.mockImplementation((name: string, args: { payload?: { request_id: string } }) => name === 'connect' ? Promise.resolve({ accepted: true, request_id: args.payload!.request_id, connection: connection() }) : original(name, args))
    render(<Overview api={api} namespace="hub" connected onClose={vi.fn()} />)
    await screen.findByText('核验研究资料'); fireEvent.click(screen.getByText('管理来源')); fireEvent.click(screen.getByText('连接工作区'))
    fireEvent.change(screen.getByLabelText('来源名称'), { target: { value: '研究空间' } }); fireEvent.change(screen.getByLabelText('工作区地址'), { target: { value: connection().endpoint } }); fireEvent.change(screen.getByLabelText('连接凭据 JSON'), { target: { value: JSON.stringify(ticket) } }); fireEvent.click(screen.getByText('核对并连接'))
    await waitFor(() => expect(screen.queryByLabelText('连接凭据 JSON')).toBeNull())
    expect(api.executePersonalWorkspaceOperation.mock.calls.filter(([name]) => name === 'connect')).toHaveLength(1)
    expect([...Array(localStorage.length)].map((_, index) => localStorage.getItem(localStorage.key(index)!)).join('')).not.toContain(ticket.secret)
  })
  it('issues a real one-time ticket view from an exact receipt and keeps it in memory only', async () => {
    const api = apiMock(), original = api.executePersonalWorkspaceOperation.getMockImplementation()!
    api.executePersonalWorkspaceOperation.mockImplementation((name: string, args: { payload?: { request_id: string; recipient: WorkspaceRecipient; source_actor_id: string; source_actor_version: number; scopes: string[]; expires_at: string } }) => {
      if (name !== 'issue') return original(name, args)
      const body = args.payload!, issued = { ...ticket, source: { ...source, workspace_key: workspace }, recipient: body.recipient, source_actor_id: body.source_actor_id, source_actor_version: body.source_actor_version, scopes: body.scopes, expires_at: body.expires_at }
      return Promise.resolve({ accepted: true, request_id: body.request_id, grant: { id: issued.grant_id, revision: 1, status: 'active', source: issued.source, recipient: issued.recipient, source_actor_id: issued.source_actor_id, source_actor_version: issued.source_actor_version, scopes: issued.scopes, expires_at: issued.expires_at, created_at: '2026-11-05T04:00:00Z' }, ticket: issued })
    })
    render(<Overview api={api} namespace="hub" connected onClose={vi.fn()} />)
    await screen.findByText('核验研究资料'); fireEvent.click(screen.getByText('管理来源')); fireEvent.click(screen.getByText('签发授权'))
    fireEvent.change(screen.getByLabelText('接收卡 JSON'), { target: { value: JSON.stringify({ ...recipient, workspace_key: remote }) } }); fireEvent.click(screen.getAllByText('签发授权').at(-1)!)
    const shown = await screen.findByLabelText('一次性连接凭据')
    expect((shown as HTMLTextAreaElement).value).toContain(ticket.secret)
    expect(localStorage.length).toBe(0); fireEvent.click(screen.getByText('复制凭据')); await screen.findByText('已复制')
  })
  it('removes a pasted secret from the DOM immediately when the hub disconnects', async () => {
    const api = apiMock(), props = { api, namespace: 'hub', connected: true, onClose: vi.fn() }, ui = render(<Overview {...props} />)
    await screen.findByText('核验研究资料'); fireEvent.click(screen.getByText('管理来源')); fireEvent.click(screen.getByText('连接工作区'))
    fireEvent.change(screen.getByLabelText('连接凭据 JSON'), { target: { value: JSON.stringify(ticket) } })
    ui.rerender(<Overview {...props} connected={false} />)
    expect(screen.queryByLabelText('连接凭据 JSON')).toBeNull(); expect(document.body.textContent).not.toContain(ticket.secret)
  })
})

describe('workspace request recovery and stale guards', () => {
  it('accepts post-commit opaque connect receipts without replaying a committed mutation', async () => {
    const api = apiMock(), original = api.executePersonalWorkspaceOperation.getMockImplementation()!
    api.executePersonalWorkspaceOperation.mockImplementation((name: string, ...args: unknown[]) => name === 'connect' ? Promise.resolve({ accepted: true, request_id: 'original', operation: 'connect', workspace_key: workspace, identity: { id: connectionId, revision: 1, status: 'active' }, projection_available: false, content_transport: 'requests' }) : original(name, ...args))
    const { result } = renderHook(() => useWorkspaceOverview({ api, namespace: 'hub', connected: true })); await waitFor(() => expect(result.current.context).not.toBeNull())
    await act(async () => { expect(await result.current.submit('connect', { payload: { request_id: 'original', endpoint: connection().endpoint, label: connection().label, ticket } })).not.toBeNull() })
    expect(result.current.pending).toBeNull(); expect(localStorage.length).toBe(0); expect(result.current.notice).toContain('连接已受理')
    expect(api.executePersonalWorkspaceOperation.mock.calls.filter(([name]) => name === 'connect')).toHaveLength(1)
  })
  it('accepts post-commit opaque issue receipts but never fabricates a lost one-time ticket', async () => {
    const api = apiMock(), original = api.executePersonalWorkspaceOperation.getMockImplementation()!
    api.executePersonalWorkspaceOperation.mockImplementation((name: string, ...args: unknown[]) => name === 'issue' ? Promise.resolve({ accepted: true, request_id: 'original', operation: 'issue', workspace_key: workspace, identity: { id: ticket.grant_id, revision: 1, status: 'active' }, projection_available: false, content_transport: 'requests' }) : original(name, ...args))
    const { result } = renderHook(() => useWorkspaceOverview({ api, namespace: 'hub', connected: true })); await waitFor(() => expect(result.current.context).not.toBeNull())
    await act(async () => { const value = await result.current.submit('issue', { payload: { request_id: 'original', recipient, source_actor_id: 'main', source_actor_version: 1, scopes: ['items'], expires_at: ticket.expires_at } }); expect(value && 'ticket' in value).toBe(false) })
    expect(result.current.pending).toBeNull(); expect(result.current.notice).toContain('请撤销该授权后重新签发'); expect(localStorage.length).toBe(0)
  })
  it('keeps UNKNOWN identity when an opaque accepted receipt is substituted from another workspace', async () => {
    const api = apiMock(), original = api.executePersonalWorkspaceOperation.getMockImplementation()!
    api.executePersonalWorkspaceOperation.mockImplementation((name: string, ...args: unknown[]) => name === 'connect' ? Promise.resolve({ accepted: true, request_id: 'original', operation: 'connect', workspace_key: remote, identity: { id: connectionId, revision: 1, status: 'active' }, projection_available: false, content_transport: 'requests' }) : original(name, ...args))
    const { result } = renderHook(() => useWorkspaceOverview({ api, namespace: 'hub', connected: true })); await waitFor(() => expect(result.current.context).not.toBeNull())
    await act(async () => { expect(await result.current.submit('connect', { payload: { request_id: 'original', endpoint: connection().endpoint, label: connection().label, ticket } })).toBeNull() })
    expect(result.current.pending?.request_id).toBe('original')
  })
  it('stores only original identity after UNKNOWN connect and reconciles without replaying the write', async () => {
    const api = apiMock(), original = api.executePersonalWorkspaceOperation.getMockImplementation()!
    api.executePersonalWorkspaceOperation.mockImplementation((name: string, args: Record<string, unknown>) => name === 'connect' ? Promise.reject(new Error('response lost')) : name === 'requests' ? Promise.resolve({ status: 'accepted', request_id: 'original', operation: 'connect', identity: { id: connectionId, revision: 1, status: 'active' } }) : original(name, args))
    const { result } = renderHook(() => useWorkspaceOverview({ api, namespace: 'hub', connected: true }))
    await waitFor(() => expect(result.current.context).not.toBeNull())
    await act(async () => { await result.current.submit('connect', { payload: { request_id: 'original', endpoint: 'http://127.0.0.1:8766', label: '研究空间', ticket } }) })
    expect(JSON.parse(localStorage.getItem(workspacePendingKey('hub'))!)).toEqual({ version: 1, operation: 'connect', request_id: 'original', workspace_key: workspace })
    await act(async () => { await result.current.reconcile() })
    expect(result.current.pending).toBeNull(); expect(localStorage.length).toBe(0)
    expect(api.executePersonalWorkspaceOperation.mock.calls.filter(([name]) => name === 'connect')).toHaveLength(1)
    expect(api.executePersonalWorkspaceOperation.mock.calls.filter(([name]) => name === 'requests')).toHaveLength(1)
  })
  it('retains missing original request rather than issuing a new mutation', async () => {
    localStorage.setItem(workspacePendingKey('hub'), JSON.stringify({ version: 1, operation: 'connect', request_id: 'original', workspace_key: workspace }))
    const api = apiMock(), { result } = renderHook(() => useWorkspaceOverview({ api, namespace: 'hub', connected: true }))
    await waitFor(() => expect(result.current.context).not.toBeNull())
    await act(async () => { await result.current.reconcile() })
    expect(result.current.pending?.request_id).toBe('original'); expect(result.current.error).toContain('未重新提交')
    expect(api.executePersonalWorkspaceOperation.mock.calls.some(([name]) => name === 'connect')).toBe(false)
  })
  it('rejects a recovered receipt for the wrong operation', async () => {
    localStorage.setItem(workspacePendingKey('hub'), JSON.stringify({ version: 1, operation: 'connect', request_id: 'original', workspace_key: workspace }))
    const api = apiMock(), original = api.executePersonalWorkspaceOperation.getMockImplementation()!
    api.executePersonalWorkspaceOperation.mockImplementation((name: string, ...args: unknown[]) => name === 'requests' ? Promise.resolve({ status: 'accepted', request_id: 'original', operation: 'issue', identity: { id: ticket.grant_id, revision: 1, status: 'active' } }) : original(name, ...args))
    const { result } = renderHook(() => useWorkspaceOverview({ api, namespace: 'hub', connected: true })); await waitFor(() => expect(result.current.context).not.toBeNull())
    await act(async () => { await result.current.reconcile() }); expect(result.current.pending).not.toBeNull()
  })
  it('clears a definitively rejected 409 but keeps UNKNOWN 503 identity', async () => {
    const api = apiMock(), original = api.executePersonalWorkspaceOperation.getMockImplementation()!; let status = 409
    api.executePersonalWorkspaceOperation.mockImplementation((name: string, ...args: unknown[]) => name === 'disconnect' ? Promise.reject(new WorkbenchRequestError('conflict', status)) : original(name, ...args))
    const { result } = renderHook(() => useWorkspaceOverview({ api, namespace: 'hub', connected: true })); await waitFor(() => expect(result.current.context).not.toBeNull())
    await act(async () => { await result.current.submit('disconnect', { payload: { request_id: 'first', connection_id: connectionId, expected_revision: 1 } }) })
    expect(result.current.pending).toBeNull(); status = 503
    await waitFor(() => expect(result.current.context).not.toBeNull())
    await act(async () => { await result.current.submit('disconnect', { payload: { request_id: 'second', connection_id: connectionId, expected_revision: 1 } }) })
    expect(result.current.pending?.request_id).toBe('second')
  })
  it('ignores a late result from an old namespace', async () => {
    const api = apiMock(), original = api.executePersonalWorkspaceOperation.getMockImplementation()!, delayed = deferred<WorkspaceReadResult>()
    api.executePersonalWorkspaceOperation.mockImplementation((name: string, ...args: unknown[]) => name === 'result' ? delayed.promise : original(name, ...args))
    const { result, rerender } = renderHook(({ namespace }) => useWorkspaceOverview({ api, namespace, connected: true }), { initialProps: { namespace: 'hub-A' } })
    await waitFor(() => expect(result.current.context).not.toBeNull())
    let reading!: Promise<void>; act(() => { reading = result.current.openResult(connectionId, itemId) })
    rerender({ namespace: 'hub-B' }); await act(async () => { delayed.resolve(readResult()); await reading })
    expect(result.current.result).toBeNull(); expect(result.current.selection).toBeNull()
  })
  it('discards an in-flight result after a refresh proves its source unavailable', async () => {
    const api = apiMock(), original = api.executePersonalWorkspaceOperation.getMockImplementation()!, delayed = deferred<WorkspaceReadResult>(); let offline = false
    api.executePersonalWorkspaceOperation.mockImplementation((name: string, ...args: unknown[]) => {
      if (name === 'result') return delayed.promise
      if (name === 'overview' && offline) { const page = overview(); page.sources[0].availability = 'unavailable'; page.sources[0].code = 'source_unavailable'; page.sources[0].partial = true; return Promise.resolve(page) }
      return original(name, ...args)
    })
    const { result } = renderHook(() => useWorkspaceOverview({ api, namespace: 'hub', connected: true })); await waitFor(() => expect(result.current.context).not.toBeNull())
    let reading!: Promise<void>; act(() => { reading = result.current.openResult(connectionId, itemId) }); offline = true; act(() => result.current.refresh())
    await waitFor(() => expect(result.current.view?.sources[0].availability).toBe('unavailable'))
    await act(async () => { delayed.resolve(readResult()); await reading }); expect(result.current.result).toBeNull()
  })
  it('does not overlap slow refreshes or starve a slow read', async () => {
    const api = apiMock(), delayed = deferred<WorkspaceContext>()
    api.getPersonalWorkspaceContext = vi.fn().mockReturnValue(delayed.promise)
    const { result } = renderHook(() => useWorkspaceOverview({ api, namespace: 'hub', connected: true }))
    act(() => { result.current.refresh(); result.current.refresh() })
    expect(api.getPersonalWorkspaceContext).toHaveBeenCalledTimes(1)
    await act(async () => { delayed.resolve(context); await delayed.promise })
    await waitFor(() => expect(result.current.context).not.toBeNull())
  })
})

describe('workspace runtime HTTP client', () => {
  it('sends flat operation arguments to the current hub only, with header authentication and no-store', async () => {
    const seen: { path: string; body: unknown; key: string | null }[] = []
    const server = setupServer(http.get('http://127.0.0.1:8765/personal/workspaces/context', ({ request }) => { seen.push({ path: new URL(request.url).pathname, body: null, key: request.headers.get('authorization') }); return HttpResponse.json(context) }), http.post('http://127.0.0.1:8765/personal/workspaces/operations', async ({ request }) => { seen.push({ path: new URL(request.url).pathname, body: await request.json(), key: request.headers.get('authorization') }); return HttpResponse.json(overview()) }))
    server.listen({ onUnhandledRequest: 'error' })
    try {
      const client = new WorkbenchRuntimeClient('http://127.0.0.1:8765', async () => 'hub-key')
      await client.getPersonalWorkspaceContext(); await client.executePersonalWorkspaceOperation('overview', { connection_id: connectionId, limit: 50 })
      expect(seen.map(row => row.path)).toEqual(['/personal/workspaces/context', '/personal/workspaces/operations'])
      expect(seen[1].body).toEqual({ operation: 'overview', connection_id: connectionId, limit: 50 }); expect(seen.every(row => row.key === 'Bearer hub-key')).toBe(true)
    } finally { server.close() }
  })
})
