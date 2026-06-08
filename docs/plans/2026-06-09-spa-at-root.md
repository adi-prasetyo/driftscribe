# Serve the operator SPA at `/` (delete `/ui/transparency`) — Implementation Plan

> **For Claude:** Execute task-by-task. Small, mechanical, fully-typed change.

**Goal:** Serve the Svelte transparency SPA at the site root `/` instead of `/ui/transparency`. Remove the old `/ui/transparency` route entirely — no redirect, no alias (per user decision; a bare 404 on the old path is acceptable).

**Architecture:** Move the existing `transparency_ui` FastAPI handler from path `/ui/transparency` to `/`. Nothing else about the handler changes (same template, same `_shell_assets`, same `Cache-Control: no-store`, same no-app-auth/CF-Access-at-edge posture).

**Tech Stack:** FastAPI (coordinator), Svelte+Vite SPA (`base: '/static/'`), pytest + Playwright smoke/e2e.

---

## Why this is low-risk (grounding, all verified)

- **Asset URLs are path-independent.** `frontend/vite.config.ts` sets `base: '/static/'`, so every JS/CSS URL the shell emits is absolute `/static/...`, resolved from the Vite manifest. They don't depend on the shell's own URL.
- **API calls are absolute-from-root.** The SPA fetches `/chat`, `/trace/{id}`, `/decisions` — unaffected by where the shell is served.
- **No client-side routing / deep links.** `grep` of `frontend/src` found no `URLSearchParams` / `location.search` / `location.hash` / `pushState` / `<base>`. The SPA is a single view with no on-load query-param read, so there is nothing to preserve and no refresh-404 surface (no catch-all needed).
- **Nothing links to `/ui/transparency`.** The only code references are the route decorator + doc comments in `agent/main.py`. Approval templates don't link to it; the legacy route is independent.
- **CF Access posture unchanged.** The bare domain `/` is already edge-gated (the shell was always unauthenticated at the app layer, gated by Cloudflare Access at the edge).

## Out of scope

- `/ui/transparency-legacy` stays as-is (independent one-release safety net).
- Historical `docs/plans/*` and memory files that mention the old path are left untouched (they're records of past work).
- No frontend `src/` change. No `/static` mount change.

---

### Task 1: Move the route to `/` and delete the old path

**Files:** Modify `agent/main.py`

**Step 1 — change the route decorator (main.py:2056).** Replace
`@app.get("/ui/transparency", response_class=HTMLResponse)`
with
`@app.get("/", response_class=HTMLResponse)`.
Keep the function name `transparency_ui` and its entire body unchanged. Update the first docstring line to say it serves the SPA shell at `/` (drop the `/ui/transparency` mention).

**Step 2 — update the two doc comments.** `main.py:211` ("The operator UI (GET /ui/transparency) is a Svelte SPA…") and `main.py:1695` ("backs the `/ui/transparency` decision history") → reference `/`.

**Step 3 — leave `/ui/transparency-legacy` (main.py:2083) exactly as-is.**

### Task 2: Update the integration test + assert the old path 404s

**Files:** Modify `tests/integration/test_ui_transparency.py`

- Change all 5 `client.get("/ui/transparency")` → `client.get("/")`.
- Update the module docstring and the route-named test functions (`test_ui_transparency_route_*` → `test_root_route_*` is optional; at minimum fix the docstrings so they don't lie).
- Keep `test_legacy_ui_still_reachable` (hits `/ui/transparency-legacy`) untouched.
- **Add** `test_old_transparency_path_is_gone`: `client.get("/ui/transparency").status_code == 404` (the route is deleted, no redirect).

**Run:** `pytest tests/integration/test_ui_transparency.py -q` — expect all green, including the new 404 assertion.

### Task 3: Update the frontend smoke + e2e gotos

**Files:** Modify `frontend/tests/smoke/transparency.smoke.ts`, `tests/e2e/ui/tests/transparency.spec.ts`, `tests/e2e/ui/README.md`

- `page.goto('/ui/transparency')` → `page.goto('/')` everywhere (≈9 in the smoke, 1 in the e2e spec). The smoke's `webServer` boots real uvicorn in DRY_RUN, so this genuinely exercises the new root route.
- README: update the `/ui/transparency` mention to `/`.

### Task 4: Cosmetic doc-comment path fixes

**Files:** Modify `tests/unit/test_transparency_template_testids.py` (line 4), `tests/integration/test_decisions_endpoint.py` (line 4)

- Update the `/ui/transparency` mentions in their module docstrings to `/`. No behavioral change.

### Task 5: Verification gate

- `ruff check agent/ tests/` — clean.
- `pytest tests/integration/test_ui_transparency.py tests/integration/test_decisions_endpoint.py tests/unit/test_transparency_template_testids.py -q` — green.
- Full backend `pytest -q` — green (no other test asserts the old path).
- Frontend: `cd frontend && npm run check && npm run build` — clean. Smoke if runnable in this env.

---

## Codex plan review (thread 019ea7fe-cb31-7e33-a44e-5e7623edea52) — GO, with additions

Codex confirmed: (1) `@app.get("/")` is exact and won't steal `/static/*`; `/docs`/`/openapi.json` unaffected; Starlette won't slash-redirect `/ui/transparency`→`/`. (2) Deleting the path is infra-safe from repo-visible config — CF Access is host-scoped, the Worker route is `driftscribe.adp-app.com/*`, no Eventarc/health/IAP pin to the UI path. (3) "No catch-all" is sound (no client router → refresh only needs `/`; hashes never reach the server). Two changes folded in below.

**Expanded operator-facing reference list (Task 4 grows; historical `docs/plans/*` + memory stay stale):**
- `README.md:147`, `README.ja.md:143` — Transparency-UI URL → `/`.
- `docs/demo-script.md` lines 342, 376, 382, 383, 389 — prose + `curl`/open examples + walkthrough table → `/` (keep `/trace`, `/decisions`).
- `docs/demo-script.ja.md` lines 255, 282, 285, 291 — same, in Japanese.
- `infra/cloudflare/setup-access.sh:140` — printed `Public URL: https://$HOST/ui/transparency` → `https://$HOST/`.

**Test refinements:** the smoke has **10** `page.goto` hits (not ~9). The deletion test uses `client.get("/ui/transparency", follow_redirects=False).status_code == 404` so it explicitly rejects any redirect/alias.

## Deploy (operator, after merge)

Same coordinator redeploy + traffic-shift as PR #77:
`gcloud builds submit --config=infra/cloudbuild.coordinator-update.yaml --substitutions=_TAG=$(git rev-parse --short HEAD) --project=driftscribe-hack-2026`, then `gcloud run services update-traffic driftscribe-agent --to-revisions=<new-rev>=100 --region=asia-northeast1` (traffic-pinning gotcha — the build lands the new rev at 0%). Smoke `/` on the direct run.app URL for a 200 HTML shell; the public domain stays CF-Access-gated.
