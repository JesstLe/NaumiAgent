import type { ResearchArtifactRef } from './personalResearch'

export interface ResearchSessionContextMaterial {
  original_ref: ResearchArtifactRef
  title: string
  body_sha256: string
  read_count: number
}
export interface ResearchSessionContextChange {
  notebook_id: string
  source_id: string
  from_revision: number
  to_revision: number
  from_expired: boolean
  to_expired: boolean
}
export interface ResearchSessionContextPreview {
  workspace_key: string
  session_id: string
  actor_id: string
  actor_version: number
  actor_name: string
  session_title: string
  context_revision: number
  plan_digest: string
  status: 'ready' | 'current' | 'blocked'
  materials: ResearchSessionContextMaterial[]
  changes: ResearchSessionContextChange[]
  historical_notice: string
  error_code?: string
}
export interface ResearchSessionContextWrite {
  session_id: string
  actor_id: string
  request_id: string
  expected_context_revision: number
  expected_plan_digest: string
}
export interface ResearchSessionContextReceipt {
  status: 'completed'
  request_id: string
  workspace_key: string
  session_id: string
  actor_id: string
  context_revision: number
  updated_count: number
  view: ResearchSessionContextPreview
}
export interface ResearchSessionContextRequest {
  status: 'unknown' | 'pending' | 'completed' | 'failed'
  request_id: string
  receipt?: ResearchSessionContextReceipt
  code?: string
}
export interface ResearchSessionContextApi {
  getPersonalResearchSessionContext(sessionId: string, actorId: string, signal?: AbortSignal): Promise<ResearchSessionContextPreview>
  rollforwardPersonalResearchContext(body: ResearchSessionContextWrite, signal?: AbortSignal): Promise<ResearchSessionContextReceipt>
  getPersonalResearchContextRequest(requestId: string, signal?: AbortSignal): Promise<ResearchSessionContextRequest>
}
export interface ResearchSessionContextPending {
  version: 1
  request_id: string
  operation: 'context_rollforward'
  workspace_key: string
  session_id: string
  actor_id: string
}
