export type NotificationKind = 'item_result' | 'item_failure' | 'calendar_approval' | 'followup_proposal'
export type NotificationFilter = 'unread' | 'attention' | 'all'
export interface NotificationQuietWindow { weekdays: number[]; start: string; end: string; full_day: boolean }
export interface NotificationPreferenceInput {
  enabled: boolean; timezone: string; quiet_hours: NotificationQuietWindow[]; muted_actor_ids: string[]
  debounce_seconds: number; max_delay_seconds: number; bypass_kinds: ('item_failure' | 'calendar_approval')[]; include_existing: boolean
}
export interface NotificationPreferences extends NotificationPreferenceInput {
  id: string; workspace_key: string; revision: number; timezone_sha256: string
  cutover_at: string | null; created_at: string | null; updated_at: string | null
}
export interface NotificationQuietState {
  quiet: boolean; next_release: string | null; timezone_sha256: string; timezone_data_changed: boolean
  windows: { start_at: string; end_at: string }[]; clock_rollback?: boolean; hold_reason?: string | null
}
export interface NotificationPreferencesView { preferences: NotificationPreferences; revision: number; digest: string; quiet: NotificationQuietState }
export interface NotificationEventRef { event_id: string; event_digest: string }
export interface PersonalNotificationEntry {
  id: string; event_digest: string; kind: NotificationKind; actor_id: string; actor_name: string | null
  title: string | null; occurred_at: string; availability: 'available' | 'resolved' | 'source_unavailable'
  priority: 'normal' | 'high'; unread: boolean; notice_read: Record<string, unknown> | null
  source_ref: Record<string, unknown>; jump: { kind: 'result' | 'item' | 'calendar_action' | 'followup'; id: string } | null; batch_id: string | null
}
export interface NotificationQuery { filter?: NotificationFilter; actor_id?: string; limit?: number; after?: string }
export interface NotificationPage {
  entries: PersonalNotificationEntry[]; next_after: string | null; partial: boolean; consistency: 'live_keyset'
  unread_count: number; attention_count: number
}
export interface NotificationContext {
  workspace_key: string; owner_key: string; configured: boolean; preferences: NotificationPreferencesView
  actors: { id: string; name: string; version: number }[]; unread_count: number; attention_count: number; partial: boolean
  worker: { last_error_code: string | null; last_tick_at: string | null }; quiet: NotificationQuietState
}
export interface NotificationBatch {
  id: string; digest: string; workspace_key: string; actor_id: string; kind: NotificationKind
  member_refs: NotificationEventRef[]; entries: PersonalNotificationEntry[]; created_at: string
  presentation_status: 'ready' | 'claimed' | 'presented'; quiet: NotificationQuietState; prompt_available: boolean
}
export interface NotificationPrompt { batch: NotificationBatch | null; quiet: NotificationQuietState; partial: boolean }
export type NotificationOperation = 'preferences' | 'claim' | 'present' | 'read'
export interface NotificationMutationIdentity {
  preferences_id?: string; preferences_revision?: number; batch_id?: string; batch_digest?: string
  claim_id?: string; client_id?: string; event_refs?: NotificationEventRef[]; notice_read_ids?: string[]; presentation_id?: string
}
export interface NotificationMutation {
  accepted: true; request_id: string; operation: string; identity: NotificationMutationIdentity; projection_available: boolean
  preferences?: NotificationPreferencesView; batch?: NotificationBatch
}
export interface NotificationRequest {
  status: 'unknown' | 'accepted'; request_id: string; operation?: string; identity?: NotificationMutationIdentity; projection_available?: boolean
}
export interface NotificationPreferenceWrite extends NotificationPreferenceInput { request_id: string; expected_revision: number }
export interface NotificationClaim { request_id: string; batch_id: string; expected_digest: string; client_id: string }
export interface NotificationPresentation {
  request_id: string; batch_id: string; batch_digest: string; client_id: string; claim_id: string; event_refs: NotificationEventRef[]
}
export interface NotificationRead { request_id: string; event_refs: NotificationEventRef[] }
