import type { PersonalArtifactRef } from './personalArtifacts'
import type { PersonalItem, PersonalItemDirectory, PersonalItemPolicy } from './personalItems'

export type RoutineStatus = 'active' | 'paused' | 'cancelled' | 'closed'
interface ScheduleBase {
  timezone: string
  starts_at: string | null
  ends_at: string | null
  dst_gap: 'skip'
  dst_fold: 'first' | 'second' | 'both'
}
export type RoutineScheduleInput = ScheduleBase & (
  | { kind: 'once'; local_time: string; fold?: 0 | 1 }
  | { kind: 'cron'; expression: string }
  | { kind: 'trading_day'; calendar_id: string; coverage_start: string; coverage_end: string; at: 'open' | 'close' | 'time'; local_time?: string; offset_minutes: number }
)
export interface RoutineLimits {
  max_executions: number
  total_cost_microusd: number
  max_concurrent: number
  max_pending: number
}
export interface RoutineRecovery {
  misfire_policy: 'skip' | 'latest' | 'catch_up'
  grace_seconds: number
  max_catch_up: number
  overlap_policy: 'queue' | 'skip' | 'allow'
}
export interface PersonalRoutineDefinitionInput {
  title: string
  instructions: string
  tags: string[]
  assignee_id: string
  assignee_version: number
  schedule: RoutineScheduleInput
  policy: PersonalItemPolicy
  input_refs: PersonalArtifactRef[]
  directories: PersonalItemDirectory[]
  limits: RoutineLimits
  recovery: RoutineRecovery
}
export interface PersonalRoutineDefinition extends Omit<PersonalRoutineDefinitionInput, 'assignee_id' | 'assignee_version' | 'input_refs' | 'directories' | 'schedule'> {
  schema_version: 1
  assignee: { id: string; version: number; name: string; kind: string }
  inputs: { artifact_refs: PersonalArtifactRef[]; directories: PersonalItemDirectory[] }
  route: Record<string, string>
  schedule: RoutineScheduleInput & { version?: number | string; timezone_sha256?: string; calendar_evidence?: Record<string, unknown> }
}
export interface RoutineCalendar {
  id: string; name: string; version: string; timezone: string; coverage_start: string; coverage_end: string
}
export interface PersonalRoutineContext {
  owner_key: string
  workspace_key: string
  calendars: RoutineCalendar[]
  default_policy: PersonalItemPolicy
}
export interface PersonalRoutinePreviewInput {
  request_id: string
  definition: PersonalRoutineDefinitionInput
  routine_id?: string
  expected_revision?: number
}
export interface PersonalRoutinePreview {
  preview_id: string
  definition_digest: string
  expires_at: string
  definition: PersonalRoutineDefinition
  schedule_preview: { due_at: string; local_time: string; fold: number; calendar_evidence?: Record<string, unknown> | null }[]
  base_routine_id: string | null
  base_revision: number | null
}
export interface PersonalRoutineBudget {
  reserved_slots: number
  started_executions: number
  settled_cost_microusd: number
  reserved_cost_microusd: number
  unknown_count: number
  remaining_cost_microusd: number
  remaining_slots: number
  accounting: 'rate_card_estimate_not_provider_billing'
}
export interface PersonalRoutine {
  id: string
  revision: number
  status: RoutineStatus
  epoch: string
  definition: PersonalRoutineDefinition
  definition_sha256: string
  next_due_at: string | null
  budget: PersonalRoutineBudget
  created_at: string
  updated_at: string
  attention_code?: string | null
}
export interface PersonalRoutineOccurrence {
  id: string
  routine_id: string
  epoch: string
  definition_revision: number
  nominal_due_at: string
  local_time: string
  fold: number
  status: 'reserved' | 'linked' | 'skipped' | 'blocked' | 'settled' | 'unknown'
  item_id: string | null
  child?: PersonalItem | null
  decision?: string | Record<string, unknown> | null
  reserved_cost_microusd: number
  settled_cost_microusd?: number | null
  created_at: string
}
export interface PersonalRoutineConfirm {
  request_id: string; preview_id: string; expected_digest: string; enabled: boolean
}
export interface PersonalRoutineRevise {
  request_id: string; preview_id: string; expected_digest: string; expected_revision: number
}
export interface PersonalRoutineControl { request_id: string; expected_revision: number }
export type PersonalRoutineAction = 'pause' | 'resume' | 'cancel' | 'extra-run'
export interface PersonalRoutinePage { routines: PersonalRoutine[]; next_after?: string | null }
export interface PersonalRoutineOccurrencesPage { occurrences: PersonalRoutineOccurrence[]; next_after: string | null }
export interface PersonalRoutineRequestReceipt {
  request_id: string
  operation: string | null
  status: 'applied' | 'pending' | 'unknown'
  result_kind: 'context' | 'routine' | 'preview' | 'occurrence' | null
  result_id: string | null
  result: PersonalRoutine | PersonalRoutinePreview | PersonalRoutineOccurrence | PersonalRoutineContext | null
}
