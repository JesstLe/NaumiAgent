import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { WorkbenchRuntimeClient, type PersonalAgent } from '@naumi/shared/api/WorkbenchRuntimeClient'
import type { PersonalRoutine, PersonalRoutineContext, PersonalRoutineDefinition, PersonalRoutineDefinitionInput, PersonalRoutineOccurrence, PersonalRoutinePreview } from '@naumi/shared/api/personalRoutines'
import type { PersonalItem } from '@naumi/shared/api/personalItems'
import { PersonalRoutines } from '../../../web2/src/personal/routines/PersonalRoutines'
import { routineRawDefinition } from '../../../web2/src/personal/routines/RoutineEditor'
import { parseRoutinePending, routinePendingKey, usePersonalRoutines, type PersonalRoutinesApi, type RoutineWrite } from '../../../web2/src/personal/routines/usePersonalRoutines'
import { requestArtifactNavigation } from '../../../web2/src/personal/library/navigation'

const id = `pr_${'a'.repeat(32)}`, occurrenceId = `pro_${'b'.repeat(32)}`, previewId = `prp_${'c'.repeat(32)}`, actorId = `pa_${'d'.repeat(32)}`, hash = 'e'.repeat(64), epoch = 'f'.repeat(32)
const agent: PersonalAgent = { id: actorId, name: '我的资料官', version: 3, status: 'active', kind: 'specialist', instructions: '核验资料', description: '', model_tier: 'capable', tool_names: ['personal_artifacts'], member_ids: [], created_at: '', updated_at: '' }
const policy = { tool_names: ['personal_artifacts'], artifact_operations: ['get', 'read', 'publish'], model_tier: 'capable' as const, max_turns: 12, max_cost_microusd: 2_000_000, timeout_seconds: 180 }
const context: PersonalRoutineContext = { owner_key: hash, workspace_key: hash, default_policy: policy, calendars: [{ id: 'XNYS', name: '纽约市场', version: '2026-exact', timezone: 'America/New_York', coverage_start: '2026-01-01', coverage_end: '2026-12-31' }] }
const definition: PersonalRoutineDefinition = {
  schema_version: 1, title: '每日资料核对', instructions: '私密正文:原材料只在本次请求中使用', tags: ['资料'], assignee: { id: actorId, version: 3, name: agent.name, kind: 'specialist' },
  inputs: { artifact_refs: [], directories: [] }, route: { workspace_key: hash }, policy,
  schedule: { kind: 'cron', expression: '0 9 * * 1-5', timezone: 'Asia/Shanghai', starts_at: '2099-11-05T00:00:00Z', ends_at: null, dst_gap: 'skip', dst_fold: 'first', version: 1, timezone_sha256: hash },
  limits: { max_executions: 10, total_cost_microusd: 20_000_000, max_concurrent: 1, max_pending: 4 },
  recovery: { misfire_policy: 'skip', grace_seconds: 3600, max_catch_up: 1, overlap_policy: 'queue' },
}
const raw = (): PersonalRoutineDefinitionInput => routineRawDefinition(definition)
const routine = (changes: Partial<PersonalRoutine> = {}): PersonalRoutine => ({ id, revision: 1, status: 'active', epoch, definition, definition_sha256: hash, next_due_at: '2099-11-06T01:00:00Z', budget: { reserved_slots: 0, started_executions: 0, settled_cost_microusd: 0, reserved_cost_microusd: 0, unknown_count: 0, remaining_cost_microusd: 20_000_000, remaining_slots: 10, accounting: 'rate_card_estimate_not_provider_billing' }, created_at: '2099-11-05T00:00:00Z', updated_at: '2099-11-05T00:00:00Z', ...changes })
const preview = (changes: Partial<PersonalRoutinePreview> = {}): PersonalRoutinePreview => ({ preview_id: previewId, definition_digest: hash, expires_at: '2099-11-05T01:00:00Z', definition, schedule_preview: [{ due_at: '2099-11-06T01:00:00Z', local_time: '2099-11-06T09:00:00', fold: 0 }], base_routine_id: null, base_revision: null, ...changes })
const occurrence = (changes: Partial<PersonalRoutineOccurrence> = {}): PersonalRoutineOccurrence => ({ id: occurrenceId, routine_id: id, epoch, definition_revision: 1, nominal_due_at: '2099-11-06T01:00:00Z', local_time: '2099-11-06T09:00:00', fold: 0, status: 'reserved', item_id: null, reserved_cost_microusd: 2_000_000, created_at: '2099-11-06T01:00:00Z', ...changes })
function apiMock(value = routine()) {
  return {
    personalRoutineContext: vi.fn().mockResolvedValue(context), personalRoutines: vi.fn().mockResolvedValue({ routines: [value], next_after: null }), personalRoutine: vi.fn().mockResolvedValue(value),
    previewPersonalRoutine: vi.fn().mockResolvedValue(preview()), createPersonalRoutine: vi.fn().mockResolvedValue(value), revisePersonalRoutine: vi.fn().mockResolvedValue(routine({ revision: 2 })),
    controlPersonalRoutine: vi.fn().mockResolvedValue(routine({ revision: 2, status: 'paused' })), personalRoutineHistory: vi.fn().mockResolvedValue({ history: [value], next_after: null }),
    personalRoutineOccurrences: vi.fn().mockResolvedValue({ occurrences: [], next_after: null }),
    personalRoutineRequest: vi.fn().mockResolvedValue({ request_id: '1'.repeat(32), operation: 'create', status: 'applied', result_kind: 'routine', result_id: id, result: value }),
    personalArtifacts: vi.fn().mockResolvedValue({ artifacts: [] }), personalArtifact: vi.fn(), runs: vi.fn().mockResolvedValue({ runs: [] }),
  } satisfies PersonalRoutinesApi
}
function renderWorkspace(api = apiMock(), onClose = vi.fn()) {
  return { api, onClose, ...render(<PersonalRoutines api={api} namespace="workspace-A" agents={[agent, { ...agent, id: `pa_${'9'.repeat(32)}`, kind: 'team', name: '团队' }, { ...agent, id: `pa_${'8'.repeat(32)}`, status: 'paused', name: '已停用' }]} connected onClose={onClose} onOpenArtifact={vi.fn()} />) }
}
async function openEditor() {
  await waitFor(() => expect(screen.getByRole('button', { name: '新建计划' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '新建计划' }))
}
const createWrite = (): RoutineWrite => ({ operation: 'create', body: { request_id: '1'.repeat(32), preview_id: previewId, expected_digest: hash, enabled: true } })
beforeEach(() => { localStorage.clear(); history.replaceState({}, '', '/web2') })
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('opaque durable routine requests', () => {
  it('records only opaque identities, keeps 503 unknown and reconciles by GET after remount without a second POST', async () => {
    const api = apiMock(); api.previewPersonalRoutine.mockRejectedValueOnce(new Error('503 UNKNOWN'))
    const request: RoutineWrite = { operation: 'preview', body: { request_id: '2'.repeat(32), definition: { ...raw(), directories: [{ path: 'E:/private-folder', pattern: '**/*', max_files: 10, max_bytes: 100 }] } } }
    const first = renderHook(() => usePersonalRoutines({ api, namespace: 'private', connected: true }))
    await act(async () => { await first.result.current.submit(request) })
    const persisted = localStorage.getItem(routinePendingKey('private'))!
    expect(JSON.parse(persisted)).toEqual({ version: 1, operation: 'preview', request_id: '2'.repeat(32) })
    for (const secret of ['私密正文', 'E:/private-folder', 'assignee', 'input_refs', 'definition']) expect(persisted).not.toContain(secret)
    first.unmount()
    api.personalRoutineRequest.mockResolvedValue({ request_id: '2'.repeat(32), operation: 'preview', status: 'applied', result_kind: 'preview', result_id: previewId, result: preview() })
    const reopened = renderHook(() => usePersonalRoutines({ api, namespace: 'private', connected: true }))
    await waitFor(() => expect(reopened.result.current.pending?.request_id).toBe('2'.repeat(32)))
    await act(async () => { await reopened.result.current.reconcile() })
    expect(api.previewPersonalRoutine).toHaveBeenCalledTimes(1)
    expect(api.personalRoutineRequest).toHaveBeenCalledExactlyOnceWith('2'.repeat(32))
    expect(reopened.result.current.preview?.preview_id).toBe(previewId)
    expect(localStorage.getItem(routinePendingKey('private'))).toBeNull()
  })
  it('retains create identity through empty lists, pending receipts, 404 and mismatched result identity', async () => {
    const api = apiMock(); api.createPersonalRoutine.mockRejectedValueOnce(new Error('response lost')); api.personalRoutines.mockResolvedValue({ routines: [], next_after: null })
    const state = renderHook(() => usePersonalRoutines({ api, namespace: 'lost', connected: true }))
    await act(async () => { await state.result.current.submit(createWrite()) })
    api.personalRoutineRequest.mockRejectedValueOnce(new Error('404'))
    await act(async () => { await state.result.current.reconcile() })
    expect(state.result.current.pending).not.toBeNull()
    api.personalRoutineRequest.mockResolvedValueOnce({ request_id: '1'.repeat(32), operation: null, status: 'unknown', result_kind: null, result_id: null, result: null })
    await act(async () => { await state.result.current.reconcile() })
    expect(state.result.current.pending).not.toBeNull()
    expect(state.result.current.error).toContain('仍待核对')
    api.personalRoutineRequest.mockResolvedValueOnce({ request_id: '1'.repeat(32), operation: 'create', status: 'applied', result_kind: 'routine', result_id: 'wrong-id', result: routine() })
    await act(async () => { await state.result.current.reconcile() })
    expect(state.result.current.pending).not.toBeNull()
    await act(async () => { await state.result.current.submit(createWrite()) })
    expect(api.createPersonalRoutine).toHaveBeenCalledTimes(1)
  })
  it('blocks all transport when local identity storage fails', async () => {
    const api = apiMock(); const state = renderHook(() => usePersonalRoutines({ api, namespace: 'storage', connected: true }))
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('storage full') })
    await act(async () => { await state.result.current.submit(createWrite()) })
    expect(api.createPersonalRoutine).not.toHaveBeenCalled()
    expect(state.result.current.error).toContain('storage full')
  })
  it('rejects corrupted or secret-bearing pending records instead of replaying them', async () => {
    expect(() => parseRoutinePending(JSON.stringify({ version: 1, operation: 'preview', request_id: '1'.repeat(32), definition: raw() }))).toThrow('身份记录')
    localStorage.setItem(routinePendingKey('broken'), '{broken')
    const api = apiMock(); const state = renderHook(() => usePersonalRoutines({ api, namespace: 'broken', connected: true }))
    await waitFor(() => expect(state.result.current.identityError).not.toBe(''))
    await act(async () => { await state.result.current.submit(createWrite()) })
    expect(api.createPersonalRoutine).not.toHaveBeenCalled()
    expect(localStorage.getItem(routinePendingKey('broken'))).toBe('{broken')
  })
  it('does not clear an identity changed by another tab during transport', async () => {
    const api = apiMock(); let resolve!: (value: PersonalRoutine) => void
    api.createPersonalRoutine.mockReturnValueOnce(new Promise(value => { resolve = value }))
    const state = renderHook(() => usePersonalRoutines({ api, namespace: 'race', connected: true }))
    let task!: Promise<unknown>
    act(() => { task = state.result.current.submit(createWrite()) })
    const changed = JSON.stringify({ version: 1, operation: 'preview', request_id: '9'.repeat(32) })
    localStorage.setItem(routinePendingKey('race'), changed)
    await act(async () => { resolve(routine()); await task })
    expect(localStorage.getItem(routinePendingKey('race'))).toBe(changed)
    expect(state.result.current.accepted).toBeNull()
  })
  it('rejects a preview with another base revision and never confirms it', async () => {
    const api = apiMock(); api.previewPersonalRoutine.mockResolvedValueOnce(preview({ base_routine_id: id, base_revision: 2 }))
    const state = renderHook(() => usePersonalRoutines({ api, namespace: 'base', connected: true }))
    await act(async () => { await state.result.current.submit({ operation: 'preview', body: { request_id: '5'.repeat(32), definition: raw(), routine_id: id, expected_revision: 1 } }) })
    expect(state.result.current.preview).toBeNull()
    expect(state.result.current.pending?.expected_revision).toBe(1)
    expect(api.createPersonalRoutine).not.toHaveBeenCalled()
  })
})

describe('operative light routine workspace', () => {
  it('previews actual returned times and requires an explicit digest-bound confirmation before creating', async () => {
    const { api } = renderWorkspace()
    await openEditor()
    fireEvent.change(screen.getByLabelText('计划名称'), { target: { value: '真实周期' } })
    fireEvent.change(screen.getByLabelText('计划执行要求'), { target: { value: '核对指定材料' } })
    fireEvent.change(screen.getByLabelText('计划执行 Agent'), { target: { value: actorId } })
    expect(screen.queryByRole('option', { name: /团队/ })).toBeNull()
    expect(screen.queryByRole('option', { name: /已停用/ })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '预览计划' }))
    expect(await screen.findByLabelText('计划预览')).toHaveTextContent('2099-11-06T09:00:00')
    expect(api.createPersonalRoutine).not.toHaveBeenCalled()
    const submitted = api.previewPersonalRoutine.mock.calls[0][0]
    expect(submitted.definition.assignee_id).toBe(actorId)
    expect(submitted.definition.assignee_version).toBe(3)
    expect(submitted.definition.policy.model_tier).toBe(agent.model_tier)
    expect(submitted.definition.policy.tool_names.every((tool: string) => agent.tool_names.includes(tool))).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: '确认保存并启用' }))
    await waitFor(() => expect(api.createPersonalRoutine).toHaveBeenCalledTimes(1))
    expect(api.createPersonalRoutine.mock.calls[0][0]).toMatchObject({ preview_id: previewId, expected_digest: hash, enabled: true })
    expect(screen.queryByRole('button', { name: '确认保存并启用' })).toBeNull()
  })
  it('invalidates preview when any execution field changes and permits zero limits without treating them as unlimited', async () => {
    const { api } = renderWorkspace()
    await openEditor()
    fireEvent.change(screen.getByLabelText('计划名称'), { target: { value: '零额度' } })
    fireEvent.change(screen.getByLabelText('计划执行要求'), { target: { value: '只保存关闭执行的计划' } })
    fireEvent.change(screen.getByLabelText('计划执行次数上限'), { target: { value: '0' } })
    fireEvent.change(screen.getByLabelText('计划总额度'), { target: { value: '0' } })
    fireEvent.click(screen.getByRole('button', { name: '预览计划' }))
    await screen.findByLabelText('计划预览')
    expect(api.previewPersonalRoutine.mock.calls[0][0].definition.limits).toMatchObject({ max_executions: 0, total_cost_microusd: 0 })
    fireEvent.change(screen.getByLabelText('cron 表达式'), { target: { value: '0 10 * * 1-5' } })
    expect(screen.queryByLabelText('计划预览')).toBeNull()
    expect(api.createPersonalRoutine).not.toHaveBeenCalled()
  })
  it('uses only calendar context and sends exact coverage/time semantics without synthetic sessions', async () => {
    const { api } = renderWorkspace()
    await openEditor()
    fireEvent.change(screen.getByLabelText('计划名称'), { target: { value: '市场日报' } })
    fireEvent.change(screen.getByLabelText('计划执行要求'), { target: { value: '研究并保存报告' } })
    fireEvent.change(screen.getByLabelText('计划时间规则'), { target: { value: 'trading_day' } })
    expect(screen.getByLabelText('交易日日历').querySelectorAll('option')).toHaveLength(1)
    expect(screen.getByLabelText('交易日日历')).toHaveTextContent('纽约市场')
    fireEvent.change(screen.getByLabelText('交易日触发基准'), { target: { value: 'time' } })
    fireEvent.change(screen.getByLabelText('交易日当地时刻'), { target: { value: '15:10' } })
    fireEvent.click(screen.getByRole('button', { name: '预览计划' }))
    await waitFor(() => expect(api.previewPersonalRoutine).toHaveBeenCalledTimes(1))
    expect(api.previewPersonalRoutine.mock.calls[0][0].definition.schedule).toMatchObject({ kind: 'trading_day', calendar_id: 'XNYS', coverage_start: '2026-01-01', coverage_end: '2026-12-31', at: 'time', local_time: '15:10', offset_minutes: 0 })
    expect(JSON.stringify(api.previewPersonalRoutine.mock.calls[0][0])).not.toContain('calendar_sessions')
  })
  it('keeps an unsaved draft on navigation and leaves only after explicit discard', async () => {
    const { onClose } = renderWorkspace()
    await openEditor()
    fireEvent.change(screen.getByLabelText('计划名称'), { target: { value: '尚未保存' } })
    fireEvent.click(screen.getByRole('button', { name: '关闭周期计划' }))
    expect(await screen.findByRole('alertdialog', { name: '放弃未保存计划' })).toBeInTheDocument()
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '继续编辑' }))
    expect(screen.getByLabelText('计划名称')).toHaveValue('尚未保存')
    act(() => requestArtifactNavigation(onClose))
    fireEvent.click(screen.getByRole('button', { name: '放弃修改并离开' }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })
  it('reopens an applied preview by opaque request lookup after page reload', async () => {
    localStorage.setItem(routinePendingKey('workspace-A'), JSON.stringify({ version: 1, operation: 'preview', request_id: '6'.repeat(32) }))
    const api = apiMock(); api.personalRoutineRequest.mockResolvedValue({ request_id: '6'.repeat(32), operation: 'preview', status: 'applied', result_kind: 'preview', result_id: previewId, result: preview() })
    renderWorkspace(api)
    fireEvent.click(await screen.findByRole('button', { name: '核对原请求' }))
    expect(await screen.findByRole('button', { name: '确认保存并启用' })).toBeInTheDocument()
    expect(screen.getByLabelText('计划执行要求')).toHaveValue(definition.instructions)
    expect(api.previewPersonalRoutine).not.toHaveBeenCalled()
    expect(api.createPersonalRoutine).not.toHaveBeenCalled()
  })
  it('shows pause as future-only and sends original CAS without claiming to stop running jobs', async () => {
    const { api } = renderWorkspace()
    fireEvent.click(await screen.findByRole('button', { name: /每日资料核对/ }))
    fireEvent.click(await screen.findByRole('button', { name: '暂停后续' }))
    const dialog = screen.getByRole('alertdialog', { name: '确认周期计划操作' })
    expect(dialog).toHaveTextContent('已受理执行保持原状态')
    fireEvent.click(within(dialog).getByRole('button', { name: '确认暂停后续触发' }))
    await waitFor(() => expect(api.controlPersonalRoutine).toHaveBeenCalledTimes(1))
    expect(api.controlPersonalRoutine.mock.calls[0]).toEqual([id, 'pause', expect.objectContaining({ expected_revision: 1 })])
  })
  it('distinguishes execution, delivery and unknown cost; paginates original occurrences and history', async () => {
    const child: PersonalItem = { item_id: `pi_${'1'.repeat(32)}`, owner_key: hash, revision: 1, once_key: epoch, state: 'active', definition: { title: definition.title, instructions: definition.instructions, tags: [], assignee: definition.assignee, origin: { actor_id: 'main', session_id: '' }, route: {}, schedule: { mode: 'once', timezone: 'Asia/Shanghai', misfire_policy: 'skip', grace_seconds: 3600 }, inputs: { artifact_refs: [], directories: [] }, policy, output: { destination: 'personal_library', format: 'report' } }, definition_sha256: hash, created_at: '', updated_at: '', occurrence: { occurrence_id: `pio_${'2'.repeat(32)}`, status: 'completed', job_id: 'job_exact', run_id: 'run_exact', execution_session_id: 'pie_exact', answer: '原始报告', artifacts: [] } }
    const api = apiMock(routine({ budget: { ...routine().budget, unknown_count: 1, reserved_cost_microusd: 2_000_000 } }))
    api.personalRoutineOccurrences.mockResolvedValueOnce({ occurrences: [occurrence({ status: 'unknown', item_id: child.item_id, child })], next_after: 'cursor-original' }).mockResolvedValue({ occurrences: [occurrence({ id: `pro_${'3'.repeat(32)}`, status: 'skipped' })], next_after: null })
    api.personalRoutineHistory.mockResolvedValueOnce({ history: [routine()], next_after: 1 }).mockResolvedValue({ history: [routine({ revision: 2 })], next_after: null })
    renderWorkspace(api)
    fireEvent.click(await screen.findByRole('button', { name: /每日资料核对/ }))
    expect(await screen.findByText('1 次费用待核对 · 原预留额度保留')).toBeInTheDocument()
    const historyArea = screen.getByLabelText('周期执行记录')
    expect(historyArea).toHaveTextContent('已完成')
    expect(historyArea).toHaveTextContent('尚无送达回执')
    expect(historyArea).toHaveTextContent('待核对')
    expect(historyArea).toHaveTextContent('原始报告')
    fireEvent.click(screen.getByRole('button', { name: '更多执行' }))
    await waitFor(() => expect(api.personalRoutineOccurrences).toHaveBeenLastCalledWith(id, { limit: 20, after: 'cursor-original' }))
    fireEvent.click(screen.getByRole('button', { name: '修订记录' }))
    fireEvent.click(await screen.findByRole('button', { name: '更多修订' }))
    await waitFor(() => expect(api.personalRoutineHistory).toHaveBeenLastCalledWith(id, { limit: 20, after: 1 }))
  })
  it('strips public schema evidence when rebuilding raw definitions for revision', () => {
    const value = raw()
    expect(value.assignee_id).toBe(actorId)
    for (const key of ['schema_version', 'assignee', 'route', 'inputs']) expect(value).not.toHaveProperty(key)
    expect(value.schedule).not.toHaveProperty('version')
    expect(value.schedule).not.toHaveProperty('timezone_sha256')
    expect(value.schedule).not.toHaveProperty('calendar_evidence')
    expect(value.schedule.kind).toBe('cron')
  })
  it('revises only after a preview bound to the original parent revision', async () => {
    const api = apiMock(); api.previewPersonalRoutine.mockResolvedValue(preview({ base_routine_id: id, base_revision: 1 }))
    renderWorkspace(api)
    fireEvent.click(await screen.findByRole('button', { name: /每日资料核对/ }))
    fireEvent.click(await screen.findByRole('button', { name: '修改计划' }))
    fireEvent.change(screen.getByLabelText('计划执行要求'), { target: { value: '后续处理新要求' } })
    fireEvent.click(screen.getByRole('button', { name: '预览计划' }))
    expect(await screen.findByText('修改仅影响后续，已受理执行保留原版本。')).toBeInTheDocument()
    expect(api.previewPersonalRoutine.mock.calls[0][0]).toMatchObject({ routine_id: id, expected_revision: 1, definition: { instructions: '后续处理新要求' } })
    expect(api.revisePersonalRoutine).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '确认修改计划' }))
    await waitFor(() => expect(api.revisePersonalRoutine).toHaveBeenCalledWith(id, expect.objectContaining({ preview_id: previewId, expected_digest: hash, expected_revision: 1 })))
    expect(api.createPersonalRoutine).not.toHaveBeenCalled()
  })
  it('blocks expired preview confirmation without minting a replacement mutation', async () => {
    const api = apiMock(); api.previewPersonalRoutine.mockResolvedValue(preview({ expires_at: '2000-01-01T00:00:00Z' }))
    renderWorkspace(api); await openEditor()
    fireEvent.change(screen.getByLabelText('计划名称'), { target: { value: '过期预览' } })
    fireEvent.change(screen.getByLabelText('计划执行要求'), { target: { value: '不执行' } })
    fireEvent.click(screen.getByRole('button', { name: '预览计划' }))
    expect(await screen.findByRole('button', { name: '确认保存并启用' })).toBeDisabled()
    expect(api.createPersonalRoutine).not.toHaveBeenCalled()
  })
  it('reuses the authorized material picker with the selected real Agent and clears old references when switching', async () => {
    const api = apiMock()
    const ref = { artifact_id: `art_${'4'.repeat(32)}`, version: 2, sha256: hash, grant_id: '', grant_revision: 0 }
    api.personalArtifacts.mockResolvedValue({ artifacts: [{ ...ref, owner_id: actorId, title: '我的精确材料', kind: 'material', bytes: 12, mime_type: 'text/plain', suffix: '.txt', source_refs: [], evidence: {}, can_revise: true, can_share: true, created_at: '' }] })
    renderWorkspace(api); await openEditor()
    fireEvent.change(screen.getByLabelText('计划执行 Agent'), { target: { value: actorId } })
    fireEvent.click(screen.getByRole('button', { name: '选择材料' }))
    fireEvent.click(await screen.findByRole('checkbox', { name: /我的精确材料/ }))
    expect(api.personalArtifacts.mock.calls[0][0]).toMatchObject({ actor_id: actorId })
    fireEvent.click(screen.getByRole('button', { name: '选择 1 项' }))
    expect(screen.getByRole('button', { name: `移除计划材料 ${ref.artifact_id}` })).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('计划执行 Agent'), { target: { value: 'main' } })
    expect(screen.queryByRole('button', { name: `移除计划材料 ${ref.artifact_id}` })).toBeNull()
  })
  it('offers no fabricated trading calendar when the server has none', async () => {
    const api = apiMock(); api.personalRoutineContext.mockResolvedValue({ ...context, calendars: [] })
    renderWorkspace(api); await openEditor()
    expect(screen.getByRole('option', { name: '交易日' })).toBeDisabled()
    expect(screen.queryByLabelText('交易日日历')).toBeNull()
  })
})

describe('routine HTTP client', () => {
  it('uses exact REST methods, encoded IDs, no-store reads and stable mutation payloads', async () => {
    const seen: { method: string; url: string; body: unknown; cache: RequestCache }[] = []
    const server = setupServer(http.all('http://localhost:8765/*', async ({ request }) => {
      seen.push({ method: request.method, url: request.url, body: request.method === 'POST' ? await request.json() : null, cache: request.cache })
      return HttpResponse.json({})
    }))
    server.listen({ onUnhandledRequest: 'error' })
    try {
      const client = new WorkbenchRuntimeClient('http://localhost:8765', async () => null)
      await client.personalRoutineContext()
      await client.personalRoutines({ status: 'paused', query: '待核对', limit: 30, after: 'id/opaque' })
      await client.previewPersonalRoutine({ request_id: '7'.repeat(32), definition: raw() })
      await client.createPersonalRoutine((createWrite() as Extract<RoutineWrite, { operation: 'create' }>).body)
      await client.revisePersonalRoutine('id/opaque', { request_id: '8'.repeat(32), preview_id: previewId, expected_digest: hash, expected_revision: 1 })
      await client.controlPersonalRoutine(id, 'extra-run', { request_id: '9'.repeat(32), expected_revision: 1 })
      await client.personalRoutineHistory(id, { after: 2, limit: 20 })
      await client.personalRoutineOccurrences(id, { after: 'opaque/after', limit: 20 })
      await client.personalRoutineRequest('req/opaque')
      expect(seen.filter(value => value.method === 'POST')).toHaveLength(4)
      expect(seen[1].url).toContain('status=paused')
      expect(seen[1].url).toContain('after=id%2Fopaque')
      expect(seen[4].url).toContain('/id%2Fopaque/revise')
      expect(seen[5].url).toContain('/extra-run')
      expect(seen[7].url).toContain('after=opaque%2Fafter')
      expect(seen[8].url).toContain('/requests/req%2Fopaque')
      expect(seen.filter(value => value.method === 'GET').every(value => value.cache === 'no-store')).toBe(true)
      expect(seen[3].body).toEqual((createWrite() as Extract<RoutineWrite, { operation: 'create' }>).body)
    } finally { server.close() }
  })
})
