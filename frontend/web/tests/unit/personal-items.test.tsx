import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { WorkbenchRuntimeClient, type PersonalAgent } from '@naumi/shared/api/WorkbenchRuntimeClient'
import type { PersonalArtifactSnapshot } from '@naumi/shared/api/personalArtifacts'
import type { PersonalItem, PersonalItemExecution, PersonalItemInput, PersonalItemResult } from '@naumi/shared/api/personalItems'
import { PersonalItems, knownTerminal } from '../../../web2/src/personal/items/PersonalItems'
import { itemPendingKey, usePersonalItems, type PersonalItemsApi } from '../../../web2/src/personal/items/usePersonalItems'
import { requestArtifactNavigation } from '../../../web2/src/personal/library/navigation'

const id = `pi_${'a'.repeat(32)}`, occurrenceId = `pio_${'b'.repeat(32)}`, actor = `pa_${'c'.repeat(32)}`, artifactId = `art_${'d'.repeat(32)}`, sha = 'e'.repeat(64)
const ref = { artifact_id: artifactId, version: 2, sha256: sha, grant_id: 'grant_exact', grant_revision: 4 }
const agent: PersonalAgent = { id: actor, name: '我的 Agent', version: 3, status: 'active', kind: 'specialist', instructions: '处理我的事项', description: '', model_tier: 'capable', tool_names: ['personal_artifacts'], member_ids: [], created_at: '', updated_at: '' }
const body: PersonalItemInput = { request_id: '1'.repeat(32), title: '资料核验', instructions: '核对原材料并保存报告', tags: ['资料'], assignee_id: 'main', assignee_version: 0, schedule: { mode: 'now', timezone: 'Asia/Shanghai', misfire_policy: 'run_once', grace_seconds: 86400 }, input_refs: [], directories: [] }
const execution = (changes: Partial<PersonalItemExecution> = {}): PersonalItemExecution => ({ occurrence_id: occurrenceId, status: 'completed', job_id: 'job_exact', run_id: 'run_exact', execution_session_id: 'pie_exact', answer: '已核验 **825 元**。', artifacts: [ref], delivery_id: 'delivery_exact', delivery_sha256: sha, read_receipt: null, ...changes })
const item = (changes: Partial<PersonalItem> = {}): PersonalItem => ({ item_id: id, owner_key: sha, once_key: 'f'.repeat(32), revision: 1, state: 'active', definition: { title: body.title, instructions: body.instructions, tags: body.tags, assignee: { id: actor, version: 3, name: agent.name, kind: 'specialist' }, origin: { actor_id: 'main', session_id: '' }, route: {}, schedule: { ...body.schedule, due_at: '2026-11-04T04:00:00Z' }, inputs: { artifact_refs: [], directories: [] }, policy: { tool_names: ['personal_artifacts'], artifact_operations: ['get', 'read', 'publish'], model_tier: 'capable', max_turns: 12, max_cost_microusd: 2000000, timeout_seconds: 180 }, output: { destination: 'personal_library', format: 'report' } }, definition_sha256: sha, created_at: '2026-11-04T04:00:00Z', updated_at: '2026-11-04T04:00:00Z', occurrence: null, ...changes })
const result = (changes: Partial<PersonalItemResult> = {}): PersonalItemResult => ({ ...execution(), item_id: id, item_revision: 1, title: '资料核验', assignee_id: actor, assignee_name: agent.name, created_at: '2026-11-04T04:02:00Z', ...changes })
const artifact: PersonalArtifactSnapshot = { ...ref, owner_id: actor, title: '核验报告', kind: 'output', bytes: 22, mime_type: 'text/markdown', suffix: '.md', source_refs: [], evidence: {}, can_revise: true, can_share: true, created_at: '2026-11-04T04:01:00Z' }
function apiMock(value = item()) {
  return {
    personalItems: vi.fn().mockResolvedValue({ items: [value], limit: 30, offset: 0, next_offset: null }), personalItem: vi.fn().mockResolvedValue(value),
    createPersonalItem: vi.fn().mockResolvedValue(value), updatePersonalItem: vi.fn().mockResolvedValue(item({ revision: 2 })), controlPersonalItem: vi.fn().mockResolvedValue(item({ revision: 2 })),
    personalItemHistory: vi.fn().mockResolvedValue({ history: [value] }), personalItemOccurrences: vi.fn().mockResolvedValue({ occurrences: value.occurrence ? [value.occurrence] : [] }),
    personalItemResults: vi.fn().mockResolvedValue({ results: [result()], limit: 30, offset: 0, next_offset: null }), personalItemResult: vi.fn().mockResolvedValue(result()),
    readPersonalItemResult: vi.fn().mockResolvedValue({ read_receipt: { actor: 'human' } }), personalArtifacts: vi.fn().mockResolvedValue({ artifacts: [artifact] }),
    personalArtifact: vi.fn().mockResolvedValue(artifact), runs: vi.fn().mockResolvedValue({ runs: [{ id: 'run_wrong', status: 'completed', started_at: '', steps: [] }, { id: 'run_exact', status: 'completed', started_at: '', steps: [{ sequence: 1, stage: 'tool', status: 'completed', summary: '读取指定材料', detail: '精确版本 2' }] }] }),
  } satisfies PersonalItemsApi
}
function renderWorkspace(api = apiMock(), options: Partial<{ onClose: () => void; onOpenArtifact: (actor: string, ref: typeof ref) => void }> = {}) {
  return { api, ...render(<PersonalItems api={api} namespace="workspace-A" agents={[agent, { ...agent, id: `pa_${'9'.repeat(32)}`, name: '团队', kind: 'team' }, { ...agent, id: `pa_${'8'.repeat(32)}`, name: '停用 Agent', status: 'paused' }]} connected onClose={options.onClose || vi.fn()} onOpenArtifact={options.onOpenArtifact || vi.fn()} />) }
}
beforeEach(() => { localStorage.clear(); history.replaceState({}, '', '/web2'); })
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('durable personal item writes', () => {
  it('retains exact create payload and request through 503, empty list, remount and retry', async () => {
    const api = apiMock(); api.createPersonalItem.mockRejectedValueOnce(new Error('503 UNKNOWN')); api.personalItems.mockResolvedValue({ items: [], limit: 30, offset: 0, next_offset: null })
    const first = renderHook(() => usePersonalItems({ api, namespace: 'workspace-A', connected: true }))
    await act(async () => { await first.result.current.submit({ operation: 'create', body }) })
    expect(first.result.current.pending?.body).toEqual(body)
    await act(async () => { await first.result.current.submit({ operation: 'create', body: { ...body, request_id: '2'.repeat(32) } }) })
    expect(api.createPersonalItem).toHaveBeenCalledTimes(1)
    first.unmount()
    const reopened = renderHook(() => usePersonalItems({ api, namespace: 'workspace-A', connected: true }))
    await waitFor(() => expect(reopened.result.current.pending).not.toBeNull())
    await act(async () => { await reopened.result.current.retry() })
    expect(api.createPersonalItem.mock.calls[1][0]).toEqual(body)
    expect(reopened.result.current.pending).toBeNull()
  })
  it('persists original control revision and action after response loss', async () => {
    const api = apiMock(); api.controlPersonalItem.mockRejectedValueOnce(new Error('response lost'))
    const state = renderHook(() => usePersonalItems({ api, namespace: 'control-A', connected: true }))
    const request = { operation: 'control' as const, item_id: id, action: 'pause' as const, body: { expected_revision: 1, request_id: '3'.repeat(32) } }
    await act(async () => { await state.result.current.submit(request); await state.result.current.retry() })
    await act(async () => { await state.result.current.retry() })
    expect(api.controlPersonalItem.mock.calls[1]).toEqual([id, 'pause', request.body])
  })
  it('keeps a read write unknown until exact delivered result has a real receipt', async () => {
    const api = apiMock(); const state = renderHook(() => usePersonalItems({ api, namespace: 'read-A', connected: true }))
    const read = { operation: 'read' as const, occurrence_id: occurrenceId, body: { request_id: '4'.repeat(32), delivery_id: 'delivery_exact', expected_delivery_sha256: sha } }
    await act(async () => { await state.result.current.submit(read) })
    expect(state.result.current.pending).toEqual(read)
    api.personalItemResult.mockResolvedValue(result({ read_receipt: { acknowledgement_id: 'ack_real' } }))
    await act(async () => { await state.result.current.retry() })
    expect(api.readPersonalItemResult.mock.calls[1]).toEqual([occurrenceId, read.body])
    expect(state.result.current.pending).toBeNull()
  })
  it('rejects corrupted persisted identity and prevents fresh writes', async () => {
    localStorage.setItem(itemPendingKey('workspace-A'), '{bad')
    const api = apiMock(); const state = renderHook(() => usePersonalItems({ api, namespace: 'workspace-A', connected: true }))
    await waitFor(() => expect(state.result.current.identityError).not.toBe(''))
    await act(async () => { await state.result.current.submit({ operation: 'create', body }) })
    expect(api.createPersonalItem).not.toHaveBeenCalled()
    expect(localStorage.getItem(itemPendingKey('workspace-A'))).toBe('{bad')
  })
  it('blocks transport entirely when original request cannot be durably recorded', async () => {
    const api = apiMock(); const state = renderHook(() => usePersonalItems({ api, namespace: 'workspace-A', connected: true }))
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('storage full') })
    await act(async () => { await state.result.current.submit({ operation: 'create', body }) })
    expect(api.createPersonalItem).not.toHaveBeenCalled()
    expect(state.result.current.error).toContain('storage full')
  })
})

describe('personal item operative GUI', () => {
  it('matches the real title, budget and timeout limits before any write', async () => {
    const { api } = renderWorkspace()
    fireEvent.click(screen.getByRole('button', { name: '新建事项' }))
    const title = screen.getByLabelText('事项名称') as HTMLInputElement
    expect(title.maxLength).toBe(120)
    fireEvent.change(title, { target: { value: '表单限额核验' } })
    fireEvent.change(screen.getByLabelText('执行要求'), { target: { value: '核对界面限额，不提交无效参数' } })
    fireEvent.click(screen.getByText('执行限制'))
    fireEvent.click(screen.getByRole('checkbox', { name: '自定义限制' }))
    const budget = screen.getByLabelText('事项预算') as HTMLInputElement
    const timeout = screen.getByLabelText('事项超时') as HTMLInputElement
    expect(budget.max).toBe('100')
    expect(timeout.max).toBe('3600')
    fireEvent.change(budget, { target: { value: '100' } })
    fireEvent.change(timeout, { target: { value: '3600' } })
    expect(budget.checkValidity()).toBe(true)
    expect(timeout.checkValidity()).toBe(true)
    fireEvent.change(budget, { target: { value: '100.01' } })
    expect(budget.validity.rangeOverflow).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: '创建事项' }))
    expect(api.createPersonalItem).not.toHaveBeenCalled()
    fireEvent.change(budget, { target: { value: '100' } })
    fireEvent.change(timeout, { target: { value: '3601' } })
    expect(timeout.validity.rangeOverflow).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: '创建事项' }))
    expect(api.createPersonalItem).not.toHaveBeenCalled()
  })
  it('creates with main default, real Agent only, exact authorized material, timezone and DST fold', async () => {
    const { api } = renderWorkspace()
    fireEvent.click(screen.getByRole('button', { name: '新建事项' }))
    const selector = screen.getByRole('combobox', { name: '执行 Agent' })
    expect(selector).toHaveValue('main'); expect(within(selector).queryByText('团队')).not.toBeInTheDocument(); expect(within(selector).queryByText('停用 Agent')).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('事项名称'), { target: { value: '核对材料' } }); fireEvent.change(screen.getByLabelText('执行要求'), { target: { value: '读取选择的版本并核对' } })
    fireEvent.change(selector, { target: { value: actor } })
    fireEvent.click(screen.getByRole('button', { name: '指定时间' })); fireEvent.change(screen.getByLabelText('当地时间'), { target: { value: '2026-11-01T01:30' } })
    fireEvent.change(screen.getByLabelText('时区'), { target: { value: 'America/New_York' } }); fireEvent.change(screen.getByLabelText('夏令时重复小时'), { target: { value: '1' } })
    fireEvent.click(screen.getByRole('button', { name: '选择材料' }))
    await screen.findByRole('dialog', { name: '选择已授权材料' }); await screen.findByText('核验报告')
    expect(api.personalArtifacts.mock.calls.at(-1)?.[0]).toMatchObject({ actor_id: actor })
    fireEvent.click(within(screen.getByRole('dialog', { name: '选择已授权材料' })).getByRole('checkbox')); fireEvent.click(screen.getByRole('button', { name: '选择 1 项' }))
    fireEvent.click(screen.getByRole('button', { name: '创建事项' }))
    await waitFor(() => expect(api.createPersonalItem).toHaveBeenCalledTimes(1))
    expect(api.createPersonalItem.mock.calls[0][0]).toMatchObject({ title: '核对材料', assignee_id: actor, assignee_version: 3, input_refs: [ref], schedule: { mode: 'once', timezone: 'America/New_York', local_time: '2026-11-01T01:30:00', fold: 1 } })
  })
  it('clears old private materials when changing assignee', async () => {
    const { api } = renderWorkspace(); fireEvent.click(screen.getByRole('button', { name: '新建事项' }))
    fireEvent.click(screen.getByRole('button', { name: '选择材料' })); await screen.findByText('核验报告'); fireEvent.click(within(screen.getByRole('dialog', { name: '选择已授权材料' })).getByRole('checkbox')); fireEvent.click(screen.getByRole('button', { name: '选择 1 项' }))
    expect(screen.getByText(artifactId)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('执行 Agent'), { target: { value: actor } }); expect(screen.queryByText(artifactId)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '选择材料' })); await waitFor(() => expect(api.personalArtifacts.mock.calls.at(-1)?.[0]).toMatchObject({ actor_id: actor }))
  })
  it('guards unsaved navigation and only discards after explicit confirmation', async () => {
    const onClose = vi.fn(); renderWorkspace(apiMock(), { onClose }); fireEvent.click(screen.getByRole('button', { name: '新建事项' }))
    fireEvent.change(screen.getByLabelText('事项名称'), { target: { value: '未保存' } })
    fireEvent.click(screen.getByRole('button', { name: '关闭事项' }))
    expect(onClose).not.toHaveBeenCalled(); expect(screen.getByRole('alertdialog', { name: '放弃未保存事项' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '继续编辑' })); expect(screen.getByLabelText('事项名称')).toHaveValue('未保存')
    const proceed = vi.fn(); act(() => requestArtifactNavigation(proceed)); expect(proceed).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '放弃修改并离开' })); expect(proceed).toHaveBeenCalledTimes(1)
  })
  it('shows UNKNOWN without editable definition, run-now or restart', async () => {
    renderWorkspace(apiMock(item({ occurrence: execution({ status: 'unknown', answer: '', artifacts: [], delivery_id: undefined, delivery_sha256: undefined }) })))
    fireEvent.click(await screen.findByRole('button', { name: /资料核验 我的 Agent/ }))
    await screen.findByText('执行结果未知 · 保留原执行身份')
    expect(screen.queryByRole('button', { name: '再次执行' })).not.toBeInTheDocument(); expect(screen.queryByRole('button', { name: '编辑' })).not.toBeInTheDocument(); expect(screen.queryByRole('button', { name: '立即运行' })).not.toBeInTheDocument()
    expect(knownTerminal('unknown')).toBe(false)
  })
  it('requires explicit restart confirmation and sends a new now schedule with CAS', async () => {
    const { api } = renderWorkspace(apiMock(item({ state: 'closed', occurrence: execution() })))
    fireEvent.click(await screen.findByRole('button', { name: /资料核验 我的 Agent/ })); await screen.findByRole('button', { name: '再次执行' })
    fireEvent.click(screen.getByRole('button', { name: '再次执行' })); expect(api.controlPersonalItem).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '确认再次执行' })); await waitFor(() => expect(api.controlPersonalItem).toHaveBeenCalledTimes(1))
    expect(api.controlPersonalItem.mock.calls[0]).toEqual([id, 'restart', expect.objectContaining({ expected_revision: 1, schedule: expect.objectContaining({ mode: 'now', timezone: 'Asia/Shanghai' }) })])
  })
  it('only GETs results/reports/Run on view and refresh; acknowledges exact delivery only on explicit read', async () => {
    const onOpenArtifact = vi.fn(); const { api } = renderWorkspace(apiMock(), { onOpenArtifact })
    fireEvent.click(screen.getByRole('tab', { name: '结果收件箱' })); fireEvent.click(await screen.findByRole('button', { name: /资料核验/ }))
    await screen.findByRole('button', { name: '标记已读' }); await screen.findByRole('button', { name: '打开核验报告' })
    expect(api.readPersonalItemResult).not.toHaveBeenCalled(); expect(api.personalArtifact).toHaveBeenCalledWith(artifactId, actor, 2, expect.any(AbortSignal))
    fireEvent.click(screen.getByRole('button', { name: '刷新事项' })); await waitFor(() => expect(api.personalItemResult.mock.calls.length).toBeGreaterThan(1)); expect(api.readPersonalItemResult).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Run 记录' })); await screen.findByText('读取指定材料'); expect(api.runs).toHaveBeenCalledWith('pie_exact')
    fireEvent.click(screen.getByRole('button', { name: '打开核验报告' })); expect(onOpenArtifact).toHaveBeenCalledWith(actor, ref)
    api.personalItemResult.mockResolvedValue(result({ read_receipt: { acknowledgement_id: 'ack_real' } }))
    fireEvent.click(screen.getByRole('button', { name: '标记已读' })); await waitFor(() => expect(api.readPersonalItemResult).toHaveBeenCalledTimes(1))
    expect(api.readPersonalItemResult.mock.calls[0]).toEqual([occurrenceId, expect.objectContaining({ delivery_id: 'delivery_exact', expected_delivery_sha256: sha })])
    expect(api.readPersonalItemResult.mock.calls[0][1]).not.toHaveProperty('actor_id')
  })
  it('rejects report hash or grant drift instead of opening a latest/private fallback', async () => {
    const api = apiMock(item({ occurrence: execution() })); api.personalArtifact.mockResolvedValue({ ...artifact, grant_revision: 5 })
    const onOpenArtifact = vi.fn(); renderWorkspace(api, { onOpenArtifact }); fireEvent.click(await screen.findByRole('button', { name: /资料核验 我的 Agent/ }))
    await screen.findByText('材料授权或内容校验已变化，请核对原版本')
    expect(screen.getByRole('button', { name: '打开材料' })).toBeDisabled(); expect(onOpenArtifact).not.toHaveBeenCalled()
  })
  it('routes list filters and unread/all pagination through the server rather than local fabricated states', async () => {
    const { api } = renderWorkspace(); fireEvent.click(screen.getByRole('button', { name: '已暂停' }))
    await waitFor(() => expect(api.personalItems.mock.calls.at(-1)?.[0]).toMatchObject({ state: 'paused', offset: 0 }))
    fireEvent.click(screen.getByRole('tab', { name: '结果收件箱' })); await waitFor(() => expect(api.personalItemResults.mock.calls.at(-1)?.[0]).toMatchObject({ unread: true }))
    fireEvent.click(screen.getByRole('button', { name: '全部' })); await waitFor(() => expect(api.personalItemResults.mock.calls.at(-1)?.[0]).toMatchObject({ unread: false }))
  })
})

describe('personal items actual HTTP contract', () => {
  it('preserves authenticated typed routes, exact payloads and server errors', async () => {
    const calls: { method: string; url: string; body: unknown; token: string | null }[] = []
    const server = setupServer(http.all('http://127.0.0.1:9456/api/v1/personal/*', async ({ request }) => {
      const value = request.method === 'GET' ? null : await request.json()
      calls.push({ method: request.method, url: request.url, body: value, token: request.headers.get('Authorization') })
      if (request.url.endsWith('/pause')) return HttpResponse.json({ detail: '当前修订已改变', code: 'revision_conflict' }, { status: 409 })
      return HttpResponse.json(request.method === 'GET' ? { items: [], limit: 30, offset: 0, next_offset: null } : item())
    }))
    server.listen({ onUnhandledRequest: 'error' })
    try {
      const client = new WorkbenchRuntimeClient('http://127.0.0.1:9456/api/v1', async () => 'human-token')
      await client.personalItems({ state: 'paused', query: '待核对', limit: 30, offset: 60 })
      await client.createPersonalItem(body)
      await expect(client.controlPersonalItem(id, 'pause', { expected_revision: 2, request_id: 'a'.repeat(32) })).rejects.toThrow('当前修订已改变')
      expect(calls[0]).toMatchObject({ method: 'GET', token: 'Bearer human-token' }); expect(new URL(calls[0].url).searchParams.get('query')).toBe('待核对')
      expect(calls[1].body).toEqual(body); expect(calls[2].body).toEqual({ expected_revision: 2, request_id: 'a'.repeat(32) })
    } finally { server.close() }
  })
})
