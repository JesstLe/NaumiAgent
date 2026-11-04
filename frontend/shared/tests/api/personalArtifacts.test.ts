import { afterEach, describe, expect, it, vi } from 'vitest'
import { WorkbenchRequestError, WorkbenchRuntimeClient } from '../../src/api/WorkbenchRuntimeClient'

const id = `art_${'a'.repeat(32)}`
const actor = `pa_${'b'.repeat(32)}`
const client = () => new WorkbenchRuntimeClient('http://127.0.0.1:18768/api/v1/', async () => 'test-token')
const jsonResponse = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })

afterEach(() => vi.restoreAllMocks())

describe('personal artifact HTTP contract', () => {
  it('keeps owner selection independent of a chat session and encodes real list filters', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({ artifacts: [] }))
    await client().personalArtifacts({ actor_id: actor, q: '预算 & 旅行', kind: 'material', limit: 50, offset: 50 })
    const [url, init] = fetch.mock.calls[0]
    const parsed = new URL(url as string)
    expect(parsed.pathname).toBe('/api/v1/personal/artifacts')
    expect(parsed.searchParams.get('actor_id')).toBe(actor)
    expect(parsed.searchParams.get('q')).toBe('预算 & 旅行')
    expect(parsed.searchParams.get('offset')).toBe('50')
    expect(parsed.searchParams.has('session_id')).toBe(false)
    expect(init?.cache).toBe('no-store')
    expect(init?.headers).toMatchObject({ Authorization: 'Bearer test-token' })
  })

  it('opens exact versions and separate version/grant records without public hash URLs', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => jsonResponse({}))
    const api = client()
    await api.personalArtifact(id, actor, 3)
    await api.personalArtifactVersions(id, actor)
    await api.personalArtifactPreview(id, actor, 3)
    await api.personalArtifactGrants(id, 3, actor)
    expect(fetch.mock.calls.map(([url]) => String(url))).toEqual([
      `http://127.0.0.1:18768/api/v1/personal/artifacts/${id}?actor_id=${actor}&version=3`,
      `http://127.0.0.1:18768/api/v1/personal/artifacts/${id}/versions?actor_id=${actor}`,
      `http://127.0.0.1:18768/api/v1/personal/artifacts/${id}/preview?actor_id=${actor}&version=3`,
      `http://127.0.0.1:18768/api/v1/personal/artifacts/${id}/versions/3/grants?actor_id=${actor}`,
    ])
    expect(fetch.mock.calls.every(([, init]) => init?.cache === 'no-store')).toBe(true)
  })

  it('gets authenticated raw bytes and their required content hash', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('budget', { headers: { 'X-Content-SHA256': 'c'.repeat(64) } }))
    const value = await client().personalArtifactContent(id, actor, 1)
    expect(value.sha256).toBe('c'.repeat(64))
    expect(await value.blob.text()).toBe('budget')
  })

  it.each([{}, { 'X-Content-SHA256': 'invalid' }])('does not present unbound download content: %j', async headers => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('budget', { headers }))
    await expect(client().personalArtifactContent(id, actor, 1)).rejects.toThrow('缺少版本校验')
  })

  it('uploads the original file with browser-owned multipart headers and a stable request id', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({}))
    const file = new File(['income,expense\n1200,375\n'], 'budget.csv', { type: 'text/csv' })
    await client().importPersonalArtifact(file, { actor_id: actor, request_id: 'original-request', kind: 'material' })
    const [url, init] = fetch.mock.calls[0]
    expect(new URL(url as string).searchParams.get('request_id')).toBe('original-request')
    expect(init?.headers).not.toHaveProperty('Content-Type')
    expect((init?.body as FormData).get('file')).toBe(file)
  })

  it('sends binary revisions with the exact original CAS version', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({}))
    const file = new File(['revised'], 'image.svg', { type: 'image/svg+xml' })
    await client().importPersonalArtifactVersion(id, file, { actor_id: actor, request_id: 'revision-request', expected_version: 5, kind: 'output' })
    const [url, init] = fetch.mock.calls[0], parsed = new URL(url as string)
    expect(parsed.pathname).toBe(`/api/v1/personal/artifacts/${id}/versions/import`)
    expect(parsed.searchParams.get('expected_version')).toBe('5')
    expect((init?.body as FormData).get('file')).toBe(file)
  })

  it('retains exact content, source references and grant revisions in write payloads', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => jsonResponse({}))
    const body = { actor_id: actor, request_id: 'create-1', title: '预算', kind: 'output' as const, filename: 'budget.md', content: '825 元', source_refs: [{ artifact_id: id, version: 2, sha256: 'd'.repeat(64), grant_id: 'asg_source', grant_revision: 1 }] }
    await client().createPersonalArtifact(body)
    await client().revisePersonalArtifact(id, { ...body, expected_version: 4 })
    await client().sharePersonalArtifact(id, 4, { actor_id: actor, request_id: 'share-1', recipient_id: 'main' })
    await client().revokePersonalArtifactGrant('asg_original', { actor_id: actor, request_id: 'revoke-1', expected_revision: 1 })
    expect(JSON.parse(fetch.mock.calls[0][1]?.body as string)).toEqual(body)
    expect(JSON.parse(fetch.mock.calls[1][1]?.body as string).expected_version).toBe(4)
    expect(JSON.parse(fetch.mock.calls[2][1]?.body as string).recipient_id).toBe('main')
    expect(JSON.parse(fetch.mock.calls[3][1]?.body as string).expected_revision).toBe(1)
  })

  it('preserves an explicit accepted receipt without inferring nonacceptance from errors', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({ error: 'delivery_error', detail: '版本已保存，对话卡片待恢复', accepted: true, artifact_id: id, version: 2 }, 503))
    const error = await client().createPersonalArtifact({ actor_id: actor, request_id: 'same', title: '预算', filename: 'budget.md', content: '825', kind: 'output' }).catch(value => value)
    expect(error).toBeInstanceOf(WorkbenchRequestError)
    expect(error.code).toBe('delivery_error')
    expect(error.receipt).toEqual({ accepted: true, artifact_id: id, version: 2 })
    vi.mocked(globalThis.fetch).mockResolvedValue(jsonResponse({ error: 'storage_error', detail: '暂不可用' }, 503))
    const unknown = await client().personalArtifact(id, actor).catch(value => value)
    expect(unknown.receipt).toBeUndefined()
  })
})
