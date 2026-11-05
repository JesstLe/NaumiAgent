import type { ResearchArtifactRef } from './personalResearch'

export interface ResearchSemanticReviewer { id: string; version: number; name: string }
export interface ResearchSemanticPreview {
  workspace_key: string
  notebook_id: string
  notebook_revision: number
  author_id: string
  source_session_id: string
  reviewer: ResearchSemanticReviewer
  input_digest: string
  sources: { id: string; revision: number; title: string; body_sha256: string; bytes: number }[]
  materials: { artifact_id: string; version: number; title: string; owner_id: string; sharing: 'existing' | 'requires_owner_confirmation' | 'missing_owner_grant' }[]
  memory_count: number
  can_start: boolean
  blocking_codes: string[]
}
export interface ResearchSemanticAnchor {
  source_id: string
  source_revision: number
  body_sha256: string
  quote: string
  start: number
  end: number
  start_byte: number
  end_byte: number
}
export interface ResearchSemanticVerdict {
  claim_id: string
  verdict: 'supported' | 'contradicted' | 'insufficient'
  reason: string
  evidence: ResearchSemanticAnchor[]
}
export type ResearchSemanticStatus = 'proposed' | 'admitted' | 'running' | 'completed' | 'failed' | 'unknown' | 'cancelled'
export interface ResearchSemanticReview {
  id: string
  request_id: string
  workspace_key: string
  author_id: string
  notebook_id: string
  notebook_revision: number
  input_digest: string
  reviewer: ResearchSemanticReviewer
  collaboration_id: string
  step_id: 'review'
  job_id: string
  session_id: string
  run_id: string
  execution_status: ResearchSemanticStatus
  basis_status: 'current' | 'stale' | 'unavailable'
  result?: {
    digest: string
    verification_scope: 'provided_sources'
    independently_reviewed: true
    verdicts: ResearchSemanticVerdict[]
    summary: string
    artifact_ref?: ResearchArtifactRef
  }
}
export interface ResearchSemanticStart {
  id: string
  source_session_id: string
  reviewer_agent_id: string
  expected_reviewer_version: number
  expected_revision: number
  expected_input_digest: string
  request_id: string
}
export interface ResearchSemanticMutation { id: string; request_id: string }
export type ResearchSemanticOperation = 'semantic_start' | 'semantic_reconcile' | 'semantic_cancel'
export interface ResearchSemanticRequest {
  status: 'unknown' | 'pending' | 'completed'
  request_id: string
  review?: ResearchSemanticReview
}
export interface ResearchSemanticApi {
  getPersonalResearchSemanticContext(id: string, sourceSessionId: string, reviewerAgentId: string, signal?: AbortSignal): Promise<ResearchSemanticPreview>
  listPersonalResearchSemanticReviews(id: string, signal?: AbortSignal): Promise<{ reviews: ResearchSemanticReview[] }>
  getPersonalResearchSemanticReview(id: string, signal?: AbortSignal): Promise<ResearchSemanticReview>
  getPersonalResearchSemanticRequest(requestId: string, signal?: AbortSignal): Promise<ResearchSemanticRequest>
  startPersonalResearchSemanticReview(body: ResearchSemanticStart, signal?: AbortSignal): Promise<ResearchSemanticReview>
  operatePersonalResearchSemanticReview(operation: 'semantic_reconcile' | 'semantic_cancel', body: ResearchSemanticMutation, signal?: AbortSignal): Promise<ResearchSemanticReview>
}
export interface ResearchSemanticPending {
  version: 1
  operation: ResearchSemanticOperation
  request_id: string
  workspace_key: string
  author_id: string
  notebook_id: string
  notebook_revision: number
  source_session_id: string
  reviewer_id: string
  reviewer_version: number
  review_id: string
}
