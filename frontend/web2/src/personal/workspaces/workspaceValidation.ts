import type { WorkspaceConnection, WorkspaceContext, WorkspaceGrant, WorkspaceItemRef, WorkspaceOverview, WorkspaceOverviewEntry, WorkspaceReadResult, WorkspaceRecipient, WorkspaceSourceEnvelope, WorkspaceSourceIdentity, WorkspaceTicket } from '@naumi/shared/api/personalWorkspaces'
export const workspaceHash = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
export const workspaceId = (value: unknown, prefix: string): value is string => typeof value === 'string' && new RegExp(`^${prefix}_[a-f0-9]{32}$`).test(value)
const hex32 = (value: unknown) => typeof value === 'string' && /^[a-f0-9]{32}$/.test(value)
const integer = (value: unknown, min = 0): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= min
const actor = (value: unknown) => value === 'main' || workspaceId(value, 'pa')
const instant = (value: unknown) => typeof value === 'string' && Number.isFinite(Date.parse(value))
const plain = (value: unknown): value is Record<string, unknown> => Boolean(value && typeof value === 'object' && !Array.isArray(value))
function exact(value: unknown, fields: string[], label: string) { if (!plain(value) || Object.keys(value).length !== fields.length || Object.keys(value).some(key => !fields.includes(key))) throw new Error(`${label}字段不完整`) }
export const workspaceCanonical = (value: unknown): string => JSON.stringify(value, (_, field) => plain(field) ? Object.fromEntries(Object.entries(field).sort(([a], [b]) => a.localeCompare(b))) : field)
export function parseWorkspaceJson(raw: string): unknown {
  if (new TextEncoder().encode(raw).length > 256 * 1024) throw new Error('连接资料超过 256 KB')
  let value: unknown
  try { value = JSON.parse(raw) } catch { throw new Error('连接资料不是有效的 JSON') }
  const stack: { object: boolean; key: boolean; keys: Set<string> }[] = []
  for (const match of raw.matchAll(/"(?:\\.|[^"\\])*"|[{}\[\],:]/g)) {
    const token = match[0], top = stack.at(-1)
    if (token === '{' || token === '[') stack.push({ object: token === '{', key: token === '{', keys: new Set() })
    else if (token === '}' || token === ']') stack.pop()
    else if (token === ',') { if (top?.object) top.key = true }
    else if (token.startsWith('"') && top?.object && top.key) { const key = JSON.parse(token) as string; if (top.keys.has(key)) throw new Error('连接资料存在重复字段'); top.keys.add(key); top.key = false }
  }
  const finite = (field: unknown, depth = 0) => { if (depth > 30 || typeof field === 'number' && !Number.isFinite(field)) throw new Error('连接资料格式无效'); if (field && typeof field === 'object') Object.values(field).forEach(child => finite(child, depth + 1)) }
  finite(value); return value
}
export function validateRecipient(value: WorkspaceRecipient, workspace?: string): WorkspaceRecipient {
  exact(value, ['version', 'owner_key', 'workspace_key', 'session_db_key', 'directory_instance_id', 'actor_id', 'actor_version'], '接收卡')
  if (value.version !== 1 || !workspaceHash(value.owner_key) || !workspaceHash(value.workspace_key) || workspace && workspace !== value.workspace_key || !workspaceHash(value.session_db_key) || !hex32(value.directory_instance_id) || !actor(value.actor_id) || !integer(value.actor_version, 1)) throw new Error('接收卡身份待核对')
  return value
}
export function validateSource(value: WorkspaceSourceIdentity): WorkspaceSourceIdentity {
  exact(value, ['version', 'owner_key', 'workspace_key', 'session_db_key', 'directory_instance_id', 'item_instance_id'], '源身份')
  if (value.version !== 1 || !workspaceHash(value.owner_key) || !workspaceHash(value.workspace_key) || !workspaceHash(value.session_db_key) || !hex32(value.directory_instance_id) || !hex32(value.item_instance_id)) throw new Error('源工作区身份待核对')
  return value
}
function scopes(value: unknown) { if (!Array.isArray(value) || !value.includes('items') || value.some(field => !['items', 'results'].includes(field)) || new Set(value).size !== value.length) throw new Error('只读范围待核对') }
export function validateTicket(value: WorkspaceTicket): WorkspaceTicket {
  exact(value, ['version', 'grant_id', 'grant_revision', 'source', 'recipient', 'source_actor_id', 'source_actor_version', 'scopes', 'expires_at', 'secret'], '连接凭据')
  validateSource(value.source); validateRecipient(value.recipient); scopes(value.scopes)
  if (value.version !== 1 || !workspaceId(value.grant_id, 'wg') || value.grant_revision !== 1 || !actor(value.source_actor_id) || !integer(value.source_actor_version, 1) || !instant(value.expires_at) || !workspaceHash(value.secret)) throw new Error('连接凭据身份待核对')
  return value
}
export function validateWorkspaceEndpoint(value: string): string {
  try { const match = /^http:\/\/(127\.0\.0\.1|\[::1\]):([0-9]{1,5})\/?$/.exec(value.trim()); if (!match || Number(match[2]) < 1 || Number(match[2]) > 65535) throw new Error(); const url = new URL(value.trim()); if (url.protocol !== 'http:' || !['127.0.0.1', '[::1]'].includes(url.hostname) || url.username || url.password || url.pathname !== '/' || url.search || url.hash) throw new Error(); return `http://${match[1]}:${Number(match[2])}` }
  catch { throw new Error('请输入本机工作区地址，如 http://127.0.0.1:8765') }
}
export function validateWorkspaceContext(value: WorkspaceContext): WorkspaceContext {
  if (!value || !workspaceHash(value.workspace_key) || !workspaceHash(value.owner_key) || typeof value.configured !== 'boolean' || !Array.isArray(value.actors) || value.actors.some(row => !actor(row.id) || typeof row.name !== 'string' || !integer(row.version, 1)) || !integer(value.connection_count) || !integer(value.grant_count)) throw new Error('工作区接收身份待核对')
  return value
}
export function validateConnection(value: WorkspaceConnection, workspace: string): WorkspaceConnection {
  validateSource(value?.source); validateRecipient(value?.recipient, workspace); scopes(value.scopes)
  if (!workspaceId(value.id, 'wc') || !integer(value.revision, 1) || !['active', 'disconnected'].includes(value.status) || typeof value.label !== 'string' || !value.label || !instant(value.expires_at) || !actor(value.source_actor_id) || !integer(value.source_actor_version, 1)) throw new Error('连接身份待核对')
  validateWorkspaceEndpoint(value.endpoint)
  if (['ticket', 'secret', 'envelope', 'token_hash', 'tokenhash'].some(key => key in value)) throw new Error('连接记录混入了凭据正文')
  return value
}
export function validateGrant(value: WorkspaceGrant): WorkspaceGrant {
  validateSource(value?.source); validateRecipient(value?.recipient); scopes(value.scopes)
  if (!workspaceId(value.id, 'wg') || !integer(value.revision, 1) || !['active', 'revoked'].includes(value.status) || !actor(value.source_actor_id) || !integer(value.source_actor_version, 1) || !instant(value.expires_at) || !instant(value.created_at)) throw new Error('授权记录待核对')
  if (['ticket', 'secret', 'envelope', 'token_hash', 'tokenhash'].some(key => key in value)) throw new Error('授权记录混入了凭据正文')
  return value
}
export function validateWorkspaceEntry(value: WorkspaceOverviewEntry, connection: WorkspaceConnection): WorkspaceOverviewEntry {
  exact(value, ['item_id', 'item_revision', 'once_key', 'definition_sha256', 'title', 'state', 'assignee_id', 'assignee_name', 'schedule', 'execution', 'source_ref'], '原事项')
  exact(value.schedule, ['due_at', 'timezone'], '原执行时间')
  exact(value.source_ref, ['kind', 'workspace_key', 'item_id', 'item_revision', 'once_key', 'definition_sha256', 'assignee_id', 'assignee_version'], '原事项引用')
  if (value.execution) exact(value.execution, ['status', 'occurrence_id', 'job_id', 'run_id'], '原执行')
  const ref = value?.source_ref as WorkspaceItemRef
  if (!value || !workspaceId(value.item_id, 'pi') || !integer(value.item_revision, 1) || !hex32(value.once_key) || !workspaceHash(value.definition_sha256) || typeof value.title !== 'string' || !['active', 'paused', 'cancelled', 'closed'].includes(value.state) || !actor(value.assignee_id) || typeof value.assignee_name !== 'string' || !value.schedule || value.schedule.due_at !== null && !instant(value.schedule.due_at) || typeof value.schedule.timezone !== 'string'
    || !ref || ref.kind !== 'item' || ref.workspace_key !== connection.source.workspace_key || ref.item_id !== value.item_id || ref.item_revision !== value.item_revision || ref.once_key !== value.once_key || ref.definition_sha256 !== value.definition_sha256 || ref.assignee_id !== value.assignee_id || !integer(ref.assignee_version) || (ref.assignee_id === 'main') !== (ref.assignee_version === 0)) throw new Error('原事项与连接身份不一致')
  try { new Intl.DateTimeFormat('zh-CN', { timeZone: value.schedule.timezone }) } catch { throw new Error('原事项时区待核对') }
  if (value.execution && (!['prepared', 'queued', 'running', 'completed', 'error', 'timeout', 'max_turns', 'cancelled', 'unknown', 'blocked', 'skipped'].includes(value.execution.status) || !workspaceId(value.execution.occurrence_id, 'pio') || typeof value.execution.job_id !== 'string' || typeof value.execution.run_id !== 'string')) throw new Error('原执行身份待核对')
  return value
}
export function validateWorkspaceEnvelope(value: WorkspaceSourceEnvelope, connection: WorkspaceConnection) {
  if (!value || value.connection_id !== connection.id || value.connection_revision !== connection.revision || workspaceCanonical(value.source) !== workspaceCanonical(connection.source) || workspaceCanonical(value.recipient) !== workspaceCanonical(connection.recipient)) throw new Error('源工作区已变化，请重新核对连接')
}
export function validateWorkspaceOverview(value: WorkspaceOverview, connections: WorkspaceConnection[]): WorkspaceOverview {
  if (!value || !Array.isArray(value.sources) || typeof value.partial !== 'boolean' || value.consistency !== 'live_keyset' || new Set(value.sources.map(source => source.connection_id)).size !== value.sources.length) throw new Error('工作区汇总尚待核对')
  return { ...value, sources: value.sources.map(source => {
    const connection = connections.find(row => row.id === source.connection_id && row.status === 'active'); if (!connection) throw new Error('来源连接已失效')
    validateWorkspaceEnvelope(source, connection)
    if (!['available', 'unavailable'].includes(source.availability) || typeof source.partial !== 'boolean' || !Array.isArray(source.entries) || source.next_after !== null && typeof source.next_after !== 'string' || typeof source.label !== 'string' || source.code !== null && typeof source.code !== 'string') throw new Error('来源状态待核对')
    if (source.availability === 'unavailable') return { ...source, label: connection.label, entries: [], next_after: null, partial: true }
    const entries = source.entries.map(entry => validateWorkspaceEntry(entry, connection)); if (new Set(entries.map(entry => entry.item_id)).size !== entries.length) throw new Error('原事项身份重复')
    return { ...source, label: connection.label, entries }
  }) }
}
export function validateWorkspaceResult(value: WorkspaceReadResult, connection: WorkspaceConnection, itemId: string): WorkspaceReadResult {
  validateWorkspaceEnvelope(value, connection)
  if (!['available', 'unavailable'].includes(value.availability) || value.code !== null && typeof value.code !== 'string') throw new Error('原结果读取状态待核对')
  if (value.availability === 'unavailable') return { ...value, item: null, result: null }
  if (!value.item || !value.result || validateWorkspaceEntry(value.item, connection).item_id !== itemId) throw new Error('原结果与事项不一致')
  const result = value.result
  exact(result, ['occurrence_id', 'status', 'job_id', 'run_id', 'answer', 'artifacts', 'delivery_id', 'delivery_sha256', 'read_receipt'], '原交付')
  if (Array.isArray(result.artifacts)) result.artifacts.forEach(artifact => exact(artifact, ['artifact_id', 'version', 'sha256', 'title', 'content_text'], '原材料'))
  if (!workspaceId(result.occurrence_id, 'pio') || typeof result.status !== 'string' || typeof result.job_id !== 'string' || typeof result.run_id !== 'string' || typeof result.answer !== 'string' || !Array.isArray(result.artifacts) || !result.delivery_id || !workspaceHash(result.delivery_sha256) || result.read_receipt !== null
    || !value.item.execution || value.item.execution.occurrence_id !== result.occurrence_id || value.item.execution.job_id !== result.job_id || value.item.execution.run_id !== result.run_id
    || result.artifacts.some(artifact => !workspaceId(artifact.artifact_id, 'art') || !integer(artifact.version, 1) || !workspaceHash(artifact.sha256) || typeof artifact.title !== 'string' || typeof artifact.content_text !== 'string')) throw new Error('原交付回执或材料待核对')
  return value
}
