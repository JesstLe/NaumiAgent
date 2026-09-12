import { useEffect, useMemo, useState } from 'react'
import { useWorkspace } from '@naumi/shared/hooks/WorkspaceProvider'
import {
  activityState,
  executionStages,
  liveExecutionActivity,
  liveExecutionTimeline,
  runExecutionTimeline,
  type ExecutionStage,
  type LiveExecutionActivity,
  type ToolTimelineStep,
} from '@naumi/shared/api/activity'
import type { Run } from '@naumi/shared/api/WorkbenchRuntimeClient'
import { ToolChips } from './ToolChips'
import Primitive, { type ThinkingRow } from './upstream/components/primitives/ThinkingState'
import { AgentAvatar } from '../community/AgentAvatar'
import { MessageContent } from '../rich/MessageContent'

const webTools = /^(web_fetch|fetch|browser_goto|browser_observe|browser_screenshot|browser_evaluate|web_search)$/
const splitAction = (step: ToolTimelineStep) => {
  const action = step.action || step.label
  const separator = action.indexOf('：')
  return {
    operation: separator >= 0 ? action.slice(0, separator) : action,
    target: separator >= 0 ? action.slice(separator + 1) : '',
  }
}

const stageVariant = (stage: ExecutionStage) => {
  if (!stage.tools.length) {
    if (stage.notes.at(-1)?.state === 'running') return 'Reasoning'
    return stage.notes.some(note => /执行计划|压缩上下文/.test(note.label)) ? 'Steps' : 'Reasoning'
  }
  return stage.tools.every(step => webTools.test(step.label)) ? 'Search' : 'Coding'
}

const stageRows = (stage: ExecutionStage, variant: string): ThinkingRow[] => {
  if (variant === 'Steps' || variant === 'Reasoning') {
    return stage.notes.map(note => ({ primary: note.label }))
  }
  return stage.tools.map(step => {
    const { operation, target } = splitAction(step)
    return {
      primary: operation.replace(/^调用工具 /, ''),
      secondary: target,
      mono: Boolean(target),
      href: variant === 'Search' && /^https?:\/\//.test(target) ? target : undefined,
    }
  })
}

const stageSummary = (stage: ExecutionStage) => {
  const actions = stage.tools.map(step => step.action || step.label)
  const visible = actions.slice(0, 3)
  const remaining = actions.length > visible.length ? `；另有 ${actions.length - visible.length} 项` : ''
  const facts = `${visible.join('；')}${remaining}`
  const progress = stage.notes
    .map(note => note.label)
    .filter(label => /^(执行计划|已压缩上下文)/.test(label))
  const recorded = stage.notes.map(note => note.label).find(label => /^本阶段/.test(label))
  if (recorded) return [recorded, ...progress].join('\n\n')
  if (!actions.length) return progress.join('\n\n')
  const lead = stage.state === 'running'
    ? '本阶段正在执行'
    : stage.state === 'failed'
      ? '本阶段执行存在失败'
      : stage.state === 'cancelled'
        ? '本阶段已停止'
        : stage.state === 'unknown'
          ? '本阶段结果仍待确认'
          : '本阶段已完成'
  return [`${lead}：${facts}。`, ...progress].join('\n\n')
}

function StageTrace({
  stage,
  terminalLabel,
  working,
  activity,
}: {
  stage: ExecutionStage
  terminalLabel?: string
  working: boolean
  activity?: LiveExecutionActivity
}) {
  const variant = stageVariant(stage)
  const rows = stageRows(stage, variant).filter((row, index, all) => !(
    working
    && activity?.headline
    && index === all.length - 1
    && row.primary === activity.headline
  ))
  const summary = stageSummary(stage)
  const active = activity?.headline || (variant === 'Search'
    ? '正在查看参考与页面'
    : variant === 'Steps'
      ? '正在更新执行步骤'
      : variant === 'Reasoning'
        ? '正在推理'
        : `正在调用 ${stage.tools.length} 个工具`)
  const doneLabel = variant === 'Search'
    ? `已查看 ${stage.tools.length} 项参考`
    : variant === 'Steps'
      ? '执行进度已更新'
      : variant === 'Reasoning'
        ? '任务已整理'
        : `${stage.tools.length} 次工具调用`
  const done = terminalLabel || `${doneLabel} · 阶段已记录`
  const query = variant === 'Search'
    ? stage.tools.map(splitAction).find(item => item.target)?.target
    : undefined
  return <section className="bui-stage" data-turn={stage.turn} data-variant={variant}>
    <Primitive
      variant={variant}
      rows={variant === 'Coding' ? [] : rows}
      query={query}
      active={active}
      done={done}
      working={working}
      settledExpanded
      compact
    >
      {variant === 'Coding' && <ToolChips steps={stage.tools} status={status} working={working} showHeader={false} />}
    </Primitive>
    {summary && stage.tools.length > 0 && <div className="bui-stage-summary"><MessageContent content={summary} plain /></div>}
    {activity?.summary && <div className="bui-live-summary" role="status" aria-live="polite" aria-label="流式思考摘要">
      <span className="bui-live-summary-label">思考摘要</span>
      <span className="bui-live-summary-text">{activity.summary}</span>
    </div>}
    {activity?.detail && <div className="bui-stage-summary" data-freshness={activity.freshness}><MessageContent content={activity.detail} plain /></div>}
  </section>
}

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
  const stages = useMemo(() => executionStages(steps), [steps])
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (!w.busy) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [w.busy])
  if (!stages.length) return null
  const terminal = [...w.liveEvents].reverse().find(event => ['agent_end', 'agent_error'].includes(event.type))
  const liveWorking = useLive && w.busy && !terminal
  const liveStatus = terminal?.type === 'agent_error'
    ? 'failed'
    : typeof terminal?.data.status === 'string'
      ? activityState(terminal.data.status)
      : 'unknown'
  const status = liveWorking ? 'running' : run && !useLive ? activityState(run.status) : liveStatus
  const firstEvent = w.liveEvents.find(event => event.timestamp)
  const terminalElapsed = run && !useLive
    ? elapsedLabel(run.started_at, run.completed_at, now)
    : firstEvent && terminal
      ? elapsedLabel(firstEvent.timestamp || '', terminal.timestamp, now)
      : ''
  const terminalLabel = terminalElapsed
    ? status === 'completed'
      ? `任务已完成 · ${terminalElapsed}`
      : status === 'failed'
        ? `任务执行失败 · ${terminalElapsed}`
        : status === 'cancelled'
          ? `任务已停止 · ${terminalElapsed}`
          : `任务状态待确认 · ${terminalElapsed}`
    : undefined
  const liveActivity = useMemo(
    () => useLive ? liveExecutionActivity(w.liveEvents, steps, liveWorking, { objective, workspace }, now) : undefined,
    [useLive, w.liveEvents, steps, liveWorking, objective, workspace, now],
  )
  return <div className="bui-root bui-execution" aria-label="执行过程" data-run-id={run?.id || 'live'}>
    <AgentAvatar seed={run?.id || `live:${w.sessionId || 'new'}`} size={25} working={liveWorking} />
    <div className="bui-execution-body">
      {stages.map((stage, index) => <StageTrace
        key={stage.id}
        stage={stage}
        terminalLabel={index === stages.length - 1 ? terminalLabel : undefined}
        working={liveWorking && index === stages.length - 1}
        activity={liveWorking && index === stages.length - 1 ? liveActivity : undefined}
      />)}
    </div>
  </div>
}
