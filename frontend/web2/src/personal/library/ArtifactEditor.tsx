import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { ArrowDown, ArrowUp, Check, ChevronDown, Code2, Eye, FileText, Link2, Loader2, Plus, Search, Trash2, X } from 'lucide-react'
import type { PersonalArtifactKind, PersonalArtifactPage, PersonalArtifactRef, PersonalArtifactSnapshot } from '@naumi/shared/api/personalArtifacts'
import { ArtifactMarkdown, PrivateWidget, artifactFilename, validatePreview } from './ArtifactPage'
import { artifactError, validateArtifact, type ArtifactApi, type ArtifactLibraryState } from './useArtifactLibrary'
import { artifactNavigationEvent, requestArtifactNavigation, type ArtifactNavigation } from './navigation'

export type ArtifactEditorValue = { item?: PersonalArtifactSnapshot; content: string }

function asPage(content: string): PersonalArtifactPage | null {
  try {
    const value = JSON.parse(content)
    return value?.type === 'page' ? validatePreview(value) as PersonalArtifactPage : null
  } catch { return null }
}
function SourcePicker({ api, actorId, value, onChange, excludeId, disabled }: {
  api: ArtifactApi; actorId: string; value: PersonalArtifactRef[]; onChange: (value: PersonalArtifactRef[]) => void
  excludeId?: string; disabled: boolean
}) {
  const [query, setQuery] = useState(''), [items, setItems] = useState<PersonalArtifactSnapshot[]>([]), [loading, setLoading] = useState(false), [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    const timer = setTimeout(() => {
      void api.personalArtifacts({ actor_id: actorId, q: query.trim(), limit: 20, offset: 0 }, controller.signal).then(result => {
        if (!Array.isArray(result.artifacts)) throw new Error('来源目录返回不完整')
        const values = result.artifacts.map(item => validateArtifact(item)).filter(item => item.artifact_id !== excludeId)
        if (!controller.signal.aborted) { setItems(values); setError('') }
      }).catch(cause => { if (!controller.signal.aborted) setError(artifactError(cause)) })
        .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    }, query ? 180 : 0)
    return () => { clearTimeout(timer); controller.abort() }
  }, [api, actorId, query, excludeId])
  return <details className="al-editor-sources"><summary><Link2 size={14} />来源 {value.length}<ChevronDown size={14} /></summary>
    <label className="al-search"><Search size={15} /><input aria-label="搜索可引用资料" value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索资料" /></label>
    {loading && <p role="status">正在加载来源…</p>}{error && <p role="alert" className="al-error">{error}</p>}
    <div className="al-source-options">{items.map(item => {
      const selected = value.some(ref => ref.artifact_id === item.artifact_id && ref.version === item.version)
      return <label key={item.artifact_id}><input type="checkbox" disabled={disabled || !selected && value.length >= 32} checked={selected} onChange={event => onChange(event.target.checked
        ? [...value.filter(ref => ref.artifact_id !== item.artifact_id), { artifact_id: item.artifact_id, version: item.version, sha256: item.sha256, grant_id: item.grant_id, grant_revision: item.grant_revision }]
        : value.filter(ref => ref.artifact_id !== item.artifact_id || ref.version !== item.version))} /><span>{item.title}</span><code>v{item.version}</code></label>
    })}</div>
    {value.filter(ref => !items.some(item => item.artifact_id === ref.artifact_id && item.version === ref.version)).map(ref => <div className="al-source-preserved" key={`${ref.artifact_id}:${ref.version}`}><code>{ref.artifact_id} · v{ref.version}</code><button type="button" disabled={disabled} aria-label={`移除来源${ref.artifact_id}`} onClick={() => onChange(value.filter(source => source !== ref))}><X size={13} /></button></div>)}
  </details>
}

function StructuredPageEditor({ value, onChange, disabled, widgetDrafts, onWidgetChange }: {
  value: PersonalArtifactPage; onChange: (value: PersonalArtifactPage) => void; disabled: boolean
  widgetDrafts: Record<string, string>; onWidgetChange: (id: string, code: string) => void
}) {
  const update = (index: number, changes: Partial<PersonalArtifactPage['sections'][number]>) => onChange({ ...value, sections: value.sections.map((section, n) => n === index ? { ...section, ...changes } : section) })
  const move = (index: number, direction: number) => {
    const sections = [...value.sections], other = index + direction
    ;[sections[index], sections[other]] = [sections[other], sections[index]]
    onChange({ ...value, sections })
  }
  return <div className="al-section-editor">{value.sections.map((section, index) => <section key={section.id}>
    <header><label>章节标题<input aria-label={`章节 ${index + 1} 标题`} value={section.title} maxLength={120} disabled={disabled} onChange={event => update(index, { title: event.target.value })} /></label>
      <select aria-label={`章节 ${index + 1} 证据标注`} value={section.evidence_status} disabled={disabled} onChange={event => update(index, { evidence_status: event.target.value as typeof section.evidence_status })}><option value="unknown">未知</option><option value="fact">事实</option><option value="estimate">估算</option></select>
      <button type="button" aria-label={`上移章节 ${index + 1}`} disabled={disabled || !index} onClick={() => move(index, -1)}><ArrowUp size={14} /></button>
      <button type="button" aria-label={`下移章节 ${index + 1}`} disabled={disabled || index === value.sections.length - 1} onClick={() => move(index, 1)}><ArrowDown size={14} /></button>
      <button type="button" aria-label={`删除章节 ${index + 1}`} disabled={disabled || value.sections.length === 1} onClick={() => onChange({ ...value, sections: value.sections.filter((_, n) => n !== index) })}><Trash2 size={14} /></button>
    </header>
    <textarea aria-label={`章节 ${index + 1} 正文`} value={section.markdown || ''} maxLength={100000} rows={7} disabled={disabled} onChange={event => update(index, { markdown: event.target.value })} />
    {section.widget ? <details><summary><Code2 size={13} />交互组件</summary><WidgetEditor code={widgetDrafts[section.id] ?? JSON.stringify(section.widget, null, 2)} disabled={disabled} onChange={code => onWidgetChange(section.id, code)} /></details>
      : <div className="al-widget-actions"><button type="button" disabled={disabled} onClick={() => update(index, { widget: { version: 1, type: 'table', title: section.title || '表格', columns: [{ key: 'item', label: '项目' }, { key: 'value', label: '数值' }], rows: [] } })}><Plus size={13} />添加表格</button></div>}
  </section>)}<button type="button" disabled={disabled || value.sections.length >= 40} onClick={() => onChange({ ...value, sections: [...value.sections, { id: 'section-' + crypto.randomUUID(), title: '', markdown: '', evidence_status: 'unknown' }] })}><Plus size={14} />添加章节</button></div>
}
function WidgetEditor({ code, onChange, disabled }: { code: string; onChange: (value: string) => void; disabled: boolean }) {
  let valid = false
  try { const parsed = JSON.parse(code); valid = Boolean(parsed && typeof parsed === 'object' && !Array.isArray(parsed)) } catch { /* Preserve the unfinished draft. */ }
  return <><textarea aria-label="交互组件定义" value={code} disabled={disabled} rows={8} onChange={event => onChange(event.target.value)} />{!valid && <p className="al-error" role="alert">组件 JSON 尚未完整，保存前请修正</p>}</>
}

export function ArtifactEditor({ value, state, api, actorId, sessionId, onClose }: {
  value: ArtifactEditorValue; state: ArtifactLibraryState; api: ArtifactApi; actorId: string; sessionId?: string
  onClose: () => void
}) {
  const [title, setTitle] = useState(value.item?.title || ''), [kind, setKind] = useState<PersonalArtifactKind>(value.item?.kind || 'output')
  const [filename, setFilename] = useState(value.item ? artifactFilename(value.item) : 'document.md')
  const [content, setContent] = useState(value.content), [format, setFormat] = useState<'text' | 'page'>(() => value.item?.suffix.toLowerCase() === '.json' && asPage(value.content) ? 'page' : 'text')
  const [sources, setSources] = useState<PersonalArtifactRef[]>(value.item?.source_refs || []), [preview, setPreview] = useState(false), [error, setError] = useState('')
  const [confirmClose, setConfirmClose] = useState(false)
  const pendingNavigation = useRef<ArtifactNavigation | null>(null), bypassHistoryGuard = useRef(false)
  useEffect(() => () => { pendingNavigation.current?.cancel?.(); pendingNavigation.current = null }, [])
  const page = useMemo(() => format === 'page' ? asPage(content) : null, [content, format])
  const markdownPreview = filename.toLowerCase().endsWith('.md') || value.item?.mime_type.includes('markdown')
  const [widgetDrafts, setWidgetDrafts] = useState<Record<string, string>>({})
  const invalidWidget = Object.entries(widgetDrafts).some(([id, code]) => {
    if (!page?.sections.some(section => section.id === id)) return false
    try { const value = JSON.parse(code); return !value || typeof value !== 'object' || Array.isArray(value) } catch { return true }
  })
  const dirty = title !== (value.item?.title || '') || filename !== (value.item ? artifactFilename(value.item) : 'document.md') || kind !== (value.item?.kind || 'output')
    || content !== value.content || JSON.stringify(sources) !== JSON.stringify(value.item?.source_refs || []) || Object.keys(widgetDrafts).length > 0
  useEffect(() => {
    if (!dirty || state.pending) return
    const stop = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = '' }
    window.addEventListener('beforeunload', stop)
    return () => window.removeEventListener('beforeunload', stop)
  }, [dirty, state.pending])
  useEffect(() => {
    if (!dirty || state.pending) return
    const currentUrl = location.href
    const navigate = (event: Event) => {
      const command = (event as CustomEvent<ArtifactNavigation>).detail
      if (!command || typeof command.proceed !== 'function') return
      event.preventDefault()
      pendingNavigation.current?.cancel?.()
      pendingNavigation.current = command
      setConfirmClose(true)
    }
    const pop = (event: PopStateEvent) => {
      if (bypassHistoryGuard.current) return
      event.stopImmediatePropagation()
      const target = location.href
      history.pushState({}, '', currentUrl)
      requestArtifactNavigation(() => {
        bypassHistoryGuard.current = true
        history.replaceState({}, '', target)
        window.dispatchEvent(new PopStateEvent('popstate', { state: event.state }))
        bypassHistoryGuard.current = false
      })
    }
    window.addEventListener(artifactNavigationEvent, navigate)
    window.addEventListener('popstate', pop, { capture: true })
    return () => { window.removeEventListener(artifactNavigationEvent, navigate); window.removeEventListener('popstate', pop, { capture: true }) }
  }, [dirty, state.pending])
  const save = async (event: FormEvent) => {
    event.preventDefault()
    if (state.locked) return
    if (invalidWidget) { setError('请先修正未完成的组件 JSON'); return }
    if (format === 'page' && !page) { setError('文档结构不完整，请检查章节'); return }
    if (format === 'page' && page?.sections.some(section => !section.title.trim())) { setError('请填写每个章节标题'); return }
    setError('')
    const body = { title: title.trim(), kind, filename, content: format === 'page' ? JSON.stringify({ ...page!, title: title.trim() }) : content, source_refs: sources, ...(sessionId ? { session_id: sessionId } : {}) }
    const result = value.item ? await state.revise(value.item.artifact_id, { ...body, expected_version: value.item.version }) : await state.create(body)
    if (result && 'kind' in result) onClose()
  }
  return <section className="al-editor" aria-label={value.item ? '修订文档' : '新建文档'}>
    <form onSubmit={event => void save(event)}>
      <header><h2><FileText size={18} />{value.item ? `修订 v${value.item.version}` : '新建文档'}</h2><div>
        <button type="button" aria-pressed={preview} onClick={() => setPreview(value => !value)}>{preview ? <PencilIcon /> : <Eye size={15} />}{preview ? '编辑' : '预览'}</button>
        <button type="button" onClick={() => requestArtifactNavigation(onClose)} disabled={state.posting}>关闭</button><button className="al-primary" disabled={!title.trim() || !filename.trim() || state.locked || invalidWidget}>{state.posting ? <Loader2 size={14} className="pa-spin" /> : <Check size={14} />}{state.posting ? '保存中…' : '保存新版本'}</button>
      </div></header>
      {(error || state.error) && <div className="al-error" role="alert">{error || state.error}</div>}
      {confirmClose && <div className="al-confirm" role="group" aria-label="确认关闭未保存文档"><strong>放弃尚未保存的修改？</strong><div><button type="button" disabled={state.posting} onClick={() => { pendingNavigation.current?.cancel?.(); pendingNavigation.current = null; setConfirmClose(false) }}>继续编辑</button><button type="button" disabled={state.posting} onClick={() => { const command = pendingNavigation.current; pendingNavigation.current = null; setConfirmClose(false); (command?.proceed || onClose)() }}>放弃修改</button></div></div>}
      <div className="al-editor-fields"><label>标题<input aria-label="文档标题" value={title} disabled={state.locked} maxLength={120} required onChange={event => { setTitle(event.target.value); if (page) setContent(JSON.stringify({ ...page, title: event.target.value })) }} /></label>
        <label>分类<select aria-label="文档分类" value={kind} disabled={state.locked || Boolean(value.item)} onChange={event => setKind(event.target.value as PersonalArtifactKind)}><option value="output">产物</option><option value="material">材料</option></select></label>
        {!value.item && <label>格式<select aria-label="文档格式" value={format} disabled={state.locked} onChange={event => {
          const next = event.target.value as 'text' | 'page'; setFormat(next)
          if (next === 'page') { setContent(JSON.stringify({ version: 1, type: 'page', title, sections: [{ id: 'section-' + crypto.randomUUID(), title: '正文', markdown: content, evidence_status: 'unknown' }] })); setFilename('document.json') }
          else { setContent(page?.sections.map(section => `## ${section.title}\n\n${section.markdown || ''}`).join('\n\n') || content); setFilename('document.md') }
        }}><option value="text">Markdown / 文本</option><option value="page">Page</option></select></label>}
        <label>文件名<input aria-label="文档文件名" value={filename} disabled={state.locked} maxLength={180} required onChange={event => setFilename(event.target.value)} /></label>
      </div>
      <SourcePicker api={api} actorId={actorId} value={sources} onChange={setSources} excludeId={value.item?.artifact_id} disabled={state.locked} />
      {preview ? <div className="al-editor-preview">{format === 'page' && page ? page.sections.map(section => <section key={section.id}><h2>{section.title}</h2><ArtifactMarkdown content={section.markdown || ''} />{section.widget && <PrivateWidget code={section.widget} />}</section>) : markdownPreview ? <ArtifactMarkdown content={content} /> : <pre className="al-plain-text">{content}</pre>}</div>
        : format === 'page' && page ? <StructuredPageEditor value={page} disabled={state.locked} onChange={next => setContent(JSON.stringify(next))} widgetDrafts={widgetDrafts} onWidgetChange={(id, code) => {
          setWidgetDrafts(value => ({ ...value, [id]: code }))
          try { const widget = JSON.parse(code); if (widget && typeof widget === 'object' && !Array.isArray(widget)) setContent(JSON.stringify({ ...page, sections: page.sections.map(section => section.id === id ? { ...section, widget } : section) })) } catch { /* Keep the invalid draft separate from the last valid document. */ }
        }} />
          : <textarea className="al-editor-body" aria-label="文档正文" value={content} maxLength={2_000_000} disabled={state.locked} onChange={event => setContent(event.target.value)} />}
    </form>
  </section>
}

function PencilIcon() { return <Code2 size={15} /> }
