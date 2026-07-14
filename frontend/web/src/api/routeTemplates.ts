// Route templates mirror the backend capabilities contract.
// The backend is the source of truth; these are defaults used before capabilities are fetched.

import { ApiException } from './ApiException'

export const defaultRouteTemplates: Record<string, string> = {
  daemon_status: '/workbench/daemon/status',
  capabilities: '/workbench/capabilities',
  bootstrap: '/workbench/bootstrap',
  sessions: '/workbench/sessions',
  create_session: '/workbench/sessions',
  snapshot: '/workbench/sessions/{session_id}/snapshot',
  missions: '/workbench/sessions/{session_id}/missions',
  create_mission: '/workbench/sessions/{session_id}/missions',
  mission: '/workbench/sessions/{session_id}/missions/{mission_id}',
  issues: '/workbench/sessions/{session_id}/issues',
  issue: '/workbench/sessions/{session_id}/issues/{task_id}',
  mission_issues: '/workbench/sessions/{session_id}/missions/{mission_id}/issues',
  create_issue: '/workbench/sessions/{session_id}/missions/{mission_id}/issues',
  claim_issue: '/workbench/sessions/{session_id}/issues/{task_id}/claim',
  leases: '/workbench/sessions/{session_id}/leases',
  lease: '/workbench/sessions/{session_id}/leases/{lease_id}',
  release_lease: '/workbench/sessions/{session_id}/leases/{lease_id}/release',
  expire_leases: '/workbench/sessions/{session_id}/leases/expire',
  worktrees: '/workbench/sessions/{session_id}/worktrees',
  worktree: '/workbench/sessions/{session_id}/worktrees/{name}',
  keep_worktree: '/workbench/sessions/{session_id}/worktrees/{name}/keep',
  delete_worktree: '/workbench/sessions/{session_id}/worktrees/{name}',
  validation_runs: '/workbench/sessions/{session_id}/validation-runs',
  run_validation: '/workbench/sessions/{session_id}/validation-runs',
  validation_run: '/workbench/sessions/{session_id}/validation-runs/{run_id}',
  context_snapshots: '/workbench/sessions/{session_id}/context-snapshots',
  context_snapshot: '/workbench/sessions/{session_id}/context-snapshots/{snapshot_id}',
  record_context_health: '/workbench/sessions/{session_id}/issues/{task_id}/context-health',
  failures: '/workbench/sessions/{session_id}/failures',
  failure: '/workbench/sessions/{session_id}/failures/{failure_id}',
  events: '/workbench/sessions/{session_id}/events',
  event: '/workbench/sessions/{session_id}/events/{event_id}',
  event_stream: '/workbench/sessions/{session_id}/events/stream',
  messages: '/sessions/{session_id}/messages',
  list_messages: '/sessions/{session_id}/messages',
  send_message: '/sessions/{session_id}/messages',
  send_message_with_issue: '/sessions/{session_id}/messages',
  chat_environment: '/sessions/{session_id}/environment',
  add_chat_source: '/sessions/{session_id}/sources',
  resolve_permission: '/sessions/{session_id}/permissions/{call_id}/resolve',
  agents: '/workbench/sessions/{session_id}/agents',
  agent: '/workbench/sessions/{session_id}/agents/{agent_id}',
  upsert_agent_profile: '/workbench/sessions/{session_id}/agents/{agent_id}',
  approvals: '/workbench/sessions/{session_id}/approvals',
  approval: '/workbench/sessions/{session_id}/approvals/{approval_id}',
  resolve_approval: '/workbench/sessions/{session_id}/approvals/{approval_id}/resolve',
  intent_locks: '/workbench/sessions/{session_id}/missions/{mission_id}/intent-locks',
  create_intent_lock: '/workbench/sessions/{session_id}/missions/{mission_id}/intent-locks',
  intent_lock: '/workbench/sessions/{session_id}/missions/{mission_id}/intent-locks/{lock_id}',
  decisions: '/workbench/sessions/{session_id}/missions/{mission_id}/decisions',
  create_decision: '/workbench/sessions/{session_id}/missions/{mission_id}/decisions',
  decision: '/workbench/sessions/{session_id}/missions/{mission_id}/decisions/{decision_id}',
}

export function expandRoute(template: string, params: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (_match, key) => {
    const value = params[key]
    if (value === undefined || value === null) {
      throw new ApiException('InvalidUrl', `缺少路由参数: ${key}`)
    }
    return encodeURIComponent(String(value))
  })
}
