import { defineConfig, devices } from '@playwright/test'

const live = !!process.env.PLAYWRIGHT_LIVE

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: true,
  retries: process.env.CI ? 2 : 0,
  reporter: 'list',
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL ?? (live ? 'http://127.0.0.1:3001' : 'http://127.0.0.1:3101'),
    trace: 'off',
    video: 'off',
    screenshot: 'off',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: live
    ? undefined
    : [
        {
          command: 'node tests/mock-api.mjs',
          port: 8010,
          reuseExistingServer: false,
        },
        {
          command: 'VECTORLESS_API_URL=http://127.0.0.1:8010 APP_ORIGIN=http://127.0.0.1:3101 SESSION_SECRET=1111111111111111111111111111111111111111111111111111111111111111 pnpm dev --host 127.0.0.1 --port 3101',
          port: 3101,
          reuseExistingServer: false,
        },
      ],
})
