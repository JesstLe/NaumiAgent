import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ResearchApi } from '../../../shared/src/api/personalResearch'
import type { ResearchSessionContextApi, ResearchSessionContextPending, ResearchSessionContextPreview, ResearchSessionContextReceipt, ResearchSessionContextWrite } from '../../../shared/src/api/personalResearchContext'
import { WorkbenchRequestError, WorkbenchRuntimeClient } from '../../../shared/src/api/WorkbenchRuntimeClient'
import { ResearchContext } from '../../../web2/src/personal/research/ResearchContext'
import { ResearchWorkspace } from '../../../web2/src/personal/research/ResearchWorkspace'
import { parseResearchContextPending, researchContextError, researchContextPendingKey, validateResearchContext, validateResearchContextReceipt } from '../../../web2/src/personal/research/useResearchContext'

const workspace = 'a'.repeat(64), hash = 'b'.repeat(64), digest = 'c'.repeat(64)
const actor = `pa_${'d'.repeat(32)}`, session = 'original-session-42', requestId = 'e'.repeat(32)
const notebook = `rq_${'f'.repeat(32)}`, source = `rs_${'1'.repeat(32)}`, artifact = `art_${'2'.repeat(32)}`
const target = { workspaceKey: workspace, sessionId: session, actorId: actor }
const namespace = 'context-A'
const key = researchContextPendingKey(namespace, target)
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T
const deferred = <T,>() => { let resolve!: (value: T) => void, reject!: (error: unknown) => void; const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const failure = (status: number, code = '') => Object.assign(new Error('PRIVATE_RAW_SERVER_ERROR'), { status, code })
const pending = (): ResearchSessionContextPending => ({ version: 1, operation: 'context_rollforward', request_id: requestId, workspace_key: workspace, session_id: session, actor_id: actor })
function preview(changes: Partial<ResearchSessionContextPreview> = {}): ResearchSessionContextPreview {
  return { workspace_key: workspace, actor_id: actor, actor_version: 2, actor_name: 'Mira', session_id: session, session_title: '原始研究对话', context_revision: 0,
    plan_digest: digest, status: 'ready', historical_notice: '原文保持不变，旧结论仍需重新核实。',
    materials: [{ original_ref: { artifact_id: artifact, version: 1, sha256: hash, grant_id: '', grant_revision: 0 }, title: 'PRIVATE_RESEARCH_MATERIAL_42', body_sha256: hash, read_count: 2 }],
    changes: [{ notebook_id: notebook, source_id: source, from_revision: 1, to_revision: 2, from_expired: true, to_expired: false }], ...changes }
}
function receipt(id: string = requestId): ResearchSessionContextReceipt {
  return { status: 'completed', request_id: id, workspace_key: workspace, session_id: session, actor_id: actor, context_revision: 1, updated_count: 1,
    view: preview({ status: 'current', context_revision: 1, materials: [], changes: [] }) }
}
function apiMock(value = preview()) {
  return {
    getPersonalResearchSessionContext: vi.fn().mockResolvedValue(clone(value)),
    rollforwardPersonalResearchContext: vi.fn().mockImplementation(async (body: ResearchSessionContextWrite) => receipt(body.request_id)),
    getPersonalResearchContextRequest: vi.fn().mockResolvedValue({ status: 'unknown', request_id: requestId }),
  } satisfies ResearchSessionContextApi
}
async function ready(api = apiMock()) {
  const onClose = vi.fn()
  const rendered = render(<ResearchContext api={api} namespace={namespace} target={target} connected onClose={onClose} />)
  await screen.findByRole('heading', { name: '原始研究对话' })
  return { ...rendered, api, onClose }
}
function confirm() {
  fireEvent.click(screen.getByRole('button', { name: '更新依据', exact: true }))
  return screen.getByRole('button', { name: '确认更新', exact: true })
}

beforeEach(() => { localStorage.clear() })
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('research session context exact contracts', () => {
  it('stores only opaque identity and rejects private recovery fields and malformed targets', () => {
    expect(parseResearchContextPending(JSON.stringify(pending()))).toEqual(pending())
    expect(() => parseResearchContextPending(JSON.stringify({ ...pending(), expected_plan_digest: digest }))).toThrow('待核对')
    expect(() => parseResearchContextPending(JSON.stringify({ ...pending(), body: 'PRIVATE_BODY' }))).toThrow('待核对')
    expect(() => parseResearchContextPending('not json')).toThrow('待核对')
    expect(() => parseResearchContextPending(JSON.stringify({ ...pending(), actor_id: 'unknown' }))).toThrow('身份')
    expect(researchContextPendingKey(namespace, { ...target, sessionId: 'other' })).not.toBe(key)
    expect(researchContextPendingKey(namespace, { ...target, actorId: 'main' })).not.toBe(key)
    expect(researchContextPendingKey('context-B', target)).not.toBe(key)
  })
  it('validates original materials, byte identity, source changes and all receipt identities', () => {
    expect(validateResearchContext(preview(), target)).toEqual(preview())
    expect(() => validateResearchContext(preview({ session_id: 'other' }), target)).toThrow('身份')
    expect(() => validateResearchContext(preview({ actor_id: 'main' }), target)).toThrow('身份')
    expect(() => validateResearchContext(preview({ workspace_key: 'f'.repeat(64) }), target)).toThrow('身份')
    const bad = preview(); bad.materials[0].body_sha256 = 'f'.repeat(64)
    expect(() => validateResearchContext(bad, target)).toThrow('摘要')
    const changed = preview(); changed.changes[0].to_revision = 0
    expect(() => validateResearchContext(changed, target)).toThrow('来源')
    expect(() => validateResearchContext(preview({ status: 'ready', materials: [] }), target)).toThrow('不完整')
    expect(validateResearchContextReceipt(receipt(), pending(), target)).toEqual(receipt())
    expect(() => validateResearchContextReceipt(receipt('other'), pending(), target)).toThrow('不一致')
    expect(() => validateResearchContextReceipt({ ...receipt(), workspace_key: 'f'.repeat(64) }, pending(), target)).toThrow('不一致')
  })
  it('redacts blocked material and maps errors without showing backend content', () => {
    const blocked = validateResearchContext(preview({ status: 'blocked' }), target)
    expect(blocked.materials).toEqual([]); expect(blocked.changes).toEqual([]); expect(blocked.historical_notice).toBe('')
    expect(JSON.stringify(blocked)).not.toContain('PRIVATE_RESEARCH_MATERIAL')
    for (const code of ['source_stale', 'source_unavailable', 'artifact_access_revoked', 'binding_conflict', 'execution_session_readonly']) {
      expect(researchContextError(failure(409, code))).not.toContain('PRIVATE_RAW')
      expect(researchContextError(failure(409, code))).not.toContain(code)
    }
    expect(researchContextError(new Error('PRIVATE_RAW'))).not.toContain('PRIVATE_RAW')
  })
  it('uses authenticated no-store shared client paths and a distinct explicit operation', async () => {
    const fetcher = vi.fn().mockImplementation(async () => new Response(JSON.stringify(preview()), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetcher)
    const client = new WorkbenchRuntimeClient('http://local-test/api/v1', async () => 'local-fixture-only')
    const body: ResearchSessionContextWrite = { actor_id: actor, session_id: session, request_id: requestId, expected_context_revision: 0, expected_plan_digest: digest }
    await client.getPersonalResearchSessionContext(session, actor)
    await client.rollforwardPersonalResearchContext(body)
    await client.getPersonalResearchContextRequest(requestId)
    expect(fetcher.mock.calls.map(row => row[0])).toEqual([
      `http://local-test/api/v1/personal/research/session-context/${session}?actor_id=${actor}`,
      'http://local-test/api/v1/personal/research/operations/context_rollforward',
      `http://local-test/api/v1/personal/research/context-requests/${requestId}`,
    ])
    expect(fetcher.mock.calls.every(row => row[1].cache === 'no-store' && row[1].headers.Authorization === 'Bearer local-fixture-only')).toBe(true)
    expect(fetcher.mock.calls[1][1].method).toBe('POST'); expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual(body)
    expect(fetcher.mock.calls[0][1].body).toBeUndefined(); expect(fetcher.mock.calls[2][1].body).toBeUndefined()
  })
})

describe('explicit research context update', () => {
  it('previews the actual Agent and original dialogue with zero automatic POST', async () => {
    const { api } = await ready()
    expect(api.getPersonalResearchSessionContext).toHaveBeenCalledWith(session, actor, expect.any(AbortSignal))
    expect(screen.getByText('Mira')).toBeInTheDocument(); expect(screen.getByText(session)).toBeInTheDocument()
    expect(screen.getByLabelText('来源变化')).toHaveTextContent('v1 · 已过期'); expect(screen.getByLabelText('来源变化')).toHaveTextContent('v2')
    expect(screen.getByText('PRIVATE_RESEARCH_MATERIAL_42')).toBeInTheDocument()
    expect(api.rollforwardPersonalResearchContext).not.toHaveBeenCalled(); expect(localStorage.length).toBe(0)
    confirm()
    expect(screen.getByRole('group', { name: '确认更新对话依据' })).toHaveTextContent('原对话和原文保持不变')
    expect(api.rollforwardPersonalResearchContext).not.toHaveBeenCalled()
  })
  it('only confirms the exact preview once and persists no private content', async () => {
    const api = apiMock(), held = deferred<ResearchSessionContextReceipt>()
    api.rollforwardPersonalResearchContext.mockImplementation(() => held.promise)
    await ready(api)
    const button = confirm(); fireEvent.click(button); fireEvent.click(button)
    await waitFor(() => expect(api.rollforwardPersonalResearchContext).toHaveBeenCalledTimes(1))
    const body = api.rollforwardPersonalResearchContext.mock.calls[0][0]
    expect(body).toEqual({ actor_id: actor, session_id: session, request_id: expect.stringMatching(/^[a-f0-9]{32}$/), expected_context_revision: 0, expected_plan_digest: digest })
    const saved = parseResearchContextPending(localStorage.getItem(key)!)
    expect(saved.request_id).toBe(body.request_id); expect(Object.keys(saved).sort()).toEqual(['actor_id', 'operation', 'request_id', 'session_id', 'version', 'workspace_key'])
    expect(localStorage.getItem(key)).not.toContain(digest); expect(localStorage.getItem(key)).not.toContain('PRIVATE_'); expect(localStorage.getItem(key)).not.toContain(artifact)
    await act(async () => { held.resolve(receipt(body.request_id)); await held.promise })
    await waitFor(() => expect(localStorage.getItem(key)).toBeNull())
    expect(screen.getByRole('status')).toHaveTextContent('已更新'); expect(screen.getByRole('button', { name: '更新依据', exact: true })).toBeDisabled()
    expect(screen.getByText('无需更新')).toBeInTheDocument()
  })
  it.each(['current', 'blocked'] as const)('never offers a write for a %s preview', async status => {
    const api = apiMock(preview({ status, materials: status === 'current' ? [] : preview().materials }))
    await ready(api)
    expect(screen.getByRole('button', { name: '更新依据', exact: true })).toBeDisabled()
    if (status === 'blocked') { expect(screen.getByText('原依据不可用')).toBeInTheDocument(); expect(screen.queryByText('PRIVATE_RESEARCH_MATERIAL_42')).not.toBeInTheDocument() }
    expect(api.rollforwardPersonalResearchContext).not.toHaveBeenCalled()
  })
  it('permits a zero-update completion without falsely increasing its revision', async () => {
    const api = apiMock()
    api.rollforwardPersonalResearchContext.mockImplementation(async body => ({ ...receipt(body.request_id), context_revision: 0, updated_count: 0, view: preview({ status: 'current', context_revision: 0, materials: [], changes: [] }) }))
    await ready(api); fireEvent.click(confirm())
    await waitFor(() => expect(localStorage.getItem(key)).toBeNull())
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('无需更新'))
    expect(api.rollforwardPersonalResearchContext).toHaveBeenCalledTimes(1)
  })
  it('does not send if durable opaque identity storage fails', async () => {
    const { api } = await ready()
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('denied') })
    fireEvent.click(confirm())
    await screen.findByRole('alert')
    expect(screen.getByRole('alert')).toHaveTextContent('请求状态保存失败'); expect(api.rollforwardPersonalResearchContext).not.toHaveBeenCalled()
  })
  it('drops sensitive preview on read failure and only refreshes with GET', async () => {
    const { api } = await ready()
    api.getPersonalResearchSessionContext.mockRejectedValueOnce(failure(403, 'scope_denied'))
    fireEvent.click(screen.getByRole('button', { name: '重新核对' }))
    await screen.findByRole('alert')
    expect(screen.queryByText('PRIVATE_RESEARCH_MATERIAL_42')).not.toBeInTheDocument(); expect(screen.queryByText('原始研究对话')).not.toBeInTheDocument()
    expect(api.rollforwardPersonalResearchContext).not.toHaveBeenCalled()
  })
  it('retains unknown original request after timeout and reconciles it after remount without POST', async () => {
    const api = apiMock()
    api.rollforwardPersonalResearchContext.mockRejectedValueOnce(new Error('timeout'))
    const mounted = await ready(api); fireEvent.click(confirm())
    await screen.findByRole('alert')
    const saved = parseResearchContextPending(localStorage.getItem(key)!)
    expect(api.rollforwardPersonalResearchContext).toHaveBeenCalledTimes(1); expect(screen.queryByRole('button', { name: '重试' })).not.toBeInTheDocument()
    mounted.unmount()
    api.getPersonalResearchContextRequest.mockResolvedValueOnce({ status: 'completed', request_id: saved.request_id, receipt: receipt(saved.request_id) })
    render(<ResearchContext api={api} namespace={namespace} target={target} connected onClose={vi.fn()} />)
    await waitFor(() => expect(localStorage.getItem(key)).toBeNull())
    expect(api.getPersonalResearchContextRequest).toHaveBeenCalledWith(saved.request_id, expect.any(AbortSignal))
    expect(api.rollforwardPersonalResearchContext).toHaveBeenCalledTimes(1); expect(screen.getByRole('status')).toHaveTextContent('原请求已读回')
  })
  it.each(['unknown', 'pending', 'failed'] as const)('keeps %s recovery opaque without recreating a mutation', async status => {
    localStorage.setItem(key, JSON.stringify(pending()))
    const api = apiMock(); api.getPersonalResearchContextRequest.mockResolvedValueOnce({ status, request_id: requestId })
    render(<ResearchContext api={api} namespace={namespace} target={target} connected onClose={vi.fn()} />)
    await waitFor(() => expect(api.getPersonalResearchContextRequest).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByRole('button', { name: '核对原请求' })).toBeEnabled())
    expect(localStorage.getItem(key)).not.toBeNull(); expect(api.rollforwardPersonalResearchContext).not.toHaveBeenCalled(); expect(api.getPersonalResearchSessionContext).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: '更新依据', exact: true })).toBeDisabled()
  })
  it('keeps the original identity when GET returns a different request receipt', async () => {
    localStorage.setItem(key, JSON.stringify(pending()))
    const api = apiMock(); api.getPersonalResearchContextRequest.mockResolvedValueOnce({ status: 'completed', request_id: requestId, receipt: receipt('other') })
    render(<ResearchContext api={api} namespace={namespace} target={target} connected onClose={vi.fn()} />)
    await screen.findByRole('alert')
    expect(screen.getByRole('alert')).toHaveTextContent('不一致'); expect(parseResearchContextPending(localStorage.getItem(key)!)).toEqual(pending())
    expect(api.rollforwardPersonalResearchContext).not.toHaveBeenCalled(); expect(screen.queryByText('原始研究对话')).not.toBeInTheDocument()
  })
  it('does not accept wrong-workspace POST success or clear the original pending request', async () => {
    const api = apiMock(); api.rollforwardPersonalResearchContext.mockImplementation(async body => ({ ...receipt(body.request_id), workspace_key: 'f'.repeat(64) }))
    await ready(api); fireEvent.click(confirm())
    await screen.findByRole('alert')
    expect(localStorage.getItem(key)).not.toBeNull(); expect(screen.queryByText('原始研究对话')).not.toBeInTheDocument()
  })
  it.each([403, 409])('records a definitive HTTP %i without replaying or retaining sensitive preview', async status => {
    const api = apiMock(); api.rollforwardPersonalResearchContext.mockRejectedValueOnce(failure(status))
    await ready(api); fireEvent.click(confirm())
    await screen.findByRole('alert'); expect(localStorage.getItem(key)).toBeNull(); expect(api.rollforwardPersonalResearchContext).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('PRIVATE_RESEARCH_MATERIAL_42')).not.toBeInTheDocument(); expect(screen.getByRole('alert')).not.toHaveTextContent('PRIVATE_RAW')
  })
  it.each(['top-level', 'runtime-client-receipt'] as const)('retains identity for a %s accepted response even with a 4xx status', async shape => {
    const api = apiMock(); api.rollforwardPersonalResearchContext.mockRejectedValueOnce(shape === 'top-level'
      ? Object.assign(failure(409), { accepted: true }) : new WorkbenchRequestError('PRIVATE_RAW', 409, 'source_stale', { accepted: true }))
    await ready(api); fireEvent.click(confirm())
    await screen.findByRole('alert'); expect(localStorage.getItem(key)).not.toBeNull()
  })
  it('preserves a later local identity mismatch and stops without another request', async () => {
    const api = apiMock(), held = deferred<ResearchSessionContextReceipt>()
    api.rollforwardPersonalResearchContext.mockImplementationOnce(() => held.promise)
    await ready(api); fireEvent.click(confirm())
    await waitFor(() => expect(api.rollforwardPersonalResearchContext).toHaveBeenCalledTimes(1))
    const original = api.rollforwardPersonalResearchContext.mock.calls[0][0]
    localStorage.setItem(key, JSON.stringify({ ...pending(), request_id: 'f'.repeat(32) }))
    await act(async () => { held.resolve(receipt(original.request_id)); await held.promise })
    await screen.findByRole('alert'); expect(screen.getByRole('alert')).toHaveTextContent('编号已变化')
    expect(parseResearchContextPending(localStorage.getItem(key)!).request_id).toBe('f'.repeat(32)); expect(api.rollforwardPersonalResearchContext).toHaveBeenCalledTimes(1)
  })
})

describe('context identity and lifecycle fencing', () => {
  it.each(['namespace', 'session', 'actor', 'workspace', 'api'] as const)('aborts and fences a private preview after %s changes', async field => {
    const api = apiMock(), held = deferred<ResearchSessionContextPreview>()
    api.getPersonalResearchSessionContext.mockImplementationOnce(() => held.promise)
    const mounted = render(<ResearchContext api={api} namespace={namespace} target={target} connected onClose={vi.fn()} />)
    await waitFor(() => expect(api.getPersonalResearchSessionContext).toHaveBeenCalledTimes(1))
    const signal = api.getPersonalResearchSessionContext.mock.calls[0][2]
    const nextTarget = { ...target, ...(field === 'session' ? { sessionId: 'other-session' } : field === 'actor' ? { actorId: 'main' } : field === 'workspace' ? { workspaceKey: 'f'.repeat(64) } : {}) }
    const nextApi = field === 'api' ? apiMock(preview({ status: 'blocked' })) : api
    api.getPersonalResearchSessionContext.mockResolvedValue(preview({ status: 'blocked', actor_id: nextTarget.actorId, session_id: nextTarget.sessionId, workspace_key: nextTarget.workspaceKey }))
    mounted.rerender(<ResearchContext api={nextApi} namespace={field === 'namespace' ? 'context-B' : namespace} target={nextTarget} connected onClose={vi.fn()} />)
    expect(signal?.aborted).toBe(true)
    await act(async () => { held.resolve(preview()); await held.promise })
    await waitFor(() => expect(screen.queryByText('PRIVATE_RESEARCH_MATERIAL_42')).not.toBeInTheDocument())
    expect(api.rollforwardPersonalResearchContext).not.toHaveBeenCalled()
  })
  it('hides private material on disconnect and cancels a stale confirmation', async () => {
    const state = await ready(); confirm()
    state.rerender(<ResearchContext api={state.api} namespace={namespace} target={target} connected={false} onClose={state.onClose} />)
    expect(screen.queryByText('PRIVATE_RESEARCH_MATERIAL_42')).not.toBeInTheDocument(); expect(screen.queryByRole('button', { name: '确认更新' })).not.toBeInTheDocument()
    expect(screen.getByText('连接已断开')).toBeInTheDocument(); expect(state.api.rollforwardPersonalResearchContext).not.toHaveBeenCalled()
  })
  it('does not read or mutate a readonly/unconfirmed session', async () => {
    const api = apiMock()
    render(<ResearchContext api={api} namespace={namespace} target={target} connected writable={false} onClose={vi.fn()} />)
    expect(screen.getByText('当前对话暂不可更新')).toBeInTheDocument(); expect(api.getPersonalResearchSessionContext).not.toHaveBeenCalled(); expect(api.rollforwardPersonalResearchContext).not.toHaveBeenCalled()
  })
  it('rejects a malformed or cross-scope opaque slot without leaking it into a request', async () => {
    localStorage.setItem(key, JSON.stringify({ ...pending(), actor_id: 'main' }))
    const api = apiMock()
    render(<ResearchContext api={api} namespace={namespace} target={target} connected onClose={vi.fn()} />)
    await screen.findByRole('alert')
    expect(api.getPersonalResearchContextRequest).not.toHaveBeenCalled(); expect(api.getPersonalResearchSessionContext).not.toHaveBeenCalled(); expect(api.rollforwardPersonalResearchContext).not.toHaveBeenCalled()
    expect(localStorage.getItem(key)).not.toBeNull()
  })
  it('fences a late successful POST across Session changes and leaves the old receipt recoverable', async () => {
    const api = apiMock(), held = deferred<ResearchSessionContextReceipt>()
    api.rollforwardPersonalResearchContext.mockImplementationOnce(() => held.promise)
    const mounted = await ready(api); fireEvent.click(confirm())
    await waitFor(() => expect(api.rollforwardPersonalResearchContext).toHaveBeenCalledTimes(1))
    const original = api.rollforwardPersonalResearchContext.mock.calls[0][0]
    const nextTarget = { ...target, sessionId: 'other-session' }
    api.getPersonalResearchSessionContext.mockResolvedValueOnce(preview({ session_id: nextTarget.sessionId, status: 'blocked' }))
    mounted.rerender(<ResearchContext api={api} namespace={namespace} target={nextTarget} connected onClose={mounted.onClose} />)
    await act(async () => { held.resolve(receipt(original.request_id)); await held.promise })
    expect(localStorage.getItem(key)).not.toBeNull(); expect(localStorage.getItem(researchContextPendingKey(namespace, nextTarget))).toBeNull()
    expect(screen.queryByText('已更新')).not.toBeInTheDocument(); expect(api.rollforwardPersonalResearchContext).toHaveBeenCalledTimes(1)
  })
  it('keeps the original slot after unmount and uses Escape to close only an idle dialog', async () => {
    const mounted = await ready()
    fireEvent.keyDown(screen.getByRole('dialog', { name: '对话依据' }), { key: 'Escape' })
    expect(mounted.onClose).toHaveBeenCalledTimes(1)
    confirm(); fireEvent.keyDown(screen.getByRole('dialog', { name: '对话依据' }), { key: 'Escape' })
    expect(screen.queryByRole('button', { name: '确认更新' })).not.toBeInTheDocument(); expect(mounted.onClose).toHaveBeenCalledTimes(1)
  })
  it('integrates a persistent toolbar entry with an explicit actual Session, independent of main notebook selection', async () => {
    const api = Object.assign(apiMock(), {
      getPersonalResearchContext: vi.fn().mockResolvedValue({ actor_id: 'main', workspace_key: workspace, server_now: '2026-11-06T05:00:00Z' }),
      listPersonalResearch: vi.fn().mockResolvedValue({ notebooks: [], next_after: null }),
      getPersonalResearch: vi.fn(), executePersonalResearch: vi.fn(), getPersonalResearchSource: vi.fn(), getPersonalResearchHistory: vi.fn(),
    } satisfies ResearchApi)
    const mounted = render(<ResearchWorkspace api={api} namespace={namespace} connected contextTarget={null} onClose={vi.fn()} />)
    await waitFor(() => expect(screen.getByRole('button', { name: '对话依据' })).toBeDisabled())
    expect(api.getPersonalResearchSessionContext).not.toHaveBeenCalled()
    mounted.rerender(<ResearchWorkspace api={api} namespace={namespace} connected contextTarget={{ sessionId: session, actorId: actor }} onClose={vi.fn()} />)
    await waitFor(() => expect(screen.getByRole('button', { name: '对话依据' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '对话依据' }))
    const dialog = await screen.findByRole('dialog', { name: '对话依据' })
    await within(dialog).findByRole('heading', { name: '原始研究对话' })
    expect(api.getPersonalResearchSessionContext).toHaveBeenCalledWith(session, actor, expect.any(AbortSignal)); expect(api.rollforwardPersonalResearchContext).not.toHaveBeenCalled()
  })
})
