import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { CollaborationComposer, PersonalCollaborations, type PersonalCollaborationsApi } from '../../../web2/src/personal/Collaborations'
import type { PersonalAgent, PersonalCollaboration, PersonalCollaborationEvent, PersonalCollaborationInput } from '@naumi/shared/api/WorkbenchRuntimeClient'

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', '') }
  HTMLDialogElement.prototype.close = function () { this.removeAttribute('open') }
})
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks() })

const agents: PersonalAgent[] = [
  { id: `pa_${'a'.repeat(32)}`, name: '资料整理员', description: '', instructions: '按指定资料整理。', model_tier: 'fast', tool_names: ['file_read'], kind: 'specialist', member_ids: [], status: 'active', version: 3, created_at: '2026-11-04T00:00:00Z', updated_at: '2026-11-04T00:00:00Z' },
  { id: `pa_${'b'.repeat(32)}`, name: '结果复核员', description: '', instructions: '复核收到的材料。', model_tier: 'capable', tool_names: [], kind: 'specialist', member_ids: [], status: 'active', version: 2, created_at: '2026-11-04T00:00:00Z', updated_at: '2026-11-04T00:00:00Z' },
]
const collaboration = (changes: Partial<PersonalCollaboration> = {}): PersonalCollaboration => ({
  id: 'pc_test', request_id: 'request_test', source_session_id: 'source', source_agent_id: '', title: '家庭资料核对', context: '只共享这段指定材料。', created_at: '2026-11-04T01:00:00Z', cancel_requested: false, status: 'completed',
  steps: [
    { id: 'step1', agent_id: agents[0].id, agent_version: 3, task: '读取指定预算文件。', depends_on: [], job_id: 'job_1', session_id: 'execution_1', status: 'completed', response: '资料里剩余预算为 825 元。', error: '', run_id: 'run_1' },
    { id: 'step2', agent_id: agents[1].id, agent_version: 2, task: '核对前一步的计算。', depends_on: ['step1'], job_id: 'job_2', session_id: 'execution_2', status: 'completed', response: '已核对，1200 - 375 = 825。', error: '', run_id: 'run_2' },
  ], events: [{ sequence: 1, key: 'handoff_1', kind: 'handoff', step_id: 'step2', data: { from_step_id: 'step1', result_sha256: 'verified-digest' }, created_at: '2026-11-04T01:00:30Z' }], ...changes,
})
const permission = (changes: Partial<PersonalCollaborationEvent> = {}): PersonalCollaborationEvent => ({ sequence: 2, key: 'permission_1', kind: 'permission_requested', step_id: 'step1', data: { call_id: 'call_1', session_id: 'execution_1', tool_name: 'file_write', reason: '写入汇总结果', arguments: { path: 'report.md', content: '报告' } }, created_at: '2026-11-04T01:00:35Z', ...changes })
const questionId = `ask-${'c'.repeat(32)}`
const question = (changes: Partial<PersonalCollaborationEvent> = {}): PersonalCollaborationEvent => ({ sequence: 3, key: 'question_1', kind: 'question_requested', step_id: 'step1', data: { request_id: questionId, session_id: 'execution_1', header: '报告格式', question: '这次预算报告使用哪种格式？', options: [{ value: 'markdown', label: 'Markdown', description: '保留可编辑内容' }, { value: 'text', label: '纯文本', description: '适合直接复制' }], allow_custom: true, custom_label: '指定其他格式', expires_at: '2099-11-04T01:00:35Z' }, created_at: '2026-11-04T01:00:35Z', ...changes })
function apiMock(items: PersonalCollaboration[] = [collaboration()]) {
  return {
    personalCollaborations: vi.fn().mockResolvedValue({ collaborations: items }),
    personalCollaboration: vi.fn().mockImplementation(async (id: string) => items.find(item => item.id === id) || collaboration({ id })),
    createPersonalCollaboration: vi.fn().mockImplementation(async (body: PersonalCollaborationInput) => collaboration({ request_id: body.request_id, source_session_id: body.source_session_id })),
    cancelPersonalCollaboration: vi.fn().mockImplementation(async (id: string) => collaboration({ id, status: 'cancelled', cancel_requested: true })),
    replyPersonalCollaboration: vi.fn().mockImplementation(async (_id, requestId, body) => ({ status: 'answered' as const, request_id: requestId, response: body })),
    resolvePermission: vi.fn().mockResolvedValue({ status: 'resolved' }),
  } satisfies PersonalCollaborationsApi
}
function renderDialog(api: PersonalCollaborationsApi, options: { onClose?: () => void; onOpenSession?: (id: string) => Promise<unknown>; busy?: boolean } = {}) {
  return render(<PersonalCollaborations api={api} agents={agents} sourceSessionId="source" busy={options.busy || false} onClose={options.onClose || vi.fn()} onOpenSession={options.onOpenSession} />)
}
function fillStep(index: number, agentIndex = 0, task = `步骤 ${index} 的明确任务`) {
  fireEvent.change(screen.getByLabelText(`步骤 ${index} 专员`), { target: { value: agents[agentIndex].id } })
  fireEvent.change(screen.getByLabelText(`步骤 ${index} 任务`), { target: { value: task } })
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

it('renders completed specialist Markdown with the existing sanitized message renderer', async () => {
  const item = collaboration()
  item.steps[0] = { ...item.steps[0], response: '# 核算结论\n\n**825 元**\n\n<script>window.unsafe = true</script>\n\n[不可执行](javascript:alert(1))' }
  renderDialog(apiMock([item]))
  const result = await screen.findByLabelText('步骤 1 的结果')
  expect(within(result).getByRole('heading', { name: '核算结论' })).toBeVisible()
  expect(within(result).getByText('825 元').tagName).toBe('STRONG')
  expect(result.querySelector('script')).toBeNull()
  expect(within(result).queryByRole('link', { name: '不可执行' })).not.toBeInTheDocument()
})

describe('collaboration composer', () => {
  it('submits explicit tasks, frozen role versions and context with a real sequential dependency', async () => {
    const api = apiMock(), created = vi.fn(), onBusy = vi.fn()
    render(<CollaborationComposer api={api} agents={agents} sourceSessionId="source" onBusy={onBusy} onCreated={created} />)
    fillStep(1, 0, '整理收支。')
    fireEvent.click(screen.getByRole('button', { name: '添加步骤' }))
    fillStep(2, 1, '根据交接结果复核。')
    fireEvent.change(screen.getByLabelText('协作任务名称'), { target: { value: '家庭预算' } })
    fireEvent.change(screen.getByLabelText('协作共享上下文'), { target: { value: '1200 元预算，已支出 375 元。' } })
    fireEvent.click(screen.getByRole('button', { name: '开始协作' }))
    await waitFor(() => expect(created).toHaveBeenCalledOnce())
    const body = api.createPersonalCollaboration.mock.calls[0][0]
    expect(body).toMatchObject({ source_session_id: 'source', title: '家庭预算', context: '1200 元预算，已支出 375 元。', request_id: expect.any(String) })
    expect(body.steps).toHaveLength(2)
    expect(body.steps[0]).toMatchObject({ agent_id: agents[0].id, agent_version: 3, task: '整理收支。', depends_on: [] })
    expect(body.steps[1]).toMatchObject({ agent_id: agents[1].id, agent_version: 2, task: '根据交接结果复核。', depends_on: [body.steps[0].id] })
    expect(body).not.toHaveProperty('messages')
    expect(onBusy.mock.calls.map(call => call[0])).toEqual([true, false])
  })

  it('runs independent steps in parallel only when selected', async () => {
    const api = apiMock()
    render(<CollaborationComposer api={api} agents={agents} sourceSessionId="source" onBusy={vi.fn()} onCreated={vi.fn()} />)
    fillStep(1); fireEvent.click(screen.getByRole('button', { name: '添加步骤' })); fillStep(2, 1)
    fireEvent.click(screen.getByRole('radio', { name: '并行执行' }))
    fireEvent.click(screen.getByRole('button', { name: '开始协作' }))
    await waitFor(() => expect(api.createPersonalCollaboration).toHaveBeenCalledOnce())
    expect(api.createPersonalCollaboration.mock.calls[0][0].steps.map(step => step.depends_on)).toEqual([[], []])
  })

  it('retains the request identity and entered fields after an uncertain response', async () => {
    const api = apiMock()
    api.createPersonalCollaboration.mockRejectedValueOnce(new Error('连接中断，请核对协作记录后重试'))
    render(<CollaborationComposer api={api} agents={agents} sourceSessionId="source" onBusy={vi.fn()} onCreated={vi.fn()} />)
    fillStep(1)
    fireEvent.click(screen.getByRole('button', { name: '开始协作' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('连接中断')
    expect(screen.getByLabelText('步骤 1 任务')).toHaveValue('步骤 1 的明确任务')
    fireEvent.change(screen.getByLabelText('协作任务名称'), { target: { value: '修改后的标题' } })
    fireEvent.click(screen.getByRole('button', { name: '开始协作' }))
    await waitFor(() => expect(api.createPersonalCollaboration).toHaveBeenCalledTimes(2))
    expect(api.createPersonalCollaboration.mock.calls[0][0].request_id).toBe(api.createPersonalCollaboration.mock.calls[1][0].request_id)
  })

  it('prevents duplicate submission while the first admission is pending', async () => {
    const api = apiMock(), pending = deferred<PersonalCollaboration>()
    api.createPersonalCollaboration.mockReturnValue(pending.promise)
    render(<CollaborationComposer api={api} agents={agents} sourceSessionId="source" onBusy={vi.fn()} onCreated={vi.fn()} />)
    fillStep(1)
    const button = screen.getByRole('button', { name: '开始协作' })
    fireEvent.click(button); fireEvent.click(button)
    expect(api.createPersonalCollaboration).toHaveBeenCalledOnce()
    expect(screen.getByLabelText('步骤 1 任务')).toBeDisabled()
    await act(async () => { pending.resolve(collaboration({ request_id: api.createPersonalCollaboration.mock.calls[0][0].request_id })); await pending.promise })
  })

  it('requires an existing source session and never silently creates a main assistant or role', async () => {
    const api = apiMock()
    render(<CollaborationComposer api={api} agents={agents} sourceSessionId={null} onBusy={vi.fn()} onCreated={vi.fn()} />)
    fillStep(1); fireEvent.click(screen.getByRole('button', { name: '开始协作' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('请先打开一个已有对话')
    expect(api.createPersonalCollaboration).not.toHaveBeenCalled()
  })

  it('rejects a mismatched admission result instead of showing another request as accepted', async () => {
    const api = apiMock(), created = vi.fn()
    api.createPersonalCollaboration.mockResolvedValue(collaboration({ request_id: 'other-request' }))
    render(<CollaborationComposer api={api} agents={agents} sourceSessionId="source" onBusy={vi.fn()} onCreated={created} />)
    fillStep(1); fireEvent.click(screen.getByRole('button', { name: '开始协作' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('返回的协作与本次请求不一致')
    expect(created).not.toHaveBeenCalled()
  })

  it('excludes paused specialists and teams, and enforces twelve steps', () => {
    const api = apiMock()
    render(<CollaborationComposer api={api} agents={[...agents, { ...agents[0], id: 'paused', name: '暂停专员', status: 'paused' }, { ...agents[1], id: 'team', name: '团队档案', kind: 'team' }]} sourceSessionId="source" onBusy={vi.fn()} onCreated={vi.fn()} />)
    expect(screen.queryByRole('option', { name: '暂停专员' })).not.toBeInTheDocument()
    expect(screen.queryByRole('option', { name: '团队档案' })).not.toBeInTheDocument()
    for (let i = 0; i < 11; i++) fireEvent.click(screen.getByRole('button', { name: '添加步骤' }))
    expect(screen.getByRole('button', { name: '添加步骤' })).toBeDisabled()
    expect(screen.getAllByRole('combobox')).toHaveLength(12)
    expect(screen.getByRole('button', { name: '开始协作' })).toBeDisabled()
  })

  it('rewires the explicit sequential dependency when an intermediate step is removed', async () => {
    const api = apiMock()
    render(<CollaborationComposer api={api} agents={agents} sourceSessionId="source" onBusy={vi.fn()} onCreated={vi.fn()} />)
    fillStep(1); fireEvent.click(screen.getByRole('button', { name: '添加步骤' })); fillStep(2, 1)
    fireEvent.click(screen.getByRole('button', { name: '添加步骤' })); fillStep(3)
    fireEvent.click(screen.getByRole('button', { name: '删除步骤 2' }))
    fireEvent.click(screen.getByRole('button', { name: '开始协作' }))
    await waitFor(() => expect(api.createPersonalCollaboration).toHaveBeenCalledOnce())
    const steps = api.createPersonalCollaboration.mock.calls[0][0].steps
    expect(steps).toHaveLength(2)
    expect(steps[1].depends_on).toEqual([steps[0].id])
    expect(steps[1].task).toBe('步骤 3 的明确任务')
  })
})

describe('collaboration workbench', () => {
  it('shows actual independent results, versions, handoff receipts and source context', async () => {
    const api = apiMock(), open = vi.fn().mockResolvedValue(undefined), close = vi.fn()
    renderDialog(api, { onOpenSession: open, onClose: close })
    expect(await screen.findByLabelText('步骤 1 的结果')).toHaveTextContent('825 元')
    expect(screen.getByLabelText('步骤 2 的结果')).toHaveTextContent('1200 - 375 = 825')
    expect(screen.getByText('v3')).toBeVisible()
    fireEvent.click(screen.getByText('共享上下文', { selector: 'summary' }))
    expect(screen.getByText('只共享这段指定材料。')).toBeVisible()
    fireEvent.click(screen.getByText('查看原始记录'))
    expect(screen.getByText(/verified-digest/)).toBeVisible()
    fireEvent.click(screen.getAllByRole('button', { name: '查看执行记录' })[0])
    await waitFor(() => expect(open).toHaveBeenCalledWith('execution_1'))
    expect(close).toHaveBeenCalledOnce()
  })

  it('preserves UNKNOWN and never presents it as completed', async () => {
    const item = collaboration({ status: 'unknown', steps: [ { ...collaboration().steps[0], status: 'unknown', response: '', error: '执行中断，结果需要核实。' } ] })
    const api = apiMock([item])
    renderDialog(api)
    expect(await screen.findByText('执行中断，结果需要核实。')).toBeVisible()
    expect(screen.getAllByText('结果待核实').length).toBeGreaterThan(0)
    expect(screen.queryByText('已完成')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '停止协作' })).not.toBeInTheDocument()
  })

  it('shows an empty state without creating any specialist or task', async () => {
    const api = apiMock([])
    renderDialog(api)
    expect(await screen.findByText('尚无协作任务')).toBeVisible()
    expect(api.createPersonalCollaboration).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '发起协作' }))
    expect(screen.getByLabelText('步骤 1 任务')).toHaveValue('')
    expect(screen.getByLabelText('协作共享上下文')).toHaveValue('')
  })

  it('keeps the draft and request identity while inspecting an existing task after an uncertain admission', async () => {
    const api = apiMock()
    api.createPersonalCollaboration.mockRejectedValueOnce(new Error('连接中断，受理状态待确认'))
    renderDialog(api)
    await screen.findByLabelText('步骤 1 的结果')
    fireEvent.click(screen.getByRole('button', { name: '发起协作' }))
    fillStep(1)
    fireEvent.click(screen.getByRole('button', { name: '开始协作' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('连接中断')
    fireEvent.click(screen.getByRole('button', { name: /家庭资料核对/ }))
    expect(await screen.findByLabelText('步骤 1 的结果')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: '发起协作' }))
    expect(screen.getByLabelText('步骤 1 任务')).toHaveValue('步骤 1 的明确任务')
    fireEvent.click(screen.getByRole('button', { name: '开始协作' }))
    await waitFor(() => expect(api.createPersonalCollaboration).toHaveBeenCalledTimes(2))
    expect(api.createPersonalCollaboration.mock.calls[0][0].request_id).toBe(api.createPersonalCollaboration.mock.calls[1][0].request_id)
  })

  it('surfaces list failure and recovers only after an explicit retry', async () => {
    const api = apiMock()
    api.personalCollaborations.mockRejectedValueOnce(new Error('协作资料库暂不可用'))
    renderDialog(api)
    expect(await screen.findByRole('alert')).toHaveTextContent('协作资料库暂不可用')
    fireEvent.click(screen.getByRole('button', { name: '重试加载列表' }))
    expect(await screen.findByLabelText('步骤 1 的结果')).toBeVisible()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('rejects corrupted or mismatched detail rather than displaying another task', async () => {
    const api = apiMock()
    api.personalCollaboration.mockResolvedValue(collaboration({ id: 'wrong-task' }))
    renderDialog(api)
    expect(await screen.findByRole('alert')).toHaveTextContent('协作记录返回不完整')
    expect(screen.queryByLabelText('步骤 1 的结果')).not.toBeInTheDocument()
  })

  it('requires confirmation before cancellation and remains viewable during another main run', async () => {
    const api = apiMock([collaboration({ status: 'running' })])
    renderDialog(api, { busy: true })
    fireEvent.click(await screen.findByRole('button', { name: '停止协作' }))
    expect(api.cancelPersonalCollaboration).not.toHaveBeenCalled()
    const group = screen.getByRole('group', { name: '确认停止协作' })
    fireEvent.click(within(group).getByRole('button', { name: '确认停止' }))
    await waitFor(() => expect(api.cancelPersonalCollaboration).toHaveBeenCalledExactlyOnceWith('pc_test'))
    expect(screen.getAllByText('已停止').length).toBeGreaterThan(0)
  })

  it('keeps cancellation errors visible without pretending the task stopped', async () => {
    const api = apiMock([collaboration({ status: 'running' })])
    api.cancelPersonalCollaboration.mockRejectedValue(new Error('停止请求尚未受理，请重试'))
    renderDialog(api)
    fireEvent.click(await screen.findByRole('button', { name: '停止协作' }))
    fireEvent.click(screen.getByRole('button', { name: '确认停止' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('停止请求尚未受理')
    expect(screen.queryByText('已停止')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '确认停止' })).toBeEnabled()
  })

  it('shows exact child tool arguments and sends only a one-call permission decision', async () => {
    const value = collaboration({ status: 'running', events: [permission()] })
    const api = apiMock([value])
    renderDialog(api)
    const allowed = await screen.findByRole('button', { name: '允许本次操作' })
    expect(screen.getByLabelText('协作待授权操作')).toHaveTextContent('file_write')
    expect(screen.getByLabelText('协作待授权操作')).toHaveTextContent('report.md')
    expect(screen.queryByRole('button', { name: /bypass|全部允许|跳过授权/ })).not.toBeInTheDocument()
    fireEvent.click(allowed); fireEvent.click(allowed)
    await waitFor(() => expect(api.resolvePermission).toHaveBeenCalledExactlyOnceWith('execution_1', 'call_1', { decision: 'allow' }))
    expect(await screen.findByText('授权决定已提交')).toBeVisible()
  })

  it('supports denial and suppresses requests with a resolved receipt or mismatched session', async () => {
    const resolved = permission({ sequence: 3, key: 'resolved_1', kind: 'permission_resolved', data: { call_id: 'call_1' } })
    const mismatch = permission({ sequence: 4, key: 'wrong_session', data: { call_id: 'call_2', session_id: 'another_session', tool_name: 'bash_run' } })
    const pending = permission({ sequence: 5, key: 'pending_3', data: { call_id: 'call_3', session_id: 'execution_1', tool_name: 'file_write' } })
    const api = apiMock([collaboration({ status: 'running', events: [permission(), resolved, mismatch, pending] })])
    renderDialog(api)
    fireEvent.click(await screen.findByRole('button', { name: '拒绝' }))
    await waitFor(() => expect(api.resolvePermission).toHaveBeenCalledExactlyOnceWith('execution_1', 'call_3', { decision: 'deny' }))
  })

  it('preserves permission rejection and allows a deliberate retry', async () => {
    const api = apiMock([collaboration({ status: 'running', events: [permission()] })])
    api.resolvePermission.mockRejectedValueOnce(new Error('该操作授权已过期'))
    renderDialog(api)
    fireEvent.click(await screen.findByRole('button', { name: '允许本次操作' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('该操作授权已过期')
    expect(screen.getByRole('button', { name: '允许本次操作' })).toBeEnabled()
  })

  it('answers a structured specialist question with one exact option and prevents duplicate replies', async () => {
    const item = collaboration({ status: 'running', steps: [{ ...collaboration().steps[0], status: 'running' }], events: [question()] })
    const api = apiMock([item])
    renderDialog(api)
    const option = await screen.findByRole('button', { name: /Markdown/ })
    expect(screen.getByLabelText('协作待回答问题')).toHaveTextContent('这次预算报告使用哪种格式？')
    fireEvent.click(option); fireEvent.click(option)
    await waitFor(() => expect(api.replyPersonalCollaboration).toHaveBeenCalledExactlyOnceWith('pc_test', questionId, { kind: 'option', value: 'markdown' }))
    expect(await screen.findByText('回答已提交')).toBeVisible()
    expect(screen.queryByRole('button', { name: /Markdown/ })).not.toBeInTheDocument()
  })

  it('retains a custom answer after a server conflict, caps input and permits deliberate retry', async () => {
    const item = collaboration({ status: 'running', steps: [{ ...collaboration().steps[0], status: 'running' }], events: [question()] })
    const api = apiMock([item])
    api.replyPersonalCollaboration.mockRejectedValueOnce(new Error('回答与已有决定冲突，请核对记录'))
    renderDialog(api)
    const input = await screen.findByLabelText(`问题 ${questionId} 自定义回答`)
    expect(input).toHaveAttribute('maxlength', '4000')
    expect(screen.getByRole('button', { name: '提交回答' })).toBeDisabled()
    fireEvent.change(input, { target: { value: '  请使用 CSV，并保留来源列。  ' } })
    fireEvent.click(screen.getByRole('button', { name: '提交回答' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('回答与已有决定冲突')
    expect(input).toHaveValue('  请使用 CSV，并保留来源列。  ')
    expect(screen.getByRole('button', { name: '提交回答' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: '提交回答' }))
    await waitFor(() => expect(api.replyPersonalCollaboration).toHaveBeenCalledTimes(2))
    expect(api.replyPersonalCollaboration.mock.calls[1]).toEqual(['pc_test', questionId, { kind: 'custom', custom_text: '请使用 CSV，并保留来源列。' }])
  })

  it('suppresses answered and server-expired questions and hides requests outside the running step', async () => {
    const expiredId = `ask-${'d'.repeat(32)}`
    const endedId = `ask-${'e'.repeat(32)}`
    const item = collaboration({ status: 'running', steps: [{ ...collaboration().steps[0], status: 'running' }, { ...collaboration().steps[1], status: 'completed' }], events: [
      question(), { ...question(), key: 'answer_1', kind: 'question_answered', data: { request_id: questionId, response: { kind: 'option', value: 'text' } } },
      question({ key: 'expired_request', data: { ...question().data, request_id: expiredId } }),
      { ...question(), key: 'expiry', kind: 'question_expired', data: { request_id: expiredId } },
      question({ key: 'finished_request', step_id: 'step2', data: { ...question().data, request_id: endedId, session_id: 'execution_2' } }),
    ] })
    renderDialog(apiMock([item]))
    await screen.findByLabelText('步骤 1 的结果')
    expect(screen.queryByLabelText('协作待回答问题')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Markdown/ })).not.toBeInTheDocument()
  })

  it('shows a locally elapsed question as expired before a journal-expiry receipt arrives', async () => {
    const item = collaboration({ status: 'running', steps: [{ ...collaboration().steps[0], status: 'running' }], events: [question({ data: { ...question().data, expires_at: '2000-01-01T00:00:00Z' } })] })
    const api = apiMock([item])
    renderDialog(api)
    expect(await screen.findByText('问题已过期')).toBeVisible()
    expect(screen.queryByRole('button', { name: /Markdown/ })).not.toBeInTheDocument()
    expect(api.replyPersonalCollaboration).not.toHaveBeenCalled()
  })

  it('ignores a late detail response after another task has been selected', async () => {
    const second = collaboration({ id: 'pc_second', title: '第二个任务', steps: [{ ...collaboration().steps[0], response: '第二个真实结果' }] })
    const api = apiMock([collaboration(), second]), first = deferred<PersonalCollaboration>()
    api.personalCollaboration.mockImplementation(async id => id === 'pc_test' ? first.promise : second)
    renderDialog(api)
    fireEvent.click(await screen.findByRole('button', { name: /第二个任务/ }))
    expect(await screen.findByText('第二个真实结果')).toBeVisible()
    await act(async () => { first.resolve(collaboration()); await first.promise })
    expect(screen.getByText('第二个真实结果')).toBeVisible()
    expect(screen.queryByText('资料里剩余预算为 825 元。')).not.toBeInTheDocument()
  })

  it('stops all polling after the workbench closes', async () => {
    vi.useFakeTimers()
    const api = apiMock([collaboration({ status: 'running' })])
    const { unmount } = renderDialog(api)
    await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() })
    expect(api.personalCollaborations).toHaveBeenCalledOnce()
    expect(api.personalCollaboration).toHaveBeenCalledOnce()
    unmount()
    await act(async () => { await vi.advanceTimersByTimeAsync(12000) })
    expect(api.personalCollaborations).toHaveBeenCalledOnce()
    expect(api.personalCollaboration).toHaveBeenCalledOnce()
  })
})
