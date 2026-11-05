import type { FollowupContext, FollowupEvent, FollowupPage, FollowupProposal, FollowupProposalRef, FollowupRule, FollowupRuleBody, FollowupSource, FollowupTarget } from '@naumi/shared/api/personalFollowups'
import { validateTodayRef } from '../today/usePersonalToday'
export const followupId = (value: unknown, prefix: string): value is string => typeof value === 'string' && new RegExp(`^${prefix}_[a-f0-9]{32}$`).test(value)
export const followupHash = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
export const followupInteger = (value: unknown, minimum = 0): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= minimum
const instant = (value: unknown): value is string => typeof value === 'string' && Number.isFinite(Date.parse(value))
const actor = (id: unknown, version: unknown) => (id === 'main' && version === 0) || followupId(id, 'pa') && followupInteger(version, 1)
export const followupCanonical = (value: unknown): string => JSON.stringify(value, (_, field) => field && typeof field === 'object' && !Array.isArray(field) ? Object.fromEntries(Object.entries(field).sort(([a], [b]) => a.localeCompare(b))) : field)
function exact(value: unknown, fields: string[], name: string) { if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).length !== fields.length || Object.keys(value).some(key => !fields.includes(key))) throw new Error(`${name}字段待核对`) }
export function validateFollowupContext(value: FollowupContext) {
  if (!value || !followupHash(value.workspace_key) || !followupHash(value.owner_key) || typeof value.configured !== 'boolean' || !Array.isArray(value.actors) || value.actors.some(row => !actor(row.id, row.version) || typeof row.name !== 'string') || new Set(value.actors.map(row => row.id)).size !== value.actors.length || !Array.isArray(value.connections) || value.connections.some(row => !followupId(row.id, 'pc') || typeof row.email !== 'string' || typeof row.can_write !== 'boolean' || typeof row.status !== 'string' || row.last_sync_at !== null && !instant(row.last_sync_at)) || !value.worker || typeof value.worker.partial !== 'boolean' || !instant(value.server_now)) throw new Error('跟进工作区身份待核对')
  return value
}
export function validateFollowupRuleBody(body: FollowupRuleBody, archived = false) {
  if (!body || typeof body.name !== 'string' || !body.name.trim() || body.name.length > 80 || !actor(body.actor_id, body.actor_version) || !(archived ? ['active', 'paused', 'archived'] : ['active', 'paused']).includes(body.status) || typeof body.include_existing !== 'boolean' || !followupInteger(body.max_lateness_minutes, 1) || body.max_lateness_minutes > 10080 || !followupInteger(body.freshness_seconds, 60) || body.freshness_seconds > 604800 || !['latest', 'skip'].includes(body.misfire_policy)) throw new Error('跟进规则字段待核对')
  try { if (typeof body.timezone !== 'string' || !body.timezone) throw new Error(); new Intl.DateTimeFormat('zh-CN', { timeZone: body.timezone }) } catch { throw new Error('请选择有效的规则时区') }
  const trigger = body.trigger
  if (trigger?.kind === 'deadline') {
    if (!Array.isArray(trigger.item_ids) || trigger.item_ids.length > 100 || new Set(trigger.item_ids).size !== trigger.item_ids.length || trigger.item_ids.some(id => !followupId(id, 'pi')) || !Array.isArray(trigger.offsets_minutes) || !trigger.offsets_minutes.length || trigger.offsets_minutes.length > 8 || new Set(trigger.offsets_minutes).size !== trigger.offsets_minutes.length || trigger.offsets_minutes.some(value => !Number.isSafeInteger(value) || Math.abs(value) > 10080)) throw new Error('请选择事项范围和有效跟进时点')
  } else if (trigger?.kind === 'calendar_change') {
    if (!followupId(trigger.connection_id, 'pc') || trigger.event_id !== null && (typeof trigger.event_id !== 'string' || !trigger.event_id || trigger.event_id.length > 1024) || !Array.isArray(trigger.change_kinds) || !trigger.change_kinds.length || new Set(trigger.change_kinds).size !== trigger.change_kinds.length || trigger.change_kinds.some(kind => !['created', 'updated', 'cancelled', 'unavailable', 'restored'].includes(kind)) || !Array.isArray(trigger.fields) || !trigger.fields.length || new Set(trigger.fields).size !== trigger.fields.length || trigger.fields.some(field => !['summary', 'description', 'start', 'end', 'status', 'available'].includes(field))) throw new Error('请选择真实日历来源和变更字段')
  } else throw new Error('请选择跟进来源')
  return body
}
export function validateFollowupRule(value: FollowupRule, workspace: string) {
  if (!value || !followupId(value.id, 'fr') || value.workspace_key !== workspace || !followupInteger(value.revision, 1) || !followupHash(value.digest) || typeof value.epoch !== 'string' || !/^[a-f0-9]{32}$/.test(value.epoch) || !instant(value.cutover_at) || !instant(value.created_at) || !instant(value.updated_at)) throw new Error('规则原版本待核对')
  validateFollowupRuleBody(value.body as FollowupRuleBody, true); return value
}
export function validateFollowupRef(value: FollowupProposalRef, workspace: string) {
  exact(value, ['proposal_id', 'revision', 'digest', 'workspace_key'], '建议引用')
  if (!followupId(value.proposal_id, 'fp') || value.revision !== 1 || !followupHash(value.digest) || value.workspace_key !== workspace) throw new Error('建议原身份待核对')
  return value
}
function validateSource(source: FollowupSource, workspace: string) {
  if (source.kind === 'deadline') {
    validateTodayRef(source.item_ref, workspace); const ref = source.metadata_ref
    if (!ref || !followupId(ref.metadata_id, 'tm') || !followupInteger(ref.metadata_revision) || !followupHash(ref.metadata_digest) || ref.workspace_key !== workspace || followupCanonical(ref.item_ref) !== followupCanonical(source.item_ref) || !followupInteger(source.current_metadata_revision) || source.current_metadata_revision < ref.metadata_revision || !source.deadline || !['date', 'instant'].includes(source.deadline.kind) || !followupHash(source.deadline.timezone_sha256)) throw new Error('原截止证据待核对')
    if (typeof source.deadline.timezone !== 'string' || !source.deadline.timezone || !instant(source.deadline.boundary_at || source.deadline.resolved_at) || source.deadline.kind === 'date' && !/^\d{4}-\d{2}-\d{2}$/.test(source.deadline.local_date) || source.deadline.kind === 'instant' && !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?$/.test(source.deadline.local_time)) throw new Error('原截止时间待核对')
    new Intl.DateTimeFormat('zh-CN', { timeZone: source.deadline.timezone })
  } else if (source.kind === 'calendar_change') {
    const ref = source.change_ref
    if (!ref || !followupId(ref.connection_id, 'pc') || typeof ref.connection_instance !== 'string' || !/^[a-f0-9]{32}$/.test(ref.connection_instance) || !followupInteger(ref.auth_epoch) || !followupInteger(ref.sequence, 1) || ref.change_id !== `cch_${ref.connection_id.slice(3)}_${String(ref.sequence).padStart(20, '0')}` || !followupHash(ref.change_digest) || typeof ref.grant_id !== 'string' || ref.grant_id.length > 64 || !followupInteger(ref.grant_revision) || Boolean(ref.grant_id) !== Boolean(ref.grant_revision) || typeof source.change_kind !== 'string' || !Array.isArray(source.changed_fields) || source.changed_fields.some(field => !['summary', 'description', 'start', 'end', 'status', 'available'].includes(field)) || source.last_sync_at !== null && !instant(source.last_sync_at)) throw new Error('原事件变更证据待核对')
    for (const event of [source.before, source.after]) if (event) {
      if (typeof event !== 'object' || Array.isArray(event) || event.summary !== undefined && typeof event.summary !== 'string' || event.description !== undefined && typeof event.description !== 'string') throw new Error('原事件正文待核对')
      for (const date of [event.start, event.end]) if (date) {
        if (typeof date !== 'object' || Array.isArray(date) || date.date !== undefined && !/^\d{4}-\d{2}-\d{2}$/.test(date.date) || date.dateTime !== undefined && !instant(date.dateTime) || !date.date && !date.dateTime) throw new Error('原日程时间待核对')
        if (date.timeZone !== undefined) { if (typeof date.timeZone !== 'string' || !date.timeZone) throw new Error('原日程时区待核对'); new Intl.DateTimeFormat('zh-CN', { timeZone: date.timeZone }) }
      }
    }
  } else throw new Error('建议来源待核对')
}
export function validateFollowupTarget(value: FollowupTarget, workspace: string) {
  if (value.kind === 'today_place') {
    if (!followupId(value.day_id, 'td') || !followupInteger(value.day_revision, 1) || !followupId(value.metadata_id, 'tm') || !followupId(value.placement_id, 'tp')) throw new Error('原安排回执待核对')
    validateTodayRef(value.item_ref, workspace)
  } else if (value.kind === 'calendar_draft') {
    if (!followupId(value.action_id, 'ca') || !followupInteger(value.action_revision, 1) || !followupHash(value.digest) || !['draft', 'approved', 'dispatching', 'unknown', 'submitted', 'verified', 'conflict', 'rejected', 'expired', 'cancelled'].includes(value.status)) throw new Error('原日程动作回执待核对')
  } else throw new Error('原采纳目标待核对')
  return value
}
export function validateFollowupProposal(value: FollowupProposal, workspace: string): FollowupProposal {
  exact(value, ['id', 'workspace_key', 'actor_id', 'actor_version', 'proposal_ref', 'decision', 'availability', 'title', 'reason', 'rule', 'source', 'effect_kind', 'effect_status', 'target', 'expires_at', 'created_at'], '跟进建议')
  validateFollowupRef(value.proposal_ref, workspace)
  if (value.id !== value.proposal_ref.proposal_id || value.workspace_key !== workspace || !actor(value.actor_id, value.actor_version) || !['available', 'source_stale', 'source_unavailable', 'resolved'].includes(value.availability) || !['today_place', 'calendar_draft'].includes(value.effect_kind) || !['none', 'pending', 'unknown', 'blocked', 'materialized'].includes(value.effect_status) || !instant(value.expires_at) || !instant(value.created_at) || !value.decision || !followupInteger(value.decision.revision) || !followupInteger(value.decision.presentation_epoch) || !['pending', 'snoozed', 'accepted', 'dismissed'].includes(value.decision.status) || value.decision.snooze_until !== null && !instant(value.decision.snooze_until) || value.decision.effect_id !== null && !followupId(value.decision.effect_id, 'fe')) throw new Error('建议处置状态待核对')
  if (['source_stale', 'source_unavailable'].includes(value.availability)) return { ...value, title: null, reason: null, rule: null, source: null, target: null }
  if (value.title !== null && typeof value.title !== 'string') throw new Error('建议正文待核对')
  if (value.rule && (!followupId(value.rule.id, 'fr') || !followupInteger(value.rule.revision, 1) || !followupHash(value.rule.digest) || typeof value.rule.name !== 'string')) throw new Error('建议规则待核对')
  if (value.reason && (!['deadline_approaching', 'deadline_due', 'deadline_overdue', 'calendar_existing', 'calendar_changed', 'calendar_cancelled', 'calendar_unavailable', 'calendar_restored'].includes(value.reason.code) || !instant(value.reason.nominal_at) || !instant(value.reason.observed_at))) throw new Error('建议原因待核对')
  if (value.source) validateSource(value.source, workspace)
  if (value.target) validateFollowupTarget(value.target, workspace)
  if (value.availability === 'available' && value.decision.status === 'pending' && (!value.source || !value.reason || !value.rule || !value.title)) throw new Error('建议缺少原来源证据')
  if (value.source?.kind === 'calendar_change' && (['cancelled', 'unavailable'].includes(value.source.change_kind) || value.source.after?.status === 'cancelled' || value.source.after?.available === false)) {
    const sanitize = (event: FollowupEvent | null) => event ? { ...(event.status !== undefined ? { status: event.status } : {}), ...(event.available !== undefined ? { available: event.available } : {}) } : null
    return { ...value, title: '核对日程变更', source: { ...value.source, before: sanitize(value.source.before), after: sanitize(value.source.after) } }
  }
  return value
}
export function validateFollowupPage(value: FollowupPage, workspace: string) {
  if (!value || !Array.isArray(value.proposals) || value.next_after !== null && !followupId(value.next_after, 'fc') || typeof value.partial !== 'boolean' || !instant(value.server_now)) throw new Error('跟进分页待核对')
  const proposals = value.proposals.map(row => validateFollowupProposal(row, workspace))
  if (new Set(proposals.map(row => row.id)).size !== proposals.length) throw new Error('建议分页包含重复身份')
  return { ...value, proposals }
}
