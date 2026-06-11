# Adopt Button UI (Adopt/Import Phase 4) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let a ClickOps operator adopt an unmanaged resource straight from the resource
map — an Adopt affordance on unmanaged nodes that prefills the chat, plus calm
adoption framing on the approval page (banner slot + blast-radius reframe + meter
note). Design: docs/plans/2026-06-11-adopt-import-design.md §6 + §7 item 4.

**Architecture:** The map's Mermaid SVG is rendered with `securityLevel:'strict'` /
`htmlLabels:false` (deliberate security posture) — so the Adopt affordance is NOT an
in-SVG click target; it is an "Unmanaged resources" action list in the same panel,
derived from the graph DTO. The server marks which groups are adoptable (single
source of truth: `ADOPTABLE_RESOURCE_TYPES`); the frontend composes the prefill text
from the group label + node label (all four group labels already canonicalize via
`adopt_recipe._TYPE_ALIASES`). The approval page gains an adopt-only banner branch
and an adopt-only blast-radius variant, both driven by a new
`PlanSummary.adopt_only` property.

**Tech Stack:** Python 3.12 + Jinja2 template (coordinator), Svelte 5 runes + vitest +
@testing-library/svelte (SPA). Coordinator rebake + pinned-traffic cutover on deploy.

---

## 0. Grounding facts (verified in-repo 2026-06-11)

- Mermaid strict mode forbids click handlers; labels are entity-escaped
  (`frontend/src/lib/infra_graph.ts`); the panel already renders legend/notes as
  normal DOM below the SVG — the action list lives there.
- Graph DTO: groups `{asset_type,label,count,managed,drift,sensitive,nodes[,truncated_in_group]}`,
  nodes `{id,label,asset_type,managed,location}` (`driftscribe_lib/infra_graph.py:258-282`).
  `build_graph` is called ONLY by the coordinator (`agent/main.py` /infra/graph) —
  adding a group field needs ONLY a coordinator rebake.
- `PLAN_RTYPE_TO_ASSET_TYPE` (`driftscribe_lib/infra_graph.py:36-50`) already maps the
  four adoptable HCL types to CAI asset types; `ADOPTABLE_RESOURCE_TYPES` lives in
  `driftscribe_lib/iac_plan_denylist.py:281-288` (no circular import risk —
  iac_plan_denylist does not import infra_graph).
- Group labels: "Storage bucket", "Pub/Sub topic", "Pub/Sub subscription",
  "Cloud Run service" — each normalizes to an existing `_TYPE_ALIASES` key
  (`storage_bucket`, `pub/sub_topic`, `pub/sub_subscription`, `cloud_run_service`),
  so `propose_adoption_tool` canonicalizes the prefill's type words deterministically.
- Adoption rides the **provision** workload (`provision_propose_adoption`); the chat
  default is `drift` — the prefill MUST also switch the workload select.
- ChatForm: `prompt` is local `$state`; no programmatic set today. App submits via
  `submitChat(prompt, workload)` → POST /chat.
- Approval template banner chain (`agent/templates/iac_approval.html:99-114`):
  `s.destructive` → red; `not s.all_accounted_safe` → caution; else green
  no-destroy-note. Import-only plans currently land on the generic green note.
  Blast line at :115-120; entries already carry the per-row "imported into
  management" subtitle (:134-137). The approve-outcome copy in `agent/main.py:3644`
  is already adoption-aware — untouched.
- Pin tests to respect: `tests/unit/test_iac_plan_summary.py:642-660`
  (BLAST_CANNOT_TOUCH_NOTE), `frontend/tests/unit/CapabilityCard.test.ts:333-365`
  (adoptable-types card). Frontend tests: vitest + @testing-library/svelte under
  `frontend/tests/unit/` (InfraDiagram.test.ts, infra_graph.test.ts exist).

## 1. Product behavior

**Map panel** (open state, non-degraded, after the legend): a new "Unmanaged
resources" block listing the drift nodes SHOWN ON THE MAP (non-sensitive groups
only — sensitive groups are counts-only and carry no names by design; the
inventory samples up to 10 nodes per type, so the heading copy must not claim
exhaustiveness — Codex finding 4):

```
Unmanaged resources shown on the map — they exist in your project but are not
under IaC management
  Storage bucket   my-old-uploads          [ Adopt into IaC ]
  Service account  ci-runner@…             not yet adoptable
```

- Adoptable group (`group.adoptable`) → button. Click → chat input is prefilled
  (NOT sent — the operator stays in charge):
  `Adopt the Storage bucket `my-old-uploads` into IaC management.` (with
  `` in LOCATION `` inserted before "into" when the node has a location), the
  workload select flips to **Provision (infra edits)**, and the chat input is
  focused. Clicking a second Adopt overwrites the prefill (epoch-bumped).
- Non-adoptable group → muted "not yet adoptable" (design §6's honest affordance).
- No drift nodes → the block does not render at all.
- **Disabled with the chat** (Codex must-fix 3): App passes the SAME condition that
  disables ChatForm (busy / historical replay) as `adoptDisabled`; buttons render
  `disabled` with title "Unavailable while the chat is busy or reviewing a past
  trace." — an Adopt click can never silently mutate a disabled input or leave a
  stale draft behind a historical view.
- Hidden-unmanaged honesty (Codex round-2 fix): `truncated_in_group` counts ALL
  unsampled resources, not unmanaged ones — so the muted trailer line computes
  `hiddenUnmanaged = max(0, group.drift - <unmanaged rows actually shown for the
  group>)` and renders "+N more unmanaged {label}(s) not on the map" ONLY when
  that is > 0 (never claims hidden managed resources are unmanaged).

**Approval page**, when the plan is adopt-only (`s.adopt_only`):
- Banner slot (replaces the generic green note, NEVER reachable when destructive):
  > Nothing in your infrastructure will be modified — this only puts
  > 1 resource under management. The resource map and coverage meter count it as
  > managed once the change merges (moments before the apply completes).
- Blast line variant:
  > This change puts under management at most: 1 Storage bucket. It modifies
  > nothing — the live resource is only recorded in OpenTofu state.
  > {{ cannot_touch_note }}
- Mixed plans (import + anything else): `adopt_only` is False → existing branches
  render exactly as today (destructive still wins outright).

## 2. Changes by file

### Backend

1. `driftscribe_lib/infra_graph.py`
   - `ADOPTABLE_ASSET_TYPES: frozenset[str]` = the CAI types of
     `ADOPTABLE_RESOURCE_TYPES` mapped through `PLAN_RTYPE_TO_ASSET_TYPE`
     (computed, not hand-listed — a new adoptable type propagates automatically;
     a drift-pin test asserts the resolved set is exactly the four).
   - Each group dict gains `"adoptable": (asset_type in ADOPTABLE_ASSET_TYPES) and not sensitive`.

2. `driftscribe_lib/iac_plan_summary.py` — on `PlanSummary`:
   ```python
   @property
   def adopt_only(self) -> bool:
       """True iff the plan does NOTHING except import (adopt) resources —
       drives the approval page's calm adoption framing. Counts are full-plan
       (not display-capped), so n_hidden does not weaken the claim."""
       return self.n_import > 0 and (
           self.n_create + self.n_update + self.n_destroy
           + self.n_replace + self.n_forget + self.n_change
       ) == 0
   ```

3. `agent/templates/iac_approval.html`
   - Banner chain becomes: `{% if s.destructive %}` (unchanged red) →
     `{% elif s.adopt_only %}` new `ds-ok` block `data-testid="adopt-note"` with the
     §1 copy (`{{ s.n_import }} resource{{ '' if s.n_import == 1 else 's' }}`) →
     `{% elif not s.all_accounted_safe %}` (unchanged) → `{% else %}` (unchanged).
   - Blast block: keep the single `{% if (blast_phrase | default("")) %}` gate and
     `data-testid="blast-radius"`; inside, branch on `s.adopt_only` (guarded:
     `{% set s = ... %}` is already in scope) for the §1 variant copy vs the
     existing "can affect at most" copy. `cannot_touch_note` renders in BOTH.

### Frontend

4. `frontend/src/lib/infra_graph.ts`
   - `InfraGroup` gains `adoptable?: boolean` (optional — a stale coordinator
     response without the field simply renders no buttons; fail-quiet, not wrong).
   - New pure helpers (unit-testable, keep the component thin):
   ```ts
   export interface AdoptRow {
     nodeId: string;
     groupLabel: string;
     nodeLabel: string;
     adoptable: boolean;
     /** Chat prefill — composed ONLY for adoptable rows, else ''. */
     prefill: string;
   }

   /** Drift nodes across non-sensitive groups, in render order. */
   export function adoptRows(graph: InfraGraph): AdoptRow[] { ... }

   /**
    * Normalize an untrusted fragment for inclusion in the agent prompt
    * (Codex must-fix 2): strip C0/C1 control chars (incl. CR/LF/tab — collapse
    * to a single space), collapse whitespace runs, trim, and cap length
    * (node label 254 — the adopt_recipe name validator's own max, so a valid
    * adopt name is NEVER truncated; location 40, group label 40). NOT an HTML
    * escape — the only sink is a text input / JSON prompt field.
    */
   export function normalizeForPrompt(raw: string, max: number): string { ... }

   /** "Adopt the Storage bucket `name` in asia-northeast1 into IaC management." */
   export function adoptPrefill(groupLabel: string, nodeLabel: string,
                                location: string | null): string {
     const type = normalizeForPrompt(groupLabel, 40);
     const name = normalizeForPrompt(nodeLabel, 254);
     const loc = location ? normalizeForPrompt(location, 40) : '';
     const where = loc ? ` in ${loc}` : '';
     return `Adopt the ${type} \`${name}\`${where} into IaC management.`;
   }
   ```
   (`adoptRows` filters `g.sensitive === false` and `!n.managed`; `adoptable` is
   `g.adoptable === true`. Node labels/locations are untrusted; there is no
   DOM/HTML sink (Svelte text interpolation + a text input), but the prefill IS a
   prompt-to-the-agent path, hence the normalizer. The operator always sees and
   sends the text themself; the agent side independently re-validates via
   `propose_adoption_tool` param checks + the full gate chain.)

5. `frontend/src/components/InfraDiagram.svelte`
   - New props `onAdopt?: (prefill: string) => void`, `adoptDisabled?: boolean`.
   - After the legend block: `{#if graph && !degraded}` and `adoptRows(graph)`
     non-empty → render the §1 list (`data-testid="adopt-list"`, per-row
     `data-testid="adopt-row"`, button `data-testid="adopt-btn"`, muted span
     `data-testid="adopt-unavailable"`). Button onclick → `onAdopt?.(row.prefill)`;
     `disabled={adoptDisabled}` + the §1 title when disabled.

6. `frontend/src/components/ChatForm.svelte`
   - New prop `prefill?: { text: string; workload: Workload; epoch: number } | null`.
   - `bind:this` on the input; an `$effect` keyed on `prefill?.epoch` sets
     `prompt = prefill.text`, `workload = prefill.workload`, focuses the input.
     (Epoch lets the same/another Adopt click re-apply after the operator edits.)

7. `frontend/src/App.svelte`
   - `let chatPrefill = $state<...>(null)`; `function handleAdopt(text: string)`
     sets `{ text, workload: 'provision', epoch: (chatPrefill?.epoch ?? 0) + 1 }`
     and scrolls the chat form into view.
   - `<InfraDiagram ... onAdopt={handleAdopt} adoptDisabled={CHAT_DISABLED_EXPR} />`,
     `<ChatForm ... prefill={chatPrefill} />` — `CHAT_DISABLED_EXPR` is the exact
     expression ChatForm's `disabled` prop already receives (busy/historical);
     factor it into one `$derived` so the two can never diverge.

## 3. What this does NOT touch

- Mermaid composition/diagram (`toMermaid` byte-identical), the preview overlay,
  RefreshScheduler, the capability card (its adoptable-types pin stays as-is),
  `propose_adoption_tool`/adopt_recipe (aliases already cover the labels),
  the approve-outcome adoption copy in main.py, denylist/static-gate/worker.
- No auto-send: the button only prefills; the human presses Send (design §6).

## 4. Tasks (TDD; commit per task)

### Task 1: `adoptable` group flag (lib + pin)
- Tests first in `tests/unit/test_infra_graph.py` (or the existing graph-builder
  test module): bucket/topic/sub/run groups → `adoptable: True`; a service-account
  group → `False`; a SENSITIVE group → `False` even if its type were adoptable;
  drift-pin: `ADOPTABLE_ASSET_TYPES == {storage.googleapis.com/Bucket,
  pubsub.googleapis.com/Topic, pubsub.googleapis.com/Subscription,
  run.googleapis.com/Service}` (catches a denylist-side adoptable-type change that
  forgets the map). Run (fail) → implement → green → commit.

### Task 2: `PlanSummary.adopt_only` (lib)
- Tests in `tests/unit/test_iac_plan_summary.py`: import-only (1 and 2 imports) →
  True; import+update / import+create / import+destroy / import+replace /
  import+forget / pure create / empty plan → False; an `importing`+`["update"]`
  row (counted as update with imported=True, NOT import) → False; a deposed
  destroy alongside an import → False; n_hidden>0 with import-only counts →
  still True AND n_hidden>0 with a mutation beyond MAX_ENTRIES → False (counts
  are full-plan — Codex test-gap list). Run (fail) → implement → green → commit.

### Task 3: approval-template adoption framing
- Extend the existing iac_approval template-render tests (find them via
  `grep -rl "change-summary" tests/`): adopt-only plan → `adopt-note` present with
  the §1 copy, generic no-destroy-note ABSENT, blast line carries "puts under
  management at most" AND the cannot-touch note; import+update plan → no adopt-note,
  existing copy unchanged; destructive plan → red warning unchanged (adopt-note
  absent). Run (fail) → edit template → green → commit.

### Task 4: frontend lib helpers
- `frontend/tests/unit/infra_graph.test.ts`: `adoptRows` ordering/filtering
  (managed skipped, sensitive groups skipped, missing `adoptable` field → row with
  `adoptable:false`), `adoptPrefill` exact strings with/without location;
  `normalizeForPrompt` — CR/LF/tab/NUL/C1 controls collapse to single spaces,
  whitespace runs collapse, 300-char label caps at 254, backticks/quotes pass
  through unchanged (no HTML escaping). Run (fail) → implement → green → commit.

### Task 5: InfraDiagram adopt list (component)
- `frontend/tests/unit/InfraDiagram.test.ts` additions: graph with one adoptable
  drift node + one non-adoptable drift node + one managed node → list renders 2
  rows (button on row 1, "not yet adoptable" on row 2, managed absent); click fires
  `onAdopt` with the exact prefill; all-managed graph → no `adopt-list`;
  `adoptDisabled` → buttons disabled, click fires nothing; hidden-unmanaged
  trailer: group with drift=5 but only 2 unmanaged nodes sampled → "+3 more",
  and group with all drift nodes shown but managed ones truncated → NO trailer
  (the truncated_in_group≠drift distinction). Run (fail) → implement → green →
  commit.

### Task 6: ChatForm prefill + App wiring (component)
- ChatForm test: render with `prefill={text:'X', workload:'provision', epoch:1}` →
  input value 'X', select value 'provision', input has focus; bump epoch with new
  text → re-applies. App-level wiring is covered by the InfraDiagram/ChatForm unit
  tests + ui-smoke (no new App test harness). Run (fail) → implement → green →
  commit.

### Task 7: full suites + lint
- `.venv/bin/pytest -q` (2606 baseline), `cd frontend && npm test`, `npm run lint`
  (or the repo's configured frontend lint/check scripts — match `frontend` CI job),
  `.venv/bin/ruff check .`. Commit any straggler fixes.

## 5. Ship + live e2e (Phase 4 exit, design §8)

1. PR → CI green → Codex completed-work review (same thread as this plan's review)
   → squash-merge.
2. **Coordinator rebake** (frontend + template + lib all bake into the coordinator
   image) + **MANDATORY pinned-traffic cutover**:
   `gcloud run services update-traffic driftscribe-agent --to-revisions=<new-rev>=100 --region=asia-northeast1`.
   No worker rebakes in this step (no iac/, no worker changes).
3. Live e2e with a FRESH hand-made bucket — **driven through the deployed button**
   (Codex must-fix 1: the e2e must exercise the shipped UI, not replay its output):
   - `gcloud storage buckets create gs://driftscribe-hack-2026-adopt-ui-probe --location=asia-northeast1` + a console-style label; wait out CAI lag.
   - `/infra/graph` shows the node unmanaged with `adoptable: true` on its group.
   - **Playwright click-through against prod** (the repo already ships
     @playwright/test for the smoke suite; a NON-committed scratch script targets
     the Cloud Run URL with `extraHTTPHeaders: {"X-DriftScribe-Token": …}`): open
     the SPA → expand Infrastructure → find the probe bucket's adopt row → click
     Adopt → assert the chat input equals the `adoptPrefill` string AND the
     workload select reads `provision` → click Send → wait for the authoring
     reply (PR link). The agent chain (propose_adoption → PR) runs from the real
     button click.
   - Approve via the proven curl + CF-JWT recipe → merge → tofu-apply +
     infra-reader rebakes at the merged SHA → re-POST resume-apply. (The
     benign-drift allowlist fix PR #97 is live, so organic bucket churn no longer
     blocks the apply.)
   - Verify: curl the approval GET and grep for `adopt-note` + the reframed
     blast line **BEFORE the approve POST** (resolved/outcome pages suppress the
     summary card — checking after would flake); CAI describe byte-identical
     pre/post; state lists the adopt address; `/infra/graph` managed count +1
     and the node flips managed after the infra-reader rebake.
4. Memory update (clickops initiative: Phase 4 shipped; rev pointers).
