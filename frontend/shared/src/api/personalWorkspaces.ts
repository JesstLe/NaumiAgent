export interface WorkspaceSourceIdentity {
  version: 1; owner_key: string; workspace_key: string; session_db_key: string; directory_instance_id: string; item_instance_id: string
}
export interface WorkspaceRecipient {
  version: 1; owner_key: string; workspace_key: string; session_db_key: string; directory_instance_id: string; actor_id: string; actor_version: number
}
export type WorkspaceReadScope = 'items' | 'results'
export interface WorkspaceTicket {
  version: 1; grant_id: string; grant_revision: 1; source: WorkspaceSourceIdentity; recipient: WorkspaceRecipient
  source_actor_id: string; source_actor_version: number; scopes: WorkspaceReadScope[]; expires_at: string; secret: string
}
export interface WorkspaceGrant {
  id: string; revision: number; status: 'active' | 'revoked'; source: WorkspaceSourceIdentity; recipient: WorkspaceRecipient
  source_actor_id: string; source_actor_version: number; scopes: WorkspaceReadScope[]; expires_at: string; created_at: string
}
export interface WorkspaceConnection {
  id: string; revision: number; status: 'active' | 'disconnected'; label: string; endpoint: string
  source: WorkspaceSourceIdentity; recipient: WorkspaceRecipient; source_actor_id: string; source_actor_version: number
  scopes: WorkspaceReadScope[]; expires_at: string
}
export interface WorkspaceContext { workspace_key: string; owner_key: string; configured: boolean; actors: { id: string; name: string; version: number }[]; connection_count: number; grant_count: number }
export interface WorkspaceItemRef {
  kind: 'item'; workspace_key: string; item_id: string; item_revision: number; once_key: string; definition_sha256: string; assignee_id: string; assignee_version: number
}
export interface WorkspaceOverviewEntry {
  item_id: string; item_revision: number; once_key: string; definition_sha256: string; title: string; state: 'active' | 'paused' | 'cancelled' | 'closed'
  assignee_id: string; assignee_name: string; schedule: { due_at: string | null; timezone: string }
  execution: { status: string; occurrence_id: string; job_id: string; run_id: string } | null; source_ref: WorkspaceItemRef
}
export interface WorkspaceSourceEnvelope {
  connection_id: string; connection_revision: number; source: WorkspaceSourceIdentity; recipient: WorkspaceRecipient
}
export interface WorkspaceOverviewSource extends WorkspaceSourceEnvelope {
  label: string; availability: 'available' | 'unavailable'; code: string | null; next_after: string | null; partial: boolean; entries: WorkspaceOverviewEntry[]
}
export interface WorkspaceOverview { sources: WorkspaceOverviewSource[]; partial: boolean; consistency: 'live_keyset' }
export interface WorkspaceReadResult extends WorkspaceSourceEnvelope {
  availability: 'available' | 'unavailable'; code: string | null; item: WorkspaceOverviewEntry | null
  result: { occurrence_id: string; status: string; job_id: string; run_id: string; answer: string
    artifacts: { artifact_id: string; version: number; sha256: string; title: string; content_text: string }[]
    delivery_id: string; delivery_sha256: string; read_receipt: null } | null
}
export interface WorkspaceRequest { status: 'missing' | 'accepted'; request_id: string; operation?: string; identity?: Record<string, unknown> }
export interface WorkspaceIdentityMutation { accepted: true; request_id: string; identity: { id: string; revision: number; status: 'revoked' | 'disconnected' } }
export interface WorkspaceOpaqueMutation { accepted: true; request_id: string; operation: 'issue' | 'connect'; workspace_key: string; identity: { id: string; revision: number; status: 'active' }; projection_available: false; content_transport: 'requests' }
export interface WorkspaceArguments {
  context: Record<string, never>; list: Record<string, never>; grants: Record<string, never>
  recipient: { actor_id: string }; requests: { request_id: string }
  issue: { payload: { request_id: string; recipient: WorkspaceRecipient; source_actor_id: string; source_actor_version: number; scopes: WorkspaceReadScope[]; expires_at: string } }
  revoke: { payload: { request_id: string; grant_id: string; expected_revision: number } }
  connect: { payload: { request_id: string; endpoint: string; label: string; ticket: WorkspaceTicket } }
  disconnect: { payload: { request_id: string; connection_id: string; expected_revision: number } }
  overview: { connection_id?: string; after?: string; limit?: number }
  result: { connection_id: string; item_id: string }
}
export interface WorkspaceResponses {
  context: WorkspaceContext; list: { connections: WorkspaceConnection[] }; grants: { grants: WorkspaceGrant[] }
  recipient: { recipient: WorkspaceRecipient }; requests: WorkspaceRequest
  issue: { accepted: true; request_id: string; grant: WorkspaceGrant; ticket: WorkspaceTicket } | WorkspaceOpaqueMutation
  connect: { accepted: true; request_id: string; connection: WorkspaceConnection } | WorkspaceOpaqueMutation
  revoke: WorkspaceIdentityMutation; disconnect: WorkspaceIdentityMutation; overview: WorkspaceOverview; result: WorkspaceReadResult
}
export type WorkspaceOperation = keyof WorkspaceArguments
export type WorkspaceMutationOperation = 'issue' | 'revoke' | 'connect' | 'disconnect'
