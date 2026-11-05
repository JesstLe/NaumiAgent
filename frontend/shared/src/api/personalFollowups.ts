import type { CalendarAction, CalendarActionBody, CalendarDate } from './personalConnections'
import type { TodayDeadline, TodayItemRef } from './personalToday'

export type FollowupChangeKind = 'created' | 'updated' | 'cancelled' | 'unavailable' | 'restored'
export type FollowupChangeField = 'summary' | 'description' | 'start' | 'end' | 'status' | 'available'
export type FollowupTrigger = { kind: 'deadline'; item_ids: string[]; offsets_minutes: number[] }
  | { kind: 'calendar_change'; connection_id: string; event_id: string | null; change_kinds: FollowupChangeKind[]; fields: FollowupChangeField[] }
export interface FollowupRuleBody {
  name: string; actor_id: string; actor_version: number; status: 'active' | 'paused'; timezone: string; include_existing: boolean
  max_lateness_minutes: number; freshness_seconds: number; misfire_policy: 'latest' | 'skip'; trigger: FollowupTrigger
}
export interface FollowupRule {
  id: string; workspace_key: string; revision: number; digest: string; body: Omit<FollowupRuleBody, 'status'> & { status: 'active' | 'paused' | 'archived' }
  epoch: string; cutover_at: string; source_binding: Record<string, unknown> | null; created_at: string; updated_at: string
}
export interface FollowupContext {
  workspace_key: string; owner_key: string; configured: boolean; actors: { id: string; name: string; version: number }[]
  connections: { id: string; email: string; can_write: boolean; last_sync_at: string | null; status: string }[]
  worker: { last_tick_at: string | null; last_error_code: string | null; partial: boolean }; server_now: string
}
export interface FollowupProposalRef { proposal_id: string; revision: 1; digest: string; workspace_key: string }
export interface FollowupMetadataRef { metadata_id: string; metadata_revision: number; metadata_digest: string; workspace_key: string; item_ref: TodayItemRef }
export interface FollowupChangeRef { connection_id: string; connection_instance: string; auth_epoch: number; change_id: string; sequence: number; change_digest: string; grant_id: string; grant_revision: number }
export interface FollowupEvent { summary?: string; description?: string; start?: CalendarDate; end?: CalendarDate; status?: string; available?: boolean; [field: string]: unknown }
export type FollowupSource = { kind: 'deadline'; item_ref: TodayItemRef; metadata_ref: FollowupMetadataRef; current_metadata_revision: number; deadline: TodayDeadline }
  | { kind: 'calendar_change'; change_ref: FollowupChangeRef; change_kind: string; changed_fields: string[]; before: FollowupEvent | null; after: FollowupEvent | null; last_sync_at: string | null }
export interface FollowupReason { code: 'deadline_approaching' | 'deadline_due' | 'deadline_overdue' | 'calendar_existing' | 'calendar_changed' | 'calendar_cancelled' | 'calendar_unavailable' | 'calendar_restored'; nominal_at: string; observed_at: string; offset_minutes?: number }
export type FollowupTarget = { kind: 'today_place'; day_id: string; day_revision: number; metadata_id: string; placement_id: string; item_ref: TodayItemRef }
  | { kind: 'calendar_draft'; action_id: string; action_revision: number; digest: string; status: CalendarAction['status'] }
export interface FollowupProposal {
  id: string; workspace_key: string; actor_id: string; actor_version: number; proposal_ref: FollowupProposalRef
  decision: { revision: number; status: 'pending' | 'snoozed' | 'accepted' | 'dismissed'; presentation_epoch: number; snooze_until: string | null; effect_id: string | null }
  availability: 'available' | 'source_stale' | 'source_unavailable' | 'resolved'; title: string | null; reason: FollowupReason | null
  rule: { id: string; revision: number; digest: string; name: string } | null; source: FollowupSource | null
  effect_kind: 'today_place' | 'calendar_draft'; effect_status: 'none' | 'pending' | 'unknown' | 'blocked' | 'materialized'; target: FollowupTarget | null
  expires_at: string; created_at: string
}
export interface FollowupPage { proposals: FollowupProposal[]; next_after: string | null; partial: boolean; server_now: string }
export type FollowupFilter = 'pending' | 'all' | 'accepted' | 'dismissed' | 'snoozed'
export type FollowupAcceptTarget = { kind: 'today_place'; date: string; timezone: string; expected_day_revision: number; expected_metadata_revision: number }
  | { kind: 'calendar_draft'; body: CalendarActionBody; expires_at: string }
export interface FollowupIdentity { rule_id?: string; rule_revision?: number; proposal_id?: string; decision_revision?: number; effect_id?: string }
export interface FollowupMutation { accepted: true; request_id: string; operation: FollowupMutationOperation; identity: FollowupIdentity; projection_available: boolean; view?: FollowupRule | FollowupProposal }
export interface FollowupRequest { status: 'missing' | 'accepted'; request_id: string; operation?: FollowupMutationOperation; identity?: FollowupIdentity; projection_available?: boolean; view?: FollowupRule | FollowupProposal }
interface DecisionPayload { request_id: string; proposal_ref: FollowupProposalRef; expected_decision_revision: number }
export interface FollowupArguments {
  context: Record<string, never>; rules: Record<string, never>
  list: { status?: FollowupFilter; actor_id?: string; limit?: number; after?: string }; get: { proposal_id: string }; requests: { request_id: string }
  create_rule: { payload: { request_id: string; rule: FollowupRuleBody } }
  update_rule: { payload: { request_id: string; rule_id: string; expected_revision: number; rule: FollowupRuleBody } }
  set_status: { payload: { request_id: string; rule_id: string; expected_revision: number; status: 'active' | 'paused' | 'archived' } }
  dismiss: { payload: DecisionPayload }; snooze: { payload: DecisionPayload & { until: string } }
  accept: { payload: DecisionPayload & { target: FollowupAcceptTarget } }; reconcile: { payload: { request_id: string; effect_id: string } }
}
export type FollowupMutationOperation = 'create_rule' | 'update_rule' | 'set_status' | 'dismiss' | 'snooze' | 'accept' | 'reconcile'
export interface FollowupResponses {
  context: FollowupContext; rules: { rules: FollowupRule[] }; list: FollowupPage; get: FollowupProposal; requests: FollowupRequest
  create_rule: FollowupMutation; update_rule: FollowupMutation; set_status: FollowupMutation; dismiss: FollowupMutation; snooze: FollowupMutation; accept: FollowupMutation; reconcile: FollowupMutation
}
