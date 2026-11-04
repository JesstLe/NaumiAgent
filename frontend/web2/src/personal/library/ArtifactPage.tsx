import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { ArrowLeft, Check, ChevronRight, Download, FileUp, History, Link2, Loader2, Pencil, RefreshCw, Share2, ShieldCheck, X } from 'lucide-react'
import type { PersonalAgent } from '@naumi/shared/api/WorkbenchRuntimeClient'
import type { PersonalArtifactGrant, PersonalArtifactPage, PersonalArtifactPreview, PersonalArtifactRef, PersonalArtifactSnapshot } from '@naumi/shared/api/personalArtifacts'
import { ImagePreview } from '../../rich/MessageContent'
import { parseRichSpec, type RichSpec } from '../../rich/schema'
import { artifactError, blobBuffer, blobSha256, validateArtifact, validateArtifactGrant, validateArtifactRef, type ArtifactApi, type ArtifactLibraryState } from './useArtifactLibrary'

const RichWidget = lazy(() => import('../../rich/RichWidget'))

export type ArtifactSelection = { artifact_id: string; version?: number }
export const actorName = (id: string, agents: PersonalAgent[]) => id === 'main' ? '主 Agent' : agents.find(agent => agent.id === id)?.name || id
export const artifactFilename = (item: PersonalArtifactSnapshot) => {
  const name = item.title.replace(/[\\/:*?"<>|]/g, '_').slice(0, 120), suffix = item.suffix.replace(/^\./, '')
  return suffix && !name.toLowerCase().endsWith('.' + suffix.toLowerCase()) ? `${name}.${suffix}` : name
}

export function validatePreview(value: PersonalArtifactPreview): PersonalArtifactPreview {
  if (!value || typeof value !== 'object') throw new Error('资料预览格式不完整')
  if (value.type === 'text') {
    if (typeof value.content !== 'string' || typeof value.truncated !== 'boolean') throw new Error('文本预览格式不完整')
  } else if (value.type === 'table') {
    parseRichSpec(JSON.stringify({ version: 1, type: 'table', title: '资料预览', columns: value.columns, rows: value.rows }))
    if (!Number.isSafeInteger(value.total_rows) || !Number.isSafeInteger(value.preview_limit) || value.preview_limit < 1
      || value.total_rows < value.rows.length || value.rows.length > value.preview_limit) throw new Error('表格预览范围不完整')
  } else if (value.type === 'page') {
    if (value.version !== 1 || typeof value.title !== 'string' || !Array.isArray(value.sections) || value.sections.length > 40
      || new Set(value.sections.map(section => section.id)).size !== value.sections.length) throw new Error('文档目录格式不完整')
    for (const section of value.sections) {
      if (!section || typeof section.id !== 'string' || !section.id || typeof section.title !== 'string'
        || !['fact', 'estimate', 'unknown'].includes(section.evidence_status)
        || section.markdown !== undefined && typeof section.markdown !== 'string'
        || section.widget !== undefined && (!section.widget || typeof section.widget !== 'object')) throw new Error('文档章节格式不完整')
      section.source_refs?.forEach(validateArtifactRef)
    }
  } else throw new Error('当前资料预览类型不受支持')
  return value
}

export async function verifiedContent(api: ArtifactApi, item: PersonalArtifactSnapshot, actorId: string, signal?: AbortSignal) {
  const value = await api.personalArtifactContent(item.artifact_id, actorId, item.version, signal)
  if (value.sha256 !== item.sha256 || value.blob.size !== item.bytes || await blobSha256(value.blob) !== item.sha256)
    throw new Error('文件内容与所选版本不一致，请重新加载原版本')
  return value.blob
}

function privateUrl(value: string) {
  if (value.startsWith('#')) return value
  try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : '' }
  catch { return '' }
}
export function ArtifactMarkdown({ content }: { content: string }) {
  return <div className="rich-content al-markdown"><Markdown remarkPlugins={[remarkGfm]} skipHtml urlTransform={privateUrl}
    components={{ img: ({ alt }) => <span className="al-image-reference">{alt || '图片引用'}</span>,
      a: ({ href, children }) => href ? <a href={href} target={href.startsWith('#') ? undefined : '_blank'} rel="noopener noreferrer">{children}</a> : <span>{children}</span>,
      table: ({ children }) => <div className="rich-table-scroll"><table>{children}</table></div> }}>{content}</Markdown></div>
}

function privateWidget(code: Record<string, unknown>) {
  const parsed = parseRichSpec(JSON.stringify(code))
  const inspect = (value: RichSpec) => {
    if (value.type === 'image') throw new Error('图片引用未绑定资料版本，请从资料卡打开原图片')
    if (value.type === 'tabs') value.tabs.forEach(tab => inspect(tab.content))
  }
  inspect(parsed)
  return JSON.stringify(code)
}
export function PrivateWidget({ code }: { code: Record<string, unknown> }) {
  const value = useMemo(() => { try { return { code: privateWidget(code), error: '' } } catch (cause) { return { code: '', error: artifactError(cause) } } }, [code])
  return value.error ? <div role="alert" className="al-error">{value.error}</div> : <Suspense fallback={<p role="status">正在加载组件…</p>}><RichWidget code={value.code} /></Suspense>
}

function SourceCard({ source, api, actorId, onOpen }: {
  source: PersonalArtifactRef; api: ArtifactApi; actorId: string; onOpen: (selection: ArtifactSelection) => void
}) {
  const [title, setTitle] = useState('来源版本'), [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    void api.personalArtifact(source.artifact_id, actorId, source.version, controller.signal).then(value => {
      validateArtifact(value, { id: source.artifact_id, version: source.version })
      if (value.sha256 !== source.sha256) throw new Error('来源摘要与原引用不一致')
      if (!controller.signal.aborted) { setTitle(value.title); setError('') }
    }).catch(cause => { if (!controller.signal.aborted) setError(artifactError(cause)) })
    return () => controller.abort()
  }, [api, actorId, source.artifact_id, source.version, source.sha256])
  return <button className="al-source-card" type="button" onClick={() => onOpen(source)}>
    <Link2 size={14} /><span><strong>{error ? '来源访问待核对' : title}</strong><code>v{source.version} · {source.sha256.slice(0, 12)}</code></span><ChevronRight size={14} />
  </button>
}

function DocumentPage({ page, api, actorId, onOpen }: {
  page: PersonalArtifactPage; api: ArtifactApi; actorId: string; onOpen: (selection: ArtifactSelection) => void
}) {
  const sectionRefs = useRef<(HTMLElement | null)[]>([])
  return <div className="al-document-grid">
    {!!page.sections.length && <nav className="al-outline" aria-label="文档目录">{page.sections.map((section, index) => <button key={section.id} onClick={() => sectionRefs.current[index]?.scrollIntoView({ behavior: 'smooth', block: 'start' })}>{section.title}</button>)}</nav>}
    <div className="al-document-sections">{page.sections.map((section, index) => <section key={section.id} ref={element => { sectionRefs.current[index] = element }} className="al-document-section" aria-label={section.title}>
      <header><h2>{section.title}</h2><span className={`al-evidence al-evidence-${section.evidence_status}`} title="作者标注，不代表独立核验">{{ fact: '事实', estimate: '估算', unknown: '未知' }[section.evidence_status]}</span></header>
      {section.markdown !== undefined && <ArtifactMarkdown content={section.markdown} />}
      {section.widget && <PrivateWidget code={section.widget} />}
      {!!section.source_refs?.length && <div className="al-source-list" aria-label={`${section.title}的来源`}>{section.source_refs.map((source, n) => <SourceCard key={`${source.artifact_id}:${source.version}:${n}`} source={source} api={api} actorId={actorId} onOpen={onOpen} />)}</div>}
    </section>)}</div>
  </div>
}

function Sharing({ item, api, actorId, agents, state, refreshToken, onClose }: {
  item: PersonalArtifactSnapshot; api: ArtifactApi; actorId: string; agents: PersonalAgent[]
  state: ArtifactLibraryState; refreshToken: number; onClose: () => void
}) {
  const [grants, setGrants] = useState<PersonalArtifactGrant[]>([]), [recipient, setRecipient] = useState('')
  const [loading, setLoading] = useState(true), [error, setError] = useState(''), [revision, setRevision] = useState(0)
  const [confirm, setConfirm] = useState<PersonalArtifactGrant | null>(null)
  const recipients = [{ id: 'main', name: '主 Agent', status: 'active' }, ...agents].filter(agent => agent.id !== item.owner_id && agent.status === 'active')
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true); setError('')
    void api.personalArtifactGrants(item.artifact_id, item.version, actorId, controller.signal).then(result => {
      if (!Array.isArray(result.grants)) throw new Error('分享清单返回不完整，请重试')
      const values = result.grants.map(grant => validateArtifactGrant(grant, { artifactId: item.artifact_id, version: item.version, owner: item.owner_id }))
      if (!controller.signal.aborted) setGrants(values)
    }).catch(cause => { if (!controller.signal.aborted) setError(artifactError(cause)) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [api, actorId, item.artifact_id, item.version, item.owner_id, refreshToken, revision])
  return <section className="al-sharing" aria-label="版本分享">
    <header><h2><Share2 size={17} />分享 v{item.version}</h2><button aria-label="关闭版本分享" onClick={onClose}><X size={17} /></button></header>
    <form onSubmit={async event => { event.preventDefault(); if (!recipient || !recipients.some(value => value.id === recipient)) return; const result = await state.share(item.artifact_id, item.version, recipient); if (result) { setRecipient(''); setRevision(value => value + 1) } }}>
      <select aria-label="分享给" value={recipient} disabled={state.locked} onChange={event => setRecipient(event.target.value)}><option value="">选择 Agent 或团队</option>{recipients.map(agent => <option key={agent.id} value={agent.id}>{agent.name}</option>)}</select>
      <button className="al-primary" disabled={!recipient || state.locked}>分享此版本</button>
    </form>
    {loading && <p role="status"><Loader2 className="pa-spin" size={14} />正在加载分享…</p>}
    {error && <div className="al-error" role="alert">{error}<button onClick={() => setRevision(value => value + 1)}>重试</button></div>}
    {!loading && !error && !grants.length && <p className="al-empty-small">尚未分享</p>}
    {grants.map(grant => <div className="al-grant" key={grant.id}><span><strong>{actorName(grant.recipient_id, agents)}</strong><small>{grant.status === 'active' ? '可访问此版本' : '已撤销'}</small></span>
      {grant.status === 'active' && <button disabled={state.locked} onClick={() => setConfirm(grant)}>撤销</button>}
    </div>)}
    {confirm && <div role="group" aria-label="确认撤销分享" className="al-confirm"><strong>撤销对{actorName(confirm.recipient_id, agents)}的 v{confirm.version} 分享？</strong><div><button disabled={state.posting} onClick={() => setConfirm(null)}>保留分享</button><button disabled={state.locked} onClick={async () => { const result = await state.revoke(confirm); if (result) { setConfirm(null); setRevision(value => value + 1) } }}>确认撤销</button></div></div>}
  </section>
}

export function ArtifactPage({ api, actorId, selection, agents, state, refreshToken, sessionId, onOpen, onBack, onEdit }: {
  api: ArtifactApi; actorId: string; selection: ArtifactSelection; agents: PersonalAgent[]; state: ArtifactLibraryState
  refreshToken: number; sessionId?: string; onOpen: (selection: ArtifactSelection) => void; onBack: () => void
  onEdit: (item: PersonalArtifactSnapshot, content: string) => void
}) {
  const [item, setItem] = useState<PersonalArtifactSnapshot | null>(null), [versions, setVersions] = useState<PersonalArtifactSnapshot[]>([])
  const [preview, setPreview] = useState<PersonalArtifactPreview | null>(null), [blobUrl, setBlobUrl] = useState('')
  const [loading, setLoading] = useState(true), [error, setError] = useState(''), [revision, setRevision] = useState(0)
  const [action, setAction] = useState(''), [actionError, setActionError] = useState(''), [sharing, setSharing] = useState(false), [copied, setCopied] = useState(false)
  const upload = useRef<HTMLInputElement>(null), alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  useEffect(() => {
    const controller = new AbortController()
    let url = ''
    setLoading(true); setError(''); setItem(null); setPreview(null); setBlobUrl(''); setVersions([])
    void (async () => {
      const value = validateArtifact(await api.personalArtifact(selection.artifact_id, actorId, selection.version, controller.signal), { id: selection.artifact_id, version: selection.version })
      const history = await api.personalArtifactVersions(value.artifact_id, actorId, controller.signal)
      if (!Array.isArray(history.versions)) throw new Error('版本记录返回不完整，请重试')
      const visible = history.versions.map(version => validateArtifact(version, { id: value.artifact_id }))
      if (!visible.some(version => version.version === value.version && version.sha256 === value.sha256)) throw new Error('当前版本不在可访问的版本记录中')
      if (value.mime_type.startsWith('image/') || value.mime_type === 'application/pdf') {
        const blob = await verifiedContent(api, value, actorId, controller.signal)
        if (controller.signal.aborted) return
        url = URL.createObjectURL(blob); setBlobUrl(url)
      } else {
        const view = validatePreview(await api.personalArtifactPreview(value.artifact_id, actorId, value.version, controller.signal))
        if (controller.signal.aborted) return
        setPreview(view)
      }
      if (!controller.signal.aborted) { setItem(value); setVersions(visible.sort((a, b) => b.version - a.version)) }
    })().catch(cause => { if (!controller.signal.aborted) setError(artifactError(cause)) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => { controller.abort(); if (url) URL.revokeObjectURL(url) }
  }, [api, actorId, selection.artifact_id, selection.version, revision, refreshToken])
  useEffect(() => { const focus = () => setRevision(value => value + 1); window.addEventListener('focus', focus); return () => window.removeEventListener('focus', focus) }, [])

  const runAction = async (kind: string, callback: () => Promise<void>) => {
    if (action || !item) return
    setAction(kind); setActionError('')
    try { await callback() } catch (cause) { if (alive.current) setActionError(artifactError(cause)) }
    finally { if (alive.current) setAction('') }
  }
  const download = () => runAction('download', async () => {
    const blob = await verifiedContent(api, item!, actorId)
    if (!alive.current) return
    const url = URL.createObjectURL(blob), link = document.createElement('a')
    link.href = url; link.download = artifactFilename(item!); link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000)
  })
  const edit = () => runAction('edit', async () => {
    const blob = await verifiedContent(api, item!, actorId)
    if (blob.size > 2_000_000) throw new Error('文件较大，请使用“上传新版本”修订完整文件')
    const content = new TextDecoder('utf-8', { fatal: true }).decode(await blobBuffer(blob))
    if (alive.current) onEdit(item!, content)
  })
  const latest = item && item.version === Math.max(...versions.map(value => value.version))
  const editable = Boolean(item?.can_revise && latest && !state.locked)
  return <section className="al-page" aria-label="资料文档">
    <div className="al-page-toolbar"><button onClick={onBack}><ArrowLeft size={16} />资料库</button><div>
      <button aria-label="刷新资料版本" disabled={loading} onClick={() => setRevision(value => value + 1)}><RefreshCw size={15} /></button>
      {item && <><button aria-label="下载原文件" disabled={Boolean(action)} onClick={() => void download()}><Download size={15} />{action === 'download' ? '下载中…' : '下载'}</button>
        {item.can_revise && <button disabled={!editable || Boolean(action) || !preview} onClick={() => void edit()}><Pencil size={15} />{action === 'edit' ? '读取中…' : '编辑'}</button>}
        {item.can_revise && <button disabled={!editable || Boolean(action)} onClick={() => upload.current?.click()}><FileUp size={15} />上传新版本</button>}
        {item.can_share && <button aria-pressed={sharing} onClick={() => setSharing(value => !value)}><Share2 size={15} />分享</button>}
        <button aria-label="复制此版本链接" onClick={() => { const url = new URL(location.href); url.searchParams.set('artifact', item.artifact_id); url.searchParams.set('version', String(item.version)); url.searchParams.set('actor', actorId); void navigator.clipboard.writeText(url.href).then(() => setCopied(true)).catch(() => setActionError('复制失败，请从地址栏复制当前链接')) }}>{copied ? <Check size={15} /> : <Link2 size={15} />}</button>
      </>}
    </div></div>
    {loading && <div className="al-loading" role="status"><Loader2 className="pa-spin" />正在加载资料…</div>}
    {error && <div className="al-error" role="alert">{error}<button onClick={() => setRevision(value => value + 1)}>重新加载</button></div>}
    {actionError && <div className="al-error" role="alert">{actionError}</div>}
    {item && <div className="al-page-body">
      <header className="al-document-title"><div><span className="al-kind">{item.kind === 'material' ? '材料' : '产物'}</span><h1>{item.title}</h1></div>
        <div className="al-page-metadata"><span>{actorName(item.owner_id, agents)}</span><label><History size={13} /><select aria-label="资料版本" value={item.version} onChange={event => onOpen({ artifact_id: item.artifact_id, version: Number(event.target.value) })}>{versions.map(version => <option key={version.version} value={version.version}>v{version.version}</option>)}</select></label><time>{new Date(item.created_at).toLocaleString('zh-CN')}</time><span>{item.bytes.toLocaleString()} B</span></div>
      </header>
      {sharing && item.can_share && <Sharing item={item} api={api} actorId={actorId} agents={agents} state={state} refreshToken={refreshToken} onClose={() => setSharing(false)} />}
      {blobUrl && item.mime_type.startsWith('image/') && <div className="al-image-preview"><ImagePreview src={blobUrl} alt={item.title} /></div>}
      {blobUrl && item.mime_type === 'application/pdf' && <iframe title={item.title} className="al-pdf-preview" sandbox="" referrerPolicy="no-referrer" src={blobUrl} />}
      {preview?.type === 'page' && <DocumentPage page={preview} api={api} actorId={actorId} onOpen={onOpen} />}
      {preview?.type === 'table' && <>
        {preview.rows.length < preview.total_rows && <div className="al-preview-boundary" role="status">预览 {preview.rows.length.toLocaleString()} / {preview.total_rows.toLocaleString()} 行；下方导出仅含当前预览筛选结果，完整文件可从顶部下载。</div>}
        <Suspense fallback={<p role="status">正在加载表格…</p>}><RichWidget code={JSON.stringify({ version: 1, type: 'table', title: item.title, columns: preview.columns, rows: preview.rows })} /></Suspense>
      </>}
      {preview?.type === 'text' && <>{preview.truncated && <div className="al-preview-boundary" role="status">预览已截取，下载可获得完整原文件。</div>}{item.mime_type.includes('markdown') || item.suffix.replace(/^\./, '').toLowerCase() === 'md' ? <ArtifactMarkdown content={preview.content} /> : <pre className="al-plain-text">{preview.content}</pre>}</>}
      {!!item.source_refs.length && <section className="al-original-sources" aria-label="资料来源"><h2><Link2 size={15} />来源</h2><div className="al-source-list">{item.source_refs.map((source, index) => <SourceCard key={`${source.artifact_id}:${source.version}:${index}`} source={source} api={api} actorId={actorId} onOpen={onOpen} />)}</div></section>}
      <details className="al-version-identity"><summary><ShieldCheck size={13} />版本记录</summary><dl><dt>资料 ID</dt><dd>{item.artifact_id}</dd><dt>SHA-256</dt><dd>{item.sha256}</dd>{Object.keys(item.evidence).length > 0 && <><dt>来源记录</dt><dd><pre>{JSON.stringify(item.evidence, null, 2)}</pre></dd></>}</dl></details>
    </div>}
    <input ref={upload} className="al-hidden" type="file" aria-label="选择修订文件" onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; if (file && item) void state.importVersion(item, file, sessionId) }} />
  </section>
}
