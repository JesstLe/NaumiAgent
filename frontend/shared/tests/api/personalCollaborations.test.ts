import { afterEach, describe, expect, it, vi } from 'vitest'
import { WorkbenchRuntimeClient, type PersonalCollaborationInput } from '@naumi/shared/api/WorkbenchRuntimeClient'

afterEach(() => vi.restoreAllMocks())
const client = () => new WorkbenchRuntimeClient('http://localhost/api/v1/', async () => 'private-test-token')
const jsonResponse = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })

describe('personal collaboration HTTP contract', () => {
  it('lists the workspace or filters a source session with URL encoding and authentication', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => jsonResponse({ collaborations: [] }))
    const api = client()
    await expect(api.personalCollaborations()).resolves.toEqual({ collaborations: [] })
    await api.personalCollaborations('source/with space')
    expect(fetch.mock.calls[0][0]).toBe('http://localhost/api/v1/personal/collaborations')
    expect(fetch.mock.calls[1][0]).toBe('http://localhost/api/v1/personal/collaborations?source_session_id=source%2Fwith%20space')
    expect(fetch.mock.calls[1][1]?.headers).toMatchObject({ Authorization: 'Bearer private-test-token' })
  })

  it('submits the exact stable request, step versions and dependency graph', async () => {
    const body: PersonalCollaborationInput = { source_session_id: 'main', request_id: 'stable-admission', title: '资料核对', context: '仅这段资料', steps: [{ id: 'first', agent_id: `pa_${'a'.repeat(32)}`, agent_version: 2, task: '核对', depends_on: [] }] }
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({ id: 'pc_test' }))
    await client().createPersonalCollaboration(body)
    expect(fetch).toHaveBeenCalledWith('http://localhost/api/v1/personal/collaborations', expect.objectContaining({ method: 'POST', body: JSON.stringify(body) }))
  })

  it('reads and cancels exactly the encoded collaboration without a broad cancellation payload', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => jsonResponse({ id: 'pc_test' }))
    const api = client()
    await api.personalCollaboration('pc_1/other')
    await api.cancelPersonalCollaboration('pc_1/other')
    expect(fetch.mock.calls[0][0]).toBe('http://localhost/api/v1/personal/collaborations/pc_1%2Fother')
    expect(fetch.mock.calls[1][0]).toBe('http://localhost/api/v1/personal/collaborations/pc_1%2Fother/cancel')
    expect(fetch.mock.calls[1][1]).toMatchObject({ method: 'POST', body: '{}' })
  })

  it('surfaces backend version/idempotency errors instead of hiding them as accepted', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ detail: '专员职责已更新，请刷新后重新提交', code: 'version_conflict' }), { status: 409, headers: { 'Content-Type': 'application/json' } }))
    await expect(client().personalCollaboration('pc_test')).rejects.toThrow('专员职责已更新')
  })

  it('replies only to the encoded collaboration question with the explicit normalized answer fields', async () => {
    const requestId = `ask-${'c'.repeat(32)}`
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({ status: 'answered', request_id: requestId, response: { kind: 'custom', value: '', label: '', custom_text: '保留来源列' } }))
    await client().replyPersonalCollaboration('pc/one', requestId, { kind: 'custom', custom_text: '保留来源列' })
    expect(fetch).toHaveBeenCalledWith(`http://localhost/api/v1/personal/collaborations/pc%2Fone/questions/${requestId}/reply`, expect.objectContaining({ method: 'POST', body: JSON.stringify({ kind: 'custom', custom_text: '保留来源列' }) }))
  })
})
