import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ResearchApi, ResearchView } from '../../../shared/src/api/personalResearch'
import type { ResearchEgressApi, ResearchEgressDecision, ResearchEgressView } from '../../../shared/src/api/personalResearchEgress'
import { ResearchEgress, researchEgressArtifacts } from '../../../web2/src/personal/research/ResearchEgress'
import { ResearchWorkspace } from '../../../web2/src/personal/research/ResearchWorkspace'
import { parseResearchEgressPending, researchEgressError, researchEgressPendingKey, validateCapturedEgressResume, validateResearchEgress } from '../../../web2/src/personal/research/useResearchEgress'

const id = `rx_${'a'.repeat(32)}`, otherId = `rx_${'b'.repeat(32)}`, notebook = `rq_${'c'.repeat(32)}`
const actor = `pa_${'d'.repeat(32)}`, sourceId = `rs_${'e'.repeat(32)}`, artifact = `art_${'f'.repeat(32)}`
const hash = '1'.repeat(64), workspace = '2'.repeat(64), requestId = '3'.repeat(32)
const query = 'PRIVATE_QUERY_42 原资料中的增长率', reason = 'PRIVATE_REASON_42 需要外部来源比较'
const clock = '2026-11-06T05:00:00Z', future = '2099-11-06T05:30:00Z'
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T
const deferred = <T,>() => { let resolve!: (value: T) => void, reject!: (reason: unknown) => void; const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const failure = (status: number, code = '') => Object.assign(new Error('PRIVATE_ERROR_BODY'), { status, code })

function view(changes: Partial<ResearchEgressView['proposal']> = {}): ResearchEgressView {
  return { availability: 'available', proposal: { id, revision: 1, digest: hash, status: 'proposed', actor_id: actor, actor_name: 'Mira', actor_version: 2,
    session_id: 'original-session-42', notebook_id: notebook, notebook_revision: 3, notebook_title: '十月收入研究', action: 'search',
    payload: { query, provider: 'duckduckgo', max_results: 5 }, destination_plan: { kind: 'search', provider: 'duckduckgo', endpoint: 'https://html.duckduckgo.com/html/',
      method: 'POST', parameters: { q: query }, headers: { 'Accept-Encoding': 'identity' }, redirect_policy: 'none', max_redirects: 0, trust_env: false, tls_verify: true,
      request_encoding: 'form-url-encoded', routing_digest: hash }, reason, created_at: clock, expires_at: future,
    approval: null, execution: null, dependencies: { artifact_count: 2, memory_count: 1, source_count: 1, notebook_revision: 3 }, ...changes } }
}
function approved(changes: Partial<ResearchEgressView['proposal']> = {}) {
  return view({ revision: 2, status: 'approved', approval: { decision_id: 'decision_42', digest: hash, proposal_revision: 1, approved_at: clock, channel: 'web' }, ...changes })
}
function completed(): ResearchEgressView {
  const result = approved({ revision: 4, status: 'completed', action: 'capture', payload: { url: 'https://example.com/report?period=2026-10', max_age_days: 30 },
    destination_plan: { ...view().proposal.destination_plan!, kind: 'url', provider: 'direct', endpoint: 'https://example.com/report?period=2026-10', method: 'GET', parameters: {}, request_encoding: 'none' },
    execution: { request_id: 'original-execute-42', status: 'completed', started_at: clock, result: { notebook_id: notebook, notebook_revision: 3, source_id: sourceId, source_revision: 1,
      source_ref: { artifact_id: artifact, version: 1, sha256: hash, grant_id: '', grant_revision: 0 } } } })
  return result
}
function resultView(): ResearchView {
  return { notebook: { id: notebook, revision: 3, title: '十月收入研究', question: '收入变化？', domain: 'operations', claims: [], conclusion: { stance: 'undecided', premises: [], invalidators: [] }, actor_id: actor, workspace_key: workspace },
    sources: [], review: { status: 'not_reviewed', semantic_verification: 'not_performed', findings: [], anchors: [] }, availability: 'available' }
}
function apiMock(initial = view()) {
  let current = clone(initial)
  const api = {
    listPersonalResearchEgress: vi.fn().mockImplementation(async () => ({ proposals: [clone(current)], next_after: null })),
    getPersonalResearchEgress: vi.fn().mockImplementation(async () => clone(current)),
    getPersonalResearchEgressRequest: vi.fn().mockResolvedValue({ request_id: requestId, status: 'pending' }),
    operatePersonalResearchEgress: vi.fn().mockImplementation(async (operation: string, body: Record<string, unknown>) => {
      if (operation === 'egress_execute') current = completed()
      else {
        const decision = body.decision as ResearchEgressDecision
        current = decision === 'approve' ? approved() : view({ ...current.proposal, revision: current.proposal.revision + 1, status: decision === 'reject' ? 'rejected' : 'revoked' })
      }
      return clone(current)
    }),
  } satisfies ResearchEgressApi
  return { api, set: (next: ResearchEgressView) => { current = clone(next) } }
}
async function ready(mock = apiMock(), namespace = 'egress-A') {
  const onClose = vi.fn(), onOpenScopedArtifact = vi.fn()
  const rendered = render(<ResearchEgress api={mock.api} namespace={namespace} workspaceKey={workspace} serverNow={clock} connected onClose={onClose} onOpenScopedArtifact={onOpenScopedArtifact} />)
  await screen.findByRole('navigation', { name: '外发审批队列' })
  await waitFor(() => expect(within(screen.getByRole('navigation', { name: '外发审批队列' })).getByRole('button', { name: /十月收入研究/ })).toBeEnabled())
  return { ...mock, ...rendered, onClose, onOpenScopedArtifact }
}
async function select() {
  fireEvent.click(within(screen.getByRole('navigation', { name: '外发审批队列' })).getByRole('button', { name: /十月收入研究/ }))
  await screen.findByLabelText(/确切外发(检索词|网址)/)
}
function confirm(action: '批准外发' | '拒绝外发' | '撤销批准' | '执行一次') {
  return within(screen.getByRole('dialog', { name: action, exact: true }))
}

beforeEach(() => { localStorage.clear(); window.history.replaceState({}, '', '/web2?research=1') })
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('research egress exact display and owner queue', () => {
  it('maps the actual approval and execution error codes without echoing private backend content', () => {
    for (const code of ['approval_required', 'approval_invalidated', 'action_expired', 'action_locked', 'lease_lost']) {
      const message = researchEgressError(failure(409, code))
      expect(message).not.toContain('PRIVATE_ERROR_BODY'); expect(message).not.toContain(code); expect(message).not.toBe('外发操作结果待核对，请检查原请求')
    }
  })
  it('sanitizes an opaque response and rejects malformed identities, destinations and private recovery fields', () => {
    const denied = validateResearchEgress({ ...view(), availability: 'source_unavailable' })
    expect(JSON.stringify(denied)).not.toContain('PRIVATE_'); expect(denied.proposal.digest).toBe('')
    expect(() => validateResearchEgress(view(), otherId)).toThrow('身份')
    expect(() => validateResearchEgress(view({ destination_plan: { ...view().proposal.destination_plan!, redirect_policy: 'any' as 'none' } }))).toThrow('接收方')
    expect(() => parseResearchEgressPending(JSON.stringify({ version: 1, request_id: requestId, operation: 'egress_decide', proposal_id: id, workspace_key: workspace, query }))).toThrow('状态')
    expect(() => parseResearchEgressPending('broken')).toThrow('状态')
  })

  it('lists the actual proposing Agent without showing its private query or reason in the queue', async () => {
    const { api } = await ready()
    expect(screen.getByText('Mira · 外部检索')).toBeInTheDocument()
    expect(screen.queryByText(query)).not.toBeInTheDocument(); expect(screen.queryByText(reason)).not.toBeInTheDocument()
    expect(api.getPersonalResearchEgress).not.toHaveBeenCalled(); expect(api.operatePersonalResearchEgress).not.toHaveBeenCalled()
    await select()
    expect(screen.getByLabelText('确切外发检索词')).toHaveTextContent(query)
    expect(screen.getByLabelText('确切接收地址')).toHaveTextContent('https://html.duckduckgo.com/html/')
    expect(screen.getByLabelText('确切外发参数')).toHaveTextContent(query)
    expect(screen.getByText('不跟随')).toBeInTheDocument()
  })

  it('opens the all-actor approval queue even when the main Agent has no notebook', async () => {
    const mock = apiMock()
    const api = { ...mock.api, getPersonalResearchContext: vi.fn().mockResolvedValue({ actor_id: 'main', workspace_key: workspace, server_now: clock }),
      listPersonalResearch: vi.fn().mockResolvedValue({ notebooks: [], next_after: null }), getPersonalResearch: vi.fn(), executePersonalResearch: vi.fn(),
      getPersonalResearchSource: vi.fn(), getPersonalResearchHistory: vi.fn() } satisfies ResearchApi & ResearchEgressApi
    render(<ResearchWorkspace api={api} namespace="empty-main" connected onClose={vi.fn()} />)
    const open = await screen.findByRole('button', { name: '外发审批', exact: true })
    await waitFor(() => expect(open).toBeEnabled()); fireEvent.click(open)
    await screen.findByText('Mira · 外部检索')
    expect(api.getPersonalResearch).not.toHaveBeenCalled()
  })

  it('displays the complete URL with sensitive parameters and never turns it into an automatic navigation', async () => {
    const exact = 'https://example.com/report?query=PRIVATE_QUERY_42&plan=2026%2F11'
    await ready(apiMock(view({ action: 'capture', payload: { url: exact, max_age_days: 30 }, destination_plan: { ...view().proposal.destination_plan!, kind: 'url', provider: 'direct', endpoint: exact, method: 'GET', parameters: {}, request_encoding: 'none' } })))
    await select()
    expect(screen.getByLabelText('确切外发网址').textContent).toBe(exact)
    expect(screen.getByLabelText('确切接收地址').textContent).toBe(exact)
    expect(screen.queryByRole('link', { name: exact })).not.toBeInTheDocument()
  })

  it('rejects a detail from another proposal instead of showing its payload', async () => {
    const mock = apiMock(); mock.api.getPersonalResearchEgress.mockResolvedValueOnce(view({ id: otherId, payload: { query: 'OTHER_PRIVATE_QUERY', provider: 'duckduckgo', max_results: 5 } }))
    await ready(mock)
    fireEvent.click(within(screen.getByRole('navigation', { name: '外发审批队列' })).getByRole('button', { name: /十月收入研究/ }))
    await screen.findByRole('alert')
    expect(screen.queryByText('OTHER_PRIVATE_QUERY')).not.toBeInTheDocument(); expect(screen.queryByRole('button', { name: '批准', exact: true })).not.toBeInTheDocument()
  })

  it('fences an expired request using the server clock and leaves no approval or execution button', async () => {
    await ready(apiMock(view({ expires_at: '2026-11-06T04:59:00Z' }))); await select()
    expect(screen.getByText('已过期')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '批准', exact: true })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '执行一次', exact: true })).not.toBeInTheDocument()
  })
})

describe('research egress human decisions and once execution', () => {
  it('freezes the exact approval snapshot and never executes after approval by effect', async () => {
    const mock = apiMock(), gate = deferred<ResearchEgressView>(); mock.api.operatePersonalResearchEgress.mockImplementationOnce(() => gate.promise)
    const { api } = await ready(mock); await select()
    fireEvent.click(screen.getByRole('button', { name: '批准', exact: true }))
    expect(confirm('批准外发').getByLabelText('确切外发检索词')).toHaveTextContent(query)
    fireEvent.click(confirm('批准外发').getByRole('button', { name: '确认批准外发', exact: true }))
    await waitFor(() => expect(api.operatePersonalResearchEgress).toHaveBeenCalledTimes(1))
    expect(api.operatePersonalResearchEgress.mock.calls[0]).toEqual(['egress_decide', expect.objectContaining({ id, expected_revision: 1, expected_digest: hash, decision: 'approve', request_id: expect.stringMatching(/^[a-f0-9]{32}$/) })])
    const stored = localStorage.getItem(researchEgressPendingKey('egress-A'))!
    expect(Object.keys(JSON.parse(stored)).sort()).toEqual(['operation', 'proposal_id', 'request_id', 'version', 'workspace_key'])
    expect(stored).not.toContain('PRIVATE'); expect(stored).not.toContain('payload'); expect(stored).not.toContain('Mira')
    await act(async () => { gate.resolve(approved()); await gate.promise })
    await screen.findByRole('button', { name: '执行一次', exact: true })
    expect(api.operatePersonalResearchEgress).toHaveBeenCalledTimes(1)
    expect(localStorage.getItem(researchEgressPendingKey('egress-A'))).toBeNull()
  })

  it('requires an explicit execute confirmation and opens the original material under its real actor', async () => {
    const { api, onOpenScopedArtifact } = await ready(apiMock(approved())); await select()
    expect(api.operatePersonalResearchEgress).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '执行一次', exact: true }))
    expect(confirm('执行一次').getByLabelText('确切接收地址')).toBeInTheDocument()
    fireEvent.click(confirm('执行一次').getByRole('button', { name: '确认执行一次', exact: true }))
    await screen.findByRole('button', { name: /打开原来源资料/ })
    expect(api.operatePersonalResearchEgress.mock.calls[0]).toEqual(['egress_execute', expect.objectContaining({ id, expected_revision: 2, expected_digest: hash })])
    expect(api.operatePersonalResearchEgress.mock.calls[0][1]).not.toHaveProperty('query')
    fireEvent.click(screen.getByRole('button', { name: /打开原来源资料/ }))
    expect(onOpenScopedArtifact).toHaveBeenCalledWith(actor, artifact, 1)
    expect(screen.queryByRole('button', { name: '执行一次', exact: true })).not.toBeInTheDocument()
  })

  it.each([['reject', '拒绝', '拒绝外发', '已拒绝'], ['revoke', '撤销批准', '撤销批准', '已撤销']] as const)('persists the exact %s decision without sending an execution', async (decision, button, title, status) => {
    const { api } = await ready(apiMock(decision === 'revoke' ? approved() : view())); await select()
    fireEvent.click(screen.getByRole('button', { name: button, exact: true }))
    fireEvent.click(confirm(title).getByRole('button', { name: `确认${title}`, exact: true }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: title, exact: true })).not.toBeInTheDocument())
    expect(screen.getAllByText(status).length).toBeGreaterThan(0)
    expect(api.operatePersonalResearchEgress.mock.calls[0][0]).toBe('egress_decide')
    expect(api.operatePersonalResearchEgress.mock.calls[0][1]).toMatchObject({ decision })
    expect(api.operatePersonalResearchEgress).toHaveBeenCalledTimes(1)
  })

  it('does not send when opaque recovery storage fails and keeps private text out of the error', async () => {
    const { api } = await ready(); await select()
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota PRIVATE_STORAGE') })
    fireEvent.click(screen.getByRole('button', { name: '批准', exact: true }))
    fireEvent.click(confirm('批准外发').getByRole('button', { name: '确认批准外发', exact: true }))
    await screen.findByText('原请求状态保存失败，未发送外发操作')
    expect(api.operatePersonalResearchEgress).not.toHaveBeenCalled(); expect(screen.queryByText('PRIVATE_STORAGE')).not.toBeInTheDocument()
  })

  it('retries only the frozen approval request with its original ID', async () => {
    const mock = apiMock(); mock.api.operatePersonalResearchEgress.mockRejectedValueOnce(failure(503))
    const { api } = await ready(mock); await select()
    fireEvent.click(screen.getByRole('button', { name: '批准', exact: true })); fireEvent.click(confirm('批准外发').getByRole('button', { name: '确认批准外发', exact: true }))
    await screen.findByRole('button', { name: '重试原审批请求' })
    const original = clone(api.operatePersonalResearchEgress.mock.calls[0])
    fireEvent.click(screen.getByRole('button', { name: '取消', exact: true }))
    fireEvent.click(screen.getByRole('button', { name: '重试原审批请求' }))
    await screen.findByRole('button', { name: '执行一次', exact: true })
    expect(api.operatePersonalResearchEgress.mock.calls[1]).toEqual(original)
    expect(screen.queryByText('PRIVATE_ERROR_BODY')).not.toBeInTheDocument()
  })

  it('keeps an execute UNKNOWN on its original receipt and offers only readback, not execute retry', async () => {
    const mock = apiMock(approved()); mock.api.operatePersonalResearchEgress.mockRejectedValueOnce(failure(503))
    const { api } = await ready(mock); await select()
    fireEvent.click(screen.getByRole('button', { name: '执行一次', exact: true })); fireEvent.click(confirm('执行一次').getByRole('button', { name: '确认执行一次', exact: true }))
    await screen.findByRole('button', { name: '核对原外发请求' })
    expect(screen.queryByRole('button', { name: '重试原审批请求' })).not.toBeInTheDocument()
    expect(localStorage.getItem(researchEgressPendingKey('egress-A'))).not.toBeNull()
    expect(api.operatePersonalResearchEgress).toHaveBeenCalledTimes(1)
  })

  it('preserves the original execute receipt on lease loss instead of treating its HTTP409 as definite rejection', async () => {
    const mock = apiMock(approved()); mock.api.operatePersonalResearchEgress.mockRejectedValueOnce(failure(409, 'lease_lost'))
    const { api } = await ready(mock); await select()
    fireEvent.click(screen.getByRole('button', { name: '执行一次', exact: true })); fireEvent.click(confirm('执行一次').getByRole('button', { name: '确认执行一次', exact: true }))
    await screen.findByText('原发送结果仍待核对，请保留原请求编号')
    expect(localStorage.getItem(researchEgressPendingKey('egress-A'))).not.toBeNull()
    expect(screen.queryByRole('button', { name: '重试原审批请求' })).not.toBeInTheDocument()
    expect(api.operatePersonalResearchEgress).toHaveBeenCalledTimes(1)
  })

  it('clears a definitive version conflict and refreshes the original proposal instead of editing its payload', async () => {
    const mock = apiMock(); mock.api.operatePersonalResearchEgress.mockRejectedValueOnce(failure(409, 'version_conflict'))
    const { api, set } = await ready(mock); await select()
    fireEvent.click(screen.getByRole('button', { name: '批准', exact: true })); set(view({ revision: 2 }))
    fireEvent.click(confirm('批准外发').getByRole('button', { name: '确认批准外发', exact: true }))
    await screen.findByText('原外发记录已变化，请重新审阅')
    expect(localStorage.getItem(researchEgressPendingKey('egress-A'))).toBeNull()
    expect(api.getPersonalResearchEgress.mock.calls.every(args => args[0] === id)).toBe(true)
  })
})

describe('research egress original receipt and identity boundaries', () => {
  const pending = (changes = {}) => JSON.stringify({ version: 1, request_id: requestId, operation: 'egress_execute', proposal_id: id, workspace_key: workspace, ...changes })
  const originalArgs = { id, request_id: requestId, expected_revision: 2, expected_digest: hash }
  const captured = () => approved({ revision: 3, status: 'started', execution: { request_id: `egress-${id}`, status: 'started', started_at: clock, phase: 'captured', saved_stage: true } })

  it('resumes captured local saving only after explicit action with the original outer arguments after remount', async () => {
    localStorage.setItem(researchEgressPendingKey('egress-A'), pending())
    const mock = apiMock(); mock.api.getPersonalResearchEgressRequest.mockResolvedValue({ request_id: requestId, status: 'pending', view: captured(), resume: { operation: 'egress_execute', arguments: originalArgs } })
    const { api } = await ready(mock)
    expect(api.operatePersonalResearchEgress).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '核对原外发请求' }))
    await screen.findByRole('button', { name: '继续保存原结果' })
    expect(screen.getByText('原结果已接收，等待保存')).toBeInTheDocument()
    expect(api.operatePersonalResearchEgress).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: '重试原审批请求' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '继续保存原结果' }))
    await screen.findByText('原结果已保存并读回')
    expect(api.operatePersonalResearchEgress.mock.calls).toEqual([['egress_execute', originalArgs]])
    expect(api.operatePersonalResearchEgress.mock.calls[0][1]).not.toHaveProperty('query')
    expect(api.operatePersonalResearchEgress.mock.calls[0][1]).not.toHaveProperty('url')
    expect(localStorage.getItem(researchEgressPendingKey('egress-A'))).toBeNull()
  })

  it('rejects unproven captured recovery and any changed outer identity or network payload', () => {
    const saved = parseResearchEgressPending(pending())
    const receipt = { request_id: requestId, status: 'pending' as const, view: captured(), resume: { operation: 'egress_execute' as const, arguments: originalArgs } }
    expect(validateCapturedEgressResume(receipt, saved)).toEqual(originalArgs)
    const started = clone(receipt); started.view.proposal.execution!.phase = 'started'; started.view.proposal.execution!.saved_stage = false
    expect(() => validateCapturedEgressResume(started, saved)).toThrow('凭据')
    expect(() => validateCapturedEgressResume({ ...receipt, view: { ...receipt.view, availability: 'source_unavailable' } }, saved)).toThrow('凭据')
    expect(() => validateCapturedEgressResume({ ...receipt, resume: { ...receipt.resume, arguments: { ...originalArgs, request_id: '4'.repeat(32) } } }, saved)).toThrow('凭据')
    expect(() => validateCapturedEgressResume({ ...receipt, resume: { ...receipt.resume, arguments: { ...originalArgs, url: 'https://example.com/private' } as typeof originalArgs } }, saved)).toThrow('凭据')
  })

  it('clears a captured original receipt after accepted opaque completion without claiming private result readback', async () => {
    localStorage.setItem(researchEgressPendingKey('egress-A'), pending())
    const mock = apiMock(); mock.api.getPersonalResearchEgressRequest.mockResolvedValue({ request_id: requestId, status: 'pending', view: captured(), resume: { operation: 'egress_execute', arguments: originalArgs } })
    mock.api.operatePersonalResearchEgress.mockResolvedValueOnce({ availability: 'source_unavailable', proposal: { id, revision: 4, digest: '', status: 'unavailable' } })
    await ready(mock)
    fireEvent.click(screen.getByRole('button', { name: '核对原外发请求' })); await screen.findByRole('button', { name: '继续保存原结果' })
    fireEvent.click(screen.getByRole('button', { name: '继续保存原结果' })); await screen.findByText('原请求已受理，原依据暂不可读取')
    expect(screen.queryByText('原结果已保存并读回')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('确切外发检索词')).not.toBeInTheDocument()
    expect(localStorage.getItem(researchEgressPendingKey('egress-A'))).toBeNull()
  })

  it('does not invent a network retry after started UNKNOWN and requires a fresh GET after a local save loses its response', async () => {
    localStorage.setItem(researchEgressPendingKey('egress-A'), pending())
    const mock = apiMock(); mock.api.getPersonalResearchEgressRequest.mockResolvedValueOnce({ request_id: requestId, status: 'unknown' })
      .mockResolvedValueOnce({ request_id: requestId, status: 'pending', view: captured(), resume: { operation: 'egress_execute', arguments: originalArgs } })
    mock.api.operatePersonalResearchEgress.mockRejectedValueOnce(failure(503))
    const { api } = await ready(mock)
    fireEvent.click(screen.getByRole('button', { name: '核对原外发请求' })); await screen.findByText('原请求结果待核对，未重新提交')
    expect(screen.queryByRole('button', { name: '继续保存原结果' })).not.toBeInTheDocument(); expect(api.operatePersonalResearchEgress).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '核对原外发请求' })); await screen.findByRole('button', { name: '继续保存原结果' })
    fireEvent.click(screen.getByRole('button', { name: '继续保存原结果' })); await screen.findByRole('alert')
    expect(screen.queryByRole('button', { name: '继续保存原结果' })).not.toBeInTheDocument()
    expect(api.operatePersonalResearchEgress.mock.calls).toEqual([['egress_execute', originalArgs]])
    expect(localStorage.getItem(researchEgressPendingKey('egress-A'))).not.toBeNull()
  })

  it('reconciles a remounted completed request with GET only and never re-posts', async () => {
    localStorage.setItem(researchEgressPendingKey('egress-A'), pending())
    const mock = apiMock(); mock.api.getPersonalResearchEgressRequest.mockResolvedValue({ request_id: requestId, status: 'completed', view: completed() })
    const { api } = await ready(mock)
    fireEvent.click(screen.getByRole('button', { name: '核对原外发请求' }))
    await screen.findByRole('button', { name: /打开原来源资料/ })
    expect(api.getPersonalResearchEgressRequest).toHaveBeenCalledWith(requestId, expect.any(AbortSignal))
    expect(api.operatePersonalResearchEgress).not.toHaveBeenCalled()
    expect(localStorage.getItem(researchEgressPendingKey('egress-A'))).toBeNull()
    expect(screen.queryByRole('button', { name: '重试原审批请求' })).not.toBeInTheDocument()
  })

  it('keeps UNKNOWN and a mismatched completion bound to the original opaque request', async () => {
    localStorage.setItem(researchEgressPendingKey('egress-A'), pending())
    const mock = apiMock(); mock.api.getPersonalResearchEgressRequest.mockResolvedValueOnce({ request_id: requestId, status: 'unknown' }).mockResolvedValueOnce({ request_id: requestId, status: 'completed', view: view({ id: otherId, payload: { query: 'OTHER_PRIVATE_QUERY', provider: 'duckduckgo', max_results: 5 } }) })
    const { api } = await ready(mock)
    fireEvent.click(screen.getByRole('button', { name: '核对原外发请求' })); await screen.findByText('原请求结果待核对，未重新提交')
    fireEvent.click(screen.getByRole('button', { name: '核对原外发请求' })); await screen.findByRole('alert')
    expect(localStorage.getItem(researchEgressPendingKey('egress-A'))).not.toBeNull()
    expect(api.operatePersonalResearchEgress).not.toHaveBeenCalled(); expect(screen.queryByText('OTHER_PRIVATE_QUERY')).not.toBeInTheDocument()
  })

  it('keeps corrupt or foreign-workspace recovery opaque and does not allow new decisions', async () => {
    localStorage.setItem(researchEgressPendingKey('egress-A'), pending({ workspace_key: '4'.repeat(64) }))
    const { api } = await ready(); await select()
    expect(screen.getByText('原外发请求属于另一工作区，请返回原工作区核对')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '批准', exact: true })).toBeDisabled()
    expect(screen.getByRole('button', { name: '核对原外发请求' })).toBeDisabled()
    expect(api.operatePersonalResearchEgress).not.toHaveBeenCalled()
  })

  it('clears private details and confirmation after opaque source denial and fences a previous read', async () => {
    const mock = apiMock(); await ready(mock); await select()
    fireEvent.click(screen.getByRole('button', { name: '批准', exact: true }))
    const opaque: ResearchEgressView = { availability: 'source_unavailable', proposal: { id, revision: 2, digest: '', status: 'unavailable' } }
    mock.set(opaque)
    fireEvent.click(screen.getByRole('button', { name: '刷新外发队列' }))
    await screen.findByText('原外发依据不可用')
    expect(screen.queryByRole('dialog', { name: '批准外发', exact: true })).not.toBeInTheDocument()
    expect(screen.queryByText(query)).not.toBeInTheDocument(); expect(screen.queryByText(reason)).not.toBeInTheDocument()
    expect(screen.queryByLabelText('确切外发检索词')).not.toBeInTheDocument()
  })

  it('invalidates a reviewed confirmation when list refresh reveals a new proposal revision', async () => {
    const mock = apiMock(); const { api } = await ready(mock); await select()
    fireEvent.click(screen.getByRole('button', { name: '批准', exact: true }))
    mock.set(view({ revision: 2, digest: '5'.repeat(64), payload: { query: '新修订检索词', provider: 'duckduckgo', max_results: 3 } }))
    fireEvent.click(screen.getByRole('button', { name: '刷新外发队列' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '批准外发', exact: true })).not.toBeInTheDocument())
    await screen.findByText('新修订检索词')
    expect(api.operatePersonalResearchEgress).not.toHaveBeenCalled()
  })

  it('does not reveal old private content after namespace or connection changes', async () => {
    const mock = apiMock(), onClose = vi.fn()
    const rendered = await ready(mock); await select()
    fireEvent.click(screen.getByRole('button', { name: '批准', exact: true }))
    rendered.rerender(<ResearchEgress api={mock.api} namespace="egress-B" workspaceKey={workspace} serverNow={clock} connected={false} onClose={onClose} />)
    expect(screen.queryByLabelText('确切外发检索词')).not.toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: '批准外发', exact: true })).not.toBeInTheDocument()
    expect(screen.queryByText(reason)).not.toBeInTheDocument()
  })

  it('ignores a delayed detail response from a previous namespace', async () => {
    const mock = apiMock(), gate = deferred<ResearchEgressView>(); mock.api.getPersonalResearchEgress.mockImplementationOnce(() => gate.promise)
    const rendered = await ready(mock)
    fireEvent.click(within(screen.getByRole('navigation', { name: '外发审批队列' })).getByRole('button', { name: /十月收入研究/ }))
    rendered.rerender(<ResearchEgress api={mock.api} namespace="egress-B" workspaceKey={workspace} serverNow={clock} connected onClose={vi.fn()} />)
    await act(async () => { gate.resolve(view()); await gate.promise })
    expect(screen.queryByLabelText('确切外发检索词')).not.toBeInTheDocument()
    expect(mock.api.operatePersonalResearchEgress).not.toHaveBeenCalled()
  })

  it('acknowledges an accepted opaque result without retaining its old private approval snapshot', async () => {
    const mock = apiMock(); mock.api.operatePersonalResearchEgress.mockResolvedValueOnce({ availability: 'source_unavailable', proposal: { id, revision: 2, digest: '', status: 'unavailable' } })
    await ready(mock); await select()
    fireEvent.click(screen.getByRole('button', { name: '批准', exact: true })); fireEvent.click(confirm('批准外发').getByRole('button', { name: '确认批准外发', exact: true }))
    await screen.findByText('原请求已受理，原依据暂不可读取')
    expect(localStorage.getItem(researchEgressPendingKey('egress-A'))).toBeNull()
    expect(screen.queryByLabelText('确切外发检索词')).not.toBeInTheDocument(); expect(screen.queryByText(reason)).not.toBeInTheDocument()
  })
})

describe('research egress result and accessible confirmation', () => {
  it('prefers the original receipt ArtifactRef and never replaces an old source by its newer head', () => {
    const original = completed()
    expect(researchEgressArtifacts(original)).toEqual([{ actor_id: actor, ref: original.proposal.execution!.result!.source_ref }])
    const later = clone(original); later.proposal.execution!.result!.source_ref = null
    const body = resultView(); body.sources = [{ id: sourceId, revision: 2, title: '来源新版', origin: 'url', url: 'https://example.com/report', captured_at: clock, expires_at: future, status: 'available', body_sha256: hash,
      artifact_ref: { artifact_id: artifact, version: 2, sha256: hash, grant_id: '', grant_revision: 0 } }]
    later.result = body
    expect(researchEgressArtifacts(later)).toEqual([])
    later.proposal.execution!.result!.source_revision = 2
    expect(researchEgressArtifacts(later)[0].ref.version).toBe(2)
  })

  it('provides no executable control for a started, unknown or completed proposal', async () => {
    const { api } = await ready(apiMock(view({ status: 'unknown', execution: { request_id: 'original-execute-42', status: 'started', started_at: clock } }))); await select()
    expect(screen.queryByRole('button', { name: '批准', exact: true })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '执行一次', exact: true })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '读取原执行状态' }))
    await waitFor(() => expect(api.getPersonalResearchEgress).toHaveBeenCalledTimes(2))
    expect(api.operatePersonalResearchEgress).not.toHaveBeenCalled()
  })

  it('closes an unsent confirmation with Escape without performing a mutation', async () => {
    const { api } = await ready(); await select()
    fireEvent.click(screen.getByRole('button', { name: '批准', exact: true }))
    fireEvent.keyDown(screen.getByRole('dialog', { name: '批准外发', exact: true }), { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '批准外发', exact: true })).not.toBeInTheDocument())
    expect(api.operatePersonalResearchEgress).not.toHaveBeenCalled()
  })
})
