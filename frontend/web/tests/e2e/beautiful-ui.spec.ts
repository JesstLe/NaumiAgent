import { test, expect } from '@playwright/test'
import { mockWorkbenchApi } from './mocks'

test('execution trace restores real run steps and exposes output on demand', async ({ page }) => {
  await mockWorkbenchApi(page)
  await page.route('**/sessions/*/messages?*', route => route.fulfill({ json: { messages: [{ id: 'm', role: 'assistant', content: '已检查文件', metadata: {} }], total: 1 } }))
  await page.route('**/sessions/*/runs', route => route.fulfill({ json: { runs: [{ id: 'r', started_at: '2026-09-11T01:00:00Z', status: 'failed', steps: [{ sequence: 1, stage: 'tool', status: 'failed', summary: 'read missing.txt', detail: '文件不存在' }] }] } }))
  await page.goto('/web2')
  const trace = page.getByLabel('执行过程')
  await trace.locator('summary').first().click()
  await trace.getByText('read missing.txt', { exact: true }).click()
  await expect(trace.getByText('文件不存在', { exact: true })).toBeVisible()
  await page.reload()
  await expect(trace).toBeVisible()
  await expect(page.getByRole('navigation', { name: '主导航' }).getByText('执行过程')).toHaveCount(0)
})
