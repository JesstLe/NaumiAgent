import { test, expect } from '@playwright/test'
import { mockWorkbenchApi } from './mocks'

test.describe('Workbench smoke', () => {
  test.beforeEach(async ({ page }) => {
    await mockWorkbenchApi(page)
    await page.goto('/')
  })

  test('loads and shows the chat page by default', async ({ page }) => {
    // The app boots through ConnectionBootstrap; after the mocked bootstrap
    // resolves it should land on the chat page with the composer.
    const composer = page.getByPlaceholder('输入问题或指令...')
    await expect(composer).toBeVisible({ timeout: 15000 })
  })

  test('navigates to all MVP pages', async ({ page }) => {
    // Wait for the app to be ready.
    await expect(page.getByPlaceholder('输入问题或指令...')).toBeVisible({ timeout: 15000 })

    // Dashboard
    await page.getByRole('link', { name: '总览' }).click()
    await expect(page.getByText('活跃 Agent')).toBeVisible()

    // Task Market
    await page.getByRole('link', { name: '任务市场' }).click()
    await expect(page.getByText('实现登录页')).toBeVisible()

    // Worktrees
    await page.getByRole('link', { name: '工作区' }).click()
    await expect(page.getByText('wt-login').first()).toBeVisible({ timeout: 10000 })

    // Reviews
    await page.getByRole('link', { name: '审查' }).click()
    await expect(page.getByText('审查登录页实现').first()).toBeVisible({ timeout: 10000 })

    // Timeline
    await page.getByRole('link', { name: '时间线' }).click()
    // The event type appears in a font-mono span, distinct from the hidden
    // <option> in the filter dropdown.
    await expect(page.locator('span.font-mono', { hasText: 'session.started' })).toBeVisible({ timeout: 10000 })

    // Settings
    await page.getByRole('link', { name: '设置' }).click()
    await expect(page.getByText('API 令牌').first()).toBeVisible({ timeout: 10000 })
  })

  test('chat composer accepts input', async ({ page }) => {
    const composer = page.getByPlaceholder('输入问题或指令...')
    await expect(composer).toBeVisible({ timeout: 15000 })
    await composer.fill('测试消息')
    await expect(composer).toHaveValue('测试消息')
  })

  test('task market shows the create-issue button', async ({ page }) => {
    await expect(page.getByPlaceholder('输入问题或指令...')).toBeVisible({ timeout: 15000 })
    await page.getByRole('link', { name: '任务市场' }).click()
    await expect(page.getByRole('button', { name: '新建任务' }).first()).toBeVisible()
  })

  test('can switch language to English', async ({ page }) => {
    await expect(page.getByPlaceholder('输入问题或指令...')).toBeVisible({ timeout: 15000 })
    await page.getByRole('link', { name: '设置' }).click()
    await page.getByRole('button', { name: 'English' }).click()
    // After switching, the nav label should be English.
    await expect(page.getByRole('link', { name: 'Settings' })).toBeVisible()
  })
})
