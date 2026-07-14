import { defineConfig, devices } from '@playwright/test'

/**
 * Playwright configuration for the NaumiAgent Workbench web UI.
 *
 * E2E smoke tests run against the Vite preview server with mocked backend
 * responses, so they do not require a running NaumiAgent daemon.
 */
export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: 'list',
  use: {
    baseURL: 'http://localhost:4173',
    trace: 'on-first-retry',
  },
  projects: [
    {
      name: 'chromium',
      // Prefer the system-installed Chrome/Edge to avoid a large browser
      // download; fall back to the bundled chromium if no channel is found.
      use: {
        ...devices['Desktop Chrome'],
        channel: 'chrome',
      },
    },
  ],
  webServer: {
    command: 'pnpm build && pnpm preview --port 4173',
    url: 'http://localhost:4173',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
})
