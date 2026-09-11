// Adapted from Beautiful UI ThinkingState, MIT (see LICENSE).
// Shows public execution facts, without generated reasoning or timed success.
import { ChevronDown, ListChecks } from 'lucide-react'
import { useWorkspace } from '@naumi/shared/hooks/WorkspaceProvider'
import { activityState, runActivity, toolActivity } from '@naumi/shared/api/activity'
import { activityNames, ToolChips } from './ToolChips'

export function ThinkingState() {
  const w = useWorkspace()
  const latest = w.runs[0]
  const live = w.liveEvents.length > 0
  const steps = live ? toolActivity(w.liveEvents, w.busy) : latest ? runActivity(latest) : []
  if (!steps.length) return null
  const status = w.busy ? 'running' : latest ? activityState(latest.status) : 'unknown'
  return <details className="bui-thinking" aria-label="执行过程" key={w.sessionId}>
    <summary><ListChecks size={15} /><span>{w.busy ? '正在执行' : '最近执行过程'}</span><small>{steps.length} 条记录</small><ChevronDown size={13} /></summary>
    <div className="bui-trace-body">
      <div className="bui-trace-meta"><span>{activityNames[status]}</span>{latest && !w.busy && <time>{new Date(latest.started_at).toLocaleString('zh-CN')}</time>}</div>
      <ToolChips steps={steps} />
    </div>
  </details>
}
