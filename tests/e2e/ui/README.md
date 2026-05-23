# DriftScribe UI E2E (Playwright)

Playwright UI end-to-end tests for the `/ui/transparency` page (Phase 19.B
transparency UI). Chromium-only; hackathon scope.

## Install

```bash
cd tests/e2e/ui
npm install
npx playwright install chromium
```

This creates `node_modules/` and `package-lock.json` locally — both are
ignored by the project's `.gitignore` (`node_modules/`, `package-lock.json`).

## Run

```bash
# Required env vars:
export DRIFTSCRIBE_E2E_URL="https://<agent-base-url>"
export DRIFTSCRIBE_E2E_TOKEN="<operator-token>"

npm test            # headless
npm run test:headed # head-full (debug)
```

The HTML report lands in `playwright-report/`; traces/screenshots are
retained on failure under `test-results/`.

## Provisioning

Full E2E environment setup (the agent URL, the token, the cleanup
contract) lives in [`docs/runbooks/e2e-environment.md`](../../../docs/runbooks/e2e-environment.md).
