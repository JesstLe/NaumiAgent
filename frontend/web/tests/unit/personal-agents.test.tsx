import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { PersonalAgentEditor, PersonalAgents } from '../../../web2/src/personal/PersonalAgents'
import { usePersonalAgents, type PersonalAgentsApi } from '../../../web2/src/personal/usePersonalAgents'
import type { PersonalAgent } from '@naumi/shared/api/WorkbenchRuntimeClient'

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', '') }
  HTMLDialogElement.prototype.close = function () { this.removeAttribute('open') }
})
afterEach(cleanup)

const agent = (changes: Partial<PersonalAgent> = {}): PersonalAgent => ({
  id: 'pa_travel', name: '旅行搭档', description: '安排行程', instructions: '根据我的时间和预算整理行程。',
  model_tier: 'capable', tool_names: ['file_read'], kind: 'specialist', member_ids: [],
  status: 'active', version: 3, created_at: '2026-11-03T00:00:00Z', updated_at: '2026-11-03T01:00:00Z', ...changes,
})
function apiMock(agents: PersonalAgent[] = []) {
  return {
    personalAgents: vi.fn().mockResolvedValue({ agents }),
    personalTools: vi.fn().mockResolvedValue({ tools: [{ name: 'file_read', description: '读取指定文件' }, { name: 'web_search', description: '搜索公开网页' }] }),
    createPersonalAgent: vi.fn().mockResolvedValue(agent()),
    updatePersonalAgent: vi.fn().mockResolvedValue(agent({ version: 4 })),
    personalAgentHistory: vi.fn().mockResolvedValue({ revisions: [agent(), agent({ version: 2 })] }),
    openPersonalSession: vi.fn().mockResolvedValue({ session_id: 'session-travel', agent_id: 'pa_travel', version: 3 }),
    currentPersonalAgent: vi.fn().mockResolvedValue({ agent: null }),
  } satisfies PersonalAgentsApi
}
function Directory({ api, busy = false, sessionId = null, onSelect = vi.fn().mockResolvedValue(undefined) }: {
  api: PersonalAgentsApi; busy?: boolean; sessionId?: string | null; onSelect?: (id: string | null) => Promise<unknown>
}) {
  const state = usePersonalAgents({ api, busy, sessionId, connected: true, onSelect })
  return <PersonalAgents api={api} state={state} busy={busy} connected />
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

describe('personal agent directory', () => {
  it('starts with only the main assistant and never seeds role templates', async () => {
    const api = apiMock()
    render(<Directory api={api} />)
    await waitFor(() => expect(screen.queryByText('正在加载…')).not.toBeInTheDocument())
    expect(screen.getByRole('button', { name: '主助理' })).toBeVisible()
    expect(screen.getByRole('button', { name: '创建助理' })).toBeEnabled()
    expect(screen.queryByText(/运营|投资|研究专员/)).not.toBeInTheDocument()
    expect(api.createPersonalAgent).not.toHaveBeenCalled()
  })

  it('opens the backend-bound session and disables switching while running', async () => {
    const api = apiMock([agent()]), select = vi.fn().mockResolvedValue(undefined)
    const { rerender } = render(<Directory api={api} onSelect={select} />)
    fireEvent.click(await screen.findByRole('button', { name: '与旅行搭档对话' }))
    await waitFor(() => expect(select).toHaveBeenCalledWith('session-travel'))
    expect(api.openPersonalSession).toHaveBeenCalledWith('pa_travel')
    rerender(<Directory api={api} onSelect={select} busy />)
    expect(screen.getByRole('button', { name: '主助理' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '与旅行搭档对话' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '创建助理' })).toBeDisabled()
  })

  it('shows real list errors with retry rather than silently pretending the directory is empty', async () => {
    const api = apiMock([agent()])
    api.personalAgents.mockRejectedValueOnce(new Error('目录暂时不可用'))
    render(<Directory api={api} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('目录暂时不可用')
    fireEvent.click(screen.getByRole('button', { name: '重试', exact: true }))
    expect(await screen.findByRole('button', { name: '与旅行搭档对话' })).toBeVisible()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('refreshes role creation from natural-language execution only after the run finishes', async () => {
    const api = apiMock()
    const { rerender } = render(<Directory api={api} />)
    await waitFor(() => expect(api.personalAgents).toHaveBeenCalledTimes(1))
    rerender(<Directory api={api} busy />)
    expect(api.personalAgents).toHaveBeenCalledTimes(1)
    api.personalAgents.mockResolvedValue({ agents: [agent()] })
    rerender(<Directory api={api} busy={false} />)
    expect(await screen.findByRole('button', { name: '与旅行搭档对话' })).toBeVisible()
    expect(api.personalAgents).toHaveBeenCalledTimes(2)
  })

  it('does not display stale agent identity when sessions change', async () => {
    const api = apiMock(), first = deferred<{ agent: PersonalAgent | null }>(), second = deferred<{ agent: PersonalAgent | null }>()
    api.currentPersonalAgent.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const { result, rerender } = renderHook(({ sessionId }) => usePersonalAgents({ api, sessionId, busy: false, connected: true, onSelect: vi.fn() }), { initialProps: { sessionId: 'old' } })
    rerender({ sessionId: 'new' })
    await act(async () => { first.resolve({ agent: agent() }); await first.promise })
    expect(result.current.current).toBeNull()
    expect(result.current.currentPending).toBe(true)
    await act(async () => { second.resolve({ agent: null }); await second.promise })
    expect(result.current.currentPending).toBe(false)
    expect(result.current.current).toBeNull()
  })

  it('preserves a current-identity failure instead of misidentifying it as the main assistant', async () => {
    const api = apiMock()
    api.currentPersonalAgent.mockRejectedValue(new Error('身份验证失败'))
    const { result } = renderHook(() => usePersonalAgents({ api, sessionId: 'session', busy: false, connected: true, onSelect: vi.fn() }))
    await waitFor(() => expect(result.current.currentError).toBe('身份验证失败'))
    expect(result.current.currentPending).toBe(false)
  })

  it('rejects a mismatched binding and avoids double-opening on rapid clicks', async () => {
    const api = apiMock([agent()]), select = vi.fn(), pending = deferred<{ session_id: string; agent_id: string; version: number }>()
    api.openPersonalSession.mockReturnValue(pending.promise)
    render(<Directory api={api} onSelect={select} />)
    const button = await screen.findByRole('button', { name: '与旅行搭档对话' })
    fireEvent.click(button); fireEvent.click(button)
    expect(api.openPersonalSession).toHaveBeenCalledTimes(1)
    await act(async () => { pending.resolve({ session_id: 'wrong', agent_id: 'other-agent', version: 1 }) })
    expect(await screen.findByRole('alert')).toHaveTextContent('会话与助理不匹配')
    expect(select).not.toHaveBeenCalled()
  })

  it('keeps paused agents read-only and reveals archived agents only on request', async () => {
    const api = apiMock([agent({ status: 'paused' }), agent({ id: 'pa_archived', name: '旧助手', status: 'archived' })])
    render(<Directory api={api} />)
    expect(await screen.findByRole('button', { name: '与旅行搭档对话' })).toBeDisabled()
    expect(screen.queryByRole('button', { name: '编辑旧助手' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '已归档' }))
    expect(screen.getByRole('button', { name: '编辑旧助手' })).toBeEnabled()
    expect(screen.getByRole('button', { name: '与旧助手对话' })).toBeDisabled()
  })
})

describe('personal agent editor', () => {
  it('creates an arbitrary user-defined role with an explicit tool allowlist', async () => {
    const api = apiMock(), saved = vi.fn().mockResolvedValue(undefined)
    render(<PersonalAgentEditor api={api} agent={null} agents={[]} busy={false} onClose={vi.fn()} onSaved={saved} />)
    fireEvent.change(screen.getByLabelText('名称'), { target: { value: '旅行搭档' } })
    fireEvent.change(screen.getByLabelText('职责与工作方式'), { target: { value: '按我的预算整理旅行计划。' } })
    fireEvent.click(await screen.findByRole('checkbox', { name: /web_search/ }))
    fireEvent.click(screen.getByRole('button', { name: '创建', exact: true }))
    await waitFor(() => expect(saved).toHaveBeenCalled())
    expect(api.createPersonalAgent).toHaveBeenCalledWith(expect.objectContaining({
      name: '旅行搭档', instructions: '按我的预算整理旅行计划。', kind: 'specialist',
      tool_names: ['web_search'], member_ids: [], model_tier: 'capable', request_id: expect.any(String),
    }))
  })

  it('retains the request id on retry after an uncertain create result', async () => {
    const api = apiMock()
    api.createPersonalAgent.mockRejectedValueOnce(new Error('连接中断，请重试'))
    render(<PersonalAgentEditor api={api} agent={null} agents={[]} busy={false} onClose={vi.fn()} onSaved={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('名称'), { target: { value: '生活助手' } })
    fireEvent.change(screen.getByLabelText('职责与工作方式'), { target: { value: '整理我的日常资料。' } })
    await waitFor(() => expect(screen.getByRole('button', { name: '创建', exact: true })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '创建', exact: true }))
    expect(await screen.findByRole('alert')).toHaveTextContent('连接中断')
    fireEvent.change(screen.getByLabelText('简介'), { target: { value: '更新后的简介' } })
    fireEvent.click(screen.getByRole('button', { name: '创建', exact: true }))
    await waitFor(() => expect(api.createPersonalAgent).toHaveBeenCalledTimes(2))
    expect(api.createPersonalAgent.mock.calls[0][0].request_id).toEqual(api.createPersonalAgent.mock.calls[1][0].request_id)
    expect(api.createPersonalAgent.mock.calls[0][0].tool_names).toEqual([])
  })

  it('sends the selected version and preserves edits when concurrent modification conflicts', async () => {
    const api = apiMock(), saved = vi.fn()
    api.updatePersonalAgent.mockRejectedValue(new Error('助理已被更新，请重新打开后修改'))
    render(<PersonalAgentEditor api={api} agent={agent()} agents={[agent()]} busy={false} onClose={vi.fn()} onSaved={saved} />)
    fireEvent.change(screen.getByLabelText('名称'), { target: { value: '家庭旅行' } })
    await waitFor(() => expect(screen.getByRole('button', { name: '保存修改' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('助理已被更新')
    expect(screen.getByLabelText('名称')).toHaveValue('家庭旅行')
    expect(api.updatePersonalAgent).toHaveBeenCalledWith('pa_travel', expect.objectContaining({ expected_version: 3, name: '家庭旅行' }))
    expect(api.updatePersonalAgent.mock.calls[0][1]).not.toHaveProperty('kind')
    expect(screen.getByLabelText('类型')).toBeDisabled()
    expect(saved).not.toHaveBeenCalled()
  })

  it('requires archive confirmation and reads real version history', async () => {
    const api = apiMock()
    render(<PersonalAgentEditor api={api} agent={agent()} agents={[agent()]} busy={false} onClose={vi.fn()} onSaved={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: '版本记录' }))
    expect(await screen.findByRole('list', { name: '助理版本记录' })).toHaveTextContent('v2')
    expect(api.personalAgentHistory).toHaveBeenCalledWith('pa_travel')
    fireEvent.click(screen.getByRole('button', { name: '归档助理' }))
    expect(api.updatePersonalAgent).not.toHaveBeenCalled()
    const confirmation = screen.getByRole('group', { name: '确认状态变更' })
    fireEvent.click(within(confirmation).getByRole('button', { name: '确认归档' }))
    await waitFor(() => expect(api.updatePersonalAgent).toHaveBeenCalledWith('pa_travel', { expected_version: 3, status: 'archived' }))
  })

  it('blocks saving if tools failed to load and recovers on retry', async () => {
    const api = apiMock()
    api.personalTools.mockRejectedValueOnce(new Error('工具服务未连接'))
    render(<PersonalAgentEditor api={api} agent={agent()} agents={[agent()]} busy={false} onClose={vi.fn()} onSaved={vi.fn()} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('工具服务未连接')
    expect(screen.getByRole('button', { name: '保存修改' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '重试加载工具' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '保存修改' })).toBeEnabled())
  })

  it('creates teams from explicitly selected existing agents without implicit role generation', async () => {
    const api = apiMock()
    render(<PersonalAgentEditor api={api} agent={null} agents={[agent()]} busy={false} onClose={vi.fn()} onSaved={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('名称'), { target: { value: '家庭出行' } })
    fireEvent.change(screen.getByLabelText('职责与工作方式'), { target: { value: '协调行程与预算。' } })
    fireEvent.change(screen.getByLabelText('类型'), { target: { value: 'team' } })
    fireEvent.click(screen.getByRole('checkbox', { name: '旅行搭档' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '创建', exact: true })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '创建', exact: true }))
    await waitFor(() => expect(api.createPersonalAgent).toHaveBeenCalledWith(expect.objectContaining({ kind: 'team', member_ids: ['pa_travel'] })))
  })

  it('lets a team remove a paused member instead of hiding the invalid selection', async () => {
    const api = apiMock()
    const team = agent({ id: 'pa_team', name: '出行组', kind: 'team', member_ids: ['pa_travel'] })
    render(<PersonalAgentEditor api={api} agent={team} agents={[team, agent({ status: 'paused' })]} busy={false} onClose={vi.fn()} onSaved={vi.fn()} />)
    const member = screen.getByRole('checkbox', { name: '旅行搭档 · 已暂停' })
    expect(member).toBeChecked()
    fireEvent.click(member)
    await waitFor(() => expect(screen.getByRole('button', { name: '保存修改' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }))
    await waitFor(() => expect(api.updatePersonalAgent).toHaveBeenCalledWith('pa_team', expect.objectContaining({ member_ids: [] })))
  })
})
