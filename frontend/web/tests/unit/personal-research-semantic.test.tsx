import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ComponentProps } from 'react'
import type { PersonalAgent } from '../../../shared/src/api/WorkbenchRuntimeClient'
import { WorkbenchRequestError, WorkbenchRuntimeClient } from '../../../shared/src/api/WorkbenchRuntimeClient'
import type { ResearchApi, ResearchView } from '../../../shared/src/api/personalResearch'
import type { ResearchSemanticApi, ResearchSemanticPending, ResearchSemanticPreview, ResearchSemanticReview, ResearchSemanticStart } from '../../../shared/src/api/personalResearchSemantic'
import { ResearchSemantic } from '../../../web2/src/personal/research/ResearchSemantic'
import { ResearchWorkspace } from '../../../web2/src/personal/research/ResearchWorkspace'
import { parseResearchSemanticPending, researchSemanticError, researchSemanticPendingKey, useResearchSemantic, validateResearchSemanticPreview, validateResearchSemanticReview, type ResearchSemanticTarget } from '../../../web2/src/personal/research/useResearchSemantic'

const workspace = 'a'.repeat(64), bodyHash = 'b'.repeat(64), digest = 'c'.repeat(64), resultDigest = 'd'.repeat(64)
const author = 'main', reviewerId = `pa_${'e'.repeat(32)}`, notebook = `rq_${'f'.repeat(32)}`, source = `rs_${'1'.repeat(32)}`
const reviewId = `rsv_${'2'.repeat(32)}`, artifact = `art_${'3'.repeat(32)}`, requestId = '4'.repeat(32)
const session = 'actual-author-session-42', namespace = 'semantic-A'
const target: ResearchSemanticTarget = { workspaceKey: workspace, authorId: author, notebookId: notebook, revision: 2, sourceSessionId: session, claimIds: ['c_one'] }
const reviewer = { id: reviewerId, version: 3, name: 'Mira' }
const key = researchSemanticPendingKey(namespace, target)
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T
const deferred = <T,>() => { let resolve!: (value: T) => void, reject!: (error: unknown) => void; const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const failure = (status: number, code = '') => Object.assign(new Error('PRIVATE_BACKEND_ERROR'), { status, code })
const agent = (changes: Partial<PersonalAgent> = {}): PersonalAgent => ({ ...reviewer, description: '', instructions: '', model_tier: 'capable', tool_names: [], kind: 'specialist', member_ids: [], status: 'active', created_at: '', updated_at: '', ...changes })
const pending = (changes: Partial<ResearchSemanticPending> = {}): ResearchSemanticPending => ({ version: 1, operation: 'semantic_start', request_id: requestId, workspace_key: workspace, author_id: author, notebook_id: notebook, notebook_revision: 2, source_session_id: session, reviewer_id: reviewerId, reviewer_version: 3, review_id: '', ...changes })
function preview(changes: Partial<ResearchSemanticPreview> = {}): ResearchSemanticPreview {
  return { workspace_key: workspace, author_id: author, notebook_id: notebook, notebook_revision: 2, source_session_id: session, reviewer, input_digest: digest,
    sources: [{ id: source, revision: 7, title: 'PRIVATE_EXACT_SOURCE', body_sha256: bodyHash, bytes: 14 }],
    materials: [{ artifact_id: artifact, version: 4, title: 'PRIVATE_EXACT_MATERIAL', owner_id: author, sharing: 'requires_owner_confirmation' }], memory_count: 1, can_start: true, blocking_codes: [], ...changes }
}
function review(changes: Partial<ResearchSemanticReview> = {}): ResearchSemanticReview {
  return { id: reviewId, request_id: requestId, workspace_key: workspace, author_id: author, notebook_id: notebook, notebook_revision: 2, input_digest: digest, reviewer,
    collaboration_id: 'pc_original', step_id: 'review', job_id: 'job_original', session_id: 'review-session-original', run_id: 'run_original', execution_status: 'completed', basis_status: 'current',
    result: { digest: resultDigest, verification_scope: 'provided_sources', independently_reviewed: true, summary: 'PRIVATE_RESULT_SUMMARY', verdicts: [{ claim_id: 'c_one', verdict: 'supported', reason: 'PRIVATE_CLAIM_REASON',
      evidence: [{ source_id: source, source_revision: 7, body_sha256: bodyHash, quote: '证据🙂', start: 5, end: 8, start_byte: 11, end_byte: 21 }] }] }, ...changes }
}
function apiMock() {
  return {
    getPersonalResearchSemanticContext: vi.fn().mockResolvedValue(preview()),
    listPersonalResearchSemanticReviews: vi.fn().mockResolvedValue({ reviews: [] }),
    getPersonalResearchSemanticReview: vi.fn().mockResolvedValue(review()),
    getPersonalResearchSemanticRequest: vi.fn().mockResolvedValue({ status: 'unknown', request_id: requestId }),
    startPersonalResearchSemanticReview: vi.fn().mockImplementation(async (body: ResearchSemanticStart) => review({ request_id: body.request_id })),
    operatePersonalResearchSemanticReview: vi.fn().mockResolvedValue(review()),
  } satisfies ResearchSemanticApi
}
async function ready(api = apiMock(), overrides: Partial<ComponentProps<typeof ResearchSemantic>> = {}) {
  const props = { api, namespace, target, agents: [agent()], connected: true, ...overrides }
  const mounted = render(<ResearchSemantic {...props} />)
  await act(async () => { await Promise.resolve(); await Promise.resolve() })
  return { ...mounted, api, props }
}
async function composer() {
  fireEvent.click(screen.getByRole('button', { name: '发起复核', exact: true }))
  const dialog = screen.getByRole('dialog', { name: '独立 Agent 复核' })
  fireEvent.change(within(dialog).getByRole('combobox', { name: '独立复核 Agent' }), { target: { value: reviewerId } })
  await within(dialog).findByText('PRIVATE_EXACT_SOURCE')
  await waitFor(() => expect(within(dialog).getByRole('button', { name: '发起复核', exact: true })).toBeEnabled())
  return dialog
}
function confirm(dialog: HTMLElement) { fireEvent.click(within(dialog).getByRole('button', { name: '发起复核', exact: true })); return within(dialog).getByRole('button', { name: '确认复核' }) }

beforeEach(() => { localStorage.clear() })
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('semantic research exact contracts', () => {
  it('persists only opaque identities and scopes by actual author, revision, Session and namespace', () => {
    expect(parseResearchSemanticPending(JSON.stringify(pending()))).toEqual(pending())
    for (const extra of [{ expected_input_digest: digest }, { claims: ['PRIVATE'] }, { quote: 'PRIVATE' }, { body: 'PRIVATE' }]) expect(() => parseResearchSemanticPending(JSON.stringify({ ...pending(), ...extra }))).toThrow('身份')
    for (const changes of [{ notebook_revision: 0 }, { reviewer_id: 'main' }, { request_id: 'other' }, { operation: 'semantic_cancel', review_id: '' }]) expect(() => parseResearchSemanticPending(JSON.stringify({ ...pending(), ...changes }))).toThrow('身份')
    expect(() => parseResearchSemanticPending('not json')).toThrow('身份')
    for (const changed of [{ ...target, authorId: reviewerId }, { ...target, revision: 3 }, { ...target, sourceSessionId: 'other' }, { ...target, workspaceKey: 'f'.repeat(64) }]) expect(researchSemanticPendingKey(namespace, changed)).not.toBe(key)
    expect(researchSemanticPendingKey('semantic-B', target)).not.toBe(key)
  })
  it('validates the exact selected reviewer version, source and sharing preview', () => {
    expect(validateResearchSemanticPreview(preview(), target, reviewer)).toEqual(preview())
    for (const changes of [{ workspace_key: 'f'.repeat(64) }, { author_id: reviewerId }, { notebook_revision: 3 }, { source_session_id: 'other' }, { reviewer: { ...reviewer, version: 4 } }, { input_digest: 'bad' }]) expect(() => validateResearchSemanticPreview(preview(changes), target, reviewer)).toThrow('不一致')
    const bad = preview(); bad.sources[0].revision = 0
    expect(() => validateResearchSemanticPreview(bad, target, reviewer)).toThrow('来源')
    const duplicate = preview(); duplicate.sources.push(clone(duplicate.sources[0]))
    expect(() => validateResearchSemanticPreview(duplicate, target, reviewer)).toThrow('来源')
    const missing = preview(); missing.materials[0].sharing = 'missing_owner_grant'
    expect(() => validateResearchSemanticPreview(missing, target, reviewer)).toThrow('授权')
  })
  it('requires exact claim coverage, grounded evidence and UTF-8 and character offsets', () => {
    expect(validateResearchSemanticReview(review(), target)).toEqual(review())
    const badClaims = [[], [review().result!.verdicts[0], review().result!.verdicts[0]], [{ ...review().result!.verdicts[0], claim_id: 'c_other' }]]
    for (const rows of badClaims) { const bad = review(); bad.result!.verdicts = rows; expect(() => validateResearchSemanticReview(bad, target)).toThrow() }
    for (const patch of [{ end: 7 }, { end_byte: 20 }, { source_revision: 0 }, { body_sha256: 'bad' }]) { const bad = review(); Object.assign(bad.result!.verdicts[0].evidence[0], patch); expect(() => validateResearchSemanticReview(bad, target)).toThrow('位置') }
    const ungrounded = review(); ungrounded.result!.verdicts[0].evidence = []
    expect(() => validateResearchSemanticReview(ungrounded, target)).toThrow('覆盖')
    ungrounded.result!.verdicts[0].verdict = 'insufficient'
    expect(validateResearchSemanticReview(ungrounded, target).result!.verdicts[0].verdict).toBe('insufficient')
  })
  it('rejects mismatched execution receipts and strips unavailable private results', () => {
    for (const changes of [{ id: 'rsv_bad' }, { request_id: 'bad' }, { notebook_id: `rq_${'9'.repeat(32)}` }, { reviewer: { ...reviewer, version: 4 } }, { run_id: '' }, { run_id: 'other run' }, { step_id: 'draft' as 'review' }]) expect(() => validateResearchSemanticReview(review(changes), target, reviewId, reviewer, digest)).toThrow()
    expect(() => validateResearchSemanticReview(review(), target, `rsv_${'9'.repeat(32)}`)).toThrow()
    expect(() => validateResearchSemanticReview(review(), target, undefined, undefined, bodyHash)).toThrow()
    expect(validateResearchSemanticReview(review({ basis_status: 'unavailable' }), target).result).toBeUndefined()
    expect(JSON.stringify(validateResearchSemanticReview(review({ basis_status: 'unavailable' }), target))).not.toContain('PRIVATE_')
    for (const changes of [{ run_id: 'other-run' }, { job_id: 'other-job' }, { session_id: 'other-session' }, { collaboration_id: 'other-collaboration' }, { request_id: '9'.repeat(32) }, { reviewer: { ...reviewer, version: 4 } }]) expect(() => validateResearchSemanticReview(review(changes), target, undefined, undefined, undefined, review())).toThrow('身份')
  })
  it('maps technical errors without exposing raw server content', () => {
    for (const code of ['source_stale', 'version_conflict', 'source_unavailable', 'artifact_access_revoked', 'scope_denied', 'research_busy', 'invalid_input']) {
      expect(researchSemanticError(failure(409, code))).not.toContain('PRIVATE_'); expect(researchSemanticError(failure(409, code))).not.toContain(code)
    }
    expect(researchSemanticError(new Error('PRIVATE_BODY'))).not.toContain('PRIVATE')
  })
  it('uses authenticated no-store endpoints and distinct explicit POST operations', async () => {
    const fetcher = vi.fn().mockImplementation(async () => new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetcher)
    const client = new WorkbenchRuntimeClient('http://local-test/api/v1', async () => 'fixture-token')
    const body: ResearchSemanticStart = { id: notebook, source_session_id: session, reviewer_agent_id: reviewerId, expected_reviewer_version: 3, expected_revision: 2, expected_input_digest: digest, request_id: requestId }
    await client.getPersonalResearchSemanticContext(notebook, 'session/with spaces', reviewerId)
    await client.listPersonalResearchSemanticReviews(notebook); await client.getPersonalResearchSemanticReview(reviewId); await client.getPersonalResearchSemanticRequest(requestId)
    await client.startPersonalResearchSemanticReview(body); await client.operatePersonalResearchSemanticReview('semantic_reconcile', { id: reviewId, request_id: requestId }); await client.operatePersonalResearchSemanticReview('semantic_cancel', { id: reviewId, request_id: requestId })
    expect(fetcher.mock.calls.map(row => row[0])).toEqual([
      `http://local-test/api/v1/personal/research/${notebook}/semantic-context?source_session_id=session%2Fwith+spaces&reviewer_agent_id=${reviewerId}`,
      `http://local-test/api/v1/personal/research/${notebook}/semantic-reviews`, `http://local-test/api/v1/personal/research/semantic-reviews/${reviewId}`, `http://local-test/api/v1/personal/research/semantic-requests/${requestId}`,
      'http://local-test/api/v1/personal/research/operations/semantic_start', 'http://local-test/api/v1/personal/research/operations/semantic_reconcile', 'http://local-test/api/v1/personal/research/operations/semantic_cancel',
    ])
    expect(fetcher.mock.calls.every(row => row[1].cache === 'no-store' && row[1].headers.Authorization === 'Bearer fixture-token')).toBe(true)
    expect(fetcher.mock.calls.slice(0, 4).every(row => !row[1].body)).toBe(true)
    expect(JSON.parse(fetcher.mock.calls[4][1].body)).toEqual(body)
    expect(fetcher.mock.calls.slice(4).every(row => row[1].method === 'POST' && row[1].signal instanceof AbortSignal)).toBe(true)
  })
})

describe('explicit independent Agent review UI', () => {
  it('offers only actual active leaf Agents and never creates one or starts automatically', async () => {
    const ownTarget = { ...target, authorId: `pa_${'5'.repeat(32)}` }
    const { api } = await ready(apiMock(), { target: ownTarget, agents: [agent(), agent({ id: ownTarget.authorId, name: '作者' }), agent({ id: `pa_${'6'.repeat(32)}`, kind: 'team', name: '团队' }), agent({ id: `pa_${'7'.repeat(32)}`, status: 'paused', name: '暂停' })] })
    fireEvent.click(screen.getByRole('button', { name: '发起复核', exact: true }))
    expect(screen.getAllByRole('option').map(row => row.textContent)).toEqual(['选择 Agent', 'Mira · v3'])
    expect(api.getPersonalResearchSemanticContext).not.toHaveBeenCalled(); expect(api.startPersonalResearchSemanticReview).not.toHaveBeenCalled(); expect(localStorage.length).toBe(0)
  })
  it('shows the empty real catalog without inventing reviewer roles', async () => {
    await ready(apiMock(), { agents: [] }); fireEvent.click(screen.getByRole('button', { name: '发起复核', exact: true }))
    expect(screen.getByRole('option', { name: '暂无可用 Agent' })).toBeInTheDocument()
    expect(within(screen.getByRole('dialog')).getByRole('button', { name: '发起复核', exact: true })).toBeDisabled()
  })
  it('previews recipient and exact sharing before a second explicit confirmation and one POST', async () => {
    const api = apiMock(), held = deferred<ResearchSemanticReview>(); api.startPersonalResearchSemanticReview.mockImplementation(() => held.promise)
    await ready(api); const dialog = await composer()
    expect(api.getPersonalResearchSemanticContext).toHaveBeenCalledWith(notebook, session, reviewerId, expect.any(AbortSignal))
    expect(within(dialog).getByText('PRIVATE_EXACT_MATERIAL')).toBeInTheDocument(); expect(within(dialog).getByText('确认后共享')).toBeInTheDocument()
    const button = confirm(dialog)
    expect(screen.getByRole('group', { name: '确认独立 Agent 复核' })).toHaveTextContent('Mira')
    expect(api.startPersonalResearchSemanticReview).not.toHaveBeenCalled()
    fireEvent.click(button); fireEvent.click(button)
    await waitFor(() => expect(api.startPersonalResearchSemanticReview).toHaveBeenCalledTimes(1))
    const body = api.startPersonalResearchSemanticReview.mock.calls[0][0]
    expect(body).toEqual({ id: notebook, source_session_id: session, reviewer_agent_id: reviewerId, expected_reviewer_version: 3, expected_revision: 2, expected_input_digest: digest, request_id: expect.stringMatching(/^[a-f0-9]{32}$/) })
    const stored = localStorage.getItem(key)!
    expect(parseResearchSemanticPending(stored).request_id).toBe(body.request_id)
    for (const secret of ['PRIVATE_', digest, bodyHash, source, artifact]) expect(stored).not.toContain(secret)
    await act(async () => { held.resolve(review({ request_id: body.request_id })); await held.promise })
    await waitFor(() => expect(localStorage.getItem(key)).toBeNull())
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument(); expect(screen.getByText('证据🙂')).toBeInTheDocument()
  })
  it('shows foreign-owner sharing blocks and never POSTs', async () => {
    const api = apiMock(), blocked = preview({ can_start: false, blocking_codes: ['missing_owner_grant'] }); blocked.materials[0].owner_id = reviewerId; blocked.materials[0].sharing = 'missing_owner_grant'
    api.getPersonalResearchSemanticContext.mockResolvedValue(blocked)
    await ready(api); fireEvent.click(screen.getByRole('button', { name: '发起复核', exact: true })); fireEvent.change(screen.getByRole('combobox'), { target: { value: reviewerId } })
    await screen.findByText('需所有者授权')
    expect(within(screen.getByRole('dialog')).getByRole('button', { name: '发起复核', exact: true })).toBeDisabled(); expect(api.startPersonalResearchSemanticReview).not.toHaveBeenCalled()
  })
  it('renders individual verdicts, exact provenance and stale scope without blanket fact verification', async () => {
    const api = apiMock(), open = vi.fn(); api.listPersonalResearchSemanticReviews.mockResolvedValue({ reviews: [review({ basis_status: 'stale' })] })
    await ready(api, { onOpenExecution: open }); await screen.findByText('PRIVATE_RESULT_SUMMARY')
    expect(screen.getByText('原依据已变化')).toBeInTheDocument(); expect(screen.getByText('给定来源复核')).toBeInTheDocument(); expect(screen.getByText('来源支持')).toBeInTheDocument()
    expect(screen.getByText('字符 5–8')).toBeInTheDocument(); expect(screen.getByText('字节 11–21')).toBeInTheDocument(); expect(screen.getByText(bodyHash)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '查看原执行' })); expect(open).toHaveBeenCalledWith('review-session-original', 'run_original')
    expect(screen.queryByText('事实已验证')).not.toBeInTheDocument()
  })
  it('removes private result bodies when the original basis is unavailable', async () => {
    const api = apiMock(); api.listPersonalResearchSemanticReviews.mockResolvedValue({ reviews: [review({ basis_status: 'unavailable' })] })
    await ready(api); await screen.findByText('原依据不可用')
    expect(screen.queryByText('PRIVATE_RESULT_SUMMARY')).not.toBeInTheDocument(); expect(screen.queryByText('证据🙂')).not.toBeInTheDocument()
  })
  it('requires explicit cancellation and preserves exact original execution identity', async () => {
    const api = apiMock(), running = review({ execution_status: 'running', result: undefined }); api.listPersonalResearchSemanticReviews.mockResolvedValue({ reviews: [running] }); api.operatePersonalResearchSemanticReview.mockResolvedValue(review({ execution_status: 'cancelled', result: undefined }))
    await ready(api); fireEvent.click(await screen.findByRole('button', { name: '停止复核' }))
    const dialog = screen.getByRole('dialog', { name: '停止这次复核？' }); expect(api.operatePersonalResearchSemanticReview).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: '停止复核' }))
    await waitFor(() => expect(api.operatePersonalResearchSemanticReview).toHaveBeenCalledTimes(1))
    expect(api.operatePersonalResearchSemanticReview.mock.calls[0][0]).toBe('semantic_cancel'); expect(api.operatePersonalResearchSemanticReview.mock.calls[0][1].id).toBe(reviewId)
    expect(api.startPersonalResearchSemanticReview).not.toHaveBeenCalled(); await screen.findByText('已停止', { selector: '.pres-semantic-status' })
  })
  it('reconciles UNKNOWN only through an explicit original execution POST', async () => {
    const api = apiMock(); api.listPersonalResearchSemanticReviews.mockResolvedValue({ reviews: [review({ execution_status: 'unknown', result: undefined })] })
    await ready(api); const button = await screen.findByRole('button', { name: '核对原执行' })
    expect(api.operatePersonalResearchSemanticReview).not.toHaveBeenCalled(); expect(api.startPersonalResearchSemanticReview).not.toHaveBeenCalled()
    fireEvent.click(button); await waitFor(() => expect(api.operatePersonalResearchSemanticReview).toHaveBeenCalledTimes(1))
    expect(api.operatePersonalResearchSemanticReview.mock.calls[0]).toEqual(['semantic_reconcile', { id: reviewId, request_id: expect.stringMatching(/^[a-f0-9]{32}$/) }])
  })
  it('keeps two-step confirmation scoped to the actual reviewer version', async () => {
    const mounted = await ready(); const dialog = await composer(); confirm(dialog)
    mounted.rerender(<ResearchSemantic {...mounted.props} agents={[agent({ version: 4 })]} />)
    expect(screen.queryByRole('button', { name: '确认复核' })).not.toBeInTheDocument(); expect(mounted.api.startPersonalResearchSemanticReview).not.toHaveBeenCalled()
  })
  it('does not start from disconnected, readonly or unmatched-session views', async () => {
    const mounted = await ready(apiMock(), { writable: false })
    expect(screen.getByRole('button', { name: '发起复核', exact: true })).toBeDisabled()
    mounted.rerender(<ResearchSemantic {...mounted.props} writable target={{ ...target, sourceSessionId: '' }} />)
    expect(screen.getByRole('button', { name: '发起复核', exact: true })).toBeDisabled()
    mounted.rerender(<ResearchSemantic {...mounted.props} writable connected={false} />)
    expect(screen.getByRole('button', { name: '发起复核', exact: true })).toBeDisabled(); expect(mounted.api.startPersonalResearchSemanticReview).not.toHaveBeenCalled()
  })
})

describe('semantic request recovery and response fencing', () => {
  it('recovers timeout by original GET on remount without a second start POST', async () => {
    const api = apiMock(); api.startPersonalResearchSemanticReview.mockRejectedValue(new Error('timeout'))
    const mounted = await ready(api); fireEvent.click(confirm(await composer()))
    await waitFor(() => expect(api.startPersonalResearchSemanticReview).toHaveBeenCalledTimes(1))
    const body = api.startPersonalResearchSemanticReview.mock.calls[0][0]
    await waitFor(() => expect(screen.getAllByText('原复核请求待核对').length).toBeGreaterThan(0))
    mounted.unmount(); api.getPersonalResearchSemanticRequest.mockResolvedValue({ status: 'completed', request_id: body.request_id, review: review({ request_id: body.request_id }) })
    render(<ResearchSemantic {...mounted.props} />)
    await screen.findByText('PRIVATE_RESULT_SUMMARY')
    expect(api.getPersonalResearchSemanticRequest).toHaveBeenCalledWith(body.request_id, expect.any(AbortSignal)); expect(api.startPersonalResearchSemanticReview).toHaveBeenCalledTimes(1); expect(localStorage.getItem(key)).toBeNull()
  })
  it('retains UNKNOWN original request and queries it again only on user action', async () => {
    const api = apiMock(); localStorage.setItem(key, JSON.stringify(pending()))
    render(<ResearchSemantic api={api} namespace={namespace} target={target} connected agents={[agent()]} />)
    await waitFor(() => expect(api.getPersonalResearchSemanticRequest).toHaveBeenCalledTimes(1))
    expect(localStorage.getItem(key)).not.toBeNull(); expect(api.startPersonalResearchSemanticReview).not.toHaveBeenCalled(); expect(api.operatePersonalResearchSemanticReview).not.toHaveBeenCalled()
    const button = screen.getByRole('button', { name: '核对原请求' }); await waitFor(() => expect(button).toBeEnabled()); fireEvent.click(button)
    await waitFor(() => expect(api.getPersonalResearchSemanticRequest).toHaveBeenCalledTimes(2))
  })
  it('keeps accepted nested error receipts pending even when the HTTP status is 4xx', async () => {
    const api = apiMock(); api.startPersonalResearchSemanticReview.mockRejectedValue(new WorkbenchRequestError('PRIVATE', 409, 'source_stale', { accepted: true }))
    await ready(api); fireEvent.click(confirm(await composer()))
    await waitFor(() => expect(screen.getByText('原复核请求待核对')).toBeInTheDocument()); expect(localStorage.getItem(key)).not.toBeNull(); expect(screen.queryByText('PRIVATE')).not.toBeInTheDocument()
  })
  it('clears only a definitively rejected original request and requires a fresh preview', async () => {
    const api = apiMock(); api.startPersonalResearchSemanticReview.mockRejectedValue(failure(409, 'source_stale'))
    await ready(api); const dialog = await composer(); fireEvent.click(confirm(dialog))
    await waitFor(() => expect(localStorage.getItem(key)).toBeNull())
    expect(within(dialog).getByRole('button', { name: '发起复核', exact: true })).toBeDisabled(); expect(api.startPersonalResearchSemanticReview).toHaveBeenCalledTimes(1)
  })
  it('does not apply a recovered review with another original start request ID', async () => {
    const api = apiMock(); localStorage.setItem(key, JSON.stringify(pending()))
    api.getPersonalResearchSemanticRequest.mockResolvedValue({ status: 'completed', request_id: requestId, review: review({ request_id: '9'.repeat(32) }) })
    render(<ResearchSemantic api={api} namespace={namespace} target={target} connected agents={[agent()]} />)
    await screen.findByText('原复核启动收据不一致'); expect(localStorage.getItem(key)).not.toBeNull(); expect(screen.queryByText('PRIVATE_RESULT_SUMMARY')).not.toBeInTheDocument()
  })
  it('blocks writes when opaque browser storage is malformed or unavailable', async () => {
    localStorage.setItem(key, JSON.stringify({ ...pending(), body: 'PRIVATE_BODY' }))
    const mounted = await ready(apiMock())
    expect(screen.getByRole('button', { name: '发起复核', exact: true })).toBeDisabled(); expect(mounted.api.getPersonalResearchSemanticRequest).not.toHaveBeenCalled()
    mounted.unmount(); localStorage.clear(); const api = apiMock(); await ready(api); const dialog = await composer()
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota') })
    fireEvent.click(confirm(dialog)); await waitFor(() => expect(screen.getAllByText('原请求状态保存失败，请核对浏览器存储').length).toBeGreaterThan(0)); expect(api.startPersonalResearchSemanticReview).not.toHaveBeenCalled()
  })
  it.each(['namespace', 'notebook', 'api', 'disconnect', 'readonly', 'reviewer-version'] as const)('fences late start responses after %s changes and retains the original slot', async changed => {
    const api = apiMock(), held = deferred<ResearchSemanticReview>(); api.startPersonalResearchSemanticReview.mockImplementation(() => held.promise)
    const mounted = await ready(api); fireEvent.click(confirm(await composer())); await waitFor(() => expect(api.startPersonalResearchSemanticReview).toHaveBeenCalledTimes(1))
    const body = api.startPersonalResearchSemanticReview.mock.calls[0][0]
    const changes = changed === 'namespace' ? { namespace: 'semantic-B' } : changed === 'notebook' ? { target: { ...target, notebookId: `rq_${'8'.repeat(32)}` } } : changed === 'api' ? { api: apiMock() } : changed === 'disconnect' ? { connected: false } : changed === 'readonly' ? { writable: false } : { agents: [agent({ version: 4 })] }
    mounted.rerender(<ResearchSemantic {...mounted.props} {...changes} />)
    await act(async () => { held.resolve(review({ request_id: body.request_id })); await held.promise })
    expect(localStorage.getItem(key)).not.toBeNull(); expect(screen.queryByText('PRIVATE_RESULT_SUMMARY')).not.toBeInTheDocument(); expect(api.startPersonalResearchSemanticReview).toHaveBeenCalledTimes(1)
  })
  it('hides old scope results synchronously while a new notebook list is pending', async () => {
    const api = apiMock(); api.listPersonalResearchSemanticReviews.mockResolvedValue({ reviews: [review()] })
    const mounted = await ready(api); await screen.findByText('PRIVATE_RESULT_SUMMARY')
    api.listPersonalResearchSemanticReviews.mockReturnValue(new Promise(() => {}))
    mounted.rerender(<ResearchSemantic {...mounted.props} target={{ ...target, notebookId: `rq_${'8'.repeat(32)}` }} />)
    expect(screen.queryByText('PRIVATE_RESULT_SUMMARY')).not.toBeInTheDocument()
  })
  it('polls known running GET state, pauses hidden tabs and stops at terminal receipt', async () => {
    vi.useFakeTimers(); const api = apiMock(), running = review({ execution_status: 'running', result: undefined })
    api.listPersonalResearchSemanticReviews.mockResolvedValue({ reviews: [running] }); api.getPersonalResearchSemanticReview.mockResolvedValue(running)
    render(<ResearchSemantic api={api} namespace={namespace} target={target} connected agents={[agent()]} />)
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    await act(async () => { await vi.advanceTimersByTimeAsync(1800) }); expect(api.getPersonalResearchSemanticReview).toHaveBeenCalledTimes(1)
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden'); fireEvent(document, new Event('visibilitychange'))
    await act(async () => { await vi.advanceTimersByTimeAsync(10000) }); expect(api.getPersonalResearchSemanticReview).toHaveBeenCalledTimes(1)
    api.getPersonalResearchSemanticReview.mockResolvedValue(review()); vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible'); fireEvent(document, new Event('visibilitychange'))
    await act(async () => { await Promise.resolve(); await Promise.resolve() }); expect(api.getPersonalResearchSemanticReview).toHaveBeenCalledTimes(2)
    await act(async () => { await vi.advanceTimersByTimeAsync(10000) }); expect(api.getPersonalResearchSemanticReview).toHaveBeenCalledTimes(2); expect(api.startPersonalResearchSemanticReview).not.toHaveBeenCalled()
  })
  it('does not use invalid preview identities or apply late preview from another reviewer', async () => {
    const api = apiMock(), held = deferred<ResearchSemanticPreview>(); api.getPersonalResearchSemanticContext.mockImplementation(() => held.promise)
    const mounted = renderHook(({ selected }) => useResearchSemantic({ api, namespace, target, reviewer: selected, connected: true }), { initialProps: { selected: reviewer } })
    await waitFor(() => expect(api.getPersonalResearchSemanticContext).toHaveBeenCalledTimes(1))
    mounted.rerender({ selected: { ...reviewer, version: 4 } })
    await act(async () => { held.resolve(preview()); await held.promise })
    expect(mounted.result.current.preview).toBeNull(); expect(mounted.result.current.canStart).toBe(false)
  })
})

describe('research workspace semantic integration', () => {
  function workspaceApi() {
    const value: ResearchView = { notebook: { id: notebook, revision: 2, title: '原研究', question: '原问题', domain: 'general', actor_id: author, workspace_key: workspace,
      claims: [{ id: 'c_one', text: '原声明', subject: '', kind: 'fact', citations: [], quantity: null, calculation: null, assumptions: [], upstream_claims: [] }], conclusion: { stance: 'watch', premises: [], invalidators: [] } }, sources: [],
      review: { semantic_verification: 'not_performed', status: 'mechanically_checked', findings: [], anchors: [] } }
    const api = Object.assign(apiMock(), {
      getPersonalResearchContext: vi.fn().mockResolvedValue({ actor_id: author, workspace_key: workspace, server_now: '2026-11-06T05:00:00Z' }),
      listPersonalResearch: vi.fn().mockResolvedValue({ notebooks: [{ ...value.notebook, source_count: 0, claim_count: 1, review_status: 'mechanically_checked' }], next_after: null }),
      getPersonalResearch: vi.fn().mockImplementation(async (_id, revision) => revision === undefined ? clone(value) : { ...clone(value), notebook: { ...clone(value.notebook), revision } }),
      executePersonalResearch: vi.fn(), getPersonalResearchSource: vi.fn(), getPersonalResearchHistory: vi.fn().mockResolvedValue({ history: [{ revision: 1, title: '旧研究', updated_at: '', claim_count: 1 }], next_after_revision: null }),
    } satisfies ResearchApi)
    return { api, value }
  }
  it('keeps mechanical review separate and blocks mismatched Session authors and dirty drafts', async () => {
    const { api, value } = workspaceApi(), props = { api, namespace, connected: true, agents: [agent()], contextTarget: { sessionId: session, actorId: reviewerId }, onClose: vi.fn() }
    const mounted = render(<ResearchWorkspace {...props} />); await screen.findByDisplayValue('原研究'); fireEvent.click(screen.getByRole('tab', { name: '复核' }))
    expect(screen.getByRole('button', { name: '发起复核', exact: true })).toBeDisabled(); expect(screen.getByText('语义待核实')).toBeInTheDocument()
    mounted.rerender(<ResearchWorkspace {...props} contextTarget={{ sessionId: session, actorId: author }} />)
    await waitFor(() => expect(screen.getByRole('button', { name: '发起复核', exact: true })).toBeEnabled())
    fireEvent.change(screen.getByRole('textbox', { name: '研究标题' }), { target: { value: '未保存研究' } })
    expect(screen.getByRole('button', { name: '发起复核', exact: true })).toBeDisabled(); expect(value.review.semantic_verification).toBe('not_performed'); expect(api.startPersonalResearchSemanticReview).not.toHaveBeenCalled()
  })
  it('reads only matching historical revision results without allowing a historical start', async () => {
    const { api } = workspaceApi(); api.listPersonalResearchSemanticReviews.mockResolvedValue({ reviews: [review({ notebook_revision: 1 })] })
    render(<ResearchWorkspace api={api} namespace={namespace} connected agents={[agent()]} contextTarget={{ sessionId: session, actorId: author }} onClose={vi.fn()} />)
    await screen.findByDisplayValue('原研究'); fireEvent.click(screen.getByRole('button', { name: '打开研究历史' })); fireEvent.click(await screen.findByRole('button', { name: /旧研究/ }))
    await waitFor(() => expect(screen.getByText('v1 · 历史版本')).toBeInTheDocument()); fireEvent.click(screen.getByRole('tab', { name: '复核' }))
    await screen.findByText('PRIVATE_RESULT_SUMMARY'); expect(screen.getByRole('button', { name: '发起复核', exact: true })).toBeDisabled(); expect(api.startPersonalResearchSemanticReview).not.toHaveBeenCalled()
  })
})
