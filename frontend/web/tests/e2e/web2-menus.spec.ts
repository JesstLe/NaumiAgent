import { expect, test } from '@playwright/test'
import { mockWorkbenchApi } from './mocks'

test('application menus support keyboard, draft editing and real navigation', async ({ page }) => {
  await mockWorkbenchApi(page)
  await page.goto('/web2')
  const menus = page.getByRole('navigation', { name: '应用菜单' })
  await menus.getByRole('button', { name: '文件', exact: true }).focus()
  await page.keyboard.press('ArrowDown')
  await expect(page.getByRole('menuitem', { name: '新建对话' })).toBeFocused()
  await page.keyboard.press('ArrowRight')
  await expect(page.getByRole('menuitem', { name: '编辑消息', exact: true })).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('menu')).toHaveCount(0)
  await page.getByRole('textbox', { name: '消息', exact: true }).fill('保留草稿')
  await menus.getByRole('button', { name: '编辑', exact: true }).click()
  await page.getByRole('menuitem', { name: '全选消息草稿' }).click()
  await page.keyboard.type('替换草稿')
  await expect(page.getByRole('textbox', { name: '消息', exact: true })).toHaveValue('替换草稿')
  await menus.getByRole('button', { name: '文件', exact: true }).click()
  await page.getByRole('menuitem', { name: '设置', exact: true }).click()
  await expect(page.getByLabel('API 地址')).toBeVisible()
})
