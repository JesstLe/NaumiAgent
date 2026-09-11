import { test, expect } from '@playwright/test'
import { mockWorkbenchApi } from './mocks'

test('tool output survives run completion and reload with multiline detail', async ({ page }) => {
  await mockWorkbenchApi(page)
  let finished = false
  const output = '第一行：读取成功\n第二行：共 3 条配置'
  await page.route('**/sessions/*/runs*', route => route.fulfill({ json: { runs: finished ? [{ id: 'r', status: 'completed', started_at: '2026-09-11T04:00:00Z', completed_at: '2026-09-11T04:00:02Z', steps: [
    { sequence: 1, stage: 'request', status: 'completed', summary: '继续', detail: '' },
    { sequence: 2, stage: 'analysis', status: 'completed', summary: '分析请求', detail: '' },
    { sequence: 3, stage: 'tool', status: 'completed', summary: 'read', detail: output, metadata: { tool_call_id: 'a', output_recorded: true } },
    { sequence: 4, stage: 'response', status: 'completed', summary: '生成答复', detail: '' },
  ] }] : [] } }))
  await page.route('**/sessions/*/messages', async route => {
    finished = true
    const events = [
      { id: '1', type: 'tool_call_start', run_id: 'r', data: { call_id: 'a', name: 'read' } },
      { id: '2', type: 'tool_call_end', run_id: 'r', data: { call_id: 'a', name: 'read', status: 'success', content: output } },
      { id: '3', type: 'token_delta', run_id: 'r', data: { token: '检查完成' } },
      { id: '4', type: 'agent_end', run_id: 'r', data: { status: 'completed' } },
    ]
    return route.fulfill({ contentType: 'text/event-stream', body: events.map(event => `data: ${JSON.stringify(event)}\n\n`).join('') })
  })
  await page.goto('/web2')
  await page.getByRole('textbox', { name: '消息', exact: true }).fill('继续')
  await page.getByRole('button', { name: '发送消息' }).click()
  const trace = page.getByLabel('执行过程', { exact: true })
  await expect(trace.getByRole('button', { name: /用时 .*已完成/ })).toBeVisible()
  await trace.getByRole('button', { name: 'read 已完成' }).click()
  await expect(trace.getByText(output, { exact: true })).toBeVisible()
  await expect(trace.getByText(output, { exact: true })).toHaveCSS('white-space', 'pre-wrap')
  await page.reload()
  await expect(trace.getByRole('button', { name: /用时 .*已完成/ })).toBeVisible()
  await trace.getByRole('button', { name: 'read 已完成' }).click()
  await expect(trace.getByText(output, { exact: true })).toBeVisible()
  await expect(trace.getByText('分析请求')).toHaveCount(0)
  await expect(trace.getByText('生成答复')).toHaveCount(0)
})

test('reasoning appears immediately and tools stay at their turn position', async ({ page }) => {
  await mockWorkbenchApi(page)
  let release: (() => void) | undefined
  const gate = new Promise<void>(resolve => { release = resolve })
  await page.route('**/sessions/*/messages', async route => {
    await gate
    const events = [
      { id: '1', type: 'turn_start', run_id: 'live', turn: 1, sequence: 1, data: {} },
      { id: '2', type: 'thinking_start', run_id: 'live', turn: 1, sequence: 2, data: {} },
      { id: '3', type: 'thinking_end', run_id: 'live', turn: 1, sequence: 3, data: {} },
      { id: '4', type: 'tool_call_start', run_id: 'live', turn: 1, sequence: 4, data: { call_id: 'a', name: 'read_file' } },
      { id: '5', type: 'tool_call_end', run_id: 'live', turn: 1, sequence: 5, data: { call_id: 'a', name: 'read_file', status: 'success', content: '读取完成' } },
      { id: '6', type: 'turn_start', run_id: 'live', turn: 2, sequence: 6, data: {} },
      { id: '7', type: 'tool_call_start', run_id: 'live', turn: 2, sequence: 7, data: { call_id: 'b', name: 'write_file' } },
      { id: '8', type: 'tool_call_end', run_id: 'live', turn: 2, sequence: 8, data: { call_id: 'b', name: 'write_file', status: 'success', content: '写入完成' } },
      { id: '9', type: 'token_delta', run_id: 'live', turn: 2, sequence: 9, data: { token: '处理完成' } },
      { id: '10', type: 'agent_end', run_id: 'live', turn: 2, sequence: 10, data: { status: 'completed' } },
    ]
    await route.fulfill({ contentType: 'text/event-stream', body: events.map(event => `data: ${JSON.stringify(event)}\n\n`).join('') })
  })
  await page.goto('/web2')
  await page.getByRole('textbox', { name: '消息', exact: true }).fill('检查后修改')
  await page.getByRole('button', { name: '发送消息' }).click()
  const trace = page.getByLabel('执行过程', { exact: true })
  await expect(trace.getByRole('button', { name: '正在推理' })).toBeVisible()
  await expect(trace.locator('.bui-action-summary', { hasText: '检查后修改' })).toBeVisible()
  release?.()
  await expect(page.getByText('处理完成', { exact: true })).toBeVisible()
  const labels = await trace.locator('.bui-chip-rows > div > button').allTextContents()
  expect(labels).toEqual([
    expect.stringContaining('检查后修改'),
    expect.stringContaining('读取文件'),
    expect.stringContaining('第 2 轮 · 上一步已完成：读取文件'),
    expect.stringContaining('修改文件'),
  ])
  const order = await page.locator('.w2-message-list > *').evaluateAll(nodes => nodes.map(node => node.className))
  expect(order.findIndex(value => String(value).includes('bui-execution'))).toBeLessThan(order.findLastIndex(value => String(value).includes('assistant')))
})

test('saved execution timelines remain attached to every historical turn', async ({ page }) => {
  await mockWorkbenchApi(page)
  const history = {
    messages: [
      { id: 'u1', role: 'user', content: '读取 README', timestamp: '2026-09-11T01:00:00Z', metadata: {} },
      { id: 'a1', role: 'assistant', content: '第一轮完成', timestamp: '2026-09-11T01:00:02Z', metadata: {} },
      { id: 'u2', role: 'user', content: '检查配置', timestamp: '2026-09-11T01:01:00Z', metadata: {} },
      { id: 'a2', role: 'assistant', content: '第二轮完成', timestamp: '2026-09-11T01:01:02Z', metadata: {} },
    ],
    total: 4,
  }
  const runs = [
    { id: 'r2', user_message_id: 'u2', status: 'completed', started_at: '2026-09-11T01:01:00Z', completed_at: '2026-09-11T01:01:02Z', steps: [
      { sequence: 1, stage: 'request', status: 'completed', summary: '检查配置', detail: '' },
      { sequence: 2, stage: 'analysis', status: 'completed', summary: '分析请求', detail: '' },
      { sequence: 3, stage: 'tool', status: 'completed', summary: 'read_file', detail: '配置正常', metadata: { public_action: '读取文件：config.yaml' } },
    ] },
    { id: 'r1', user_message_id: 'legacy-random-id', status: 'completed', started_at: '2026-09-11T01:00:00Z', completed_at: '2026-09-11T01:00:02Z', steps: [
      { sequence: 1, stage: 'request', status: 'completed', summary: '读取 README', detail: '' },
      { sequence: 2, stage: 'analysis', status: 'completed', summary: '分析请求', detail: '' },
      { sequence: 3, stage: 'tool', status: 'completed', summary: 'read_file', detail: '读取成功', metadata: { public_action: '读取文件：README.md' } },
    ] },
  ]
  await page.route('**/sessions/*/messages*', route => route.fulfill({ json: history }))
  await page.route('**/sessions/*/runs*', route => route.fulfill({ json: { runs, total: 2 } }))
  await page.goto('/web2')

  const traces = page.getByLabel('执行过程', { exact: true })
  await expect(traces).toHaveCount(2)
  await expect(traces.nth(0)).toHaveAttribute('data-run-id', 'r1')
  await expect(traces.nth(1)).toHaveAttribute('data-run-id', 'r2')
  await traces.nth(0).scrollIntoViewIfNeeded()
  await expect(traces.nth(0).locator('.bui-action-summary', { hasText: 'README.md' })).toBeVisible()
  await traces.nth(0).getByRole('button', { name: 'read_file 已完成' }).click()
  await expect(traces.nth(0).getByText('读取文件：README.md', { exact: true })).toBeVisible()

  const order = await page.locator('.w2-message-list > *').evaluateAll(nodes => nodes.map(node => node.getAttribute('data-run-id') || node.className))
  expect(order).toEqual(['w2-message user', 'r1', 'w2-message assistant', 'w2-message user', 'r2', 'w2-message assistant'])

  await page.reload()
  await expect(page.getByLabel('执行过程', { exact: true })).toHaveCount(2)
})
