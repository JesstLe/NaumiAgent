import { test, expect } from '@playwright/test'
import { mockWorkbenchApi } from './mocks'

test('concrete actions, task progress and compaction remain visible after reload', async ({ page }) => {
  await mockWorkbenchApi(page)
  let finished = false
  const steps = [
    { sequence: 1, stage: 'request', status: 'completed', summary: '修复归档并检查布局', detail: '' },
    { sequence: 2, stage: 'analysis', status: 'completed', summary: '第 1 轮分析', detail: 'PRIVATE_REASONING' },
    { sequence: 3, stage: 'tool', status: 'completed', summary: 'file_edit', detail: '已更新文件', metadata: { public_action: '修改文件：frontend/shared/src/api/WorkbenchApiClient.ts' } },
    { sequence: 4, stage: 'activity', status: 'completed', summary: '执行计划 · 进行中：检查 UI 元素布局；已完成 1 项', detail: '' },
    { sequence: 5, stage: 'tool', status: 'completed', summary: 'browser_observe', detail: '页面元素已读取', metadata: { public_action: '查看页面元素与布局' } },
    { sequence: 6, stage: 'activity', status: 'completed', summary: '已压缩上下文：120 → 30 条消息；归档 2 条工具结果', detail: '' },
  ]
  await page.route('**/sessions/*/runs', route => route.fulfill({ json: { runs: finished ? [{ id: 'public', status: 'completed', started_at: '2026-09-11T00:00:00Z', completed_at: '2026-09-11T00:00:28Z', steps }] : [] } }))
  await page.route('**/sessions/*/messages', async route => {
    if (route.request().method() !== 'POST') return route.fallback()
    finished = true
    const events = [
      { id: '1', type: 'turn_start', data: {} },
      { id: '2', type: 'thinking_delta', data: { content: 'PRIVATE_REASONING' } },
      { id: '3', type: 'tool_call_start', data: { call_id: 'a', name: 'file_edit', activity_summary: steps[2].metadata?.public_action } },
      { id: '4', type: 'tool_call_end', data: { call_id: 'a', name: 'file_edit', status: 'success' } },
      { id: '5', type: 'runtime_event', data: { event: 'task_snapshot', data: { activity_summary: steps[3].summary } } },
      { id: '6', type: 'context_compacted', data: { activity_summary: steps[5].summary } },
      { id: '7', type: 'agent_end', data: { status: 'completed' } },
    ]
    await route.fulfill({ contentType: 'text/event-stream', body: events.map(event => `data: ${JSON.stringify({ ...event, run_id: 'public' })}\n\n`).join('') })
  })
  await page.goto('/web2')
  await page.getByRole('textbox', { name: '消息', exact: true }).fill('修复归档并检查布局')
  await page.getByRole('button', { name: '发送消息' }).click()
  const trace = page.getByLabel('执行过程', { exact: true })
  for (const reload of [false, true]) {
    if (reload) await page.reload()
    await expect(trace).toContainText('修改文件：frontend/shared/src/api/WorkbenchApiClient.ts')
    await expect(trace).toContainText('查看页面元素与布局')
    await expect(trace).toContainText('进行中：检查 UI 元素布局')
    await expect(trace).toContainText('120 → 30 条消息')
    await expect(trace).not.toContainText('PRIVATE_REASONING')
  }
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(trace.locator('.bui-action-summary').first()).toHaveCSS('white-space', 'normal')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

test('real file tool summary is rendered from the running backend', async ({ page }) => {
  const session = process.env.NAUMI_ACTIVITY_LIVE_SESSION
  test.skip(!session, '需先运行真实文件工具验收，再提供对应会话 ID')
  await page.addInitScript(id => {
    localStorage.setItem('naumi:workspace:session', id!)
  }, session)
  await page.goto('http://127.0.0.1:5174/web2')
  const trace = page.getByLabel('执行过程', { exact: true })
  await expect(trace).toContainText('读取文件：docs/web2-public-activity.md')
  await expect(trace.getByRole('button', { name: 'file_read 已完成' })).toBeVisible()
  await page.reload()
  await expect(trace).toContainText('读取文件：docs/web2-public-activity.md')
  await page.screenshot({ path: '../../.naumi/data/public-activity-real.png', fullPage: true })
})
