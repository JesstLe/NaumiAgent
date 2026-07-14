import { test, expect } from '@playwright/test'
import { mockWorkbenchApi } from './mocks'

test.describe('UX visual check', () => {
  test.beforeEach(async ({ page }) => {
    await mockWorkbenchApi(page)
  })

  test('capture full workbench UI', async ({ page }) => {
    await page.goto('/')
    // Wait for the connection bootstrap and the first render pass.
    await page.waitForSelector('text=冒烟测试会话', { timeout: 10000 })
    await page.waitForTimeout(500)

    const screenshot = await page.screenshot({ fullPage: true })
    // Save to a stable path so it can be reviewed outside the test results.
    const fs = await import('node:fs/promises')
    const path = await import('node:path')
    const dir = path.resolve('screenshots')
    await fs.mkdir(dir, { recursive: true })
    await fs.writeFile(path.join(dir, 'ux-check.png'), screenshot)
  })
})
