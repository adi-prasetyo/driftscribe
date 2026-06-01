import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

// Mock-Playwright smoke: boots the REAL FastAPI app (serving the built Svelte
// shell + /static) and intercepts the data endpoints with page.route. This is
// the pre-merge stand-in for the dispatch-only cloud e2e (tests/e2e/ui).
//
// PREREQ: `npm run build` must have produced agent/static/ first (CI's ui-smoke
// job + the Makefile target do this). The webServer boots uvicorn in DRY_RUN so
// no GCP creds are needed; all JSON/SSE the app calls are mocked in the spec.

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(__dirname, '../../..');
const PORT = 8765;

export default defineConfig({
  testDir: '.',
  testMatch: '**/*.smoke.ts',
  timeout: 30_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: 'uv run uvicorn agent.main:app --host 127.0.0.1 --port 8765',
    cwd: REPO_ROOT,
    url: `http://127.0.0.1:${PORT}/healthz`,
    timeout: 120_000,
    reuseExistingServer: !process.env.CI,
    env: { DRY_RUN: 'true', USE_ADK: 'false' },
  },
});
