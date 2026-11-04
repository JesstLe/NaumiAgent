import type { PersonalItem, PersonalItemAction, PersonalItemControl, PersonalItemExecution, PersonalItemInput, PersonalItemPage, PersonalItemReadInput, PersonalItemReadReceipt, PersonalItemResult, PersonalItemResultsPage, PersonalItemUpdate } from './personalItems'
import type { PersonalLegacyMemoryPage, PersonalMemory, PersonalMemoryContext, PersonalMemoryGrant, PersonalMemoryHistoryEntry, PersonalMemoryMutationResult, PersonalMemoryPage, PersonalMemoryPrepare, PersonalMemoryQuery, PersonalMemoryRequest, PersonalMemorySearch } from './personalMemories'
import { WorkbenchApiClient } from '@naumi/shared/api/WorkbenchApiClient'
import type {
  EnginesResponse,
  MessageCreate,
  MessageResponse,
  Session,
} from '@naumi/shared/api/types'
import type {
  PersonalArtifactContent,
  PersonalArtifactGrant,
  PersonalArtifactInput,
  PersonalArtifactPreview,
  PersonalArtifactQuery,
  PersonalArtifactRevision,
  PersonalArtifactSnapshot,
} from './personalArtifacts'

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
  user_message_id?: string
  assistant_message_id?: string
  status: string
  started_at: string
  completed_at?: string
  steps: {
    sequence: number
    stage: string
    status: string
    summary: string
    detail: string
    metadata?: { tool_call_id?: string; input?: string; output_recorded?: boolean; output_truncated?: boolean; recovered_from?: string; public_action?: string }
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

export interface PersonalAgent {
  id: string
  name: string
  description: string
  instructions: string
  model_tier: 'fast' | 'capable' | 'reasoning'
  tool_names: string[]
  kind: 'specialist' | 'team'
  member_ids: string[]
  status: 'active' | 'paused' | 'archived'
  version: number
  created_at: string
  updated_at: string
}
export type PersonalAgentInput = Pick<PersonalAgent,
  'name' | 'description' | 'instructions' | 'model_tier' | 'tool_names' | 'kind' | 'member_ids'
>
export type PersonalAgentUpdate = Partial<Omit<PersonalAgentInput, 'kind'> & Pick<PersonalAgent, 'status'>> & {
  expected_version: number
}
export interface PersonalTool { name: string; description: string; read_only?: boolean; kinds?: PersonalAgent['kind'][] }

export interface PersonalPeerGrant {
  agent_id: string
  agent_version: number
  tool_names?: string[]
}
export type PersonalMessageState = 'queued' | 'dispatching' | 'processing' | 'responded'
  | 'acknowledged' | 'blocked' | 'unknown' | 'cancelled' | 'error' | 'timeout' | 'max_turns'
export interface PersonalMessageReceipt {
  sequence: number
  kind: string
  data: Record<string, unknown>
  created_at: string
  receipt_sha256: string
}
export interface PersonalAgentMessage {
  id: string
  collaboration_id: string
  step_id: string
  source_session_id: string
  source_run_id: string
  sender_agent_id: string
  sender_version: number
  sender_name: string
  recipient_agent_id: string
  recipient_version: number
  recipient_name: string
  receiver_session_id: string
  receiver_run_id: string
  root_job_id: string
  content: string
  context: string
  response: string
  error: string
  outcome: string
  state: PersonalMessageState
  created_at: string
  acknowledged_at: string
  acknowledged_by?: string
  response_sha256: string
  result_sha256: string
  receipts?: PersonalMessageReceipt[]
  content_truncated?: boolean
  context_truncated?: boolean
  response_truncated?: boolean
}
export interface PersonalMessageInput {
  source_session_id: string
  request_id: string
  sender_agent_id: string
  sender_agent_version: number
  recipient_agent_id: string
  recipient_agent_version: number
  content: string
  context?: string
  peer_agents?: PersonalPeerGrant[]
}
export interface PersonalPeerSession {
  message_id: string
  session_id: string
  agent_id: string
  agent_version: number
  step_id: string
  state: PersonalMessageState
}

export type PersonalCollaborationStatus =
  | 'admitted' | 'claimed' | 'running' | 'completed' | 'error' | 'timeout'
  | 'max_turns' | 'cancelled' | 'unknown' | 'blocked' | 'waiting'
export interface PersonalCollaborationStepInput {
  id: string
  agent_id: string
  agent_version: number
  task: string
  depends_on: string[]
}
export interface PersonalCollaborationInput {
  source_session_id: string
  request_id: string
  title?: string
  context?: string
  steps: PersonalCollaborationStepInput[]
  peer_agents?: PersonalPeerGrant[]
}
export interface PersonalCollaborationReply {
  kind: 'option' | 'custom'
  value?: string
  custom_text?: string
}
export interface PersonalCollaborationStep extends PersonalCollaborationStepInput {
  agent_name?: string
  job_id: string
  session_id: string
  status: PersonalCollaborationStatus
  response: string
  error: string
  run_id: string
}
export interface PersonalCollaborationEvent {
  sequence: number
  key: string
  kind: string
  step_id: string
  data: Record<string, unknown>
  created_at: string
}
export interface PersonalCollaboration {
  id: string
  request_id: string
  message_request_id?: string
  source_session_id: string
  source_agent_id: string
  title: string
  context: string
  created_at: string
  cancel_requested: boolean
  status: PersonalCollaborationStatus
  steps: PersonalCollaborationStep[]
  events?: PersonalCollaborationEvent[]
  peer_agents?: PersonalPeerGrant[]
  messages?: PersonalAgentMessage[]
  peer_sessions?: PersonalPeerSession[]
}

export type PersonalTeamTurnStatus = 'queued' | 'planning' | 'working' | 'summarizing'
  | 'completed' | 'cancelled' | 'error' | 'unknown' | 'blocked' | 'delivery_pending'
export interface PersonalTeamTurnInput {
  source_session_id: string
  team_id: string
  team_version: number
  request_id: string
  content: string
  context?: string
  source_ids?: string[]
  member_grants?: PersonalPeerGrant[]
}
export interface PersonalTeamRoster {
  teams: { agent_id: string; agent_version: number; member_ids: string[] }[]
  agents: { agent_id: string; agent_version: number; tool_names: string[] }[]
}
export interface PersonalTeamTurn {
  id: string
  request_id: string
  team_id: string
  team_version: number
  team_name: string
  source_session_id: string
  team_session_id: string
  source_user_message_id: string
  result_message_id: string
  content: string
  context: string
  created_at: string
  updated_at: string
  cancel_requested: boolean
  status: PersonalTeamTurnStatus
  response: string
  error: string
  result_sha256: string
  usage: { total_tokens: number; total_cost_usd: number; job_count: number }
  roster: PersonalTeamRoster
  phases: { phase: 'planning' | 'members' | 'summary'; collaboration_id: string; collaboration: PersonalCollaboration }[]
}

function preferenceNamespace(): string {
  try {
    return new URLSearchParams(location.search).get('naumiWindow')?.trim() || ''
  } catch {
    return ''
  }
}

function preferenceStorageKey(key: string): string {
  const namespace = preferenceNamespace()
  return `naumi:workspace:${namespace ? `${namespace}:` : ''}${key}`
}

function launchPreference(key: string): string | null {
  try {
    const params = new URLSearchParams(location.search)
    if (key === 'api') return params.get('naumiApi')
    if (key === 'session') return params.get('naumiSession')
  } catch {
    /* The browser URL is unavailable in isolated tests. */
  }
  return null
}

export function readPreference(key: string, fallback = ''): string {
  try {
    const storageKey = preferenceStorageKey(key)
    const stored = localStorage.getItem(storageKey)
    if (stored !== null) return stored
    const launched = launchPreference(key)
    if (launched !== null) {
      localStorage.setItem(storageKey, launched)
      return launched
    }
    return fallback
  } catch {
    return fallback
  }
}
export function savePreference(key: string, value: string) {
  try {
    localStorage.setItem(preferenceStorageKey(key), value)
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

export function buildWorkbenchEventStreamUrl(
  template: string,
  sessionId: string,
  browserOrigin?: string,
): URL {
  const expanded = template.replace('{session_id}', encodeURIComponent(sessionId))
  const url = new URL(expanded, browserOrigin)
  if (browserOrigin) {
    const origin = new URL(browserOrigin)
    url.protocol = origin.protocol === 'https:' ? 'wss:' : 'ws:'
    url.host = origin.host
  }
  return url
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

export class WorkbenchRequestError extends Error {
  constructor(message: string, readonly status: number, readonly code = '', readonly receipt?: {
    accepted?: true; artifact_id?: string; version?: number
  }) {
    super(message)
    this.name = 'WorkbenchRequestError'
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
        ...(!(init.body instanceof FormData) ? { 'Content-Type': 'application/json' } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...init.headers,
      },
      signal: init.signal ?? AbortSignal.timeout(20000),
    })
    if (!response.ok) {
      if (response.status === 401)
        throw new WorkbenchRequestError('连接令牌无效，请在设置中更新', 401)
      if (response.status === 429) throw new WorkbenchRequestError('请求较多，请稍后重试', 429)
      const detail = await response.json().catch(() => ({}))
      throw new WorkbenchRequestError(
        typeof detail.detail === 'string'
          ? detail.detail
          : `请求失败（${response.status}），请重试`, response.status,
        typeof detail.code === 'string' ? detail.code : typeof detail.error === 'string' ? detail.error : '',
        detail.accepted === true ? {
          accepted: true,
          artifact_id: typeof detail.artifact_id === 'string' ? detail.artifact_id : undefined,
          version: typeof detail.version === 'number' && Number.isInteger(detail.version) ? detail.version : undefined,
        } : undefined,
      )
    }
    return response
  }
  async config(): Promise<ModelConfig> {
    return (await this.fetch('/config')).json()
  }
  async personalMemoryContext(actorId = 'main', signal?: AbortSignal): Promise<PersonalMemoryContext> {
    return (await this.fetch(`/personal/memories/context?actor_id=${encodeURIComponent(actorId)}`, { signal, cache: 'no-store' })).json()
  }
  async personalMemories(query: PersonalMemoryQuery, signal?: AbortSignal): Promise<PersonalMemoryPage> {
    const params = new URLSearchParams()
    for (const [key, value] of Object.entries(query)) if (value !== undefined && value !== '') params.set(key, String(value))
    return (await this.fetch(`/personal/memories?${params}`, { signal, cache: 'no-store' })).json()
  }
  async personalMemory(id: string, actorId = 'main', signal?: AbortSignal): Promise<PersonalMemory> {
    return (await this.fetch(`/personal/memories/${encodeURIComponent(id)}?actor_id=${encodeURIComponent(actorId)}`, { signal, cache: 'no-store' })).json()
  }
  async searchPersonalMemories(query: Omit<PersonalMemoryQuery, 'state' | 'offset'> & { query: string }, signal?: AbortSignal): Promise<PersonalMemorySearch> {
    const params = new URLSearchParams()
    for (const [key, value] of Object.entries(query)) if (value !== undefined && value !== '') params.set(key, String(value))
    return (await this.fetch(`/personal/memories/search?${params}`, { signal, cache: 'no-store' })).json()
  }
  async personalMemoryHistory(id: string, actorId = 'main', signal?: AbortSignal): Promise<{ history: PersonalMemoryHistoryEntry[] }> {
    return (await this.fetch(`/personal/memories/${encodeURIComponent(id)}/history?actor_id=${encodeURIComponent(actorId)}`, { signal, cache: 'no-store' })).json()
  }
  async personalMemoryGrants(id: string, actorId = 'main', signal?: AbortSignal): Promise<{ grants: PersonalMemoryGrant[] }> {
    return (await this.fetch(`/personal/memories/${encodeURIComponent(id)}/grants?actor_id=${encodeURIComponent(actorId)}`, { signal, cache: 'no-store' })).json()
  }
  async exportPersonalMemories(query: Pick<PersonalMemoryQuery, 'actor_id' | 'scope' | 'item_id'>): Promise<Record<string, unknown>> {
    const params = new URLSearchParams()
    for (const [key, value] of Object.entries(query)) if (value !== undefined && value !== '') params.set(key, String(value))
    return (await this.fetch(`/personal/memories/export?${params}`, { cache: 'no-store' })).json()
  }
  async legacyPersonalMemories(actorId = 'main', limit = 30, offset = 0, signal?: AbortSignal): Promise<PersonalLegacyMemoryPage> {
    const params = new URLSearchParams({ actor_id: actorId, limit: String(limit), offset: String(offset) })
    return (await this.fetch(`/personal/memories/legacy?${params}`, { signal, cache: 'no-store' })).json()
  }
  async preparePersonalMemoryRequest(body: PersonalMemoryPrepare): Promise<PersonalMemoryRequest> {
    return (await this.fetch('/personal/memory-requests', { method: 'POST', body: JSON.stringify(body) })).json()
  }
  async personalMemoryRequest(id: string, actorId = 'main'): Promise<PersonalMemoryRequest> {
    return (await this.fetch(`/personal/memory-requests/${encodeURIComponent(id)}?actor_id=${encodeURIComponent(actorId)}`, { cache: 'no-store' })).json()
  }
  async applyPersonalMemoryRequest(id: string, actorId: string, expectedFingerprint: string): Promise<PersonalMemoryMutationResult> {
    return (await this.fetch(`/personal/memory-requests/${encodeURIComponent(id)}/apply?actor_id=${encodeURIComponent(actorId)}`, { method: 'POST', body: JSON.stringify({ expected_fingerprint: expectedFingerprint }) })).json()
  }
  async discardPersonalMemoryRequest(id: string, actorId: string, expectedFingerprint: string): Promise<PersonalMemoryRequest> {
    return (await this.fetch(`/personal/memory-requests/${encodeURIComponent(id)}/discard?actor_id=${encodeURIComponent(actorId)}`, { method: 'POST', body: JSON.stringify({ expected_fingerprint: expectedFingerprint }) })).json()
  }
  async personalItems(query: { state?: string; query?: string; limit?: number; offset?: number } = {}, signal?: AbortSignal): Promise<PersonalItemPage> {
    const params = new URLSearchParams()
    for (const [key, value] of Object.entries(query)) if (value !== undefined && value !== '') params.set(key, String(value))
    return (await this.fetch(`/personal/items?${params}`, { signal, cache: 'no-store' })).json()
  }
  async personalItem(id: string, signal?: AbortSignal): Promise<PersonalItem> {
    return (await this.fetch(`/personal/items/${encodeURIComponent(id)}`, { signal, cache: 'no-store' })).json()
  }
  async createPersonalItem(body: PersonalItemInput): Promise<PersonalItem> {
    return (await this.fetch('/personal/items', { method: 'POST', body: JSON.stringify(body) })).json()
  }
  async updatePersonalItem(id: string, body: PersonalItemUpdate): Promise<PersonalItem> {
    return (await this.fetch(`/personal/items/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(body) })).json()
  }
  async controlPersonalItem(id: string, action: PersonalItemAction, body: PersonalItemControl): Promise<PersonalItem> {
    return (await this.fetch(`/personal/items/${encodeURIComponent(id)}/${action}`, { method: 'POST', body: JSON.stringify(body) })).json()
  }
  async personalItemHistory(id: string, signal?: AbortSignal): Promise<{ history: PersonalItem[] }> {
    return (await this.fetch(`/personal/items/${encodeURIComponent(id)}/history`, { signal, cache: 'no-store' })).json()
  }
  async personalItemOccurrences(id: string, signal?: AbortSignal): Promise<{ occurrences: PersonalItemExecution[] }> {
    return (await this.fetch(`/personal/items/${encodeURIComponent(id)}/occurrences`, { signal, cache: 'no-store' })).json()
  }
  async personalItemResults(query: { unread?: boolean; limit?: number; offset?: number } = {}, signal?: AbortSignal): Promise<PersonalItemResultsPage> {
    const params = new URLSearchParams()
    for (const [key, value] of Object.entries(query)) if (value !== undefined) params.set(key, String(value))
    return (await this.fetch(`/personal/item-results?${params}`, { signal, cache: 'no-store' })).json()
  }
  async personalItemResult(id: string, signal?: AbortSignal): Promise<PersonalItemResult> {
    return (await this.fetch(`/personal/item-results/${encodeURIComponent(id)}`, { signal, cache: 'no-store' })).json()
  }
  async readPersonalItemResult(id: string, body: PersonalItemReadInput): Promise<PersonalItemResult | PersonalItemReadReceipt> {
    return (await this.fetch(`/personal/item-results/${encodeURIComponent(id)}/read`, { method: 'POST', body: JSON.stringify(body) })).json()
  }
  async personalAgents(includeArchived = false): Promise<{ agents: PersonalAgent[] }> {
    return (await this.fetch(`/personal/agents?include_archived=${includeArchived}`)).json()
  }
  async personalTools(): Promise<{ tools: PersonalTool[] }> {
    return (await this.fetch('/personal/tools')).json()
  }
  async createPersonalAgent(body: PersonalAgentInput & { request_id: string }): Promise<PersonalAgent> {
    return (await this.fetch('/personal/agents', { method: 'POST', body: JSON.stringify(body) })).json()
  }
  async updatePersonalAgent(id: string, body: PersonalAgentUpdate): Promise<PersonalAgent> {
    return (await this.fetch(`/personal/agents/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(body) })).json()
  }
  async personalAgentHistory(id: string): Promise<{ revisions: PersonalAgent[] }> {
    return (await this.fetch(`/personal/agents/${encodeURIComponent(id)}/history`)).json()
  }
  async openPersonalSession(id: string): Promise<{ session_id: string; agent_id: string; version: number }> {
    return (await this.fetch(`/personal/agents/${encodeURIComponent(id)}/session`, { method: 'POST' })).json()
  }
  async currentPersonalAgent(sessionId: string): Promise<{ agent: PersonalAgent | null; read_only?: boolean; collaboration_id?: string }> {
    return (await this.fetch(`/personal/current?session_id=${encodeURIComponent(sessionId)}`)).json()
  }
  async personalCollaborations(sourceSessionId?: string): Promise<{ collaborations: PersonalCollaboration[] }> {
    const query = sourceSessionId ? `?source_session_id=${encodeURIComponent(sourceSessionId)}` : ''
    return (await this.fetch(`/personal/collaborations${query}`)).json()
  }
  async createPersonalCollaboration(body: PersonalCollaborationInput): Promise<PersonalCollaboration> {
    return (await this.fetch('/personal/collaborations', { method: 'POST', body: JSON.stringify(body) })).json()
  }
  async personalCollaboration(id: string): Promise<PersonalCollaboration> {
    return (await this.fetch(`/personal/collaborations/${encodeURIComponent(id)}`)).json()
  }
  async cancelPersonalCollaboration(id: string): Promise<PersonalCollaboration> {
    return (await this.fetch(`/personal/collaborations/${encodeURIComponent(id)}/cancel`, { method: 'POST', body: '{}' })).json()
  }
  async replyPersonalCollaboration(id: string, requestId: string, body: PersonalCollaborationReply): Promise<{
    status: 'answered'; request_id: string; response: PersonalCollaborationReply & { label?: string }
  }> {
    return (await this.fetch(`/personal/collaborations/${encodeURIComponent(id)}/questions/${encodeURIComponent(requestId)}/reply`, {
      method: 'POST', body: JSON.stringify(body),
    })).json()
  }
  async personalMessages(collaborationId: string): Promise<{ messages: PersonalAgentMessage[] }> {
    return (await this.fetch(`/personal/collaborations/${encodeURIComponent(collaborationId)}/messages`)).json()
  }
  async personalMessage(id: string): Promise<PersonalAgentMessage> {
    return (await this.fetch(`/personal/messages/${encodeURIComponent(id)}`)).json()
  }
  async acknowledgePersonalMessage(id: string, body: { expected_response_sha256: string }): Promise<PersonalAgentMessage> {
    return (await this.fetch(`/personal/messages/${encodeURIComponent(id)}/ack`, {
      method: 'POST', body: JSON.stringify(body),
    })).json()
  }
  async createPersonalMessage(body: PersonalMessageInput): Promise<PersonalCollaboration> {
    return (await this.fetch('/personal/messages', { method: 'POST', body: JSON.stringify(body) })).json()
  }
  async personalTeamTurns(teamSessionId: string): Promise<{ turns: PersonalTeamTurn[] }> {
    return (await this.fetch(`/personal/team-turns?team_session_id=${encodeURIComponent(teamSessionId)}`)).json()
  }
  async personalTeamTurn(id: string): Promise<PersonalTeamTurn> {
    return (await this.fetch(`/personal/team-turns/${encodeURIComponent(id)}`)).json()
  }
  async createPersonalTeamTurn(body: PersonalTeamTurnInput): Promise<PersonalTeamTurn> {
    return (await this.fetch('/personal/team-turns', { method: 'POST', body: JSON.stringify(body) })).json()
  }
  async cancelPersonalTeamTurn(id: string): Promise<PersonalTeamTurn> {
    return (await this.fetch(`/personal/team-turns/${encodeURIComponent(id)}/cancel`, { method: 'POST', body: '{}' })).json()
  }
  async personalArtifacts(query: PersonalArtifactQuery, signal?: AbortSignal): Promise<{ artifacts: PersonalArtifactSnapshot[] }> {
    const params = new URLSearchParams()
    for (const [key, value] of Object.entries(query)) if (value !== undefined) params.set(key, String(value))
    return (await this.fetch(`/personal/artifacts?${params}`, { signal, cache: 'no-store' })).json()
  }
  async personalArtifact(id: string, actorId: string, version?: number, signal?: AbortSignal): Promise<PersonalArtifactSnapshot> {
    const params = new URLSearchParams({ actor_id: actorId })
    if (version !== undefined) params.set('version', String(version))
    return (await this.fetch(`/personal/artifacts/${encodeURIComponent(id)}?${params}`, { signal, cache: 'no-store' })).json()
  }
  async personalArtifactVersions(id: string, actorId: string, signal?: AbortSignal): Promise<{ versions: PersonalArtifactSnapshot[] }> {
    return (await this.fetch(`/personal/artifacts/${encodeURIComponent(id)}/versions?${new URLSearchParams({ actor_id: actorId })}`, { signal, cache: 'no-store' })).json()
  }
  async personalArtifactPreview(id: string, actorId: string, version: number, signal?: AbortSignal): Promise<PersonalArtifactPreview> {
    return (await this.fetch(`/personal/artifacts/${encodeURIComponent(id)}/preview?${new URLSearchParams({ actor_id: actorId, version: String(version) })}`, { signal, cache: 'no-store' })).json()
  }
  async personalArtifactContent(id: string, actorId: string, version: number, signal?: AbortSignal): Promise<PersonalArtifactContent> {
    const response = await this.fetch(`/personal/artifacts/${encodeURIComponent(id)}/content?${new URLSearchParams({ actor_id: actorId, version: String(version) })}`, { signal, cache: 'no-store' })
    const sha256 = response.headers.get('X-Content-SHA256') || ''
    if (!/^[a-f0-9]{64}$/.test(sha256)) throw new Error('资料缺少版本校验，请重新加载')
    return { blob: await response.blob(), sha256 }
  }
  async createPersonalArtifact(body: PersonalArtifactInput): Promise<PersonalArtifactSnapshot> {
    return (await this.fetch('/personal/artifacts', { method: 'POST', body: JSON.stringify(body) })).json()
  }
  async revisePersonalArtifact(id: string, body: PersonalArtifactRevision): Promise<PersonalArtifactSnapshot> {
    return (await this.fetch(`/personal/artifacts/${encodeURIComponent(id)}/versions`, { method: 'POST', body: JSON.stringify(body) })).json()
  }
  async importPersonalArtifact(file: File, body: { actor_id: string; request_id: string; title?: string; kind: 'material'; session_id?: string }): Promise<PersonalArtifactSnapshot> {
    const params = new URLSearchParams()
    for (const [key, value] of Object.entries(body)) if (value !== undefined) params.set(key, value)
    const data = new FormData()
    data.append('file', file)
    return (await this.fetch(`/personal/artifacts/import?${params}`, { method: 'POST', body: data })).json()
  }
  async importPersonalArtifactVersion(id: string, file: File, body: { actor_id: string; request_id: string; expected_version: number; title?: string; kind: 'material' | 'output'; session_id?: string }): Promise<PersonalArtifactSnapshot> {
    const params = new URLSearchParams()
    for (const [key, value] of Object.entries(body)) if (value !== undefined) params.set(key, String(value))
    const data = new FormData()
    data.append('file', file)
    return (await this.fetch(`/personal/artifacts/${encodeURIComponent(id)}/versions/import?${params}`, { method: 'POST', body: data })).json()
  }
  async personalArtifactGrants(id: string, version: number, actorId: string, signal?: AbortSignal): Promise<{ grants: PersonalArtifactGrant[] }> {
    return (await this.fetch(`/personal/artifacts/${encodeURIComponent(id)}/versions/${version}/grants?${new URLSearchParams({ actor_id: actorId })}`, { signal, cache: 'no-store' })).json()
  }
  async sharePersonalArtifact(id: string, version: number, body: { actor_id: string; request_id: string; recipient_id: string }): Promise<PersonalArtifactGrant> {
    return (await this.fetch(`/personal/artifacts/${encodeURIComponent(id)}/versions/${version}/grants`, { method: 'POST', body: JSON.stringify(body) })).json()
  }
  async revokePersonalArtifactGrant(id: string, body: { actor_id: string; request_id: string; expected_revision: number }): Promise<PersonalArtifactGrant> {
    return (await this.fetch(`/personal/artifact-grants/${encodeURIComponent(id)}/revoke`, { method: 'POST', body: JSON.stringify(body) })).json()
  }
  async outputAsset(path: string, signal?: AbortSignal): Promise<Blob> {
    if (!/^\/api\/v1\/output-assets\/[a-f0-9]{64}\.(svg|png|jpg|webp|gif|pdf|csv|json|txt|md)$/.test(path)) throw new Error('文件地址无效')
    return (await this.fetch(path.replace('/api/v1', ''), { signal })).blob()
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
  async create(title?: string, model?: string, engine?: string): Promise<Session> {
    return (
      await this.fetch('/sessions', {
        method: 'POST',
        body: JSON.stringify({ title, model, engine }),
      })
    ).json()
  }
  async engines(): Promise<EnginesResponse> {
    return (await this.fetch('/engines')).json()
  }
  async runs(id: string): Promise<{ runs: Run[] }> {
    return (await this.fetch(`/sessions/${encodeURIComponent(id)}/runs?limit=200`)).json()
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
        body: JSON.stringify(command ? {
          command: message.content,
          runtime_mode: message.runtime_mode || 'default',
          edit_message_id: message.edit_message_id,
        } : { ...message, stream: true }),
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
