import type { Run, StreamEvent } from './WorkbenchRuntimeClient'

export type ActivityState = 'running' | 'completed' | 'failed' | 'cancelled' | 'unknown'
export interface ActivityStep { id: string; label: string; state: ActivityState; input: string; output: string }
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
      state: event.type === 'tool_call_start' ? (busy ? 'running' : 'unknown') : event.type === 'tool_call_error' ? 'failed' : 'completed',
      input: stringify(event.data.arguments ?? event.data.args ?? previous?.input),
      output: stringify(event.data.content ?? event.data.result ?? event.data.message ?? previous?.output),
    })
  }
  return [...rows.values()]
}
export function runActivity(run: Run): ActivityStep[] {
  return run.steps.map(step => ({ id: `${run.id}:${step.sequence}`, label: step.summary || step.stage, state: activityState(step.status), input: '', output: step.detail || '' }))
}
