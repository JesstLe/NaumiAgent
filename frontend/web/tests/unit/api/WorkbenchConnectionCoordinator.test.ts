import { describe, it, expect, beforeAll, afterAll, afterEach, vi } from 'vitest'
import { setupServer } from 'msw/node'
import { http, HttpResponse } from 'msw'
import { WorkbenchConnectionCoordinator } from '@/api/WorkbenchConnectionCoordinator'

const baseURL = 'http://127.0.0.1:8765/api/v1'

class MockWebSocket {
  static OPEN = 1
  static CONNECTING = 0
  static CLOSING = 2
  static CLOSED = 3
  static lastInstance: MockWebSocket | null = null
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: ((error?: unknown) => void) | null = null
  url: string
  readyState = 0
  sentMessages: string[] = []

  constructor(url: string) {
    this.url = url
    MockWebSocket.lastInstance = this
    queueMicrotask(() => this.simulateOpen())
  }

  private simulateOpen() {
    this.readyState = 1
    this.onopen?.()
  }

  simulateMessage(data: string) {
    this.onmessage?.({ data })
  }

  simulateClose() {
    this.readyState = 3
    this.onclose?.()
  }

  close() {
    this.readyState = 3
  }

  send(message: string) {
    this.sentMessages.push(message)
  }
}

const handlers = [
  http.get(`${baseURL}/workbench/daemon/status`, () =>
    HttpResponse.json({
      status: 'running',
      version: '0.0.1',
      pid: 123,
      host: '127.0.0.1',
      port: 8765,
      started_at: '2026-07-14T10:00:00Z',
      workspace_count: 1,
      workspace_root: '/tmp',
      workspace_name: 'demo',
      api_base_url: `${baseURL}`,
      workbench_base_url: `${baseURL}`,
      event_stream_url_template: 'ws://127.0.0.1:8765/api/v1/ws/sessions/{session_id}/events',
      auth_mode: 'dev_token',
    })),

  http.get(`${baseURL}/workbench/sessions/s1/snapshot`, () =>
    HttpResponse.json({
      version: 1,
      session_id: 's1',
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
    })),
]

const server = setupServer(...handlers)

let coordinators: WorkbenchConnectionCoordinator[] = []

function createCoordinator(token: string | null = null, options: { pollIntervalMs?: number } = {}) {
  const coordinator = new WorkbenchConnectionCoordinator(async () => token, options)
  coordinators.push(coordinator)
  return coordinator
}

describe('WorkbenchConnectionCoordinator', () => {
  beforeAll(() => {
    server.listen({ onUnhandledRequest: 'error' })
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    coordinators.forEach((c) => c.disconnect())
    coordinators = []
    server.resetHandlers()
    MockWebSocket.lastInstance = null
  })

  afterAll(() => {
    server.close()
    vi.unstubAllGlobals()
  })

  it('connects to the default daemon URL and exposes the API client', async () => {
    const coordinator = createCoordinator()
    const client = await coordinator.connect()
    expect(client).toBeDefined()
    expect(coordinator.baseUrl).toBe(baseURL)
    expect(coordinator.isConnected).toBe(true)
  })

  it('emits status updates when connected', async () => {
    const coordinator = createCoordinator()
    const statuses: { isConnected: boolean }[] = []
    coordinator.subscribeToStatus((status) => statuses.push({ isConnected: status.isConnected }))
    await coordinator.connect()
    expect(statuses.some((s) => s.isConnected)).toBe(true)
  })

  it('selects a session, loads snapshot, and opens an event stream', async () => {
    const coordinator = createCoordinator('secret')
    await coordinator.connect()
    const snapshot = await coordinator.selectSession('s1')
    expect(snapshot.session_id).toBe('s1')
    expect(coordinator.sessionId).toBe('s1')
    expect(MockWebSocket.lastInstance).not.toBeNull()
    expect(MockWebSocket.lastInstance!.url).toContain('api_key=secret')
  })

  it('sends an initial refresh request after the event stream opens', async () => {
    const coordinator = createCoordinator(null, { pollIntervalMs: 10000 })
    await coordinator.connect()
    await coordinator.selectSession('s1')
    // Wait for the mock WebSocket open microtask to fire.
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(MockWebSocket.lastInstance!.sentMessages).toHaveLength(1)
    expect(JSON.parse(MockWebSocket.lastInstance!.sentMessages[0])).toEqual({ type: 'refresh', limit: 50 })
  })

  it('unwraps workbench/event envelopes before dispatching', async () => {
    const coordinator = createCoordinator()
    await coordinator.connect()
    await coordinator.selectSession('s1')

    const events: unknown[] = []
    coordinator.subscribeToEvents((event) => events.push(event))

    MockWebSocket.lastInstance!.simulateMessage(
      JSON.stringify({
        type: 'workbench/event',
        version: 1,
        payload: {
          id: 'e1',
          session_id: 's1',
          type: 'agent.joined',
          actor: 'agent-1',
          subject_id: 'agent-1',
          payload: {},
          timestamp: '2026-07-14T10:00:00Z',
          correlation_id: null,
          parent_event_id: null,
          severity: 'info',
          task: null,
        },
      }),
    )

    expect(events).toHaveLength(1)
    expect((events[0] as { type: string }).type).toBe('agent.joined')
  })

  it('does not emit envelope-only messages as events', async () => {
    const coordinator = createCoordinator()
    await coordinator.connect()
    await coordinator.selectSession('s1')

    const events: unknown[] = []
    coordinator.subscribeToEvents((event) => events.push(event))

    MockWebSocket.lastInstance!.simulateMessage(JSON.stringify({ type: 'connected', session_id: 's1' }))
    MockWebSocket.lastInstance!.simulateMessage(JSON.stringify({ type: 'refresh_complete', count: 0 }))

    expect(events).toHaveLength(0)
  })

  it('disconnects and resets state', async () => {
    const coordinator = createCoordinator()
    await coordinator.connect()
    coordinator.disconnect()
    expect(coordinator.isConnected).toBe(false)
    expect(coordinator.sessionId).toBeNull()
    expect(coordinator.client).toBeNull()
  })
})
