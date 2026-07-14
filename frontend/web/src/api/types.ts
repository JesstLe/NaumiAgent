// TypeScript DTOs matching the Python backend Workbench API contract.
// Sources: src/naumi_agent/api/routes/workbench.py, messages.py, ws.py,
//          src/naumi_agent/api/schemas.py, src/naumi_agent/workbench/models.py

export type ParallelMode = 'exclusive' | 'cooperative' | 'competitive' | 'exploratory'
export type RiskLevel = 'low' | 'medium' | 'high' | 'critical'
export type LeaseState = 'active' | 'released' | 'expired'
export type ApprovalState = 'waiting' | 'approved' | 'rejected' | 'not_required'
export type DecisionKind = 'principle' | 'architecture' | 'policy' | 'temporary' | 'experiment'
export type DecisionStrength = 'advisory' | 'required' | 'blocking'
export type FailureKind =
  | 'lease_expired'
  | 'agent_timeout'
  | 'test_failed'
  | 'merge_conflict'
  | 'review_rejected'
  | 'scope_violation'
  | 'budget_exceeded'
  | 'context_stale'
  | 'permission_denied'
  | 'worktree_dirty'
export type ContextHealth = 'good' | 'stale' | 'overloaded' | 'missing' | 'conflicted'
export type TaskStatus = 'pending' | 'in_progress' | 'blocked' | 'completed'
export type WorktreeStatus = 'clean' | 'dirty' | 'missing' | 'kept'
export type EventSeverity = 'info' | 'warning' | 'error' | 'critical'
export type ProposalState = 'open' | 'approved' | 'rejected' | 'converted'

export interface Task {
  id: string
  session_id: string
  subject: string
  description: string
  status: TaskStatus
  active_form: string | null
  owner: string | null
  blocks: string[]
  blocked_by: string[]
  created_at: string
  updated_at: string
}

export interface Mission {
  id: string
  session_id: string
  title: string
  goal: string
  status: string
  created_at: string
  updated_at: string
}

export interface Issue {
  session_id: string
  task_id: string
  mission_id: string
  parallel_mode: ParallelMode
  risk_level: RiskLevel
  requires_human_approval: boolean
  acceptance_criteria: string[]
  expected_artifacts: string[]
  related_branch: string
  related_worktree: string
  related_pr: string
  created_at: string
  updated_at: string
  task: Task | null
}

export interface Lease {
  id: string
  session_id: string
  task_id: string
  agent_id: string
  state: LeaseState
  expires_at: string
  worktree_name: string
  created_at: string
  updated_at: string
  task: Task | null
}

export interface Worktree {
  name: string
  path: string
  branch: string
  base_ref: string
  status: WorktreeStatus
  task_id: string
  dirty_files: number
  commits_ahead: number
  created_at: string
  updated_at: string
  kept_reason: string
  metadata: Record<string, string>
  removable: boolean
  task: Task | null
}

export interface ValidationRun {
  id: string
  session_id: string
  task_id: string
  actor: string
  command: string[]
  cwd: string
  status: 'passed' | 'failed'
  exit_code: number
  output: string
  started_at: string
  completed_at: string
  task: Task | null
}

export interface Failure {
  id: string
  session_id: string
  task_id: string
  kind: FailureKind
  title: string
  detail: string
  source_id: string
  status: string
  created_at: string
  task: Task | null
}

export interface Approval {
  id: string
  session_id: string
  mission_id: string
  task_id: string
  state: ApprovalState
  title: string
  detail: string
  requester: string
  reviewer: string
  decision_note: string
  created_at: string
  updated_at: string
  task: Task | null
}

export interface AgentProfileEnriched {
  id: string
  session_id: string
  name: string
  role: string
  capabilities: string[]
  permissions: string[]
  max_parallel_tasks: number
  status: 'idle' | 'busy' | 'stale' | 'offline'
  last_heartbeat_at: string
  created_at: string
  updated_at: string
  current_lease: Lease | null
  current_issue: Issue | null
}

export interface ContextSnapshot {
  id: string
  session_id: string
  agent_id: string
  task_id: string
  health: ContextHealth
  reasons: string[]
  created_at: string
  task: Task | null
}

export interface Event {
  id: string
  session_id: string
  type: string
  actor: string
  subject_id: string
  payload: Record<string, unknown>
  timestamp: string
  correlation_id: string | null
  parent_event_id: string | null
  severity: EventSeverity
  task: Task | null
}

export interface Bid {
  id: string
  session_id: string
  task_id: string
  agent_id: string
  confidence: number
  estimate_minutes: number
  eta: string
  note: string
  created_at: string
  updated_at: string
}

export interface Proposal {
  id: string
  session_id: string
  mission_id: string
  task_id: string
  agent_id: string
  title: string
  impact_scope: string
  intended_files: string[]
  validation_plan: string[]
  risk_level: RiskLevel
  questions: string[]
  state: ProposalState
  decision_note: string
  converted_issue_id: string
  created_at: string
  updated_at: string
}

export interface IntentLock {
  id: string
  session_id: string
  mission_id: string
  rule: string
  blocked_paths: string[]
  allowed_paths: string[]
  require_proposal_for_risk: RiskLevel
  active: boolean
  created_by: string
  created_at: string
  updated_at: string
}

export interface Decision {
  id: string
  session_id: string
  mission_id: string
  kind: DecisionKind
  title: string
  content: string
  actor: string
  strength: DecisionStrength
  created_at: string
}

export interface WorkbenchSnapshotSummary {
  current_mission_title: string
  active_agents: number
  open_issues: number
  blocked_issues: number
  pending_approvals: number
  failed_validations: number
}

export interface WorkbenchSnapshot {
  version: 1
  session_id: string
  summary: WorkbenchSnapshotSummary
  missions: Mission[]
  agent_profiles: AgentProfileEnriched[]
  intent_locks: IntentLock[]
  decisions: Decision[]
  tasks: Task[]
  issues: Issue[]
  leases: Lease[]
  bids: Bid[]
  proposals: Proposal[]
  failures: Failure[]
  events: Event[]
  validation_runs: ValidationRun[]
  context_snapshots: ContextSnapshot[]
  approvals: Approval[]
  worktrees: Worktree[]
}

export interface DaemonStatusResponse {
  status: string
  version: string
  pid: number
  host: string
  port: number
  started_at: string
  workspace_count: number
  workspace_root: string
  workspace_name: string
  api_base_url: string
  workbench_base_url: string
  event_stream_url_template: string
  auth_mode: string
}

export interface WorkbenchCapabilitiesResponse {
  supports_daemon_management: boolean
  supports_workspace_registry: boolean
  supports_validation_runner: boolean
  supports_event_stream: boolean
  supports_cloud_sync: boolean
  supported_locales: string[]
  default_locale: string
  protocol_version: number
  supported_resources: string[]
  supported_actions: string[]
  route_templates: Record<string, string>
  allowed_validation_commands: string[][]
  agent_stale_threshold_seconds: number
  agent_offline_threshold_seconds: number
}

export interface Session {
  id: string
  title: string | null
  model: string
  created_at: string
  updated_at: string
  message_count: number
  total_tokens: number
  total_cost_usd: number
  status: string
}

export interface SessionListResponse {
  sessions: Session[]
  total: number
  page: number
  page_size: number
}

export interface WorkbenchBootstrapResponse {
  daemon_status: DaemonStatusResponse
  capabilities: WorkbenchCapabilitiesResponse
  sessions: Session[]
  total_sessions: number
  selected_session_id: string | null
  snapshot: WorkbenchSnapshot | null
}

export interface MessageListResponse {
  messages: MessageResponse[]
  total: number
}

export interface MessageCreate {
  content: string
  stream?: boolean
  runtime_mode?: 'default' | 'plan' | 'bypass'
  workbench_issue?: {
    mission_id: string
    title: string
    description?: string
    blocked_by?: string[]
    acceptance_criteria?: string[]
    parallel_mode?: ParallelMode
    risk_level?: RiskLevel
  }
  source_ids?: string[]
  linked_issue_id?: string
}

export interface MessageResponse {
  id: string
  role: string
  content: string
  timestamp: string
  metadata: Record<string, unknown>
  model?: string
}

export interface ChatSource {
  id: string
  kind: 'file' | 'screenshot'
  title: string
  path: string
  run_id: string
  created_at: string
}

export interface ChatSourceCreate {
  path: string
  kind?: 'file' | 'screenshot'
  title?: string
}

export interface PermissionResolution {
  decision: 'allow' | 'deny' | 'bypass'
}

export interface SessionUpdate {
  title?: string
  model?: string
  system_prompt?: string
}

export interface ChatEnvironmentResponse {
  session_id: string
  workspace_root: string
  workspace_name: string
  sources: ChatSource[]
}

export interface GitDiffFile {
  path: string
  status: string
  stage: string
  additions: number
  deletions: number
  patch: string
}

export interface GitDiffResponse {
  available: boolean
  branch: string
  upstream: string
  ahead: number
  behind: number
  error: string
  files: GitDiffFile[]
}

export type RuntimeMode = 'default' | 'plan' | 'bypass'

export interface MissionsResponse {
  missions: Mission[]
  status: string | null
  limit: number
}

export interface AgentProfilesResponse {
  agent_profiles: AgentProfileEnriched[]
  status: string | null
  limit: number
}

export interface IssuesResponse {
  issues: Issue[]
  mission_id: string | null
  risk_level: string | null
  status: string | null
  limit: number
}

export interface ValidationRunsResponse {
  validation_runs: ValidationRun[]
  task_id: string | null
  status: string | null
  limit: number
}

export interface ContextSnapshotsResponse {
  context_snapshots: ContextSnapshot[]
  task_id: string | null
  agent_id: string | null
  health: string | null
  limit: number
}

export interface FailuresResponse {
  failures: Failure[]
  task_id: string | null
  status: string | null
  kind: string | null
  limit: number
}

export interface ApprovalsResponse {
  approvals: Approval[]
  state: string | null
  mission_id: string | null
  task_id: string | null
  limit: number
}

export interface LeasesResponse {
  leases: Lease[]
  state: string | null
  task_id: string | null
  agent_id: string | null
  limit: number
}

export interface WorktreesResponse {
  worktrees: Worktree[]
  task_id: string | null
  status: string | null
  limit: number
}

export interface EventsResponse {
  events: Event[]
  event_type: string | null
  subject_id: string | null
  actor: string | null
  since: string | null
  severity: string | null
  correlation_id: string | null
  parent_event_id: string | null
  limit: number
}

export interface DecisionsResponse {
  decisions: Decision[]
  mission_id: string
  kind: string | null
}

export interface IntentLocksResponse {
  intent_locks: IntentLock[]
  mission_id: string
  active: boolean | null
}
