import type { ResearchDomain, ResearchDraft, ResearchKind, ResearchNotebook, ResearchOperation, ResearchPending, ResearchReview, ResearchStance, ResearchView } from '@naumi/shared/api/personalResearch'
export type * from '@naumi/shared/api/personalResearch'
export const researchDomains: Record<ResearchDomain, string> = { general: '综合研究', competitor: '竞品', operations: '运营', market: '市场' }
export const researchKinds: Record<ResearchKind, string> = { fact: '事实', estimate: '估算', unknown: '未知' }
export const researchStances: Record<ResearchStance, string> = { act: '执行', watch: '观察', avoid: '暂不执行', undecided: '待判断' }
export const researchReviewNames: Record<ResearchReview['status'], string> = { issues_found: '发现问题', mechanically_checked: '机械检查完成', needs_sources: '待补来源', not_reviewed: '未检查' }
export const researchPendingKey = (namespace: string) => `naumi:research:pending:v1:${encodeURIComponent(namespace)}`
const operations = new Set<ResearchOperation>(['create', 'revise', 'search', 'capture', 'import', 'recheck', 'review', 'publish'])
export const researchRequestId = () => crypto.randomUUID().replaceAll('-', '')
export const cloneResearch = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T

export function parseResearchPending(raw: string): ResearchPending {
  let value: unknown
  try { value = JSON.parse(raw) } catch { throw new Error('原研究请求状态待核对') }
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('原研究请求状态待核对')
  const row = value as Record<string, unknown>
  if (Object.keys(row).sort().join(',') !== 'operation,request_id,target,version' || row.version !== 1
      || typeof row.request_id !== 'string' || !/^[a-f0-9]{32}$/.test(row.request_id)
      || typeof row.operation !== 'string' || !operations.has(row.operation as ResearchOperation)
      || row.target !== null && (typeof row.target !== 'string' || !/^(rq|rs)_[a-f0-9]{32}$/.test(row.target))) throw new Error('原研究请求状态待核对')
  return row as unknown as ResearchPending
}

export function validateResearchView(value: ResearchView, expectedId?: string): ResearchView {
  const n = value?.notebook
  if (!n || !/^rq_[a-f0-9]{32}$/.test(n.id) || expectedId && n.id !== expectedId
      || !Number.isSafeInteger(n.revision) || n.revision < 1 || typeof n.title !== 'string' || typeof n.question !== 'string'
      || !Object.hasOwn(researchDomains, n.domain) || !Array.isArray(n.claims) || n.claims.length > 64
      || !Array.isArray(value.sources) || !value.review || !Object.hasOwn(researchReviewNames, value.review.status)
      || !Array.isArray(value.review.findings) || !Array.isArray(value.review.anchors)) throw new Error('研究记录身份或内容待核对')
  for (const claim of n.claims) {
    if (!/^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(claim.id) || typeof claim.text !== 'string' || !Object.hasOwn(researchKinds, claim.kind)
        || !Array.isArray(claim.citations)) throw new Error('研究声明格式待核对')
    for (const citation of claim.citations) if (!/^rs_[a-f0-9]{32}$/.test(citation.source_id)
        || !Number.isSafeInteger(citation.source_revision) || citation.source_revision < 1 || typeof citation.quote !== 'string') throw new Error('研究引用身份待核对')
  }
  for (const source of value.sources) if (!/^rs_[a-f0-9]{32}$/.test(source.id) || !Number.isSafeInteger(source.revision)
      || source.revision < 1 || !['available', 'unavailable'].includes(source.status)) throw new Error('研究来源身份待核对')
  return value
}

export function researchDraft(value?: ResearchNotebook): ResearchDraft {
  return value ? cloneResearch({ title: value.title, question: value.question, domain: value.domain,
    claims: value.claims.map(claim => ({ ...claim, subject: claim.subject || '', quantity: claim.quantity || null,
      calculation: claim.calculation || null, assumptions: claim.assumptions || [], upstream_claims: claim.upstream_claims || [] })),
    conclusion: value.conclusion || { stance: 'undecided', premises: [], invalidators: [] } })
    : { title: '', question: '', domain: 'general', claims: [], conclusion: { stance: 'undecided', premises: [], invalidators: [] } }
}

export function validateResearchDraft(draft: ResearchDraft) {
  if (!draft.title.trim() || draft.title.length > 160) throw new Error('请填写 160 字以内的研究标题')
  if (!draft.question.trim() || draft.question.length > 4000) throw new Error('请填写 4000 字以内的研究问题')
  if (draft.claims.length > 64 || new Set(draft.claims.map(c => c.id)).size !== draft.claims.length) throw new Error('声明数量或编号待核对')
  for (const claim of draft.claims) {
    if (!claim.text.trim()) throw new Error(`请填写声明 ${claim.id}`)
    if (claim.kind === 'estimate' && !claim.assumptions.some(text => text.trim())) throw new Error(`请填写 ${claim.id} 的估算前提`)
    if (claim.quantity && (!claim.quantity.value.trim() || !claim.quantity.unit.trim())) throw new Error(`请填写 ${claim.id} 的数值和单位`)
    if (claim.quantity?.currency && !/^[A-Z]{3}$/.test(claim.quantity.currency)) throw new Error(`请填写 ${claim.id} 的三位币种代码`)
    if (claim.calculation && (claim.calculation.inputs.length < 2 || claim.calculation.inputs.length > 16 || !claim.calculation.expected.trim())) throw new Error(`请填写 ${claim.id} 的计算输入和预期值`)
    if (claim.upstream_claims.some(ref => !/^rq_[a-f0-9]{32}$/.test(ref.notebook_id) || !Number.isSafeInteger(ref.revision) || ref.revision < 1 || !ref.claim_id.trim())) throw new Error(`请核对 ${claim.id} 的上游引用`)
  }
}

export function researchError(cause: unknown) {
  const status = cause && typeof cause === 'object' && 'status' in cause ? Number(cause.status) : 0
  if (status === 409) return '原版本已变化，当前草稿保留'
  if ([401, 403, 404].includes(status)) return '原研究记录或来源暂不可用，请重新核对'
  if (status >= 500 || cause instanceof Error && /timeout|abort|unknown|fetch/i.test(cause.message)) return '操作结果待核对，请检查原请求'
  return cause instanceof Error ? cause.message.slice(0, 240) : '研究操作未完成，请核对原请求'
}
