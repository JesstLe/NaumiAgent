import type { Run, StreamEvent } from './WorkbenchRuntimeClient'
import type { MessageResponse } from './types'

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
  action?: string
}
export type ExecutionTimelineStep = ReasoningStep | ToolTimelineStep
const stringify = (value: unknown): string => value == null ? '' : typeof value === 'string' ? value : JSON.stringify(value, null, 2)

const normalizedText = (value: unknown): string => typeof value === 'string'
  ? value.replace(/\s+/g, ' ').trim()
  : ''

const runRequest = (run: Run): string => normalizedText(
  run.steps.find(step => step.stage === 'request')?.summary
  ?? run.steps.find(step => step.stage === 'command')?.summary,
)

/** Associate each durable execution with the user turn that started it. */
export function runsByUserMessage(
  messages: MessageResponse[],
  runs: Run[],
): Map<string, Run> {
  const users = messages.filter(message => message.role === 'user')
  const userIds = new Set(users.map(message => message.id))
  const assigned = new Map<string, Run>()
  const usedRuns = new Set<string>()

  for (const run of runs) {
    if (run.user_message_id && userIds.has(run.user_message_id) && !assigned.has(run.user_message_id)) {
      assigned.set(run.user_message_id, run)
      usedRuns.add(run.id)
    }
  }

  const remainingUsers = users.filter(message => !assigned.has(message.id))
  const remainingRuns = runs
    .filter(run => !usedRuns.has(run.id))
    .sort((left, right) => left.started_at.localeCompare(right.started_at) || left.id.localeCompare(right.id))
  let userCursor = remainingUsers.length - 1

  for (let runIndex = remainingRuns.length - 1; runIndex >= 0 && userCursor >= 0; runIndex--) {
    const run = remainingRuns[runIndex]
    const request = runRequest(run)
    let match = -1
    if (request) {
      for (let index = userCursor; index >= 0; index--) {
        const content = normalizedText(remainingUsers[index].content)
        if (content.startsWith(request) || request.startsWith(content.slice(0, 160))) {
          match = index
          break
        }
      }
    }
    if (match < 0) match = userCursor
    assigned.set(remainingUsers[match].id, run)
    userCursor = match - 1
  }
  return assigned
}
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

export interface ActivityContext { objective?: string; workspace?: string }
const excerpt = (value: unknown, limit = 180): string => typeof value !== 'string' ? '' : value
  .replace(/sk-[\w-]{20,}|gh[pousr]_[\w]{20,}/g, '[已隐藏]')
  .replace(/(authorization\s*[:=]\s*(?:bearer\s+)?|(?:api[_-]?key|password|secret|token)\s*[=:]\s*|bearer\s+)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;"']+)/gi, '$1[已隐藏]')
  .replace(/[\x00-\x1f\x7f]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, limit)

/** Old runs have no public_action; recover only the operation's target, never analysis text. */
function legacyAction(name: string, input: unknown): string {
  let args: Record<string, unknown> = {}
  try {
    const parsed = typeof input === 'string' ? JSON.parse(input) : input
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) args = parsed
  } catch { /* Old or truncated argument records may not be JSON. */ }
  const target = excerpt(args.path ?? args.file_path ?? args.command ?? args.pattern ?? args.query)
  const actions: Record<string, string> = {
    read: '读取文件', file_read: '读取文件', read_file: '读取文件',
    file_write: '修改文件', write_file: '修改文件', file_edit: '修改文件', write: '修改文件',
    bash_run: '在工作目录执行命令', exec_command: '执行命令',
    glob: '搜索文件', grep: '搜索文件内容', browser_observe: '查看页面元素与布局',
    browser_screenshot: '截取页面，记录当前布局', browser_evaluate: '在页面执行检查脚本',
  }
  return `${actions[name] || `调用工具 ${excerpt(name)}`}${target ? `：${target}` : ''}`
}

const reasoningLabel = (turn: number, context: ActivityContext, previous?: ToolTimelineStep) => {
  if (previous) {
    const outcome = { completed: '已完成', running: '仍在执行', failed: '执行失败', cancelled: '已取消', unknown: '结果未确认' }[previous.state]
    return `第 ${turn} 轮 · 上一步${outcome}：${previous.action || previous.label}`
  }
  const objective = excerpt(context.objective)
  const workspace = excerpt(context.workspace)
  return `${objective ? `本次任务：${objective}` : `第 ${turn} 轮 · 等待具体操作或答复`}${workspace ? `\n工作目录：${workspace}` : ''}`
}

export function isTimelineEvent(event: StreamEvent): boolean {
  return ['turn_start', 'thinking_start', 'thinking_end', 'tool_call_start', 'tool_call_end',
    'tool_call_error', 'permission_request', 'agent_end', 'agent_error', 'context_compacted'].includes(event.type)
    || (event.type === 'runtime_event' && event.data.event === 'task_snapshot')
}

function progressLabel(type: string, data: Record<string, unknown>): string {
  const supplied = excerpt(data.activity_summary, 500)
  if (supplied) return supplied
  // Older daemons already emit facts, but do not yet attach a public summary.
  if (type === 'context_compacted') {
    const count = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
    const size = count(data.before) && count(data.after) ? `：${data.before} → ${data.after} 条消息` : ''
    const archived = count(data.archived_tool_results) && data.archived_tool_results > 0 ? `；归档 ${data.archived_tool_results} 条工具结果` : ''
    return `已压缩上下文${size}${archived}`
  }
  if (type === 'task_snapshot' && Array.isArray(data.items)) {
    const states: Record<string, string> = { pending: '待处理', in_progress: '进行中', blocked: '受阻' }
    const tasks = data.items.slice(0, 3).flatMap(item => item && typeof item === 'object' && excerpt(item.subject)
      ? [`${states[String(item.status)] || '状态未确认'}：${excerpt(item.subject, 100)}`] : [])
    if (typeof data.completed_count === 'number' && Number.isSafeInteger(data.completed_count) && data.completed_count >= 0) tasks.push(`已完成 ${data.completed_count} 项`)
    return tasks.length ? `执行计划 · ${tasks.join('；')}` : ''
  }
  return ''
}

/** Build the public, ordered execution trace. Raw model reasoning is intentionally absent. */
export function liveExecutionTimeline(
  events: StreamEvent[],
  busy: boolean,
  context: ActivityContext = {},
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
  const seen = new Set<string>()
  for (const event of events) {
    if (seen.has(event.id)) continue
    seen.add(event.id)
    const turn = eventTurn(event)
    if (event.type === 'context_compacted' || (event.type === 'runtime_event' && event.data.event === 'task_snapshot')) {
      const data = event.type === 'runtime_event' ? event.data.data : event.data
      if (data && typeof data === 'object' && !Array.isArray(data)) {
        const label = progressLabel(event.type === 'runtime_event' ? 'task_snapshot' : event.type, data as Record<string, unknown>)
        if (label) rows.push({ id: `activity:${event.id}`, kind: 'reasoning', label, state: 'completed', turn })
      }
      continue
    }
    if (['turn_start', 'thinking_start', 'thinking_delta', 'thinking_end'].includes(event.type)) {
      const id = `reasoning:${turn}`
      const ended = event.type === 'thinking_end'
      const previous = [...rows].reverse().find((row): row is ToolTimelineStep => row.kind !== 'reasoning')
      upsert(
        id,
        () => ({ id, kind: 'reasoning', label: reasoningLabel(turn, context, previous), state: ended ? 'completed' : 'running', turn }),
        row => ({ ...row, state: ended ? 'completed' : row.state }),
      )
      continue
    }
    if (!['tool_call_start', 'tool_call_end', 'tool_call_error', 'permission_request'].includes(event.type)) continue
    const latestReasoning = [...rows].reverse().find(row => row.kind === 'reasoning' && row.state === 'running')
    if (latestReasoning) latestReasoning.state = 'completed'
    const id = String(event.data.call_id ?? event.data.tool_call_id ?? event.id)
    const positionId = `tool:${event.run_id ? `${event.run_id}:` : ''}${id}`
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
      action: excerpt(event.data.activity_summary) || previous?.action || legacyAction(
        String(event.data.name ?? event.data.tool_name ?? previous?.label ?? '工具'),
        event.data.arguments ?? event.data.args ?? previous?.input,
      ),
    }
    upsert(positionId, () => next, () => next)
  }
  if (busy && !rows.length) {
    rows.push({ id: 'reasoning:pending', kind: 'reasoning', label: reasoningLabel(1, context), state: 'running', turn: 1 })
  }
  if (!busy) {
    const terminal = [...events].reverse().find(event => ['agent_end', 'agent_error'].includes(event.type))
    const state = terminal?.type === 'agent_error' ? 'failed' : activityState(String(terminal?.data.status || 'unknown'))
    for (const row of rows) if (row.state === 'running') row.state = state === 'completed' && row.kind !== 'reasoning' ? 'unknown' : state
  }
  return rows
}

export function runExecutionTimeline(run: Run, context: ActivityContext = {}): ExecutionTimelineStep[] {
  const request = run.steps.find(step => step.stage === 'request')
  const runContext = { ...context, objective: request?.summary || context.objective }
  let previous: ToolTimelineStep | undefined
  return run.steps
    .filter(step => ['analysis', 'tool', 'approval', 'activity'].includes(step.stage))
    .map((step): ExecutionTimelineStep => {
      if (step.stage === 'activity') {
        return { id: `${run.id}:activity:${step.sequence}`, kind: 'reasoning', label: excerpt(step.summary, 500), state: activityState(step.status), turn: 0 }
      }
      if (step.stage === 'analysis') {
        const match = step.summary.match(/(\d+)/)
        const turn = match ? Number(match[1]) : 1
        return {
          id: `${run.id}:reasoning:${step.sequence}`,
          kind: 'reasoning',
          label: reasoningLabel(turn, runContext, previous),
          state: activityState(step.status) === 'running' && activityState(run.status) !== 'running' ? 'unknown' : activityState(step.status),
          turn,
        }
      }
      previous = {
        id: `${run.id}:${step.metadata?.tool_call_id || step.sequence}`,
        kind: step.stage === 'approval' ? 'approval' : 'tool',
        label: step.summary || step.stage,
        state: activityState(step.status) === 'running' && activityState(run.status) !== 'running' ? 'unknown' : activityState(step.status),
        input: step.metadata?.input || '',
        output: step.detail || '',
        outputRecorded: step.metadata?.output_recorded,
        outputTruncated: step.metadata?.output_truncated,
        action: excerpt(step.metadata?.public_action) || legacyAction(step.summary, step.metadata?.input),
      }
      return previous
    })
}
