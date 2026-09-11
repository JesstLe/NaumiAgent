import { test, expect } from '@playwright/test'
import { mockWorkbenchApi } from './mocks'

test('task and context features stay in the right panel and panel controls retain their order', async ({ page }) => {
  await mockWorkbenchApi(page)
  await page.goto('/web2')
  const navigation = page.getByRole('navigation', { name: '主导航' })
  await expect(navigation.getByRole('button')).toHaveText(['新对话＋', '工具与扩展', '定时任务', '插件'])
  await expect(page.getByRole('button', { name: '删除当前会话' })).toHaveCount(0)
  for (let index = 0; index < 4; index++) {
    const controls = page.getByRole('group', { name: '面板显示控制' }).filter({ visible: true })
    await expect(controls).toHaveCount(1)
    await expect(controls.getByRole('button').nth(0)).toHaveAttribute('aria-label', '切换右侧面板')
    await expect(controls.getByRole('button').nth(1)).toHaveAttribute('aria-label', '切换执行面板')
    await controls.getByRole('button', { name: '切换执行面板' }).click()
    await expect(page.getByLabel('终端执行记录')).toBeVisible({ visible: index % 2 === 1 })
    await controls.getByRole('button', { name: '切换右侧面板' }).click()
    await expect(page.getByLabel('工作区面板')).toBeVisible({ visible: index % 2 === 1 })
  }
  await expect(page.getByRole('region', { name: '对话摘要' })).toHaveCount(0)
  await page.getByRole('button', { name: '打开待办面板' }).click()
  const workspacePanel = page.getByLabel('工作区面板')
  await expect(workspacePanel.getByLabel('新增待办')).toBeVisible()
  await workspacePanel.getByRole('button', { name: '目标', exact: true }).click()
  await expect(workspacePanel.getByText('持久目标')).toBeVisible()
  await page.getByRole('button', { name: '切换右侧面板' }).filter({ visible: true }).click()
  await page.reload()
  await expect(page.getByLabel('工作区面板')).toBeHidden()
  await page.getByRole('button', { name: '切换右侧面板' }).filter({ visible: true }).click()
  await expect(page.getByLabel('工作区面板')).toBeVisible()
})
