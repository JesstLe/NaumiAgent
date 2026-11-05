import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ResearchWorkspace } from '../../../web2/src/personal/research/ResearchWorkspace'
import { cloneResearch, parseResearchPending, researchPendingKey, validateResearchDraft, validateResearchView } from '../../../web2/src/personal/research/types'
import type { ResearchApi, ResearchClaim, ResearchContext, ResearchResult, ResearchSource, ResearchView } from '../../../web2/src/personal/research/types'

const notebookId = `rq_${'a'.repeat(32)}`, secondId = `rq_${'b'.repeat(32)}`, sourceId = `rs_${'c'.repeat(32)}`
const artifactId = `art_${'d'.repeat(32)}`, hash = 'e'.repeat(64), requestId = 'f'.repeat(32), workspace = '1'.repeat(64)
const source: ResearchSource = { id: sourceId, revision: 2, title: '十月月报', origin: 'url', url: 'https://example.com/report', captured_at: '2026-11-06T02:00:00Z', expires_at: '2026-12-06T02:00:00Z', status: 'available', body_sha256: hash,
  artifact_ref: { artifact_id: artifactId, version: 1, sha256: hash, grant_id: '', grant_revision: 0 } }
const sourceBody = '月报🧮：收入 1200 元，支出 375 元，结余 825 元。'
const claim: ResearchClaim = { id: 'income', text: '本期收入为 1200 元。', subject: '收入', kind: 'fact', citations: [], quantity: null, calculation: null, assumptions: [], upstream_claims: [] }
const context: ResearchContext = { actor_id: 'main', workspace_key: workspace, server_now: '2026-11-06T04:00:00Z' }
const view = (changes: Partial<ResearchView> = {}): ResearchView => ({ notebook: { id: notebookId, revision: 3, title: '收入研究', question: '本期收入与支出有何变化？', domain: 'operations', updated_at: '2026-11-06T03:00:00Z', actor_id: 'main', workspace_key: workspace,
  claims: [cloneResearch(claim)], conclusion: { stance: 'undecided', premises: [], invalidators: [] } }, sources: [cloneResearch(source)], review: { status: 'not_reviewed', semantic_verification: 'not_performed', findings: [], anchors: [] }, publication: null, ...changes })
const failure = (status: number, message = 'request failed') => Object.assign(new Error(message), { status })
const deferred = <T,>() => { let resolve!: (value: T) => void, reject!: (reason: unknown) => void; const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no }); return { resolve, reject, promise } }

function apiMock(initial = view()) {
  const api = {
    getPersonalResearchContext: vi.fn().mockResolvedValue(context),
    listPersonalResearch: vi.fn().mockResolvedValue({ notebooks: [{ id: initial.notebook.id, revision: initial.notebook.revision, title: initial.notebook.title, question: initial.notebook.question, domain: initial.notebook.domain, updated_at: initial.notebook.updated_at || '', source_count: initial.sources.length, claim_count: initial.notebook.claims.length, review_status: initial.review.status }], next_after: null }),
    getPersonalResearch: vi.fn().mockResolvedValue(initial),
    getPersonalResearchSource: vi.fn().mockResolvedValue({ source: cloneResearch(source), body: sourceBody, availability: 'available' }),
    getPersonalResearchHistory: vi.fn().mockResolvedValue({ history: [{ revision: 1, title: '最初收入研究', updated_at: '2026-11-05T03:00:00Z', claim_count: 0 }], next_after_revision: null }),
    getPersonalResearchRequest: vi.fn().mockResolvedValue({ request_id: requestId, status: 'pending' }),
    executePersonalResearch: vi.fn().mockImplementation(async (operation: string, payload: Record<string, unknown>): Promise<ResearchResult> => {
      const next = cloneResearch(initial)
      if (operation === 'create') { next.notebook.id = secondId; next.notebook.revision = 1; next.notebook.title = String(payload.title); next.notebook.question = String(payload.question); next.notebook.domain = payload.domain as typeof next.notebook.domain; next.notebook.claims = []; next.sources = [] }
      if (operation === 'revise') { next.notebook = { ...next.notebook, ...(payload as unknown as Partial<typeof next.notebook>), revision: next.notebook.revision + 1 } }
      if (operation === 'review') next.review = { id: `rr_${'2'.repeat(32)}`, status: 'mechanically_checked', semantic_verification: 'not_performed', findings: [], anchors: [], digest: hash }
      if (operation === 'publish') next.publication = { artifact_id: artifactId, version: 2, sha256: hash }
      if (operation === 'search') return { discovery: { id: `rd_${'3'.repeat(32)}`, query: String(payload.query), status: 'success', text: '结果', items: [{ title: '原始月报', url: 'https://example.com/new-report', snippet: '本期收入资料' }] }, view: next }
      return next
    }),
  } satisfies ResearchApi
  return api
}

async function ready(api = apiMock(), namespace = 'research-A') {
  const onClose = vi.fn(), onOpenArtifact = vi.fn()
  const rendered = render(<ResearchWorkspace api={api} namespace={namespace} connected onClose={onClose} onOpenArtifact={onOpenArtifact} />)
  await waitFor(() => expect(screen.getByLabelText('研究标题')).toHaveValue('收入研究'))
  return { api, onClose, onOpenArtifact, ...rendered }
}

beforeEach(() => { localStorage.clear(); window.history.replaceState({}, '', '/web2?research=1') })
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('research exact identities and opaque requests', () => {
  it('rejects private request fields and malformed source/version contracts', () => {
    expect(parseResearchPending(JSON.stringify({ version: 1, request_id: requestId, operation: 'revise', target: notebookId }))).toEqual({ version: 1, request_id: requestId, operation: 'revise', target: notebookId })
    expect(() => parseResearchPending(JSON.stringify({ version: 1, request_id: requestId, operation: 'revise', target: notebookId, title: 'PRIVATE' }))).toThrow('原研究请求状态待核对')
    expect(() => parseResearchPending('not JSON')).toThrow('原研究请求状态待核对')
    expect(() => validateResearchView(view(), secondId)).toThrow()
    const invalid = view(); invalid.sources[0].revision = 0
    expect(() => validateResearchView(invalid)).toThrow()
    expect(() => validateResearchDraft({ title: '原研究', question: '问题', domain: 'general', claims: [{ ...claim, kind: 'estimate' }], conclusion: { stance: 'undecided', premises: [], invalidators: [] } })).toThrow('估算前提')
  })

  it('saves a full current-revision edit without putting private text in pending storage', async () => {
    const api = apiMock(), gate = deferred<ResearchResult>(); api.executePersonalResearch.mockImplementationOnce(() => gate.promise)
    await ready(api)
    fireEvent.change(screen.getByLabelText('研究标题'), { target: { value: 'PRIVATE_RESEARCH_TITLE' } })
    fireEvent.change(screen.getByLabelText('声明 1 内容'), { target: { value: 'PRIVATE_CLAIM_BODY' } })
    fireEvent.click(screen.getByRole('button', { name: '保存', exact: true }))
    await waitFor(() => expect(api.executePersonalResearch).toHaveBeenCalledTimes(1))
    const [operation, payload] = api.executePersonalResearch.mock.calls[0]
    expect(operation).toBe('revise'); expect(payload).toMatchObject({ id: notebookId, expected_revision: 3, title: 'PRIVATE_RESEARCH_TITLE' })
    expect(payload.claims).toEqual([{ ...claim, text: 'PRIVATE_CLAIM_BODY' }])
    const stored = localStorage.getItem(researchPendingKey('research-A'))!
    expect(stored).not.toContain('PRIVATE_RESEARCH_TITLE'); expect(stored).not.toContain('PRIVATE_CLAIM_BODY'); expect(stored).not.toContain('claims'); expect(stored).not.toContain('question')
    expect(parseResearchPending(stored).target).toBe(notebookId)
    const saved = view(); saved.notebook = { ...saved.notebook, title: 'PRIVATE_RESEARCH_TITLE', claims: [{ ...claim, text: 'PRIVATE_CLAIM_BODY' }], revision: 4 }
    await act(async () => { gate.resolve(saved); await gate.promise })
    await waitFor(() => expect(localStorage.getItem(researchPendingKey('research-A'))).toBeNull())
    expect(screen.getByRole('button', { name: '保存', exact: true })).toBeDisabled()
  })

  it('keeps UNKNOWN and retries only the original frozen operation and request identity', async () => {
    const api = apiMock(); api.executePersonalResearch.mockRejectedValueOnce(failure(503, '503 UNKNOWN'))
    await ready(api)
    fireEvent.change(screen.getByLabelText('研究标题'), { target: { value: '原请求研究' } })
    fireEvent.click(screen.getByRole('button', { name: '保存', exact: true }))
    await waitFor(() => expect(screen.getByRole('button', { name: '重试原请求' })).toBeEnabled())
    const original = cloneResearch(api.executePersonalResearch.mock.calls[0])
    const id = parseResearchPending(localStorage.getItem(researchPendingKey('research-A'))!).request_id
    fireEvent.click(screen.getByRole('button', { name: '重试原请求' }))
    await waitFor(() => expect(api.executePersonalResearch).toHaveBeenCalledTimes(2))
    expect(api.executePersonalResearch.mock.calls[1]).toEqual(original)
    expect(api.executePersonalResearch.mock.calls[1][1].request_id).toBe(id)
    await waitFor(() => expect(localStorage.getItem(researchPendingKey('research-A'))).toBeNull())
  })

  it('reconciles a remounted original request by GET and never reposts its private body', async () => {
    localStorage.setItem(researchPendingKey('research-A'), JSON.stringify({ version: 1, request_id: requestId, operation: 'revise', target: notebookId }))
    const api = apiMock(); api.getPersonalResearchRequest.mockResolvedValueOnce({ request_id: requestId, status: 'completed', operation: 'revise', view: view({ notebook: { ...view().notebook, title: '原受理版本', revision: 4 } }) })
    await ready(api)
    expect(screen.queryByRole('button', { name: '重试原请求' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '核对原请求' }))
    await waitFor(() => expect(screen.getByLabelText('研究标题')).toHaveValue('原受理版本'))
    expect(api.getPersonalResearchRequest).toHaveBeenCalledWith(requestId, expect.any(AbortSignal)); expect(api.executePersonalResearch).not.toHaveBeenCalled()
    expect(localStorage.getItem(researchPendingKey('research-A'))).toBeNull()
  })

  it('retains an unknown GET reconciliation without inventing a new mutation', async () => {
    localStorage.setItem(researchPendingKey('research-A'), JSON.stringify({ version: 1, request_id: requestId, operation: 'capture', target: notebookId }))
    const api = apiMock(); api.getPersonalResearchRequest.mockResolvedValue({ request_id: requestId, status: 'unknown' })
    await ready(api); fireEvent.click(screen.getByRole('button', { name: '核对原请求' }))
    await screen.findByText('原请求结果待核对，未重新提交')
    expect(api.executePersonalResearch).not.toHaveBeenCalled(); expect(localStorage.getItem(researchPendingKey('research-A'))).not.toBeNull()
  })

  it('does not transmit anything when the original opaque receipt cannot be stored', async () => {
    const api = apiMock(); await ready(api)
    fireEvent.change(screen.getByLabelText('研究标题'), { target: { value: '保存失败的草稿' } })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota') })
    fireEvent.click(screen.getByRole('button', { name: '保存', exact: true }))
    await screen.findByText('请求状态保存失败，请重试')
    expect(api.executePersonalResearch).not.toHaveBeenCalled(); expect(screen.getByLabelText('研究标题')).toHaveValue('保存失败的草稿')
  })
})

describe('research source, claims and review workflow', () => {
  it('adds an exact source quotation without sending browser character offsets', async () => {
    const { api } = await ready()
    fireEvent.click(screen.getByRole('button', { name: /十月月报/ }))
    await waitFor(() => expect(screen.getByLabelText('来源正文')).toHaveTextContent(sourceBody))
    fireEvent.change(screen.getByLabelText('引用原文'), { target: { value: '收入 1200 元' } })
    fireEvent.change(screen.getByLabelText('引用目标声明'), { target: { value: 'income' } })
    fireEvent.change(screen.getByLabelText('引用匹配序号'), { target: { value: '2' } })
    fireEvent.click(screen.getByRole('button', { name: '加入引用' }))
    expect(within(screen.getByRole('article', { name: '声明 1' })).getByText('收入 1200 元')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '保存', exact: true }))
    await waitFor(() => expect(api.executePersonalResearch).toHaveBeenCalledTimes(1))
    const citation = (api.executePersonalResearch.mock.calls[0][1].claims as ResearchClaim[])[0].citations[0]
    expect(citation).toEqual({ source_id: sourceId, source_revision: 2, quote: '收入 1200 元', occurrence: 1 })
    expect(citation).not.toHaveProperty('start'); expect(citation).not.toHaveProperty('byte_start')
    expect(api.getPersonalResearchSource).toHaveBeenCalledWith(sourceId, 2, expect.any(AbortSignal))
  })

  it('opens the cited historical source revision rather than silently substituting the latest source', async () => {
    const initial = view({ notebook: { ...view().notebook, claims: [{ ...claim, citations: [{ source_id: sourceId, source_revision: 1, quote: '原版本收入', occurrence: 0 }] }] },
      review: { status: 'issues_found', semantic_verification: 'not_performed', findings: [], anchors: [{ claim_id: 'income', source_id: sourceId, source_revision: 1, quote: '原版本收入', start: 0, end: 6, byte_start: 0, byte_end: 18, sha256: hash }] } })
    const api = apiMock(initial); api.getPersonalResearchSource.mockImplementation(async (_id: string, revision?: number) => ({ source: { ...source, revision: revision || 2 }, body: '原版本收入', availability: 'available' }))
    await ready(api)
    fireEvent.click(screen.getByRole('button', { name: /十月月报\s*v1/ }))
    await waitFor(() => expect(api.getPersonalResearchSource).toHaveBeenLastCalledWith(sourceId, 1, expect.any(AbortSignal)))
    fireEvent.click(screen.getByRole('tab', { name: '复核', exact: true }))
    fireEvent.click(screen.getByRole('button', { name: /income.*来源 v1/ }))
    await waitFor(() => expect(api.getPersonalResearchSource).toHaveBeenCalledTimes(2))
    expect(api.getPersonalResearchSource).toHaveBeenLastCalledWith(sourceId, 1, expect.any(AbortSignal))
  })

  it('edits quantities, units, currencies, calculations and upstream references in the real payload', async () => {
    const { api } = await ready()
    fireEvent.click(screen.getByText('数值与前提'))
    fireEvent.click(screen.getByLabelText('声明 1 包含数值'))
    fireEvent.change(screen.getByLabelText('声明 1 数值'), { target: { value: '1200' } })
    fireEvent.change(screen.getByLabelText('声明 1 单位'), { target: { value: '元' } })
    fireEvent.change(screen.getByLabelText('声明 1 币种'), { target: { value: 'CNY' } })
    fireEvent.change(screen.getByLabelText('声明 1 期间'), { target: { value: '2026-10' } })
    fireEvent.click(screen.getByLabelText('声明 1 包含计算'))
    fireEvent.change(screen.getByLabelText('声明 1 运算'), { target: { value: 'difference' } })
    fireEvent.change(screen.getByLabelText('声明 1 输入声明编号'), { target: { value: 'gross, costs' } })
    fireEvent.change(screen.getByLabelText('声明 1 预期值'), { target: { value: '825' } })
    fireEvent.click(screen.getByRole('button', { name: '添加声明 1 的上游引用' }))
    fireEvent.change(screen.getByLabelText('声明 1 上游研究 1'), { target: { value: secondId } })
    fireEvent.change(screen.getByLabelText('声明 1 上游版本 1'), { target: { value: '7' } })
    fireEvent.change(screen.getByLabelText('声明 1 上游声明 1'), { target: { value: 'gross' } })
    fireEvent.click(screen.getByRole('button', { name: '保存', exact: true }))
    await waitFor(() => expect(api.executePersonalResearch).toHaveBeenCalledTimes(1))
    const saved = (api.executePersonalResearch.mock.calls[0][1].claims as ResearchClaim[])[0]
    expect(saved.quantity).toEqual({ value: '1200', unit: '元', currency: 'CNY', period: '2026-10' })
    expect(saved.calculation).toEqual({ operation: 'difference', inputs: ['gross', 'costs'], expected: '825', tolerance: '0.000001' })
    expect(saved.upstream_claims).toEqual([{ notebook_id: secondId, revision: 7, claim_id: 'gross' }])
  })

  it('requires an estimate premise and keeps unknown claims classified as unknown', async () => {
    const { api } = await ready()
    fireEvent.change(screen.getByLabelText('声明 1 类型'), { target: { value: 'estimate' } })
    fireEvent.click(screen.getByRole('button', { name: '保存', exact: true }))
    await screen.findByText('请填写 income 的估算前提'); expect(api.executePersonalResearch).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('声明 1 类型'), { target: { value: 'unknown' } })
    fireEvent.click(screen.getByRole('button', { name: '保存', exact: true }))
    await waitFor(() => expect(api.executePersonalResearch).toHaveBeenCalledTimes(1))
    expect((api.executePersonalResearch.mock.calls[0][1].claims as ResearchClaim[])[0].kind).toBe('unknown')
  })

  it('runs mechanical review on the saved revision and renders findings plus original-byte anchors', async () => {
    const api = apiMock()
    api.executePersonalResearch.mockResolvedValueOnce(view({ review: { id: `rr_${'2'.repeat(32)}`, status: 'issues_found', semantic_verification: 'not_performed', digest: hash,
      findings: [{ code: 'calculation_mismatch', claim_id: 'income', severity: 'error', message: '复算结果为 20，原声明为 25' }],
      anchors: [{ claim_id: 'income', source_id: sourceId, source_revision: 2, quote: '收入 1200 元', start: 5, end: 14, byte_start: 16, byte_end: 35, sha256: hash }] } }))
    await ready(api)
    fireEvent.click(screen.getByRole('button', { name: '检查', exact: true }))
    await screen.findByText('复算结果为 20，原声明为 25')
    expect(api.executePersonalResearch).toHaveBeenCalledWith('review', { id: notebookId, expected_revision: 3, request_id: expect.stringMatching(/^[a-f0-9]{32}$/) })
    expect(screen.getByText('语义待核实')).toBeInTheDocument(); expect(screen.queryByText('已证实')).not.toBeInTheDocument()
    expect(screen.getByText('字符 5–14 · 字节 16–35')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /income.*来源 v2/ }))
    await waitFor(() => expect(api.getPersonalResearchSource).toHaveBeenCalledWith(sourceId, 2, expect.any(AbortSignal)))
  })

  it('captures discovery results only after an explicit user request to fetch the actual body', async () => {
    const { api } = await ready()
    fireEvent.click(screen.getByRole('button', { name: '添加来源' }))
    fireEvent.change(screen.getByLabelText('来源检索词'), { target: { value: '月度收入' } })
    fireEvent.click(screen.getByRole('button', { name: '检索来源' }))
    await screen.findByText('尚未读取正文')
    expect(api.executePersonalResearch.mock.calls[0][0]).toBe('search')
    expect(api.executePersonalResearch.mock.calls.filter(call => call[0] === 'capture')).toHaveLength(0)
    fireEvent.click(screen.getByRole('button', { name: /原始月报/ }))
    expect(screen.getByLabelText('来源地址')).toHaveValue('https://example.com/new-report')
    fireEvent.change(screen.getByLabelText('来源有效期'), { target: { value: '7' } })
    fireEvent.click(screen.getByRole('button', { name: '抓取正文' }))
    await waitFor(() => expect(api.executePersonalResearch).toHaveBeenCalledTimes(2))
    expect(api.executePersonalResearch.mock.calls[1]).toEqual(['capture', { id: notebookId, url: 'https://example.com/new-report', max_age_days: 7, request_id: expect.any(String) }])
  })

  it('imports a precise artifact reference instead of accepting copied source text', async () => {
    const { api } = await ready()
    fireEvent.click(screen.getByRole('button', { name: '添加来源' })); fireEvent.click(screen.getByRole('button', { name: '资料', exact: true }))
    fireEvent.change(screen.getByLabelText('研究资料编号'), { target: { value: artifactId } })
    fireEvent.change(screen.getByLabelText('研究资料版本'), { target: { value: '4' } })
    fireEvent.change(screen.getByLabelText('研究资料摘要'), { target: { value: hash } })
    fireEvent.click(screen.getByRole('button', { name: '引用确切版本' }))
    await waitFor(() => expect(api.executePersonalResearch).toHaveBeenCalledTimes(1))
    expect(api.executePersonalResearch.mock.calls[0][1]).toMatchObject({ id: notebookId, artifact_ref: { artifact_id: artifactId, version: 4, sha256: hash, grant_id: '', grant_revision: 0 }, max_age_days: 30 })
    expect(api.executePersonalResearch.mock.calls[0][1]).not.toHaveProperty('body')
  })

  it('suppresses an unavailable source body even if a response includes stale private bytes', async () => {
    const api = apiMock(); api.getPersonalResearchSource.mockResolvedValueOnce({ source: { ...source, status: 'unavailable' }, availability: 'source_unavailable', body: 'STALE_PRIVATE_SOURCE_BODY' })
    await ready(api); fireEvent.click(screen.getByRole('button', { name: /十月月报/ }))
    await screen.findByText('原正文暂不可用')
    expect(screen.queryByText('STALE_PRIVATE_SOURCE_BODY')).not.toBeInTheDocument(); expect(screen.queryByLabelText('引用原文')).not.toBeInTheDocument()
  })

  it.each(['failed', 'expired'] as const)('keeps the notebook draft editable and permits exact recheck for an ordinary %s source', async kind => {
    const api = apiMock()
    api.getPersonalResearchSource.mockResolvedValueOnce({ source: { ...source, status: kind === 'failed' ? 'unavailable' : 'available', expires_at: '2026-11-05T02:00:00Z' },
      availability: kind === 'failed' ? 'available' : 'source_stale', body: null })
    await ready(api)
    fireEvent.change(screen.getByLabelText('研究标题'), { target: { value: '保留的未保存研究' } })
    fireEvent.click(screen.getByRole('button', { name: /十月月报/ }))
    await screen.findByText(kind === 'failed' ? '原正文暂不可用' : '原来源已变化')
    expect(screen.getByLabelText('研究标题')).toHaveValue('保留的未保存研究')
    expect(screen.getByLabelText('研究标题')).not.toHaveAttribute('readonly')
    expect(screen.queryByLabelText('引用原文')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '复查', exact: true }))
    await waitFor(() => expect(api.executePersonalResearch).toHaveBeenCalledTimes(1))
    expect(api.executePersonalResearch.mock.calls[0]).toEqual(['recheck', { source_id: sourceId, expected_revision: 2, request_id: expect.any(String) }])
    await waitFor(() => expect(localStorage.getItem(researchPendingKey('research-A'))).toBeNull())
    expect(screen.getByLabelText('研究标题')).toHaveValue('保留的未保存研究')
  })

  it('scrubs the historical notebook and an open history dialog after opaque source authorization denial', async () => {
    const api = apiMock(), gate = deferred<{ source: ResearchSource; body: string; availability: 'source_unavailable' }>()
    api.getPersonalResearch.mockImplementation(async (_id: string, revision?: number) => revision ? view({ notebook: { ...view().notebook, revision: 1,
      title: 'HISTORICAL_PRIVATE_TITLE', claims: [{ ...claim, text: 'HISTORICAL_PRIVATE_CLAIM' }] } }) : view())
    api.getPersonalResearchSource.mockReturnValueOnce(gate.promise)
    await ready(api)
    fireEvent.click(screen.getByRole('button', { name: '打开研究历史' }))
    fireEvent.click(await screen.findByRole('button', { name: /v1.*最初收入研究/ }))
    await waitFor(() => expect(screen.getByLabelText('研究标题')).toHaveValue('HISTORICAL_PRIVATE_TITLE'))
    fireEvent.click(screen.getByRole('button', { name: /十月月报/ }))
    fireEvent.click(screen.getByRole('button', { name: '打开研究历史' }))
    await screen.findByRole('dialog', { name: '研究历史' })
    await act(async () => { gate.resolve({ source: { ...source, status: 'unavailable' }, body: 'REVOKED_PRIVATE_BODY', availability: 'source_unavailable' }); await gate.promise })
    await screen.findByText('原研究来源暂不可用')
    expect(screen.queryByLabelText('研究标题')).not.toBeInTheDocument()
    expect(screen.queryByText('HISTORICAL_PRIVATE_CLAIM')).not.toBeInTheDocument()
    expect(screen.queryByDisplayValue('HISTORICAL_PRIVATE_TITLE')).not.toBeInTheDocument()
    expect(screen.queryByText('REVOKED_PRIVATE_BODY')).not.toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: '研究历史' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /v1.*最初收入研究/ })).not.toBeInTheDocument()
  })

  it('acknowledges an accepted unavailable capture without restoring private claims or keeping UNKNOWN', async () => {
    const api = apiMock(); api.executePersonalResearch.mockResolvedValueOnce(view({ availability: 'source_unavailable', notebook: { ...view().notebook, title: 'OLD_PRIVATE_TITLE', claims: [{ ...claim, text: 'OLD_PRIVATE_CLAIM' }] } }))
    await ready(api); fireEvent.click(screen.getByRole('button', { name: '添加来源' }))
    fireEvent.change(screen.getByLabelText('来源地址'), { target: { value: 'https://example.com/temporarily-unavailable' } })
    fireEvent.click(screen.getByRole('button', { name: '抓取正文' }))
    await screen.findByText('操作已受理，原来源暂待核对')
    expect(localStorage.getItem(researchPendingKey('research-A'))).toBeNull(); expect(screen.queryByText('OLD_PRIVATE_CLAIM')).not.toBeInTheDocument(); expect(screen.queryByDisplayValue('OLD_PRIVATE_TITLE')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '重试原请求' })).not.toBeInTheDocument()
  })

  it('renders expired and upstream-unverified source state without calling them verified', async () => {
    const initial = view({ sources: [{ ...source, expires_at: '2026-11-05T02:00:00Z' }, { ...source, id: `rs_${'4'.repeat(32)}`, title: '上游转述', upstream_unverified: true }] })
    await ready(apiMock(initial))
    expect(screen.getByText('已过期')).toBeInTheDocument(); expect(screen.getByText('上游未核实')).toBeInTheDocument(); expect(screen.queryByText('已验证')).not.toBeInTheDocument()
  })

  it('exports the exact saved revision and review digest, then opens the actual artifact', async () => {
    const initial = view({ review: { status: 'mechanically_checked', semantic_verification: 'not_performed', findings: [], anchors: [], digest: hash } })
    const { api, onOpenArtifact } = await ready(apiMock(initial))
    fireEvent.click(screen.getByRole('button', { name: '导出', exact: true }))
    await screen.findByRole('button', { name: /打开报告/ })
    expect(api.executePersonalResearch.mock.calls[0]).toEqual(['publish', { id: notebookId, expected_revision: 3, expected_review_digest: hash, request_id: expect.any(String) }])
    fireEvent.click(screen.getByRole('button', { name: /打开报告/ })); expect(onOpenArtifact).toHaveBeenCalledWith(artifactId, 2)
  })
})

describe('research lifecycle and navigation fences', () => {
  it('creates a real named notebook through the shared operation API', async () => {
    const { api } = await ready()
    fireEvent.click(screen.getByRole('button', { name: '新建研究', exact: true }))
    const dialog = screen.getByRole('dialog', { name: '新建研究' })
    fireEvent.change(within(dialog).getByLabelText('新研究标题'), { target: { value: '季度竞品研究' } })
    fireEvent.change(within(dialog).getByLabelText('新研究问题'), { target: { value: '产品定价有哪些差异？' } })
    fireEvent.change(within(dialog).getByLabelText('新研究类型'), { target: { value: 'competitor' } })
    fireEvent.click(within(dialog).getByRole('button', { name: '创建' }))
    await waitFor(() => expect(screen.getByLabelText('研究标题')).toHaveValue('季度竞品研究'))
    expect(api.executePersonalResearch.mock.calls[0]).toEqual(['create', { title: '季度竞品研究', question: '产品定价有哪些差异？', domain: 'competitor', request_id: expect.any(String) }])
    expect(screen.queryByRole('dialog', { name: '新建研究' })).not.toBeInTheDocument()
  })

  it('protects unsaved edits before closing and really discards them when confirmed', async () => {
    const { onClose } = await ready()
    fireEvent.change(screen.getByLabelText('研究标题'), { target: { value: '未保存的标题' } })
    fireEvent.click(screen.getByRole('button', { name: '关闭研究工作台' }))
    expect(onClose).not.toHaveBeenCalled(); expect(screen.getByRole('dialog', { name: '放弃未保存的修改？' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '继续编辑' })); expect(screen.getByLabelText('研究标题')).toHaveValue('未保存的标题')
    fireEvent.click(screen.getByRole('button', { name: '关闭研究工作台' })); fireEvent.click(screen.getByRole('button', { name: '放弃修改' }))
    expect(onClose).toHaveBeenCalledTimes(1); expect(screen.getByLabelText('研究标题')).toHaveValue('收入研究')
  })

  it('loads an exact historical version in read-only mode and returns to current identity', async () => {
    const api = apiMock(); api.getPersonalResearch.mockImplementation(async (_id: string, revision?: number) => revision ? view({ notebook: { ...view().notebook, revision: 1, title: '最初收入研究', claims: [] } }) : view())
    await ready(api)
    fireEvent.click(screen.getByRole('button', { name: '打开研究历史' })); await screen.findByRole('dialog', { name: '研究历史' })
    fireEvent.click(await screen.findByRole('button', { name: /v1.*最初收入研究/ }))
    await waitFor(() => expect(screen.getByLabelText('研究标题')).toHaveValue('最初收入研究'))
    expect(screen.getByLabelText('研究标题')).toHaveAttribute('readonly'); expect(screen.queryByRole('button', { name: '保存', exact: true })).not.toBeInTheDocument()
    expect(api.getPersonalResearch).toHaveBeenCalledWith(notebookId, 1, expect.any(AbortSignal))
    fireEvent.click(screen.getByRole('button', { name: '返回当前版本' })); expect(screen.getByLabelText('研究标题')).toHaveValue('收入研究')
    expect(api.executePersonalResearch).not.toHaveBeenCalled()
  })

  it('preserves the dirty draft on CAS conflict and makes loading the current version explicit', async () => {
    const api = apiMock(); api.executePersonalResearch.mockRejectedValueOnce(failure(409))
    api.getPersonalResearch.mockResolvedValueOnce(view()).mockResolvedValue(view({ notebook: { ...view().notebook, revision: 4, title: '其他窗口的新标题' } }))
    await ready(api)
    fireEvent.change(screen.getByLabelText('研究标题'), { target: { value: '原窗口草稿' } }); fireEvent.click(screen.getByRole('button', { name: '保存', exact: true }))
    await screen.findByText('草稿基于 v3 · 当前 v4')
    expect(screen.getByLabelText('研究标题')).toHaveValue('原窗口草稿'); expect(localStorage.getItem(researchPendingKey('research-A'))).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '载入最新版本' })); fireEvent.click(screen.getByRole('button', { name: '放弃修改' }))
    expect(screen.getByLabelText('研究标题')).toHaveValue('其他窗口的新标题')
  })

  it('aborts old-source reads and ignores late private responses after a namespace change', async () => {
    const api = apiMock(), gate = deferred<{ source: ResearchSource; body: string; availability: 'available' }>()
    api.getPersonalResearchSource.mockImplementationOnce((_id: string, _revision: number | undefined, signal?: AbortSignal) => { expect(signal).toBeInstanceOf(AbortSignal); return gate.promise })
    const rendered = await ready(api)
    fireEvent.click(screen.getByRole('button', { name: /十月月报/ })); await waitFor(() => expect(api.getPersonalResearchSource).toHaveBeenCalledTimes(1))
    const oldSignal = api.getPersonalResearchSource.mock.calls[0][2]!
    api.listPersonalResearch.mockResolvedValue({ notebooks: [], next_after: null })
    rendered.rerender(<ResearchWorkspace api={api} namespace="research-B" connected onClose={rendered.onClose} />)
    expect(oldSignal.aborted).toBe(true)
    await act(async () => { gate.resolve({ source, body: 'OLD_NAMESPACE_PRIVATE_BODY', availability: 'available' }); await gate.promise })
    expect(screen.queryByText('OLD_NAMESPACE_PRIVATE_BODY')).not.toBeInTheDocument(); expect(screen.queryByLabelText('研究标题')).not.toBeInTheDocument()
  })

  it('does not cancel a submitted mutation on disconnect or clear its original pending identity', async () => {
    const api = apiMock(), gate = deferred<ResearchResult>(); api.executePersonalResearch.mockImplementationOnce(() => gate.promise)
    const rendered = await ready(api)
    fireEvent.change(screen.getByLabelText('研究标题'), { target: { value: '正在提交的原草稿' } }); fireEvent.click(screen.getByRole('button', { name: '保存', exact: true }))
    await waitFor(() => expect(api.executePersonalResearch).toHaveBeenCalledTimes(1))
    const saved = parseResearchPending(localStorage.getItem(researchPendingKey('research-A'))!)
    rendered.rerender(<ResearchWorkspace api={api} namespace="research-A" connected={false} onClose={rendered.onClose} />)
    expect(screen.queryByLabelText('研究标题')).not.toBeInTheDocument()
    await act(async () => { gate.resolve(view({ notebook: { ...view().notebook, title: '正在提交的原草稿', revision: 4 } })); await gate.promise })
    expect(parseResearchPending(localStorage.getItem(researchPendingKey('research-A'))!).request_id).toBe(saved.request_id)
    expect(api.executePersonalResearch).toHaveBeenCalledTimes(1)
    expect(api.executePersonalResearch.mock.calls[0]).toHaveLength(2)
  })
})
