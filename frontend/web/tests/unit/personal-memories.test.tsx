import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react'
import { createHash, webcrypto } from 'node:crypto'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { WorkbenchRuntimeClient, type PersonalAgent } from '@naumi/shared/api/WorkbenchRuntimeClient'
import type { PersonalMemory, PersonalMemoryContext, PersonalMemoryGrant, PersonalMemoryMutationResult, PersonalMemoryPayload, PersonalMemoryPrepare, PersonalMemoryRequest } from '@naumi/shared/api/personalMemories'
import { MemoryWorkspace } from '../../../web2/src/personal/memories/MemoryWorkspace'
import { memoryFingerprint, memoryPendingKey, parseMemoryPending, usePersonalMemories, validateMemory, type PersonalMemoriesApi } from '../../../web2/src/personal/memories/usePersonalMemories'
import { requestArtifactNavigation } from '../../../web2/src/personal/library/navigation'

const id = `pm_${'a'.repeat(32)}`, oldId = `pm_${'b'.repeat(32)}`, actorId = `pa_${'c'.repeat(32)}`, sha = 'd'.repeat(64), owner = 'e'.repeat(64), workspace = 'f'.repeat(64)
const source = (authority: 'user' | 'assistant' | 'external' | 'unknown' = 'user', excerpt = '回答先给结论') => ({ kind: 'manual' as const, authority, locator: { original: 'actual-fixture-source' }, excerpt, excerpt_sha256: sha, artifact_refs: [], scope_ceiling: 'personal', captured_at: '2026-11-04T08:00:00Z' })
const memory = (changes: Partial<PersonalMemory> = {}): PersonalMemory => ({ memory_id: id, actor_id: 'main', owner_key: owner, scope: { kind: 'personal', workspace_key: '', item_id: '' }, topic: '回答方式', topic_key: '回答方式', category: 'preference', revision: 1, state: 'active', content: '回答先给结论', content_sha256: sha, source: source(), source_sha256: sha, confirmation: { receipt_id: 'human-confirmed' }, ref: { memory_id: id, revision: 1, content_sha256: sha, grant_id: '', grant_revision: 0 }, active_ref: null, access: 'owned', source_available: true, created_at: '2026-11-04T08:00:00Z', updated_at: '2026-11-04T08:00:00Z', ...changes })
const context = (actor = 'main'): PersonalMemoryContext => ({ actor_id: actor, owner_key: owner, workspace_key: workspace, index: { semantic_available: false, status: 'fixture-lexical' } })
const agent: PersonalAgent = { id: actorId, name: 'Echo', version: 3, status: 'active', kind: 'specialist', description: '', instructions: '按要求处理', model_tier: 'capable', tool_names: [], member_ids: [], created_at: '', updated_at: '' }
const grant = (changes: Partial<PersonalMemoryGrant> = {}): PersonalMemoryGrant => ({ grant_id: `pmg_${'1'.repeat(32)}`, memory_id: id, memory_revision: 1, content_sha256: sha, recipient_actor_id: actorId, recipient_version: 3, revision: 1, state: 'active', source_refs: [], created_at: '', updated_at: '', ...changes })
function apiMock(initial = memory()) {
  let current = initial, sharing: PersonalMemoryGrant[] = []
  const requests = new Map<string, { prepared: PersonalMemoryPrepare; meta: PersonalMemoryRequest }>()
  const api = {
    personalMemoryContext: vi.fn(async (actor = 'main') => context(actor)),
    personalMemories: vi.fn(async () => ({ memories: [current], limit: 30, offset: 0, next_offset: null })),
    personalMemory: vi.fn(async (target: string) => target === oldId ? memory({ memory_id: oldId, topic: '旧记忆', content: '旧的回答方式', ref: { memory_id: oldId, revision: 1, content_sha256: sha, grant_id: '', grant_revision: 0 } }) : current),
    searchPersonalMemories: vi.fn(async () => ({ memories: [current], diagnostics: { lexical: true, semantic_available: false } })),
    personalMemoryHistory: vi.fn(async () => ({ history: [{ memory_id: current.memory_id, revision: 1, state: 'superseded', content_sha256: sha }] })),
    personalMemoryGrants: vi.fn(async () => ({ grants: sharing })),
    exportPersonalMemories: vi.fn(async () => ({ version: 1, memories: [current] })),
    legacyPersonalMemories: vi.fn(async () => ({ memories: [{ legacy_id: 'legacy-actual-id', topic: '旧偏好', category: 'preference' as const, content: '旧记录尚未确认', content_sha256: sha, source_kind: 'legacy' as const }], limit: 30, offset: 0, next_offset: null })),
    personalItems: vi.fn(async () => ({ items: [], limit: 100, offset: 0, next_offset: null })),
    preparePersonalMemoryRequest: vi.fn(async (body: PersonalMemoryPrepare) => {
      const meta: PersonalMemoryRequest = { request_id: body.request_id, actor_id: body.actor_id, workspace_key: workspace, operation: body.operation, fingerprint: await memoryFingerprint(body.actor_id, workspace, body.operation, body.payload), state: 'prepared', result: null }
      requests.set(body.request_id, { prepared: body, meta }); return meta
    }),
    personalMemoryRequest: vi.fn(async (requestId: string) => {
      const value = requests.get(requestId); if (!value) throw new Error('404 原请求未读回')
      return { ...value.meta }
    }),
    discardPersonalMemoryRequest: vi.fn(async (requestId: string, _actor: string, fingerprint: string): Promise<PersonalMemoryRequest> => {
      const value = requests.get(requestId)
      if (!value || value.meta.fingerprint !== fingerprint) throw new Error('409 原请求身份不一致')
      if (value.meta.state === 'prepared') value.meta = { ...value.meta, state: 'erased', result: null }
      return { ...value.meta }
    }),
    applyPersonalMemoryRequest: vi.fn(async (requestId: string, _actor: string, fingerprint: string): Promise<PersonalMemoryMutationResult> => {
      const value = requests.get(requestId)!
      if (!value || value.meta.fingerprint !== fingerprint) throw new Error('409 原请求身份不一致')
      const p = value.prepared.payload, operation = value.prepared.operation
      let result: PersonalMemoryMutationResult
      if (operation === 'share') { result = grant(); sharing = [result] }
      else if (operation === 'revoke') { result = grant({ state: 'revoked', revision: 2 }); sharing = [result] }
      else {
        const revision = operation === 'create' || operation === 'import_legacy' ? 1 : current.revision + 1
        const nextSource = operation === 'import_legacy' ? { ...source('unknown', '旧记录尚未确认'), kind: 'legacy' as const }
          : operation === 'create' || operation === 'revise' ? source('user', p.content) : current.source
        current = memory({ ...current, actor_id: value.prepared.actor_id, topic: p.topic || current.topic, content: p.content || (operation === 'import_legacy' ? '旧记录尚未确认' : current.content), revision, source: nextSource, ref: { memory_id: current.memory_id, revision, content_sha256: sha, grant_id: '', grant_revision: 0 }, active_ref: null,
          ...(operation === 'create' || operation === 'import_legacy' ? { scope: { kind: p.scope || 'workspace', workspace_key: p.scope === 'personal' ? '' : workspace, item_id: p.item_id || '' } } : {}),
          ...(operation === 'confirm' ? { state: 'active', confirmation: { receipt_id: 'human-adopted' } } : {}),
          ...(operation === 'import_legacy' ? { state: 'proposed', confirmation: null } : {}),
          ...(operation === 'delete' ? { state: 'deleted', content: null, source: null, source_available: false, confirmation: null } : {}),
          ...(operation === 'reject' ? { state: 'rejected' } : {}),
        })
        result = current
      }
      value.meta = { ...value.meta, state: 'applied', result }
      return result
    }),
  } satisfies PersonalMemoriesApi
  return { api, requests, getCurrent: () => current, setCurrent(value: PersonalMemory) { current = value } }
}
const hook = (api: PersonalMemoriesApi, actor = 'main', namespace = 'workspace-A') => renderHook(() => usePersonalMemories({ api, namespace, actorId: actor, scope: 'all', itemId: '', connected: true }))
function renderWorkspace(fixture = apiMock(), initialActorId = 'main') {
  return { ...fixture, ...render(<MemoryWorkspace api={fixture.api} namespace="workspace-A" initialActorId={initialActorId} agents={[agent]} connected onClose={vi.fn()} />) }
}
beforeEach(() => { vi.stubGlobal('crypto', webcrypto); localStorage.clear(); history.replaceState({}, '', '/web2'); })
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('personal memory opaque request recovery', () => {
  it('matches the recursively sorted UTF-8 server digest without storing text', async () => {
    const payload = { request_id: '1'.repeat(32), topic: '中文偏好', content: '先给结论', scope: 'personal' as const }
    const exact = `{"actor_id":"main","operation":"create","payload":{"content":"先给结论","request_id":"${'1'.repeat(32)}","scope":"personal","topic":"中文偏好"},"workspace_key":"${workspace}"}`
    expect(await memoryFingerprint('main', workspace, 'create', payload)).toBe(createHash('sha256').update(exact, 'utf8').digest('hex'))
    expect(() => parseMemoryPending(JSON.stringify({ version: 1, request_id: '1'.repeat(32), owner_key: owner, workspace_key: workspace, actor_id: 'main', operation: 'create', fingerprint: sha, payload: { content: 'private' } }))).toThrow('身份不完整')
  })
  it('keeps only the original opaque identity through apply 503, empty list and remount', async () => {
    const f = apiMock(); f.api.personalMemories.mockResolvedValue({ memories: [], limit: 30, offset: 0, next_offset: null }); f.api.applyPersonalMemoryRequest.mockRejectedValueOnce(new Error('503 UNKNOWN'))
    const first = hook(f.api); await waitFor(() => expect(first.result.current.context).not.toBeNull())
    await act(async () => { await first.result.current.submit('create', { topic: 'PRIVATE_TOPIC', content: 'PRIVATE_BODY', category: 'preference', scope: 'personal' }) })
    const saved = localStorage.getItem(memoryPendingKey('workspace-A', context()))!
    expect(saved).not.toContain('PRIVATE'); expect(saved).not.toContain('payload'); expect(saved).not.toContain('content')
    const original = JSON.parse(saved)
    await act(async () => { await first.result.current.submit('create', { topic: 'another', content: 'other', scope: 'workspace' }) })
    expect(f.api.preparePersonalMemoryRequest).toHaveBeenCalledTimes(1)
    first.unmount()
    const reopened = hook(f.api); await waitFor(() => expect(reopened.result.current.pending?.request_id).toBe(original.request_id))
    await act(async () => { await reopened.result.current.retry() })
    expect(f.api.preparePersonalMemoryRequest).toHaveBeenCalledTimes(1)
    expect(f.api.applyPersonalMemoryRequest).toHaveBeenLastCalledWith(original.request_id, 'main', original.fingerprint)
    expect(reopened.result.current.pending).toBeNull(); expect(localStorage.length).toBe(0)
  })
  it('recovers a lost prepare response by GET original ID and never re-POSTs plaintext', async () => {
    const f = apiMock(), prepare = f.api.preparePersonalMemoryRequest.getMockImplementation()!
    f.api.preparePersonalMemoryRequest.mockImplementationOnce(async body => { await prepare(body); throw new Error('response lost') })
    const first = hook(f.api); await waitFor(() => expect(first.result.current.context).not.toBeNull())
    await act(async () => { await first.result.current.submit('create', { topic: '目录', content: '不可持久缓存正文', scope: 'workspace' }) })
    expect(first.result.current.pending).not.toBeNull(); expect(f.api.applyPersonalMemoryRequest).not.toHaveBeenCalled()
    first.unmount(); const next = hook(f.api)
    await waitFor(() => expect(next.result.current.pending).not.toBeNull())
    await act(async () => { await next.result.current.retry() })
    expect(f.api.preparePersonalMemoryRequest).toHaveBeenCalledTimes(1); expect(f.api.applyPersonalMemoryRequest).toHaveBeenCalledTimes(1)
    expect(next.result.current.pending).toBeNull()
  })
  it('does not interpret a missing prepared request as permission to recreate it', async () => {
    const f = apiMock(); f.api.preparePersonalMemoryRequest.mockRejectedValue(new Error('transport unavailable'))
    const state = hook(f.api); await waitFor(() => expect(state.result.current.context).not.toBeNull())
    await act(async () => { await state.result.current.submit('create', { topic: '待核对', content: '原正文', scope: 'personal' }) })
    const original = state.result.current.pending
    await act(async () => { await state.result.current.retry() })
    expect(state.result.current.pending).toEqual(original); expect(f.api.preparePersonalMemoryRequest).toHaveBeenCalledTimes(1); expect(f.api.applyPersonalMemoryRequest).not.toHaveBeenCalled()
    await act(async () => { await state.result.current.discard() })
    expect(state.result.current.pending).toEqual(original); expect(f.api.discardPersonalMemoryRequest).not.toHaveBeenCalled()
  })
  it('cleans only a verified prepared draft and confirms the durable erased receipt', async () => {
    const f = apiMock(); f.api.applyPersonalMemoryRequest.mockRejectedValueOnce(new Error('503 UNKNOWN'))
    const state = hook(f.api); await waitFor(() => expect(state.result.current.context).not.toBeNull())
    await act(async () => { await state.result.current.submit('create', { topic: '待清理', content: 'DRAFT_PRIVATE_TEXT', scope: 'personal' }) })
    const original = state.result.current.pending!
    await act(async () => { expect(await state.result.current.discard()).toEqual({ result: null, state: 'erased' }) })
    expect(f.api.discardPersonalMemoryRequest).toHaveBeenCalledWith(original.request_id, 'main', original.fingerprint)
    expect(f.requests.get(original.request_id)?.meta.state).toBe('erased'); expect(state.result.current.pending).toBeNull(); expect(localStorage.length).toBe(0)
    expect(state.result.current.notice).toBe('未提交的原记忆草稿已清理'); expect(f.api.applyPersonalMemoryRequest).toHaveBeenCalledTimes(1)
  })
  it('retains the original token after a lost discard response and recovers erased without apply', async () => {
    const f = apiMock(), discard = f.api.discardPersonalMemoryRequest.getMockImplementation()!
    f.api.applyPersonalMemoryRequest.mockRejectedValueOnce(new Error('503 UNKNOWN'))
    f.api.discardPersonalMemoryRequest.mockImplementationOnce(async (...args) => { await discard(...args); throw new Error('503 response lost') })
    const state = hook(f.api); await waitFor(() => expect(state.result.current.context).not.toBeNull())
    await act(async () => { await state.result.current.submit('create', { topic: '待清理', content: '原草稿', scope: 'personal' }) })
    const original = state.result.current.pending!
    await act(async () => { expect(await state.result.current.discard()).toBeNull() })
    expect(state.result.current.pending).toEqual(original); expect(f.requests.get(original.request_id)?.meta.state).toBe('erased')
    await act(async () => { await state.result.current.retry() })
    expect(state.result.current.pending).toBeNull(); expect(f.api.applyPersonalMemoryRequest).toHaveBeenCalledTimes(1); expect(state.result.current.notice).toContain('不会再次执行')
  })
  it('preserves the opaque identity if discard metadata does not match', async () => {
    const f = apiMock(); f.api.applyPersonalMemoryRequest.mockRejectedValueOnce(new Error('503 UNKNOWN'))
    const state = hook(f.api); await waitFor(() => expect(state.result.current.context).not.toBeNull())
    await act(async () => { await state.result.current.submit('create', { topic: '待清理', content: '原草稿', scope: 'personal' }) })
    const original = state.result.current.pending!, discard = f.api.discardPersonalMemoryRequest.getMockImplementation()!
    f.api.discardPersonalMemoryRequest.mockImplementationOnce(async (...args) => ({ ...await discard(...args), fingerprint: '9'.repeat(64) }))
    await act(async () => { expect(await state.result.current.discard()).toBeNull() })
    expect(state.result.current.pending).toEqual(original); expect(state.result.current.error).toContain('不一致'); expect(localStorage.length).toBe(1)
  })
  it('recovers an applied deletion tombstone without claiming the memory still exists', async () => {
    const f = apiMock(), apply = f.api.applyPersonalMemoryRequest.getMockImplementation()!
    f.api.applyPersonalMemoryRequest.mockImplementationOnce(async (...args) => { await apply(...args); throw new Error('503 response lost') })
    const state = hook(f.api); await waitFor(() => expect(state.result.current.context).not.toBeNull())
    await act(async () => { await state.result.current.submit('delete', { memory_id: id, expected_revision: 1 }) })
    expect(state.result.current.pending).not.toBeNull()
    await act(async () => { expect((await state.result.current.discard())?.result).toMatchObject({ state: 'deleted', content: null, source: null }) })
    expect(state.result.current.pending).toBeNull(); expect(state.result.current.notice).toContain('本次清理未撤销已提交的操作'); expect(state.result.current.notice).not.toContain('记忆未被删除')
  })
  it('blocks metadata fingerprint mismatch and never applies the wrong request', async () => {
    const f = apiMock(), prepare = f.api.preparePersonalMemoryRequest.getMockImplementation()!
    f.api.preparePersonalMemoryRequest.mockImplementationOnce(async body => ({ ...await prepare(body), fingerprint: '9'.repeat(64) }))
    const state = hook(f.api); await waitFor(() => expect(state.result.current.context).not.toBeNull())
    await act(async () => { await state.result.current.submit('create', { topic: '主题', content: '内容', scope: 'personal' }) })
    expect(state.result.current.error).toContain('不一致'); expect(state.result.current.pending).not.toBeNull(); expect(f.api.applyPersonalMemoryRequest).not.toHaveBeenCalled()
  })
  it('separates actor namespaces and discards delayed private GET results', async () => {
    const f = apiMock(); let release!: (value: Awaited<ReturnType<typeof f.api.personalMemories>>) => void
    f.api.personalMemories.mockImplementationOnce(() => new Promise(resolve => { release = resolve }))
    const nextMemory = memory({ actor_id: actorId, content: 'Echo私有记忆' }); f.api.personalMemories.mockResolvedValue({ memories: [nextMemory], limit: 30, offset: 0, next_offset: null })
    const state = renderHook(({ actor }) => usePersonalMemories({ api: f.api, namespace: 'workspace-A', actorId: actor, scope: 'all', itemId: '', connected: true }), { initialProps: { actor: 'main' } })
    await waitFor(() => expect(release).toBeTypeOf('function'))
    state.rerender({ actor: actorId }); await waitFor(() => expect(state.result.current.memories[0]?.content).toBe('Echo私有记忆'))
    await act(async () => { release({ memories: [memory({ content: 'MAIN_PRIVATE_MUST_NOT_APPEAR' })], limit: 30, offset: 0, next_offset: null }) })
    expect(state.result.current.memories[0]?.content).toBe('Echo私有记忆')
    expect(memoryPendingKey('workspace-A', context())).not.toBe(memoryPendingKey('workspace-A', context(actorId)))
  })
  it('lets a failed context be explicitly refreshed and rejects stale current-ref identity', async () => {
    const f = apiMock(); f.api.personalMemoryContext.mockRejectedValueOnce(new Error('503 context'))
    const state = hook(f.api); await waitFor(() => expect(state.result.current.error).toContain('503'))
    act(() => state.result.current.refresh()); await waitFor(() => expect(state.result.current.context).not.toBeNull())
    expect(() => validateMemory(memory({ ref: { memory_id: oldId, revision: 1, content_sha256: sha, grant_id: '', grant_revision: 0 } }))).toThrow('修订不一致')
    expect(() => validateMemory(memory({ state: 'deleted' }))).toThrow('已删除记忆仍含正文')
    expect(() => validateMemory(memory(), actorId)).toThrow('当前上下文不一致')
    expect(() => validateMemory(memory({ access: 'shared' }), actorId)).toThrow('分享凭据尚未确认')
  })
})

describe('personal memory operative GUI', () => {
  it('requires confirmation to clear a prepared draft and never calls memory deletion', async () => {
    const fixture = apiMock(); fixture.api.applyPersonalMemoryRequest.mockRejectedValueOnce(new Error('503 UNKNOWN'))
    const first = hook(fixture.api); await waitFor(() => expect(first.result.current.context).not.toBeNull())
    await act(async () => { await first.result.current.submit('create', { topic: '草稿', content: 'DRAFT_BODY_NOT_IN_STORAGE', scope: 'personal' }) })
    first.unmount(); const f = renderWorkspace(fixture)
    fireEvent.click(await screen.findByRole('button', { name: '清理草稿', exact: true }))
    expect(f.api.discardPersonalMemoryRequest).not.toHaveBeenCalled()
    const dialog = screen.getByRole('alertdialog', { name: '清理原记忆草稿？' })
    fireEvent.click(within(dialog).getByRole('button', { name: '确认清理草稿' }))
    await screen.findByText('未提交的原记忆草稿已清理')
    expect(f.api.preparePersonalMemoryRequest).toHaveBeenCalledTimes(1); expect(f.api.preparePersonalMemoryRequest.mock.calls[0][0].operation).toBe('create')
    expect(f.api.applyPersonalMemoryRequest).toHaveBeenCalledTimes(1); expect(localStorage.length).toBe(0)
  })
  it('restores an already-applied result when apply wins the discard race instead of claiming cancellation', async () => {
    const fixture = apiMock(), apply = fixture.api.applyPersonalMemoryRequest.getMockImplementation()!, discard = fixture.api.discardPersonalMemoryRequest.getMockImplementation()!
    fixture.api.applyPersonalMemoryRequest.mockRejectedValueOnce(new Error('503 UNKNOWN'))
    const first = hook(fixture.api); await waitFor(() => expect(first.result.current.context).not.toBeNull())
    await act(async () => { await first.result.current.submit('create', { topic: '已提交主题', content: '已经提交的真实结果', scope: 'personal' }) })
    const original = first.result.current.pending!; first.unmount()
    fixture.api.discardPersonalMemoryRequest.mockImplementationOnce(async (...args) => { await apply(...args); return discard(...args) })
    renderWorkspace(fixture); fireEvent.click(await screen.findByRole('button', { name: '清理草稿', exact: true })); fireEvent.click(screen.getByRole('button', { name: '确认清理草稿' }))
    await screen.findByText('原请求已提交，已恢复当前结果；本次清理未撤销已提交的操作')
    await screen.findByRole('heading', { name: '已提交主题' }); expect(fixture.requests.get(original.request_id)?.meta.state).toBe('applied')
    expect(screen.queryByText('未提交的原记忆草稿已清理')).not.toBeInTheDocument(); expect(fixture.getCurrent().state).toBe('active'); expect(localStorage.length).toBe(0)
  })
  it('does not apply a delayed preparation or rewrite the URL after leaving its Agent', async () => {
    const f = renderWorkspace(), prepare = f.api.preparePersonalMemoryRequest.getMockImplementation()!
    let release!: () => void
    f.api.preparePersonalMemoryRequest.mockImplementationOnce(async body => { const meta = await prepare(body); await new Promise<void>(resolve => { release = resolve }); return meta })
    await waitFor(() => expect(screen.getByRole('button', { name: '新建记忆' })).not.toBeDisabled()); fireEvent.click(screen.getByRole('button', { name: '新建记忆' }))
    fireEvent.change(screen.getByLabelText('记忆主题'), { target: { value: 'DELAYED_PRIVATE_TITLE' } }); fireEvent.change(screen.getByLabelText('记忆正文'), { target: { value: 'PRIVATE_BODY' } }); fireEvent.click(screen.getByRole('button', { name: '保存记忆' }))
    await waitFor(() => expect(release).toBeTypeOf('function'))
    f.unmount(); history.replaceState({}, '', `/web2?memories=1&memory_actor=${actorId}`)
    await act(async () => { release() })
    expect(f.api.applyPersonalMemoryRequest).not.toHaveBeenCalled(); expect(new URLSearchParams(location.search).get('memory_actor')).toBe(actorId)
    expect(localStorage.getItem(memoryPendingKey('workspace-A', context()))).not.toContain('PRIVATE')
  })
  it('does not navigate a completed delayed apply into a later Agent route', async () => {
    const f = renderWorkspace(), apply = f.api.applyPersonalMemoryRequest.getMockImplementation()!
    let release!: () => void
    f.api.applyPersonalMemoryRequest.mockImplementationOnce(async (...args) => { await new Promise<void>(resolve => { release = resolve }); return apply(...args) })
    await waitFor(() => expect(screen.getByRole('button', { name: '新建记忆' })).not.toBeDisabled()); fireEvent.click(screen.getByRole('button', { name: '新建记忆' }))
    fireEvent.change(screen.getByLabelText('记忆主题'), { target: { value: 'DELAYED_APPLY' } }); fireEvent.change(screen.getByLabelText('记忆正文'), { target: { value: 'PRIVATE_BODY' } }); fireEvent.click(screen.getByRole('button', { name: '保存记忆' }))
    await waitFor(() => expect(release).toBeTypeOf('function')); f.unmount(); history.replaceState({}, '', `/web2?memories=1&memory_actor=${actorId}`)
    await act(async () => { release() })
    expect(f.getCurrent().topic).toBe('DELAYED_APPLY'); expect(new URLSearchParams(location.search).get('memory_actor')).toBe(actorId); expect(new URLSearchParams(location.search).has('memory_id')).toBe(false)
  })
  it('suppresses an export response captured before a refresh rather than downloading stale private text', async () => {
    const f = renderWorkspace(); let release!: (value: { version: number; memories: PersonalMemory[] }) => void
    f.api.exportPersonalMemories.mockImplementationOnce(() => new Promise(resolve => { release = resolve }))
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    await screen.findByRole('button', { name: /回答方式/ }); fireEvent.click(screen.getByRole('button', { name: '导出' })); await waitFor(() => expect(release).toBeTypeOf('function'))
    fireEvent.click(screen.getByRole('button', { name: '刷新记忆' })); await act(async () => { release({ version: 1, memories: [memory({ content: 'STALE_PRIVATE_EXPORT' })] }) })
    await screen.findByText('记忆上下文已更新，请重新导出当前内容'); expect(click).not.toHaveBeenCalled()
  })
  it('traps modal focus and restores the trigger on Escape without submitting', async () => {
    const f = renderWorkspace(); fireEvent.click(await screen.findByRole('button', { name: /回答方式/ }))
    const trigger = await screen.findByRole('button', { name: '分享', exact: true }); trigger.focus(); fireEvent.click(trigger)
    const dialog = screen.getByRole('dialog', { name: '分享记忆' }), close = within(dialog).getByRole('button', { name: '关闭分享记忆' })
    expect(close).toHaveFocus(); const last = within(dialog).getByRole('button', { name: '取消' }); last.focus(); fireEvent.keyDown(last, { key: 'Tab' }); expect(close).toHaveFocus()
    fireEvent.keyDown(close, { key: 'Tab', shiftKey: true }); expect(last).toHaveFocus(); fireEvent.keyDown(last, { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: '分享记忆' })).not.toBeInTheDocument(); expect(trigger).toHaveFocus(); expect(f.api.preparePersonalMemoryRequest).not.toHaveBeenCalled()
  })
  it('clears private detail text on cross-tab storage invalidation and revalidates source availability', async () => {
    const f = renderWorkspace(); fireEvent.click(await screen.findByRole('button', { name: /回答方式/ })); await screen.findByRole('heading', { name: '回答方式' })
    f.setCurrent(memory({ content: null, source: null, source_available: false, error: '原来源已撤销' }))
    act(() => window.dispatchEvent(new StorageEvent('storage', { key: memoryPendingKey('workspace-A', context()) })))
    await screen.findAllByText('原来源已撤销'); expect(screen.queryByText('回答先给结论')).not.toBeInTheDocument()
  })
  it('saves manual memory in the chosen scope without a false dirty-navigation prompt', async () => {
    const f = renderWorkspace(); await waitFor(() => expect(screen.getByRole('button', { name: '新建记忆' })).not.toBeDisabled())
    fireEvent.click(screen.getByRole('button', { name: '新建记忆' })); fireEvent.change(screen.getByLabelText('记忆主题'), { target: { value: '语言偏好' } }); fireEvent.change(screen.getByLabelText('记忆正文'), { target: { value: '默认用中文回答' } })
    fireEvent.change(screen.getByLabelText('保存记忆范围'), { target: { value: 'personal' } }); fireEvent.click(screen.getByRole('button', { name: '保存记忆' }))
    await waitFor(() => expect(screen.getByRole('heading', { name: '语言偏好' })).toBeInTheDocument())
    expect(f.api.preparePersonalMemoryRequest).toHaveBeenCalledTimes(1)
    const submitted = f.api.preparePersonalMemoryRequest.mock.calls[0][0]
    expect(submitted.payload).toMatchObject({ topic: '语言偏好', content: '默认用中文回答', scope: 'personal' }); expect(submitted.payload.request_id).toBe(submitted.request_id)
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument(); expect(localStorage.length).toBe(0)
  })
  it('shows assistant provenance, waits for a human conflict decision, and sends exact active ref', async () => {
    const activeRef = { memory_id: oldId, revision: 1, content_sha256: sha, grant_id: '', grant_revision: 0 }
    const f = renderWorkspace(apiMock(memory({ state: 'proposed', confirmation: null, source: { ...source('assistant'), kind: 'message' }, active_ref: activeRef })))
    fireEvent.click(await screen.findByRole('button', { name: /回答方式/ }))
    await screen.findByRole('heading', { name: '当前记忆' }); await screen.findByText('旧的回答方式')
    expect(f.api.preparePersonalMemoryRequest).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '替换为此条' })); fireEvent.click(screen.getByRole('button', { name: '确认替换' }))
    await waitFor(() => expect(f.api.preparePersonalMemoryRequest).toHaveBeenCalledTimes(1))
    expect(f.api.preparePersonalMemoryRequest.mock.calls[0][0].payload).toMatchObject({ memory_id: id, expected_revision: 1, expected_content_sha256: sha, expected_active_ref: activeRef })
    await waitFor(() => expect(screen.getByText('人工确认')).toBeInTheDocument())
    expect(screen.getByText('Agent 建议')).toBeInTheDocument(); expect(f.getCurrent().source?.authority).toBe('assistant')
  })
  it('shares only an exact revision with an actual Agent version and revokes its original grant', async () => {
    const f = renderWorkspace(); fireEvent.click(await screen.findByRole('button', { name: /回答方式/ })); fireEvent.click(await screen.findByRole('button', { name: '分享', exact: true }))
    fireEvent.change(screen.getByLabelText('记忆接收 Agent'), { target: { value: actorId } }); fireEvent.click(screen.getByRole('button', { name: '确认分享' }))
    await waitFor(() => expect(f.api.preparePersonalMemoryRequest).toHaveBeenCalledTimes(1))
    expect(f.api.preparePersonalMemoryRequest.mock.calls[0][0]).toMatchObject({ operation: 'share', payload: { memory_id: id, expected_revision: 1, expected_content_sha256: sha, recipient_actor_id: actorId, recipient_version: 3 } })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '分享记忆' })).not.toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: '分享记录' })); fireEvent.click(await screen.findByRole('button', { name: '撤销', exact: true })); fireEvent.click(screen.getByRole('button', { name: '确认撤销' }))
    await waitFor(() => expect(f.api.preparePersonalMemoryRequest).toHaveBeenCalledTimes(2))
    expect(f.api.preparePersonalMemoryRequest.mock.calls[1][0]).toMatchObject({ operation: 'revoke', payload: { grant_id: grant().grant_id, expected_revision: 1 } })
  })
  it('keeps shared notes read-only and identifies the exact granted revision', async () => {
    const shared = memory({ access: 'shared', ref: { memory_id: id, revision: 1, content_sha256: sha, grant_id: grant().grant_id, grant_revision: 1 } })
    renderWorkspace(apiMock(shared), actorId); fireEvent.click(await screen.findByRole('button', { name: /回答方式/ }))
    await screen.findByRole('heading', { name: '回答方式' })
    expect(screen.queryByRole('button', { name: '纠正' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '分享', exact: true })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '删除', exact: true })).not.toBeInTheDocument()
    expect(screen.getAllByText('已共享给我').length).toBeGreaterThan(0)
  })
  it('corrects through CAS and clears old source/content after revocation refresh', async () => {
    const f = renderWorkspace(apiMock(memory({ content: 'OLD_PRIVATE_VALUE', source: source('user', 'OLD_PRIVATE_VALUE') })))
    fireEvent.click(await screen.findByRole('button', { name: /回答方式/ })); fireEvent.click(await screen.findByRole('button', { name: '纠正' }))
    expect(screen.getByLabelText('记忆主题')).toBeDisabled(); fireEvent.change(screen.getByLabelText('记忆正文'), { target: { value: '已纠正的新偏好' } }); fireEvent.click(screen.getByRole('button', { name: '保存纠正' }))
    await waitFor(() => expect(f.api.preparePersonalMemoryRequest).toHaveBeenCalledTimes(1))
    expect(f.api.preparePersonalMemoryRequest.mock.calls[0][0]).toMatchObject({ operation: 'revise', payload: { memory_id: id, expected_revision: 1, expected_content_sha256: sha, content: '已纠正的新偏好' } })
    await waitFor(() => expect(within(screen.getByLabelText('记忆内容')).getByText('已纠正的新偏好')).toBeInTheDocument())
    expect(screen.queryByText('OLD_PRIVATE_VALUE')).not.toBeInTheDocument()
    f.setCurrent(memory({ content: null, source: null, source_available: false, error: '原来源已撤销' }))
    fireEvent.click(screen.getByRole('button', { name: '刷新记忆' })); await screen.findAllByText('原来源已撤销')
    expect(screen.queryByText('已纠正的新偏好')).not.toBeInTheDocument()
  })
  it('deletes to a body-free tombstone and exports actual authorized server JSON', async () => {
    const f = renderWorkspace(apiMock(memory({ content: 'ERASE_THIS_VALUE', source: source('user', 'ERASE_THIS_VALUE') })))
    let exported: Blob | undefined
    class ExportURL extends URL { static createObjectURL(blob: Blob) { exported = blob; return 'blob:memory-export' } static revokeObjectURL() {} }
    vi.stubGlobal('URL', ExportURL)
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    fireEvent.click(await screen.findByRole('button', { name: /回答方式/ })); fireEvent.click(await screen.findByRole('button', { name: '删除', exact: true })); fireEvent.click(screen.getByRole('button', { name: '确认删除' }))
    await screen.findByRole('heading', { name: '已删除的记忆' }); expect(screen.queryByText('ERASE_THIS_VALUE')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '导出' })); await waitFor(() => expect(exported).toBeDefined())
    const text = await new Promise<string>(resolve => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.readAsText(exported!) })
    expect(JSON.parse(text)).toEqual({ version: 1, memories: [f.getCurrent()] }); expect(text).not.toContain('ERASE_THIS_VALUE'); expect(click).toHaveBeenCalledTimes(1)
  })
  it('reads legacy only after explicit opening and imports as an unconfirmed original source', async () => {
    const f = renderWorkspace(); await waitFor(() => expect(screen.getByRole('button', { name: '旧记忆' })).not.toBeDisabled())
    expect(f.api.legacyPersonalMemories).not.toHaveBeenCalled(); fireEvent.click(screen.getByRole('button', { name: '旧记忆' }))
    fireEvent.click(await screen.findByRole('button', { name: /旧偏好/ })); fireEvent.click(screen.getByRole('button', { name: '导入待确认' }))
    await waitFor(() => expect(f.api.preparePersonalMemoryRequest).toHaveBeenCalledTimes(1))
    expect(f.api.preparePersonalMemoryRequest.mock.calls[0][0]).toMatchObject({ operation: 'import_legacy', payload: { legacy_id: 'legacy-actual-id', expected_content_sha256: sha, scope: 'workspace' } })
    await waitFor(() => expect(f.getCurrent().state).toBe('proposed')); expect(f.getCurrent().source?.kind).toBe('legacy'); expect(f.getCurrent().source?.authority).toBe('unknown')
  })
  it('preserves an unsaved draft on navigation and enforces the exact form limits', async () => {
    renderWorkspace(); await waitFor(() => expect(screen.getByRole('button', { name: '新建记忆' })).not.toBeDisabled()); fireEvent.click(screen.getByRole('button', { name: '新建记忆' }))
    expect(screen.getByLabelText('记忆主题')).toHaveAttribute('maxlength', '120'); expect(screen.getByLabelText('记忆正文')).toHaveAttribute('maxlength', '8000')
    fireEvent.change(screen.getByLabelText('记忆正文'), { target: { value: '未保存内容' } })
    const leave = vi.fn(); act(() => requestArtifactNavigation(leave)); fireEvent.click(screen.getByRole('button', { name: '继续编辑' }))
    expect(leave).not.toHaveBeenCalled(); expect(screen.getByLabelText('记忆正文')).toHaveValue('未保存内容')
    act(() => requestArtifactNavigation(leave)); fireEvent.click(screen.getByRole('button', { name: '放弃修改' })); expect(leave).toHaveBeenCalledTimes(1)
  })
  it('offers explicit cleanup for corrupt opaque metadata without rendering its stray plaintext', async () => {
    localStorage.setItem(memoryPendingKey('workspace-A', context()), JSON.stringify({ content: 'MUST_NOT_RENDER_CORRUPT_TEXT' }))
    renderWorkspace(); await screen.findByText(/原记忆请求身份不完整/)
    expect(screen.queryByText('MUST_NOT_RENDER_CORRUPT_TEXT')).not.toBeInTheDocument(); expect(screen.getByRole('button', { name: '新建记忆' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '清理本地记录', exact: true })); fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: '清理本地记录' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '新建记忆' })).not.toBeDisabled()); expect(localStorage.length).toBe(0)
  })
  it('uses real hybrid search and service-side conflict pagination without client guessing', async () => {
    const f = renderWorkspace(); await screen.findByRole('button', { name: /回答方式/ })
    fireEvent.change(screen.getByLabelText('搜索记忆'), { target: { value: '回答习惯' } }); await waitFor(() => expect(f.api.searchPersonalMemories).toHaveBeenCalled())
    expect(f.api.searchPersonalMemories.mock.calls.at(-1)?.[0]).toMatchObject({ actor_id: 'main', scope: 'all', query: '回答习惯', limit: 10 })
    expect(screen.getByLabelText('搜索记忆')).toHaveAttribute('maxlength', '200')
    fireEvent.click(screen.getByRole('button', { name: '冲突', exact: true })); await waitFor(() => expect(f.api.personalMemories.mock.calls.at(-1)?.[0]).toMatchObject({ state: 'conflict', offset: 0 }))
  })
})

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())
describe('personal memory authenticated HTTP client', () => {
  it('uses strict paths, exact request bodies, no-store GET and trusted token headers', async () => {
    const base = 'http://memory.test/api/v1', seen: { method: string; path: string; body?: unknown; cache?: string; auth: string | null }[] = []
    const token = { request_id: '2'.repeat(32), actor_id: 'main', workspace_key: workspace, operation: 'confirm' as const, fingerprint: sha, state: 'prepared' as const }
    server.use(http.all(`${base}/*`, async ({ request }) => {
      const url = new URL(request.url); seen.push({ method: request.method, path: url.pathname + url.search, body: request.method === 'POST' ? await request.json() : undefined, cache: request.cache, auth: request.headers.get('authorization') })
      if (url.pathname.endsWith('/context')) return HttpResponse.json(context())
      if (url.pathname.endsWith('/history')) return HttpResponse.json({ history: [] })
      if (url.pathname.endsWith('/grants')) return HttpResponse.json({ grants: [] })
      if (url.pathname.endsWith('/legacy')) return HttpResponse.json({ memories: [], limit: 30, offset: 0, next_offset: null })
      if (url.pathname.endsWith('/search')) return HttpResponse.json({ memories: [], diagnostics: {} })
      if (url.pathname.endsWith('/export')) return HttpResponse.json({ version: 1, memories: [] })
      if (url.pathname.includes('memory-requests')) return HttpResponse.json(url.pathname.endsWith('/apply') ? memory() : token)
      return HttpResponse.json(url.pathname.endsWith('/memories') ? { memories: [], limit: 30, offset: 0, next_offset: null } : memory())
    }))
    const api = new WorkbenchRuntimeClient(base, async () => 'memory-fixture-token')
    await api.personalMemoryContext(); await api.personalMemories({ actor_id: 'main', scope: 'workspace', state: 'conflict', offset: 30, limit: 30 })
    await api.personalMemory(id, actorId); await api.searchPersonalMemories({ actor_id: 'main', scope: 'personal', query: '中文偏好', limit: 10 }); await api.personalMemoryHistory(id); await api.personalMemoryGrants(id)
    await api.exportPersonalMemories({ actor_id: 'main', scope: 'all' }); await api.legacyPersonalMemories('main', 30, 0)
    const body = { request_id: token.request_id, actor_id: 'main', operation: 'confirm' as const, payload: { request_id: token.request_id, memory_id: id, expected_revision: 1, expected_content_sha256: sha, expected_active_ref: null } }
    await api.preparePersonalMemoryRequest(body); await api.personalMemoryRequest(token.request_id); await api.applyPersonalMemoryRequest(token.request_id, 'main', sha); await api.discardPersonalMemoryRequest(token.request_id, 'main', sha)
    expect(seen).toHaveLength(12); expect(seen.every(r => r.auth === 'Bearer memory-fixture-token')).toBe(true)
    expect(seen.filter(r => r.method === 'GET').every(r => r.cache === 'no-store')).toBe(true)
    expect(seen[1].path).toContain('state=conflict'); expect(seen[1].path).toContain('offset=30')
    expect(seen[8].body).toEqual(body); expect(seen[10].body).toEqual({ expected_fingerprint: sha })
    expect(seen[11].path).toBe(`/api/v1/personal/memory-requests/${token.request_id}/discard?actor_id=main`); expect(seen[11].body).toEqual({ expected_fingerprint: sha })
    expect(JSON.stringify(seen[10].body)).not.toContain('trusted_channel')
  })
})
