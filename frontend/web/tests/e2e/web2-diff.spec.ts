import { test, expect } from '@playwright/test'
import { mockWorkbenchApi } from './mocks'

test('diff filters, line numbers, split view and refreshing patches', async ({ page }) => {
  await mockWorkbenchApi(page)
  let changed = false
  await page.route('**/sessions/*/git-diff', route => route.fulfill({ json: { available: true, branch: 'main', files: [
    { path: 'readme.md', stage: 'staged', additions: 1, deletions: 1, patch: `@@ -10 +10 @@\n-old\n+${changed ? 'latest' : 'new'}\n` },
    { path: 'image.png', stage: 'untracked', additions: 0, deletions: 0, patch: '' },
  ] } }))
  await page.goto('/web2')
  await page.keyboard.press('Control+Shift+G')
  await expect(page.getByText('readme.md', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '全部展开' }).click()
  await expect(page.getByLabel('readme.md 差异')).toContainText('10')
  await page.getByRole('button', { name: '并排', exact: true }).click()
  await expect(page.locator('.w2-split-row')).toHaveCount(1)
  await page.getByLabel('更改范围').selectOption('untracked')
  await expect(page.getByText('无文本补丁（二进制文件或仅元数据变化）')).toBeVisible()
  await page.getByLabel('更改范围').selectOption('all')
  await page.getByLabel('筛选更改文件').fill('readme')
  await expect(page.getByText('image.png', { exact: true })).toHaveCount(0)
  changed = true
  await page.getByRole('button', { name: '刷新代码更改' }).click()
  await expect(page.getByLabel('readme.md 差异')).toContainText('latest')
})
