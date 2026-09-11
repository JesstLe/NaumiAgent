import { expect, test } from '@playwright/test'
import { mockWorkbenchApi } from './mocks'

test('project plus opens the Codex-style project creation flow', async ({ page }) => {
  await mockWorkbenchApi(page)
  await page.goto('/web2')

  await page.getByRole('button', { name: '创建项目', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '创建项目' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByText('项目类型')).toBeVisible()
  await expect(dialog.getByRole('radio', { name: /本地/ })).toHaveAttribute('aria-checked', 'true')
  await expect(dialog.getByRole('radio', { name: /远程/ })).toBeDisabled()
  await expect(dialog.getByText('本地项目创建需要 NaumiAgent 桌面版。')).toBeVisible()
  await expect(dialog.getByRole('button', { name: '下一步' })).toBeDisabled()
  await dialog.getByRole('button', { name: '关闭创建项目' }).click()
  await expect(dialog).toBeHidden()
})
