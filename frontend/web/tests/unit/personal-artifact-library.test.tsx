import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react'
import { createHash, webcrypto } from 'node:crypto'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import type { PersonalAgent } from '@naumi/shared/api/WorkbenchRuntimeClient'
import { WorkbenchRequestError } from '@naumi/shared/api/WorkbenchRuntimeClient'
import type { PersonalArtifactPage, PersonalArtifactRef, PersonalArtifactSnapshot } from '@naumi/shared/api/personalArtifacts'
import { LibraryWorkspace, MessageArtifactCards, messageArtifactRefs, readArtifactLocation } from '../../../web2/src/personal/library/ArtifactLibrary'
import { ArtifactPage, validatePreview, verifiedContent } from '../../../web2/src/personal/library/ArtifactPage'
import { ArtifactEditor } from '../../../web2/src/personal/library/ArtifactEditor'
import { blobBuffer, useArtifactLibrary, validateArtifact, type ArtifactApi, type ArtifactLibraryState } from '../../../web2/src/personal/library/useArtifactLibrary'

const id = `art_${'a'.repeat(32)}`, otherId = `art_${'b'.repeat(32)}`, agentId = `pa_${'c'.repeat(32)}`
const source = 'income,expense\n1200,375\n'
const hash = (text: string) => createHash('sha256').update(text).digest('hex')
const snapshot = (text = '# 预算\n\n825 元', changes: Partial<PersonalArtifactSnapshot> = {}): PersonalArtifactSnapshot => ({
  artifact_id: id, version: 1, owner_id: 'main', title: '预算核验', kind: 'output', sha256: hash(text), bytes: new TextEncoder().encode(text).byteLength,
  mime_type: 'text/markdown', suffix: '.md', created_at: '2026-11-04T03:00:00Z', source_refs: [], evidence: {},
  grant_id: '', grant_revision: 0, can_revise: true, can_share: true, ...changes,
})
const agent: PersonalAgent = { id: agentId, name: 'Lambda-7', description: '', instructions: '处理我明确交代的事情', kind: 'specialist', tool_names: [], model_tier: 'fast', member_ids: [], status: 'active', version: 1, created_at: '', updated_at: '' }
function apiMock(item = snapshot(), content = '# 预算\n\n825 元') {
  const grant = { id: 'asg_grant', artifact_id: item.artifact_id, version: item.version, owner_id: item.owner_id, recipient_id: agentId, revision: 1, status: 'active' as const, created_at: '2026-11-04T03:00:00Z', revoked_at: '', recipient_scope: {} }
  return {
    personalArtifacts: vi.fn().mockResolvedValue({ artifacts: [item] }),
    personalArtifact: vi.fn().mockResolvedValue(item),
    personalArtifactVersions: vi.fn().mockResolvedValue({ versions: [item] }),
    personalArtifactPreview: vi.fn().mockResolvedValue({ type: 'text', content, truncated: false }),
    personalArtifactContent: vi.fn().mockResolvedValue({ blob: new Blob([content], { type: item.mime_type }), sha256: item.sha256 }),
    createPersonalArtifact: vi.fn().mockResolvedValue(item),
    revisePersonalArtifact: vi.fn().mockResolvedValue(snapshot(content, { version: 2 })),
    importPersonalArtifact: vi.fn().mockResolvedValue(snapshot(source, { kind: 'material', mime_type: 'text/csv', suffix: '.csv' })),
    importPersonalArtifactVersion: vi.fn().mockResolvedValue(snapshot(source, { version: 2, kind: 'material', mime_type: 'text/csv', suffix: '.csv' })),
    personalArtifactGrants: vi.fn().mockResolvedValue({ grants: [grant] }),
    sharePersonalArtifact: vi.fn().mockResolvedValue(grant),
    revokePersonalArtifactGrant: vi.fn().mockResolvedValue({ ...grant, status: 'revoked', revision: 2, revoked_at: '2026-11-04T03:01:00Z' }),
  } satisfies ArtifactApi
}
const stateStub = (changes: Partial<ArtifactLibraryState> = {}) => ({
  locked: false, posting: false, error: '', share: vi.fn().mockResolvedValue(null), revoke: vi.fn().mockResolvedValue(null), importVersion: vi.fn().mockResolvedValue(null),
  create: vi.fn().mockResolvedValue(null), revise: vi.fn().mockResolvedValue(null), ...changes,
} as unknown as ArtifactLibraryState)
const objectBlobs: Blob[] = []
const createUrl = vi.fn((blob: Blob) => { objectBlobs.push(blob); return `blob:verified-${objectBlobs.length}` })
const revokeUrl = vi.fn()
beforeAll(() => {
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: createUrl })
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revokeUrl })
})
beforeEach(() => {
  vi.stubGlobal('crypto', webcrypto)
  history.replaceState({}, '', '/web2')
  localStorage.clear(); objectBlobs.length = 0; createUrl.mockClear(); revokeUrl.mockClear()
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
const defer = <T,>() => { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done }); return { promise, resolve } }
const body = { title: '预算核验', kind: 'output' as const, filename: 'budget.md', content: '825 元' }

describe('artifact Library stable writes', () => {
  it('keeps an unknown original request through remount and retries exact bytes with the same identity', async () => {
    const api = apiMock(), options = { api, namespace: 'workspace-A', actorId: 'main', connected: true }
    api.createPersonalArtifact.mockRejectedValueOnce(new Error('网络结果未知'))
    const first = renderHook(() => useArtifactLibrary(options))
    await act(async () => { await first.result.current.create(body) })
    const original = api.createPersonalArtifact.mock.calls[0][0]
    expect(first.result.current.pending?.body.request_id).toBe(original.request_id)
    await act(async () => { await first.result.current.create({ ...body, content: 'changed' }) })
    expect(api.createPersonalArtifact).toHaveBeenCalledTimes(1)
    first.unmount()
    const reopened = renderHook(() => useArtifactLibrary(options))
    await waitFor(() => expect(reopened.result.current.pending).not.toBeNull())
    await act(async () => { await reopened.result.current.retry() })
    expect(api.createPersonalArtifact.mock.calls[1][0]).toEqual(original)
    expect(reopened.result.current.pending).toBeNull()
  })

  it('isolates pending requests by server/workspace and actor', async () => {
    const api = apiMock(); api.createPersonalArtifact.mockRejectedValue(new Error('结果待核对'))
    const owner = renderHook(() => useArtifactLibrary({ api, namespace: 'server-A/work-A', actorId: 'main', connected: true }))
    await act(async () => { await owner.result.current.create(body) })
    const otherActor = renderHook(() => useArtifactLibrary({ api, namespace: 'server-A/work-A', actorId: agentId, connected: true }))
    const otherWorkspace = renderHook(() => useArtifactLibrary({ api, namespace: 'server-A/work-B', actorId: 'main', connected: true }))
    expect(otherActor.result.current.pending).toBeNull(); expect(otherWorkspace.result.current.pending).toBeNull()
  })

  it('persists an explicit accepted receipt but never turns a plain 503 into nonacceptance', async () => {
    const api = apiMock(); api.createPersonalArtifact.mockRejectedValueOnce(new WorkbenchRequestError('卡片待恢复', 503, 'delivery_error', { accepted: true, artifact_id: id, version: 1 }))
    const hook = renderHook(() => useArtifactLibrary({ api, namespace: 'receipt', actorId: 'main', connected: true }))
    await act(async () => { await hook.result.current.create(body) })
    expect(hook.result.current.pending?.receipt).toEqual({ artifact_id: id, version: 1 })
    expect(hook.result.current.locked).toBe(true)
  })

  it('does not POST if browser storage cannot preserve the request identity', async () => {
    const api = apiMock()
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('存储已满') })
    const hook = renderHook(() => useArtifactLibrary({ api, namespace: 'quota', actorId: 'main', connected: true }))
    await act(async () => { await hook.result.current.create(body) })
    expect(api.createPersonalArtifact).not.toHaveBeenCalled()
    expect(hook.result.current.error).toBe('存储已满')
  })

  it('does not let a late successful directory read erase a write failure', async () => {
    const api = apiMock(), list = defer<{ artifacts: PersonalArtifactSnapshot[] }>()
    api.personalArtifacts.mockReturnValue(list.promise)
    api.createPersonalArtifact.mockRejectedValue(new Error('写入结果未知，请核对原请求'))
    const hook = renderHook(() => useArtifactLibrary({ api, namespace: 'read-write-race', actorId: 'main', connected: true }))
    await waitFor(() => expect(api.personalArtifacts).toHaveBeenCalled())
    await act(async () => { await hook.result.current.create(body) })
    await act(async () => { list.resolve({ artifacts: [snapshot()] }) })
    expect(hook.result.current.error).toBe('写入结果未知，请核对原请求')
    expect(hook.result.current.pending).not.toBeNull()
  })

  it('does not overwrite a corrupted pending record or invent a replacement request', async () => {
    localStorage.setItem('naumi:library:request:broken:main', '{bad')
    const api = apiMock(), hook = renderHook(() => useArtifactLibrary({ api, namespace: 'broken', actorId: 'main', connected: true }))
    await waitFor(() => expect(hook.result.current.identityError).not.toBe(''))
    await act(async () => { await hook.result.current.create(body) })
    expect(api.createPersonalArtifact).not.toHaveBeenCalled()
    expect(localStorage.getItem('naumi:library:request:broken:main')).toBe('{bad')
  })

  it('fences simultaneous clicks before the first POST resolves', async () => {
    const api = apiMock(), pending = defer<PersonalArtifactSnapshot>(); api.createPersonalArtifact.mockReturnValue(pending.promise)
    const hook = renderHook(() => useArtifactLibrary({ api, namespace: 'double', actorId: 'main', connected: true }))
    let first!: Promise<unknown>
    act(() => { first = hook.result.current.create(body); void hook.result.current.create(body) })
    expect(api.createPersonalArtifact).toHaveBeenCalledTimes(1)
    await act(async () => { pending.resolve(snapshot()); await first })
  })

  it('requires the exact original upload after remount and checks its actual SHA', async () => {
    const api = apiMock(), options = { api, namespace: 'file', actorId: 'main', connected: true }
    api.importPersonalArtifact.mockRejectedValueOnce(new Error('上传响应未知'))
    const first = renderHook(() => useArtifactLibrary(options))
    const file = new File([source], 'budget.csv', { type: 'text/csv' })
    await act(async () => { await first.result.current.importFile(file) })
    const original = api.importPersonalArtifact.mock.calls[0][1]
    first.unmount()
    const reopened = renderHook(() => useArtifactLibrary(options))
    await waitFor(() => expect(reopened.result.current.pending?.operation).toBe('import'))
    await act(async () => { await reopened.result.current.retry(new File(['changed'], 'budget.csv', { type: 'text/csv' })) })
    expect(api.importPersonalArtifact).toHaveBeenCalledTimes(1)
    await act(async () => { await reopened.result.current.retry(file) })
    expect(api.importPersonalArtifact.mock.calls[1][1]).toEqual(original)
    expect(reopened.result.current.pending).toBeNull()
  })

  it('sends binary revisions through the same stable original version', async () => {
    const api = apiMock(), hook = renderHook(() => useArtifactLibrary({ api, namespace: 'binary', actorId: 'main', connected: true }))
    await act(async () => { await hook.result.current.importVersion(snapshot(source, { kind: 'material' }), new File([source], 'budget.csv', { type: 'text/csv' })) })
    expect(api.importPersonalArtifactVersion).toHaveBeenCalledWith(id, expect.any(File), expect.objectContaining({ actor_id: 'main', expected_version: 1 }))
  })
})

describe('artifact Library and independent Page', () => {
  it('opens a real version from the directory without needing any session id', async () => {
    const api = apiMock()
    render(<LibraryWorkspace api={api} namespace="directory" initialActorId="main" currentActorId="main" agents={[agent]} connected onClose={vi.fn()} />)
    expect(screen.getByRole('option', { name: '主 Agent' })).toBeVisible()
    fireEvent.click(await screen.findByRole('button', { name: '打开预算核验 v1' }))
    expect(await screen.findByRole('heading', { name: '预算核验' })).toBeVisible()
    expect(api.personalArtifact).toHaveBeenCalledWith(id, 'main', 1, expect.any(AbortSignal))
    expect(new URL(location.href).searchParams.get('version')).toBe('1')
    expect(screen.getByLabelText('资料版本')).toHaveValue('1')
  })

  it('uses real kind/search query filters and manual actor selection', async () => {
    const api = apiMock()
    render(<LibraryWorkspace api={api} namespace="filter" initialActorId="main" currentActorId="main" agents={[agent]} connected onClose={vi.fn()} />)
    await screen.findByRole('button', { name: '打开预算核验 v1' })
    fireEvent.click(screen.getByRole('button', { name: '材料', exact: true }))
    fireEvent.change(screen.getByLabelText('搜索资料库'), { target: { value: '旅行预算' } })
    await waitFor(() => expect(api.personalArtifacts).toHaveBeenLastCalledWith(expect.objectContaining({ actor_id: 'main', kind: 'material', q: '旅行预算' }), expect.any(AbortSignal)))
    fireEvent.change(screen.getByLabelText('资料库所属 Agent'), { target: { value: agentId } })
    await waitFor(() => expect(api.personalArtifacts).toHaveBeenLastCalledWith(expect.objectContaining({ actor_id: agentId, offset: 0 }), expect.any(AbortSignal)))
  })

  it('reopens a deep link and shows only versions actually granted to the selected Agent', async () => {
    history.replaceState({}, '', `/web2?library=1&actor=${agentId}&artifact=${id}&version=1`)
    const item = snapshot(undefined, { owner_id: 'main', can_revise: false, can_share: false, grant_id: 'asg_original', grant_revision: 1 }), api = apiMock(item)
    render(<LibraryWorkspace api={api} namespace="granted" initialActorId="main" currentActorId="main" agents={[agent]} connected onClose={vi.fn()} />)
    expect(await screen.findByRole('heading', { name: '预算核验' })).toBeVisible()
    expect(screen.getByRole('combobox', { name: '资料版本' }).children).toHaveLength(1)
    expect(screen.queryByRole('button', { name: '编辑', exact: true })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '分享', exact: true })).not.toBeInTheDocument()
  })

  it('does not silently open another actor or latest version for malformed links', () => {
    expect(readArtifactLocation(`?artifact=${id}&version=0&actor=main`).error).not.toBe('')
    expect(readArtifactLocation(`?artifact=${id}&version=1&actor=unknown`).error).not.toBe('')
    const api = apiMock(); history.replaceState({}, '', `?library=1&artifact=${id}&version=0`)
    render(<LibraryWorkspace api={api} namespace="bad-url" initialActorId="main" currentActorId="main" agents={[agent]} connected onClose={vi.fn()} />)
    expect(screen.getByRole('alert')).toHaveTextContent('链接')
    expect(api.personalArtifact).not.toHaveBeenCalled()
  })

  it('preserves unknown writes and requires confirmation before abandoning retry', async () => {
    const api = apiMock(); api.createPersonalArtifact.mockRejectedValue(new Error('结果未知'))
    render(<LibraryWorkspace api={api} namespace="unknown-ui" initialActorId="main" currentActorId="main" agents={[agent]} connected onClose={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: '新建文档' }))
    fireEvent.change(screen.getByLabelText('文档标题'), { target: { value: '新报告' } })
    fireEvent.change(screen.getByLabelText('文档正文'), { target: { value: '825 元' } })
    fireEvent.click(screen.getByRole('button', { name: '保存新版本' }))
    await waitFor(() => expect(screen.getByText('保留原请求')).toBeVisible())
    expect(screen.getByLabelText('文档正文')).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '放弃重试', exact: true }))
    expect(screen.getByRole('group', { name: '确认放弃资料请求重试' })).toBeVisible()
    expect(api.createPersonalArtifact).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: '保留原请求' }))
    expect(screen.getByLabelText('文档正文')).toBeDisabled()
  })

  it('opens precise source cards and does not turn an author fact label into verified evidence', async () => {
    const reference: PersonalArtifactRef = { artifact_id: otherId, version: 2, sha256: hash(source), grant_id: '', grant_revision: 0 }
    const page: PersonalArtifactPage = { version: 1, type: 'page', title: '预算报告', sections: [{ id: 'result', title: '预算结果', markdown: '**825 元**', evidence_status: 'fact', source_refs: [reference] }] }
    const item = snapshot(JSON.stringify(page), { title: page.title, mime_type: 'application/json', suffix: '.json' }), api = apiMock(item, JSON.stringify(page))
    api.personalArtifactPreview.mockResolvedValue(page)
    api.personalArtifact.mockImplementation(async identity => identity === otherId ? snapshot(source, { artifact_id: otherId, version: 2, title: '原始预算.csv' }) : item)
    const open = vi.fn()
    render(<ArtifactPage api={api} actorId="main" selection={{ artifact_id: id, version: 1 }} agents={[agent]} state={stateStub()} refreshToken={0} onOpen={open} onBack={vi.fn()} onEdit={vi.fn()} />)
    expect(await screen.findByRole('navigation', { name: '文档目录' })).toHaveTextContent('预算结果')
    expect(screen.getByText('事实')).toHaveAttribute('title', '作者标注，不代表独立核验')
    expect(screen.queryByText('已核实')).not.toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', { name: /原始预算.csv/ }))
    expect(open).toHaveBeenCalledWith(reference)
  })

  it('does not load remote Markdown images or unbound image widgets', async () => {
    const page: PersonalArtifactPage = { version: 1, type: 'page', title: '私有文档', sections: [{ id: 'private', title: '正文', markdown: '![remote](https://untrusted.test/collect?secret=private)', widget: { version: 1, type: 'image', title: 'external', url: 'https://untrusted.test/image' }, evidence_status: 'unknown' }] }
    const item = snapshot(JSON.stringify(page), { mime_type: 'application/json', suffix: '.json' }), api = apiMock(item)
    api.personalArtifactPreview.mockResolvedValue(page)
    const { container } = render(<ArtifactPage api={api} actorId="main" selection={{ artifact_id: id }} agents={[agent]} state={stateStub()} refreshToken={0} onOpen={vi.fn()} onBack={vi.fn()} onEdit={vi.fn()} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('未绑定资料版本')
    expect(container.querySelector('img')).toBeNull()
  })

  it('actually filters/sorts table rows and exports only filtered data with CSV formula escaping', async () => {
    const item = snapshot(source, { suffix: '.csv', mime_type: 'text/csv' }), api = apiMock(item, source)
    api.personalArtifactPreview.mockResolvedValue({ type: 'table', columns: [{ key: 'name', label: '名称' }, { key: 'value', label: '金额' }], rows: [{ name: '=SUM(A1)', value: 20 }, { name: 'other', value: 5 }], total_rows: 2, preview_limit: 2000 })
    render(<ArtifactPage api={api} actorId="main" selection={{ artifact_id: id }} agents={[agent]} state={stateStub()} refreshToken={0} onOpen={vi.fn()} onBack={vi.fn()} onEdit={vi.fn()} />)
    await screen.findByRole('table')
    fireEvent.click(screen.getByRole('button', { name: /金额 ↕/ }))
    expect(within(screen.getByRole('table')).getAllByRole('row')[1]).toHaveTextContent('other')
    fireEvent.change(screen.getByLabelText('搜索表格'), { target: { value: '=SUM' } })
    expect(screen.queryByRole('cell', { name: 'other', exact: true })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '导出 CSV' }))
    const text = new TextDecoder().decode(await blobBuffer(objectBlobs.at(-1)!))
    expect(text).toContain("'=SUM(A1)"); expect(text).not.toContain('other')
  })

  it('binds sharing and confirmed revocation to the selected version and actual recipient', async () => {
    const api = apiMock(), state = stateStub()
    render(<ArtifactPage api={api} actorId="main" selection={{ artifact_id: id, version: 1 }} agents={[agent]} state={state} refreshToken={0} onOpen={vi.fn()} onBack={vi.fn()} onEdit={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: '分享', exact: true }))
    fireEvent.change(screen.getByLabelText('分享给'), { target: { value: agentId } })
    fireEvent.click(screen.getByRole('button', { name: '分享此版本' }))
    await waitFor(() => expect(state.share).toHaveBeenCalledWith(id, 1, agentId))
    fireEvent.click(await screen.findByRole('button', { name: '撤销', exact: true }))
    expect(state.revoke).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '确认撤销' }))
    await waitFor(() => expect(state.revoke).toHaveBeenCalledWith(expect.objectContaining({ id: 'asg_grant', version: 1, revision: 1 })))
  })

  it('rejects mismatched raw bytes before displaying or downloading a private file', async () => {
    const api = apiMock(), item = snapshot('private image', { mime_type: 'image/png', suffix: '.png' })
    api.personalArtifactContent.mockResolvedValue({ blob: new Blob(['wrong bytes']), sha256: item.sha256 })
    await expect(verifiedContent(api, item, 'main')).rejects.toThrow('内容与所选版本不一致')
    expect(createUrl).not.toHaveBeenCalled()
  })

  it('shows loss of access after revalidation instead of retaining old private content', async () => {
    const api = apiMock(), props = { api, actorId: 'main', selection: { artifact_id: id }, agents: [agent], state: stateStub(), onOpen: vi.fn(), onBack: vi.fn(), onEdit: vi.fn() }
    const { rerender } = render(<ArtifactPage {...props} refreshToken={0} />)
    expect(await screen.findByText('825 元')).toBeVisible()
    api.personalArtifact.mockRejectedValue(new Error('访问已撤销'))
    rerender(<ArtifactPage {...props} refreshToken={1} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('访问已撤销')
    expect(screen.queryByText('825 元')).not.toBeInTheDocument()
  })

  it('does not display a late previous actor response after the user changes Library identity', async () => {
    history.replaceState({}, '', `?library=1&artifact=${id}&version=1&actor=main`)
    const api = apiMock(), old = defer<PersonalArtifactSnapshot>()
    api.personalArtifact.mockReturnValue(old.promise)
    api.personalArtifacts.mockImplementation(async query => ({ artifacts: [snapshot(undefined, { owner_id: query.actor_id, title: query.actor_id === agentId ? 'Lambda 的资料' : '主 Agent 的资料' })] }))
    render(<LibraryWorkspace api={api} namespace="actor-race" initialActorId="main" currentActorId="main" agents={[agent]} connected onClose={vi.fn()} />)
    await waitFor(() => expect(api.personalArtifact).toHaveBeenCalled())
    fireEvent.change(screen.getByLabelText('资料库所属 Agent'), { target: { value: agentId } })
    await screen.findByRole('button', { name: '打开Lambda 的资料 v1' })
    await act(async () => { old.resolve(snapshot(undefined, { title: '旧身份私有结果' })) })
    expect(screen.queryByText('旧身份私有结果')).not.toBeInTheDocument()
  })

  it('releases a verified private object URL when its Page closes', async () => {
    const content = '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"><rect width="1" height="1" /></svg>'
    const item = snapshot(content, { title: '图像', suffix: '.svg', mime_type: 'image/svg+xml' }), api = apiMock(item, content)
    const { unmount } = render(<ArtifactPage api={api} actorId="main" selection={{ artifact_id: id }} agents={[agent]} state={stateStub()} refreshToken={0} onOpen={vi.fn()} onBack={vi.fn()} onEdit={vi.fn()} />)
    expect(await screen.findByRole('img', { name: '图像' })).toHaveAttribute('src', 'blob:verified-1')
    unmount()
    expect(revokeUrl).toHaveBeenCalledWith('blob:verified-1')
  })

  it('keeps message cards bound to the original version and rejects forged hashes', async () => {
    const reference = { artifact_id: id, version: 1, sha256: hash('# 预算\n\n825 元'), grant_id: '', grant_revision: 0 }, api = apiMock(), open = vi.fn()
    render(<MessageArtifactCards metadata={{ artifact_refs: [reference] }} api={api} actorId="main" onOpen={open} />)
    fireEvent.click(await screen.findByRole('button', { name: /预算核验/ }))
    expect(open).toHaveBeenCalledWith(reference)
    expect(messageArtifactRefs({ artifact_refs: [{ ...reference, sha256: 'invalid' }] }).error).not.toBe('')
  })

  it('edits structured Page sections without silently saving an unfinished widget draft', async () => {
    const page: PersonalArtifactPage = { version: 1, type: 'page', title: 'Page', sections: [{ id: 'one', title: '正文', markdown: '825', evidence_status: 'unknown', widget: { version: 1, type: 'metrics', title: '净额', items: [{ label: '净额', value: 825 }] } }] }
    const state = stateStub(), item = snapshot(JSON.stringify(page), { title: 'Page', suffix: '.json', mime_type: 'application/json' })
    render(<ArtifactEditor value={{ item, content: JSON.stringify(page) }} state={state} api={apiMock(item)} actorId="main" onClose={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('交互组件定义'), { target: { value: '{unfinished' } })
    expect(screen.getByRole('button', { name: '保存新版本' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '预览', exact: true }))
    fireEvent.click(screen.getByRole('button', { name: '编辑', exact: true }))
    expect(screen.getByLabelText('交互组件定义')).toHaveValue('{unfinished')
    expect(state.revise).not.toHaveBeenCalled()
  })

  it('creates an actual structured table in a new Page instead of requiring an existing widget', async () => {
    const state = stateStub(), api = apiMock()
    render(<ArtifactEditor value={{ content: '' }} state={state} api={api} actorId="main" onClose={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('文档标题'), { target: { value: '真实预算表' } })
    fireEvent.change(screen.getByLabelText('文档格式'), { target: { value: 'page' } })
    fireEvent.click(screen.getByRole('button', { name: '添加表格' }))
    const table = { version: 1, type: 'table', title: '预算明细', columns: [{ key: 'item', label: '项目' }, { key: 'amount', label: '金额' }], rows: [{ item: '收入', amount: 1200 }, { item: '支出', amount: 375 }, { item: '结余', amount: 825 }] }
    fireEvent.change(screen.getByLabelText('交互组件定义'), { target: { value: JSON.stringify(table) } })
    fireEvent.click(screen.getByRole('button', { name: '保存新版本' }))
    await waitFor(() => expect(state.create).toHaveBeenCalled())
    const request = vi.mocked(state.create).mock.calls[0][0]
    expect(JSON.parse(request.content).sections[0].widget).toEqual(table)
  })

  it('confirms before discarding a locally edited document', async () => {
    const close = vi.fn()
    render(<ArtifactEditor value={{ content: '' }} state={stateStub()} api={apiMock()} actorId="main" onClose={close} />)
    fireEvent.change(screen.getByLabelText('文档正文'), { target: { value: '尚未保存的正文' } })
    fireEvent.click(screen.getByRole('button', { name: '关闭', exact: true }))
    expect(close).not.toHaveBeenCalled()
    expect(screen.getByRole('group', { name: '确认关闭未保存文档' })).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: '继续编辑' }))
    expect(screen.getByLabelText('文档正文')).toHaveValue('尚未保存的正文')
  })

  it('requires an explicit decision before changing actor with unsaved edits', async () => {
    const api = apiMock()
    render(<LibraryWorkspace api={api} namespace="dirty-actor" initialActorId="main" currentActorId="main" agents={[agent]} connected onClose={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: '新建文档' }))
    fireEvent.change(screen.getByLabelText('文档正文'), { target: { value: '未保存版本的正文' } })
    fireEvent.change(screen.getByLabelText('资料库所属 Agent'), { target: { value: agentId } })
    expect(screen.getByRole('group', { name: '确认关闭未保存文档' })).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: '继续编辑' }))
    expect(screen.getByLabelText('文档正文')).toHaveValue('未保存版本的正文')
    expect(screen.getByLabelText('资料库所属 Agent')).toHaveValue('main')
    fireEvent.change(screen.getByLabelText('资料库所属 Agent'), { target: { value: agentId } })
    fireEvent.click(screen.getByRole('button', { name: '放弃修改' }))
    await waitFor(() => expect(api.personalArtifacts).toHaveBeenLastCalledWith(expect.objectContaining({ actor_id: agentId }), expect.any(AbortSignal)))
    expect(screen.queryByLabelText('文档正文')).not.toBeInTheDocument()
  })

  it('guards browser history navigation without silently dropping a dirty document', async () => {
    const api = apiMock()
    history.replaceState({}, '', '?library=1&actor=main')
    render(<LibraryWorkspace api={api} namespace="dirty-history" initialActorId="main" currentActorId="main" agents={[agent]} connected onClose={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: '新建文档' }))
    fireEvent.change(screen.getByLabelText('文档正文'), { target: { value: '历史导航前的草稿' } })
    act(() => { history.replaceState({}, '', `?library=1&actor=${agentId}`); window.dispatchEvent(new PopStateEvent('popstate')) })
    expect(screen.getByRole('group', { name: '确认关闭未保存文档' })).toBeVisible()
    expect(new URL(location.href).searchParams.get('actor')).toBe('main')
    fireEvent.click(screen.getByRole('button', { name: '继续编辑' }))
    expect(screen.getByLabelText('文档正文')).toHaveValue('历史导航前的草稿')
  })

  it('opens the acknowledged new version once without a discard confirmation', async () => {
    const api = apiMock(), before = history.length
    render(<LibraryWorkspace api={api} namespace="saved-navigation" initialActorId="main" currentActorId="main" agents={[agent]} connected onClose={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: '新建文档' }))
    fireEvent.change(screen.getByLabelText('文档标题'), { target: { value: '预算核验' } })
    fireEvent.change(screen.getByLabelText('文档正文'), { target: { value: '# 预算\n\n825 元' } })
    fireEvent.click(screen.getByRole('button', { name: '保存新版本' }))
    expect(await screen.findByRole('heading', { name: '预算核验' })).toBeVisible()
    expect(screen.queryByRole('group', { name: '确认关闭未保存文档' })).not.toBeInTheDocument()
    expect(history.length).toBe(before + 1)
  })
})

describe('artifact contract rejection', () => {
  it('rejects missing identities, mismatched exact versions and unknown preview types', () => {
    expect(() => validateArtifact({ ...snapshot(), artifact_id: 'not-an-id' })).toThrow('不完整')
    expect(() => validateArtifact(snapshot(), { version: 9 })).toThrow('版本不一致')
    expect(() => validatePreview({ type: 'html' } as never)).toThrow('类型不受支持')
    expect(() => validatePreview({ type: 'table', columns: [{ key: 'a', label: 'A' }], rows: [{ a: 1 }], total_rows: 0, preview_limit: 2000 })).toThrow('范围不完整')
  })
})
