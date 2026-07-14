import axios, { type AxiosInstance, type AxiosRequestConfig, type AxiosError } from 'axios'
import { ApiException } from './ApiException'
import { defaultRouteTemplates, expandRoute } from './routeTemplates'
import type {
  DaemonStatusResponse,
  WorkbenchCapabilitiesResponse,
  WorkbenchSnapshot,
  SessionListResponse,
  Session,
  WorkbenchBootstrapResponse,
  MessageListResponse,
  MessageCreate,
  MessageResponse,
  IssuesResponse,
  Issue,
  MissionsResponse,
  Mission,
  Lease,
  Worktree,
  ValidationRun,
  Approval,
  AgentProfilesResponse,
  AgentProfileEnriched,
  ValidationRunsResponse,
  ContextSnapshotsResponse,
  FailuresResponse,
  ApprovalsResponse,
  LeasesResponse,
  WorktreesResponse,
  EventsResponse,
  DecisionsResponse,
  IntentLocksResponse,
  Decision,
  IntentLock,
  ChatSource,
  ChatSourceCreate,
  PermissionResolution,
  ChatEnvironmentResponse,
  SessionUpdate,
  GitDiffResponse,
} from './types'

export type TokenProvider = () => Promise<string | null>

interface ListOptions {
  limit?: number
  [key: string]: string | number | boolean | undefined
}

export class WorkbenchApiClient {
  private client: AxiosInstance
  private templates: Record<string, string>

  constructor(
    baseURL: string,
    private getToken: TokenProvider,
  ) {
    this.client = axios.create({
      baseURL,
      headers: { 'Content-Type': 'application/json' },
    })
    this.templates = { ...defaultRouteTemplates }

    this.client.interceptors.request.use(async (config) => {
      const token = await this.getToken()
      if (token) {
        config.headers.Authorization = `Bearer ${token}`
      }
      return config
    })
  }

  updateRouteTemplates(templates: Record<string, string>) {
    this.templates = { ...this.templates, ...templates }
  }

  private route(name: string, params: Record<string, string | number> = {}): string {
    const template = this.templates[name]
    if (!template) {
      throw new ApiException('InvalidUrl', `Unknown route template: ${name}`)
    }
    return expandRoute(template, params)
  }

  private async request<T>(config: AxiosRequestConfig): Promise<T> {
    try {
      const response = await this.client.request<T>(config)
      return response.data
    } catch (error) {
      throw this.mapError(error)
    }
  }

  private mapError(error: unknown): ApiException {
    if (!axios.isAxiosError(error)) {
      return new ApiException('NetworkFailure', error instanceof Error ? error.message : '未知错误', undefined, error)
    }

    const axiosError = error as AxiosError
    const status = axiosError.response?.status
    const detail = (axiosError.response?.data as { detail?: string } | undefined)?.detail
    const message = detail ?? axiosError.message

    if (axiosError.code === 'ECONNREFUSED' || axiosError.code === 'ERR_NETWORK') {
      return new ApiException('NetworkFailure', '无法连接到后端服务', status, axiosError)
    }
    if (status === 401) {
      return new ApiException('AuthFailed', '认证失败', status, axiosError)
    }
    if (status === 404) {
      return new ApiException('InvalidUrl', '请求的资源不存在', status, axiosError)
    }
    if (status === 422) {
      return new ApiException('InvalidResponse', detail ?? '请求参数错误', status, axiosError)
    }
    if (status && status >= 500) {
      return new ApiException('ServerError', '后端服务错误', status, axiosError)
    }
    return new ApiException('HttpStatus', message || '请求失败，请稍后重试', status, axiosError)
  }

  async fetchDaemonStatus(): Promise<DaemonStatusResponse> {
    return this.request<DaemonStatusResponse>({ method: 'GET', url: this.route('daemon_status') })
  }

  async fetchCapabilities(): Promise<WorkbenchCapabilitiesResponse> {
    return this.request<WorkbenchCapabilitiesResponse>({ method: 'GET', url: this.route('capabilities') })
  }

  async fetchBootstrap(): Promise<unknown> {
    return this.request<unknown>({ method: 'GET', url: this.route('bootstrap') })
  }

  async fetchSnapshot(sessionId: string): Promise<WorkbenchSnapshot> {
    return this.request<WorkbenchSnapshot>({ method: 'GET', url: this.route('snapshot', { session_id: sessionId }) })
  }

  async fetchSessions(): Promise<SessionListResponse> {
    return this.request<SessionListResponse>({ method: 'GET', url: this.route('sessions') })
  }

  async createSession(title?: string, model?: string, systemPrompt?: string): Promise<WorkbenchBootstrapResponse> {
    return this.request<WorkbenchBootstrapResponse>({
      method: 'POST',
      url: this.route('create_session'),
      data: { title, model, system_prompt: systemPrompt },
    })
  }

  async updateSession(sessionId: string, body: SessionUpdate): Promise<Session> {
    return this.request<Session>({
      method: 'PATCH',
      url: this.route('sessions', { session_id: sessionId }),
      data: body,
    })
  }

  async deleteSession(sessionId: string): Promise<unknown> {
    return this.request<unknown>({
      method: 'DELETE',
      url: this.route('sessions', { session_id: sessionId }),
    })
  }

  async fetchMessages(sessionId: string, page = 1, pageSize = 50): Promise<MessageListResponse> {
    return this.request<MessageListResponse>({
      method: 'GET',
      url: this.route('list_messages', { session_id: sessionId }),
      params: { page, page_size: pageSize },
    })
  }

  async sendMessage(sessionId: string, payload: MessageCreate): Promise<MessageResponse> {
    return this.request<MessageResponse>({
      method: 'POST',
      url: this.route('send_message', { session_id: sessionId }),
      data: payload,
    })
  }

  async fetchChatEnvironment(sessionId: string): Promise<ChatEnvironmentResponse> {
    return this.request<ChatEnvironmentResponse>({
      method: 'GET',
      url: this.route('chat_environment', { session_id: sessionId }),
    })
  }

  async fetchGitDiff(sessionId: string): Promise<GitDiffResponse> {
    return this.request<GitDiffResponse>({
      method: 'GET',
      url: this.route('git_diff', { session_id: sessionId }),
    })
  }

  async addChatSource(sessionId: string, source: ChatSourceCreate): Promise<ChatSource> {
    return this.request<ChatSource>({
      method: 'POST',
      url: this.route('add_chat_source', { session_id: sessionId }),
      data: source,
    })
  }

  async uploadChatSource(sessionId: string, file: File): Promise<ChatSource> {
    const formData = new FormData()
    formData.append('file', file)
    return this.request<ChatSource>({
      method: 'POST',
      url: this.route('upload_chat_source', { session_id: sessionId }),
      data: formData,
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  }

  async resolvePermission(sessionId: string, callId: string, resolution: PermissionResolution): Promise<{ status: string }> {
    return this.request<{ status: string }>({
      method: 'POST',
      url: this.route('resolve_permission', { session_id: sessionId, call_id: callId }),
      data: resolution,
    })
  }

  async fetchMissions(sessionId: string): Promise<MissionsResponse> {
    return this.request<MissionsResponse>({ method: 'GET', url: this.route('missions', { session_id: sessionId }) })
  }

  async createMission(sessionId: string, title: string, goal = ''): Promise<Mission> {
    return this.request<Mission>({
      method: 'POST',
      url: this.route('create_mission', { session_id: sessionId }),
      data: { title, goal },
    })
  }

  async fetchIssues(sessionId: string, options: ListOptions = {}): Promise<IssuesResponse> {
    return this.request<IssuesResponse>({
      method: 'GET',
      url: this.route('issues', { session_id: sessionId }),
      params: options,
    })
  }

  async fetchIssue(sessionId: string, taskId: string): Promise<Issue> {
    return this.request<Issue>({ method: 'GET', url: this.route('issue', { session_id: sessionId, task_id: taskId }) })
  }

  async createIssue(
    sessionId: string,
    missionId: string,
    issue: {
      title: string
      description?: string
      risk_level?: string
      parallel_mode?: string
      acceptance_criteria?: string[]
      blocked_by?: string[]
    },
  ): Promise<Issue> {
    return this.request<Issue>({
      method: 'POST',
      url: this.route('create_issue', { session_id: sessionId, mission_id: missionId }),
      data: issue,
    })
  }

  async claimIssue(sessionId: string, taskId: string, agentId?: string): Promise<Lease> {
    return this.request<Lease>({
      method: 'POST',
      url: this.route('claim_issue', { session_id: sessionId, task_id: taskId }),
      data: agentId ? { agent_id: agentId } : {},
    })
  }

  async fetchLeases(sessionId: string, options: ListOptions = {}): Promise<LeasesResponse> {
    return this.request<LeasesResponse>({
      method: 'GET',
      url: this.route('leases', { session_id: sessionId }),
      params: options,
    })
  }

  async releaseLease(sessionId: string, leaseId: string): Promise<Lease> {
    return this.request<Lease>({
      method: 'POST',
      url: this.route('release_lease', { session_id: sessionId, lease_id: leaseId }),
    })
  }

  async fetchWorktrees(sessionId: string, options: ListOptions = {}): Promise<WorktreesResponse> {
    return this.request<WorktreesResponse>({
      method: 'GET',
      url: this.route('worktrees', { session_id: sessionId }),
      params: options,
    })
  }

  async keepWorktree(sessionId: string, name: string, reason?: string): Promise<Worktree> {
    return this.request<Worktree>({
      method: 'POST',
      url: this.route('keep_worktree', { session_id: sessionId, name }),
      data: reason ? { reason } : {},
    })
  }

  async deleteWorktree(sessionId: string, name: string, force = false): Promise<unknown> {
    return this.request<unknown>({
      method: 'DELETE',
      url: this.route('delete_worktree', { session_id: sessionId, name }),
      params: { force },
    })
  }

  async fetchValidationRuns(sessionId: string, options: ListOptions = {}): Promise<ValidationRunsResponse> {
    return this.request<ValidationRunsResponse>({
      method: 'GET',
      url: this.route('validation_runs', { session_id: sessionId }),
      params: options,
    })
  }

  async runValidation(
    sessionId: string,
    payload: { task_id: string; argv: string[]; actor?: string; cwd?: string },
  ): Promise<ValidationRun> {
    return this.request<ValidationRun>({
      method: 'POST',
      url: this.route('run_validation', { session_id: sessionId }),
      data: payload,
    })
  }

  async fetchContextSnapshots(sessionId: string, options: ListOptions = {}): Promise<ContextSnapshotsResponse> {
    return this.request<ContextSnapshotsResponse>({
      method: 'GET',
      url: this.route('context_snapshots', { session_id: sessionId }),
      params: options,
    })
  }

  async fetchFailures(sessionId: string, options: ListOptions = {}): Promise<FailuresResponse> {
    return this.request<FailuresResponse>({
      method: 'GET',
      url: this.route('failures', { session_id: sessionId }),
      params: options,
    })
  }

  async fetchApprovals(sessionId: string, options: ListOptions = {}): Promise<ApprovalsResponse> {
    return this.request<ApprovalsResponse>({
      method: 'GET',
      url: this.route('approvals', { session_id: sessionId }),
      params: options,
    })
  }

  async resolveApproval(sessionId: string, approvalId: string, state: 'approved' | 'rejected', note?: string): Promise<Approval> {
    return this.request<Approval>({
      method: 'POST',
      url: this.route('resolve_approval', { session_id: sessionId, approval_id: approvalId }),
      data: { state, decision_note: note },
    })
  }

  async fetchAgents(sessionId: string): Promise<AgentProfilesResponse> {
    return this.request<AgentProfilesResponse>({ method: 'GET', url: this.route('agents', { session_id: sessionId }) })
  }

  async upsertAgentProfile(
    sessionId: string,
    agentId: string,
    profile: Partial<AgentProfileEnriched>,
  ): Promise<AgentProfileEnriched> {
    return this.request<AgentProfileEnriched>({
      method: 'POST',
      url: this.route('upsert_agent_profile', { session_id: sessionId, agent_id: agentId }),
      data: profile,
    })
  }

  async fetchEvents(sessionId: string, options: ListOptions = {}): Promise<EventsResponse> {
    return this.request<EventsResponse>({
      method: 'GET',
      url: this.route('events', { session_id: sessionId }),
      params: options,
    })
  }

  async fetchDecisions(sessionId: string, missionId: string, kind?: string): Promise<DecisionsResponse> {
    return this.request<DecisionsResponse>({
      method: 'GET',
      url: this.route('decisions', { session_id: sessionId, mission_id: missionId }),
      params: kind ? { kind } : {},
    })
  }

  async createDecision(
    sessionId: string,
    missionId: string,
    decision: Omit<Decision, 'id' | 'session_id' | 'mission_id' | 'created_at'>,
  ): Promise<Decision> {
    return this.request<Decision>({
      method: 'POST',
      url: this.route('create_decision', { session_id: sessionId, mission_id: missionId }),
      data: decision,
    })
  }

  async fetchIntentLocks(sessionId: string, missionId: string, active?: boolean): Promise<IntentLocksResponse> {
    return this.request<IntentLocksResponse>({
      method: 'GET',
      url: this.route('intent_locks', { session_id: sessionId, mission_id: missionId }),
      params: active !== undefined ? { active } : {},
    })
  }

  async createIntentLock(
    sessionId: string,
    missionId: string,
    lock: Omit<IntentLock, 'id' | 'session_id' | 'mission_id' | 'created_at' | 'updated_at'>,
  ): Promise<IntentLock> {
    return this.request<IntentLock>({
      method: 'POST',
      url: this.route('create_intent_lock', { session_id: sessionId, mission_id: missionId }),
      data: lock,
    })
  }
}
