import { test, expect } from '@playwright/test'
import { mockWorkbenchApi } from './mocks'

test('selection actions preserve drafts, do not send automatically, and dismiss with Escape', async ({ page }) => {
  await mockWorkbenchApi(page)
  await page.route('**/sessions/*/messages?*', route => route.fulfill({ json: { messages: [{ id: 'm', role: 'assistant', content: '这是需要解释的真实回答片段。', metadata: {} }], total: 1 } }))
  let sends = 0
  await page.route('**/sessions/*/messages', route => { sends++; return route.fulfill({ status: 500 }) })
  await page.goto('/web2')
  const composer = page.getByRole('textbox', { name: '消息', exact: true })
  await composer.fill('已有草稿')
  await page.locator('.w2-message.assistant .w2-prose').evaluate(node => { const range = document.createRange(); range.selectNodeContents(node); window.getSelection()?.removeAllRanges(); window.getSelection()?.addRange(range) })
  const actions = page.getByRole('toolbar', { name: '选中文字操作' })
  await actions.getByRole('button', { name: '解释', exact: true }).click()
  await expect(composer).toHaveValue('已有草稿\n\n请解释以下内容：\n\n> 这是需要解释的真实回答片段。')
  expect(sends).toBe(0)
  await page.locator('.w2-message.assistant .w2-prose').evaluate(node => { (document.activeElement as HTMLElement)?.blur(); const range = document.createRange(); range.selectNodeContents(node); window.getSelection()?.removeAllRanges(); window.getSelection()?.addRange(range) })
  await expect(actions).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(actions).toHaveCount(0)
})

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
