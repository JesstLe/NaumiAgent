import { test, expect } from '@playwright/test'
import { mockWorkbenchApi } from './mocks'

test('tool output survives run completion and reload with multiline detail', async ({ page }) => {
  await mockWorkbenchApi(page)
  let finished = false
  const output = '第一行：读取成功\n第二行：共 3 条配置'
  await page.route('**/sessions/*/runs', route => route.fulfill({ json: { runs: finished ? [{ id: 'r', status: 'completed', started_at: '', steps: [
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
  await trace.getByRole('button', { name: '最近执行过程 · 已完成' }).click()
  await trace.getByRole('button', { name: '1. read 已完成' }).click()
  await expect(trace.getByText(output, { exact: true })).toBeVisible()
  await expect(trace.getByText(output, { exact: true })).toHaveCSS('white-space', 'pre-wrap')
  await page.reload()
  await trace.getByRole('button', { name: '最近执行过程 · 已完成' }).click()
  await trace.getByRole('button', { name: '1. read 已完成' }).click()
  await expect(trace.getByText(output, { exact: true })).toBeVisible()
  await expect(trace.getByRole('button', { name: '1 条工具记录' })).toBeVisible()
  await expect(trace.getByText('分析请求')).toHaveCount(0)
  await expect(trace.getByText('生成答复')).toHaveCount(0)
})
