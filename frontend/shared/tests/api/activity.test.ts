import { describe, expect, it } from 'vitest'
import { isTimelineEvent, liveExecutionTimeline, runActivity, runExecutionTimeline, runsByUserMessage, toolActivity } from '../../src/api/activity'

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
  it('shows concrete task, command, result status, and compaction without raw thinking', () => {
    const events = [
      { id: '1', type: 'turn_start', turn: 1, data: {} },
      { id: '2', type: 'thinking_delta', turn: 1, data: { content: 'PRIVATE_REASONING' } },
      { id: '3', type: 'tool_call_start', turn: 1, data: { call_id: 'a', name: 'bash_run', activity_summary: '在 E:/Workspace 执行命令：pnpm build' } },
      { id: '4', type: 'tool_call_error', turn: 1, data: { call_id: 'a', name: 'bash_run', message: '构建失败' } },
      { id: '5', type: 'turn_start', turn: 2, data: {} },
      { id: '6', type: 'context_compacted', data: { activity_summary: '已压缩上下文：100 → 25 条消息' } },
      { id: '7', type: 'runtime_event', data: { event: 'task_snapshot', data: { activity_summary: '执行计划 · 进行中：修复构建问题' } } },
      { id: '8', type: 'agent_end', data: { status: 'cancelled' } },
    ]
    const rows = liveExecutionTimeline(events, false, { objective: '修复归档接口', workspace: 'E:/Workspace' })
    expect(rows[0].label).toContain('本次任务：修复归档接口')
    expect(rows[0].label).toContain('工作目录：E:/Workspace')
    expect(rows[1]).toMatchObject({ action: '在 E:/Workspace 执行命令：pnpm build', state: 'failed' })
    expect(rows[2]).toMatchObject({ label: '第 2 轮 · 上一步执行失败：在 E:/Workspace 执行命令：pnpm build', state: 'cancelled' })
    expect(rows[3].label).toContain('100 → 25')
    expect(rows[4].label).toContain('修复构建问题')
    expect(JSON.stringify(rows)).not.toContain('PRIVATE_REASONING')
    expect(isTimelineEvent(events[1])).toBe(false)
    expect(isTimelineEvent(events[5])).toBe(true)
    expect(isTimelineEvent(events[6])).toBe(true)
  })
  it('restores public summaries and never uses saved internal analysis detail', () => {
    const rows = runExecutionTimeline({ id: 'r', status: 'failed', started_at: '', steps: [
      { sequence: 1, stage: 'request', status: 'completed', summary: '检查布局', detail: '' },
      { sequence: 2, stage: 'analysis', status: 'completed', summary: '分析请求', detail: 'PRIVATE_REASONING' },
      { sequence: 3, stage: 'tool', status: 'completed', summary: 'browser_observe', detail: 'button', metadata: { public_action: '查看页面元素与布局' } },
      { sequence: 4, stage: 'activity', status: 'completed', summary: '已压缩上下文：30 → 12 条消息', detail: '' },
      { sequence: 5, stage: 'analysis', status: 'running', summary: '第 2 轮分析', detail: '' },
    ] })
    expect(rows[1]).toMatchObject({ action: '查看页面元素与布局' })
    expect(rows[2].label).toContain('30 → 12')
    expect(rows[3]).toMatchObject({ state: 'unknown' })
    expect(JSON.stringify(rows)).not.toContain('PRIVATE_REASONING')
  })
  it('deduplicates delivery and keeps concurrent calls separate', () => {
    const start = { id: '1', type: 'tool_call_start', data: { name: 'read', call_id: 'a', arguments: { path: 'a.py' } } }
    const rows = liveExecutionTimeline([start, start, { ...start, id: '2', data: { ...start.data, call_id: 'b', arguments: { path: 'b.py' } } }], false)
    expect(rows).toHaveLength(2)
    expect(rows[0]).toMatchObject({ action: '读取文件：a.py', state: 'unknown' })
    expect(rows[1]).toMatchObject({ action: '读取文件：b.py', state: 'unknown' })
  })
  it('describes progress facts from older daemons without claiming unrecorded work', () => {
    const rows = liveExecutionTimeline([
      { id: '1', type: 'context_compacted', data: { before: 80, after: 20, archived_tool_results: 1 } },
      { id: '2', type: 'runtime_event', data: { event: 'task_snapshot', data: { items: [{ status: 'in_progress', subject: '核对布局' }], completed_count: 0 } } },
    ], false)
    expect(rows.map(row => row.label)).toEqual(['已压缩上下文：80 → 20 条消息；归档 1 条工具结果', '执行计划 · 进行中：核对布局；已完成 0 项'])
  })
  it('binds every durable run to its own user turn, including legacy random ids', () => {
    const messages = [
      { id: 'u1', role: 'user', content: '读取 README', timestamp: '', metadata: {} },
      { id: 'a1', role: 'assistant', content: '已读取', timestamp: '', metadata: {} },
      { id: 'u2', role: 'user', content: '检查配置', timestamp: '', metadata: {} },
      { id: 'a2', role: 'assistant', content: '已检查', timestamp: '', metadata: {} },
    ]
    const runs = [
      { id: 'r2', user_message_id: 'legacy-random-2', status: 'completed', started_at: '2026-09-11T00:02:00Z', steps: [
        { sequence: 1, stage: 'request', status: 'completed', summary: '检查配置', detail: '' },
      ] },
      { id: 'r1', user_message_id: 'legacy-random-1', status: 'completed', started_at: '2026-09-11T00:01:00Z', steps: [
        { sequence: 1, stage: 'request', status: 'completed', summary: '读取 README', detail: '' },
      ] },
    ]
    const assigned = runsByUserMessage(messages, runs)
    expect(assigned.get('u1')?.id).toBe('r1')
    expect(assigned.get('u2')?.id).toBe('r2')
  })
  it('uses the newest matching run after regenerating an earlier answer', () => {
    const messages = [
      { id: 'u1', role: 'user', content: '读取 README', timestamp: '', metadata: {} },
      { id: 'a1', role: 'assistant', content: '第一版', timestamp: '', metadata: {} },
      { id: 'u2', role: 'user', content: '检查配置', timestamp: '', metadata: {} },
      { id: 'a2', role: 'assistant', content: '配置正常', timestamp: '', metadata: {} },
    ]
    const run = (id: string, started_at: string, summary: string) => ({
      id,
      user_message_id: `legacy-${id}`,
      status: 'completed',
      started_at,
      steps: [{ sequence: 1, stage: 'request', status: 'completed', summary, detail: '' }],
    })
    const assigned = runsByUserMessage(messages, [
      run('regenerated', '2026-09-11T00:03:00Z', '读取 README'),
      run('second', '2026-09-11T00:02:00Z', '检查配置'),
      run('first', '2026-09-11T00:01:00Z', '读取 README'),
    ])
    expect(assigned.get('u1')?.id).toBe('regenerated')
    expect(assigned.get('u2')?.id).toBe('second')
  })
})
