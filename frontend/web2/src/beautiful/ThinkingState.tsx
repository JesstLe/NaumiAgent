import { useEffect, useMemo, useState } from 'react'
import { useWorkspace } from '@naumi/shared/hooks/WorkspaceProvider'
import {
  activityState,
  liveExecutionTimeline,
  runExecutionTimeline,
} from '@naumi/shared/api/activity'
import type { Run } from '@naumi/shared/api/WorkbenchRuntimeClient'
import { activityNames, ToolChips } from './ToolChips'
import { AgentAvatar } from '../community/AgentAvatar'

const elapsedLabel = (startedAt: string, completedAt: string | undefined, now: number) => {
  const start = Date.parse(startedAt)
  if (!Number.isFinite(start)) return '推理完成'
  const end = completedAt ? Date.parse(completedAt) : now
  const seconds = Math.max(0, Math.round((end - start) / 1000))
  if (seconds < 60) return `用时 ${seconds} 秒`
  return `用时 ${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
}

export function ThinkingState({
  run,
  live = false,
  objective,
}: {
  run?: Run
  live?: boolean
  objective?: string
}) {
  const w = useWorkspace()
  const workspace = w.daemon?.workspace_root
  const useLive = live && (w.busy || !run)
  const steps = useMemo(
    () => useLive
      ? liveExecutionTimeline(w.liveEvents, w.busy, { objective, workspace })
      : run
        ? runExecutionTimeline(run, { objective, workspace })
        : [],
    [run, useLive, w.busy, w.liveEvents, objective, workspace],
  )
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (!w.busy) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [w.busy])
  if (!steps.length) return null
  const terminal = [...w.liveEvents].reverse().find(event => ['agent_end', 'agent_error'].includes(event.type))
  const liveStatus = terminal?.type === 'agent_error'
    ? 'failed'
    : typeof terminal?.data.status === 'string'
      ? activityState(terminal.data.status)
      : 'unknown'
  const status = useLive && w.busy ? 'running' : run && !useLive ? activityState(run.status) : liveStatus
  const firstEvent = w.liveEvents.find(event => event.timestamp)
  const done = run && !useLive
    ? `${elapsedLabel(run.started_at, run.completed_at, now)} · ${activityNames[status]}`
    : firstEvent
      ? `${elapsedLabel(firstEvent.timestamp || '', terminal?.timestamp, now)} · ${activityNames[status]}`
      : activityNames[status]
  return <div className="bui-root bui-execution" aria-label="执行过程" data-run-id={run?.id || 'live'}>
    <AgentAvatar seed={run?.id || `live:${w.sessionId || 'new'}`} size={25} working={useLive && w.busy} />
    <div className="bui-execution-body">
      <ToolChips steps={steps} status={done} working={useLive && w.busy} />
    </div>
  </div>
}
