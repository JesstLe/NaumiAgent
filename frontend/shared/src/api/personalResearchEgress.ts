import type { ResearchArtifactRef, ResearchResult } from './personalResearch'

export type ResearchEgressStatus = 'proposed' | 'pending' | 'approved' | 'started' | 'completed' | 'rejected' | 'revoked' | 'expired' | 'stale' | 'unavailable' | 'unknown'
export type ResearchEgressAction = 'search' | 'capture' | 'recheck'
export type ResearchEgressDecision = 'approve' | 'reject' | 'revoke'
export type ResearchEgressMutation = 'egress_decide' | 'egress_execute'
export interface ResearchEgressDestinationPlan {
  kind: 'search' | 'url'
  provider: 'duckduckgo' | 'brave' | 'direct'
  endpoint: string
  method: 'POST' | 'GET'
  parameters: Record<string, string>
  headers: Record<string, string>
  redirect_policy: 'none'
  max_redirects: 0
  trust_env: false
  tls_verify: true
  request_encoding: 'form-url-encoded' | 'query' | 'none'
  routing_digest: string
  credential_reference?: string
  credential_fingerprint?: string
}
export type ResearchEgressPayload =
  | { query: string; provider: 'duckduckgo' | 'brave'; max_results: number }
  | { url: string; max_age_days: number; source_id?: string; expected_source_revision?: number }
export interface ResearchEgressApproval {
  decision_id: string; digest: string; proposal_revision: number; approved_at: string; channel: string
}
export interface ResearchEgressExecution {
  request_id: string; status: 'started' | 'completed'; started_at: string
  phase?: 'started' | 'captured' | 'completed'; saved_stage?: boolean
  result?: { notebook_id: string; notebook_revision: number; discovery_id?: string; source_id?: string; source_revision?: number; source_ref?: ResearchArtifactRef | null; artifact_ref?: ResearchArtifactRef | null }
}
export interface ResearchEgressProposal {
  id: string; revision: number; digest: string; status: ResearchEgressStatus
  actor_id?: string; actor_name?: string; actor_version?: number; session_id?: string
  notebook_id?: string; notebook_revision?: number; notebook_title?: string; action?: ResearchEgressAction
  payload?: ResearchEgressPayload; destination_plan?: ResearchEgressDestinationPlan; reason?: string
  created_at?: string; expires_at?: string
  approval?: ResearchEgressApproval | null; execution?: ResearchEgressExecution | null
  dependencies?: { artifact_count: number; memory_count: number; source_count: number; notebook_revision: number }
}
export interface ResearchEgressView {
  availability: 'available' | 'source_unavailable'
  proposal: ResearchEgressProposal
  result?: ResearchResult
}
export interface ResearchEgressPage { proposals: ResearchEgressView[]; next_after: string | null }
export interface ResearchEgressRequest {
  status: 'completed' | 'pending' | 'unknown'; request_id: string; view?: ResearchEgressView
  resume?: { operation: 'egress_execute'; arguments: { id: string; request_id: string; expected_revision: number; expected_digest: string } }
}
export interface ResearchEgressApi {
  listPersonalResearchEgress(params?: { limit?: number; after?: string }, signal?: AbortSignal): Promise<ResearchEgressPage>
  getPersonalResearchEgress(id: string, signal?: AbortSignal): Promise<ResearchEgressView>
  getPersonalResearchEgressRequest(requestId: string, signal?: AbortSignal): Promise<ResearchEgressRequest>
  operatePersonalResearchEgress(operation: 'egress_propose' | ResearchEgressMutation, body: Record<string, unknown>, signal?: AbortSignal): Promise<ResearchEgressView>
}
export interface ResearchEgressPending {
  version: 1; request_id: string; operation: ResearchEgressMutation; proposal_id: string; workspace_key: string
}
export interface ResearchEgressArtifactTarget { actor_id: string; ref: ResearchArtifactRef }
