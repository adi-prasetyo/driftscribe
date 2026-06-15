# Vertex `global` region for thought summaries + hover-help note — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make Gemini's reasoning summaries appear in the operator UI's "Coordinator reasoning" panel by routing Vertex inference to the `global` region, and add a tiny hover-help icon explaining the region/latency tradeoff so the lag is self-documenting.

**Architecture:** Empirically established (5-region × 3-layer probe): Gemini 2.5 Flash on Vertex `asia-northeast1` spends thinking tokens but returns **zero** `thought=True` summary parts; only `global` and `us-central1` return them. `global` is the lower-latency of the two. The fix is a single env-var change (`GOOGLE_CLOUD_LOCATION`) on the coordinator — the only service that calls Gemini — plus a one-icon UI affordance. No change to the streaming/rendering path (it already renders `llm_thought` events correctly; they simply never arrived).

**Tech Stack:** Cloud Run env var, Svelte 5 SPA (Vite), vendored Lucide icons, vitest + @testing-library/svelte.

**Tradeoff accepted by operator:** ~2–4× per-call latency (measured from WSL: asia 0.9s → global 4.0s; prod gap over GCP backbone likely smaller but real), US-routed inference, and a small output-token cost bump (the summary text is billed as output; negligible vs the ¥2000/mo budget). In exchange: visible AI reasoning, which is core to the transparency pitch.

---

## Task 1: Switch Vertex region to `global` in the deploy config

**Files:**
- Modify: `infra/cloudbuild.yaml:321` (the coordinator full-deploy `--set-env-vars` string)

**Step 1:** In the single long `--set-env-vars=...` line, change ONLY `GOOGLE_CLOUD_LOCATION=asia-northeast1` → `GOOGLE_CLOUD_LOCATION=global`.
**CRITICAL — do not touch:** `TARGET_REGION=asia-northeast1` (the demo app's region) and the Cloud Run `--region asia-northeast1` deploy flag elsewhere (the coordinator service's own region). Only the Vertex inference location changes.

**Step 2:** Grep to confirm exactly one substitution and that `TARGET_REGION` is untouched:
`grep -n "GOOGLE_CLOUD_LOCATION\|TARGET_REGION" infra/cloudbuild.yaml`
Expected: `GOOGLE_CLOUD_LOCATION=global`, `TARGET_REGION=asia-northeast1` still present.

## Task 2: Add the `help-circle` icon to the registry

**Files:**
- Modify: `frontend/src/lib/icons.ts`

**Step 1:** Add a new entry to `ICON_PATHS` (standard Lucide `help-circle`, 24×24 stroke):
```ts
'help-circle': '<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><path d="M12 17h.01"/>',
```
`IconName` picks it up automatically via `keyof typeof ICON_PATHS`. No icon test pins the registry (verified — the old DOMParser drift-pin test no longer exists), so nothing else to update here.

## Task 3: Add an opt-in `hint` prop to Group.svelte

**Files:**
- Modify: `frontend/src/components/Group.svelte`
- Test: `frontend/src/components/Group.test.ts` (new)

**Step 1 (test first):** Create `Group.test.ts`:
- renders the `help-circle` hint icon + sets `title`/`aria-label` to the hint text when `hint` is passed;
- renders NO `.group__hint` when `hint` is absent.
(Use `@testing-library/svelte` `render` + a `children` snippet; assert on `container.querySelector('.group__hint')` and its `title` attribute. Mirror the existing testing-library patterns in the repo.)

**Step 2:** Run it, expect FAIL (prop doesn't exist yet).

**Step 3:** Add `hint?: string` to the `$props()` type + destructure. In the `<summary>`, inside `.group__title` after `{title}`, render:
```svelte
{#if hint}<span class="group__hint" title={hint} aria-label={hint}><Icon name="help-circle" size={13} /></span>{/if}
```
Add CSS: `.group__hint { display: inline-flex; color: var(--ds-faint); cursor: help; }` (so the hover cursor reads as "help"; zero extra layout height — it sits inline next to the title).

**Step 4:** Run the test, expect PASS. Run `npx svelte-check` (expect 0/0).

## Task 4: Wire the coordinator-only hint copy in Timeline.svelte

**Files:**
- Modify: `frontend/src/components/Timeline.svelte`

**Step 1:** Add a copy constant near `titleFor`:
```ts
const COORDINATOR_HINT =
  "Gemini's reasoning summaries are only returned by Vertex AI's ‘global’ region, " +
  'so this deployment routes inference there — which adds a little latency per turn.';
```
**Step 2:** Pass `hint={COORDINATOR_HINT}` to the coordinator `<Group>` ONLY (not tools/mcp).

**Step 3:** `npm run build` + `npx svelte-check` clean; run full `vitest` (expect green, net +N from the new Group test).

## Task 5: Local visual verify (the rig from memory)

Build, then `DRY_RUN=true USE_ADK=false DRIFTSCRIBE_TOKEN=local-dev-token uv run uvicorn agent.main:app --host 127.0.0.1 --port 8765` (restart after rebuild — the manifest is cached at startup; ensure :8765 is free). Seed the token via the Operator modal. Confirm the help-circle sits next to "Coordinator reasoning" and the tooltip shows the copy on hover. (`/chat` is off in this mode — we're verifying the affordance, not live summaries.)

## Task 6: Deploy (coordinator only — region env + new SPA bundle)

1. `gcloud builds submit --config infra/cloudbuild.coordinator-update.yaml --substitutions=_TAG=<short-sha>` (bakes the new SPA bundle, lands new revision at 0% — traffic is pinned).
2. Set the live Vertex region on the new revision's env: include `--update-env-vars GOOGLE_CLOUD_LOCATION=global` (either folded into the deploy step or a follow-up `gcloud run services update driftscribe-agent --region asia-northeast1 --update-env-vars GOOGLE_CLOUD_LOCATION=global`). NOTE: `services update --image` PRESERVES existing env, so the env var MUST be set explicitly or it stays `asia-northeast1`.
3. `gcloud run services update-traffic driftscribe-agent --region asia-northeast1 --to-revisions <new>=100`.
4. Record rollback target (current live `00086-s2w`).

## Task 7: Live verification (the real end-to-end proof)

- `gcloud run services describe driftscribe-agent ... ` → confirm `GOOGLE_CLOUD_LOCATION=global` on the serving revision.
- Send a real chat turn through prod, then:
  `gcloud logging read 'jsonPayload.event="llm_thought"' --freshness=1h --limit=3` → expect ≥1 entry (was 0 in 30 days). This is the definitive proof summaries now flow.
- Visually confirm the "Coordinator reasoning" panel now shows reasoning text rows + the hover note.

## Codex review (thread 019eca21) — adopted corrections
1. **Deploy in ONE revision:** fold `GOOGLE_CLOUD_LOCATION=global` into the `ENV_VARS` string in `infra/cloudbuild.coordinator-update.yaml:135` (the `^@^`-delimited `--update-env-vars` list) so the image+region land on a single new revision — then `update-traffic` to that one revision. (Avoids the image-revision-vs-env-revision traffic trap.)
2. **Test path:** vitest `include` is `tests/unit/**/*.test.ts` — the new test goes at `frontend/tests/unit/Group.test.ts` (NOT `src/components/`). Commands run from `frontend/`: `npm run build`, `npm run check`, `npm run test:unit`.
3. **Wording:** the `global` endpoint does NOT guarantee data residency / region — say "global endpoint (no Japan-region processing guarantee)", not "US-routed". (UI copy already only says "routes inference there", which is accurate.)
4. **a11y:** add `role="img"` alongside `aria-label`+`title` on the hint span; it's supplementary hover-help (operator-only), not robust focus/touch help — acceptable here.

## Out of scope
- Raw chain-of-thought (Gemini never exposes it — only summaries).
- Per-path region split (chat vs autonomous) — rejected: adds config complexity, and the global switch is uniform/simplest.
- Any worker change — no worker calls Gemini (verified).
