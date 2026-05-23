import { defineConfig, devices } from '@playwright/test';

const baseURL = process.env.DRIFTSCRIBE_E2E_URL;
if (!baseURL) {
  console.warn('DRIFTSCRIBE_E2E_URL not set; UI tests will fail.');
}

export default defineConfig({
  testDir: './tests',
  timeout: 90_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  retries: 1,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
});
