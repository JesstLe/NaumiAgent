import { test, expect } from '@playwright/test'
import { mockWorkbenchApi } from './mocks'

test.describe('UX visual check', () => {
  test.beforeEach(async ({ page }) => {
    await mockWorkbenchApi(page)
  })

  test('capture full workbench UI', async ({ page }, testInfo) => {
    await page.goto('/')
    // Wait for the connection bootstrap and the first render pass.
    await page.waitForSelector('text=冒烟测试会话', { timeout: 10000 })
    await expect(page.getByPlaceholder('输入问题或指令...')).toBeVisible()
    await page.screenshot({ fullPage: true, path: testInfo.outputPath('ux-check.png') })
  })
})
