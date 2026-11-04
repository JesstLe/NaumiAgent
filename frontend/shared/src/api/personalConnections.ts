import type { PersonalItem } from './personalItems'
import type { PersonalArtifactRef } from './personalArtifacts'
import type { PersonalMemoryRef } from './personalMemories'

export interface PersonalConnectionContext {
  owner_key: string; workspace_key: string; client_configured: boolean; provider: 'google_calendar'
}
export interface CalendarGrant { actor_id: string; actor_version: number; revision: number; state: string }
export interface PersonalConnection {
  id: string; revision: number; auth_epoch: number; provider: 'google_calendar'
  email: string; email_verified: boolean; can_write: boolean; scopes: string[]
  status: 'connected' | 'reauth_required' | 'disconnected' | 'refresh_unknown' | 'credential_error'
  sync_status: string; last_sync_at: string | null; last_error_code: string | null
  revoke_status: string | null; updated_at: string; grants: CalendarGrant[]
}
export interface CalendarSourceRef {
  connection_id: string; auth_epoch: number; event_id: string; revision: number
  payload_sha256: string; occurrence_key: string
  grant_id?: string; grant_revision?: number
}
export interface CalendarDate { dateTime?: string; date?: string; timeZone?: string }
export interface CalendarEvent {
  connection_id: string; event_id: string; occurrence_key: string; revision: number; etag: string
  status: string; summary: string; description: string; start: CalendarDate; end: CalendarDate
  html_link: string | null; recurring_event_id: string | null; original_start_time: CalendarDate | null
  fetched_at: string; source_ref: CalendarSourceRef
}
export interface CalendarEventPage { events: CalendarEvent[]; limit: number; offset: number; next_offset: number | null }
export interface CalendarOAuthIntent {
  intent_id: string
  status: 'pending' | 'exchanging' | 'connected' | 'denied' | 'expired' | 'cancelled' | 'interrupted' | 'exchange_unknown'
  authorization_url: string | null; expires_at: string; connection_id?: string | null; error_code?: string | null
}
export interface CalendarActionBody {
  summary: string; description: string
  start: { dateTime: string; timeZone: string }; end: { dateTime: string; timeZone: string }
}
export interface CalendarAction {
  id: string; revision: number
  status: 'draft' | 'approved' | 'dispatching' | 'unknown' | 'submitted' | 'verified' | 'conflict' | 'rejected' | 'expired' | 'cancelled'
  digest: string; connection_id: string; auth_epoch: number; actor_id: string; account_email: string
  operation: 'insert' | 'update'; event_id: string | null; base_etag: string | null; body: CalendarActionBody
  created_at: string; updated_at: string; expires_at: string; source_refs: CalendarSourceRef[]
  artifact_refs: PersonalArtifactRef[]; memory_refs: PersonalMemoryRef[]; memory_item_id: string | null
  approval: { revision: number; digest: string; expires_at: string; receipt_id: string } | null
  result: { html_link: string | null; etag: string | null; readback_at: string | null; receipt_sha256: string | null } | null
  job_id?: string | null; run_id?: string | null; last_error_code?: string | null
}
export interface CalendarActionInput {
  request_id: string; connection_id: string; actor_id: string; source_refs: CalendarSourceRef[]
  operation: 'insert' | 'update'; target_event_id?: string; base_etag?: string
  body: CalendarActionBody; expires_at: string
}
export interface CalendarActionRevision { request_id: string; expected_revision: number; body: CalendarActionBody; expires_at: string }
export interface CalendarMutation { request_id: string; expected_revision: number }
export interface CalendarEventItemInput {
  request_id: string; event_revision: number; actor_id: string; title: string; instructions: string
  schedule: { mode: 'now' | 'once'; timezone: string; local_time?: string; fold?: 0 | 1; misfire_policy: 'run_once'; grace_seconds: 3600 }
}
export interface PersonalConnectionRequest {
  request_id: string; operation: string; status: 'applied' | 'pending' | 'unknown'
  result_kind: 'context' | 'connection' | 'oauth' | 'action' | 'item' | null
  result_id: string | null; result?: PersonalConnectionContext | PersonalConnection | CalendarOAuthIntent | CalendarAction | PersonalItem | null
  fingerprint: string | null
}
