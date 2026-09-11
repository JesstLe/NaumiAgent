import type { Run, StreamEvent } from './WorkbenchRuntimeClient'

export type ActivityState = 'running' | 'completed' | 'failed' | 'cancelled' | 'unknown'
export interface ActivityStep { id: string; label: string; state: ActivityState; input: string; output: string; outputRecorded?: boolean; outputTruncated?: boolean }
export interface ReasoningStep {
  id: string
  kind: 'reasoning'
  label: string
  state: ActivityState
  turn: number
}
export interface ToolTimelineStep extends ActivityStep {
  kind: 'tool' | 'approval'
}
export type ExecutionTimelineStep = ReasoningStep | ToolTimelineStep
const stringify = (value: unknown): string => value == null ? '' : typeof value === 'string' ? value : JSON.stringify(value, null, 2)
export function activityState(status: string): ActivityState {
  if (['completed', 'success', 'passed'].includes(status)) return 'completed'
  if (['failed', 'error'].includes(status)) return 'failed'
  if (['running', 'in_progress'].includes(status)) return 'running'
  return status === 'cancelled' ? 'cancelled' : 'unknown'
}
export function toolActivity(events: StreamEvent[], busy: boolean): ActivityStep[] {
  const rows = new Map<string, ActivityStep>()
  const seen = new Set<string>()
  for (const event of events) {
    if (seen.has(event.id) || !event.type.startsWith('tool_call_')) continue
    seen.add(event.id)
    const call = event.data.call_id ?? event.data.tool_call_id
    const id = `${event.run_id ?? ''}:${typeof call === 'string' ? call : event.id}`
    const previous = rows.get(id)
    rows.set(id, {
      id, label: String(event.data.name ?? event.data.tool_name ?? previous?.label ?? '工具'),
      state: event.type === 'tool_call_start' ? (busy ? 'running' : 'unknown') : event.type === 'tool_call_error' ? 'failed' : typeof event.data.status === 'string' ? activityState(event.data.status) : 'completed',
      input: stringify(event.data.arguments ?? event.data.args ?? previous?.input),
      output: stringify(event.data.content ?? event.data.result ?? event.data.message ?? previous?.output),
      outputRecorded: event.type !== 'tool_call_start' || previous?.outputRecorded,
      outputTruncated: typeof event.data.content_length === 'number' && event.data.content_length > stringify(event.data.content ?? event.data.result ?? event.data.message).length,
    })
  }
  return [...rows.values()]
}
export function runActivity(run: Run): ActivityStep[] {
  return run.steps.filter(step => step.stage === 'tool' || step.stage === 'approval').map(step => ({
    id: `${run.id}:${step.metadata?.tool_call_id || step.sequence}`, label: step.summary || step.stage,
    state: activityState(step.status) === 'running' && activityState(run.status) !== 'running' ? 'unknown' : activityState(step.status),
    input: step.metadata?.input || '', output: step.detail || '',
    outputRecorded: step.metadata?.output_recorded, outputTruncated: step.metadata?.output_truncated,
  }))
}

const eventTurn = (event: StreamEvent): number => {
  const value = event.turn ?? event.data.turn
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : 1
}

const reasoningLabel = (turn: number) =>
  turn > 1
    ? '我会结合刚才的工具结果继续判断，并确定下一步操作。'
    : '我会先理解请求并检查当前上下文，然后选择需要执行的工具。'

/** Build the public, ordered execution trace. Raw model reasoning is intentionally absent. */
export function liveExecutionTimeline(
  events: StreamEvent[],
  busy: boolean,
): ExecutionTimelineStep[] {
  const rows: ExecutionTimelineStep[] = []
  const positions = new Map<string, number>()
  const upsert = (id: string, create: () => ExecutionTimelineStep, update: (row: ExecutionTimelineStep) => ExecutionTimelineStep) => {
    const position = positions.get(id)
    if (position === undefined) {
      positions.set(id, rows.length)
      rows.push(create())
    } else rows[position] = update(rows[position])
  }
  for (const event of events) {
    const turn = eventTurn(event)
    if (['turn_start', 'thinking_start', 'thinking_delta', 'thinking_end'].includes(event.type)) {
      const id = `reasoning:${turn}`
      const ended = event.type === 'thinking_end'
      upsert(
        id,
        () => ({ id, kind: 'reasoning', label: reasoningLabel(turn), state: ended ? 'completed' : 'running', turn }),
        row => ({ ...row, state: ended ? 'completed' : row.state }),
      )
      continue
    }
    if (!['tool_call_start', 'tool_call_end', 'tool_call_error', 'permission_request'].includes(event.type)) continue
    const latestReasoning = [...rows].reverse().find(row => row.kind === 'reasoning' && row.state === 'running')
    if (latestReasoning) latestReasoning.state = 'completed'
    const id = String(event.data.call_id ?? event.data.tool_call_id ?? event.id)
    const positionId = `tool:${id}`
    const previous = positions.has(positionId) ? rows[positions.get(positionId)!] as ToolTimelineStep : undefined
    const permission = event.type === 'permission_request'
    const state = permission && event.data.status === 'needs_confirmation'
      ? 'running'
      : event.type === 'tool_call_start'
        ? (busy ? 'running' : 'unknown')
        : event.type === 'tool_call_error'
          ? 'failed'
          : typeof event.data.status === 'string'
            ? activityState(event.data.status)
            : 'completed'
    const next: ToolTimelineStep = {
      id: positionId,
      kind: permission ? 'approval' : 'tool',
      label: String(event.data.name ?? event.data.tool_name ?? previous?.label ?? '工具'),
      state,
      input: stringify(event.data.arguments ?? event.data.args ?? previous?.input),
      output: stringify(event.data.content ?? event.data.result ?? event.data.message ?? previous?.output),
      outputRecorded: event.type !== 'tool_call_start' || previous?.outputRecorded,
      outputTruncated: typeof event.data.content_length === 'number' && event.data.content_length > stringify(event.data.content ?? event.data.result ?? event.data.message).length,
    }
    upsert(positionId, () => next, () => next)
  }
  if (busy && !rows.length) {
    rows.push({ id: 'reasoning:pending', kind: 'reasoning', label: reasoningLabel(1), state: 'running', turn: 1 })
  }
  if (!busy) {
    for (const row of rows) if (row.kind === 'reasoning' && row.state === 'running') row.state = 'completed'
  }
  return rows
}

export function runExecutionTimeline(run: Run): ExecutionTimelineStep[] {
  return run.steps
    .filter(step => ['analysis', 'tool', 'approval'].includes(step.stage))
    .map((step): ExecutionTimelineStep => {
      if (step.stage === 'analysis') {
        const match = step.summary.match(/(\d+)/)
        const turn = match ? Number(match[1]) : 1
        return {
          id: `${run.id}:reasoning:${step.sequence}`,
          kind: 'reasoning',
          label: step.detail || (/^第 \d+ 轮分析$/.test(step.summary) || step.summary === '分析请求'
            ? reasoningLabel(turn)
            : step.summary),
          state: activityState(step.status),
          turn,
        }
      }
      return {
        id: `${run.id}:${step.metadata?.tool_call_id || step.sequence}`,
        kind: step.stage === 'approval' ? 'approval' : 'tool',
        label: step.summary || step.stage,
        state: activityState(step.status) === 'running' && activityState(run.status) !== 'running' ? 'unknown' : activityState(step.status),
        input: step.metadata?.input || '',
        output: step.detail || '',
        outputRecorded: step.metadata?.output_recorded,
        outputTruncated: step.metadata?.output_truncated,
      }
    })
}
