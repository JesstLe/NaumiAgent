// Exact persistent research DTOs shared by clients and UI.
export type ResearchDomain = 'general' | 'competitor' | 'operations' | 'market'
export type ResearchKind = 'fact' | 'estimate' | 'unknown'
export type ResearchStance = 'act' | 'watch' | 'avoid' | 'undecided'
export type ResearchOperation = 'create' | 'revise' | 'search' | 'capture' | 'import' | 'recheck' | 'review' | 'publish'
export interface ResearchArtifactRef { artifact_id: string; version: number; sha256: string; grant_id: string; grant_revision: number }
export interface ResearchCitation { source_id: string; source_revision: number; quote: string; occurrence: number }
export interface ResearchQuantity { value: string; unit: string; currency: string | null; period: string | null }
export interface ResearchCalculation { operation: 'ratio' | 'percent' | 'sum' | 'difference'; inputs: string[]; expected: string; tolerance: string }
export interface ResearchUpstream { notebook_id: string; revision: number; claim_id: string }
export interface ResearchClaim {
  id: string; text: string; subject: string; kind: ResearchKind; citations: ResearchCitation[]
  quantity: ResearchQuantity | null; calculation: ResearchCalculation | null; assumptions: string[]; upstream_claims: ResearchUpstream[]
}
export interface ResearchConclusion { stance: ResearchStance; premises: string[]; invalidators: string[] }
export interface ResearchNotebook {
  id: string; revision: number; title: string; question: string; domain: ResearchDomain; claims: ResearchClaim[]
  conclusion: ResearchConclusion; updated_at?: string; actor_id?: string; workspace_key?: string
}
export interface ResearchSource {
  id: string; revision: number; title: string; origin: 'url' | 'artifact' | 'derived'; url: string | null
  captured_at: string; expires_at: string; status: 'available' | 'unavailable'; body_sha256: string | null
  artifact_ref: ResearchArtifactRef | null; upstream_unverified?: boolean
}
export interface ResearchFinding { code: string; claim_id: string | null; message: string; severity: 'error' | 'warning' }
export interface ResearchAnchor {
  claim_id: string; source_id: string; source_revision: number; quote: string
  start: number; end: number; byte_start: number; byte_end: number; sha256: string
}
export interface ResearchReview {
  id?: string; status: 'issues_found' | 'mechanically_checked' | 'needs_sources' | 'not_reviewed'
  semantic_verification: 'not_performed'; findings: ResearchFinding[]; anchors: ResearchAnchor[]; digest?: string; checked_at?: string
}
export interface ResearchView {
  notebook: ResearchNotebook; sources: ResearchSource[]; review: ResearchReview
  publication?: { artifact_id: string; version: number; sha256: string } | null
  availability?: 'available' | 'source_unavailable' | 'source_stale'
}
export interface ResearchSummary {
  id: string; revision: number; title: string; question: string; domain: ResearchDomain
  updated_at: string; source_count: number; claim_count: number; review_status: ResearchReview['status']
}
export interface ResearchPage { notebooks: ResearchSummary[]; next_after: string | null }
export interface ResearchContext { actor_id: string; workspace_key: string; server_now: string }
export interface ResearchSourceView { source: ResearchSource; body: string | null; availability: 'available' | 'source_unavailable' | 'source_stale' }
export interface ResearchHistory { history: { revision: number; title: string; updated_at: string; claim_count: number }[]; next_after_revision: number | null }
export interface ResearchDiscovery {
  discovery: { id: string; query: string; text: string; items: { title: string; url: string; snippet: string }[]; status: string }
  view: ResearchView
}
export type ResearchResult = ResearchView | ResearchDiscovery
export interface ResearchRequest {
  status: 'completed' | 'pending' | 'unknown'; request_id: string; view?: ResearchView; operation?: string
}
export interface ResearchApi {
  getPersonalResearchContext(signal?: AbortSignal): Promise<ResearchContext>
  listPersonalResearch(params?: { limit?: number; after?: string }, signal?: AbortSignal): Promise<ResearchPage>
  getPersonalResearch(id: string, revision?: number, signal?: AbortSignal): Promise<ResearchView>
  executePersonalResearch(operation: string, payload: Record<string, unknown>, signal?: AbortSignal): Promise<ResearchResult>
  getPersonalResearchSource(id: string, revision?: number, signal?: AbortSignal): Promise<ResearchSourceView>
  getPersonalResearchHistory(id: string, params?: { limit?: number; after_revision?: number }, signal?: AbortSignal): Promise<ResearchHistory>
  getPersonalResearchRequest?(id: string, signal?: AbortSignal): Promise<ResearchRequest>
}
export interface ResearchDraft { title: string; question: string; domain: ResearchDomain; claims: ResearchClaim[]; conclusion: ResearchConclusion }
export interface ResearchPending { version: 1; request_id: string; operation: ResearchOperation; target: string | null }

