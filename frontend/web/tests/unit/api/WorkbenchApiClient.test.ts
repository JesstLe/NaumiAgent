import { describe, it, expect, beforeAll, afterAll, afterEach } from 'vitest'
import { setupServer } from 'msw/node'
import { http, HttpResponse } from 'msw'
import { WorkbenchApiClient } from '@/api/WorkbenchApiClient'
import { ApiException, isApiException } from '@/api/ApiException'

const baseURL = 'http://localhost:9876/api/v1'

let lastToken: string | null = null

function makeSession(id: string, title = '') {
  return {
    id,
    title,
    model: 'default',
    created_at: '2026-07-14T10:00:00Z',
    updated_at: '2026-07-14T10:00:00Z',
    message_count: 0,
    total_tokens: 0,
    total_cost_usd: 0,
    status: 'active',
  }
}

const handlers = [
  http.get(`${baseURL}/workbench/daemon/status`, ({ request }) => {
    lastToken = request.headers.get('authorization')
    return HttpResponse.json({
      status: 'ok',
      version: '0.0.1',
      pid: 123,
      host: '127.0.0.1',
      port: 8765,
      started_at: '2026-07-14T10:00:00Z',
      workspace_count: 1,
      workspace_root: '/tmp',
      workspace_name: 'demo',
      api_base_url: 'http://127.0.0.1:8765/api/v1',
      workbench_base_url: 'http://127.0.0.1:8765/api/v1',
      event_stream_url_template: 'ws://127.0.0.1:8765/api/v1/ws/sessions/{session_id}/events',
      auth_mode: 'token',
    })
  }),

  http.get(`${baseURL}/workbench/capabilities`, () =>
    HttpResponse.json({
      supports_daemon_management: true,
      supports_workspace_registry: true,
      supports_validation_runner: true,
      supports_event_stream: true,
      supports_cloud_sync: false,
      supported_locales: ['zh-CN', 'en-US'],
      default_locale: 'zh-CN',
      protocol_version: 1,
      supported_resources: [],
      supported_actions: [],
      route_templates: {},
      allowed_validation_commands: [],
      agent_stale_threshold_seconds: 60,
      agent_offline_threshold_seconds: 300,
    }),
  ),

  http.get(`${baseURL}/workbench/sessions`, () =>
    HttpResponse.json({ sessions: [makeSession('s1', '测试')], total: 1, page: 1, page_size: 20 })),

  http.post(`${baseURL}/workbench/sessions`, async ({ request }) => {
    const body = (await request.json()) as { title?: string; model?: string }
    return HttpResponse.json(
      {
        daemon_status: {},
        capabilities: {},
        sessions: [makeSession('s2', body.title ?? '')],
        total_sessions: 1,
        selected_session_id: 's2',
        snapshot: null,
      },
      { status: 201 },
    )
  }),

  http.get(`${baseURL}/workbench/sessions/:sessionId/snapshot`, ({ params }) => {
    if (params.sessionId === 'missing') {
      return HttpResponse.json({ detail: '未找到会话' }, { status: 404 })
    }
    return HttpResponse.json({
      version: 1,
      session_id: params.sessionId as string,
      summary: {
        current_mission_title: '',
        active_agents: 0,
        open_issues: 0,
        blocked_issues: 0,
        pending_approvals: 0,
        failed_validations: 0,
      },
      missions: [],
      agent_profiles: [],
      intent_locks: [],
      decisions: [],
      tasks: [],
      issues: [],
      leases: [],
      bids: [],
      proposals: [],
      failures: [],
      events: [],
      validation_runs: [],
      context_snapshots: [],
      approvals: [],
      worktrees: [],
    })
  }),

  http.get(`${baseURL}/workbench/sessions/:sessionId/missions`, () =>
    HttpResponse.json({ missions: [], status: null, limit: 50 })),

  http.get(`${baseURL}/workbench/sessions/:sessionId/agents`, () =>
    HttpResponse.json({ agent_profiles: [], status: null, limit: 50 })),

  http.get(`${baseURL}/workbench/sessions/:sessionId/events`, ({ request }) => {
    const url = new URL(request.url)
    return HttpResponse.json({
      events: [],
      event_type: url.searchParams.get('type'),
      subject_id: null,
      actor: null,
      since: null,
      severity: null,
      correlation_id: null,
      parent_event_id: null,
      limit: 50,
    })
  }),

  http.get(`${baseURL}/sessions/:sessionId/messages`, ({ params, request }) => {
    const url = new URL(request.url)
    return HttpResponse.json({
      messages: [
        {
          id: 'm1',
          role: 'user',
          content: 'hello',
          timestamp: '2026-07-14T10:00:00Z',
          metadata: { session_id: params.sessionId },
        },
      ],
      total: parseInt(url.searchParams.get('page_size') ?? '50', 10),
    })
  }),
]

const server = setupServer(...handlers)

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => {
  server.resetHandlers()
  lastToken = null
})
afterAll(() => server.close())

describe('WorkbenchApiClient', () => {
  it('fetches daemon status and injects bearer token', async () => {
    const client = new WorkbenchApiClient(baseURL, async () => 'test-token')
    const status = await client.fetchDaemonStatus()
    expect(status.status).toBe('ok')
    expect(lastToken).toBe('Bearer test-token')
  })

  it('returns paginated sessions list', async () => {
    const client = new WorkbenchApiClient(baseURL, async () => null)
    const response = await client.fetchSessions()
    expect(response.sessions).toHaveLength(1)
    expect(response.sessions[0].id).toBe('s1')
    expect(response.total).toBe(1)
    expect(response.page).toBe(1)
  })

  it('creates a session and returns bootstrap payload', async () => {
    const client = new WorkbenchApiClient(baseURL, async () => null)
    const response = await client.createSession('新会话', 'fast')
    expect(response.selected_session_id).toBe('s2')
    expect(response.sessions[0].title).toBe('新会话')
  })

  it('fetches messages for a session', async () => {
    const client = new WorkbenchApiClient(baseURL, async () => null)
    const response = await client.fetchMessages('s1', 1, 10)
    expect(response.messages).toHaveLength(1)
    expect(response.messages[0].role).toBe('user')
    expect(response.total).toBe(10)
  })

  it('fetches missions wrapper', async () => {
    const client = new WorkbenchApiClient(baseURL, async () => null)
    const response = await client.fetchMissions('s1')
    expect(response.missions).toEqual([])
    expect(response.limit).toBe(50)
  })

  it('fetches agent profiles wrapper', async () => {
    const client = new WorkbenchApiClient(baseURL, async () => null)
    const response = await client.fetchAgents('s1')
    expect(response.agent_profiles).toEqual([])
    expect(response.limit).toBe(50)
  })

  it('passes event type query param and parses response', async () => {
    const client = new WorkbenchApiClient(baseURL, async () => null)
    const response = await client.fetchEvents('s1', { type: 'agent.joined' })
    expect(response.event_type).toBe('agent.joined')
  })

  it('maps 404 to InvalidUrl ApiException', async () => {
    const client = new WorkbenchApiClient(baseURL, async () => null)
    let caught: unknown
    try {
      await client.fetchSnapshot('missing')
    } catch (error) {
      caught = error
    }
    expect(isApiException(caught)).toBe(true)
    const apiError = caught as ApiException
    expect(apiError.code).toBe('InvalidUrl')
    expect(apiError.status).toBe(404)
  })

  it('maps network error to NetworkFailure ApiException', async () => {
    const client = new WorkbenchApiClient('http://127.0.0.1:1/api/v1', async () => null)
    let caught: unknown
    try {
      await client.fetchCapabilities()
    } catch (error) {
      caught = error
    }
    expect(isApiException(caught)).toBe(true)
    const apiError = caught as ApiException
    expect(apiError.code).toBe('NetworkFailure')
  })
})
