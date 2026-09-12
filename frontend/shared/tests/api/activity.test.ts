import { describe, expect, it } from 'vitest'
import { assistantActionsReady, executionStages, isTimelineEvent, liveExecutionActivity, liveExecutionTimeline, precedingUserMessages, runActivity, runExecutionTimeline, runsByUserMessage, toolActivity } from '../../src/api/activity'

describe('public execution activity', () => {
  it('pairs assistant messages with the nearest preceding user in one pass', () => {
    const message = (id: string, role: string) => ({
      id,
      role,
      content: id,
      timestamp: '',
      metadata: {},
    })
    const messages = [
      message('system', 'system'),
      message('u1', 'user'),
      message('a1', 'assistant'),
      message('tool', 'tool'),
      message('a2', 'assistant'),
      message('u2', 'user'),
      message('a3', 'assistant'),
    ]

    expect(precedingUserMessages(messages).map(item => item?.id)).toEqual([
      undefined,
      undefined,
      'u1',
      undefined,
      'u1',
      undefined,
      'u2',
    ])
  })
  it('shows assistant actions only after the owning run reaches a terminal state', () => {
    const base = {
      content: '已经收到部分正文',
      assistantPending: false,
      userPending: false,
      userMessageId: 'u1',
      runningUserMessageId: null,
    }
    expect(assistantActionsReady({ ...base, runStatus: 'running' })).toBe(false)
    expect(assistantActionsReady({ ...base, runningUserMessageId: 'u1', runStatus: 'completed' })).toBe(false)
    expect(assistantActionsReady({ ...base, userPending: true, runStatus: 'completed' })).toBe(false)
    expect(assistantActionsReady({ ...base, runStatus: 'completed' })).toBe(true)
    expect(assistantActionsReady({ ...base, runStatus: 'failed' })).toBe(true)
    expect(assistantActionsReady({ ...base, runStatus: undefined })).toBe(true)
  })
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
    expect(executionStages(rows).map(stage => ({ turn: stage.turn, tools: stage.tools.length }))).toEqual([
      { turn: 1, tools: 1 },
      { turn: 2, tools: 0 },
    ])
  })
  it('groups phase summaries with their real turn and keeps tool state', () => {
    const timeline = liveExecutionTimeline([
      { id: '1', type: 'turn_start', turn: 1, sequence: 1, data: {} },
      { id: '2', type: 'tool_call_start', turn: 1, sequence: 2, data: { call_id: 'a', name: 'read_file', activity_summary: '读取文件：README.md' } },
      { id: '3', type: 'tool_call_end', turn: 1, sequence: 3, data: { call_id: 'a', name: 'read_file', status: 'success' } },
      { id: '4', type: 'phase_summary', turn: 1, sequence: 4, data: { activity_summary: '本阶段已完成：读取文件：README.md。' } },
      { id: '5', type: 'turn_start', turn: 2, sequence: 5, data: {} },
    ], true)
    const stages = executionStages(timeline)
    expect(stages).toHaveLength(2)
    expect(stages[0].tools[0]).toMatchObject({ turn: 1, state: 'completed' })
    expect(stages[0].notes.map(note => note.label)).toContain('本阶段已完成：读取文件：README.md。')
    expect(isTimelineEvent({ id: '4', type: 'phase_summary', data: {} })).toBe(true)
  })
  it('merges adjacent recovery-only turns and states the task only once', () => {
    const timeline = liveExecutionTimeline([
      { id: '1', type: 'turn_start', turn: 1, sequence: 1, data: {} },
      { id: '2', type: 'context_compacted', turn: 1, sequence: 2, data: { activity_summary: '已压缩上下文：41 → 4 条消息' } },
      { id: '3', type: 'phase_summary', turn: 1, sequence: 3, data: { activity_summary: '本阶段已完成：已要求继续调用写入工具。' } },
      { id: '4', type: 'turn_start', turn: 2, sequence: 4, data: {} },
      { id: '5', type: 'phase_summary', turn: 2, sequence: 5, data: { activity_summary: '本阶段已完成：已自动压缩上下文并重新执行。' } },
      { id: '6', type: 'turn_start', turn: 3, sequence: 6, data: {} },
      { id: '7', type: 'thinking_start', turn: 3, sequence: 7, data: {} },
    ], true, { objective: '创建高级页面', workspace: 'E:/Workspace/NaumiAgent' })

    const stages = executionStages(timeline)
    expect(stages).toHaveLength(1)
    expect(stages[0].state).toBe('running')
    expect(stages[0].notes.map(note => note.label)).toEqual([
      '本次任务：创建高级页面\n工作目录：E:/Workspace/NaumiAgent',
      '已压缩上下文：41 → 4 条消息',
      '本阶段已完成：已要求继续调用写入工具。',
      '第 2 轮 · 继续处理本次任务',
      '本阶段已完成：已自动压缩上下文并重新执行。',
      '第 3 轮 · 继续处理本次任务',
    ])
    expect(stages[0].notes.map(note => note.label).join('\n').match(/本次任务：/g)).toHaveLength(1)
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
  it('shows the persisted failure reason for an empty historical response', () => {
    const rows = runExecutionTimeline({ id: 'r', status: 'failed', started_at: '', steps: [
      { sequence: 1, stage: 'request', status: 'completed', summary: '生成页面', detail: '' },
      { sequence: 2, stage: 'analysis', status: 'completed', summary: '第 1 轮分析', detail: '' },
      { sequence: 3, stage: 'response', status: 'failed', summary: '生成答复', detail: '任务结束但未返回可显示结果，请重试' },
    ] })

    expect(rows).toHaveLength(2)
    expect(rows[1]).toMatchObject({
      kind: 'reasoning',
      state: 'failed',
      label: '任务结束但未返回可显示结果，请重试',
    })
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
  it('uses the active plan as the next public reasoning summary', () => {
    const rows = liveExecutionTimeline([
      { id: '1', type: 'tool_call_start', turn: 1, data: { call_id: 'plan', name: 'todo_write', activity_summary: '更新执行计划' } },
      { id: '2', type: 'tool_call_end', turn: 1, data: { call_id: 'plan', name: 'todo_write', status: 'success' } },
      { id: '3', type: 'runtime_event', turn: 1, data: { event: 'task_snapshot', data: { items: [
        { status: 'pending', subject: '稍后运行浏览器验收' },
        { status: 'in_progress', subject: '编写 Three.js 界面代码' },
      ] } } },
      { id: '4', type: 'turn_start', turn: 2, data: {} },
    ], true)

    expect(rows.at(-1)).toMatchObject({
      kind: 'reasoning',
      label: '正在推进：编写 Three.js 界面代码',
      state: 'running',
    })
    expect(JSON.stringify(rows)).not.toContain('调用工具 todo_write')
  })
  it('reports observable freshness and distinguishes quiet from stalled work', () => {
    const started = '2026-09-12T00:00:00.000Z'
    const events = [
      { id: '1', type: 'turn_start', timestamp: started, data: {} },
      { id: '2', type: 'runtime_event', timestamp: '2026-09-12T00:00:10.000Z', data: { event: 'task_snapshot', data: {
        items: [{ status: 'in_progress', active_form: '正在检查页面元素与布局', subject: '检查页面' }],
      } } },
    ]
    const rows = liveExecutionTimeline(events, true)
    const quiet = liveExecutionActivity(events, rows, true, {}, Date.parse('2026-09-12T00:01:00.000Z'))
    const stalled = liveExecutionActivity(events, rows, true, {}, Date.parse('2026-09-12T00:02:20.000Z'))

    expect(quiet).toMatchObject({
      headline: '正在推进：检查页面元素与布局',
      freshness: 'quiet',
      idleMilliseconds: 50_000,
    })
    expect(quiet.detail).toContain('50 秒没有新的工具或公开输出')
    expect(stalled.freshness).toBe('stalled')
    expect(stalled.detail).toContain('任务可能停滞')
  })
  it('describes the workspace while the first executable step is being prepared', () => {
    const events = [{ id: '1', type: 'turn_start', timestamp: '2026-09-12T00:00:00.000Z', data: {} }]
    const rows = liveExecutionTimeline(events, true, { objective: '创建复杂页面', workspace: 'E:/Workspace/NaumiAgent' })
    const activity = liveExecutionActivity(
      events,
      rows,
      true,
      { objective: '创建复杂页面', workspace: 'E:/Workspace/NaumiAgent' },
      Date.parse('2026-09-12T00:00:01.000Z'),
    )
    expect(activity.headline).toBe('正在分析任务，准备在 E:/Workspace/NaumiAgent 执行')
    expect(activity.summary).toBe('正在围绕“创建复杂页面”梳理上下文，尚未开始工具操作。')
  })
  it('updates the short reasoning summary from observable execution facts', () => {
    const started = '2026-09-12T00:00:00.000Z'
    const initialEvents = [{ id: '1', type: 'turn_start', timestamp: started, data: {} }]
    const initialRows = liveExecutionTimeline(initialEvents, true)
    expect(liveExecutionActivity(initialEvents, initialRows, true, {}, Date.parse('2026-09-12T00:00:01.000Z')).summary)
      .toBe('正在梳理当前请求和上下文，尚未开始工具操作。')

    const runningEvents = [...initialEvents, {
      id: '2', type: 'tool_call_start', timestamp: '2026-09-12T00:00:02.000Z', data: {
        call_id: 'read', name: 'read_file', activity_summary: '读取文件：README.md',
      },
    }]
    const runningRows = liveExecutionTimeline(runningEvents, true)
    expect(liveExecutionActivity(runningEvents, runningRows, true, {}, Date.parse('2026-09-12T00:00:03.000Z')).summary)
      .toBe('已进入执行阶段，当前操作完成后会根据实际结果继续。')

    const completedEvents = [...runningEvents, {
      id: '3', type: 'tool_call_end', timestamp: '2026-09-12T00:00:04.000Z', data: {
        call_id: 'read', name: 'read_file', status: 'success', activity_summary: '读取文件：README.md',
      },
    }]
    const completedRows = liveExecutionTimeline(completedEvents, true)
    expect(liveExecutionActivity(completedEvents, completedRows, true, {}, Date.parse('2026-09-12T00:00:05.000Z')).summary)
      .toBe('已记录 1 项完成，正在结合结果准备下一步。')
    expect(JSON.stringify(completedRows)).not.toContain('PRIVATE_REASONING')

    const failedEvents = [...runningEvents, {
      id: '4', type: 'tool_call_error', timestamp: '2026-09-12T00:00:04.000Z', data: {
        call_id: 'read', name: 'read_file', message: '文件不存在', activity_summary: '读取文件：README.md',
      },
    }]
    const failedRows = liveExecutionTimeline(failedEvents, true)
    expect(liveExecutionActivity(failedEvents, failedRows, true, {}, Date.parse('2026-09-12T00:00:05.000Z')).summary)
      .toBe('已记录 1 项失败，正在结合结果准备下一步。')
  })
  it('prefers a running tool, resets freshness on new public progress, and stops at terminal state', () => {
    const events = [
      { id: '1', type: 'turn_start', timestamp: '2026-09-12T00:00:00.000Z', data: {} },
      { id: '2', type: 'tool_call_start', timestamp: '2026-09-12T00:02:05.000Z', data: {
        call_id: 'browser', name: 'browser_observe', activity_summary: '查看页面元素与布局',
      } },
    ]
    const rows = liveExecutionTimeline(events, true)
    const active = liveExecutionActivity(events, rows, true, {}, Date.parse('2026-09-12T00:02:10.000Z'))
    expect(active).toMatchObject({
      headline: '正在查看页面元素与布局',
      freshness: 'current',
      idleMilliseconds: 5_000,
    })

    const terminalEvents = [...events, {
      id: '3', type: 'agent_end', timestamp: '2026-09-12T00:02:11.000Z', data: { status: 'completed' },
    }]
    expect(liveExecutionActivity(terminalEvents, rows, false, {}, Date.parse('2026-09-12T00:05:00.000Z'))).toMatchObject({
      headline: '', detail: '', freshness: 'terminal',
    })
  })
  it('clears a stale active plan when the latest snapshot has no in-progress item', () => {
    const events = [
      { id: '1', type: 'runtime_event', timestamp: '2026-09-12T00:00:00.000Z', data: { event: 'task_snapshot', data: {
        items: [{ status: 'in_progress', subject: '读取文件' }],
      } } },
      { id: '2', type: 'tool_call_start', timestamp: '2026-09-12T00:00:01.000Z', data: { call_id: 'read', name: 'read', activity_summary: '读取文件：README.md' } },
      { id: '3', type: 'tool_call_end', timestamp: '2026-09-12T00:00:02.000Z', data: { call_id: 'read', name: 'read', status: 'success' } },
      { id: '4', type: 'runtime_event', timestamp: '2026-09-12T00:00:03.000Z', data: { event: 'task_snapshot', data: {
        items: [], completed_count: 1,
      } } },
      { id: '5', type: 'turn_start', turn: 2, timestamp: '2026-09-12T00:00:04.000Z', data: {} },
    ]
    const rows = liveExecutionTimeline(events, true)
    const activity = liveExecutionActivity(events, rows, true, {}, Date.parse('2026-09-12T00:00:05.000Z'))

    expect(rows.at(-1)?.label).toContain('上一步已完成：读取文件：README.md')
    expect(activity.headline).toBe('执行计划已完成，正在整理最终结果')
    expect(activity.headline).not.toContain('正在推进：读取文件')
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
  it('does not attach an edited-away run to an unrelated remaining user', () => {
    const messages = [
      { id: 'u1', role: 'user', content: '保留的问题', timestamp: '', metadata: {} },
      { id: 'a1', role: 'assistant', content: '保留的回答', timestamp: '', metadata: {} },
    ]
    const assigned = runsByUserMessage(messages, [{
      id: 'removed-run',
      user_message_id: 'legacy-removed',
      status: 'completed',
      started_at: '2026-09-11T00:03:00Z',
      steps: [{ sequence: 1, stage: 'request', status: 'completed', summary: '已经编辑删除的问题', detail: '' }],
    }])
    expect(assigned.size).toBe(0)
  })
})
