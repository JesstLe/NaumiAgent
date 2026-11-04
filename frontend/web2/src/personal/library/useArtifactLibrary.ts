import { useCallback, useEffect, useRef, useState } from 'react'
import { WorkbenchRequestError, type WorkbenchRuntimeClient } from '@naumi/shared/api/WorkbenchRuntimeClient'
import type {
  PersonalArtifactGrant, PersonalArtifactInput, PersonalArtifactKind, PersonalArtifactRef,
  PersonalArtifactRevision, PersonalArtifactSnapshot,
} from '@naumi/shared/api/personalArtifacts'

export type ArtifactApi = Pick<WorkbenchRuntimeClient,
  'personalArtifacts' | 'personalArtifact' | 'personalArtifactVersions' | 'personalArtifactPreview'
  | 'personalArtifactContent' | 'createPersonalArtifact' | 'revisePersonalArtifact' | 'importPersonalArtifact' | 'importPersonalArtifactVersion'
  | 'personalArtifactGrants' | 'sharePersonalArtifact' | 'revokePersonalArtifactGrant'
>
const actorPattern = /^(main|pa_[a-f0-9]{32})$/
const artifactPattern = /^art_[a-f0-9]{32}$/
const hashPattern = /^[a-f0-9]{64}$/
const positive = (n: unknown): n is number => Number.isSafeInteger(n) && (n as number) > 0
const counter = (n: unknown): n is number => Number.isSafeInteger(n) && (n as number) >= 0
export const artifactError = (error: unknown) => error instanceof Error ? error.message : '操作未完成，请重试'

export function validateArtifactRef(value: PersonalArtifactRef): PersonalArtifactRef {
  if (!value || !artifactPattern.test(value.artifact_id) || !positive(value.version) || !hashPattern.test(value.sha256)
    || typeof value.grant_id !== 'string' || !counter(value.grant_revision)) throw new Error('资料版本引用不完整，请核对原记录')
  return value
}

export function validateArtifact(value: PersonalArtifactSnapshot, expected?: { id?: string; version?: number; owner?: string }) {
  if (!value || !artifactPattern.test(value.artifact_id) || !positive(value.version) || !actorPattern.test(value.owner_id)
    || typeof value.title !== 'string' || !value.title.trim() || !['material', 'output'].includes(value.kind)
    || !hashPattern.test(value.sha256) || !counter(value.bytes) || typeof value.mime_type !== 'string'
    || typeof value.suffix !== 'string' || typeof value.created_at !== 'string'
    || !Array.isArray(value.source_refs) || !value.evidence || typeof value.evidence !== 'object' || Array.isArray(value.evidence)
    || typeof value.grant_id !== 'string' || !counter(value.grant_revision)
    || typeof value.can_revise !== 'boolean' || typeof value.can_share !== 'boolean')
    throw new Error('资料记录不完整，请重新加载')
  value.source_refs.forEach(validateArtifactRef)
  if (expected?.id && value.artifact_id !== expected.id || expected?.version && value.version !== expected.version
    || expected?.owner && value.owner_id !== expected.owner) throw new Error('资料与所选版本不一致，请重新加载')
  return value
}

export function validateArtifactGrant(value: PersonalArtifactGrant, expected?: { artifactId?: string; version?: number; owner?: string; recipient?: string }) {
  if (!value || typeof value.id !== 'string' || !value.id || !artifactPattern.test(value.artifact_id)
    || !positive(value.version) || !actorPattern.test(value.owner_id) || !actorPattern.test(value.recipient_id)
    || !positive(value.revision) || !['active', 'revoked'].includes(value.status)
    || typeof value.created_at !== 'string' || typeof value.revoked_at !== 'string'
    || !value.recipient_scope || typeof value.recipient_scope !== 'object') throw new Error('分享记录不完整，请重新加载')
  if (expected?.artifactId && value.artifact_id !== expected.artifactId || expected?.version && value.version !== expected.version
    || expected?.owner && value.owner_id !== expected.owner || expected?.recipient && value.recipient_id !== expected.recipient)
    throw new Error('分享记录与原请求不一致，请核对原记录')
  if (value.status === 'revoked' && !value.revoked_at) throw new Error('撤销记录尚无终态时间，请核对原记录')
  return value
}

export async function blobBuffer(blob: Blob): Promise<ArrayBuffer> {
  if (typeof blob.arrayBuffer === 'function') return blob.arrayBuffer()
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(new Error('文件读取失败，请重新选择'))
    reader.onload = () => reader.result instanceof ArrayBuffer ? resolve(reader.result) : reject(new Error('文件读取不完整'))
    reader.readAsArrayBuffer(blob)
  })
}

export async function blobSha256(blob: Blob): Promise<string> {
  if (!crypto.subtle) throw new Error('当前环境未提供文件校验，请在本机应用或受信任连接中重试')
  return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', await blobBuffer(blob))), v => v.toString(16).padStart(2, '0')).join('')
}

type ImportBody = { actor_id: string; request_id: string; title?: string; kind: 'material'; session_id?: string }
type ShareBody = { actor_id: string; request_id: string; recipient_id: string }
type RevokeBody = { actor_id: string; request_id: string; expected_revision: number }
type FileIdentity = { name: string; type: string; bytes: number; sha256: string }
export type ArtifactPending = (
  | { operation: 'create'; body: PersonalArtifactInput }
  | { operation: 'revise'; artifact_id: string; body: PersonalArtifactRevision }
  | { operation: 'import'; body: ImportBody; file: FileIdentity }
  | { operation: 'import_version'; artifact_id: string; body: { actor_id: string; request_id: string; expected_version: number; title?: string; kind: PersonalArtifactKind; session_id?: string }; file: FileIdentity }
  | { operation: 'share'; artifact_id: string; version: number; body: ShareBody }
  | { operation: 'revoke'; grant_id: string; body: RevokeBody }
) & { receipt?: { artifact_id: string; version: number } }

function pendingKey(namespace: string, actor: string) { return `naumi:library:request:${encodeURIComponent(namespace)}:${actor}` }
function parsePending(value: string, actor: string): ArtifactPending {
  const pending = JSON.parse(value) as ArtifactPending
  if (!pending || !pending.body || pending.body.actor_id !== actor || typeof pending.body.request_id !== 'string'
    || !pending.body.request_id || !['create', 'revise', 'import', 'import_version', 'share', 'revoke'].includes(pending.operation)) throw new Error('原请求记录不完整，请先核对资料库')
  if ((pending.operation === 'create' || pending.operation === 'revise')
    && (typeof pending.body.content !== 'string' || typeof pending.body.filename !== 'string' || typeof pending.body.title !== 'string'
      || !['material', 'output'].includes(pending.body.kind))) throw new Error('原请求正文不完整，请先核对资料库')
  if (pending.operation === 'revise' && (!artifactPattern.test(pending.artifact_id) || !positive(pending.body.expected_version))) throw new Error('原修订版本不完整，请先核对资料库')
  if (pending.operation === 'import_version' && (!artifactPattern.test(pending.artifact_id) || !positive(pending.body.expected_version))) throw new Error('原文件修订版本不完整，请先核对资料库')
  if (['import', 'import_version'].includes(pending.operation) && (!('file' in pending) || !pending.file || typeof pending.file.name !== 'string' || typeof pending.file.type !== 'string'
    || !counter(pending.file.bytes) || !hashPattern.test(pending.file.sha256))) throw new Error('原文件校验记录不完整，请先核对资料库')
  if (pending.operation === 'share' && (!artifactPattern.test(pending.artifact_id) || !positive(pending.version) || !actorPattern.test(pending.body.recipient_id))) throw new Error('原分享请求不完整，请先核对资料库')
  if (pending.operation === 'revoke' && (typeof pending.grant_id !== 'string' || !pending.grant_id || !positive(pending.body.expected_revision))) throw new Error('原撤销请求不完整，请先核对资料库')
  if (pending.receipt && (!artifactPattern.test(pending.receipt.artifact_id) || !positive(pending.receipt.version))) throw new Error('原受理凭据不完整，请先核对资料库')
  return pending
}

const pageSize = 50
export function useArtifactLibrary({ api, namespace, actorId, connected, onAccepted }: {
  api: ArtifactApi; namespace: string; actorId: string; connected: boolean
  onAccepted?: (value: PersonalArtifactSnapshot | PersonalArtifactGrant) => void
}) {
  const [artifacts, setArtifacts] = useState<PersonalArtifactSnapshot[]>([])
  const [kind, setKind] = useState<PersonalArtifactKind | ''>(''), [query, setQuery] = useState('')
  const [offset, setOffset] = useState(0), [loading, setLoading] = useState(false), [revision, setRevision] = useState(0)
  const [error, setError] = useState(''), [readError, setReadError] = useState(''), [posting, setPosting] = useState(false)
  const [pending, setPending] = useState<ArtifactPending | null>(null), [identityError, setIdentityError] = useState('')
  const alive = useRef(true), sequence = useRef(0), writeLock = useRef(false), pendingFile = useRef<File | null>(null)
  const key = pendingKey(namespace, actorId)

  useEffect(() => {
    alive.current = true
    try {
      const stored = localStorage.getItem(key)
      setPending(stored ? parsePending(stored, actorId) : null)
      setIdentityError('')
    } catch (cause) { setIdentityError(artifactError(cause)) }
    return () => { alive.current = false; sequence.current += 1 }
  }, [key, actorId])

  useEffect(() => {
    const controller = new AbortController(), token = ++sequence.current
    if (!connected) { setArtifacts([]); setLoading(false); return }
    setLoading(true)
    const timer = setTimeout(() => {
      void api.personalArtifacts({ actor_id: actorId, ...(kind ? { kind } : {}), ...(query.trim() ? { q: query.trim() } : {}), limit: pageSize, offset }, controller.signal)
        .then(result => {
          if (!Array.isArray(result.artifacts)) throw new Error('资料目录返回不完整，请重试')
          const values = result.artifacts.map(item => validateArtifact(item))
          if (new Set(values.map(item => item.artifact_id)).size !== values.length) throw new Error('资料目录包含重复身份，请重新加载')
          if (alive.current && token === sequence.current && !controller.signal.aborted) { setArtifacts(values); setReadError('') }
        }).catch(cause => { if (alive.current && token === sequence.current && !controller.signal.aborted) { setArtifacts([]); setReadError(artifactError(cause)) } })
        .finally(() => { if (alive.current && token === sequence.current) setLoading(false) })
    }, query ? 180 : 0)
    return () => { clearTimeout(timer); controller.abort() }
  }, [api, actorId, connected, kind, query, offset, revision])

  const refresh = useCallback(() => setRevision(value => value + 1), [])
  useEffect(() => {
    const focus = () => refresh()
    window.addEventListener('focus', focus)
    return () => window.removeEventListener('focus', focus)
  }, [refresh])

  const execute = async (request: ArtifactPending, file?: File) => {
    if (writeLock.current || !connected) return null
    writeLock.current = true
    if (alive.current) { setPosting(true); setError('') }
    try {
      let result: PersonalArtifactSnapshot | PersonalArtifactGrant
      if (request.operation === 'create') result = validateArtifact(await api.createPersonalArtifact(request.body), { owner: actorId })
      else if (request.operation === 'revise') result = validateArtifact(await api.revisePersonalArtifact(request.artifact_id, request.body), { id: request.artifact_id, version: request.body.expected_version + 1, owner: actorId })
      else if (request.operation === 'share') result = validateArtifactGrant(await api.sharePersonalArtifact(request.artifact_id, request.version, request.body), { artifactId: request.artifact_id, version: request.version, owner: actorId, recipient: request.body.recipient_id })
      else if (request.operation === 'revoke') {
        result = validateArtifactGrant(await api.revokePersonalArtifactGrant(request.grant_id, request.body), { owner: actorId })
        if (result.id !== request.grant_id || result.status !== 'revoked' || result.revision !== request.body.expected_revision + 1) throw new Error('撤销结果与原请求不一致，请核对原分享记录')
      } else {
        const original = file || pendingFile.current
        if (!original) throw new Error('请选择原文件后重试；原请求编号保持不变')
        if (original.name !== request.file.name || original.type !== request.file.type || original.size !== request.file.bytes
          || await blobSha256(original) !== request.file.sha256) throw new Error('所选文件与原文件不一致，请选择同名且内容相同的原文件')
        pendingFile.current = original
        result = request.operation === 'import_version'
          ? validateArtifact(await api.importPersonalArtifactVersion(request.artifact_id, original, request.body), { id: request.artifact_id, version: request.body.expected_version + 1, owner: actorId })
          : validateArtifact(await api.importPersonalArtifact(original, request.body), { owner: actorId })
        if (result.sha256 !== request.file.sha256 || result.bytes !== request.file.bytes) throw new Error('服务器返回的文件与原文件校验不一致，请核对原请求')
      }
      if (localStorage.getItem(key) !== JSON.stringify(request)) throw new Error('原请求记录在提交期间改变，请核对已受理结果')
      localStorage.removeItem(key)
      pendingFile.current = null
      if (alive.current) { setPending(null); refresh(); onAccepted?.(result) }
      return result
    } catch (cause) {
      if (cause instanceof WorkbenchRequestError && cause.receipt?.accepted === true
        && artifactPattern.test(cause.receipt.artifact_id || '') && positive(cause.receipt.version)) {
        const updated = { ...request, receipt: { artifact_id: cause.receipt.artifact_id!, version: cause.receipt.version! } }
        try { localStorage.setItem(key, JSON.stringify(updated)); if (alive.current) setPending(updated) } catch { /* Keep the original persisted request. */ }
      }
      if (alive.current) setError(artifactError(cause))
      return null
    } finally { writeLock.current = false; if (alive.current) setPosting(false) }
  }

  const begin = async (request: ArtifactPending, file?: File) => {
    if (pending || identityError || writeLock.current || !connected) return null
    try {
      if (localStorage.getItem(key)) throw new Error('已有待核对的原请求，请先核对或原样重试')
      localStorage.setItem(key, JSON.stringify(request))
      setPending(request)
      if (file) pendingFile.current = file
    } catch (cause) { setError(artifactError(cause)); return null }
    return execute(request, file)
  }

  const newRequest = () => 'library-' + crypto.randomUUID()
  const create = (body: Omit<PersonalArtifactInput, 'actor_id' | 'request_id'>) => begin({ operation: 'create', body: { ...body, actor_id: actorId, request_id: newRequest() } })
  const revise = (artifactId: string, body: Omit<PersonalArtifactRevision, 'actor_id' | 'request_id'>) => begin({ operation: 'revise', artifact_id: artifactId, body: { ...body, actor_id: actorId, request_id: newRequest() } })
  const share = (artifactId: string, version: number, recipientId: string) => begin({ operation: 'share', artifact_id: artifactId, version, body: { actor_id: actorId, request_id: newRequest(), recipient_id: recipientId } })
  const revoke = (grant: PersonalArtifactGrant) => begin({ operation: 'revoke', grant_id: grant.id, body: { actor_id: actorId, request_id: newRequest(), expected_revision: grant.revision } })
  const importFile = async (file: File, sessionId?: string) => {
    if (file.size > 20 * 1024 * 1024) { setError('文件超过 20 MB，请选择较小的文件'); return null }
    try {
      return await begin({ operation: 'import', body: { actor_id: actorId, request_id: newRequest(), kind: 'material', ...(sessionId ? { session_id: sessionId } : {}) },
        file: { name: file.name, type: file.type, bytes: file.size, sha256: await blobSha256(file) } }, file)
    } catch (cause) { setError(artifactError(cause)); return null }
  }
  const importVersion = async (snapshot: PersonalArtifactSnapshot, file: File, sessionId?: string) => {
    if (file.size > 20 * 1024 * 1024) { setError('文件超过 20 MB，请选择较小的文件'); return null }
    try {
      return await begin({ operation: 'import_version', artifact_id: snapshot.artifact_id,
        body: { actor_id: actorId, request_id: newRequest(), expected_version: snapshot.version, title: snapshot.title, kind: snapshot.kind, ...(sessionId ? { session_id: sessionId } : {}) },
        file: { name: file.name, type: file.type, bytes: file.size, sha256: await blobSha256(file) } }, file)
    } catch (cause) { setError(artifactError(cause)); return null }
  }
  const abandon = () => {
    if (writeLock.current) return
    try { localStorage.removeItem(key); pendingFile.current = null; setPending(null); setIdentityError(''); setError(''); refresh() }
    catch { setError('原请求记录尚未清除，请检查浏览器存储后重试') }
  }
  return {
    artifacts, kind, query, offset, loading, posting, error: error || readError, pending, identityError, refresh,
    setKind: (value: PersonalArtifactKind | '') => { setKind(value); setOffset(0) },
    setQuery: (value: string) => { setQuery(value); setOffset(0) },
    previous: () => setOffset(Math.max(0, offset - pageSize)), next: () => setOffset(offset + pageSize),
    hasNext: artifacts.length === pageSize, page: offset / pageSize + 1,
    create, revise, share, revoke, importFile, importVersion, abandon,
    retry: (file?: File) => pending ? execute(pending, file) : Promise.resolve(null),
    locked: posting || Boolean(pending) || Boolean(identityError) || !connected,
  }
}

export type ArtifactLibraryState = ReturnType<typeof useArtifactLibrary>
