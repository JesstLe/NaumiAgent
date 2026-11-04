import type { PersonalArtifactRef } from './personalArtifacts'

export type PersonalMemoryScopeKind = 'personal' | 'workspace' | 'item'
export type PersonalMemoryCategory = 'preference' | 'fact' | 'decision' | 'experience' | 'plan_template'
export type PersonalMemoryState = 'proposed' | 'active' | 'superseded' | 'rejected' | 'deleted'
export type PersonalMemoryFilter = PersonalMemoryState | 'conflict' | ''
export type PersonalMemoryOperation = 'create' | 'propose' | 'confirm' | 'revise' | 'delete' | 'reject' | 'share' | 'revoke' | 'import_legacy'

export interface PersonalMemoryScope {
  kind: PersonalMemoryScopeKind
  workspace_key: string
  item_id: string
}
export interface PersonalMemoryRef {
  memory_id: string
  revision: number
  content_sha256: string
  grant_id: string
  grant_revision: number
}
export interface PersonalMemorySource {
  kind: 'manual' | 'message' | 'artifact' | 'legacy' | 'compaction'
  authority: 'user' | 'assistant' | 'external' | 'unknown'
  locator: Record<string, unknown>
  excerpt: string | null
  excerpt_sha256: string
  artifact_refs: PersonalArtifactRef[]
  scope_ceiling: unknown
  captured_at: string
}
export interface PersonalMemory {
  memory_id: string
  actor_id: string
  owner_key: string
  scope: PersonalMemoryScope
  topic: string | null
  topic_key: string
  category: PersonalMemoryCategory
  revision: number
  state: PersonalMemoryState
  content: string | null
  content_sha256: string | null
  source: PersonalMemorySource | null
  source_sha256: string | null
  confirmation: Record<string, unknown> | null
  ref: PersonalMemoryRef | null
  active_ref: PersonalMemoryRef | null
  access: 'owned' | 'shared'
  source_available: boolean
  error?: string
  created_at: string
  updated_at: string
}
export interface PersonalMemoryGrant {
  grant_id: string
  memory_id: string
  memory_revision: number
  content_sha256: string
  recipient_actor_id: string
  recipient_version: number
  revision: number
  state: 'active' | 'revoked'
  source_refs: PersonalArtifactRef[]
  created_at: string
  updated_at: string
}
export interface PersonalMemoryContext {
  owner_key: string
  workspace_key: string
  actor_id: string
  index: Record<string, unknown>
  stats?: Record<string, unknown>
}
export interface PersonalMemoryPage {
  memories: PersonalMemory[]
  limit: number
  offset: number
  next_offset: number | null
}
export interface PersonalMemoryQuery {
  actor_id: string
  scope?: 'all' | PersonalMemoryScopeKind
  item_id?: string
  state?: PersonalMemoryFilter
  query?: string
  limit?: number
  offset?: number
}
export interface PersonalMemorySearch {
  memories: PersonalMemory[]
  diagnostics: Record<string, unknown>
}
export interface PersonalMemoryHistoryEntry {
  memory_id?: string
  revision?: number
  state?: string
  operation?: string
  content_sha256?: string | null
  source_sha256?: string | null
  created_at?: string
  updated_at?: string
  occurred_at?: string
  [key: string]: unknown
}
export interface PersonalLegacyMemory {
  legacy_id: string
  topic: string
  content: string
  content_sha256: string
  category: PersonalMemoryCategory
  source_kind: 'legacy'
}
export interface PersonalLegacyMemoryPage {
  memories: PersonalLegacyMemory[]
  limit: number
  offset: number
  next_offset: number | null
  diagnostics?: Record<string, unknown>
}
export interface PersonalMemoryPayload {
  request_id: string
  memory_id?: string
  expected_revision?: number
  expected_content_sha256?: string
  expected_active_ref?: PersonalMemoryRef | null
  topic?: string
  category?: PersonalMemoryCategory
  content?: string
  scope?: PersonalMemoryScopeKind
  item_id?: string
  recipient_actor_id?: string
  recipient_version?: number
  grant_id?: string
  legacy_id?: string
  source_message_index?: number
  source_message_sha256?: string
  source_quote?: string
  source_artifact_ref?: PersonalArtifactRef
}
export interface PersonalMemoryPrepare {
  request_id: string
  actor_id: string
  operation: PersonalMemoryOperation
  payload: PersonalMemoryPayload
}
export type PersonalMemoryMutationResult = PersonalMemory | PersonalMemoryGrant
export interface PersonalMemoryRequest {
  request_id: string
  actor_id: string
  workspace_key: string
  operation: PersonalMemoryOperation
  fingerprint: string
  state: 'prepared' | 'applied' | 'erased'
  result?: PersonalMemoryMutationResult | null
}
