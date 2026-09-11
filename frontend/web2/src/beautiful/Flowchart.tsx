import { useMemo } from 'react'
import { useWorkspace } from '@naumi/shared/hooks/WorkspaceProvider'
import { dependencyGraph } from '@naumi/shared/api/dependencies'
import Primitive from './upstream/components/primitives/Flowchart'
export function Flowchart() {
  const w = useWorkspace()
  const graph = useMemo(() => dependencyGraph(w.todos), [w.todos])
  const steps = useMemo(() => graph.ordered.map((todo, row) => ({ id: todo.id, row, x: .5, w: 300,
    title: todo.subject, caption: todo.active_form || `#${todo.id}${todo.blocked_by.length ? ` · 依赖 ${todo.blocked_by.map(id => `#${id}`).join('、')}` : ' · 无前置依赖'}`,
    kind: { label: { pending: '待处理', in_progress: '进行中', completed: '已完成', blocked: '受阻' }[todo.status], hue: { pending: '#9a5cff', in_progress: '#3d9aff', completed: '#31a66c', blocked: '#f09a2f' }[todo.status] },
    hue: '#9a5cff',
  })), [graph])
  return <div className="bui-root" aria-label="任务依赖图">
    <header className="bui-section-heading"><span>任务依赖 <small>{steps.length} 个节点 · {graph.edges.length} 条连接</small></span><button disabled={w.tasksLoading || w.tasksMutating} onClick={() => void w.refreshTasks()}>刷新</button></header>
    {w.taskError && <p className="w2-error" role="alert">{w.taskError}</p>}
    {graph.warnings.map(message => <p className="w2-error" role="alert" key={message}>{message}</p>)}
    {steps.length ? <Primitive steps={steps} edges={graph.edges} /> : <p className="bui-empty">当前会话暂无待办依赖</p>}
  </div>
}
