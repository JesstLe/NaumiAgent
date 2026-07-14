import type { Page, Route } from '@playwright/test'

/**
 * Mock backend fixtures for E2E smoke tests.
 * These mirror the Workbench API contract so the UI can fully render
 * without a running NaumiAgent daemon.
 */

const SESSION_ID = 'smoke-session-001'
const MISSION_ID = 'smoke-mission-001'
const TASK_ID = 'smoke-task-001'

const daemonStatus = {
  status: 'running',
  version: '0.1.0',
  pid: 12345,
  host: '127.0.0.1',
  port: 8765,
  started_at: '2026-07-14T00:00:00Z',
  workspace_count: 1,
  workspace_root: '.',
  workspace_name: 'naumiagent',
  api_base_url: 'http://127.0.0.1:8765/api/v1',
  workbench_base_url: 'http://127.0.0.1:8765/api/v1/workbench',
  // Leave the event stream template empty so the coordinator skips the
  // WebSocket connection — E2E tests validate the REST-driven UI only.
  event_stream_url_template: '',
  auth_mode: 'api_key',
}

const snapshot = {
  version: 1 as const,
  session_id: SESSION_ID,
  summary: {
    current_mission_title: '冒烟测试 Mission',
    active_agents: 2,
    open_issues: 3,
    blocked_issues: 1,
    pending_approvals: 2,
    failed_validations: 1,
  },
  missions: [
    {
      id: MISSION_ID,
      session_id: SESSION_ID,
      title: '冒烟测试 Mission',
      goal: '验证 Workbench UI 端到端可用',
      status: 'in_progress',
      created_at: '2026-07-14T00:00:00Z',
      updated_at: '2026-07-14T00:00:00Z',
    },
  ],
  agent_profiles: [
    {
      id: 'agent-alpha',
      session_id: SESSION_ID,
      name: 'Alpha',
      role: 'architect',
      capabilities: ['code', 'review'],
      permissions: ['read', 'write'],
      max_parallel_tasks: 3,
      status: 'idle',
      last_heartbeat_at: '2026-07-14T00:00:00Z',
      created_at: '2026-07-14T00:00:00Z',
      updated_at: '2026-07-14T00:00:00Z',
      current_lease: null,
      current_issue: null,
    },
  ],
  intent_locks: [],
  decisions: [],
  tasks: [
    {
      id: TASK_ID,
      session_id: SESSION_ID,
      subject: '实现登录页',
      description: '使用 Tauri 的安全存储保存 API 令牌',
      status: 'pending',
      active_form: null,
      owner: null,
      blocks: [],
      blocked_by: [],
      created_at: '2026-07-14T00:00:00Z',
      updated_at: '2026-07-14T00:00:00Z',
    },
  ],
  issues: [
    {
      session_id: SESSION_ID,
      task_id: TASK_ID,
      mission_id: MISSION_ID,
      parallel_mode: 'cooperative',
      risk_level: 'medium',
      requires_human_approval: false,
      acceptance_criteria: ['页面可加载'],
      expected_artifacts: [],
      related_branch: '',
      related_worktree: '',
      related_pr: '',
      created_at: '2026-07-14T00:00:00Z',
      updated_at: '2026-07-14T00:00:00Z',
      task: {
        id: TASK_ID,
        session_id: SESSION_ID,
        subject: '实现登录页',
        description: '使用 Tauri 的安全存储保存 API 令牌',
        status: 'pending',
        active_form: null,
        owner: null,
        blocks: [],
        blocked_by: [],
        created_at: '2026-07-14T00:00:00Z',
        updated_at: '2026-07-14T00:00:00Z',
      },
    },
  ],
  leases: [],
  bids: [],
  proposals: [],
  failures: [],
  events: [
    {
      id: 'evt-001',
      session_id: SESSION_ID,
      type: 'session.started',
      actor: 'system',
      subject_id: SESSION_ID,
      payload: {},
      timestamp: '2026-07-14T00:00:00Z',
      correlation_id: null,
      parent_event_id: null,
      severity: 'info',
      task: null,
    },
  ],
  validation_runs: [],
  context_snapshots: [],
  approvals: [
    {
      id: 'appr-001',
      session_id: SESSION_ID,
      mission_id: MISSION_ID,
      task_id: TASK_ID,
      state: 'waiting',
      title: '审查登录页实现',
      detail: '请确认令牌存储方案是否安全',
      requester: 'agent-alpha',
      reviewer: '',
      decision_note: '',
      created_at: '2026-07-14T00:00:00Z',
      updated_at: '2026-07-14T00:00:00Z',
      task: null,
    },
  ],
  worktrees: [
    {
      name: 'wt-login',
      path: 'C:\\repo\\.naumi\\worktrees\\wt-login',
      branch: 'feat/login',
      base_ref: 'main',
      status: 'clean',
      task_id: TASK_ID,
      dirty_files: 0,
      commits_ahead: 1,
      created_at: '2026-07-14T00:00:00Z',
      updated_at: '2026-07-14T00:00:00Z',
      kept_reason: '',
      metadata: {},
      removable: true,
      task: null,
    },
  ],
}

const bootstrap = {
  daemon_status: daemonStatus,
  capabilities: {
    supports_daemon_management: true,
    supports_workspace_registry: false,
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
    agent_stale_threshold_seconds: 300,
    agent_offline_threshold_seconds: 900,
  },
  sessions: [
    {
      id: SESSION_ID,
      title: '冒烟测试会话',
      model: 'kimi',
      created_at: '2026-07-14T00:00:00Z',
      updated_at: '2026-07-14T00:00:00Z',
      message_count: 0,
      total_tokens: 0,
      total_cost_usd: 0,
      status: 'active',
    },
  ],
  total_sessions: 1,
  selected_session_id: SESSION_ID,
  snapshot,
}

const messages = {
  messages: [],
  total: 0,
}

/**
 * Install route handlers that mock the full Workbench API.
 * WebSocket upgrade requests are aborted so the coordinator's event stream
 * does not hang.
 */
export async function mockWorkbenchApi(page: Page): Promise<void> {
  // Use a catch-all first: any request to the daemon port that is not
  // matched by a more specific handler below returns an empty 200 so a
  // real backend running on 127.0.0.1:8765 cannot leak 404s into the test.
  await page.route('http://127.0.0.1:8765/**', (route: Route) => {
    const url = route.request().url()
    const method = route.request().method()

    if (url.includes('/workbench/daemon/status')) {
      return route.fulfill({ status: 200, json: daemonStatus })
    }
    if (url.includes('/workbench/bootstrap')) {
      return route.fulfill({ status: 200, json: bootstrap })
    }
    if (url.includes('/snapshot')) {
      return route.fulfill({ status: 200, json: snapshot })
    }
    if (url.endsWith('/workbench/sessions') && method === 'GET') {
      return route.fulfill({ status: 200, json: { sessions: bootstrap.sessions, total: 1, page: 1, page_size: 50 } })
    }
    if (url.includes('/workbench/sessions') && method === 'POST') {
      return route.fulfill({ status: 200, json: { session_id: SESSION_ID, snapshot } })
    }
    if (url.includes('/sessions/') && url.includes('/messages')) {
      return route.fulfill({ status: 200, json: messages })
    }
    if (url.includes('/issues') && !url.includes('/claim')) {
      return route.fulfill({ status: 200, json: { issues: snapshot.issues, mission_id: null, risk_level: null, status: null, limit: 100 } })
    }
    if (url.includes('/leases')) {
      return route.fulfill({ status: 200, json: { leases: [], state: 'active', task_id: null, agent_id: null, limit: 100 } })
    }
    if (url.includes('/worktrees')) {
      return route.fulfill({ status: 200, json: { worktrees: snapshot.worktrees, task_id: null, status: null, limit: 100 } })
    }
    if (url.includes('/approvals')) {
      return route.fulfill({ status: 200, json: { approvals: snapshot.approvals, state: null, mission_id: null, task_id: null, limit: 50 } })
    }
    if (url.includes('/validation-runs')) {
      return route.fulfill({ status: 200, json: { validation_runs: [], task_id: null, status: null, limit: 50 } })
    }
    if (url.includes('/events') && !url.includes('/stream')) {
      return route.fulfill({ status: 200, json: { events: snapshot.events, event_type: null, subject_id: null, actor: null, since: null, severity: null, correlation_id: null, parent_event_id: null, limit: 200 } })
    }
    return route.fulfill({ status: 200, json: {} })
  })
}
