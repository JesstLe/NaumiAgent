import { test, expect } from '@playwright/test'
import { mockWorkbenchApi } from './mocks'

test.beforeEach(async ({ page }) => {
  await mockWorkbenchApi(page)
})

test('both shells decode the approved logo and publish valid favicon assets', async ({ page }) => {
  await page.goto('/web2')
  const sidebar = page.locator('.w2-sidebar-logo')
  await expect(sidebar).toBeVisible()
  await expect.poll(() => sidebar.evaluate((image: HTMLImageElement) => image.naturalWidth)).toBeGreaterThan(0)
  await page.getByRole('button', { name: '新对话' }).click()
  const welcome = page.locator('.w2-welcome-logo')
  await expect(welcome).toBeVisible()
  await expect.poll(() => welcome.evaluate((image: HTMLImageElement) => image.naturalWidth)).toBeGreaterThan(0)
  await expect(welcome).toHaveAttribute('src', await sidebar.getAttribute('src') as string)

  for (const width of [390, 768, 1280]) {
    await page.setViewportSize({ width, height: 900 })
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  }

  const favicon = await page.request.get('/brand-mark.svg')
  expect(favicon.ok()).toBe(true)
  expect(favicon.headers()['content-type']).toContain('image/svg+xml')
  expect(await favicon.text()).toContain('M38 86L48 42L79 86L89 42')
  const ico = await page.request.get('/favicon.ico')
  expect(ico.ok()).toBe(true)
  expect((await ico.body()).subarray(0, 4)).toEqual(Buffer.from([0, 0, 1, 0]))

  await page.goto('/chat')
  const original = page.getByRole('img', { name: 'NaumiAgent', exact: true })
  await expect(original).toBeVisible()
  await expect.poll(() => original.evaluate((image: HTMLImageElement) => image.naturalWidth)).toBeGreaterThan(0)
})
