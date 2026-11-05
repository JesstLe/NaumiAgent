import { useCallback, useEffect, useRef, useState } from 'react'
import { WorkbenchRequestError, type WorkbenchRuntimeClient } from '@naumi/shared/api/WorkbenchRuntimeClient'
import type { FollowupArguments, FollowupContext, FollowupFilter, FollowupIdentity, FollowupMutation, FollowupMutationOperation, FollowupPage, FollowupProposal, FollowupRule } from '@naumi/shared/api/personalFollowups'
import type { CalendarAction } from '@naumi/shared/api/personalConnections'
import { followupCanonical, followupHash, followupId, followupInteger, validateFollowupContext, validateFollowupPage, validateFollowupProposal, validateFollowupRule } from './followupValidation'
import { validateTodayView } from '../today/usePersonalToday'
import { validateAction } from '../connections/usePersonalConnections'

export type FollowupApi = Pick<WorkbenchRuntimeClient, 'getPersonalFollowupContext' | 'executePersonalFollowupOperation' | 'getPersonalToday' | 'getPersonalTodayMetadata' | 'getPersonalTodayDay' | 'personalCalendarAction' | 'getPersonalTodaySources' | 'personalCalendarEvents'>
export const followupError = (cause: unknown) => cause && typeof cause === 'object' && 'name' in cause && cause.name === 'TimeoutError' ? '本次核对等待较久，请稍后核对原请求' : cause && typeof cause === 'object' && 'name' in cause && cause.name === 'AbortError' ? '已停止等待，请核对原请求' : cause instanceof Error ? cause.message : '跟进操作未完成，请核对原记录'
export const followupActionStatusNames: Record<CalendarAction['status'], string> = { draft: '待审批', approved: '已批准', dispatching: '正在执行', unknown: '结果待核对', submitted: '已提交，待回读', verified: '已回读验证', conflict: '日程已变化', rejected: '已拒绝', expired: '已过期', cancelled: '已取消' }
export const followupRequestId = () => crypto.randomUUID().replaceAll('-', '')
export const followupPendingKey = (namespace: string) => `naumi:followup-request:v1:${encodeURIComponent(namespace)}`
export interface FollowupPending { version: 1; request_id: string; operation: FollowupMutationOperation; workspace_key: string; rule_id?: string; expected_revision?: number; proposal_id?: string; proposal_digest?: string; expected_decision_revision?: number; effect_id?: string }
export function parseFollowupPending(raw: string): FollowupPending {
  if (raw.length > 4096) throw new Error('原跟进请求身份待核对')
  const value = JSON.parse(raw) as FollowupPending
  if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).some(key => !['version', 'request_id', 'operation', 'workspace_key', 'rule_id', 'expected_revision', 'proposal_id', 'proposal_digest', 'expected_decision_revision', 'effect_id'].includes(key)) || value.version !== 1 || typeof value.request_id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$/.test(value.request_id) || !followupHash(value.workspace_key) || !['create_rule', 'update_rule', 'set_status', 'dismiss', 'snooze', 'accept', 'reconcile'].includes(value.operation)) throw new Error('原跟进请求身份待核对')
  const fields = Object.keys(value).sort().join('|'), base = ['version', 'request_id', 'operation', 'workspace_key']
  if (['update_rule', 'set_status'].includes(value.operation)) { if (!followupId(value.rule_id, 'fr') || !followupInteger(value.expected_revision, 1) || fields !== [...base, 'rule_id', 'expected_revision'].sort().join('|')) throw new Error('原规则请求身份待核对') }
  else if (['accept', 'dismiss', 'snooze'].includes(value.operation)) { if (!followupId(value.proposal_id, 'fp') || !followupHash(value.proposal_digest) || !followupInteger(value.expected_decision_revision) || fields !== [...base, 'proposal_id', 'proposal_digest', 'expected_decision_revision'].sort().join('|')) throw new Error('原建议请求身份待核对') }
  else if (value.operation === 'reconcile') { if (!followupId(value.effect_id, 'fe') || fields !== [...base, 'effect_id'].sort().join('|')) throw new Error('原采纳请求身份待核对') }
  else if (fields !== base.sort().join('|')) throw new Error('原规则创建身份待核对')
  return value
}
export function useFollowups({ api, namespace, connected, initialProposalId }: { api: FollowupApi; namespace: string; connected: boolean; initialProposalId?: string | null }) {
  const key = followupPendingKey(namespace), active = useRef(key); active.current = key
  const [scope, setScope] = useState(key), [context, setContext] = useState<FollowupContext | null>(null), [rules, setRules] = useState<FollowupRule[]>([]), [page, setPage] = useState<FollowupPage | null>(null)
  const [selectedId, setSelectedId] = useState(initialProposalId || ''), [selected, setSelected] = useState<FollowupProposal | null>(null), [filter, setFilter] = useState<FollowupFilter>('pending'), [actorId, setActorId] = useState('')
  const [selectionEpoch, setSelectionEpoch] = useState(0)
  const [pending, setPending] = useState<FollowupPending | null>(null), [identityError, setIdentityError] = useState(''), [error, setError] = useState(''), [notice, setNotice] = useState('')
  const [loading, setLoading] = useState(false), [detailLoading, setDetailLoading] = useState(false), [posting, setPosting] = useState(false), [readbackLoading, setReadbackLoading] = useState(false)
  const [readback, setReadback] = useState<{ proposal_id: string; target: NonNullable<FollowupProposal['target']>; title: string; verified: boolean } | null>(null), [token, setToken] = useState(0)
  const generation = useRef(0), detailSequence = useRef(0), readSequence = useRef(0), writeLock = useRef(false), flight = useRef(false), moreLock = useRef(false), pendingRef = useRef(pending)
  const writeController = useRef<AbortController | null>(null), readController = useRef<AbortController | null>(null)
  pendingRef.current = pending
  const live = useRef({ connected, selectedId, context }); live.current = { connected, selectedId, context }
  const refresh = useCallback(() => setToken(value => value + 1), [])
  useEffect(() => {
    setScope(key); setContext(null); setRules([]); setPage(null); setSelected(null); setSelectedId(initialProposalId || ''); setReadback(null); setError(''); setNotice(''); setPosting(false); writeLock.current = false
    const restore = () => { try { const raw = localStorage.getItem(key); const saved = raw ? parseFollowupPending(raw) : null; pendingRef.current = saved; setPending(saved); setIdentityError('') } catch { setIdentityError('原跟进请求身份存储待核对') } }
    restore(); window.addEventListener('storage', restore)
    return () => { window.removeEventListener('storage', restore); writeController.current?.abort(); readController.current?.abort(); generation.current++; detailSequence.current++; readSequence.current++ }
  }, [key])
  useEffect(() => { if (!connected) { writeController.current?.abort(); readController.current?.abort() } }, [connected])
  useEffect(() => { if (initialProposalId) { setSelectedId(initialProposalId); setSelected(null); setReadback(null); detailSequence.current++ } }, [initialProposalId])
  useEffect(() => {
    const controller = new AbortController(), run = ++generation.current
    let timer: ReturnType<typeof setTimeout> | undefined
    const current = () => !controller.signal.aborted && active.current === key && generation.current === run && live.current.connected
    const load = async () => {
      if (!connected) { setContext(null); setPage(null); setRules([]); setSelected(null); setReadback(null); setLoading(false); return }
      if (!current()) return
      if (flight.current || moreLock.current) { timer = setTimeout(() => void load(), 150); return }
      flight.current = true; setLoading(true)
      try {
        const ctx = validateFollowupContext(await api.getPersonalFollowupContext(controller.signal))
        const [listed, proposed] = await Promise.all([api.executePersonalFollowupOperation('rules', {}, controller.signal), api.executePersonalFollowupOperation('list', { status: filter, ...(actorId ? { actor_id: actorId } : {}), limit: 50 }, controller.signal)])
        const nextRules = listed.rules.map(row => validateFollowupRule(row, ctx.workspace_key)), nextPage = validateFollowupPage(proposed, ctx.workspace_key)
        if (new Set(nextRules.map(row => row.id)).size !== nextRules.length) throw new Error('规则记录身份重复')
        if (!current()) return
        setContext(ctx); setRules(nextRules); setPage(nextPage); setError('')
        const id = live.current.selectedId
        if (id) {
          const captured = detailSequence.current
          const value = validateFollowupProposal(await api.executePersonalFollowupOperation('get', { proposal_id: id }, controller.signal), ctx.workspace_key)
          if (value.id !== id) throw new Error('跟进建议身份不一致')
          if (current() && captured === detailSequence.current) { setSelected(value); setError(''); if (['source_stale', 'source_unavailable'].includes(value.availability)) { readSequence.current++; setReadback(null) } }
        }
      } catch (cause) { if (current()) { detailSequence.current++; readSequence.current++; setContext(null); setRules([]); setPage(null); setSelected(null); setReadback(null); setError(followupError(cause)) } }
      finally { flight.current = false; if (current()) { setLoading(false); timer = setTimeout(() => void load(), 15000) } }
    }
    void load(); return () => { controller.abort(); clearTimeout(timer) }
  }, [api, key, connected, filter, actorId, token])
  useEffect(() => {
    const controller = new AbortController(), seq = ++detailSequence.current
    setSelected(null); setReadback(null); readSequence.current++
    if (!selectedId || !context || !connected) { setDetailLoading(false); return () => controller.abort() }
    setDetailLoading(true)
    void api.executePersonalFollowupOperation('get', { proposal_id: selectedId }, controller.signal).then(value => {
      if (controller.signal.aborted || active.current !== key || seq !== detailSequence.current || !live.current.connected) return
      const checked = validateFollowupProposal(value, context.workspace_key); if (checked.id !== selectedId) throw new Error('跟进建议身份不一致'); setSelected(checked); setError('')
    }).catch(cause => { if (!controller.signal.aborted && active.current === key && seq === detailSequence.current) setError(followupError(cause)) }).finally(() => { if (!controller.signal.aborted && active.current === key && seq === detailSequence.current) setDetailLoading(false) })
    return () => controller.abort()
  }, [api, key, selectedId, selectionEpoch, context?.workspace_key, connected])
  const select = (id: string) => { readController.current?.abort(); detailSequence.current++; readSequence.current++; setSelected(null); setReadback(null); setSelectedId(id); setSelectionEpoch(value => value + 1); setError(''); setNotice('') }
  const clearSelection = () => select('')
  const more = async () => {
    if (!page?.next_after || !context || !connected || flight.current || moreLock.current) return
    const run = generation.current; moreLock.current = true; setLoading(true)
    try {
      const next = validateFollowupPage(await api.executePersonalFollowupOperation('list', { status: filter, ...(actorId ? { actor_id: actorId } : {}), after: page.next_after, limit: 50 }), context.workspace_key)
      if (active.current !== key || run !== generation.current || !live.current.connected) return
      setPage(previous => previous && ({ ...next, partial: previous.partial || next.partial, proposals: [...previous.proposals.filter(old => !next.proposals.some(row => row.id === old.id)), ...next.proposals] }))
    } catch (cause) { if (active.current === key && run === generation.current) { setPage(null); setSelected(null); setReadback(null); setError(followupError(cause)) } }
    finally { moreLock.current = false; if (active.current === key && run === generation.current) setLoading(false) }
  }
  const checkIdentity = (identity: FollowupIdentity | undefined, saved: FollowupPending) => {
    if (!identity || Object.keys(identity).some(field => !['rule_id', 'rule_revision', 'proposal_id', 'decision_revision', 'effect_id'].includes(field))) throw new Error('原跟进受理回执待核对')
    if (['create_rule', 'update_rule', 'set_status'].includes(saved.operation)) { if (!followupId(identity.rule_id, 'fr') || !followupInteger(identity.rule_revision, 1) || saved.rule_id && identity.rule_id !== saved.rule_id || saved.expected_revision !== undefined && identity.rule_revision !== saved.expected_revision + 1 || saved.operation === 'create_rule' && identity.rule_revision !== 1) throw new Error('原规则回执身份不一致') }
    else if (saved.operation === 'reconcile') { if (identity.effect_id !== saved.effect_id || identity.proposal_id !== undefined && !followupId(identity.proposal_id, 'fp')) throw new Error('原采纳恢复回执不一致') }
    else if (identity.proposal_id !== saved.proposal_id || !followupInteger(identity.decision_revision, 1) || identity.decision_revision !== saved.expected_decision_revision! + 1 || saved.operation === 'accept' && !followupId(identity.effect_id, 'fe')) throw new Error('原建议处置回执不一致')
    if (identity.effect_id !== undefined && !followupId(identity.effect_id, 'fe')) throw new Error('原采纳身份待核对')
  }
  const acceptReceipt = (value: FollowupMutation, saved: FollowupPending) => {
    if (active.current !== key || !live.current.connected) return false
    if (!value || value.accepted !== true || value.request_id !== saved.request_id || value.operation !== saved.operation || typeof value.projection_available !== 'boolean') throw new Error('原跟进请求回执不一致')
    checkIdentity(value.identity, saved)
    if (value.view) {
      if ('proposal_ref' in value.view) { validateFollowupProposal(value.view, saved.workspace_key); if (value.view.id !== value.identity.proposal_id) throw new Error('原建议回执投影不一致') }
      else { validateFollowupRule(value.view, saved.workspace_key); if (value.view.id !== value.identity.rule_id || value.view.revision !== value.identity.rule_revision) throw new Error('原规则回执投影不一致') }
    }
    const stored = localStorage.getItem(key); if (!stored || parseFollowupPending(stored).request_id !== saved.request_id) throw new Error('原跟进请求已变化，请核对')
    localStorage.removeItem(key); pendingRef.current = null; setPending(null); setNotice(value.projection_available ? saved.operation === 'accept' ? '采纳已受理，正在核对原目标' : '已保存' : '操作已受理，原来源或目标暂待核对'); setReadback(null); setPage(null); refresh(); return true
  }
  const submit = async <K extends FollowupMutationOperation>(operation: K, args: FollowupArguments[K]): Promise<FollowupMutation | null> => {
    if (writeLock.current || pendingRef.current || identityError || !connected || !context || scope !== key) return null
    writeLock.current = true; setPosting(true); setError(''); setNotice('')
    const body = args.payload
    const saved: FollowupPending = { version: 1, request_id: body.request_id, operation, workspace_key: context.workspace_key, ...('rule_id' in body ? { rule_id: body.rule_id, expected_revision: body.expected_revision } : {}), ...('proposal_ref' in body ? { proposal_id: body.proposal_ref.proposal_id, proposal_digest: body.proposal_ref.digest, expected_decision_revision: body.expected_decision_revision } : {}), ...('effect_id' in body ? { effect_id: body.effect_id } : {}) }
    const controller = new AbortController(); writeController.current = controller
    let sent = false
    try {
      parseFollowupPending(JSON.stringify(saved)); if (localStorage.getItem(key)) throw new Error('请先核对原跟进请求')
      localStorage.setItem(key, JSON.stringify(saved)); pendingRef.current = saved; setPending(saved); sent = true
      const receipt = await api.executePersonalFollowupOperation(operation, args, AbortSignal.any([controller.signal, AbortSignal.timeout(180000)]))
      if (controller.signal.aborted) return null
      return acceptReceipt(receipt, saved) ? receipt : null
    } catch (cause) {
      if (active.current !== key || writeController.current !== controller) return null
      if (sent && cause instanceof WorkbenchRequestError && cause.status >= 400 && cause.status < 500 && ![408, 425, 429].includes(cause.status)) { localStorage.removeItem(key); pendingRef.current = null; setPending(null); if ([401, 403, 404, 409].includes(cause.status)) { setSelected(null); setReadback(null); refresh() } }
      setError(followupError(cause)); return null
    } finally { if (writeController.current === controller) { writeController.current = null; if (active.current === key) { writeLock.current = false; setPosting(false) } } }
  }
  const reconcileRequest = async () => {
    const saved = pendingRef.current
    if (!saved || !context || saved.workspace_key !== context.workspace_key || writeLock.current || !connected) return
    writeLock.current = true; setPosting(true); setError('')
    const controller = new AbortController(); writeController.current = controller
    try {
      const value = await api.executePersonalFollowupOperation('requests', { request_id: saved.request_id }, AbortSignal.any([controller.signal, AbortSignal.timeout(180000)]))
      if (controller.signal.aborted || active.current !== key || !live.current.connected) return
      if (value.request_id !== saved.request_id || value.status !== 'accepted' || value.operation !== saved.operation || !value.identity) throw new Error('原请求尚待核对，未重新提交')
      acceptReceipt({ ...value, accepted: true, operation: value.operation, identity: value.identity, projection_available: value.projection_available === true }, saved)
    } catch (cause) { if (active.current === key && writeController.current === controller) setError(followupError(cause)) }
    finally { if (writeController.current === controller) { writeController.current = null; if (active.current === key) { writeLock.current = false; setPosting(false) } } }
  }
  const verifyTarget = async (proposalId: string) => {
    readController.current?.abort(); const controller = new AbortController(); readController.current = controller
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(180000)]), seq = ++readSequence.current
    setReadback(null); setReadbackLoading(true); setError('')
    try {
      if (!context || !connected) throw new Error('当前工作区已断开')
      const proposal = validateFollowupProposal(await api.executePersonalFollowupOperation('get', { proposal_id: proposalId }, signal), context.workspace_key)
      if (proposal.id !== proposalId || ['source_stale', 'source_unavailable'].includes(proposal.availability) || !proposal.target || proposal.decision.status !== 'accepted' || proposal.effect_status !== 'materialized') throw new Error('采纳已受理，原目标尚待核对')
      const target = proposal.target
      let title: string
      if (target.kind === 'today_place') {
        const original = await api.getPersonalTodayDay(target.day_id, undefined, signal)
        validateTodayView(original, context.workspace_key, { date: original.day.date, timezone: original.day.timezone, planner_id: proposal.actor_id })
        const placement = original.day.placements.find(row => row.id === target.placement_id), entry = original.entries.find(row => row.metadata_id === target.metadata_id)
        if (original.day.id !== target.day_id || original.day.revision < target.day_revision || !placement || placement.metadata_id !== target.metadata_id || followupCanonical(placement.source_ref) !== followupCanonical(target.item_ref) || !entry || entry.availability !== 'available' || followupCanonical(entry.item_ref) !== followupCanonical(target.item_ref)) throw new Error('原安排版本或来源待核对')
        title = `${original.day.date} · ${entry.title}`
      } else {
        const original = validateAction(await api.personalCalendarAction(target.action_id, signal))
        if (original.id !== target.action_id || original.actor_id !== proposal.actor_id || original.revision !== target.action_revision || original.digest !== target.digest || original.status !== target.status || original.operation !== 'insert') throw new Error('原日程动作已变化，请到动作审批核对')
        title = original.body.summary
      }
      if (!controller.signal.aborted && active.current === key && seq === readSequence.current && live.current.connected) { setReadback({ proposal_id: proposalId, target, title, verified: true }); setNotice(target.kind === 'today_place' ? '原安排已读回' : target.status === 'draft' ? '原日程草稿已读回，尚未批准写入' : `原日程动作已读回 · ${followupActionStatusNames[target.status]}`) }
    } catch (cause) { if (active.current === key && seq === readSequence.current) setNotice(followupError(cause)) }
    finally { if (readController.current === controller) readController.current = null; if (active.current === key && seq === readSequence.current) setReadbackLoading(false) }
  }
  const current = connected && scope === key
  const observe = (value: FollowupProposal) => { if (!context || active.current !== key) return; const checked = validateFollowupProposal(value, context.workspace_key); if (checked.id === live.current.selectedId) { setSelected(checked); if (['source_stale', 'source_unavailable'].includes(checked.availability)) { readSequence.current++; setReadback(null) } } }
  return { context: current ? context : null, rules: current ? rules : [], page: current ? page : null, selected: current ? selected : null, selectedId, filter, actorId, pending, identityError, error, notice, loading, detailLoading, posting, readbackLoading, readback: current ? readback : null, refresh, select, clearSelection, more, submit, reconcileRequest, verifyTarget, observe, stopWaiting: () => writeController.current?.abort(), stopReadback: () => readController.current?.abort(),
    changeFilter: (value: FollowupFilter) => { setPage(null); setFilter(value) }, changeActor: (value: string) => { setPage(null); setActorId(value) }, locked: !current || !context || posting || Boolean(pending) || Boolean(identityError) || Boolean(pending && context && pending.workspace_key !== context.workspace_key) }
}
export type FollowupState = ReturnType<typeof useFollowups>
