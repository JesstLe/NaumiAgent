import { useMemo } from 'react'
import { useWorkspace } from '@naumi/shared/hooks/WorkspaceProvider'
import { distribution } from '@naumi/shared/api/distribution'
import Primitive, { AllocationCard, type InsightPage } from './upstream/components/primitives/InsightCards'
import { healthNames, SnapshotHeading } from './ContextCards'

export function InsightCards({ onInspect }: { onInspect: (key: string) => void }) {
  const w = useWorkspace()
  const pages = useMemo<InsightPage[]>(() => {
    const groups: { key: string; title: string; badge: string; values: string[]; labels: Record<string, string>; pill: string }[] = [
      { key: 'todos', title: '任务状态', badge: 'T', values: w.todos.map(todo => todo.status), labels: { pending: '待处理', in_progress: '进行中', blocked: '受阻', completed: '已完成' }, pill: '查看待办' },
      { key: 'validation', title: '验证结果', badge: 'V', values: (w.snapshot?.validation_runs ?? []).map(run => run.status), labels: { passed: '通过', failed: '失败' }, pill: '刷新验证结果' },
      { key: 'context', title: '上下文健康', badge: 'C', values: (w.snapshot?.context_snapshots ?? []).map(snapshot => snapshot.health), labels: healthNames, pill: '查看上下文依据' },
    ]
    return groups.filter(group => group.values.length).map(group => {
      const rows = distribution(group.values, group.labels)
      const segments = rows.map((row, index) => ({ name: row.label, label: row.label, pct: row.percent, amount: `${row.count} / ${group.values.length} 条`, cls: ['bg-orange', 'bg-accent', 'bg-green', 'bg-line-strong', 'bg-red'][index % 5], tone: 'text-ink-2' }))
      return { key: group.key, prose: `${group.values.length} 条${group.title}记录`, pill: group.pill,
        Card: () => <AllocationCard segments={segments} title={group.title} badge={group.badge} description={rows.map(row => `${row.label} ${row.count} 条`).join(' · ')} /> }
    })
  }, [w.todos, w.snapshot])
  return <div className="bui-root" aria-label="会话洞察"><SnapshotHeading title="会话统计" count={pages.length} />
    {pages.length ? <Primitive pages={pages} labels={{ title: '洞察' }} onAction={key => key === 'validation' ? void w.refreshSnapshot() : onInspect(key)} /> : <p className="bui-empty">当前会话暂无可统计记录</p>}
  </div>
}
