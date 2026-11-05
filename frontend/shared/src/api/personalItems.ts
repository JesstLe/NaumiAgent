import type { PersonalArtifactRef } from './personalArtifacts'
import type { Run } from './WorkbenchRuntimeClient'

export type PersonalItemState = 'active' | 'paused' | 'cancelled' | 'closed'
export type PersonalItemExecutionStatus = 'prepared' | 'queued' | 'running' | 'completed' | 'error' | 'timeout' | 'max_turns' | 'cancelled' | 'unknown' | 'blocked' | 'skipped'
export interface PersonalItemSchedule {
  mode: 'now' | 'once'
  local_time?: string
  timezone: string
  fold?: 0 | 1 | null
  due_at?: string
  misfire_policy: 'run_once' | 'skip'
  grace_seconds: number
}
export interface PersonalItemDirectory { path: string; pattern: string; max_files: number; max_bytes: number }
export interface PersonalItemPolicy {
  tool_names: string[]; artifact_operations: string[]; model_tier: 'fast' | 'capable' | 'reasoning'
  max_turns: number; max_cost_microusd: number; timeout_seconds: number
}
export interface PersonalItemInput {
  request_id: string; title: string; instructions: string; tags: string[]
  assignee_id: string; assignee_version: number; schedule: PersonalItemSchedule
  input_refs: PersonalArtifactRef[]; directories: PersonalItemDirectory[]
  policy?: PersonalItemPolicy; origin_session_id?: string
}
export type PersonalItemUpdate = PersonalItemInput & { expected_revision: number }
export interface PersonalItemControl { expected_revision: number; request_id: string; schedule?: PersonalItemSchedule }
export type PersonalItemAction = 'pause' | 'resume' | 'cancel' | 'run-now' | 'restart' | 'close'
export interface PersonalItemReadReceipt { [key: string]: unknown }
export interface PersonalItemExecution {
  occurrence_id: string; status: PersonalItemExecutionStatus; job_id: string; run_id: string; execution_session_id: string
  source_available?: boolean
  answer?: string; artifacts: PersonalArtifactRef[]; delivery_id?: string; delivery_sha256?: string
  delivered_at?: string | null; read_receipt?: PersonalItemReadReceipt | null; error?: string | Record<string, unknown>; receipt?: Record<string, unknown>
  steps?: Run['steps']
}
export interface PersonalItem {
  item_id: string; owner_key: string; revision: number; once_key: string; state: PersonalItemState
  definition: {
    title: string; instructions: string; tags: string[]
    assignee: { id: string; version: number; name: string; kind: string }
    origin: { actor_id: string; session_id: string }; route: Record<string, string>
    schedule: PersonalItemSchedule; inputs: { artifact_refs: PersonalArtifactRef[]; directories: PersonalItemDirectory[] }
    policy: PersonalItemPolicy; output: { destination: string; format: string }
  }
  definition_sha256: string; created_at: string; updated_at: string; occurrence?: PersonalItemExecution | null
}
export interface PersonalItemResult extends PersonalItemExecution {
  item_id: string; item_revision: number; title: string; assignee_id: string; assignee_name: string; created_at: string
}
export interface PersonalItemPage { items: PersonalItem[]; limit: number; offset: number; next_offset: number | null }
export interface PersonalItemResultsPage { results: PersonalItemResult[]; limit: number; offset: number; next_offset: number | null }
export interface PersonalItemReadInput { request_id: string; delivery_id: string; expected_delivery_sha256: string }
