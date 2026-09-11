// Adapted from Beautiful UI ContextCards, MIT (see LICENSE).
// Card content and source badges are backed by actual context snapshots.
import { RefreshCw } from 'lucide-react'
import { useWorkspace } from '@naumi/shared/hooks/WorkspaceProvider'
import type { ContextHealth } from '@naumi/shared/api/types'
import Primitive from './upstream/components/primitives/ContextCards'

export const healthNames: Record<ContextHealth, string> = { good: '正常', stale: '待同步', overloaded: '负载偏高', missing: '缺少上下文', conflicted: '存在冲突' }
export function SnapshotHeading({ title, count }: { title: string; count: number }) {
  const w = useWorkspace()
  return <><header className="bui-section-heading"><span>{title} <small>{count}</small></span><button aria-label={`刷新${title}`} disabled={!w.sessionId || w.snapshotLoading || w.loading || !w.daemon} onClick={() => void w.refreshSnapshot()}><RefreshCw size={14} className={w.snapshotLoading ? 'w2-spin' : ''} /></button></header>
    {w.snapshotError && <p role="alert" className="w2-error">{w.snapshotError}</p>}</>
}
export function ContextCards() {
  const w = useWorkspace()
  const contexts = w.snapshot?.context_snapshots ?? []
  return <div className="bui-root">
    <SnapshotHeading title="上下文快照" count={contexts.length} />
    {!contexts.length && <p className="w2-muted">{w.loading ? '正在读取上下文…' : '当前会话暂无上下文快照'}</p>}
    {!!contexts.length && <Primitive labels={{ header: '上下文依据', count: String(contexts.length) }} chunks={contexts.map(chunk => ({
      title: chunk.task?.subject || w.todos.find(todo => todo.id === chunk.task_id)?.subject || w.snapshot?.tasks.find(task => task.id === chunk.task_id)?.subject || chunk.task_id || '会话上下文', chars: healthNames[chunk.health] || chunk.health,
      body: chunk.reasons.join('\n') || '此快照未附带原因说明', source: `${chunk.agent_id || '未标记来源'} · ${new Date(chunk.created_at).toLocaleString('zh-CN')}`,
      badge: 'CTX', tone: chunk.health === 'good' ? 'bg-green' : 'bg-orange',
    }))} />}
  </div>
}
