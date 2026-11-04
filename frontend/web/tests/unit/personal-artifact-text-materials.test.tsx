import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createHash, webcrypto } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { PersonalArtifactSnapshot } from '@naumi/shared/api/personalArtifacts'
import { ArtifactEditor } from '../../../web2/src/personal/library/ArtifactEditor'
import { ArtifactPage } from '../../../web2/src/personal/library/ArtifactPage'
import type { ArtifactApi, ArtifactLibraryState } from '../../../web2/src/personal/library/useArtifactLibrary'

const id = `art_${'7'.repeat(32)}`
const source = '# source comment\n<script>globalThis.__materialRan = true</script>\n<img src="https://untrusted.test/exfil" onerror="alert(1)">\nconst value = "**literal**";\n'
const pageText = JSON.stringify({ version: 1, type: 'page', title: 'Data only', sections: [{ id: 'one', title: 'A section', markdown: 'Plain data', evidence_status: 'unknown' }] })

function item(suffix: string, content: string, mime = 'text/plain'): PersonalArtifactSnapshot {
  return {
    artifact_id: id, version: 1, owner_id: 'main', title: 'source', kind: 'material',
    sha256: createHash('sha256').update(content).digest('hex'), bytes: new TextEncoder().encode(content).length,
    suffix, mime_type: mime, created_at: '2026-11-04T03:00:00Z', source_refs: [], evidence: {},
    grant_id: '', grant_revision: 0, can_revise: true, can_share: true,
  }
}

function setup(snapshot = item('.py', source), content = source) {
  const api = {
    personalArtifacts: vi.fn().mockResolvedValue({ artifacts: [] }),
    personalArtifact: vi.fn().mockResolvedValue(snapshot),
    personalArtifactVersions: vi.fn().mockResolvedValue({ versions: [snapshot] }),
    personalArtifactPreview: vi.fn().mockResolvedValue({ type: 'text', content, truncated: false }),
    personalArtifactContent: vi.fn(),
  } as unknown as ArtifactApi
  const state = {
    locked: false, posting: false, error: '', pending: null,
    create: vi.fn().mockResolvedValue(null), revise: vi.fn().mockResolvedValue(null),
  } as unknown as ArtifactLibraryState
  return { api, state }
}

beforeEach(() => {
  vi.stubGlobal('crypto', webcrypto)
  history.replaceState({}, '', '/web2')
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => 'blob:unexpected') })
})

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('private source-text material previews', () => {
  it.each(['.py', '.ts', '.yaml', '.log', '.js', '.html'])('keeps %s source literal in the viewer, editor preview and revision', async suffix => {
    const snapshot = item(suffix, source), { api, state } = setup(snapshot)
    const viewer = render(<ArtifactPage api={api} actorId="main" selection={{ artifact_id: id, version: 1 }} agents={[]} state={state} refreshToken={0} onOpen={vi.fn()} onBack={vi.fn()} onEdit={vi.fn()} />)
    await waitFor(() => expect(viewer.container.querySelector('pre.al-plain-text')?.textContent).toBe(source))
    expect(viewer.container.querySelector('script, iframe, img')).toBeNull()
    expect(URL.createObjectURL).not.toHaveBeenCalled()
    expect(api.personalArtifactContent).not.toHaveBeenCalled()
    viewer.unmount()

    const editor = render(<ArtifactEditor value={{ item: snapshot, content: source }} api={api} state={state} actorId="main" onClose={vi.fn()} />)
    expect(screen.getByLabelText('文档正文')).toHaveValue(source)
    fireEvent.click(screen.getByRole('button', { name: '预览', exact: true }))
    expect(editor.container.querySelector('pre.al-plain-text')?.textContent).toBe(source)
    expect(editor.container.querySelector('script, iframe, img')).toBeNull()
    expect(editor.container.querySelector('.al-editor-preview h1')).toBeNull()
    expect((globalThis as typeof globalThis & { __materialRan?: boolean }).__materialRan).toBeUndefined()
    fireEvent.click(screen.getByRole('button', { name: '保存新版本' }))
    await waitFor(() => expect(state.revise).toHaveBeenCalledWith(id, expect.objectContaining({
      filename: `source${suffix}`, kind: 'material', content: source, expected_version: 1,
    })))
  })

  it.each(['.py', '.yaml'])('does not interpret Page-shaped %s source or rewrite it when the title changes', suffix => {
    const snapshot = item(suffix, pageText), { api, state } = setup(snapshot, pageText)
    const editor = render(<ArtifactEditor value={{ item: snapshot, content: pageText }} api={api} state={state} actorId="main" onClose={vi.fn()} />)
    expect(screen.getByLabelText('文档正文')).toHaveValue(pageText)
    expect(screen.queryByLabelText('章节 1 正文')).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('文档标题'), { target: { value: 'Changed source title' } })
    expect(screen.getByLabelText('文档正文')).toHaveValue(pageText)
    fireEvent.click(screen.getByRole('button', { name: '预览', exact: true }))
    expect(editor.container.querySelector('pre.al-plain-text')?.textContent).toBe(pageText)
    expect(editor.container.querySelector('.al-editor-preview section')).toBeNull()
  })

  it('preserves explicit Markdown rendering while skipping active HTML', () => {
    const content = '# Markdown title\n\n<script>globalThis.__materialRan = true</script>'
    const snapshot = item('.md', content, 'text/markdown'), { api, state } = setup(snapshot, content)
    const editor = render(<ArtifactEditor value={{ item: snapshot, content }} api={api} state={state} actorId="main" onClose={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: '预览', exact: true }))
    expect(screen.getByRole('heading', { name: 'Markdown title' })).toBeVisible()
    expect(editor.container.querySelector('script, iframe, img')).toBeNull()
  })

  it('retains structured editing for an actual JSON Page', () => {
    const snapshot = item('.json', pageText, 'application/json'), { api, state } = setup(snapshot, pageText)
    render(<ArtifactEditor value={{ item: snapshot, content: pageText }} api={api} state={state} actorId="main" onClose={vi.fn()} />)
    expect(screen.getByLabelText('章节 1 正文')).toHaveValue('Plain data')
    expect(screen.queryByLabelText('文档正文')).not.toBeInTheDocument()
  })

  it('creates a structured Page only after the user explicitly selects Page', () => {
    const { api, state } = setup()
    render(<ArtifactEditor value={{ content: 'Draft text' }} api={api} state={state} actorId="main" onClose={vi.fn()} />)
    expect(screen.getByLabelText('文档正文')).toHaveValue('Draft text')
    fireEvent.change(screen.getByLabelText('文档格式'), { target: { value: 'page' } })
    expect(screen.getByLabelText('文档文件名')).toHaveValue('document.json')
    expect(screen.getByLabelText('章节 1 正文')).toHaveValue('Draft text')
  })

  it('does not infer Page mode from an untyped new draft body', () => {
    const { api, state } = setup()
    render(<ArtifactEditor value={{ content: pageText }} api={api} state={state} actorId="main" onClose={vi.fn()} />)
    expect(screen.getByLabelText('文档正文')).toHaveValue(pageText)
    expect(screen.getByLabelText('文档格式')).toHaveValue('text')
  })
})
