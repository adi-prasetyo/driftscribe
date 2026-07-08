import { defineConfig, devices } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

// Visual route-mock rig for the composer New-chat + crew-lock feature
// (docs/plans/2026-07-08-composer-new-chat-crew-lock.md, Task 5.2).
//
// Deliberately SEPARATE from the smoke rig (tests/smoke): this one boots the
// VITE DEV SERVER (not uvicorn, not the live deployment) and mocks every JSON
// endpoint with page.route, so it needs zero backend and never touches GCP or
// the public demo host. It exists to eyeball the lock states + capture PNGs, not
// to gate CI — run it by hand:
//
//   npx playwright test --config tests/visual/playwright.visual.config.ts
//
// (NOT wired into package.json — `npm run test:smoke` stays the only Playwright
// script, and it drives the live demo, so it must not be run during the window.)

const __dirname = dirname(fileURLToPath(import.meta.url));
const FRONTEND_ROOT = resolve(__dirname, '../..');
const PORT = 5199;

export default defineConfig({
  testDir: '.',
  testMatch: '**/*.visual.ts',
  timeout: 30_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: `http://127.0.0.1:${PORT}/`,
    // A comfortable desktop viewport so the two-column layout renders (the SPA
    // drops to a single column below 760px).
    viewport: { width: 1280, height: 900 },
    trace: 'off',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    // Force base '/' for dev (vite.config pins base '/static/' for the backend-
    // integration BUILD; in dev we want the app served at the root).
    command: `npm run dev -- --host 127.0.0.1 --port ${PORT} --strictPort --base /`,
    cwd: FRONTEND_ROOT,
    url: `http://127.0.0.1:${PORT}/`,
    timeout: 120_000,
    reuseExistingServer: true,
  },
});
