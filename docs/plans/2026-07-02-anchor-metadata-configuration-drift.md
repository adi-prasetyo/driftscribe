# Anchor Self-Description: "configuration drift" Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make Anchor's own operator-facing metadata call its domain "configuration drift" (not a bare "drift"), so the crew-picker summary and the Capabilities card match the disambiguation the chat prompt now enforces.

**Architecture:** Copy-only change to two operator-facing description fields — the backend workload manifest (`workloads/drift/workload.yaml` `description`) and the checked-in SPA crew catalog (`frontend/src/lib/workloads.catalog.json` `summary`). One insertion each ("drift" → "configuration drift"). The catalog summary is byte-pinned by a Vitest test that must change in lockstep; the yaml description is not byte-pinned but has a mock fixture worth keeping realistic. No logic, no schema, no behavior change.

**Tech Stack:** YAML manifest under `workloads/`, JSON catalog + Vitest (`vitest`) under `frontend/`, Python (`uv` + `pytest` + `ruff`) for the cross-surface sync test.

---

## Background — why this change (read before touching anything)

This is the **optional follow-up** flagged at the end of `docs/plans/2026-07-02-drift-term-disambiguation.md`. That plan (shipped on branch `fix/drift-term-disambiguation`, commit `8a59288`) made Anchor's **chat prompt** always qualify its sense as "configuration drift" and route the infra-map sense ("drift (not in IaC)") to Explore/Provision. But Anchor's *own metadata* — the copy an operator reads in the crew picker and the Capabilities card, before they even open a chat — still introduces the workload with a bare "Detect drift…". This change makes the self-description consistent with the vocabulary the chat now uses.

**Value is deliberately modest.** Unlike the chat-prompt bug (a bare "no drift" that actively misled an operator), these fields already spell the sense out inline — *"drift between a Cloud Run service's live env vars and the … ops-contract.yaml"* is, in context, the definition of config drift. So this is a polish/consistency change, not a defect fix. It's worth doing precisely because it's cheap and removes the one place where Anchor's public vocabulary still differs from its chat vocabulary.

### The two operator-facing copy fields (both say bare "drift" today)

1. **`workloads/drift/workload.yaml` `description:` (lines 22-27)** — surfaced by `agent/capabilities.py` on the `/capabilities` payload and **rendered in full by `CapabilityCard.svelte`** (see the doc-comment on `frontend/src/lib/workloads.ts:54-57`). Current text:
   > Detect drift between a Cloud Run service's live env vars and the team's declared ops-contract.yaml. Propose docs PRs (for sanctioned changes) or HITL rollbacks (for unsanctioned drift on allow_manual_change=false vars). Event-triggered via Eventarc — it runs when the service changes, not on a polling loop.
2. **`frontend/src/lib/workloads.catalog.json` drift entry `summary:` (line 6)** — the shorter crew-picker blurb. Current text:
   > Detects drift between a Cloud Run service's live env vars and the declared ops-contract.yaml, then proposes docs PRs for sanctioned changes or rollbacks for unsanctioned ones. Event-triggered via Eventarc: it runs when the service changes, not on a polling loop.

### Pin surface (verified — this is what makes the change surgical)

- The catalog `summary` is **byte-pinned verbatim** at `frontend/tests/unit/workloads.test.ts:44`. That literal MUST be updated in lockstep with the JSON, or Vitest fails.
- The yaml `description` is **NOT byte-pinned** anywhere. `tests/unit/test_capabilities.py::test_frontend_catalog_matches_backend` compares only `name` / `descriptor` / `group` across the two surfaces — **not** the summary or description. The string at `frontend/tests/unit/CapabilityCard.test.ts:33` (`'Detect drift between a Cloud Run service\'s live env vars and the team\'s declared ops-contract.yaml.'`) is a **self-contained mock fixture** inside that test's `workloads: [...]` array — it does not read the real file, so editing the yaml won't fail it. We update it anyway (Task 3) only to keep the fixture realistic; skipping it would NOT break CI.
- **No overlap** with the concurrent timeline count-bug work (`Timeline.svelte` / `Group.svelte` / `timeline.ts`). The earlier "frontend collision risk" note in the sibling plan does not actually bite — different files.

### The exact wording change

Insert the single word **`configuration`** before **`drift`** in the opening verb phrase of each field. Keep the second "drift" in the yaml (`unsanctioned drift on allow_manual_change=false vars`) **as-is** — that one is already unambiguous in context and reads naturally.

- yaml: `Detect drift between…` → `Detect configuration drift between…`
- catalog: `Detects drift between…` → `Detects configuration drift between…`

### Explicitly OUT OF SCOPE

- **Do NOT rename the workload symbolic `name: drift`, the `display_name: Anchor`, or the `descriptor: Cloud Run config`.** `name` is FROZEN (comment at `workload.yaml:13-14`); `descriptor` "Cloud Run config" is already a good config-scoped subtitle and is pinned in many tests (`test_workload_spec.py`, `CrewPicker.test.ts`, `CapabilityCard.test.ts`, etc.). Touching them is a much larger blast radius for zero benefit here.
- **Do NOT touch the infra map's "drift (not in IaC)" vocabulary** (`InfraDiagram.svelte`, `infra_graph.ts`, `totals.drift`, Mermaid class). That is the separate, larger "single-sense drift" rename deferred in the sibling plan.
- **Do NOT touch `agent/main.py:1438`** (`f"Detect drift for Cloud Run service …"`) — it's an internal autonomous-notification string, not the picker/Capabilities self-description. Optional-within-optional; flag it, don't bundle it (Notes).
- **Do NOT re-touch the chat prompt or its golden/named tests** — that shipped separately on `fix/drift-term-disambiguation`.

---

## Task 1: Update the crew-catalog summary + its Vitest pin (frontend)

**Files:**
- Modify: `frontend/src/lib/workloads.catalog.json` (drift entry `summary`, line 6)
- Modify (lockstep, same commit): `frontend/tests/unit/workloads.test.ts` (the pinned summary literal, line 44)

**Step 1: Update the Vitest pin first (make it fail)**

In `frontend/tests/unit/workloads.test.ts`, find the drift `summary:` literal (line 44) and change its leading `Detects drift between` to `Detects configuration drift between`. Leave the rest of the sentence byte-identical.

**Step 2: Run the frontend catalog test to verify it fails**

Run: `cd frontend && npm run test:unit -- workloads.test.ts`
Expected: FAIL — the JSON (still `Detects drift…`) no longer equals the updated literal. This proves the pin is live.

**Step 3: Make the identical edit to the JSON**

In `frontend/src/lib/workloads.catalog.json`, change the drift entry's `summary` opening from `Detects drift between a Cloud Run service's live env vars` to `Detects configuration drift between a Cloud Run service's live env vars`. Nothing else on that line changes.

**Step 4: Run the frontend catalog test to verify it passes**

Run: `cd frontend && npm run test:unit -- workloads.test.ts`
Expected: PASS — JSON literal matches the pin again.

**Step 5: Commit**

```bash
git add frontend/src/lib/workloads.catalog.json frontend/tests/unit/workloads.test.ts
git commit -m "chore(catalog): Anchor summary says 'configuration drift' (match chat vocabulary)"
```

---

## Task 2: Update the backend workload manifest description

**Files:**
- Modify: `workloads/drift/workload.yaml` (`description`, line 22-23: the `Detect drift between…` clause)

**Step 1: Edit the yaml description**

In `workloads/drift/workload.yaml`, change the first line of the folded `description:` block from:
```
  Detect drift between a Cloud Run service's live env vars and the
```
to:
```
  Detect configuration drift between a Cloud Run service's live env vars and
```
(Re-flow the folded-scalar wrap so lines stay ≲72 cols; the `>` folded block joins with spaces, so exact wrap points don't affect the rendered value — only readability. Keep the later `unsanctioned drift on allow_manual_change=false vars` clause unchanged.)

**Step 2: Verify the manifest still loads + cross-surface sync still holds**

Run: `uv run --extra dev pytest tests/unit/test_drift_workload_loads.py tests/unit/test_capabilities.py tests/unit/test_workload_spec.py -q`
Expected: PASS — the description is not byte-pinned, and `test_frontend_catalog_matches_backend` checks only name/descriptor/group, so this edit is green without any test change. (This step's real job is to *prove* that: if anything here fails, a pin exists that the Background missed — stop and reconcile.)

> Note: this repo's tests run under the `dev` optional-dependency group — use `uv run --extra dev pytest …` (a bare `uv run pytest` fails to spawn pytest).

**Step 3: Commit**

```bash
git add workloads/drift/workload.yaml
git commit -m "chore(drift): manifest description says 'configuration drift'"
```

---

## Task 3: Keep the CapabilityCard mock fixture realistic (optional-but-tidy)

**Why:** `frontend/tests/unit/CapabilityCard.test.ts:33` hardcodes a mock `description` mirroring the yaml's first sentence. It does NOT read the real file, so it will stay green regardless — but leaving it saying "Detect drift…" while the manifest says "Detect configuration drift…" makes the fixture lie about production copy. One-word update keeps future readers honest. Skip only if you want an absolutely minimal diff.

**Files:**
- Modify: `frontend/tests/unit/CapabilityCard.test.ts` (mock `description`, line 33)

**Step 1: Edit the mock string**

Change `description: 'Detect drift between a Cloud Run service\'s live env vars and the team\'s declared ops-contract.yaml.'` to `description: 'Detect configuration drift between a Cloud Run service\'s live env vars and the team\'s declared ops-contract.yaml.'`.

**Step 2: Run the component test**

Run: `cd frontend && npm run test:unit -- CapabilityCard.test.ts`
Expected: PASS (it asserts substrings like `'Cloud Run config'` and `'Anchor — Cloud Run config …'`, none of which touch the changed word).

**Step 3: Commit**

```bash
git add frontend/tests/unit/CapabilityCard.test.ts
git commit -m "test(capability-card): mock fixture mirrors 'configuration drift' manifest copy"
```

---

## Task 4: Full gate

**Files:** none (verification only)

**Step 1: Grep for any remaining un-updated pin of the old opening phrase**

Run: `rg -n "Detects? drift between" workloads frontend/src frontend/tests`
Expected: every hit now reads `configuration drift` (the two source fields + the two test pins). If a bare `Detect(s) drift between` remains in a *pinned* location you didn't touch, update it in the same spirit. (Scope the scan to those paths as written — do NOT broaden to the repo root: `agent/static/transparency-*.js` is a gitignored built SPA bundle carrying the OLD copy and would report a false positive. Codex 019f2288.)

**Step 2: Frontend suite**

Run: `cd frontend && npm run test:unit`
Expected: all green (Vitest).

**Step 3: Backend gate**

Run: `uv run --extra dev pytest -q && uv run --extra dev ruff check .`
Expected: all green; `3230+` passed, ruff clean.

**Step 4: (No deploy in this plan.)** Same prod caveat as the sibling plan: `workloads/` is baked into the coordinator image (`Dockerfile.agent`) and the SPA is a built bundle, so neither the manifest nor the catalog copy goes live until a **new coordinator image / SPA build + revision** is rolled. Call this out to the user; do not deploy without confirmation.

---

## Verification checklist (definition of done)

- [ ] `workloads/drift/workload.yaml` `description` opens with "Detect configuration drift between…".
- [ ] `frontend/src/lib/workloads.catalog.json` drift `summary` opens with "Detects configuration drift between…", byte-identical to the updated Vitest pin.
- [ ] `frontend/tests/unit/workloads.test.ts:44` literal updated in lockstep; `workloads.test.ts` passes.
- [ ] (Optional Task 3) `CapabilityCard.test.ts:33` mock mirrors the manifest.
- [ ] Symbolic `name`, `display_name: Anchor`, `descriptor: Cloud Run config` **unchanged**; infra-map "drift" vocabulary **unchanged**; chat prompt + its tests **unchanged**.
- [ ] `cd frontend && npm run test:unit` and `uv run --extra dev pytest -q` + `ruff check .` all clean.

---

## Notes for the user (surface these; not implementation steps)

- **This is polish, not a fix.** The chat-prompt change (`fix/drift-term-disambiguation`) already closed the actual mislead. These two fields already define the sense inline; the edit just makes Anchor's *word* ("configuration drift") consistent everywhere an operator meets it. Low urgency — bundle it into the same PR as the chat fix, ship it separately, or skip it.
- **Optional-within-optional:** `agent/main.py:1438` autonomous notification string also says "Detect drift for Cloud Run service …". If you want *zero* bare "Detect drift" anywhere Anchor speaks, that's a third one-word edit — left out here because it's an internal Eventarc notification, not the operator-facing picker/Capabilities self-description.
- **Prod pickup needs a new coordinator image + SPA build/revision** — not carried by merge alone.
