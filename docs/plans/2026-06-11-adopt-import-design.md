# Adopt/import flow — design (ClickOps roadmap item 9)

**Status:** design approved by operator 2026-06-11 (interactive review, sections 1–3);
Codex review record at the bottom. No code yet — this doc is the prerequisite the
roadmap mandates ("touches the apply pipeline's gating semantics — it deserves its
own design doc + Codex review before any code").

**Audience recap** (roadmap `2026-06-10-clickops-audience-roadmap.md`): ClickOps→IaC
migrant operators. Anxiety A — "How do I migrate my hand-built infra?" — and this
item is its flagship answer: an **Adopt** button on unmanaged map nodes that brings
a hand-made resource under IaC management *without recreating or modifying it*,
via OpenTofu import blocks, through the existing author→approve→apply pipeline.

---

## 1. Decisions (locked with the operator, 2026-06-11)

| # | Decision | Choice |
|---|----------|--------|
| D1 | What "adopt" means in v1 | **Strict zero-change adopt.** Every `importing` entry's actions must be pure `no-op`, and the plan must contain **no other mutations** (unrelated `no-op`/`read` entries are normal and fine — OpenTofu lists every configured resource in `resource_changes`). If applying would change anything in the cloud, the pipeline refuses. Reconciliation afterwards is a normal update PR through the existing flow. |
| D3 | Batch size | **One adoption per PR** (v1). Enforced structurally (§4.2, §5), not by convention. Rules out partial-batch adoption state on failure (whether a given failure wrote state at all is still *proven*, never assumed — §4.4) and keeps the approval framing crisp ("adopt *this* bucket"). Batch adoption is item-10 territory. |
| D2 | v1 adoptable resource types | **The readable-today four:** `google_storage_bucket`, `google_pubsub_topic`, `google_pubsub_subscription`, `google_cloud_run_v2_service`. Exactly the types both the plan-builder WIF SA and `tofu-apply-sa` can already read, and (minus SA) exactly the types `driftscribe_lib/iac_hcl` has declared-identity templates for. **Zero IAM changes.** Everything else renders "not yet adoptable". |

Deliberately **not** chosen for v1: adopt+reconcile in one PR (muddies the
zero-change trust promise); service-account adoption (identities are the most
sensitive type and need a new read role); broad/CAI-wide scope (requires
`roles/viewer`-class grants on two deliberately least-privilege SAs).

## 2. Finding: imports are invisible to every gate today

The roadmap assumed the gates would need *loosening* to admit imports ("the apply
worker's resource_set_guard and denylist must admit it deliberately"). Grounding
showed the opposite — **they admit imports silently, right now**:

- **Static gate** (`tools/iac_static_gate.py`): only the *path* `iac/imports.tf`
  is operator-protected (`PROTECTED_FOUNDATION`). An `import {}` block inside any
  other agent-authored `.tf` has **no content rule** — `evaluate()` inspects
  `provider`/`module`/`resource`/`data` blocks only. Passes clean.
- **Denylist** (`driftscribe_lib/iac_plan_denylist.py`): a pure import plans as
  `actions=["no-op"]` + `change.importing`. `("no-op",)` skips the v1 floors AND
  skips every identity check (`_is_mutation` is `actions not in
  NO_OP_ACTION_TUPLES` → False). The `importing` field is never inspected. Even
  **importing DriftScribe's own control-plane resources into agent-managed state
  passes with zero violations.**
- **Routing** (`driftscribe_lib/iac_plan_classify.plan_has_create`): ignores
  `importing` → import-only plans route down the *lenient* C5 update path (apply
  from the PR artifact, **no merge-first**). Actively dangerous for imports: state
  gains an address whose config never landed on `main`, so the next plan from
  `main` proposes **deleting the freshly-adopted resource**.
- **`resource_set_guard`** (`workers/tofu_apply/tofu_runner.py:225`): `continue`s
  on `["no-op"]` entries. Silent pass.

The only things keeping this theoretical today are convention (the authoring
prompt doesn't write import blocks) and human PR review. **Phase 1 of this design
closes the hole as a safety fix in its own right, independent of the adopt
feature** — imports become visible and denied-by-default, then Phases 2–4
re-admit them deliberately through the adopt path.

## 3. OpenTofu grounding (verified against opentofu.org/docs/language/import, 1.12.x)

- **Import blocks go inert after success.** "An `import` block is active only if
  OpenTofu is not already tracking an object with the address given in `to`.
  After importing is successful, an `import` block becomes inert" — and may be
  retained "as a historical record for future maintainers." → Retention is a
  **product choice this design makes** (the docs permit either): adopt PRs
  leave the import block in place permanently as an audit record, avoiding a
  cleanup-PR lifecycle.
- **State mutation happens at apply, not plan.** "Once a plan is approved,
  OpenTofu imports the object into its state during the subsequent apply stage."
  → The saved-plan C4 apply model carries imports unchanged; the plan artifact is
  a *preview* of the import exactly as it is for creates/updates.
- **`-generate-config-out` is NOT used.** The agent authors the resource block
  explicitly from the `iac_hcl` identity templates + live CAI describe — config
  generation is marked unstable in the docs, unsupported with `for_each`, and
  would bypass the template-grounded authoring discipline.
- **Import `id` normalization** (existing repo constraint, not OpenTofu's):
  `iac_hcl.extract_declared_identities` already consumes `import` blocks as
  high-confidence declared identities, taking `id` **verbatim**. The id must be
  written in the CAI-normalized form (`bare-bucket-name`,
  `projects/<P>/topics/<N>`, `projects/<P>/subscriptions/<N>`,
  `projects/<P>/locations/<L>/services/<N>`) or the map shows the resource as
  format-mismatch false-drift. The adopt recipe MUST emit ids in exactly these
  forms; the static gate enforces shape (§5).
- The resolver's standing comment "imports.tf is operator-only foundation …
  agents cannot add imports" becomes stale under this design and is updated in
  Phase 2.

## 4. Gating semantics (the heart — approved §1)

Encoded **once** in the shared denylist, which both the coordinator (at approval
render/POST) and the apply worker (on its independently fetched plan) re-run —
no new trust boundary anywhere.

### 4.1 Identity checks now run on importing entries

`evaluate()` treats an entry with `change.importing` present as
identity-checkable even when its actions are `["no-op"]` (today `_is_mutation`
short-circuits them out). Importing any control-plane identity —
services, SAs, state/artifact buckets, secrets, KMS, WIF — fires the existing
`control-plane-*` / `wif-config-change` rules. Adopting DriftScribe into
DriftScribe stays impossible.

### 4.2 New denylist rules (v1 floors + adopt admission, all fail-closed)

Phase 1 ships a single blanket **`import-forbidden-v1`** (every `importing`
entry denied). Phase 2 *replaces* it with the conditional rules below, in the
same PR that adds the static-gate authoring controls (§7):

- **`import-with-changes-forbidden-v1`** — an `importing` entry whose actions are
  not exactly `("no-op",)`. This *is* D1, enforced at the plan level: if the
  authored HCL doesn't byte-match live reality, the plan shows
  `importing`+update and is refused with a message telling the agent to
  regenerate. The zero-change guarantee is enforced, never assumed.
- **`import-type-not-adoptable-v1`** — an `importing` entry whose resource type is
  outside the D2 four-type allowlist (new lib constant, e.g.
  `ADOPTABLE_RESOURCE_TYPES`, exported for the UI/capability card).
- **`import-mixed-plan-forbidden-v1`** — a plan containing both `importing`
  entries AND any other mutation (create/update/delete/replace/forget on
  non-importing entries). Unrelated `no-op`/`read` entries do NOT trip this —
  OpenTofu lists every configured resource in `resource_changes`, so they are
  always present. The invariant is "no other *mutations*", and the fixture
  matrix pins exactly that. This keeps the approval page's "nothing will be
  modified" claim plan-wide, not per-resource.
- **`import-batch-forbidden-v1`** — more than one `importing` entry in a plan
  (D3). Enforced at the plan level so the worker re-verifies it independently
  of the static gate.
- A malformed `importing` value (non-dict) folds into the existing
  `plan-json-malformed-change` fail-closed rule. A protected-type importing
  entry whose `before`/`after` both lack the expected identity fields is
  already covered by the existing "protected resource with no identity"
  fail-closed behavior — sparse import rows fail closed, never open.

`RULE_DESCRIPTIONS` gains plain-language entries for each (the AST-pinned
set-equality test in `tests/unit/test_denylist_rule_descriptions.py` forces
this); the item-8 `BLAST_CANNOT_TOUCH_NOTE` drift pin (exact 14-rule-ID set)
is updated to the new set in the same PR.

### 4.3 Routing: imports are create-class

`plan_has_create` (consumed by `agent/iac_artifacts.py` for routing and the
worker for gate selection) learns: an entry with `importing` present counts as
create-class. Adopt PRs therefore take the strict C6 **merge-first** flow —
config lands on `main`, the `iac/`-tree hash gate proves the baked config equals
the reviewed head, *then* apply runs from `main`. This kills the
state-without-config failure mode in §2 structurally. Fail-closed behavior of
`plan_has_create` (malformed ⇒ True ⇒ stricter path) is unchanged.

**Import-aware C6 lifecycle copy.** The C6 coordinator copy is create-specific
today and would be *wrong* for imports: the post-merge outcome text says "This
plan CREATES a resource, so the worker must be RE-BAKED…" (`agent/main.py`
~3644) and the failure/recovery copy warns "a created resource may exist out
of state — run the apply-failure recovery runbook (orphan check)"
(~3761). For an adoption neither claim holds — nothing is created, and the
orphan-check framing misleads. The copy branches on plan class (create vs
import) in the same phase that admits imports (§7 Phase 2), with import-honest
text: post-merge → "this plan ADOPTS an existing resource…"; failure → see
§4.4.

### 4.4 Failure story (post-merge, import-specific)

The worker's **freshness gate is `tofu plan -refresh-only`** — it refreshes
*state-tracked* objects only, so it cannot see the import target at all (the
target isn't in state yet). Consequences, stated honestly:

- **Target deleted in the console between approval and apply:** caught only at
  saved-plan apply, when OpenTofu errors on the non-existent remote object.
  The apply fails, the existing apply-failure alert fires, and the approval
  page shows the terminal failure. D3 rules out partial-*batch* state; whether
  the failure wrote state at all follows the worker's **existing suspicion
  model** — failed-apply state is suspect unless the serial/lineage is proven
  unchanged (the `failed` vs `failed_state_suspect` distinction in
  `driftscribe_lib/approvals.py` / the worker handler). Imports keep that
  model untouched; an import failure is *expected* to fail before any state
  write, and the worker proves it rather than assuming it.
- **Read-permission failure at import time:** same path — apply fails, alert +
  terminal failure, state suspicion model applies. (Shouldn't occur within the
  D2 scope, whose types are chosen *because* both SAs can read them; this is
  the belt-and-braces story for when it does.)
- **Retry/recovery:** the C6 resume path may retry ONLY when the worker can
  prove state clean/serial-fresh; otherwise the outcome is terminal
  `failed_state_suspect` and recovery is operator-led via the existing
  apply-failure runbook. If the operator abandons the adoption instead, the
  recovery is a revert PR removing the resource + import block — after which
  the map returns the node to unmanaged (§6).

### 4.5 `resource_set_guard`: `allow_import_of_declared`

Mirrors the existing `allow_create_of_declared` coupling: an `importing`+no-op
entry is admitted ONLY when (i) the flag is set — which the worker does only
after the tree-hash proof — AND (ii) the target address is in the baked declared
set. A leftover-inert import block on a later, unrelated plan stays a plain
no-op entry (no `importing` in that plan's resource_changes) and is untouched.

## 5. Authoring (approved §2)

**Placement — co-located.** The `import` block sits next to the `resource` block
it adopts, in agent-authored `.tf` files. `iac/imports.tf` stays operator-only
and untouched; the foundation-file protection is NOT loosened.

**Static gate: new AGENT-mode content rules for `import` blocks** (today it has
no opinion — part of the §2 hole). The gate receives **only the PR's changed
files** (`iac_static_gate.py` CLI, `_git_diff_names`), which forces an explicit
scope decision:

> **Invariant: the import block and its target `resource` block must both be in
> the PR's changed files.** An adopt PR always carries the pair together (the
> recipe authors them co-located, §5 recipe). An import block targeting a
> resource that exists only on `main` is **refused in v1** — a deliberate
> false-negative: it costs nothing for the recipe-authored flow and avoids
> giving the gate a whole-head view it doesn't have today (which would also
> falsify "no importing onto config the PR doesn't carry").

- `import-target-undeclared` — the `to` address must reference a `resource`
  block declared in the gate's supplied (changed) HCL files, per the invariant
  above.
- `import-type-not-adoptable` — `to`'s resource type must be in
  `ADOPTABLE_RESOURCE_TYPES` (defense-in-depth with the denylist's plan-level
  rule; the static gate catches it pre-plan, cheaper and earlier).
- `import-id-not-literal` — `id` must be a plain literal string matching the
  CAI-normalized shape for the type (§3); no interpolation/expressions — the
  import target stays reviewable text. The OpenTofu-1.12 `identity` attribute
  (object-form alternative to `id`) is **rejected** in AGENT mode in v1 —
  `iac_hcl.extract_declared_identities` only consumes `id`, so an
  `identity`-form import would silently break declared-identity resolution.
- `import-target-indexed` — `to` must be a plain `type.name` address: no
  `[0]`/`["key"]` index, and the target resource block must not use
  `count`/`for_each`. Indexed targets break the id-literal reviewability rule
  and the one-name-one-resource adoption story; v1 bans them outright.
- `import-foreach-forbidden` — no `for_each` on import blocks themselves (keeps
  them statically analyzable; config-gen doesn't support them either).
- `import-batch-forbidden` — at most one `import` block across the PR's changed
  files (D3; mirrored at plan level by `import-batch-forbidden-v1`).
- OPERATOR mode behavior is unchanged (operators keep full import freedom in
  foundation files).

`ADOPTABLE_RESOURCE_TYPES` is a **new, separate lib constant** — deliberately
NOT derived from `iac_hcl`'s supported-template types, which include
`google_service_account` (excluded by D2). A drift pin asserts the allowlist is
a strict subset of the template types (every adoptable type must be
authorable), without importing the exclusion decision.

**The adopt recipe (agent side)** reuses existing machinery end-to-end: read the
live resource via the infra-reader's CAI-grounded `/describe` → render the
resource block from the Phase-2 declared-identity templates in `iac_hcl` →
append the co-located `import` block with the normalized id → open the PR
through the **unchanged** tofu-editor (same `infra/` branch rules, same
`tofu fmt`, same PR ceremony, same item-7 pending-approval notification via
`open_infra_pr_tool`). If authored HCL drifts from live, the C2 plan shows
`importing`+update and the denylist refuses with a regenerate hint (§4.2).

## 6. Operator experience (approved §3)

- **Map:** Adopt button on unmanaged nodes in `InfraDiagram.svelte` → opens chat
  prefilled ("Adopt the Cloud Storage bucket `my-old-uploads` into IaC
  management"). Non-adoptable types render a "not yet adoptable" affordance
  instead of the button.
- **Approval page:** adoption framing. The summary lib already speaks the
  `import` verb (Wave 1 item 1 groundwork); the destructive-banner slot shows a
  calm "Nothing in your infrastructure will be modified — this only puts N
  resource(s) under management"; the item-8 blast-radius line reframes to "puts
  1 Cloud Storage bucket under management — modifies nothing".
- **Ghost preview (item 6)** already renders `importing` entries as "will be
  imported" ghosts — zero work.
- **Notifications (item 7)** fire automatically on adopt PRs — zero work.
- **Coverage meter:** ticks on the next graph refresh **after merge** (managed
  coloring derives from *declared* identities — `infra_inventory` is
  declaration-based — and the import block is a high-confidence declared
  identity the moment it lands on `main`), i.e. slightly before apply
  completes. In the happy path this is cosmetic (merge-first means apply
  follows immediately). The dishonest case is **apply failure after merge**:
  the map then shows managed-green for a resource whose import never landed,
  and that persists until recovery. Mitigations, not hand-waving: the apply
  failure alerts (item 7) and renders as a terminal failure on the approval
  page; D3 rules out half-adopted batch state; and the §4.4 recovery paths
  (retry via C6 resume when state is proven clean, or revert PR → node returns
  to unmanaged) both restore truth. A declared-but-unapplied visual state on the
  map is explicitly **deferred** (it would equally apply to creates today and
  belongs to a general "pending apply" treatment, not to this item).
- **Pause button (item 5)** gates `/apply` already → pausing blocks adoptions
  too, for free.

**Autonomy-dial interplay (roadmap item 11, not yet built):** adopt introduces
no new mutation tool — it rides `open_infra_pr` + the existing apply pipeline.
Whatever Layer-0 registry gating the dial later applies to those tools gates
adoption identically. No special-casing now or later.

## 7. Phasing — four PRs, each shippable alone

The phase boundary is **deny-everything first, admit later** — at no point
between phases does an unadmitted import path exist (Codex must-fix #1: the
original draft admitted allowlisted imports at the denylist before the static
gate had any import rules, leaving a window where a manually-authored import
block would sail through).

1. **Safety floor — blanket deny** (lib + worker, no feature): a single
   **`import-forbidden-v1`** denylist floor (every `importing` entry denied,
   period) + identity checks running on importing entries + create-class
   routing (§4.3) + `resource_set_guard` refusing importing entries. Ships on
   its own merits: closes the §2 silent-pass hole completely. No admission of
   anything.
2. **Admission — gate and denylist together, one PR**: the blanket
   `import-forbidden-v1` is *replaced* by the conditional §4.2 rules
   (with-changes / type / mixed / batch) **in the same PR** that adds the §5
   static-gate import rules, the `resource_set_guard`
   `allow_import_of_declared` flag, and the import-aware C6 lifecycle copy
   (§4.3) — so admission and authoring controls are never live separately.
   Also: stale-comment update in `iac_hcl`; capability card gains the
   adoptable-types list. (RULE_DESCRIPTIONS and the item-8 blast-note pin
   update in both phases — twice, deliberately.)
3. **Agent recipe**: adopt prompt/tool grounded in CAI describe + `iac_hcl`
   templates, one resource per PR (D3); honest failure copy when live config
   can't be mirrored (runtime-valued attrs → "this resource can't be cleanly
   adopted yet").
4. **UI + approval framing**: Adopt button, prefilled chat, approval-page
   adoption copy, blast-radius reframe, meter behavior note.

Each phase: grounded spec → Codex plan review → subagent TDD → Opus final review
→ PR → CI → Codex completed-work → merge + redeploy + live verify (the
established item workflow).

## 8. Testing strategy

- Same drift-pin discipline as items 1–8: exact rule-ID set pins (denylist
  RULE_DESCRIPTIONS AST pin; static-gate rule list; **and the capability-card
  pins that mirror RULE_DESCRIPTIONS** — frontend tests that enumerate rule
  categories must be updated in the same PRs), coordinator/worker parity tests
  asserting `evaluate()` verdicts match on identical plan JSON.
- **Real import-shaped fixture, not hand-written:** per the OpenTofu JSON
  format, `importing` is additive to `actions`, but exactly which identity
  fields populate `before`/`after` on an import row is provider behavior, not
  spec. Phase 1 generates a REAL `plan.json` from a live import against the
  e2e project (one bucket) and pins the fixture from it — the identity-check
  claim in §4.1 is verified against that artifact, not assumed. Sparse rows
  fail closed (§4.2).
- Fixture matrix for §4: pure import (admitted iff allowlisted + declared +
  flag), importing+update (refused), mixed plan with another mutation
  (refused), pure import alongside unrelated no-op entries (**admitted** — the
  D1-wording regression test), two importing entries (refused, D3), import of
  each control-plane identity class (refused), malformed `importing`
  (fail-closed), inert-import-later-plan (untouched), non-allowlisted type
  (refused).
- Routing: `plan_has_create` truth-table extension incl. importing+no-op ⇒ True.
- **Live e2e on prod (Phase 4 exit):** hand-make a test bucket in the console,
  adopt it through the full loop, verify (a) CAI describe before/after apply is
  identical (zero cloud mutation), (b) state contains the address, (c) the map
  node flips managed and the meter increments, (d) the approval page showed the
  zero-change framing.

## 9. Risks / notes

- **GCS-backend lock nuance:** `tofu plan` with the GCS backend writes a lock
  object, so "plan-only" is not credential-read-only — already true for the C2
  pipeline today; imports change nothing here.
- **Adoption fidelity:** some live resources carry runtime-valued or
  server-defaulted attributes the templates can't mirror exactly; the plan then
  shows `importing`+update and is refused. This is the *correct* v1 behavior
  (the agent reports "can't cleanly adopt yet") but will limit which real-world
  resources adopt cleanly — measured in the Phase-3 spec with per-type fixture
  probes before promising the button works on everything.
- **Roadmap 9b question answered:** "import is create-class for routing?" —
  **yes**, decided §4.3, with the §2 delete-proposal failure mode as the
  forcing argument.

---

## Plan-review record (Codex)

Thread `019eb41f-2e72-73b0-a404-08f8c2fe28a5`. Round 1: **NO-GO**, 4 must-fix +
5 important + 2 nits — all verified against the code and folded:

1. **Phase boundary left an admission window** (denylist would admit
   allowlisted imports before any static-gate authoring rule existed) → §7
   re-phased: Phase 1 = blanket `import-forbidden-v1` deny; Phase 2 = admission
   + authoring controls in ONE PR.
2. **C6 lifecycle copy is create-specific and wrong for imports**
   ("CREATES a resource… RE-BAKED", "created resource may exist out of state —
   orphan check"), and the freshness gate is `-refresh-only` (cannot see the
   untracked import target) → §4.3 import-aware copy + new §4.4 failure story
   (deleted target / permission failure caught only at apply; D3 single-import
   rule added; retry + revert recovery).
3. **"ONLY importing entries" wording was wrong** (unrelated no-op entries are
   always present in plan JSON) → D1 reworded to "no other mutations"; explicit
   admitted-fixture in §8.
4. **Static-gate target-declaration scope was ambiguous** (gate sees changed
   files only) → §5 invariant pinned: import + target resource must both be in
   the PR's changed files; importing onto main-only config refused in v1,
   documented as a deliberate false-negative.

Importants folded: `identity`-form import attribute rejected in AGENT mode
(resolver only consumes `id`); real provider-generated import fixture required
before trusting §4.1 identity-check claims (sparse rows fail closed); indexed
(`count`/`for_each`) import targets banned (`import-target-indexed`);
meter-ticks-at-merge acknowledged as more than cosmetic on apply failure (§6
mitigations + deferred "pending apply" map treatment);
`ADOPTABLE_RESOURCE_TYPES` kept separate from `iac_hcl` template types (SA
excluded) with a subset drift pin. Nits folded: capability-card pins mirroring
RULE_DESCRIPTIONS called out in §8; import-block retention reworded as a
product choice.

Round 2: **GO**, no must-fix. One Important folded: the draft overstated
"state untouched" on failed imports — softened to the worker's existing
suspicion model (failed-apply state is suspect unless serial/lineage is proven
unchanged; retry via C6 resume only when proven clean, else terminal
`failed_state_suspect`; D3 rules out partial-*batch* state specifically, §4.4 /
D3 / §6). Typo fixed (`failed_state` was not a real status token). Nit fixed
(§4.4/§4.5 ordering — failure story is now §4.4, `resource_set_guard` §4.5).
