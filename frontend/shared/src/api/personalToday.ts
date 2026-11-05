import type { PersonalArtifactRef } from './personalArtifacts'
import type { PersonalItemReadReceipt } from './personalItems'

export type TodayPriority = 'high' | 'normal' | 'low'
export type TodayAvailability = 'available' | 'stale' | 'source_unavailable'
export interface TodayItemRef {
  kind: 'item'; workspace_key: string; item_id: string; item_revision: number; once_key: string
  definition_sha256: string; assignee_id: string; assignee_version: number
}
export type TodayDeadlineInput = null
  | { kind: 'date'; local_date: string; timezone: string }
  | { kind: 'instant'; local_time: string; timezone: string; fold?: 0 | 1 }
export type TodayDeadline = Exclude<TodayDeadlineInput, null> & {
  boundary_at?: string; resolved_at?: string; timezone_sha256: string
  status?: 'none' | 'upcoming' | 'due' | 'overdue' | 'timezone_data_changed'
}
export interface TodayHumanCheck { checked: boolean; channel: string; checked_at: string; correction_revision: number }
export interface TodayDelivery {
  item_id: string; item_revision: number; occurrence_id: string; job_id: string; run_id: string
  publication_id: string; delivery_id: string; delivery_sha256: string; delivered_at: string
  read_receipt: PersonalItemReadReceipt | null; artifacts: PersonalArtifactRef[]; title: string | null; assignee_id: string
}
export interface TodayEntry {
  id: string; placement_id: string | null; metadata_id: string; metadata_revision: number; item_ref: TodayItemRef
  availability: TodayAvailability; title: string | null; assignee: { id: string; name: string; version: number } | null
  item_status: string | null; execution_status: string | null; due_at: string | null; deadline: TodayDeadline | null
  priority: TodayPriority; human_check: TodayHumanCheck | null; metadata_status: 'active' | 'archived'
  planned: boolean; disposition: 'include' | 'exclude'; report: TodayDelivery | null
}
export interface TodayPlacement { id: string; metadata_id: string; source_ref: TodayItemRef; disposition: 'include' | 'exclude' }
export interface TodayDay {
  id: string; revision: number; digest: string; date: string; timezone: string; timezone_sha256: string
  planner_id: string; workspace_key?: string; placements: TodayPlacement[]
}
export interface TodayRoutine {
  id: string; title: string | null; availability: TodayAvailability; status: string
  attention_code: string | null; next_due_at: string | null; plan_ref: Record<string, unknown>; assignee_id: string
}
export interface TodaySourceState { next_after?: string | null; partial: boolean; error?: string }
export interface TodayDayView {
  workspace_key: string; day: TodayDay; window: { start_at: string; end_at: string; date?: string; timezone?: string }
  entries: TodayEntry[]; routines: TodayRoutine[]; deliveries: TodayDelivery[]
  sources: { items: TodaySourceState; routines: TodaySourceState; deliveries: TodaySourceState }
  next_cursor: string | null; observed_at: string; consistency: 'live_keyset'; timezone_data_changed?: boolean
  projection_unavailable?: boolean
}
export interface TodayContext {
  workspace_key: string; default_planner: string; planners: { id: string; name: string; version: number }[]
  limits: Record<string, number>; server_now: string
}
export interface TodayQuery { date: string; timezone: string; planner_id?: string; limit?: number; cursor?: string }
export interface TodaySources { entries: TodayEntry[]; next_after: string | null; partial: boolean; consistency: 'live_keyset' }
export interface TodayMetadata {
  id: string; workspace_key: string; item_ref: TodayItemRef; priority: TodayPriority; deadline: TodayDeadline | null
  human_check: TodayHumanCheck | null; status: 'active' | 'archived'; revision: number; created_at: string; updated_at: string
}
export interface TodayMetadataView { metadata: TodayMetadata | null; entry: TodayEntry; metadata_id: string; revision: number; digest: string }
export interface TodayIdentity {
  day_id?: string; day_revision?: number; source_day_id?: string; source_day_revision?: number
  metadata_ids: string[]; change_id: string
}
export interface TodayMutation {
  accepted: true; request_id: string; operation: string; identity: TodayIdentity; projection_available: boolean
  view?: TodayDayView; metadata?: TodayMetadataView
}
export interface TodayRequest {
  status: 'unknown' | 'accepted'; request_id: string; accepted?: boolean; operation?: string; identity?: TodayIdentity
  projection_available?: boolean; view?: TodayDayView; metadata?: TodayMetadataView
}
export interface TodayHistory { history: Record<string, unknown>[]; next_after_revision?: number | null; next_after?: number | null }
interface TodayCAS { request_id: string; expected_revision: number }
export interface TodayWriteBodies {
  'set-metadata': { request_id: string; expected_revision: number; source_ref: TodayItemRef; deadline: TodayDeadlineInput; priority: TodayPriority; status: 'active' | 'archived' }
  place: { request_id: string; date: string; timezone: string; planner_id?: string; expected_day_revision: number; expected_metadata_revision: number; source_ref: TodayItemRef }
  reorder: TodayCAS & { day_id: string; placement_ids: string[] }
  remove: TodayCAS & { day_id: string; placement_id: string }
  rebase: { request_id: string; day_id: string; expected_day_revision: number; metadata_id: string; expected_metadata_revision: number; old_source_ref: TodayItemRef; new_source_ref: TodayItemRef; preserve_deadline: boolean; preserve_priority: boolean }
  check: TodayCAS & { metadata_id: string; source_ref: TodayItemRef; checked: boolean }
  move: TodayCAS & { day_id: string; placement_id: string; target_date: string; target_timezone: string; target_planner_id?: string; expected_target_revision: number }
}
export type TodayOperation = keyof TodayWriteBodies
export type TodayWrite = { [K in TodayOperation]: { operation: K; body: TodayWriteBodies[K] } }[TodayOperation]
