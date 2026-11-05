import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowLeft, BookOpen, ChevronDown, ChevronRight, ClipboardCheck, ExternalLink, FileDown, FileText, History, Link2, Loader2, Plus, Quote, RefreshCw, Save, Search, ShieldAlert, Trash2, X } from 'lucide-react'
import { artifactNavigationEvent, requestArtifactNavigation, type ArtifactNavigation } from '../library/navigation'
import { cloneResearch, parseResearchPending, researchDomains, researchDraft, researchError, researchKinds, researchPendingKey, researchRequestId, researchReviewNames, researchStances, validateResearchDraft, validateResearchView } from './types'
import type { ResearchApi, ResearchCalculation, ResearchClaim, ResearchContext, ResearchDiscovery, ResearchDraft, ResearchHistory, ResearchOperation, ResearchPending, ResearchResult, ResearchSource, ResearchSourceView, ResearchSummary, ResearchView } from './types'
import './research.css'

const lines = (value: string) => value.split('\n').map(text => text.trim()).filter(Boolean)
const time = (value: string | undefined) => {
  try { return value ? new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(value)) : '—' } catch { return '时间待核对' }
}
const newClaim = (): ResearchClaim => ({ id: `c_${researchRequestId().slice(0, 10)}`, text: '', subject: '', kind: 'fact', citations: [], quantity: null, calculation: null, assumptions: [], upstream_claims: [] })
const operationNames: Record<ResearchOperation, string> = { create: '新建研究', revise: '保存修订', search: '检索来源', capture: '抓取来源', import: '引用资料', recheck: '复查来源', review: '机械检查', publish: '导出报告' }
const calculationNames: Record<ResearchCalculation['operation'], string> = { ratio: '比值', percent: '百分比', sum: '合计', difference: '差值' }
const isDiscovery = (value: ResearchResult): value is ResearchDiscovery => 'discovery' in value
const safeUrl = (value: string | null | undefined) => {
  try { const url = new URL(value || ''); return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : null } catch { return null }
}
const statusCode = (cause: unknown) => cause && typeof cause === 'object' && 'status' in cause ? Number(cause.status) : 0
const definitiveFailure = (cause: unknown) => {
  if (cause && typeof cause === 'object' && 'accepted' in cause && cause.accepted === true) return false
  const status = statusCode(cause)
  return status >= 400 && status < 500 && ![408, 425, 429].includes(status)
}

export function ResearchWorkspace({ api, namespace, connected, onClose, onOpenArtifact }: {
  api: ResearchApi; namespace: string; connected: boolean; onClose: () => void
  onOpenArtifact?: (id: string, version: number) => void
}) {
  const [context, setContext] = useState<ResearchContext | null>(null)
  const [scopeKey, setScopeKey] = useState(''), [pendingScope, setPendingScope] = useState('')
  const [notebooks, setNotebooks] = useState<ResearchSummary[]>([]), [nextAfter, setNextAfter] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState(''), [view, setView] = useState<ResearchView | null>(null)
  const [draft, setDraft] = useState<ResearchDraft | null>(null), [baseRevision, setBaseRevision] = useState(0)
  const [loading, setLoading] = useState(false), [loadingDetail, setLoadingDetail] = useState(false), [posting, setPosting] = useState(false)
  const [notice, setNotice] = useState(''), [error, setError] = useState(''), [pending, setPending] = useState<ResearchPending | null>(null)
  const [newOpen, setNewOpen] = useState(false), [newDraft, setNewDraft] = useState(researchDraft())
  const [pane, setPane] = useState<'sources' | 'review'>('sources'), [activeClaim, setActiveClaim] = useState('')
  const [source, setSource] = useState<ResearchSourceView | null>(null), [sourceLoading, setSourceLoading] = useState(false)
  const [quote, setQuote] = useState(''), [occurrence, setOccurrence] = useState(0), [sourceForm, setSourceForm] = useState(false)
  const [sourceMode, setSourceMode] = useState<'url' | 'artifact'>('url'), [sourceUrl, setSourceUrl] = useState('')
  const [ageDays, setAgeDays] = useState(30), [query, setQuery] = useState(''), [discovery, setDiscovery] = useState<ResearchDiscovery['discovery'] | null>(null)
  const [artifact, setArtifact] = useState({ artifact_id: '', version: 1, sha256: '', grant_id: '', grant_revision: 0 })
  const [historyOpen, setHistoryOpen] = useState(false), [history, setHistory] = useState<ResearchHistory | null>(null)
  const [historical, setHistorical] = useState<ResearchView | null>(null), [historyLoading, setHistoryLoading] = useState(false)
  const [leave, setLeave] = useState<ArtifactNavigation | null>(null)
  const [listQuery, setListQuery] = useState('')
  const mounted = useRef(true), activeKey = useRef(''), epoch = useRef(0), selected = useRef(''), readControllers = useRef(new Set<AbortController>())
  const detailSequence = useRef(0), sourceSequence = useRef(0), historySequence = useRef(0), moreLock = useRef(false)
  const writeLock = useRef(false), pendingRef = useRef<ResearchPending | null>(null)
  const frozen = useRef<{ key: string; operation: ResearchOperation; payload: Record<string, unknown>; saved: ResearchPending } | null>(null)
  const draftRef = useRef<ResearchDraft | null>(null), draftBase = useRef(0), currentView = useRef<ResearchView | null>(null), currentContext = useRef<ResearchContext | null>(null)
  const recovery = useRef(new Map<string, { id: string; draft: ResearchDraft; revision: number }>())
  const bodyElement = useRef<HTMLPreElement>(null)
  const key = researchPendingKey(namespace)
  activeKey.current = key; selected.current = selectedId; draftRef.current = draft; draftBase.current = baseRevision; currentView.current = view; currentContext.current = context
  const connection = useRef(connected); connection.current = connected
  const dirty = Boolean(draft && view && JSON.stringify(draft) !== JSON.stringify(researchDraft(view.notebook))) || newOpen && Boolean(newDraft.title || newDraft.question)
  const dirtyRef = useRef(dirty); dirtyRef.current = dirty
  const locked = posting || Boolean(pending) || !connected || !context
  const scopeReady = scopeKey === key && connected
  const displayed = scopeReady ? historical || view : null
  const editable = Boolean(scopeReady && draft && view && !historical)
  const filtered = useMemo(() => scopeReady ? notebooks.filter(item => !listQuery.trim() || `${item.title}\n${item.question}`.toLowerCase().includes(listQuery.trim().toLowerCase())) : [], [notebooks, listQuery, scopeReady])

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; for (const controller of readControllers.current) controller.abort(); readControllers.current.clear() } }, [])
  useEffect(() => {
    const guard = (event: Event) => { if (dirtyRef.current) { event.preventDefault(); setLeave((event as CustomEvent<ArtifactNavigation>).detail) } }
    const unload = (event: BeforeUnloadEvent) => { if (dirtyRef.current) { event.preventDefault(); event.returnValue = '' } }
    window.addEventListener(artifactNavigationEvent, guard); window.addEventListener('beforeunload', unload)
    return () => { window.removeEventListener(artifactNavigationEvent, guard); window.removeEventListener('beforeunload', unload) }
  }, [])

  const controller = () => { const value = new AbortController(); readControllers.current.add(value); return value }
  const valid = (captured: string, generation: number, signal?: AbortSignal) => mounted.current && activeKey.current === captured && epoch.current === generation && connection.current && !signal?.aborted
  const setPendingValue = (value: ResearchPending | null) => { pendingRef.current = value; setPending(value); setPendingScope(activeKey.current) }
  const clearPending = (saved: ResearchPending, capturedKey: string) => {
    const raw = localStorage.getItem(capturedKey)
    if (!raw || parseResearchPending(raw).request_id !== saved.request_id) throw new Error('原研究请求编号已变化')
    localStorage.removeItem(capturedKey)
    if (activeKey.current === capturedKey) setPendingValue(null)
    if (frozen.current?.saved.request_id === saved.request_id) frozen.current = null
  }
  const applyView = (next: ResearchView, preserveDraft = false) => {
    validateResearchView(next)
    const scope = currentContext.current
    if (scope && (next.notebook.workspace_key && next.notebook.workspace_key !== scope.workspace_key || next.notebook.actor_id && next.notebook.actor_id !== scope.actor_id)) throw new Error('研究记录与当前身份不一致')
    if (next.availability === 'source_unavailable') {
      const opaque: ResearchView = { ...next, notebook: { ...next.notebook, title: '来源不可用', question: '', claims: [], conclusion: { stance: 'undecided', premises: [], invalidators: [] } }, sources: [], publication: null,
        review: { status: 'needs_sources', semantic_verification: 'not_performed', findings: [], anchors: [] } }
      setSelectedId(next.notebook.id); selected.current = next.notebook.id; setView(opaque); currentView.current = opaque
      setDraft(null); draftRef.current = null; recovery.current.delete(key); setSource(null); setQuote('')
      setHistorical(null); setHistory(null); setHistoryOpen(false); setDiscovery(null); setSourceForm(false)
      setQuery(''); setSourceUrl(''); setArtifact({ artifact_id: '', version: 1, sha256: '', grant_id: '', grant_revision: 0 })
      setNewOpen(false); setNewDraft(researchDraft()); setActiveClaim('')
      if (frozen.current?.key === key) frozen.current = null
      detailSequence.current += 1; sourceSequence.current += 1; historySequence.current += 1
      for (const read of readControllers.current) read.abort(); readControllers.current.clear()
      setLoadingDetail(false); setSourceLoading(false); setHistoryLoading(false)
      setNotebooks(previous => previous.map(item => item.id === next.notebook.id ? { ...item, title: '来源不可用', question: '', claim_count: 0, source_count: 0, review_status: 'needs_sources' } : item))
      return false
    }
    const previousId = selected.current
    setSelectedId(next.notebook.id); selected.current = next.notebook.id; setView(next); currentView.current = next
    setHistorical(null)
    if (!preserveDraft || !draftRef.current || next.notebook.id !== previousId) {
      const updated = researchDraft(next.notebook); setDraft(updated); draftRef.current = updated
      setBaseRevision(next.notebook.revision); draftBase.current = next.notebook.revision
      recovery.current.delete(key)
    }
    setNotebooks(previous => {
      const row: ResearchSummary = { id: next.notebook.id, revision: next.notebook.revision, title: next.notebook.title, question: next.notebook.question, domain: next.notebook.domain,
        updated_at: next.notebook.updated_at || '', source_count: next.sources.length, claim_count: next.notebook.claims.length, review_status: next.review.status }
      return [row, ...previous.filter(item => item.id !== row.id)]
    })
    return true
  }
  const loadNotebook = async (id: string, revision?: number, preserveDraft = false) => {
    const captured = activeKey.current, generation = epoch.current, sequence = ++detailSequence.current, read = controller()
    setLoadingDetail(true); setError(''); setSource(null); setQuote(''); setHistorical(null)
    if (revision !== undefined) { setSourceForm(false); setDiscovery(null) }
    try {
      const next = validateResearchView(await api.getPersonalResearch(id, revision, read.signal), id)
      if (!valid(captured, generation, read.signal) || sequence !== detailSequence.current) return
      const scope = currentContext.current
      if (scope && (next.notebook.workspace_key && next.notebook.workspace_key !== scope.workspace_key || next.notebook.actor_id && next.notebook.actor_id !== scope.actor_id)) throw new Error('研究记录与当前身份不一致')
      if (next.availability === 'source_unavailable') { applyView(next); return }
      if (revision !== undefined) { setHistorical(next); return }
      const saved = recovery.current.get(captured)
      if (!applyView(next, preserveDraft)) return
      if (!preserveDraft && saved?.id === id) {
        setDraft(saved.draft); draftRef.current = saved.draft; setBaseRevision(saved.revision); draftBase.current = saved.revision
        if (saved.revision !== next.notebook.revision) setNotice('原版本已变化，当前草稿保留')
      }
    } catch (cause) { if (valid(captured, generation, read.signal) && sequence === detailSequence.current) { setView(null); setSource(null); setError(researchError(cause)) } }
    finally { readControllers.current.delete(read); if (valid(captured, generation, read.signal) && sequence === detailSequence.current) setLoadingDetail(false) }
  }
  const loadList = async (append = false) => {
    if (!connection.current || moreLock.current) return
    moreLock.current = true; setLoading(true)
    const captured = activeKey.current, generation = epoch.current, read = controller()
    try {
      const [scope, page] = await Promise.all([api.getPersonalResearchContext(read.signal), api.listPersonalResearch({ limit: 50, ...(append && nextAfter ? { after: nextAfter } : {}) }, read.signal)])
      if (!valid(captured, generation, read.signal)) return
      if (!scope || !/^[a-f0-9]{64}$/.test(scope.workspace_key) || typeof scope.actor_id !== 'string' || !Array.isArray(page.notebooks)
          || page.notebooks.some(item => !/^rq_[a-f0-9]{32}$/.test(item.id) || !Number.isSafeInteger(item.revision) || item.revision < 1)) throw new Error('研究目录身份待核对')
      setContext(scope); currentContext.current = scope; setScopeKey(captured); setNotebooks(previous => append ? [...previous, ...page.notebooks.filter(item => !previous.some(old => old.id === item.id))] : page.notebooks); setNextAfter(page.next_after)
      if (!append && !selected.current && page.notebooks.length) {
        const saved = recovery.current.get(captured)
        const id = saved && page.notebooks.some(item => item.id === saved.id) ? saved.id : page.notebooks[0].id
        setSelectedId(id); selected.current = id; void loadNotebook(id)
      }
    } catch (cause) { if (valid(captured, generation, read.signal)) { setContext(null); setNotebooks([]); setView(null); setSource(null); setError(researchError(cause)) } }
    finally { readControllers.current.delete(read); moreLock.current = false; if (valid(captured, generation, read.signal)) setLoading(false) }
  }

  useEffect(() => {
    epoch.current += 1; for (const read of readControllers.current) read.abort(); readControllers.current.clear(); moreLock.current = false
    detailSequence.current += 1; sourceSequence.current += 1; historySequence.current += 1
    setContext(null); setScopeKey(''); setNotebooks([]); setNextAfter(null); setView(null); setDraft(null); setBaseRevision(0); setSelectedId(''); selected.current = ''
    setSource(null); setQuote(''); setHistory(null); setHistorical(null); setHistoryOpen(false); setDiscovery(null); setSourceForm(false); setNewOpen(false); setNotice(''); setError(''); setActiveClaim(''); setLeave(null)
    setSourceLoading(false); setHistoryLoading(false); setLoadingDetail(false); setLoading(false)
    setQuery(''); setSourceUrl(''); setListQuery(''); setArtifact({ artifact_id: '', version: 1, sha256: '', grant_id: '', grant_revision: 0 })
    try { const raw = localStorage.getItem(key); setPendingValue(raw ? parseResearchPending(raw) : null) } catch (cause) { setPendingValue(null); setError(researchError(cause)) }
    if (connected) void loadList()
    return () => {
      if (dirtyRef.current && selected.current && draftRef.current) recovery.current.set(key, { id: selected.current, draft: cloneResearch(draftRef.current), revision: draftBase.current })
      for (const read of readControllers.current) read.abort(); readControllers.current.clear()
    }
  }, [api, namespace, connected])

  const acceptResult = (result: ResearchResult, saved: ResearchPending) => {
    const next = isDiscovery(result) ? result.view : result
    validateResearchView(next, saved.target || undefined)
    const keepDraft = ['capture', 'import', 'recheck', 'search'].includes(saved.operation) && dirtyRef.current
    const available = applyView(next, keepDraft)
    if (isDiscovery(result) && available) setDiscovery(result.discovery)
    setNotice(!available ? '操作已受理，原来源暂待核对' : saved.operation === 'review' ? '机械检查已完成' : saved.operation === 'publish' ? '报告已保存' : '已保存')
    if (saved.operation === 'create') { setNewOpen(false); setNewDraft(researchDraft()) }
    if (['capture', 'import'].includes(saved.operation)) { setSourceForm(false); setSourceUrl('') }
  }
  const perform = async (operation: ResearchOperation, fields: Record<string, unknown>, target: string | null, retry = false) => {
    if (writeLock.current || !connected || !context || pendingRef.current && !retry) return false
    const captured = activeKey.current
    const saved = retry && frozen.current?.key === captured ? frozen.current.saved : { version: 1 as const, request_id: researchRequestId(), operation, target }
    const payload = retry && frozen.current?.key === captured ? frozen.current.payload : cloneResearch({ ...fields, request_id: saved.request_id })
    if (retry && (pendingRef.current?.request_id !== saved.request_id || frozen.current?.key !== captured)) return false
    writeLock.current = true; setPosting(true); setError(''); setNotice('')
    let sent = false
    try {
      if (new TextEncoder().encode(JSON.stringify(payload)).byteLength > 256 * 1024) throw new Error('研究请求超过容量上限')
      if (!retry) {
        if (localStorage.getItem(captured)) throw new Error('请先核对原研究请求')
        parseResearchPending(JSON.stringify(saved))
        try { localStorage.setItem(captured, JSON.stringify(saved)) } catch { throw new Error('请求状态保存失败，请重试') }
        setPendingValue(saved)
        frozen.current = { key: captured, operation, payload: cloneResearch(payload), saved }
      }
      sent = true
      const result = await api.executePersonalResearch(operation, cloneResearch(payload))
      if (!mounted.current || activeKey.current !== captured || !connection.current) return false
      acceptResult(result, saved); clearPending(saved, captured); return true
    } catch (cause) {
      if (!mounted.current || activeKey.current !== captured || !connection.current) return false
      if (sent && definitiveFailure(cause)) {
        clearPending(saved, captured)
        if (statusCode(cause) === 409 && target) void loadNotebook(target, undefined, true)
        else if ([401, 403, 404].includes(statusCode(cause))) { setView(null); setSource(null); setQuote('') }
      }
      setError(researchError(cause)); return false
    } finally { writeLock.current = false; if (mounted.current) setPosting(false) }
  }
  const reconcile = async () => {
    const saved = pendingRef.current
    if (!saved || !api.getPersonalResearchRequest || writeLock.current || !connected) return
    const captured = activeKey.current, generation = epoch.current, read = controller()
    setPosting(true); writeLock.current = true; setError('')
    try {
      const receipt = await api.getPersonalResearchRequest(saved.request_id, read.signal)
      if (!valid(captured, generation, read.signal)) return
      if (receipt.request_id !== saved.request_id || receipt.operation && receipt.operation !== saved.operation) throw new Error('原研究请求回执不一致')
      if (receipt.status !== 'completed' || !receipt.view) { setNotice('原请求结果待核对，未重新提交'); return }
      acceptResult(receipt.view, saved); clearPending(saved, captured)
    } catch (cause) { if (valid(captured, generation, read.signal)) setError(researchError(cause)) }
    finally { readControllers.current.delete(read); writeLock.current = false; if (mounted.current) setPosting(false) }
  }
  const saveDraft = async () => {
    if (!draft || !view || historical) return
    try { validateResearchDraft(draft); await perform('revise', { id: view.notebook.id, expected_revision: baseRevision, ...cloneResearch(draft) }, view.notebook.id) }
    catch (cause) { setError(researchError(cause)) }
  }
  const changeClaim = (id: string, update: Partial<ResearchClaim>) => setDraft(old => old ? { ...old, claims: old.claims.map(item => item.id === id ? { ...item, ...update } : item) } : old)
  const removeClaim = (id: string) => { setDraft(old => old ? { ...old, claims: old.claims.filter(item => item.id !== id) } : old); if (activeClaim === id) setActiveClaim('') }
  const openSource = async (item: ResearchSource) => {
    const captured = activeKey.current, generation = epoch.current, sequence = ++sourceSequence.current, read = controller()
    setSource(null); setQuote(''); setOccurrence(0); setSourceLoading(true); setPane('sources'); setError('')
    try {
      const next = await api.getPersonalResearchSource(item.id, item.revision, read.signal)
      if (!valid(captured, generation, read.signal) || sequence !== sourceSequence.current) return
      if (!next?.source || next.source.id !== item.id || next.source.revision !== item.revision) throw new Error('原来源版本待核对')
      if (next.availability === 'available' && next.source.status === 'available' && typeof next.body !== 'string') throw new Error('原来源正文格式待核对')
      setSource({ ...next, body: next.availability === 'available' && next.source.status === 'available' ? next.body : null })
      if (next.availability === 'source_unavailable') {
        if (currentView.current) applyView({ ...currentView.current, availability: 'source_unavailable' })
        setSource({ ...next, source: { ...next.source, title: '来源不可用', url: null }, body: null })
        setDraft(null); draftRef.current = null; recovery.current.delete(captured)
      }
    } catch (cause) { if (valid(captured, generation, read.signal) && sequence === sourceSequence.current) { setSource(null); setError(researchError(cause)); if ([401, 403, 404].includes(statusCode(cause))) { if (currentView.current) applyView({ ...currentView.current, availability: 'source_unavailable' }); setDraft(null); draftRef.current = null; recovery.current.delete(captured) } } }
    finally { readControllers.current.delete(read); if (valid(captured, generation, read.signal) && sequence === sourceSequence.current) setSourceLoading(false) }
  }
  const selectQuote = () => {
    const selection = window.getSelection()
    if (!selection || !selection.rangeCount || !bodyElement.current || !selection.anchorNode || !selection.focusNode
        || !bodyElement.current.contains(selection.anchorNode) || !bodyElement.current.contains(selection.focusNode)) return
    const value = selection.toString()
    if (value.trim() && value.length <= 4000) { setQuote(value); setOccurrence(0) }
  }
  const addCitation = () => {
    if (!source?.body || source.availability !== 'available' || !quote.trim() || !draft || historical || locked) return
    const citation = { source_id: source.source.id, source_revision: source.source.revision, quote, occurrence }
    if (activeClaim && draft.claims.some(claim => claim.id === activeClaim)) {
      const claim = draft.claims.find(item => item.id === activeClaim)!
      if (claim.citations.length >= 8) { setError('每条声明最多保留 8 处引用'); return }
      changeClaim(activeClaim, { citations: [...claim.citations, citation] })
    } else {
      const claim = { ...newClaim(), text: quote, citations: [citation] }
      setDraft({ ...draft, claims: [...draft.claims, claim] }); setActiveClaim(claim.id)
    }
    setQuote(''); setNotice('引用已加入草稿')
  }
  const readHistory = async (append = false) => {
    if (!view || !connected || historyLoading || append && !history?.next_after_revision) return
    const captured = activeKey.current, generation = epoch.current, id = view.notebook.id, sequence = ++historySequence.current, read = controller()
    setHistoryLoading(true)
    try {
      const page = await api.getPersonalResearchHistory(id, { limit: 20, ...(append && history?.next_after_revision ? { after_revision: history.next_after_revision } : {}) }, read.signal)
      if (!valid(captured, generation, read.signal) || sequence !== historySequence.current || selected.current !== id) return
      setHistory(old => append && old ? { ...page, history: [...old.history, ...page.history.filter(item => !old.history.some(previous => previous.revision === item.revision))] } : page)
    } catch (cause) { if (valid(captured, generation, read.signal)) setError(researchError(cause)) }
    finally { readControllers.current.delete(read); if (valid(captured, generation, read.signal) && sequence === historySequence.current) setHistoryLoading(false) }
  }
  const navigate = (proceed: () => void) => requestArtifactNavigation(proceed)
  const chooseNotebook = (id: string) => navigate(() => { recovery.current.delete(key); setDraft(null); draftRef.current = null; setSelectedId(id); selected.current = id; setHistory(null); setHistoryOpen(false); setDiscovery(null); setNotice(''); void loadNotebook(id) })
  const create = async () => {
    try { validateResearchDraft(newDraft); await perform('create', { title: newDraft.title, question: newDraft.question, domain: newDraft.domain }, null) } catch (cause) { setError(researchError(cause)) }
  }
  const submitSource = async () => {
    if (!view || historical) return
    if (sourceMode === 'url') {
      if (!safeUrl(sourceUrl)) { setError('请填写完整的 http 或 https 来源地址'); return }
      await perform('capture', { id: view.notebook.id, url: sourceUrl.trim(), max_age_days: ageDays }, view.notebook.id)
    } else {
      if (!/^art_[a-f0-9]{32}$/.test(artifact.artifact_id) || !/^[a-f0-9]{64}$/.test(artifact.sha256) || !Number.isSafeInteger(artifact.version) || artifact.version < 1
          || artifact.grant_id && !/^asg_[a-f0-9]{32}$/.test(artifact.grant_id) || !Number.isSafeInteger(artifact.grant_revision) || artifact.grant_revision < 0) { setError('请核对资料编号、确切版本和摘要'); return }
      await perform('import', { id: view.notebook.id, artifact_ref: artifact, max_age_days: ageDays }, view.notebook.id)
    }
  }
  const exportReport = async () => {
    if (!view || !view.review.digest || view.review.status === 'not_reviewed' || dirty || historical) return
    await perform('publish', { id: view.notebook.id, expected_revision: view.notebook.revision, expected_review_digest: view.review.digest }, view.notebook.id)
  }
  const openPublication = () => {
    const ref = displayed?.publication
    if (!ref) return
    navigate(() => { if (onOpenArtifact) onOpenArtifact(ref.artifact_id, ref.version); else { const url = new URL(location.href); url.searchParams.set('artifact', ref.artifact_id); url.searchParams.set('version', String(ref.version)); window.location.assign(url.href) } })
  }
  const visibleClaims = historical ? historical.notebook.claims : draft?.claims || []

  return <section className="pres-workspace" aria-label="研究工作台">
    <header className="pres-header"><div className="pres-heading"><span className="pres-mark"><BookOpen size={21} /></span><h1>研究</h1>{!connected && <span className="pres-status">已断开</span>}</div><div className="pres-header-actions"><button aria-label="刷新研究目录" disabled={!connected || loading} onClick={() => navigate(() => { recovery.current.delete(key); setSelectedId(''); selected.current = ''; setView(null); setDraft(null); draftRef.current = null; void loadList() })}><RefreshCw size={16} /></button><button aria-label="关闭研究工作台" onClick={() => navigate(onClose)}><X size={19} /></button></div></header>
    {error && <div className="pres-alert" role="alert"><ShieldAlert size={16} /><span>{error}</span><button aria-label="关闭研究错误" onClick={() => setError('')}><X size={14} /></button></div>}
    {notice && <div className="pres-notice" role="status">{notice}<button aria-label="关闭研究提示" onClick={() => setNotice('')}><X size={13} /></button></div>}
    {pending && pendingScope === key && <div className="pres-pending" role="status"><Loader2 size={16} className={posting ? 'pres-spin' : ''} /><strong>{operationNames[pending.operation]}</strong><span>结果待核对</span><code>{pending.request_id}</code><div><button disabled={posting || !connected || !api.getPersonalResearchRequest} onClick={() => void reconcile()}>核对原请求</button>{frozen.current?.key === key && frozen.current.saved.request_id === pending.request_id && <button disabled={posting || !connected} onClick={() => { const original = frozen.current; if (original) void perform(original.operation, original.payload, original.saved.target, true) }}>重试原请求</button>}</div></div>}
    <div className="pres-layout">
      <aside className="pres-notebooks"><div className="pres-rail-heading"><h2>研究记录</h2><button className="pres-primary pres-icon" aria-label="新建研究" disabled={locked} onClick={() => navigate(() => { setNewDraft(researchDraft()); setNewOpen(true) })}><Plus size={17} /></button></div><label className="pres-search"><Search size={15} /><input aria-label="筛选研究" placeholder="搜索研究" value={listQuery} onChange={event => setListQuery(event.target.value)} /></label><nav aria-label="研究目录">{filtered.map(item => <button key={item.id} className={`pres-notebook${selectedId === item.id ? ' selected' : ''}`} onClick={() => chooseNotebook(item.id)} disabled={!connected}><span><strong>{item.title}</strong><small>{researchDomains[item.domain]} · v{item.revision}</small></span><span className={`pres-dot ${item.review_status}`} aria-label={researchReviewNames[item.review_status]} /></button>)}{!loading && !filtered.length && <div className="pres-empty"><BookOpen size={28} /><strong>{listQuery ? '没有匹配记录' : '暂无研究'}</strong></div>}</nav>{loading && <div className="pres-loading" role="status"><Loader2 size={16} className="pres-spin" />读取目录</div>}{nextAfter && <button className="pres-more" disabled={loading || !connected} onClick={() => void loadList(true)}>更多记录<ChevronDown size={14} /></button>}</aside>
      <main className="pres-document">
        {!connected ? <div className="pres-empty pres-document-empty"><ShieldAlert size={32} /><strong>连接已断开</strong></div> : loadingDetail && !displayed ? <div className="pres-loading pres-document-empty" role="status"><Loader2 size={22} className="pres-spin" />读取研究</div> : displayed?.availability === 'source_unavailable' ? <div className="pres-empty pres-document-empty" role="status"><ShieldAlert size={32} /><strong>原研究来源暂不可用</strong></div> : !displayed || !draft ? <div className="pres-empty pres-document-empty"><FileText size={35} /><strong>选择一项研究</strong><button className="pres-primary" disabled={locked} onClick={() => setNewOpen(true)}><Plus size={16} />新建研究</button></div> : <>
          <div className="pres-document-toolbar"><span className="pres-revision">v{displayed.notebook.revision}{historical ? ' · 历史版本' : dirty ? ' · 未保存' : ''}</span><div>{historical ? <button onClick={() => { setHistorical(null); setSource(null); setQuote('') }}><ArrowLeft size={14} />返回当前版本</button> : <><button disabled={locked || !dirty} onClick={() => void saveDraft()}><Save size={15} />保存</button><button disabled={locked || dirty} onClick={() => { setPane('review'); void perform('review', { id: view!.notebook.id, expected_revision: view!.notebook.revision }, view!.notebook.id) }}><ClipboardCheck size={15} />检查</button><button disabled={locked || dirty || !view!.review.digest || view!.review.status === 'not_reviewed'} onClick={() => void exportReport()}><FileDown size={15} />导出</button></>}<button aria-label="打开研究历史" disabled={!connected} onClick={() => { setHistoryOpen(true); void readHistory() }}><History size={16} /></button></div></div>
          {!historical && baseRevision !== view!.notebook.revision && <div className="pres-conflict"><span>草稿基于 v{baseRevision} · 当前 v{view!.notebook.revision}</span><button onClick={() => navigate(() => { const latest = researchDraft(view!.notebook); setDraft(latest); draftRef.current = latest; setBaseRevision(view!.notebook.revision); recovery.current.delete(key) })}>载入最新版本</button></div>}
          <div className="pres-title-block"><select aria-label="研究类型" value={historical?.notebook.domain || draft.domain} disabled={!editable || locked} onChange={event => setDraft({ ...draft, domain: event.target.value as ResearchDraft['domain'] })}>{Object.entries(researchDomains).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select><input className="pres-title-input" aria-label="研究标题" value={historical?.notebook.title || draft.title} maxLength={160} readOnly={!editable || locked} onChange={event => setDraft({ ...draft, title: event.target.value })} /><textarea aria-label="研究问题" value={historical?.notebook.question || draft.question} maxLength={4000} readOnly={!editable || locked} onChange={event => setDraft({ ...draft, question: event.target.value })} rows={2} /></div>
          <div className="pres-section-heading"><h2>声明</h2><span>{visibleClaims.length}</span>{!historical && <button disabled={locked || draft.claims.length >= 64} onClick={() => { const claim = newClaim(); setDraft({ ...draft, claims: [...draft.claims, claim] }); setActiveClaim(claim.id) }}><Plus size={15} />添加声明</button>}</div>
          <div className="pres-claims">{visibleClaims.map((claim, index) => <ClaimEditor key={claim.id} claim={claim} number={index + 1} active={activeClaim === claim.id} disabled={!editable || locked} sources={displayed.sources} onFocus={() => setActiveClaim(claim.id)} onChange={update => changeClaim(claim.id, update)} onRemove={() => removeClaim(claim.id)} onSource={item => void openSource(item)} />)}{!visibleClaims.length && <div className="pres-empty pres-claims-empty"><Quote size={27} /><strong>暂无声明</strong></div>}</div>
          <div className="pres-section-heading"><h2>判断</h2></div><div className="pres-conclusion"><label>当前选择<select aria-label="研究判断" disabled={!editable || locked} value={(historical?.notebook.conclusion || draft.conclusion).stance} onChange={event => setDraft({ ...draft, conclusion: { ...draft.conclusion, stance: event.target.value as ResearchDraft['conclusion']['stance'] } })}>{Object.entries(researchStances).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label>成立前提<textarea aria-label="判断成立前提" rows={3} readOnly={!editable || locked} value={(historical?.notebook.conclusion || draft.conclusion).premises.join('\n')} onChange={event => setDraft({ ...draft, conclusion: { ...draft.conclusion, premises: event.target.value.split('\n') } })} onBlur={() => setDraft(old => old ? { ...old, conclusion: { ...old.conclusion, premises: old.conclusion.premises.map(text => text.trim()).filter(Boolean) } } : old)} /></label><label>失效条件<textarea aria-label="判断失效条件" rows={3} readOnly={!editable || locked} value={(historical?.notebook.conclusion || draft.conclusion).invalidators.join('\n')} onChange={event => setDraft({ ...draft, conclusion: { ...draft.conclusion, invalidators: event.target.value.split('\n') } })} onBlur={() => setDraft(old => old ? { ...old, conclusion: { ...old.conclusion, invalidators: old.conclusion.invalidators.map(text => text.trim()).filter(Boolean) } } : old)} /></label></div>
          {displayed.publication && <button className="pres-publication" onClick={openPublication}><FileText size={19} /><span><strong>打开报告</strong><small>v{displayed.publication.version} · {displayed.publication.sha256.slice(0, 12)}</small></span><ExternalLink size={15} /></button>}
        </>}
      </main>
      <aside className="pres-inspector">{scopeReady && <><div className="pres-tabs" role="tablist" aria-label="研究依据"><button role="tab" aria-selected={pane === 'sources'} onClick={() => setPane('sources')}><Link2 size={15} />来源{displayed && <span>{displayed.sources.length}</span>}</button><button role="tab" aria-selected={pane === 'review'} onClick={() => setPane('review')}><ClipboardCheck size={15} />复核</button></div>{pane === 'sources' ? <>
        <div className="pres-source-actions"><button disabled={locked || !view || Boolean(historical)} onClick={() => setSourceForm(!sourceForm)}><Plus size={15} />添加来源</button><span>{displayed?.sources.filter(item => item.status !== 'available').length || 0} 待核对</span></div>
        {sourceForm && view && <div className="pres-source-form"><div className="pres-segment"><button aria-pressed={sourceMode === 'url'} onClick={() => setSourceMode('url')}>网页</button><button aria-pressed={sourceMode === 'artifact'} onClick={() => setSourceMode('artifact')}>资料</button></div>{sourceMode === 'url' ? <><form onSubmit={event => { event.preventDefault(); if (query.trim()) void perform('search', { id: view.notebook.id, query: query.trim(), max_results: 5 }, view.notebook.id) }} className="pres-query"><input aria-label="来源检索词" placeholder="检索词" value={query} maxLength={400} onChange={event => setQuery(event.target.value)} /><button aria-label="检索来源" disabled={locked || !query.trim()}><Search size={16} /></button></form><label>来源地址<input aria-label="来源地址" type="url" value={sourceUrl} maxLength={8192} onChange={event => setSourceUrl(event.target.value)} /></label></> : <><label>资料编号<input aria-label="研究资料编号" value={artifact.artifact_id} onChange={event => setArtifact({ ...artifact, artifact_id: event.target.value.trim() })} /></label><div className="pres-two-fields"><label>版本<input aria-label="研究资料版本" type="number" min={1} value={artifact.version} onChange={event => setArtifact({ ...artifact, version: Number(event.target.value) })} /></label><label>授权版本<input aria-label="研究资料授权版本" type="number" min={0} value={artifact.grant_revision} onChange={event => setArtifact({ ...artifact, grant_revision: Number(event.target.value) })} /></label></div><label>SHA256<input aria-label="研究资料摘要" value={artifact.sha256} maxLength={64} onChange={event => setArtifact({ ...artifact, sha256: event.target.value.trim() })} /></label><label>授权编号<input aria-label="研究资料授权编号" value={artifact.grant_id} onChange={event => setArtifact({ ...artifact, grant_id: event.target.value.trim() })} /></label></>}<div className="pres-source-submit"><label>有效期<select aria-label="来源有效期" value={ageDays} onChange={event => setAgeDays(Number(event.target.value))}>{[1, 7, 30, 90].map(day => <option key={day} value={day}>{day} 天</option>)}</select></label><button className="pres-primary" disabled={locked} onClick={() => void submitSource()}>{sourceMode === 'url' ? '抓取正文' : '引用确切版本'}</button></div></div>}
        {discovery && <div className="pres-discovery"><header><strong>检索结果</strong><button aria-label="关闭检索结果" onClick={() => setDiscovery(null)}><X size={14} /></button></header>{discovery.items.length ? discovery.items.map((item, index) => <button key={`${item.url}:${index}`} disabled={locked || Boolean(historical)} onClick={() => { setSourceMode('url'); setSourceForm(true); setSourceUrl(item.url) }}><strong>{item.title || item.url}</strong><small>{item.url}</small>{item.snippet && <span>{item.snippet}</span>}<ChevronRight size={14} /></button>) : <p>{discovery.status === 'empty' ? '没有检索结果' : '检索暂未完成'}</p>}<span className="pres-discovery-status">尚未读取正文</span></div>}
        <div className="pres-source-list" aria-label="研究来源">{displayed?.sources.map(item => { const expired = Date.parse(item.expires_at) <= Date.parse(context?.server_now || new Date().toISOString()); return <button key={`${item.id}:${item.revision}`} className={`pres-source${source?.source.id === item.id ? ' selected' : ''}`} disabled={!connected} onClick={() => void openSource(item)}><span className="pres-source-icon">{item.origin === 'url' ? <Link2 size={16} /> : <FileText size={16} />}</span><span><strong>{item.title || item.url || item.id}</strong><small>v{item.revision} · {time(item.captured_at)}</small></span>{item.status !== 'available' ? <em>待核对</em> : expired ? <em>已过期</em> : item.upstream_unverified ? <em>上游未核实</em> : null}</button> })}{displayed && !displayed.sources.length && <div className="pres-empty"><Link2 size={28} /><strong>暂无来源</strong></div>}</div>
        {sourceLoading && <div className="pres-loading" role="status"><Loader2 size={16} className="pres-spin" />读取原正文</div>}
        {source && <div className="pres-source-detail"><header><strong>{source.source.title || '来源正文'}</strong><button aria-label="关闭来源正文" onClick={() => { sourceSequence.current += 1; setSource(null); setQuote('') }}><X size={15} /></button></header><div className="pres-source-meta"><span>v{source.source.revision} · {time(source.source.captured_at)}</span>{safeUrl(source.source.url) && <a href={safeUrl(source.source.url)!} target="_blank" rel="noopener noreferrer">原网页<ExternalLink size={12} /></a>}<button disabled={locked || Boolean(historical)} onClick={() => view && void perform('recheck', { source_id: source.source.id, expected_revision: source.source.revision }, view.notebook.id)}><RefreshCw size={12} />复查</button></div>{source.body !== null ? <><pre ref={bodyElement} aria-label="来源正文" onMouseUp={selectQuote} onKeyUp={selectQuote}>{source.body}</pre>{editable && <div className="pres-quote-picker"><label>引用原文<textarea aria-label="引用原文" rows={3} maxLength={4000} value={quote} onChange={event => setQuote(event.target.value)} /></label><div className="pres-two-fields"><label>加入声明<select aria-label="引用目标声明" value={activeClaim} onChange={event => setActiveClaim(event.target.value)}><option value="">新声明</option>{draft?.claims.map((claim, index) => <option key={claim.id} value={claim.id}>{index + 1} · {claim.text.slice(0, 22) || claim.id}</option>)}</select></label><label>匹配序号<input aria-label="引用匹配序号" type="number" min={1} max={500} value={occurrence + 1} onChange={event => setOccurrence(Math.max(0, Number(event.target.value) - 1))} /></label></div><button className="pres-primary" disabled={locked || !quote.trim()} onClick={addCitation}><Quote size={14} />加入引用</button></div>}</> : <div className="pres-source-unavailable" role="status"><ShieldAlert size={19} /><strong>{source.availability === 'source_stale' ? '原来源已变化' : '原正文暂不可用'}</strong></div>}<code className="pres-sha">{source.source.body_sha256}</code></div>}
      </> : <ReviewPanel view={displayed} onSource={(sourceId, sourceRevision) => { const item = displayed?.sources.find(value => value.id === sourceId); if (item) void openSource({ ...item, revision: sourceRevision }) }} />}</>}</aside>
    </div>
    {newOpen && scopeReady && <div className="pres-dialog-backdrop"><section className="pres-dialog" role="dialog" aria-modal="true" aria-labelledby="pres-create-title"><header><h2 id="pres-create-title">新建研究</h2><button aria-label="关闭新建研究" disabled={posting} onClick={() => navigate(() => setNewOpen(false))}><X size={18} /></button></header><label>标题<input aria-label="新研究标题" autoFocus maxLength={160} value={newDraft.title} onChange={event => setNewDraft({ ...newDraft, title: event.target.value })} /></label><label>问题<textarea aria-label="新研究问题" rows={4} maxLength={4000} value={newDraft.question} onChange={event => setNewDraft({ ...newDraft, question: event.target.value })} /></label><label>类型<select aria-label="新研究类型" value={newDraft.domain} onChange={event => setNewDraft({ ...newDraft, domain: event.target.value as ResearchDraft['domain'] })}>{Object.entries(researchDomains).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><footer><button disabled={posting} onClick={() => navigate(() => setNewOpen(false))}>取消</button><button className="pres-primary" disabled={locked || !newDraft.title.trim() || !newDraft.question.trim()} onClick={() => void create()}>创建</button></footer></section></div>}
    {historyOpen && scopeReady && <div className="pres-dialog-backdrop"><section className="pres-dialog pres-history-dialog" role="dialog" aria-modal="true" aria-labelledby="pres-history-title"><header><h2 id="pres-history-title">研究历史</h2><button aria-label="关闭研究历史" onClick={() => setHistoryOpen(false)}><X size={18} /></button></header>{history?.history.map(item => <button className="pres-history-row" key={item.revision} onClick={() => navigate(() => { setHistoryOpen(false); if (view) void loadNotebook(view.notebook.id, item.revision) })}><span>v{item.revision}</span><strong>{item.title}</strong><small>{item.claim_count} 条声明 · {time(item.updated_at)}</small><ChevronRight size={15} /></button>)}{historyLoading && <div className="pres-loading" role="status"><Loader2 size={16} className="pres-spin" />读取历史</div>}{history && !history.history.length && <div className="pres-empty"><History size={28} /><strong>暂无历史</strong></div>}{history?.next_after_revision && <button className="pres-more" disabled={historyLoading} onClick={() => void readHistory(true)}>更多版本</button>}</section></div>}
    {leave && <div className="pres-dialog-backdrop"><section className="pres-dialog pres-leave-dialog" role="dialog" aria-modal="true" aria-labelledby="pres-leave-title"><h2 id="pres-leave-title">放弃未保存的修改？</h2><footer><button onClick={() => { leave.cancel?.(); setLeave(null) }}>继续编辑</button><button className="pres-danger" onClick={() => { const next = leave; dirtyRef.current = false; recovery.current.delete(key); if (currentView.current) { const clean = researchDraft(currentView.current.notebook); setDraft(clean); draftRef.current = clean; setBaseRevision(currentView.current.notebook.revision) }; setNewDraft(researchDraft()); setLeave(null); next.proceed() }}>放弃修改</button></footer></section></div>}
  </section>
}

function ClaimEditor({ claim, number, active, disabled, sources, onFocus, onChange, onRemove, onSource }: {
  claim: ResearchClaim; number: number; active: boolean; disabled: boolean; sources: ResearchSource[]
  onFocus: () => void; onChange: (update: Partial<ResearchClaim>) => void; onRemove: () => void; onSource: (source: ResearchSource) => void
}) {
  return <article className={`pres-claim${active ? ' active' : ''}`} onFocus={onFocus} aria-label={`声明 ${number}`}><header><button className="pres-claim-number" onClick={onFocus} aria-label={`选择声明 ${number}`}>{number.toString().padStart(2, '0')}</button><select aria-label={`声明 ${number} 类型`} value={claim.kind} disabled={disabled} onChange={event => onChange({ kind: event.target.value as ResearchClaim['kind'] })}>{Object.entries(researchKinds).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select><code>{claim.id}</code><button className="pres-remove" aria-label={`删除声明 ${number}`} disabled={disabled} onClick={onRemove}><Trash2 size={14} /></button></header><textarea aria-label={`声明 ${number} 内容`} placeholder="声明内容" rows={3} value={claim.text} maxLength={4000} readOnly={disabled} onChange={event => onChange({ text: event.target.value })} />
    {claim.citations.length > 0 && <div className="pres-citations">{claim.citations.map((ref, index) => { const source = sources.find(item => item.id === ref.source_id); return <div key={`${ref.source_id}:${ref.source_revision}:${index}`}><button className="pres-citation-source" disabled={!source} onClick={() => source && onSource({ ...source, revision: ref.source_revision })}><Quote size={13} /><span>{source?.title || ref.source_id}</span><small>v{ref.source_revision}</small></button><blockquote>{ref.quote}</blockquote>{!disabled && <button className="pres-citation-remove" aria-label={`移除声明 ${number} 的引用 ${index + 1}`} onClick={() => onChange({ citations: claim.citations.filter((_, position) => position !== index) })}><X size={13} /></button>}</div> })}</div>}
    <details className="pres-claim-details"><summary>数值与前提<ChevronDown size={13} /></summary><label>对象<input aria-label={`声明 ${number} 对象`} value={claim.subject} maxLength={256} readOnly={disabled} onChange={event => onChange({ subject: event.target.value })} /></label><label>前提<textarea aria-label={`声明 ${number} 前提`} rows={2} readOnly={disabled} value={claim.assumptions.join('\n')} onChange={event => onChange({ assumptions: event.target.value.split('\n') })} onBlur={() => onChange({ assumptions: lines(claim.assumptions.join('\n')) })} /></label><label className="pres-check"><input aria-label={`声明 ${number} 包含数值`} type="checkbox" checked={Boolean(claim.quantity)} disabled={disabled} onChange={event => onChange({ quantity: event.target.checked ? { value: '', unit: '', currency: null, period: null } : null })} />数值</label>{claim.quantity && <><div className="pres-two-fields"><label>数值<input aria-label={`声明 ${number} 数值`} value={claim.quantity.value} maxLength={80} readOnly={disabled} onChange={event => onChange({ quantity: { ...claim.quantity!, value: event.target.value } })} /></label><label>单位<input aria-label={`声明 ${number} 单位`} value={claim.quantity.unit} maxLength={32} readOnly={disabled} onChange={event => onChange({ quantity: { ...claim.quantity!, unit: event.target.value } })} /></label></div><div className="pres-two-fields"><label>币种<input aria-label={`声明 ${number} 币种`} value={claim.quantity.currency || ''} maxLength={3} readOnly={disabled} onChange={event => onChange({ quantity: { ...claim.quantity!, currency: event.target.value.toUpperCase() || null } })} /></label><label>期间<input aria-label={`声明 ${number} 期间`} value={claim.quantity.period || ''} maxLength={128} readOnly={disabled} onChange={event => onChange({ quantity: { ...claim.quantity!, period: event.target.value || null } })} /></label></div></>}
    <label className="pres-check"><input aria-label={`声明 ${number} 包含计算`} type="checkbox" checked={Boolean(claim.calculation)} disabled={disabled} onChange={event => onChange({ calculation: event.target.checked ? { operation: 'percent', inputs: [], expected: '', tolerance: '0.000001' } : null })} />计算</label>{claim.calculation && <><div className="pres-two-fields"><label>运算<select aria-label={`声明 ${number} 运算`} disabled={disabled} value={claim.calculation.operation} onChange={event => onChange({ calculation: { ...claim.calculation!, operation: event.target.value as ResearchCalculation['operation'] } })}>{Object.entries(calculationNames).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label>预期值<input aria-label={`声明 ${number} 预期值`} readOnly={disabled} value={claim.calculation.expected} onChange={event => onChange({ calculation: { ...claim.calculation!, expected: event.target.value } })} /></label></div><label>输入声明编号<input aria-label={`声明 ${number} 输入声明编号`} readOnly={disabled} value={claim.calculation.inputs.join(', ')} onChange={event => onChange({ calculation: { ...claim.calculation!, inputs: event.target.value.split(',').map(id => id.trim()) } })} onBlur={() => onChange({ calculation: { ...claim.calculation!, inputs: claim.calculation!.inputs.filter(Boolean) } })} /></label><label>容差<input aria-label={`声明 ${number} 容差`} readOnly={disabled} value={claim.calculation.tolerance} onChange={event => onChange({ calculation: { ...claim.calculation!, tolerance: event.target.value } })} /></label></>}
    <div className="pres-upstream-heading"><strong>上游声明</strong><button aria-label={`添加声明 ${number} 的上游引用`} disabled={disabled || claim.upstream_claims.length >= 8} onClick={() => onChange({ upstream_claims: [...claim.upstream_claims, { notebook_id: '', revision: 1, claim_id: '' }] })}><Plus size={13} /></button></div>{claim.upstream_claims.map((ref, index) => <div className="pres-upstream" key={index}><label>研究编号<input aria-label={`声明 ${number} 上游研究 ${index + 1}`} value={ref.notebook_id} readOnly={disabled} onChange={event => onChange({ upstream_claims: claim.upstream_claims.map((item, position) => position === index ? { ...item, notebook_id: event.target.value } : item) })} /></label><div className="pres-two-fields"><label>版本<input aria-label={`声明 ${number} 上游版本 ${index + 1}`} type="number" min={1} value={ref.revision} disabled={disabled} onChange={event => onChange({ upstream_claims: claim.upstream_claims.map((item, position) => position === index ? { ...item, revision: Number(event.target.value) } : item) })} /></label><label>声明编号<input aria-label={`声明 ${number} 上游声明 ${index + 1}`} value={ref.claim_id} readOnly={disabled} onChange={event => onChange({ upstream_claims: claim.upstream_claims.map((item, position) => position === index ? { ...item, claim_id: event.target.value } : item) })} /></label></div><button disabled={disabled} onClick={() => onChange({ upstream_claims: claim.upstream_claims.filter((_, position) => position !== index) })}>移除上游引用</button></div>)}</details>
  </article>
}

function ReviewPanel({ view, onSource }: { view: ResearchView | null; onSource: (id: string, revision: number) => void }) {
  const review = view?.review
  if (!review) return <div className="pres-empty"><ClipboardCheck size={29} /><strong>暂无复核记录</strong></div>
  return <div className="pres-review"><header><span className={`pres-review-badge ${review.status}`}><ClipboardCheck size={17} />{researchReviewNames[review.status]}</span><span className="pres-unverified">语义待核实</span>{review.checked_at && <time>{time(review.checked_at)}</time>}</header><div className="pres-findings" aria-label="研究检查结果">{review.findings.map((finding, index) => <article key={`${finding.code}:${finding.claim_id}:${index}`} className={finding.severity}><span>{finding.severity === 'error' ? '问题' : '注意'}{finding.claim_id && ` · ${finding.claim_id}`}</span><p>{finding.message}</p><code>{finding.code}</code></article>)}{!review.findings.length && <div className="pres-empty"><ClipboardCheck size={26} /><strong>{review.status === 'not_reviewed' ? '尚未检查' : '未发现机械问题'}</strong></div>}</div>{review.anchors.length > 0 && <><h3>原文定位</h3><div className="pres-anchors">{review.anchors.map((anchor, index) => <button key={`${anchor.claim_id}:${anchor.source_id}:${index}`} onClick={() => onSource(anchor.source_id, anchor.source_revision)}><span><strong>{anchor.claim_id}</strong><small>来源 v{anchor.source_revision}</small></span><blockquote>{anchor.quote}</blockquote><code>字符 {anchor.start}–{anchor.end} · 字节 {anchor.byte_start}–{anchor.byte_end}</code></button>)}</div></>}{review.digest && <code className="pres-sha">{review.digest}</code>}</div>
}
