import { useEffect, useRef, useState, type FormEvent } from 'react'
import { ArrowLeft, CalendarClock, FileText, Plus, X } from 'lucide-react'
import type { PersonalAgent } from '@naumi/shared/api/WorkbenchRuntimeClient'
import type { PersonalRoutineDefinition, PersonalRoutineDefinitionInput, PersonalRoutinePreview, RoutineScheduleInput } from '@naumi/shared/api/personalRoutines'
import { MaterialPicker } from '../items/ItemEditor'
import { artifactNavigationEvent, requestArtifactNavigation, type ArtifactNavigation } from '../library/navigation'
import { routineError, routineRequestId, type PersonalRoutinesApi, type PersonalRoutinesState } from './usePersonalRoutines'

export function routineRawDefinition(value: PersonalRoutineDefinition): PersonalRoutineDefinitionInput {
  const source = value.schedule
  const base = { timezone: source.timezone, starts_at: source.starts_at, ends_at: source.ends_at, dst_gap: source.dst_gap, dst_fold: source.dst_fold }
  const schedule: RoutineScheduleInput = source.kind === 'once'
    ? { ...base, kind: 'once', local_time: source.local_time, ...(source.fold !== undefined && source.fold !== null ? { fold: source.fold } : {}) }
    : source.kind === 'cron' ? { ...base, kind: 'cron', expression: source.expression }
      : { ...base, kind: 'trading_day', calendar_id: source.calendar_id, coverage_start: source.coverage_start, coverage_end: source.coverage_end, at: source.at, offset_minutes: source.offset_minutes, ...(source.at === 'time' ? { local_time: source.local_time } : {}) }
  return { title: value.title, instructions: value.instructions, tags: [...value.tags], assignee_id: value.assignee.id, assignee_version: value.assignee.version,
    schedule, policy: { ...value.policy, tool_names: [...value.policy.tool_names], artifact_operations: [...value.policy.artifact_operations] },
    input_refs: value.inputs.artifact_refs.map(ref => ({ ...ref })), directories: value.inputs.directories.map(directory => ({ ...directory })), limits: { ...value.limits }, recovery: { ...value.recovery } }
}
const utcField = (value: string | null) => value ? value.slice(0, 16) : ''
const utcValue = (value: string) => value ? new Date(value + ':00Z').toISOString() : null
export const routineMoney = (value: number | null | undefined) => value === null || value === undefined ? '待核对' : `$${(value / 1_000_000).toLocaleString('zh-CN', { maximumFractionDigits: 6 })}`
export function routineDate(value: string | null | undefined, zone?: string) {
  if (!value) return '—'
  try { return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short', ...(zone ? { timeZone: zone } : {}) }).format(new Date(value)) }
  catch { return '时间待核对' }
}

export function RoutineEditor({ api, agents, state, definition, routineId, revision, onClose, onSaved }: {
  api: PersonalRoutinesApi; agents: PersonalAgent[]; state: PersonalRoutinesState; definition?: PersonalRoutineDefinition
  routineId?: string; revision?: number; onClose: () => void; onSaved: (id: string) => void
}) {
  const context = state.context!
  const [form, setForm] = useState<PersonalRoutineDefinitionInput>(() => definition ? routineRawDefinition(definition) : ({
    title: '', instructions: '', tags: [], assignee_id: 'main', assignee_version: 0,
    schedule: { kind: 'cron', expression: '0 9 * * 1-5', timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai', starts_at: null, ends_at: null, dst_gap: 'skip', dst_fold: 'first' },
    input_refs: [], directories: [], policy: { ...context.default_policy },
    limits: { max_executions: 10, total_cost_microusd: 20_000_000, max_concurrent: 1, max_pending: 4 },
    recovery: { misfire_policy: 'skip', grace_seconds: 3600, max_catch_up: 1, overlap_policy: 'queue' },
  }))
  const [tags, setTags] = useState(form.tags.join('，')), [picker, setPicker] = useState(false), [error, setError] = useState('')
  const [enabled, setEnabled] = useState(true), [leave, setLeave] = useState<ArtifactNavigation | null>(null)
  const [now, setNow] = useState(Date.now())
  const saved = useRef(false), initial = useRef(JSON.stringify(form))
  const dirty = JSON.stringify(form) !== initial.current || Boolean(state.preview)
  const preview = state.preview, locked = state.locked
  const update = (fn: (value: PersonalRoutineDefinitionInput) => PersonalRoutineDefinitionInput) => { state.clearPreview(); setForm(fn); setError('') }
  const schedule = (patch: Partial<RoutineScheduleInput>) => update(value => ({ ...value, schedule: { ...value.schedule, ...patch } as RoutineScheduleInput }))
  useEffect(() => {
    const guard = (event: Event) => { if (dirty && !saved.current) { event.preventDefault(); setLeave((event as CustomEvent<ArtifactNavigation>).detail) } }
    const unload = (event: BeforeUnloadEvent) => { if (dirty && !saved.current) { event.preventDefault(); event.returnValue = '' } }
    window.addEventListener(artifactNavigationEvent, guard); window.addEventListener('beforeunload', unload)
    return () => { window.removeEventListener(artifactNavigationEvent, guard); window.removeEventListener('beforeunload', unload) }
  }, [dirty])
  useEffect(() => {
    if (!state.preview) return
    const delay = Math.max(1, Math.min(60_000, Date.parse(state.preview.expires_at) - Date.now() + 1))
    const timer = setTimeout(() => setNow(Date.now()), delay)
    return () => clearTimeout(timer)
  }, [state.preview, now])
  const changeKind = (kind: RoutineScheduleInput['kind']) => {
    const base = { timezone: form.schedule.timezone, starts_at: form.schedule.starts_at, ends_at: form.schedule.ends_at, dst_gap: 'skip' as const, dst_fold: form.schedule.dst_fold }
    const calendar = context.calendars[0]
    update(value => ({ ...value, schedule: kind === 'once' ? { ...base, kind, local_time: '' }
      : kind === 'cron' ? { ...base, kind, expression: '0 9 * * 1-5' }
        : { ...base, timezone: calendar?.timezone || base.timezone, kind, calendar_id: calendar?.id || '', coverage_start: calendar?.coverage_start || '', coverage_end: calendar?.coverage_end || '', at: 'close', offset_minutes: 0 } }))
  }
  const requestPreview = async (event: FormEvent) => {
    event.preventDefault(); setError('')
    try {
      if (!form.title.trim() || !form.instructions.trim()) throw new Error('请填写计划名称和执行要求')
      new Intl.DateTimeFormat('zh-CN', { timeZone: form.schedule.timezone }).format()
      const agent = form.assignee_id === 'main' ? null : agents.find(value => value.id === form.assignee_id && value.status === 'active' && value.kind !== 'team')
      if (form.assignee_id !== 'main' && !agent) throw new Error('所选 Agent 已停用，请重新选择')
      if (agent && agent.version !== form.assignee_version) throw new Error('Agent 配置版本已变化，请重新选择并确认')
      if (form.input_refs.length > 64 || form.directories.length > 8) throw new Error('材料最多 64 项，资料目录最多 8 项')
      if (form.directories.some(directory => !directory.path.trim())) throw new Error('请填写资料目录或移除空目录')
      if (form.limits.max_pending < form.limits.max_concurrent) throw new Error('待执行上限应不小于并发上限')
      if (form.schedule.kind === 'trading_day' && !context.calendars.some(calendar => calendar.id === (form.schedule as Extract<RoutineScheduleInput, { kind: 'trading_day' }>).calendar_id)) throw new Error('请选择已接入的交易日日历')
      const value = { ...form, title: form.title.trim(), instructions: form.instructions.trim(), tags: tags.split(/[,，]/).map(value => value.trim()).filter(Boolean), assignee_version: agent?.version || 0 }
      await state.submit({ operation: 'preview', body: { request_id: routineRequestId(), definition: value, ...(routineId ? { routine_id: routineId, expected_revision: revision } : {}) } })
    } catch (cause) { setError(routineError(cause)) }
  }
  const confirm = async () => {
    if (!preview) return
    try {
      if (Date.parse(preview.expires_at) <= Date.now()) throw new Error('预览已过期，请重新预览')
      if (preview.base_routine_id !== (routineId ?? null) || preview.base_revision !== (revision ?? null)) throw new Error('预览对应的计划版本已变化，请重新预览')
      const common = { request_id: routineRequestId(), preview_id: preview.preview_id, expected_digest: preview.definition_digest }
      const result = routineId
        ? await state.submit({ operation: 'revise', routine_id: routineId, body: { ...common, expected_revision: revision! } })
        : await state.submit({ operation: 'create', body: { ...common, enabled } })
      if (result && 'id' in result && 'budget' in result) { saved.current = true; onSaved(result.id) }
    } catch (cause) { setError(routineError(cause)) }
  }
  const expired = Boolean(preview && Date.parse(preview.expires_at) <= now)
  return <section className="pr-editor" aria-label={routineId ? '修改周期计划' : '新建周期计划'}>
    <header className="pr-editor-heading"><button type="button" aria-label="返回周期计划" onClick={() => requestArtifactNavigation(onClose)}><ArrowLeft size={18} /></button><h2>{routineId ? '修改计划' : '新建计划'}</h2></header>
    {leave && <div className="pr-confirm" role="alertdialog" aria-label="放弃未保存计划"><strong>保留未保存的计划？</strong><div><button onClick={() => { leave.cancel?.(); setLeave(null) }}>继续编辑</button><button onClick={() => { saved.current = true; const target = leave; setLeave(null); target.proceed() }}>放弃修改并离开</button></div></div>}
    <form onSubmit={requestPreview}>
      <fieldset disabled={locked}>
        <label>计划名称<input aria-label="计划名称" required maxLength={120} value={form.title} onChange={event => update(value => ({ ...value, title: event.target.value }))} /></label>
        <label>执行要求<textarea aria-label="计划执行要求" required maxLength={12000} rows={5} value={form.instructions} onChange={event => update(value => ({ ...value, instructions: event.target.value }))} /></label>
        <div className="pr-form-row"><label>执行 Agent<select aria-label="计划执行 Agent" value={form.assignee_id} onChange={event => { const agent = agents.find(agent => agent.id === event.target.value); update(value => ({ ...value, assignee_id: event.target.value, assignee_version: agent?.version || 0, input_refs: [], policy: { ...value.policy, model_tier: agent?.model_tier || context.default_policy.model_tier, tool_names: agent ? value.policy.tool_names.filter(tool => agent.tool_names.includes(tool)) : [...context.default_policy.tool_names] } })) }}><option value="main">主 Agent</option>{agents.filter(agent => agent.status === 'active' && agent.kind !== 'team').map(agent => <option key={agent.id} value={agent.id}>{agent.name} · v{agent.version}</option>)}{form.assignee_id !== 'main' && !agents.some(agent => agent.id === form.assignee_id && agent.status === 'active' && agent.kind !== 'team') && <option disabled value={form.assignee_id}>{definition?.assignee.name || form.assignee_id} · 已停用</option>}</select></label><label>标签<input aria-label="计划标签" value={tags} onChange={event => { setTags(event.target.value); update(value => ({ ...value, tags: event.target.value.split(/[,，]/) })) }} /></label></div>
        <section><h3>执行时间</h3><div className="pr-form-row"><label>规则<select aria-label="计划时间规则" value={form.schedule.kind} onChange={event => changeKind(event.target.value as RoutineScheduleInput['kind'])}><option value="once">单次</option><option value="cron">周期（cron）</option><option value="trading_day" disabled={!context.calendars.length}>交易日</option></select></label><label>时区<input aria-label="计划时区" required value={form.schedule.timezone} onChange={event => schedule({ timezone: event.target.value })} /></label></div>
          {form.schedule.kind === 'cron' && <label>cron 表达式<input aria-label="cron 表达式" required maxLength={200} value={form.schedule.expression} onChange={event => schedule({ expression: event.target.value })} /></label>}
          {form.schedule.kind === 'once' && <div className="pr-form-row"><label>当地时间<input aria-label="计划当地时间" type="datetime-local" required value={form.schedule.local_time.slice(0, 16)} onChange={event => schedule({ local_time: event.target.value + ':00' })} /></label><label>重复时刻<select aria-label="单次重复时刻" value={form.schedule.fold ?? ''} onChange={event => schedule({ fold: event.target.value === '' ? undefined : Number(event.target.value) as 0 | 1 })}><option value="">需明确时询问</option><option value="0">第一次</option><option value="1">第二次</option></select></label></div>}
          {form.schedule.kind === 'trading_day' && <><label>交易日日历<select aria-label="交易日日历" required value={form.schedule.calendar_id} onChange={event => { const calendar = context.calendars.find(value => value.id === event.target.value); if (calendar) schedule({ calendar_id: calendar.id, coverage_start: calendar.coverage_start, coverage_end: calendar.coverage_end, timezone: calendar.timezone }) }}>{context.calendars.map(calendar => <option key={calendar.id} value={calendar.id}>{calendar.name} · {calendar.coverage_start}—{calendar.coverage_end}</option>)}</select></label><div className="pr-form-row"><label>触发基准<select aria-label="交易日触发基准" value={form.schedule.at} onChange={event => schedule({ at: event.target.value as 'open' | 'close' | 'time', ...(event.target.value === 'time' ? { local_time: '09:00' } : { local_time: undefined }) })}><option value="open">开市</option><option value="close">收市</option><option value="time">指定时间</option></select></label>{form.schedule.at === 'time' && <label>当地时刻<input aria-label="交易日当地时刻" type="time" required value={form.schedule.local_time || ''} onChange={event => schedule({ local_time: event.target.value })} /></label>}<label>偏移（分钟）<input aria-label="交易日偏移分钟" type="number" min={-10080} max={10080} step={1} required value={form.schedule.offset_minutes} onChange={event => schedule({ offset_minutes: Number(event.target.value) })} /></label></div></>}
          <div className="pr-form-row"><label>开始（UTC）<input aria-label="计划开始UTC" type="datetime-local" value={utcField(form.schedule.starts_at)} onChange={event => schedule({ starts_at: utcValue(event.target.value) })} /></label><label>结束（UTC）<input aria-label="计划结束UTC" type="datetime-local" value={utcField(form.schedule.ends_at)} onChange={event => schedule({ ends_at: utcValue(event.target.value) })} /></label></div>
          <label>夏令时重复小时<select aria-label="周期重复小时" value={form.schedule.dst_fold} onChange={event => schedule({ dst_fold: event.target.value as 'first' | 'second' | 'both' })}><option value="first">只执行第一次</option><option value="second">只执行第二次</option><option value="both">两次分别执行</option></select></label>
        </section>
        <section><div className="pr-section-heading"><h3>材料 · {form.input_refs.length}</h3><button type="button" onClick={() => setPicker(true)}><Plus size={14} />选择材料</button></div>{form.input_refs.map(ref => <div className="pr-material" key={`${ref.artifact_id}:${ref.version}`}><FileText size={16} /><code>{ref.artifact_id}</code><span>v{ref.version}</span><button type="button" aria-label={`移除计划材料 ${ref.artifact_id}`} onClick={() => update(value => ({ ...value, input_refs: value.input_refs.filter(value => value !== ref) }))}><X size={14} /></button></div>)}
          <details><summary>资料目录</summary>{form.directories.map((directory, index) => <div className="pr-directory" key={index}><label>目录<input aria-label={`计划资料目录 ${index + 1}`} value={directory.path} onChange={event => update(value => ({ ...value, directories: value.directories.map((value, n) => n === index ? { ...value, path: event.target.value } : value) }))} /></label><label>文件匹配<input aria-label={`计划文件匹配 ${index + 1}`} value={directory.pattern} onChange={event => update(value => ({ ...value, directories: value.directories.map((value, n) => n === index ? { ...value, pattern: event.target.value } : value) }))} /></label><button type="button" aria-label={`移除计划目录 ${index + 1}`} onClick={() => update(value => ({ ...value, directories: value.directories.filter((_, n) => n !== index) }))}><X size={14} /></button></div>)}<button type="button" disabled={form.directories.length >= 8} onClick={() => update(value => ({ ...value, directories: [...value.directories, { path: '', pattern: '**/*', max_files: 100, max_bytes: 52428800 }] }))}><Plus size={14} />添加目录</button></details>
        </section>
        <section><h3>次数与估算额度</h3><div className="pr-form-row"><label>执行次数上限<input aria-label="计划执行次数上限" type="number" min={0} max={1000000} step={1} required value={form.limits.max_executions} onChange={event => update(value => ({ ...value, limits: { ...value.limits, max_executions: Number(event.target.value) } }))} /></label><label>总额度（美元）<input aria-label="计划总额度" type="number" min={0} max={1000000000} step="0.000001" required value={form.limits.total_cost_microusd / 1_000_000} onChange={event => update(value => ({ ...value, limits: { ...value.limits, total_cost_microusd: Math.round(Number(event.target.value) * 1_000_000) } }))} /></label></div><div className="pr-form-row"><label>并发上限<input aria-label="计划并发上限" type="number" min={1} max={64} step={1} required value={form.limits.max_concurrent} onChange={event => update(value => ({ ...value, limits: { ...value.limits, max_concurrent: Number(event.target.value) } }))} /></label><label>待执行上限<input aria-label="计划待执行上限" type="number" min={form.limits.max_concurrent} max={10000} step={1} required value={form.limits.max_pending} onChange={event => update(value => ({ ...value, limits: { ...value.limits, max_pending: Number(event.target.value) } }))} /></label></div></section>
        <details><summary>恢复与重叠</summary><div className="pr-form-row"><label>错过触发<select aria-label="计划错过触发" value={form.recovery.misfire_policy} onChange={event => update(value => ({ ...value, recovery: { ...value.recovery, misfire_policy: event.target.value as 'skip' | 'latest' | 'catch_up' } }))}><option value="skip">跳过</option><option value="latest">只补最近一次</option><option value="catch_up">限量补执行</option></select></label><label>重叠执行<select aria-label="计划重叠执行" value={form.recovery.overlap_policy} onChange={event => update(value => ({ ...value, recovery: { ...value.recovery, overlap_policy: event.target.value as 'queue' | 'skip' | 'allow' } }))}><option value="queue">排队</option><option value="skip">跳过重叠</option><option value="allow">允许并发</option></select></label></div><div className="pr-form-row"><label>宽限期（秒）<input aria-label="计划宽限期" type="number" min={0} max={604800} step={1} required value={form.recovery.grace_seconds} onChange={event => update(value => ({ ...value, recovery: { ...value.recovery, grace_seconds: Number(event.target.value) } }))} /></label><label>最多补执行<input aria-label="计划最多补执行" type="number" min={1} max={1000} step={1} required value={form.recovery.max_catch_up} onChange={event => update(value => ({ ...value, recovery: { ...value.recovery, max_catch_up: Number(event.target.value) } }))} /></label></div></details>
        <details><summary>单次执行限制</summary><div className="pr-form-row"><label>模型<select aria-label="计划模型" disabled={form.assignee_id !== 'main'} value={form.policy.model_tier} onChange={event => update(value => ({ ...value, policy: { ...value.policy, model_tier: event.target.value as typeof value.policy.model_tier } }))}><option value="fast">快速</option><option value="capable">标准</option><option value="reasoning">深度思考</option></select></label><label>最大轮数<input aria-label="计划单次最大轮数" type="number" min={1} max={100} required value={form.policy.max_turns} onChange={event => update(value => ({ ...value, policy: { ...value.policy, max_turns: Number(event.target.value) } }))} /></label></div><div className="pr-form-row"><label>单次额度（美元）<input aria-label="计划单次额度" type="number" min={0} max={100} step="0.000001" required value={form.policy.max_cost_microusd / 1_000_000} onChange={event => update(value => ({ ...value, policy: { ...value.policy, max_cost_microusd: Math.round(Number(event.target.value) * 1_000_000) } }))} /></label><label>超时（秒）<input aria-label="计划单次超时" type="number" min={1} max={3600} required value={form.policy.timeout_seconds} onChange={event => update(value => ({ ...value, policy: { ...value.policy, timeout_seconds: Number(event.target.value) } }))} /></label></div><div className="pr-tool-options">{['personal_artifacts', 'code_execute', 'bash_run', 'file_read', 'read'].map(tool => <label key={tool}><input type="checkbox" disabled={form.assignee_id !== 'main' && !agents.find(agent => agent.id === form.assignee_id)?.tool_names.includes(tool)} checked={form.policy.tool_names.includes(tool)} onChange={event => update(value => ({ ...value, policy: { ...value.policy, tool_names: event.target.checked ? [...value.policy.tool_names, tool] : value.policy.tool_names.filter(value => value !== tool) } }))} />{({ personal_artifacts: '资料库', code_execute: '隔离计算', bash_run: '命令执行', file_read: '文件读取', read: '内容读取' } as Record<string, string>)[tool]}</label>)}</div></details>
      </fieldset>
      {error && <div className="pr-error" role="alert">{error}</div>}
      <footer className="pr-form-actions"><button type="button" onClick={() => requestArtifactNavigation(onClose)}>取消</button><button className="pr-primary" disabled={locked} type="submit"><CalendarClock size={15} />{state.posting ? '正在核对…' : '预览计划'}</button></footer>
    </form>
    {preview && <Preview value={preview} expired={expired} />}
    {preview && <div className="pr-preview-confirm">{routineId ? <p>修改仅影响后续，已受理执行保留原版本。</p> : <label className="pr-inline-check"><input type="checkbox" checked={enabled} disabled={locked} onChange={event => setEnabled(event.target.checked)} />保存后启用</label>}<button type="button" className="pr-primary" disabled={locked || expired} onClick={() => void confirm()}>{routineId ? '确认修改计划' : enabled ? '确认保存并启用' : '确认保存为暂停'}</button></div>}
    {picker && <MaterialPicker api={api} actor={form.assignee_id} selected={form.input_refs} onClose={() => setPicker(false)} onSelect={refs => { update(value => ({ ...value, input_refs: refs })); setPicker(false) }} />}
  </section>
}
function Preview({ value, expired }: { value: PersonalRoutinePreview; expired: boolean }) {
  return <section className="pr-preview" aria-label="计划预览"><div className="pr-section-heading"><h3>近期触发</h3><span>{value.definition.schedule.timezone}</span></div><ol>{value.schedule_preview.map((entry, index) => <li key={`${entry.due_at}:${index}`}><time dateTime={entry.due_at}>{entry.local_time}</time><span>{entry.fold === 1 ? '第二次' : ''}</span><code>{entry.due_at}</code>{entry.calendar_evidence && <details><summary>日历依据</summary><pre>{JSON.stringify(entry.calendar_evidence, null, 2)}</pre></details>}</li>)}</ol><dl><dt>执行次数上限</dt><dd>{value.definition.limits.max_executions}</dd><dt>总估算额度</dt><dd>{routineMoney(value.definition.limits.total_cost_microusd)}</dd><dt>单次估算上限</dt><dd>{routineMoney(value.definition.policy.max_cost_microusd)}</dd><dt>预览有效期</dt><dd>{routineDate(value.expires_at)}{expired ? ' · 已过期' : ''}</dd></dl><p className="pr-accounting">本地单价估算上限，非服务商最终账单。</p></section>
}
