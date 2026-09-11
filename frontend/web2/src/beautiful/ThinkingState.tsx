import { useWorkspace } from '@naumi/shared/hooks/WorkspaceProvider'
import { activityState, runActivity, toolActivity } from '@naumi/shared/api/activity'
import Primitive from './upstream/components/primitives/ThinkingState'
import { activityNames, ToolChips } from './ToolChips'
export function ThinkingState() {
  const w = useWorkspace()
  const latest = w.runs[0]
  const steps = w.liveEvents.length ? toolActivity(w.liveEvents, w.busy) : latest ? runActivity(latest) : []
  if (!steps.length) return null
  const status = w.busy ? 'running' : latest ? activityState(latest.status) : 'unknown'
  return <div className="bui-root bui-execution" aria-label="执行过程" key={w.sessionId}>
    <Primitive rows={[]} active="正在执行" done={`最近执行过程 · ${activityNames[status]}`} working={w.busy}><ToolChips steps={steps} /></Primitive>
  </div>
}
