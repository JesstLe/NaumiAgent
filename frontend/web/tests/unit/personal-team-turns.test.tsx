import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  PersonalAgent, PersonalCollaboration, PersonalCollaborationEvent, PersonalTeamTurn, PersonalTeamTurnInput,
} from '@naumi/shared/api/WorkbenchRuntimeClient'
import { PersonalAgentEditor } from '../../../web2/src/personal/PersonalAgents'
import { PersonalCollaborations, type PersonalCollaborationsApi } from '../../../web2/src/personal/Collaborations'
import { TeamComposerControls, TeamTurnCard } from '../../../web2/src/personal/TeamConversation'
import { resolveTeamRoster, retainTeamTurn, useTeamTurns, validateTeamTurn, type TeamTurnsApi } from '../../../web2/src/personal/useTeamTurns'

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', '') }
  HTMLDialogElement.prototype.close = function () { this.removeAttribute('open') }
})
beforeEach(() => localStorage.clear())
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks() })
const id = (letter: string) => `pa_${letter.repeat(32)}`
const team: PersonalAgent = { id: id('a'), name: '资料协作组', description: '', instructions: '按明确资料核对预算。',
  kind: 'team', member_ids: [id('b')], model_tier: 'fast', tool_names: ['personal_team'], status: 'active', version: 3,
  created_at: '2026-11-04T01:00:00Z', updated_at: '2026-11-04T01:00:00Z' }
const member: PersonalAgent = { ...team, id: id('b'), name: '预算核算员', kind: 'specialist', member_ids: [],
  tool_names: ['file_read', 'file_write', 'personal_message'], version: 2 }
const catalog = [team, member]
const tools = [{ name: 'file_read', description: '读取资料', read_only: true }, { name: 'file_write', description: '写入文件', read_only: false },
  { name: 'personal_message', description: '专员互询', read_only: false }, { name: 'personal_team', description: '协调团队任务', read_only: false, kinds: ['team' as const] }]
const turn = (updates: Partial<PersonalTeamTurn> = {}): PersonalTeamTurn => ({ id: `pt_${'c'.repeat(32)}`, request_id: 'team-request', team_id: team.id,
  team_version: 3, team_name: team.name, source_session_id: 'team-daily', team_session_id: 'team-daily', source_user_message_id: 'user-one', result_message_id: '',
  content: '核对预算', context: '', created_at: '2026-11-04T01:00:00Z', updated_at: '2026-11-04T01:00:00Z', cancel_requested: false,
  status: 'queued', response: '', error: '', result_sha256: '', usage: { total_tokens: 0, total_cost_usd: 0, job_count: 0 },
  roster: { teams: [{ agent_id: team.id, agent_version: 3, member_ids: [member.id] }], agents: [{ agent_id: member.id, agent_version: 2, tool_names: ['file_read'] }] }, phases: [], ...updates })
const accepted = (body: PersonalTeamTurnInput, updates: Partial<PersonalTeamTurn> = {}) => turn({ request_id: body.request_id,
  team_id: body.team_id, team_version: body.team_version, source_session_id: body.source_session_id, team_session_id: body.source_session_id,
  content: body.content, context: body.context || '', ...updates })
const deferred = <T,>() => { let resolve!: (value: T) => void; let reject!: (value: Error) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail }); return { promise, resolve, reject } }
function apiMock() {
  return { personalTools: vi.fn().mockResolvedValue({ tools }), personalTeamTurns: vi.fn().mockResolvedValue({ turns: [] }),
    personalTeamTurn: vi.fn().mockResolvedValue(turn()), createPersonalTeamTurn: vi.fn().mockImplementation(async body => accepted(body)),
    cancelPersonalTeamTurn: vi.fn().mockImplementation(async () => turn({ cancel_requested: true })),
    personalCollaborations: vi.fn().mockResolvedValue({ collaborations: [] }), personalCollaboration: vi.fn(),
    cancelPersonalCollaboration: vi.fn(), replyPersonalCollaboration: vi.fn(), resolvePermission: vi.fn().mockResolvedValue({}),
    personalMessages: vi.fn().mockResolvedValue({ messages: [] }), personalMessage: vi.fn(), acknowledgePersonalMessage: vi.fn(), createPersonalMessage: vi.fn(),
  }
}
const settings = (api: TeamTurnsApi) => ({ api, namespace: 'http://team-test/api', team, sessionId: 'team-daily', catalog, connected: true })
async function ready(result: { current: ReturnType<typeof useTeamTurns> }) {
  await waitFor(() => expect(result.current.loading).toBe(false))
  await waitFor(() => expect(result.current.toolsLoading).toBe(false))
}
const collaboration = (updates: Partial<PersonalCollaboration> = {}): PersonalCollaboration => ({
  id: `pc_${'d'.repeat(32)}`, request_id: 'phase-request', source_session_id: 'phase-source', source_agent_id: team.id,
  title: '成员执行', context: '', created_at: team.created_at, cancel_requested: false, status: 'running',
  steps: [{ id: 'read', agent_id: member.id, agent_version: 2, task: '读取预算', depends_on: [], job_id: 'member-job', session_id: 'member-session',
    run_id: 'member-run', status: 'running', response: '', error: '', agent_name: member.name }], events: [], ...updates,
})

describe('team admission identity and scope', () => {
  it('submits exact team/version and sources with only classified read tools plus configured messaging', async () => {
    const api = apiMock(), onAccepted = vi.fn(), refresh = vi.fn().mockResolvedValue(true)
    const { result } = renderHook(() => useTeamTurns({ ...settings(api), onAccepted, onConversationRefresh: refresh }))
    await ready(result)
    await act(async () => { expect(await result.current.submit('  核对预算  ', ['source-one'])).toBe(true) })
    expect(api.createPersonalTeamTurn).toHaveBeenCalledTimes(1)
    expect(api.createPersonalTeamTurn.mock.calls[0][0]).toMatchObject({ source_session_id: 'team-daily', team_id: team.id, team_version: 3,
      content: '核对预算', context: '', source_ids: ['source-one'], member_grants: [{ agent_id: member.id, agent_version: 2, tool_names: ['file_read', 'personal_message'] }] })
    expect(onAccepted).toHaveBeenCalledWith(api.createPersonalTeamTurn.mock.calls[0][0], 'http://team-test/api')
    await waitFor(() => expect(result.current.turns).toHaveLength(1))
  })
  it('routes an empty-tool team through persistent admission without granting coordination', async () => {
    const api = apiMock()
    const { result } = renderHook(() => useTeamTurns({ ...settings(api), team: { ...team, tool_names: [], member_ids: [] } }))
    await ready(result)
    await act(async () => { expect(await result.current.submit('讨论计划')).toBe(true) })
    expect(api.personalTools).not.toHaveBeenCalled()
    expect(api.createPersonalTeamTurn.mock.calls[0][0]).not.toHaveProperty('member_grants')
  })
  it('freezes the entire original body after an uncertain POST and reuses it after edits', async () => {
    const api = apiMock()
    api.createPersonalTeamTurn.mockRejectedValueOnce(new Error('连接中断，原请求待核对'))
    const { result } = renderHook(() => useTeamTurns(settings(api)))
    await ready(result)
    act(() => result.current.setContext('原共享资料'))
    await act(async () => { expect(await result.current.submit('原问题', ['original-source'])).toBe(false) })
    const original = api.createPersonalTeamTurn.mock.calls[0][0]
    act(() => { result.current.setContext('另一份资料'); result.current.setGrants([]) })
    await act(async () => { expect(await result.current.submit('修改后的问题', ['different-source'])).toBe(true) })
    expect(api.createPersonalTeamTurn.mock.calls[1][0]).toEqual(original)
  })
  it('persists an uncertain identity across remounts and never infers rejection from an empty list', async () => {
    const api = apiMock()
    api.createPersonalTeamTurn.mockRejectedValueOnce(new Error('503 服务暂不可用'))
    const first = renderHook(() => useTeamTurns(settings(api)))
    await ready(first.result)
    await act(async () => { await first.result.current.submit('持久原请求') })
    const body = api.createPersonalTeamTurn.mock.calls[0][0]
    first.unmount()
    const second = renderHook(() => useTeamTurns(settings(api)))
    await ready(second.result)
    expect(second.result.current.pending).toEqual(body)
    await act(async () => { await second.result.current.submit('') })
    expect(api.createPersonalTeamTurn.mock.calls[1][0]).toEqual(body)
  })
  it('prevents duplicate in-flight POSTs and ignores an accepted reply after switching team session', async () => {
    const api = apiMock(), response = deferred<PersonalTeamTurn>(), acceptedCallback = vi.fn()
    api.createPersonalTeamTurn.mockReturnValueOnce(response.promise)
    const { result, rerender } = renderHook(({ sessionId }) => useTeamTurns({ ...settings(api), sessionId, onAccepted: acceptedCallback }), { initialProps: { sessionId: 'team-daily' } })
    await ready(result)
    let pending!: Promise<boolean>
    act(() => { pending = result.current.submit('原团队问题'); void result.current.submit('重复发送') })
    expect(api.createPersonalTeamTurn).toHaveBeenCalledTimes(1)
    const body = api.createPersonalTeamTurn.mock.calls[0][0]
    rerender({ sessionId: 'other-team-daily' })
    await act(async () => { response.resolve(accepted(body)); expect(await pending).toBe(false) })
    expect(result.current.turns).toEqual([])
    expect(result.current.pending).toBeNull()
    expect(acceptedCallback).toHaveBeenCalledWith(body, 'http://team-test/api')
  })
  it('rejects a wrong returned team/session/request and preserves the original request for reconciliation', async () => {
    const api = apiMock()
    api.createPersonalTeamTurn.mockImplementation(async body => accepted(body, { team_session_id: 'someone-else' }))
    const { result } = renderHook(() => useTeamTurns(settings(api)))
    await ready(result)
    await act(async () => { expect(await result.current.submit('原问题')).toBe(false) })
    expect(result.current.pending?.content).toBe('原问题')
    expect(result.current.turns).toEqual([])
    expect(result.current.error).toMatch(/来源不匹配/)
  })
  it('reconciles an uncertain POST only with the same original request identity in real returned records', async () => {
    const api = apiMock(), onAccepted = vi.fn()
    api.createPersonalTeamTurn.mockRejectedValueOnce(new Error('响应不明'))
    const { result } = renderHook(() => useTeamTurns({ ...settings(api), onAccepted }))
    await ready(result)
    await act(async () => { await result.current.submit('原问题') })
    const body = api.createPersonalTeamTurn.mock.calls[0][0]
    api.personalTeamTurns.mockResolvedValue({ turns: [accepted(body)] })
    act(() => result.current.refresh())
    await waitFor(() => expect(result.current.pending).toBeNull())
    expect(onAccepted).toHaveBeenCalledTimes(1)
    expect(api.createPersonalTeamTurn).toHaveBeenCalledTimes(1)
  })
  it('allows explicit removal of a member and never silently upgrades a selected grant version', async () => {
    const api = apiMock()
    const { result, rerender } = renderHook(({ profiles }) => useTeamTurns({ ...settings(api), catalog: profiles }), { initialProps: { profiles: catalog } })
    await ready(result)
    act(() => result.current.setGrants([{ agent_id: member.id, agent_version: 2, tool_names: [] }]))
    rerender({ profiles: [team, { ...member, version: 4, tool_names: ['file_read', 'file_write'] }] })
    expect(result.current.staleGrants).toBe(true)
    await act(async () => { expect(await result.current.submit('旧授权问题')).toBe(false) })
    expect(api.createPersonalTeamTurn).not.toHaveBeenCalled()
    act(() => result.current.setGrants([]))
    await act(async () => { expect(await result.current.submit('不分配成员')).toBe(true) })
    expect(api.createPersonalTeamTurn.mock.calls[0][0].member_grants).toEqual([])
  })
  it.each([
    ['paused', 'empty'], ['paused', 'subset'], ['archived', 'empty'], ['archived', 'subset'],
  ] as const)('scope C: admits %s leaves outside the explicit %s grant', async (status, selection) => {
    const api = apiMock()
    const excluded: PersonalAgent = { ...member, id: id('e'), name: '未授权成员', status }
    const nested: PersonalAgent = { ...team, id: id('d'), member_ids: [member.id, excluded.id] }
    const root: PersonalAgent = { ...team, member_ids: [nested.id, excluded.id] }
    const { result } = renderHook(() => useTeamTurns({ ...settings(api), team: root, catalog: [root, nested, member, excluded] }))
    await ready(result)
    act(() => result.current.setGrants(selection === 'empty' ? [] : [{ agent_id: member.id, agent_version: member.version, tool_names: [] }]))
    expect(result.current.roster.teams.map(value => value.id)).toEqual([root.id, nested.id])
    expect(result.current.roster.errors).toEqual([])
    expect(result.current.staleGrants).toBe(false)
    expect(result.current.canSubmit).toBe(true)
    await act(async () => { expect(await result.current.submit('只使用本次授权')).toBe(true) })
    expect(api.createPersonalTeamTurn).toHaveBeenCalledTimes(1)
    expect(api.createPersonalTeamTurn.mock.calls[0][0].member_grants).toEqual(selection === 'empty' ? []
      : [{ agent_id: member.id, agent_version: member.version, tool_names: [] }])
  })
  it.each([
    ['paused', 'root'], ['paused', 'nested'], ['archived', 'root'], ['archived', 'nested'],
  ] as const)('scope C: blocks the %s %s team even with an empty grant', async (status, target) => {
    const api = apiMock()
    const nested: PersonalAgent = { ...team, id: id('e'), member_ids: [member.id], status: target === 'nested' ? status : 'active' }
    const root: PersonalAgent = { ...team, member_ids: [nested.id], status: target === 'root' ? status : 'active' }
    const { result } = renderHook(() => useTeamTurns({ ...settings(api), team: root, catalog: [root, nested, member] }))
    await ready(result)
    act(() => result.current.setGrants([]))
    expect(result.current.roster.errors).toHaveLength(1)
    expect(result.current.canSubmit).toBe(false)
    await act(async () => { expect(await result.current.submit('停用团队任务')).toBe(false) })
    expect(api.createPersonalTeamTurn).not.toHaveBeenCalled()
  })
  it('hydrates an older message-linked task outside the bounded list without dropping its identity', async () => {
    const api = apiMock(), old = turn({ id: `pt_${'f'.repeat(32)}`, status: 'completed', response: '历史预算结果825',
      result_message_id: 'old-delivery', result_sha256: 'e'.repeat(64), created_at: '2026-10-01T01:00:00Z' })
    api.personalTeamTurns.mockResolvedValue({ turns: [] })
    api.personalTeamTurn.mockResolvedValue(old)
    const { result } = renderHook(() => useTeamTurns({ ...settings(api), linkedTurnIds: [old.id, old.id, 'untrusted-text-not-an-id'] }))
    await ready(result)
    expect(result.current.turns).toEqual([old])
    expect(api.personalTeamTurn).toHaveBeenCalledExactlyOnceWith(old.id)
    act(() => result.current.refresh())
    await waitFor(() => expect(api.personalTeamTurns).toHaveBeenCalledTimes(2))
    expect(api.personalTeamTurn).toHaveBeenCalledTimes(1)
    expect(result.current.turns[0].id).toBe(old.id)
  })
})

describe('team records and roster integrity', () => {
  it('rejects incomplete completion receipts and source substitutions', () => {
    expect(() => validateTeamTurn(turn({ status: 'completed' }), { teamId: team.id, sessionId: 'team-daily' })).toThrow(/交付凭据/)
    expect(() => validateTeamTurn(turn({ team_id: member.id }), { teamId: team.id, sessionId: 'team-daily' })).toThrow(/来源不匹配/)
  })
  it('keeps verified terminal results against stale polling and rejects conflicting results', () => {
    const complete = turn({ status: 'completed', result_message_id: 'delivered', result_sha256: 'f'.repeat(64), response: '825' })
    expect(retainTeamTurn(complete, turn())).toEqual(complete)
    expect(() => retainTeamTurn(complete, { ...complete, response: '另一个结果' })).toThrow(/摘要发生冲突/)
    expect(() => retainTeamTurn(complete, { ...complete, roster: { ...complete.roster, agents: [] } })).toThrow(/身份/)
    expect(retainTeamTurn(turn({ status: 'unknown' }), complete)).toEqual(complete)
  })
  it('recursively resolves only explicitly configured existing members and detects cycles/missing profiles', () => {
    const nested = { ...team, id: id('e'), member_ids: [member.id] }
    const root = { ...team, member_ids: [nested.id, member.id] }
    const result = resolveTeamRoster(root, [root, nested, member])
    expect(result.teams.map(value => value.id)).toEqual([root.id, nested.id])
    expect(result.members.map(value => value.id)).toEqual([member.id])
    expect(result.errors).toEqual([])
    expect(resolveTeamRoster({ ...team, member_ids: [id('f')] }, catalog).errors[0]).toMatch(/暂不可用/)
    expect(resolveTeamRoster({ ...team, member_ids: [team.id] }, catalog).errors).toEqual(['团队成员关系形成循环'])
  })
  it.each(['paused', 'archived'] as const)('scope C: retains a %s leaf without a global roster error', status => {
    const excluded: PersonalAgent = { ...member, status }
    const nested: PersonalAgent = { ...team, id: id('e'), member_ids: [member.id] }
    const root: PersonalAgent = { ...team, member_ids: [nested.id, member.id] }
    const resolved = resolveTeamRoster(root, [root, nested, excluded])
    expect(resolved.teams).toEqual([root, nested])
    expect(resolved.members).toEqual([excluded])
    expect(resolved.errors).toEqual([])
  })
})

describe('team visible operations', () => {
  it.each(['paused', 'archived'] as const)('scope C: blocks a selected %s leaf until the user deselects it', async status => {
    const api = apiMock()
    function Harness({ profiles }: { profiles: PersonalAgent[] }) {
      const state = useTeamTurns({ ...settings(api), catalog: profiles })
      return <><button disabled={!state.canSubmit} onClick={() => void state.submit('调整授权后的任务')}>提交范围测试</button>
        <TeamComposerControls team={team} state={state} /></>
    }
    const view = render(<Harness profiles={catalog} />)
    const selection = await screen.findByLabelText('允许分配给预算核算员')
    await waitFor(() => expect(screen.getByLabelText('预算核算员的file_read权限')).toBeEnabled())
    // Persist an explicit selection rather than relying on the active-only default.
    fireEvent.click(selection)
    fireEvent.click(selection)
    expect(selection).toBeChecked()
    view.rerender(<Harness profiles={[team, { ...member, status }]} />)
    expect(screen.getByLabelText('允许分配给预算核算员')).toBeChecked()
    expect(screen.getByRole('button', { name: '提交范围测试' })).toBeDisabled()
    expect(screen.getByLabelText('预算核算员的file_read权限')).toBeDisabled()
    expect(api.createPersonalTeamTurn).not.toHaveBeenCalled()
    fireEvent.click(screen.getByLabelText('允许分配给预算核算员'))
    expect(screen.getByRole('button', { name: '提交范围测试' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: '提交范围测试' }))
    await waitFor(() => expect(api.createPersonalTeamTurn).toHaveBeenCalledTimes(1))
    expect(api.createPersonalTeamTurn.mock.calls[0][0].member_grants).toEqual([])
  })
  it('requires explicit confirmation to abandon retry, without cancelling an accepted task', async () => {
    const api = apiMock()
    api.createPersonalTeamTurn.mockRejectedValueOnce(new Error('连接中断'))
    function Harness() {
      const state = useTeamTurns(settings(api))
      return <><button onClick={() => void state.submit('原请求')}>提交测试任务</button><TeamComposerControls team={team} state={state} /></>
    }
    render(<Harness />)
    await waitFor(() => expect(api.personalTools).toHaveBeenCalled())
    await waitFor(() => expect(screen.getByLabelText('预算核算员的file_read权限')).toBeEnabled())
    fireEvent.click(screen.getByText('提交测试任务'))
    expect(await screen.findByText('保留原请求')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: '放弃重试' }))
    const confirm = screen.getByRole('group', { name: '确认放弃原请求重试' })
    expect(confirm).toHaveTextContent('不会取消已受理的任务')
    fireEvent.click(within(confirm).getByRole('button', { name: '确认放弃重试' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: '重试原请求' })).not.toBeInTheDocument())
    expect(api.cancelPersonalTeamTurn).not.toHaveBeenCalled()
  })
  it('renders actual phase/member versions and cancels only the accepted team task after confirmation', async () => {
    const phase = collaboration(), original = turn({ status: 'working', phases: [{ phase: 'members', collaboration_id: phase.id, collaboration: phase }] })
    const cancel = vi.fn().mockResolvedValue(undefined), api = apiMock()
    render(<TeamTurnCard turn={original} state={{ cancel, refresh: vi.fn() }} api={api} agents={catalog} />)
    expect(screen.getByLabelText('团队成员执行')).toHaveTextContent('预算核算员v2')
    fireEvent.click(screen.getByRole('button', { name: '停止' }))
    expect(cancel).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '确认停止', exact: true }))
    await waitFor(() => expect(cancel).toHaveBeenCalledWith(original))
    expect(api.cancelPersonalCollaboration).not.toHaveBeenCalled()
  })
  it('scopes phase details to supplied team collaborations instead of fetching all workspace history', async () => {
    const api = apiMock(), phase = collaboration()
    api.personalCollaboration.mockResolvedValue(phase)
    render(<PersonalCollaborations api={api} agents={catalog} sourceSessionId="team-daily" busy={false} onClose={vi.fn()}
      scopedCollaborations={[phase]} initialId={phase.id} allowCancel={false} />)
    expect(await screen.findByText('读取预算')).toBeVisible()
    expect(api.personalCollaborations).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: '发起协作' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '停止协作' })).not.toBeInTheDocument()
  })
  it('does not let one receiver permission resolution hide another receiver with the same call ID', async () => {
    const api = apiMock()
    const event = (sequence: number, kind: string, session: string, messageId: string): PersonalCollaborationEvent => ({ sequence,
      key: 'event-' + sequence, kind, step_id: 'read', created_at: team.created_at,
      data: { session_id: session, peer_message_id: messageId, call_id: 'same-call', tool_name: 'file_write', arguments: { path: 'report.md' } } })
    const phase = collaboration({ events: [event(1, 'permission_requested', 'receiver-one', 'pm_one'), event(2, 'permission_resolved', 'receiver-one', 'pm_one'),
      event(3, 'permission_requested', 'receiver-two', 'pm_two')],
      peer_sessions: [{ message_id: 'pm_one', session_id: 'receiver-one', agent_id: member.id, agent_version: 2, step_id: 'read', state: 'responded' },
        { message_id: 'pm_two', session_id: 'receiver-two', agent_id: member.id, agent_version: 2, step_id: 'read', state: 'processing' }] })
    api.personalCollaboration.mockResolvedValue(phase)
    render(<PersonalCollaborations api={api} agents={catalog} sourceSessionId="team-daily" busy={false} onClose={vi.fn()} scopedCollaborations={[phase]} />)
    fireEvent.click(await screen.findByRole('button', { name: '允许本次操作' }))
    await waitFor(() => expect(api.resolvePermission).toHaveBeenCalledWith('receiver-two', 'same-call', { decision: 'allow' }))
  })
  it('exposes coordination tools only for the team kind without granting them by default', async () => {
    const api = { personalTools: vi.fn().mockResolvedValue({ tools }), createPersonalAgent: vi.fn(), updatePersonalAgent: vi.fn(), personalAgentHistory: vi.fn() } as never
    render(<PersonalAgentEditor api={api} agent={null} agents={catalog} busy={false} onClose={vi.fn()} onSaved={vi.fn()} />)
    await waitFor(() => expect(screen.getByLabelText('搜索可用工具')).toBeEnabled())
    expect(screen.queryByText('personal_team')).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('类型'), { target: { value: 'team' } })
    expect(await screen.findByText('personal_team')).toBeVisible()
    expect(screen.getByLabelText('personal_team协调团队任务')).not.toBeChecked()
  })
})
