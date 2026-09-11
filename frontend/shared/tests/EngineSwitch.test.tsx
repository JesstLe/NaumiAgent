import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, afterAll, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { WorkspaceProvider, useWorkspace } from '@naumi/shared/hooks/WorkspaceProvider'
import { PlatformProvider } from '@naumi/shared/platform'
import { savePreference } from '@naumi/shared/api/WorkbenchRuntimeClient'

const base = 'http://localhost:9888/api/v1'
const session = (id: string, engine = 'naumi') => ({
  id,
  title: id,
  model: 'test-model',
  message_count: 0,
  updated_at: '',
  created_at: '',
  status: 'active',
  total_tokens: 0,
  total_cost_usd: 0,
  engine,
})

let createBodies: Array<Record<string, unknown>> = []

const server = setupServer(
  http.get(`${base}/workbench/daemon/status`, () =>
    HttpResponse.json({ workspace_name: '引擎测试', event_stream_url_template: '' }),
  ),
  http.get(`${base}/engines`, () =>
    HttpResponse.json({
      default: 'pi',
      engines: [
        { id: 'naumi', name: 'NaumiAgent 引擎', available: true, default: false },
        {
          id: 'pi',
          name: 'pi coding agent',
          available: true,
          default: true,
          provider: 'zai-coding-cn',
          model: 'glm-4.7',
        },
      ],
    }),
  ),
  http.get(`${base}/sessions`, () =>
    HttpResponse.json({ sessions: [session('pi-chat', 'pi')], total: 1 }),
  ),
  http.get(`${base}/config`, () =>
    HttpResponse.json({ models: [], tools: [] }),
  ),
  http.get(`${base}/commands`, () => HttpResponse.json({ commands: [] })),
  http.get(`${base}/sessions/:id/messages`, () =>
    HttpResponse.json({ messages: [], total: 0 }),
  ),
  http.get(`${base}/sessions/:id/environment`, () =>
    HttpResponse.json({ sources: [] }),
  ),
  http.get(`${base}/sessions/:id/runs`, () => HttpResponse.json({ runs: [] })),
  http.get(`${base}/workbench/sessions/:id/snapshot`, () =>
    HttpResponse.json({
      version: 1,
      session_id: 'x',
      summary: {},
      missions: [],
      tasks: [],
      issues: [],
      worktrees: [],
      events: [],
      agent_profiles: [],
      approvals: [],
    }),
  ),
  http.post(`${base}/sessions`, async ({ request }) => {
    createBodies.push((await request.json()) as Record<string, unknown>)
    return HttpResponse.json(session('created', 'pi'))
  }),
  http.post(`${base}/sessions/:id/messages`, () =>
    new HttpResponse(
      'data: {"id":"t","type":"token_delta","data":{"token":"ok"}}\n\ndata: {"id":"e","type":"agent_end","data":{"status":"completed"}}\n\n',
      { headers: { 'Content-Type': 'text/event-stream' } },
    ),
  ),
)

function EngineView() {
  const w = useWorkspace()
  return (
    <section aria-label="engine">
      <div data-testid="engine-current">{w.engine}</div>
      <div data-testid="engine-list">
        {w.engines.map((item) => `${item.id}:${item.available}`).join(',')}
      </div>
      <div data-testid="engine-session">{w.sessionId ?? 'new'}</div>
      <button onClick={() => w.switchEngine('naumi')}>切到 naumi</button>
      <button onClick={() => void w.select('pi-chat')}>选 pi 会话</button>
      <button onClick={() => void w.send()}>发送</button>
      <input
        aria-label="draft"
        value={w.draft}
        onChange={(e) => w.setDraft(e.target.value)}
      />
    </section>
  )
}

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
beforeEach(() => {
  localStorage.clear()
  savePreference('api', base)
})
afterEach(() => {
  server.resetHandlers()
  createBodies = []
})
afterAll(() => server.close())

describe('web2 引擎切换', () => {
  it('加载引擎目录并以服务端默认引擎启动', async () => {
    render(
      <PlatformProvider>
        <WorkspaceProvider>
          <EngineView />
        </WorkspaceProvider>
      </PlatformProvider>,
    )
    await waitFor(() => {
      expect(screen.getByTestId('engine-current').textContent).toBe('pi')
    })
    expect(screen.getByTestId('engine-list').textContent).toContain('naumi:true')
    expect(screen.getByTestId('engine-list').textContent).toContain('pi:true')
    cleanup()
  })

  it('切换引擎重置到新对话，新会话携带 engine 字段', async () => {
    render(
      <PlatformProvider>
        <WorkspaceProvider>
          <EngineView />
        </WorkspaceProvider>
      </PlatformProvider>,
    )
    await waitFor(() => {
      expect(screen.getByTestId('engine-current').textContent).toBe('pi')
    })
    act(() => {
      screen.getByText('切到 naumi').click()
    })
    await waitFor(() => {
      expect(screen.getByTestId('engine-current').textContent).toBe('naumi')
      expect(screen.getByTestId('engine-session').textContent).toBe('new')
    })
    await act(async () => {
      fireEvent.change(screen.getByLabelText('draft'), {
        target: { value: '你好' },
      })
    })
    await act(async () => {
      screen.getByText('发送').click()
    })
    await waitFor(() => {
      expect(createBodies.length).toBe(1)
    })
    expect(createBodies[0]).toMatchObject({ engine: 'naumi' })
    cleanup()
  })

  it('选择已有会话时采用该会话的引擎', async () => {
    render(
      <PlatformProvider>
        <WorkspaceProvider>
          <EngineView />
        </WorkspaceProvider>
      </PlatformProvider>,
    )
    await waitFor(() => {
      expect(screen.getByTestId('engine-current').textContent).toBe('pi')
    })
    await act(async () => {
      screen.getByText('切到 naumi').click()
    })
    await waitFor(() => {
      expect(screen.getByTestId('engine-current').textContent).toBe('naumi')
    })
    await act(async () => {
      screen.getByText('选 pi 会话').click()
    })
    await waitFor(() => {
      expect(screen.getByTestId('engine-session').textContent).toBe('pi-chat')
      expect(screen.getByTestId('engine-current').textContent).toBe('pi')
    })
    cleanup()
  })
})
