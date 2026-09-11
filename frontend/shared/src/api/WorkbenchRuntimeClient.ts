import { WorkbenchApiClient } from '@naumi/shared/api/WorkbenchApiClient'
import type { MessageCreate, MessageResponse, Session } from '@naumi/shared/api/types'

export interface ModelConfig {
  models: { id: string; name: string; tier: string }[]
  tools: { name: string; description: string }[]
  model_warnings: string[]
}
export interface Todo {
  id: string
  subject: string
  description: string
  status: 'pending' | 'in_progress' | 'blocked' | 'completed'
  active_form: string | null
  blocked_by: string[]
  updated_at: string
}
export interface WorkspaceGoal {
  goal_id: string
  objective: string
  status: 'active' | 'paused' | 'blocked' | 'completed' | 'cancelled'
  note: string
  session_id: string
  pursuit_run_id: string
  updated_at: string
  pursuit: null | {
    status: string; phase: string; iteration: number; criteria_total: number; criteria_verified: number
    blocked_reason: string; next_action: string
    evidence: { kind: string; source: string; summary: string; is_hard: boolean }[]
  }
}
export interface GoalSnapshot { current_goal_id: string; goals: WorkspaceGoal[]; warnings: string[]; truncated: boolean }
export interface SlashCommand { command: string; description: string; aliases: string[]; readonly: boolean; arguments: { syntax: string; required: boolean } }
export interface Run {
  id: string
  status: string
  started_at: string
  completed_at?: string
  steps: {
    sequence: number
    stage: string
    status: string
    summary: string
    detail: string
    metadata?: { tool_call_id?: string; input?: string; output_recorded?: boolean; output_truncated?: boolean; recovered_from?: string }
  }[]
}
export interface StreamEvent {
  id: string
  type: string
  run_id?: string
  turn?: number
  sequence?: number
  timestamp?: string
  data: Record<string, unknown>
}

export function readPreference(key: string, fallback = ''): string {
  try {
    return localStorage.getItem(`naumi:workspace:${key}`) ?? fallback
  } catch {
    return fallback
  }
}
export function savePreference(key: string, value: string) {
  try {
    localStorage.setItem(`naumi:workspace:${key}`, value)
  } catch {
    /* Storage can be unavailable in private sessions. */
  }
}

export function safeWebUrl(value: string): string {
  const url = new URL(value.includes('://') ? value : `https://${value}`)
  if (
    !['http:', 'https:'].includes(url.protocol) ||
    url.username ||
    url.password
  ) {
    throw new Error('请输入有效的 HTTP 或 HTTPS 网址')
  }
  return url.href
}

// Decode complete SSE frames; chunks may split both UTF-8 characters and CRLF.
export async function consumeEvents(
  body: ReadableStream<Uint8Array>,
  onEvent: (event: StreamEvent) => void,
) {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let pending = ''
  const dispatch = (frame: string) => {
    const data = frame
      .split(/\r?\n/)
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).trimStart())
      .join('\n')
    if (data && data !== '[DONE]') onEvent(JSON.parse(data) as StreamEvent)
  }
  try {
    while (true) {
      const { value, done } = await reader.read()
      pending += done
        ? decoder.decode()
        : decoder.decode(value, { stream: true })
      let boundary: RegExpExecArray | null
      while ((boundary = /\r?\n\r?\n/.exec(pending))) {
        dispatch(pending.slice(0, boundary.index))
        pending = pending.slice(boundary.index + boundary[0].length)
      }
      if (done) {
        if (pending.trim()) dispatch(pending)
        break
      }
    }
  } finally {
    reader.releaseLock()
  }
}

export class WorkbenchRuntimeClient extends WorkbenchApiClient {
  constructor(
    private base: string,
    private token: () => Promise<string | null>,
  ) {
    super(base, token)
  }
  private async fetch(path: string, init: RequestInit = {}) {
    const token = await this.token()
    const response = await fetch(`${this.base.replace(/\/$/, '')}${path}`, {
      ...init,
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...init.headers,
      },
      signal: init.signal ?? AbortSignal.timeout(20000),
    })
    if (!response.ok) {
      if (response.status === 401)
        throw new Error('连接令牌无效，请在设置中更新')
      if (response.status === 429) throw new Error('请求较多，请稍后重试')
      const detail = await response.json().catch(() => ({}))
      throw new Error(
        typeof detail.detail === 'string'
          ? detail.detail
          : `请求失败（${response.status}），请重试`,
      )
    }
    return response
  }
  async config(): Promise<ModelConfig> {
    return (await this.fetch('/config')).json()
  }
  async commands(): Promise<{ commands: SlashCommand[] }> { return (await this.fetch('/commands')).json() }
  async todos(session: string): Promise<{ todos: Todo[] }> {
    return (await this.fetch(`/sessions/${encodeURIComponent(session)}/todos`)).json()
  }
  async goals(): Promise<GoalSnapshot> { return (await this.fetch('/goals')).json() }
  async createGoal(session: string, objective: string): Promise<GoalSnapshot> {
    return (await this.fetch(`/sessions/${encodeURIComponent(session)}/goals`, { method: 'POST', body: JSON.stringify({ objective }) })).json()
  }
  async updateGoal(goal: string, status: WorkspaceGoal['status'], note: string): Promise<GoalSnapshot> {
    return (await this.fetch(`/goals/${encodeURIComponent(goal)}`, { method: 'PATCH', body: JSON.stringify({ status, note }) })).json()
  }
  async createTodo(session: string, body: { subject: string; blocked_by: string[] }): Promise<{ todos: Todo[] }> {
    return (await this.fetch(`/sessions/${encodeURIComponent(session)}/todos`, { method: 'POST', body: JSON.stringify(body) })).json()
  }
  async updateTodo(session: string, task: string, status: Todo['status']): Promise<{ todos: Todo[] }> {
    return (await this.fetch(`/sessions/${encodeURIComponent(session)}/todos/${encodeURIComponent(task)}`, { method: 'PATCH', body: JSON.stringify({ status }) })).json()
  }
  async sessions(page = 1): Promise<{ sessions: Session[]; total: number }> {
    return (await this.fetch(`/sessions?page=${page}&page_size=100`)).json()
  }
  async create(title?: string, model?: string): Promise<Session> {
    return (
      await this.fetch('/sessions', {
        method: 'POST',
        body: JSON.stringify({ title, model }),
      })
    ).json()
  }
  async runs(id: string): Promise<{ runs: Run[] }> {
    return (await this.fetch(`/sessions/${encodeURIComponent(id)}/runs`)).json()
  }
  async cancel(id: string, runId: string) {
    await this.fetch(
      `/sessions/${encodeURIComponent(id)}/runs/${encodeURIComponent(runId)}/cancel`,
      { method: 'POST' },
    )
  }
  async stream(
    id: string,
    message: MessageCreate,
    onEvent: (event: StreamEvent) => void,
    signal: AbortSignal,
  ) {
    const command = message.content.trim().startsWith('/')
    const response = await this.fetch(
      `/sessions/${encodeURIComponent(id)}/${command ? 'commands' : 'messages'}`,
      {
        method: 'POST',
        body: JSON.stringify(command ? { command: message.content, runtime_mode: message.runtime_mode || 'default' } : { ...message, stream: true }),
        signal,
      },
    )
    if (response.headers.get('content-type')?.includes('application/json')) {
      const result = (await response.json()) as MessageResponse
      onEvent({
        id: result.id,
        type: 'token_delta',
        data: { token: result.content },
      })
      onEvent({
        id: result.id,
        type: 'agent_end',
        data: { status: 'completed' },
      })
      return
    }
    if (!response.body) throw new Error('未收到响应内容，请重试')
    await consumeEvents(response.body, onEvent)
  }
}
