import { useEffect, useRef, useState, type FormEvent } from 'react'
import { ArrowLeft, Check, FileText, Plus, Search, X } from 'lucide-react'
import type { PersonalAgent } from '@naumi/shared/api/WorkbenchRuntimeClient'
import type { PersonalArtifactRef, PersonalArtifactSnapshot } from '@naumi/shared/api/personalArtifacts'
import type { PersonalItem, PersonalItemDirectory, PersonalItemInput, PersonalItemPolicy } from '@naumi/shared/api/personalItems'
import { artifactNavigationEvent, requestArtifactNavigation, type ArtifactNavigation } from '../library/navigation'
import { validateArtifact } from '../library/useArtifactLibrary'
import { itemError, type PersonalItemsApi, type PersonalItemsState } from './usePersonalItems'

const defaultPolicy: PersonalItemPolicy = { tool_names: ['personal_artifacts', 'code_execute'], artifact_operations: ['get', 'read', 'publish'], model_tier: 'capable', max_turns: 12, max_cost_microusd: 2000000, timeout_seconds: 180 }
const exact = (artifact: PersonalArtifactSnapshot): PersonalArtifactRef => ({ artifact_id: artifact.artifact_id, version: artifact.version, sha256: artifact.sha256, grant_id: artifact.grant_id, grant_revision: artifact.grant_revision })
export function ItemEditor({ item, agents, api, state, onSave, onClose }: {
  item?: PersonalItem; agents: PersonalAgent[]; api: PersonalItemsApi; state: PersonalItemsState
  onSave: (body: Omit<PersonalItemInput, 'request_id'>) => Promise<boolean>; onClose: () => void
}) {
  const definition = item?.definition
  const [title, setTitle] = useState(definition?.title || ''), [instructions, setInstructions] = useState(definition?.instructions || '')
  const [tags, setTags] = useState(definition?.tags.join('，') || ''), [actor, setActor] = useState(definition?.assignee.id || 'main')
  const [mode, setMode] = useState<'now' | 'once'>(definition?.schedule.mode || 'now')
  const [localTime, setLocalTime] = useState(definition?.schedule.local_time?.slice(0, 16) || '')
  const [timezone, setTimezone] = useState(definition?.schedule.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai')
  const [fold, setFold] = useState(definition?.schedule.fold === 0 ? '0' : definition?.schedule.fold === 1 ? '1' : '')
  const [misfire, setMisfire] = useState<'run_once' | 'skip'>(definition?.schedule.misfire_policy || 'run_once')
  const [refs, setRefs] = useState<PersonalArtifactRef[]>(definition?.inputs.artifact_refs || [])
  const [directories, setDirectories] = useState<PersonalItemDirectory[]>(definition?.inputs.directories || [])
  const [policy, setPolicy] = useState<PersonalItemPolicy>(definition?.policy || defaultPolicy)
  const [customPolicy, setCustomPolicy] = useState(Boolean(definition?.policy))
  const [error, setError] = useState(''), [picker, setPicker] = useState(false)
  const [leave, setLeave] = useState<ArtifactNavigation | null>(null)
  const saved = useRef(false)
  const current = JSON.stringify({ title, instructions, tags, actor, mode, localTime, timezone, fold, misfire, refs, directories, policy, customPolicy })
  const initial = useRef(current), dirty = current !== initial.current
  useEffect(() => {
    const guard = (event: Event) => {
      if (!dirty || saved.current) return
      event.preventDefault(); setLeave((event as CustomEvent<ArtifactNavigation>).detail)
    }
    const unload = (event: BeforeUnloadEvent) => { if (dirty && !saved.current) { event.preventDefault(); event.returnValue = '' } }
    window.addEventListener(artifactNavigationEvent, guard); window.addEventListener('beforeunload', unload)
    return () => { window.removeEventListener(artifactNavigationEvent, guard); window.removeEventListener('beforeunload', unload) }
  }, [dirty])
  const save = async (event: FormEvent) => {
    event.preventDefault(); setError('')
    try {
      if (!title.trim() || !instructions.trim()) throw new Error('请填写事项名称和执行要求')
      new Intl.DateTimeFormat('zh-CN', { timeZone: timezone }).format()
      if (mode === 'once' && !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(localTime)) throw new Error('请选择执行时间')
      const agent = actor === 'main' ? null : agents.find(value => value.id === actor && value.status === 'active' && value.kind !== 'team')
      if (actor !== 'main' && !agent) throw new Error('所选 Agent 已停用或版本已变化，请重新选择')
      if (refs.length > 64) throw new Error('材料最多选择 64 项，请移除超出的材料')
      if (directories.some(value => !value.path.trim())) throw new Error('请填写资料目录或移除空目录')
      const accepted = await onSave({ title: title.trim(), instructions: instructions.trim(), tags: tags.split(/[,，]/).map(value => value.trim()).filter(Boolean),
        assignee_id: actor, assignee_version: agent?.version || 0,
        schedule: { mode, timezone, ...(mode === 'once' ? { local_time: localTime + ':00', ...(fold !== '' ? { fold: Number(fold) as 0 | 1 } : {}) } : {}), misfire_policy: misfire, grace_seconds: definition?.schedule.grace_seconds ?? 86400 },
        input_refs: refs, directories, ...(customPolicy ? { policy } : {}),
      })
      if (accepted) saved.current = true
    } catch (cause) { setError(itemError(cause)) }
  }
  const locked = state.locked
  return <section className="pi-editor" aria-label={item ? '编辑事项' : '新建事项'}>
    <header className="pi-editor-header"><button type="button" onClick={() => requestArtifactNavigation(onClose)} aria-label="返回事项"><ArrowLeft size={18} /></button><h2>{item ? '编辑事项' : '新建事项'}</h2></header>
    {leave && <div className="pi-confirm" role="alertdialog" aria-label="放弃未保存事项"><strong>保留未保存的修改？</strong><div><button type="button" onClick={() => { leave.cancel?.(); setLeave(null) }}>继续编辑</button><button type="button" onClick={() => { saved.current = true; const target = leave; setLeave(null); onClose(); target.proceed() }}>放弃修改并离开</button></div></div>}
    <form onSubmit={save}>
      <fieldset disabled={locked}>
        <label>事项名称<input autoFocus aria-label="事项名称" required maxLength={120} value={title} onChange={event => setTitle(event.target.value)} /></label>
        <label>执行要求<textarea aria-label="执行要求" required rows={6} value={instructions} onChange={event => setInstructions(event.target.value)} /></label>
        <div className="pi-form-row"><label>执行 Agent<select aria-label="执行 Agent" value={actor} onChange={event => { setActor(event.target.value); setRefs([]) }}><option value="main">主 Agent</option>{agents.filter(agent => agent.status === 'active' && agent.kind !== 'team').map(agent => <option key={agent.id} value={agent.id}>{agent.name} · v{agent.version}</option>)}{actor !== 'main' && !agents.some(agent => agent.id === actor && agent.status === 'active' && agent.kind !== 'team') && <option value={actor} disabled>{definition?.assignee.name || actor} · 已停用</option>}</select></label>
          <label>标签<input aria-label="事项标签" value={tags} onChange={event => setTags(event.target.value)} /></label></div>
        <section className="pi-form-section"><h3>执行时间</h3><div className="pi-segment" role="group" aria-label="执行方式"><button type="button" aria-pressed={mode === 'now'} onClick={() => setMode('now')}>立即</button><button type="button" aria-pressed={mode === 'once'} onClick={() => setMode('once')}>指定时间</button></div>
          {mode === 'once' && <div className="pi-form-row"><label>当地时间<input aria-label="当地时间" type="datetime-local" required value={localTime} onChange={event => setLocalTime(event.target.value)} /></label><label>重复小时<select aria-label="夏令时重复小时" value={fold} onChange={event => setFold(event.target.value)}><option value="">自动判定</option><option value="0">第一次（fold 0）</option><option value="1">第二次（fold 1）</option></select></label></div>}
          <div className="pi-form-row"><label>时区<input aria-label="时区" required value={timezone} onChange={event => setTimezone(event.target.value)} list="pi-timezones" /><datalist id="pi-timezones">{[Intl.DateTimeFormat().resolvedOptions().timeZone, 'Asia/Shanghai', 'UTC', 'America/New_York', 'Europe/London', 'Asia/Tokyo'].filter((value, index, values) => value && values.indexOf(value) === index).map(value => <option key={value} value={value} />)}</datalist></label><label>错过时间后<select aria-label="错过时间后" value={misfire} onChange={event => setMisfire(event.target.value as typeof misfire)}><option value="run_once">补执行一次</option><option value="skip">跳过</option></select></label></div>
        </section>
        <section className="pi-form-section"><div className="pi-section-title"><h3>材料 <small>{refs.length}</small></h3><button type="button" onClick={() => setPicker(true)}><Plus size={15} />选择材料</button></div>
          {refs.map(ref => <div className="pi-input-ref" key={`${ref.artifact_id}:${ref.version}`}><FileText size={16} /><code>{ref.artifact_id}</code><span>v{ref.version}</span><button type="button" aria-label={`移除材料 ${ref.artifact_id}`} onClick={() => setRefs(values => values.filter(value => value !== ref))}><X size={15} /></button></div>)}
          <details><summary>资料目录</summary>{directories.map((directory, index) => <div className="pi-directory-input" key={index}><input aria-label={`资料目录 ${index + 1}`} value={directory.path} onChange={event => setDirectories(values => values.map((value, n) => n === index ? { ...value, path: event.target.value } : value))} /><input aria-label={`文件匹配 ${index + 1}`} value={directory.pattern} onChange={event => setDirectories(values => values.map((value, n) => n === index ? { ...value, pattern: event.target.value } : value))} /><button type="button" aria-label={`移除目录 ${index + 1}`} onClick={() => setDirectories(values => values.filter((_, n) => n !== index))}><X size={15} /></button></div>)}<button type="button" disabled={directories.length >= 8} onClick={() => setDirectories(values => [...values, { path: '', pattern: '**/*', max_files: 100, max_bytes: 52428800 }])}><Plus size={14} />添加目录</button></details>
        </section>
        <details className="pi-form-section"><summary>执行限制</summary><label className="pi-check"><input type="checkbox" checked={customPolicy} onChange={event => setCustomPolicy(event.target.checked)} />自定义限制</label>{customPolicy && <><div className="pi-form-row"><label>模型<select aria-label="事项模型" value={policy.model_tier} onChange={event => setPolicy(value => ({ ...value, model_tier: event.target.value as PersonalItemPolicy['model_tier'] }))}><option value="fast">快速</option><option value="capable">标准</option><option value="reasoning">深度思考</option></select></label><label>最大轮数<input aria-label="最大轮数" type="number" min={1} max={100} required value={policy.max_turns} onChange={event => setPolicy(value => ({ ...value, max_turns: Number(event.target.value) }))} /></label></div><div className="pi-form-row"><label>预算（美元）<input aria-label="事项预算" type="number" step="0.01" min="0.01" max={100} required value={policy.max_cost_microusd / 1000000} onChange={event => setPolicy(value => ({ ...value, max_cost_microusd: Math.round(Number(event.target.value) * 1000000) }))} /></label><label>超时（秒）<input aria-label="事项超时" type="number" min={1} max={3600} required value={policy.timeout_seconds} onChange={event => setPolicy(value => ({ ...value, timeout_seconds: Number(event.target.value) }))} /></label></div><div className="pi-tool-options">{['personal_artifacts', 'code_execute', 'bash_run', 'file_read', 'read'].map(tool => <label className="pi-check" key={tool}><input type="checkbox" checked={policy.tool_names.includes(tool)} onChange={event => setPolicy(value => ({ ...value, tool_names: event.target.checked ? [...value.tool_names, tool] : value.tool_names.filter(name => name !== tool) }))} />{tool}</label>)}</div></>}</details>
      </fieldset>
      {error && <div className="pi-error" role="alert">{error}</div>}
      <footer className="pi-form-actions"><button type="button" onClick={() => requestArtifactNavigation(onClose)}>取消</button><button className="pi-primary" disabled={locked} type="submit">{state.posting ? '正在保存…' : item ? '保存修改' : '创建事项'}</button></footer>
    </form>
    {picker && <MaterialPicker key={actor} api={api} actor={actor} selected={refs} onClose={() => setPicker(false)} onSelect={value => { setRefs(value); setPicker(false) }} />}
  </section>
}

function MaterialPicker({ api, actor, selected, onSelect, onClose }: { api: PersonalItemsApi; actor: string; selected: PersonalArtifactRef[]; onSelect: (refs: PersonalArtifactRef[]) => void; onClose: () => void }) {
  const [artifacts, setArtifacts] = useState<PersonalArtifactSnapshot[]>([]), [query, setQuery] = useState(''), [offset, setOffset] = useState(0)
  const dialogRef = useRef<HTMLElement>(null)
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    const dialog = dialogRef.current
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); onClose(); return }
      if (event.key !== 'Tab' || !dialog) return
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),[tabindex="0"]'))
      const first = focusable[0], last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
    }
    dialog?.querySelector<HTMLElement>('input')?.focus(); dialog?.addEventListener('keydown', key)
    return () => { dialog?.removeEventListener('keydown', key); previous?.focus() }
  }, [onClose])
  const [refs, setRefs] = useState(selected), [loading, setLoading] = useState(false), [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController(); setLoading(true)
    const timer = setTimeout(() => { void api.personalArtifacts({ actor_id: actor, q: query.trim(), limit: 30, offset }, controller.signal)
      .then(page => { if (!controller.signal.aborted) { setArtifacts(page.artifacts.map(value => validateArtifact(value))); setError('') } })
      .catch(cause => { if (!controller.signal.aborted) setError(itemError(cause)) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) }) }, query ? 180 : 0)
    return () => { clearTimeout(timer); controller.abort() }
  }, [api, actor, query, offset])
  return <div className="pi-modal-backdrop"><section ref={dialogRef} className="pi-material-picker" role="dialog" aria-modal="true" aria-label="选择已授权材料"><header><h2>选择材料</h2><button type="button" onClick={onClose} aria-label="关闭材料选择"><X size={18} /></button></header><label className="pi-search"><Search size={16} /><input autoFocus aria-label="搜索已授权材料" value={query} onChange={event => { setQuery(event.target.value); setOffset(0) }} /></label>
    {error && <div className="pi-error" role="alert">{error}</div>}{refs.length > 64 && <div className="pi-error" role="alert">材料最多选择 64 项，请移除超出的材料</div>}{loading && <div role="status">正在加载材料…</div>}
    <div className="pi-picker-list">{artifacts.map(artifact => {
      const checked = refs.some(ref => ref.artifact_id === artifact.artifact_id && ref.version === artifact.version)
      return <label key={`${artifact.artifact_id}:${artifact.version}`}><input type="checkbox" checked={checked} onChange={event => setRefs(values => event.target.checked ? [...values.filter(value => value.artifact_id !== artifact.artifact_id), exact(artifact)] : values.filter(value => value.artifact_id !== artifact.artifact_id || value.version !== artifact.version))} /><FileText size={18} /><span><strong>{artifact.title}</strong><small>v{artifact.version} · {artifact.bytes.toLocaleString()} B</small></span>{checked && <Check size={16} />}</label>
    })}{!loading && !error && !artifacts.length && <div className="pi-empty">暂无已授权材料</div>}</div><footer><button type="button" disabled={!offset || loading} onClick={() => setOffset(value => Math.max(0, value - 30))}>上一页</button><button type="button" disabled={artifacts.length < 30 || loading} onClick={() => setOffset(value => value + 30)}>下一页</button><button type="button" className="pi-primary" disabled={refs.length > 64} onClick={() => onSelect(refs)}>选择 {refs.length} 项</button></footer></section></div>
}
