import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { WorkspaceProvider, useWorkspace } from '../src/hooks/WorkspaceProvider'
import { PlatformProvider } from '../src/platform'
import { savePreference } from '../src/api/WorkbenchRuntimeClient'

const base = 'http://localhost:9890/api/v1'
const session = (id: string) => ({ id, title: id, model: 'test', message_count: 1, created_at: '', updated_at: '', status: 'active', total_tokens: 0, total_cost_usd: 0 })
const snapshot = { version: 1, session_id: 'one', summary: {}, missions: [], tasks: [], issues: [], worktrees: [], events: [], agent_profiles: [], approvals: [] }
const server = setupServer(
  http.get(`${base}/workbench/daemon/status`, () => HttpResponse.json({ workspace_name: '团队会话刷新测试', event_stream_url_template: '' })),
  http.get(`${base}/sessions`, () => HttpResponse.json({ sessions: [session('one'), session('two')], total: 2 })),
  http.get(`${base}/config`, () => HttpResponse.json({ models: [{ id: 'test', name: '测试', tier: 'capable' }], tools: [] })),
  http.get(`${base}/commands`, () => HttpResponse.json({ commands: [] })),
  http.get(`${base}/engines`, () => HttpResponse.json({ engines: [{ id: 'naumi', available: true }], default: 'naumi' })),
  http.get(`${base}/goals`, () => HttpResponse.json({ goals: [] })),
  http.get(`${base}/sessions/:id/todos`, () => HttpResponse.json({ todos: [] })),
  http.get(`${base}/sessions/:id/environment`, () => HttpResponse.json({ sources: [] })),
  http.get(`${base}/sessions/:id/runs`, () => HttpResponse.json({ runs: [] })),
  http.get(`${base}/workbench/sessions/:id/snapshot`, () => HttpResponse.json(snapshot)),
  http.get(`${base}/sessions/:id/messages`, ({ params }) => HttpResponse.json({ messages: [{ id: 'history-' + params.id,
    role: 'assistant', content: '历史-' + params.id, timestamp: '', metadata: {} }], total: 1 })),
)
function View() {
  const state = useWorkspace()
  return <><span data-testid="connected">{String(Boolean(state.daemon))}</span><span data-testid="history">{state.messages.map(message => message.content).join('|')}</span>
    <span data-testid="sources">{state.selectedSources.join(',')}</span>
    <input aria-label="当前草稿" value={state.draft} onChange={event => state.setDraft(event.target.value)} />
    <button onClick={() => void state.select('one')}>选择one</button><button onClick={() => void state.select('two')}>选择two</button>
    <button onClick={() => state.setSelectedSources(['attached-source'])}>选择资料</button>
    <button onClick={() => void state.refreshConversation('one')}>刷新one</button>
  </>
}
function setup() { render(<PlatformProvider><WorkspaceProvider><View /></WorkspaceProvider></PlatformProvider>) }
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
beforeEach(() => { localStorage.clear(); savePreference('api', base) })
afterEach(() => { cleanup(); server.resetHandlers() })
afterAll(() => server.close())

describe('source-bound team conversation refresh', () => {
  it('updates only conversation history while preserving the user draft and selected sources', async () => {
    setup()
    await waitFor(() => expect(screen.getByTestId('connected')).toHaveTextContent('true'))
    fireEvent.click(screen.getByText('选择one'))
    await waitFor(() => expect(screen.getByTestId('history')).toHaveTextContent('历史-one'))
    fireEvent.change(screen.getByLabelText('当前草稿'), { target: { value: '下一条团队消息' } })
    fireEvent.click(screen.getByText('选择资料'))
    server.use(http.get(`${base}/sessions/one/messages`, () => HttpResponse.json({ messages: [
      { id: 'team-delivery', role: 'assistant', content: '持久团队结果825', timestamp: '', metadata: { team_turn_id: 'pt_original' } },
    ], total: 1 })))
    fireEvent.click(screen.getByText('刷新one'))
    await waitFor(() => expect(screen.getByTestId('history')).toHaveTextContent('持久团队结果825'))
    expect(screen.getByLabelText('当前草稿')).toHaveValue('下一条团队消息')
    expect(screen.getByTestId('sources')).toHaveTextContent('attached-source')
  })
  it('discards a late team-history response after selecting another conversation', async () => {
    setup()
    await waitFor(() => expect(screen.getByTestId('connected')).toHaveTextContent('true'))
    fireEvent.click(screen.getByText('选择one'))
    await waitFor(() => expect(screen.getByTestId('history')).toHaveTextContent('历史-one'))
    let release!: () => void, started = false
    const wait = new Promise<void>(resolve => { release = resolve })
    server.use(http.get(`${base}/sessions/one/messages`, async () => { started = true; await wait
      return HttpResponse.json({ messages: [{ id: 'late', role: 'assistant', content: '旧团队延迟结果', timestamp: '', metadata: {} }], total: 1 }) }))
    fireEvent.click(screen.getByText('刷新one'))
    await waitFor(() => expect(started).toBe(true))
    fireEvent.click(screen.getByText('选择two'))
    await waitFor(() => expect(screen.getByTestId('history')).toHaveTextContent('历史-two'))
    fireEvent.change(screen.getByLabelText('当前草稿'), { target: { value: '另一团队草稿' } })
    await act(async () => { release(); await wait })
    expect(screen.getByTestId('history')).toHaveTextContent('历史-two')
    expect(screen.getByTestId('history')).not.toHaveTextContent('旧团队延迟结果')
    expect(screen.getByLabelText('当前草稿')).toHaveValue('另一团队草稿')
  })
})
