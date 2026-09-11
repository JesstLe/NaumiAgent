import { describe, expect, it } from 'vitest'
import { liveExecutionTimeline, runActivity, runExecutionTimeline, toolActivity } from '../../src/api/activity'

describe('public execution activity', () => {
  it('pairs concurrent calls by id and retains the correct input and failure', () => {
    const rows = toolActivity([
      { id: '1', type: 'tool_call_start', data: { call_id: 'a', name: 'read', arguments: { path: 'a' } } },
      { id: '2', type: 'tool_call_start', data: { call_id: 'b', name: 'read' } },
      { id: '3', type: 'tool_call_error', data: { call_id: 'a', message: '不存在' } },
      { id: '3', type: 'tool_call_error', data: { call_id: 'a', message: '不存在' } },
    ], false)
    expect(rows).toHaveLength(2)
    expect(rows[0]).toMatchObject({ state: 'failed', output: '不存在' })
    expect(rows[0].input).toContain('"path": "a"')
    expect(rows[1].state).toBe('unknown')
  })
  it('does not invent completion for interrupted or unknown records', () => {
    expect(toolActivity([], false)).toEqual([])
    expect(runActivity({ id: 'r', status: 'cancelled', started_at: '', steps: [{ sequence: 1, stage: 'tool', status: 'cancelled', summary: 'read', detail: '' }] })[0].state).toBe('cancelled')
  })
  it('restores durable output and excludes lifecycle rows from tool counts', () => {
    const rows = runActivity({ id: 'r', status: 'completed', started_at: '', steps: [
      { sequence: 1, stage: 'request', status: 'completed', summary: '继续', detail: '' },
      { sequence: 2, stage: 'analysis', status: 'running', summary: '分析请求', detail: '' },
      { sequence: 3, stage: 'tool', status: 'completed', summary: 'read', detail: 'first\nsecond', metadata: { tool_call_id: 'a', input: 'file.txt', output_recorded: true } },
      { sequence: 4, stage: 'tool', status: 'running', summary: 'write', detail: '' },
    ] })
    expect(rows).toHaveLength(2)
    expect(rows[0]).toMatchObject({ output: 'first\nsecond', input: 'file.txt', outputRecorded: true })
    expect(rows[1].state).toBe('unknown')
  })
  it('interleaves public reasoning rounds and tool calls in event order', () => {
    const rows = liveExecutionTimeline([
      { id: '1', type: 'turn_start', turn: 1, sequence: 1, data: {} },
      { id: '2', type: 'thinking_start', turn: 1, sequence: 2, data: {} },
      { id: '3', type: 'tool_call_start', turn: 1, sequence: 3, data: { call_id: 'a', name: 'read' } },
      { id: '4', type: 'tool_call_end', turn: 1, sequence: 4, data: { call_id: 'a', name: 'read', status: 'success', content: 'ok' } },
      { id: '5', type: 'turn_start', turn: 2, sequence: 5, data: {} },
      { id: '6', type: 'tool_call_start', turn: 2, sequence: 6, data: { call_id: 'b', name: 'write' } },
    ], true)
    expect(rows.map(row => `${row.kind}:${row.id}`)).toEqual([
      'reasoning:reasoning:1',
      'tool:tool:a',
      'reasoning:reasoning:2',
      'tool:tool:b',
    ])
    expect(rows[0].state).toBe('completed')
    expect(rows[2].state).toBe('completed')
    expect(rows[3].state).toBe('running')
  })
  it('creates an immediate reasoning row and restores persisted turn order', () => {
    expect(liveExecutionTimeline([], true)[0]).toMatchObject({ kind: 'reasoning', state: 'running' })
    const rows = runExecutionTimeline({ id: 'r', status: 'completed', started_at: '', steps: [
      { sequence: 1, stage: 'request', status: 'completed', summary: '执行', detail: '' },
      { sequence: 2, stage: 'analysis', status: 'completed', summary: '第 1 轮分析', detail: '' },
      { sequence: 3, stage: 'tool', status: 'completed', summary: 'read', detail: 'ok' },
      { sequence: 4, stage: 'analysis', status: 'completed', summary: '第 2 轮分析', detail: '' },
    ] })
    expect(rows.map(row => row.kind)).toEqual(['reasoning', 'tool', 'reasoning'])
  })
})
