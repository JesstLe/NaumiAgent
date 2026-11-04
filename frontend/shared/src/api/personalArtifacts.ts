export type PersonalArtifactKind = 'material' | 'output'

export interface PersonalArtifactRef {
  artifact_id: string
  version: number
  sha256: string
  grant_id: string
  grant_revision: number
}

export interface PersonalArtifactSnapshot {
  artifact_id: string
  version: number
  owner_id: string
  title: string
  kind: PersonalArtifactKind
  sha256: string
  bytes: number
  mime_type: string
  suffix: string
  created_at: string
  source_refs: PersonalArtifactRef[]
  evidence: Record<string, unknown>
  grant_id: string
  grant_revision: number
  can_revise: boolean
  can_share: boolean
}

export interface PersonalArtifactInput {
  actor_id: string
  request_id: string
  title: string
  kind: PersonalArtifactKind
  filename: string
  content: string
  source_refs?: PersonalArtifactRef[]
  evidence?: Record<string, unknown>
  session_id?: string
}

export interface PersonalArtifactRevision extends PersonalArtifactInput {
  expected_version: number
}

export interface PersonalArtifactGrant {
  id: string
  artifact_id: string
  version: number
  owner_id: string
  recipient_id: string
  revision: number
  status: 'active' | 'revoked'
  created_at: string
  revoked_at: string
  recipient_scope: Record<string, unknown>
}

export interface PersonalArtifactPage {
  version: 1
  type: 'page'
  title: string
  sections: {
    id: string
    title: string
    markdown?: string
    widget?: Record<string, unknown>
    evidence_status: 'fact' | 'estimate' | 'unknown'
    source_refs?: PersonalArtifactRef[]
  }[]
}

export type PersonalArtifactCell = string | number | boolean | null
export type PersonalArtifactPreview = PersonalArtifactPage
  | { type: 'text'; content: string; truncated: boolean }
  | { type: 'table'; columns: { key: string; label: string }[]; rows: Record<string, PersonalArtifactCell>[]; total_rows: number; preview_limit: number }

export interface PersonalArtifactContent {
  blob: Blob
  sha256: string
}

export interface PersonalArtifactQuery {
  actor_id: string
  kind?: PersonalArtifactKind
  q?: string
  limit?: number
  offset?: number
}
