import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { createHash } from 'node:crypto'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import type {
  PersonalAgent, PersonalAgentMessage, PersonalCollaboration, PersonalCollaborationEvent, PersonalMessageInput, PersonalPeerGrant,
} from '@naumi/shared/api/WorkbenchRuntimeClient'
import {
  ManualMessageComposer, PeerConversations, PeerScopeEditor, defaultPeerTools, validatePersonalMessage, type PersonalMessagesApi,
} from '../../../web2/src/personal/PeerMessages'
import { CollaborationComposer, PersonalCollaborations, matchesCollaborationExecution, type PersonalCollaborationsApi } from '../../../web2/src/personal/Collaborations'

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', '') }
  HTMLDialogElement.prototype.close = function () { this.removeAttribute('open') }
})
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks() })

const agents: PersonalAgent[] = [
  { id: `pa_${'a'.repeat(32)}`, name: '工作统筹员', description: '', instructions: '明确共享资料后协作。', model_tier: 'fast', tool_names: ['personal_message', 'file_read', 'file_write', 'unknown_tool'], kind: 'specialist', member_ids: [], status: 'active', version: 3, created_at: '2026-11-04T01:00:00Z', updated_at: '2026-11-04T01:00:00Z' },
  { id: `pa_${'b'.repeat(32)}`, name: '预算核算员', description: '', instructions: '读取指定预算。', model_tier: 'fast', tool_names: ['file_read', 'file_write'], kind: 'specialist', member_ids: [], status: 'active', version: 2, created_at: '2026-11-04T01:00:00Z', updated_at: '2026-11-04T01:00:00Z' },
]
const tools = [{ name: 'personal_message', description: '专员互询', read_only: false },
  { name: 'file_read', description: '读取文件', read_only: true }, { name: 'file_write', description: '写入文件', read_only: false }]
const collaboration = (updates: Partial<PersonalCollaboration> = {}): PersonalCollaboration => ({
  id: `pc_${'c'.repeat(32)}`, request_id: 'peer-request', source_session_id: 'main-source', source_agent_id: '',
  title: '预算资料互询', context: '', created_at: '2026-11-04T01:00:00Z', cancel_requested: false, status: 'completed',
  steps: [{ id: 'dispatch', agent_id: agents[0].id, agent_version: 3, task: '核对共享预算。', depends_on: [], job_id: 'root-job', session_id: 'sender-session', status: 'completed', response: '', error: '', run_id: 'sender-run' }], events: [], ...updates,
})
const manualCollaboration = (body: PersonalMessageInput, updates: Partial<PersonalCollaboration> = {}) => collaboration({
  request_id: `message-${createHash('sha256').update(body.request_id).digest('hex')}`,
  message_request_id: body.request_id, source_session_id: body.source_session_id, ...updates,
})
const message = (updates: Partial<PersonalAgentMessage> = {}): PersonalAgentMessage => ({
  id: `pm_${'d'.repeat(32)}`, collaboration_id: collaboration().id, step_id: 'dispatch', source_session_id: 'sender-session', source_run_id: 'sender-run',
  sender_agent_id: agents[0].id, sender_version: 3, sender_name: agents[0].name,
  recipient_agent_id: agents[1].id, recipient_version: 2, recipient_name: agents[1].name,
  receiver_session_id: 'receiver-session', receiver_run_id: 'receiver-run', root_job_id: 'root-job',
  content: '请核对指定文件中的剩余预算。', context: '只读取 budget.csv', response: '**825 元**，来源 budget.csv。', error: '', outcome: 'completed', state: 'responded',
  created_at: '2026-11-04T01:00:05Z', acknowledged_at: '', response_sha256: 'e'.repeat(64), result_sha256: 'f'.repeat(64),
  receipts: [{ sequence: 1, kind: 'requested', data: {}, created_at: '2026-11-04T01:00:05Z', receipt_sha256: 'a'.repeat(64) },
    { sequence: 2, kind: 'received', data: {}, created_at: '2026-11-04T01:00:10Z', receipt_sha256: 'b'.repeat(64) },
    { sequence: 3, kind: 'result', data: {}, created_at: '2026-11-04T01:00:20Z', receipt_sha256: 'c'.repeat(64) }], ...updates,
})
function apiMock(): PersonalMessagesApi & {
  createPersonalMessage: ReturnType<typeof vi.fn>; personalTools: ReturnType<typeof vi.fn>;
  personalMessages: ReturnType<typeof vi.fn>; personalMessage: ReturnType<typeof vi.fn>; acknowledgePersonalMessage: ReturnType<typeof vi.fn>
} {
  return {
    personalTools: vi.fn().mockResolvedValue({ tools }),
    personalMessages: vi.fn().mockResolvedValue({ messages: [message()] }),
    personalMessage: vi.fn().mockResolvedValue(message()),
    acknowledgePersonalMessage: vi.fn().mockResolvedValue(message({ state: 'acknowledged', acknowledged_at: '2026-11-04T01:00:30Z', acknowledged_by: 'user',
      receipts: [...message().receipts!, { sequence: 4, kind: 'acknowledged', data: { actor_kind: 'user' }, created_at: '2026-11-04T01:00:30Z', receipt_sha256: 'd'.repeat(64) }] })),
    createPersonalMessage: vi.fn().mockImplementation(async (body: PersonalMessageInput) => manualCollaboration(body)),
  }
}
const deferred = <T,>() => { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done }); return { promise, resolve } }
async function fillManual() {
  await waitFor(() => expect(screen.getByLabelText('消息发送方')).toBeEnabled())
  fireEvent.change(screen.getByLabelText('消息发送方'), { target: { value: agents[0].id } })
  fireEvent.change(screen.getByLabelText('消息接收方'), { target: { value: agents[1].id } })
  fireEvent.change(screen.getByLabelText('专员消息内容'), { target: { value: '请真实读取 budget.csv 并核算剩余预算。' } })
  fireEvent.change(screen.getByLabelText('消息共享上下文'), { target: { value: '仅本次指定资料。' } })
}

describe('explicit peer scopes', () => {
  it('does not grant unclassified/write tools by default and never changes profiles', () => {
    expect(defaultPeerTools(agents[0], tools)).toEqual(['file_read', 'personal_message'])
    expect(defaultPeerTools(agents[0], tools.map(tool => ({ name: tool.name, description: tool.description })))).toEqual(['personal_message'])
    const change = vi.fn()
    render(<PeerScopeEditor agents={agents} tools={tools} value={[]} onChange={change} />)
    fireEvent.click(screen.getByRole('checkbox', { name: '允许向预算核算员询问' }))
    expect(change).toHaveBeenLastCalledWith([{ agent_id: agents[1].id, agent_version: 2, tool_names: ['file_read'] }])
    expect(agents[1].tool_names).toEqual(['file_read', 'file_write'])
  })

  it('requires an explicit tool checkbox before permitting writes and freezes the grant version', () => {
    const grant: PersonalPeerGrant = { agent_id: agents[0].id, agent_version: 3, tool_names: ['file_read'] }
    const change = vi.fn()
    render(<PeerScopeEditor agents={agents} tools={tools} value={[grant]} onChange={change} />)
    fireEvent.click(screen.getByLabelText('工作统筹员的file_write权限'))
    expect(change).toHaveBeenLastCalledWith([{ ...grant, tool_names: ['file_read', 'file_write'] }])
    expect(screen.getByLabelText('工作统筹员的unknown_tool权限')).toBeDisabled()
  })

  it('does not silently update stale versions or expand the allowed tools', () => {
    const grant: PersonalPeerGrant = { agent_id: agents[0].id, agent_version: 2, tool_names: ['file_read'] }
    const change = vi.fn()
    render(<PeerScopeEditor agents={agents} tools={tools} value={[grant]} onChange={change} />)
    expect(change).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '更新授权版本' }))
    expect(change).toHaveBeenLastCalledWith([{ ...grant, agent_version: 3 }])
  })

  it('adds selected scopes to real collaboration submissions without copying unrelated context', async () => {
    const api = { personalTools: vi.fn().mockResolvedValue({ tools }), createPersonalCollaboration: vi.fn().mockImplementation(async body => collaboration({ request_id: body.request_id, source_session_id: body.source_session_id })) }
    render(<CollaborationComposer api={api} agents={agents} sourceSessionId="main-source" onBusy={vi.fn()} onCreated={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('步骤 1 专员'), { target: { value: agents[0].id } })
    fireEvent.change(screen.getByLabelText('步骤 1 任务'), { target: { value: '向核算员询问预算。' } })
    await waitFor(() => expect(screen.getByRole('checkbox', { name: '允许向预算核算员询问' })).toBeEnabled())
    fireEvent.click(screen.getByRole('checkbox', { name: '允许向预算核算员询问' }))
    fireEvent.click(screen.getByRole('button', { name: '开始协作' }))
    await waitFor(() => expect(api.createPersonalCollaboration).toHaveBeenCalledOnce())
    expect(api.createPersonalCollaboration.mock.calls[0][0].peer_agents).toEqual([{ agent_id: agents[1].id, agent_version: 2, tool_names: ['file_read'] }])
    expect(api.createPersonalCollaboration.mock.calls[0][0].context).toBe('')
  })
})

describe('manual persistent specialist messages', () => {
  it('submits the exact sender/recipient snapshots, explicit context and a stable request', async () => {
    const api = apiMock(), created = vi.fn()
    render(<ManualMessageComposer api={api} agents={agents} sourceSessionId="main-source" onBusy={vi.fn()} onCreated={created} />)
    await fillManual()
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
    await waitFor(() => expect(created).toHaveBeenCalledOnce())
    expect(api.createPersonalMessage.mock.calls[0][0]).toMatchObject({
      source_session_id: 'main-source', sender_agent_id: agents[0].id, sender_agent_version: 3,
      recipient_agent_id: agents[1].id, recipient_agent_version: 2,
      content: '请真实读取 budget.csv 并核算剩余预算。', context: '仅本次指定资料。',
      peer_agents: [{ agent_id: agents[1].id, agent_version: 2, tool_names: ['file_read'] }],
    })
    expect(api.createPersonalMessage.mock.calls[0][0].request_id).toBeTruthy()
    const submitted = api.createPersonalMessage.mock.calls[0][0]
    expect(created.mock.calls[0][0]).toMatchObject({
      request_id: `message-${createHash('sha256').update(submitted.request_id).digest('hex')}`,
      message_request_id: submitted.request_id,
    })
  })

  it('includes the sender in further-inquiry scope only after an explicit choice', async () => {
    const api = apiMock()
    render(<ManualMessageComposer api={api} agents={agents} sourceSessionId="main-source" onBusy={vi.fn()} onCreated={vi.fn()} />)
    await fillManual()
    expect(screen.getByRole('checkbox', { name: '允许对端继续询问发送方' })).not.toBeChecked()
    fireEvent.click(screen.getByRole('checkbox', { name: '允许对端继续询问发送方' }))
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
    await waitFor(() => expect(api.createPersonalMessage).toHaveBeenCalledOnce())
    expect(api.createPersonalMessage.mock.calls[0][0].peer_agents).toContainEqual({ agent_id: agents[0].id, agent_version: 3, tool_names: ['file_read', 'personal_message'] })
  })

  it('guards duplicate clicks and preserves drafts/request/source after an uncertain response', async () => {
    const api = apiMock(), pending = deferred<PersonalCollaboration>()
    api.createPersonalMessage.mockReturnValueOnce(pending.promise)
    const view = render(<ManualMessageComposer api={api} agents={agents} sourceSessionId="main-source" onBusy={vi.fn()} onCreated={vi.fn()} />)
    await fillManual()
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
    fireEvent.click(screen.getByRole('button', { name: '正在发送…' }))
    expect(api.createPersonalMessage).toHaveBeenCalledOnce()
    await act(async () => { pending.resolve(manualCollaboration(api.createPersonalMessage.mock.calls[0][0], { message_request_id: 'wrong-message-request' })); await pending.promise })
    expect(await screen.findByRole('alert')).toHaveTextContent('与本次请求不一致')
    expect(screen.getByLabelText('专员消息内容')).toHaveValue('请真实读取 budget.csv 并核算剩余预算。')
    expect(screen.getByLabelText('专员消息内容')).toBeDisabled()
    expect(screen.getByLabelText('消息发送方')).toBeDisabled()
    expect(screen.getByRole('checkbox', { name: '允许向预算核算员询问' })).toBeDisabled()
    view.rerender(<ManualMessageComposer api={api} agents={agents} sourceSessionId="newly-selected-source" onBusy={vi.fn()} onCreated={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
    await waitFor(() => expect(api.createPersonalMessage).toHaveBeenCalledTimes(2))
    expect(api.createPersonalMessage.mock.calls[1][0].source_session_id).toBe('main-source')
    expect(api.createPersonalMessage.mock.calls[1][0].request_id).toBe(api.createPersonalMessage.mock.calls[0][0].request_id)
    expect(api.createPersonalMessage.mock.calls[1][0]).toEqual(api.createPersonalMessage.mock.calls[0][0])
  })

  it.each(['missing', 'wrong'] as const)('rejects a %s message alias even when the root hash is correct', async mode => {
    const api = apiMock(), created = vi.fn()
    api.createPersonalMessage.mockImplementation(async (body: PersonalMessageInput) => manualCollaboration(body, {
      message_request_id: mode === 'missing' ? undefined : 'another-message-request',
    }))
    render(<ManualMessageComposer api={api} agents={agents} sourceSessionId="main-source" onBusy={vi.fn()} onCreated={created} />)
    await fillManual()
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('返回的消息任务与本次请求不一致')
    expect(created).not.toHaveBeenCalled()
    expect(screen.getByLabelText('专员消息内容')).toBeDisabled()
  })

  it('retries the exact pinned request after an asynchronous transport failure and accepts its verified alias', async () => {
    const api = apiMock(), created = vi.fn()
    api.createPersonalMessage.mockRejectedValueOnce(new Error('连接中断，发送状态待核实'))
    render(<ManualMessageComposer api={api} agents={agents} sourceSessionId="main-source" onBusy={vi.fn()} onCreated={created} />)
    await fillManual()
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('发送状态待核实')
    expect(created).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
    await waitFor(() => expect(created).toHaveBeenCalledOnce())
    expect(api.createPersonalMessage.mock.calls[1][0]).toEqual(api.createPersonalMessage.mock.calls[0][0])
    expect(created.mock.calls[0][0].message_request_id).toBe(api.createPersonalMessage.mock.calls[0][0].request_id)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('does not invent an unsaved source or a sender without the communication capability', async () => {
    const api = apiMock()
    const view = render(<ManualMessageComposer api={api} agents={agents} sourceSessionId={null} onBusy={vi.fn()} onCreated={vi.fn()} />)
    await fillManual()
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('已有对话')
    expect(api.createPersonalMessage).not.toHaveBeenCalled()
    view.unmount()
    render(<ManualMessageComposer api={api} agents={[agents[1]]} sourceSessionId="main-source" onBusy={vi.fn()} onCreated={vi.fn()} />)
    expect(screen.getByText('请先给发送方配置“专员互询”工具')).toBeVisible()
    expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled()
  })

  it('retains input but prevents stale profile versions from being silently submitted', async () => {
    const api = apiMock()
    const view = render(<ManualMessageComposer api={api} agents={agents} sourceSessionId="main-source" onBusy={vi.fn()} onCreated={vi.fn()} />)
    await fillManual()
    view.rerender(<ManualMessageComposer api={api} agents={[{ ...agents[0], version: 4 }, agents[1]]} sourceSessionId="main-source" onBusy={vi.fn()} onCreated={vi.fn()} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('专员配置已更新')
    expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled()
    expect(screen.getByLabelText('专员消息内容')).toHaveValue('请真实读取 budget.csv 并核算剩余预算。')
  })
})

describe('persistent two-sided conversations and receipts', () => {
  it('loads full bodies only on expansion, renders real actor versions and never auto-acknowledges', async () => {
    const api = apiMock()
    api.personalMessages.mockResolvedValue({ messages: [message({ content: '请求预览', response: '回应预览', content_truncated: true, response_truncated: true })] })
    render(<PeerConversations api={api} collaboration={collaboration()} />)
    const row = await screen.findByRole('button', { name: '查看工作统筹员与预算核算员的对话' })
    expect(api.personalMessage).not.toHaveBeenCalled()
    expect(api.acknowledgePersonalMessage).not.toHaveBeenCalled()
    fireEvent.click(row)
    const thread = await screen.findByLabelText('专员完整对话')
    await waitFor(() => expect(within(thread).getByText('825 元').tagName).toBe('STRONG'))
    expect(within(thread).getByText('v3')).toBeVisible()
    expect(within(thread).getByText('v2')).toBeVisible()
    expect(within(thread).getByText('对端已接收')).toBeVisible()
    expect(api.acknowledgePersonalMessage).not.toHaveBeenCalled()
  })

  it('sends an explicit response-hash acknowledgement and preserves the original conversation', async () => {
    const api = apiMock()
    render(<PeerConversations api={api} collaboration={collaboration()} />)
    fireEvent.click(await screen.findByRole('button', { name: '查看工作统筹员与预算核算员的对话' }))
    const ack = await screen.findByRole('button', { name: '确认收到' })
    fireEvent.click(ack)
    await waitFor(() => expect(api.acknowledgePersonalMessage).toHaveBeenCalledWith(message().id, { expected_response_sha256: 'e'.repeat(64) }))
    expect(await screen.findByLabelText('发送方原始请求')).toHaveTextContent('请核对指定文件')
    await waitFor(() => expect(screen.queryByRole('button', { name: '确认收到' })).not.toBeInTheDocument())
    expect(screen.getByText('用户确认收件')).toBeVisible()
    await waitFor(() => expect(api.personalMessage).toHaveBeenCalledTimes(2))
    expect(screen.getByText('用户确认收件')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: '刷新专员消息' }))
    await waitFor(() => expect(api.personalMessages).toHaveBeenCalledTimes(2))
    expect(screen.queryByRole('button', { name: '确认收到' })).not.toBeInTheDocument()
  })

  it('retains the known full result and reports an immutable result conflict', async () => {
    const api = apiMock()
    render(<PeerConversations api={api} collaboration={collaboration()} />)
    fireEvent.click(await screen.findByRole('button', { name: '查看工作统筹员与预算核算员的对话' }))
    await screen.findByRole('button', { name: '确认收到' })
    api.personalMessage.mockResolvedValue(message({ response: '被替换的回应', response_sha256: '1'.repeat(64) }))
    fireEvent.click(screen.getByRole('button', { name: '刷新专员消息' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('消息身份或结果摘要发生冲突')
    expect(screen.getByLabelText('接收方原始回应')).toHaveTextContent('825 元')
    expect(screen.getByLabelText('接收方原始回应')).not.toHaveTextContent('被替换的回应')
  })

  it('rejects empty message identities and malformed result digests', () => {
    expect(() => validatePersonalMessage(message({ id: '' }))).toThrow('专员消息返回不完整')
    expect(() => validatePersonalMessage(message({ receiver_session_id: ' ' }))).toThrow('专员消息返回不完整')
    expect(() => validatePersonalMessage(message({ response_sha256: 'malformed' }))).toThrow('专员消息返回不完整')
    expect(() => validatePersonalMessage(message({ state: 'acknowledged', acknowledged_at: '' }))).toThrow('专员消息返回不完整')
    expect(() => validatePersonalMessage(message({ receipts: [message().receipts![1]] }))).toThrow('专员消息返回不完整')
    expect(validatePersonalMessage(message({ state: 'max_turns', outcome: 'max_turns' })).state).toBe('max_turns')
  })

  it('keeps the full response and visible error when receipt confirmation fails', async () => {
    const api = apiMock()
    api.acknowledgePersonalMessage.mockRejectedValue(new Error('原回应摘要已变更，请重新核对'))
    render(<PeerConversations api={api} collaboration={collaboration()} />)
    fireEvent.click(await screen.findByRole('button', { name: '查看工作统筹员与预算核算员的对话' }))
    fireEvent.click(await screen.findByRole('button', { name: '确认收到' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('原回应摘要已变更')
    expect(screen.getByLabelText('接收方原始回应')).toHaveTextContent('825 元')
    expect(screen.getByRole('button', { name: '确认收到' })).toBeEnabled()
  })

  it('opens both real execution sessions without changing message identities', async () => {
    const api = apiMock(), open = vi.fn().mockResolvedValue(undefined)
    render(<PeerConversations api={api} collaboration={collaboration()} onOpenSession={open} />)
    fireEvent.click(await screen.findByRole('button', { name: '查看工作统筹员与预算核算员的对话' }))
    fireEvent.click(await screen.findByRole('button', { name: '查看发送方执行' }))
    await waitFor(() => expect(open).toHaveBeenCalledWith('sender-session'))
    await waitFor(() => expect(screen.getByRole('button', { name: '查看接收方执行' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '查看接收方执行' }))
    await waitFor(() => expect(open).toHaveBeenCalledWith('receiver-session'))
  })

  it('keeps UNKNOWN distinct and rejects mismatched/truncated detail instead of fabricating completion', async () => {
    const api = apiMock()
    api.personalMessages.mockResolvedValue({ messages: [message({ state: 'unknown', response: '', error: '执行中断，结果不明', outcome: '' })] })
    api.personalMessage.mockResolvedValue(message({ id: 'wrong-message' }))
    render(<PeerConversations api={api} collaboration={collaboration({ status: 'unknown' })} />)
    expect(await screen.findByText('结果待核实')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: '查看工作统筹员与预算核算员的对话' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('完整消息返回不完整')
    expect(screen.queryByRole('button', { name: '确认收到' })).not.toBeInTheDocument()
    expect(() => validatePersonalMessage(message({ response_truncated: true }), message().id, true)).toThrow('完整消息返回不完整')
    expect(() => validatePersonalMessage(message({ context_truncated: true }), message().id, true)).toThrow('完整消息返回不完整')
  })

  it('stops polling after the conversation component is closed', async () => {
    vi.useFakeTimers()
    const api = apiMock()
    api.personalMessages.mockResolvedValue({ messages: [message({ state: 'processing' })] })
    const view = render(<PeerConversations api={api} collaboration={collaboration({ status: 'running' })} />)
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(api.personalMessages).toHaveBeenCalledOnce()
    view.unmount()
    await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
    expect(api.personalMessages).toHaveBeenCalledOnce()
  })
})

describe('peer interaction binding', () => {
  const event = (data: Record<string, unknown> = {}): PersonalCollaborationEvent => ({ sequence: 1, key: 'peer-question', kind: 'question_requested', step_id: 'dispatch', data: { session_id: 'receiver-session', peer_message_id: message().id, ...data }, created_at: '2026-11-04T01:00:10Z' })
  const bound = () => collaboration({ status: 'running', steps: collaboration().steps.map(step => ({ ...step, status: 'running' })),
    peer_sessions: [{ message_id: message().id, session_id: 'receiver-session', agent_id: agents[1].id, agent_version: 2, step_id: 'dispatch', state: 'processing' }] })

  it('requires exact peer message/session/root identity, not just an existing parent step', () => {
    expect(matchesCollaborationExecution(bound(), event(), true)).toBe(true)
    expect(matchesCollaborationExecution(bound(), event({ session_id: 'other-session' }), true)).toBe(false)
    expect(matchesCollaborationExecution(bound(), event({ peer_message_id: 'other-message' }), true)).toBe(false)
    expect(matchesCollaborationExecution(bound(), { ...event(), step_id: 'other-root-step' }, true)).toBe(false)
    expect(matchesCollaborationExecution({ ...bound(), peer_sessions: [{ ...bound().peer_sessions![0], state: 'responded' }] }, event(), true)).toBe(false)
    expect(matchesCollaborationExecution({ ...bound(), peer_sessions: [{ ...bound().peer_sessions![0], step_id: 'different-step' }] }, event(), true)).toBe(false)
    expect(matchesCollaborationExecution({ ...bound(), messages: [message({ step_id: 'different-step' })] }, event(), true)).toBe(false)
  })

  it('shows and resolves a peer question with the real recipient name while rejecting foreign permissions', async () => {
    const base = apiMock(), item = bound(), requestId = `ask-${'1'.repeat(32)}`
    item.events = [{ ...event(), data: { ...event().data, actor_name: agents[1].name, request_id: requestId,
      header: '预算口径', question: '采用哪个月份？', options: [{ value: 'first', label: '一月' }, { value: 'second', label: '二月' }], allow_custom: false } },
    { ...event({ session_id: 'foreign-session', call_id: 'foreign', tool_name: 'file_write' }), key: 'foreign-permission', kind: 'permission_requested' }]
    const api: PersonalCollaborationsApi = { ...base, personalCollaborations: vi.fn().mockResolvedValue({ collaborations: [item] }), personalCollaboration: vi.fn().mockResolvedValue(item),
      createPersonalCollaboration: vi.fn(), cancelPersonalCollaboration: vi.fn(), resolvePermission: vi.fn(),
      replyPersonalCollaboration: vi.fn().mockResolvedValue({ status: 'answered', request_id: requestId, response: { kind: 'option', value: 'first' } }) }
    render(<PersonalCollaborations api={api} agents={agents} sourceSessionId="main-source" busy={false} onClose={vi.fn()} />)
    const section = await screen.findByRole('region', { name: '协作待回答问题' })
    expect(within(section).getByText(agents[1].name)).toBeVisible()
    fireEvent.click(within(section).getByRole('button', { name: '一月' }))
    await waitFor(() => expect(api.replyPersonalCollaboration).toHaveBeenCalledWith(item.id, requestId, { kind: 'option', value: 'first' }))
    expect(screen.queryByRole('button', { name: '允许本次操作' })).not.toBeInTheDocument()
    expect(api.resolvePermission).not.toHaveBeenCalled()
  })
})

describe('composer continuity', () => {
  it('keeps workflow and manual drafts separate while changing the visible composer', async () => {
    const api: PersonalCollaborationsApi = { ...apiMock(), personalCollaborations: vi.fn().mockResolvedValue({ collaborations: [] }),
      personalCollaboration: vi.fn(), createPersonalCollaboration: vi.fn(), cancelPersonalCollaboration: vi.fn(),
      replyPersonalCollaboration: vi.fn(), resolvePermission: vi.fn() }
    render(<PersonalCollaborations api={api} agents={agents} sourceSessionId="main-source" busy={false} onClose={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: '发起协作' }))
    fireEvent.change(screen.getByLabelText('协作任务名称'), { target: { value: '保留的协作草稿' } })
    fireEvent.click(screen.getByRole('button', { name: '专员消息' }))
    await fillManual()
    fireEvent.click(screen.getByRole('button', { name: '发起协作' }))
    expect(screen.getByLabelText('协作任务名称')).toHaveValue('保留的协作草稿')
    expect(screen.getByLabelText('协作任务名称')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: '专员消息' }))
    expect(screen.getByLabelText('专员消息内容')).toHaveValue('请真实读取 budget.csv 并核算剩余预算。')
    expect(screen.getByLabelText('专员消息内容')).toBeVisible()
    expect(api.createPersonalMessage).not.toHaveBeenCalled()
  })
})
