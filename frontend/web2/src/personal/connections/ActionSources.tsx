import { ArrowUpRight, BrainCircuit, FileText } from 'lucide-react'
import type { CalendarAction } from '@naumi/shared/api/personalConnections'
import type { PersonalArtifactRef } from '@naumi/shared/api/personalArtifacts'
import type { PersonalMemoryRef } from '@naumi/shared/api/personalMemories'
import { requestArtifactNavigation } from '../library/navigation'

function locationBase() {
  const url = new URL(location.href)
  for (const key of ['connections', 'items', 'library', 'artifact', 'version', 'actor', 'memories', 'memory_actor', 'memory_scope', 'memory_item', 'memory_id']) url.searchParams.delete(key)
  return url
}
export function actionArtifactLocation(actorId: string, ref: PersonalArtifactRef) {
  const url = locationBase()
  url.searchParams.set('library', '1'); url.searchParams.set('actor', actorId)
  url.searchParams.set('artifact', ref.artifact_id); url.searchParams.set('version', String(ref.version))
  return url.pathname + url.search
}
export function actionMemoryLocation(actorId: string, ref: PersonalMemoryRef, itemId: string | null) {
  const url = locationBase()
  url.searchParams.set('memories', '1'); url.searchParams.set('memory_actor', actorId)
  url.searchParams.set('memory_scope', itemId ? 'item' : 'all'); url.searchParams.set('memory_id', ref.memory_id)
  if (itemId) url.searchParams.set('memory_item', itemId)
  return url.pathname + url.search
}

export function ActionSources({ action, onOpenArtifact, onOpenMemory }: {
  action: CalendarAction
  onOpenArtifact?: (actorId: string, ref: PersonalArtifactRef) => void
  onOpenMemory?: (actorId: string, ref: PersonalMemoryRef, itemId: string | null) => void
}) {
  const artifacts = action.artifact_refs || [], memories = action.memory_refs || []
  if (!artifacts.length && !memories.length) return null
  return <section className="pc-source-refs" aria-label="动作原始来源"><header><h3>来源</h3><span>{artifacts.length + memories.length}</span></header><div>{artifacts.map(ref => <a key={`${ref.artifact_id}:${ref.version}:${ref.grant_id}`} href={actionArtifactLocation(action.actor_id, ref)} aria-label={`查看来源资料 ${ref.artifact_id} 版本 ${ref.version}`} title={`原引用 SHA256 ${ref.sha256}${ref.grant_id ? ` · ${ref.grant_id} v${ref.grant_revision}` : ''}`} onClick={event => {
    if (onOpenArtifact && !event.metaKey && !event.ctrlKey && !event.shiftKey && event.button === 0) { event.preventDefault(); requestArtifactNavigation(() => onOpenArtifact(action.actor_id, ref)) }
  }}><span><FileText size={14} />资料<b>v{ref.version}</b><ArrowUpRight size={13} /></span><code>{ref.artifact_id}</code></a>)}{memories.map(ref => <a key={`${ref.memory_id}:${ref.revision}:${ref.grant_id}`} href={actionMemoryLocation(action.actor_id, ref, action.memory_item_id)} aria-label={`查看记忆记录 ${ref.memory_id} 原引用修订 ${ref.revision}`} title={`打开记忆记录；本动作仍引用修订 ${ref.revision} · SHA256 ${ref.content_sha256}${ref.grant_id ? ` · ${ref.grant_id} v${ref.grant_revision}` : ''}`} onClick={event => {
    if (onOpenMemory && !event.metaKey && !event.ctrlKey && !event.shiftKey && event.button === 0) { event.preventDefault(); requestArtifactNavigation(() => onOpenMemory(action.actor_id, ref, action.memory_item_id)) }
  }}><span><BrainCircuit size={14} />记忆<b>引用修订 {ref.revision}</b><ArrowUpRight size={13} /></span><code>{ref.memory_id}</code></a>)}</div></section>
}
