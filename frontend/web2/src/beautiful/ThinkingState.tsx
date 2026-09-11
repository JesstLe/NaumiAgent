import { useEffect, useMemo, useState } from 'react'
import { FileText, Pencil, Terminal, Wrench } from 'lucide-react'
import { useWorkspace } from '@naumi/shared/hooks/WorkspaceProvider'
import {
  activityState,
  liveExecutionTimeline,
  runExecutionTimeline,
  type ToolTimelineStep,
} from '@naumi/shared/api/activity'
import Primitive from './upstream/components/primitives/ThinkingState'
import { activityNames } from './ToolChips'

const toolIcon = (name: string) => {
  if (/read|读取|fetch|search/i.test(name)) return FileText
  if (/write|edit|patch|创建|编辑|写/i.test(name)) return Pencil
  if (/run|exec|command|shell|执行/i.test(name)) return Terminal
  return Wrench
}

const toolAction = (name: string, running: boolean) => {
  const prefix = running ? '正在' : '已'
  if (/search/i.test(name)) return `${prefix}搜索网页`
  if (/read|fetch|读取/i.test(name)) return `${prefix}读取内容`
  if (/write|edit|patch|创建|编辑|写/i.test(name)) return `${prefix}编辑文件`
  if (/run|exec|command|shell|执行/i.test(name)) return `${prefix}运行命令`
  return `${prefix}调用工具`
}

function InlineTool({ step }: { step: ToolTimelineStep }) {
  const [open, setOpen] = useState(false)
  const Icon = toolIcon(step.label)
  const detail = [
    step.input ? `输入：${step.input}` : '',
    step.output || (step.state === 'running'
      ? '等待工具结果…'
      : step.outputRecorded
        ? '工具未返回文本内容'
        : '旧记录未保存工具输出'),
    step.outputTruncated ? '当前记录仅包含工具返回的输出预览。' : '',
  ].filter(Boolean)
  return <div className="bui-timeline-tool">
    <button
      type="button"
      aria-expanded={open}
      aria-label={`${step.label} ${activityNames[step.state]}`}
      onClick={() => setOpen(value => !value)}
    >
      <Icon size={14} strokeWidth={1.8} />
      <span>{step.state === 'running' || step.state === 'completed' ? toolAction(step.label, step.state === 'running') : activityNames[step.state]}</span>
      <code title={step.input || step.label}>{step.input || step.label}</code>
      <svg viewBox="0 0 24 24" aria-hidden style={{ transform: open ? 'rotate(180deg)' : undefined }}>
        <path d="M6 9l6 6 6-6" />
      </svg>
    </button>
    <div className="bui-timeline-tool-detail" data-open={open}>
      <div><pre>{detail.join('\n\n')}</pre></div>
    </div>
  </div>
}

const elapsedLabel = (startedAt: string, completedAt: string | undefined, now: number) => {
  const start = Date.parse(startedAt)
  if (!Number.isFinite(start)) return '推理完成'
  const end = completedAt ? Date.parse(completedAt) : now
  const seconds = Math.max(0, Math.round((end - start) / 1000))
  if (seconds < 60) return `用时 ${seconds} 秒`
  return `用时 ${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
}

export function ThinkingState() {
  const w = useWorkspace()
  const latest = w.runs[0]
  const liveRun = w.liveEvents.at(-1)?.run_id || w.liveEvents.at(-1)?.data.run_id
  const useSaved = !w.busy && latest && (!w.liveEvents.length || liveRun === latest.id)
  const steps = useMemo(
    () => useSaved ? runExecutionTimeline(latest) : liveExecutionTimeline(w.liveEvents, w.busy),
    [latest, useSaved, w.busy, w.liveEvents],
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
  const status = w.busy ? 'running' : useSaved && latest ? activityState(latest.status) : liveStatus
  const firstEvent = w.liveEvents.find(event => event.timestamp)
  const done = useSaved && latest
    ? `${elapsedLabel(latest.started_at, latest.completed_at, now)} · ${activityNames[status]}`
    : firstEvent
      ? `${elapsedLabel(firstEvent.timestamp || '', terminal?.timestamp, now)} · ${activityNames[status]}`
      : activityNames[status]
  return <div className="bui-root bui-execution" aria-label="执行过程" key={w.sessionId}>
    <Primitive active="正在推理" done={done} working={w.busy} settledExpanded rows={[]}>
      <div className="bui-timeline" aria-label="执行时间线">
        {steps.map(step => step.kind === 'reasoning'
          ? <p key={step.id} className={step.state === 'running' ? 'is-running' : ''}>{step.label}</p>
          : <InlineTool key={step.id} step={step} />)}
      </div>
    </Primitive>
  </div>
}
