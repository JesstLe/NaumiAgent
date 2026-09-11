import { describe, expect, it } from 'vitest'
import { runActivity, toolActivity } from '../../src/api/activity'

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
    expect(runActivity({ id: 'r', status: 'cancelled', started_at: '', steps: [{ sequence: 1, stage: 'read', status: 'cancelled', summary: '', detail: '' }] })[0].state).toBe('cancelled')
  })
})
