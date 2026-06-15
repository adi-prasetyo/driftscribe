# Suppress Google-service-managed buckets from adoption — design

**Date:** 2026-06-15
**Status:** approved (brainstorm), pending implementation
**Branch:** `feat/service-managed-bucket-suppress`

## Problem

The homepage Tour's "Prefill the request" button (and the Infrastructure
panel's per-row Adopt buttons) pick the rank-1 unmanaged, non-control-plane,
named resource as the first-adoption suggestion. Clicking it live prefilled:

```
Adopt the Storage bucket `driftscribe-hack-2026_cloudbuild` in us into IaC management.
```

`driftscribe-hack-2026_cloudbuild` is GCP's **auto-created Cloud Build staging
bucket** — created and owned by a Google service, not by us. It is a poor
"build confidence" first adoption, and worse: it matches **neither**
`-tofu-state` **nor** `-tofu-artifacts`, so it is **not denylist-protected**.
Sending that request would have the agent author a real zero-change import PR
for a bucket the operator does not actually control.

This is the **same gap class** the `-tofu-artifacts` papercut already closed for
DriftScribe's own buckets — but for the disjoint category of buckets that *other
Google services* auto-manage. Today only `_cloudbuild` exists live; App Engine,
Cloud Functions, and Cloud Run source deploys create more on the same pattern.

## Decisions (locked in brainstorm)

1. **Separate category, shared CTA flag.** A new, honest denylist reason
   (`service-managed-bucket`) — distinct from `control-plane-bucket`, enforced
   identically (deny change **and** import). For CTA suppression the infra
   graph **reuses the existing `control_plane` node flag** — no new frontend
   signal/plumbing. One per-row UI note is broadened to honestly cover both
   kinds of untouchable infra.
2. **Bounded well-known pattern set** (not a heuristic): suffixes `_cloudbuild`,
   `.appspot.com` (App Engine default/staging **and** legacy GCR backing
   buckets); prefixes `gcf-sources-` (Cloud Functions gen1), `gcf-v2-sources-`
   + `gcf-v2-uploads-` (Cloud Functions gen2 — **tightened from `gcf-v2-` per
   Codex** so an operator bucket like `gcf-v2-assets` is not wrongly blocked),
   `run-sources-` (Cloud Run source deploys). Requires a prefix **and** suffix
   matcher; today only `_cloudbuild` is live, the rest future-proof.
3. **New capability-card category `service-managed`** (heading: "Google-managed
   infrastructure stays managed by Google" or similar). The `control-plane`
   category is labeled "DriftScribe's own infrastructure" — inaccurate for a
   Google bucket, so a separate category is the honest grouping.
4. **Broaden the agent prompt note (revised — now IN scope per Codex).**
   Originally cut as YAGNI ("just priming"), but Codex showed the consequence is
   real: the raw inventory tool shows `_cloudbuild` to chat and both prompts say
   "adopt buckets first" while only excluding DriftScribe's own control-plane
   buckets — so the agent could still verbally recommend adopting `_cloudbuild`
   (then self-reject at the tool). Control-plane buckets are already handled at
   the prompt level; service-managed would be the odd one out. So broaden
   `ADOPTION_CONTROL_PLANE_NOTE` (keep the constant name) + the two
   `workloads/{provision,explore}/system_prompt.md` files; the pin test
   whitespace-normalizes, so it auto-covers.
5. **YAGNI cut (kept):** **bucket-only** rule — no `google_storage_bucket_object`
   "object-inside" case (that exists for `-tofu-*` to stop state/artifact
   smuggling; a Google staging bucket carries no such trust dependency).

## The matcher — single source of truth

In `driftscribe_lib/iac_plan_denylist.py` (canonical home of
`CONTROL_PLANE_BUCKET_SUFFIXES`), add two sibling constants and one public
predicate:

```python
SERVICE_MANAGED_BUCKET_SUFFIXES: tuple[str, ...] = ("_cloudbuild", ".appspot.com")
SERVICE_MANAGED_BUCKET_PREFIXES: tuple[str, ...] = (
    "gcf-sources-",       # Cloud Functions gen1 source
    "gcf-v2-sources-",    # Cloud Functions gen2 source
    "gcf-v2-uploads-",    # Cloud Functions gen2 upload staging
    "run-sources-",       # Cloud Run source deploys
)

def is_service_managed_bucket_name(name: object) -> bool:
    """True iff ``name`` is a bucket auto-created by a Google service."""
    return isinstance(name, str) and (
        name.endswith(SERVICE_MANAGED_BUCKET_SUFFIXES)
        or name.startswith(SERVICE_MANAGED_BUCKET_PREFIXES)
    )
```

Kept **out of `__all__`** (consistent with `CONTROL_PLANE_BUCKET_SUFFIXES` /
`_is_protected_bucket_name`, which are cross-module-used but not in the curated
canonical `__all__`); the two constants are re-exported from the `tools/` shim
(the constants-shape test imports them from there).

The match is **multi-dimensional** (prefix **or** suffix), unlike the trivial
control-plane `endswith`, so it is worth centralizing in **one** predicate that
every surface imports — strongest parity-by-construction. `str.endswith` /
`str.startswith` accept a tuple natively.

**Parity invariant (preserved):** a node flagged `control_plane: true` ⟺ the
denylist refuses that same identity. Both the infra-graph matcher and the
denylist check call the *same* predicate, so they cannot drift; the existing
parity test drives `build_graph` + `evaluate` on one identity and asserts they
agree. Note (per Codex): the denylist matches `change.before/after["name"]`
while the graph matches the CAI-derived **bare bucket label** (last segment of
the CAI resource name, via `build_inventory()`); for buckets these are the same
string, so parity holds — and a new test will flow a full CAI resource name
through `build_inventory()` → `build_graph()` to pin the normalization. **Safe failure directions:** if a name slips the matcher, the worst case
is a button that the plan-builder denylist then blocks (UX miss, not a wrong
allow); the adopt_recipe rejection is a UX nicety whose miss is equally fail-safe
(the plan is still blocked downstream).

## Surfaces (exact edits)

### 1. Denylist enforcement — `driftscribe_lib/iac_plan_denylist.py`
- Add the two constants + `is_service_managed_bucket_name` (near
  `_is_protected_bucket_name`, ~L461).
- Add `_check_service_managed_bucket(rc, rtype, actions, before, after,
  violations)` mirroring `_check_control_plane_bucket`'s **bucket branch only**
  (`rtype == "google_storage_bucket"`, identity field `name`, malformed-guard,
  then `Violation("service-managed-bucket", ...)`). No object branch.
- Dispatch: add the call immediately after `_check_control_plane_bucket` in the
  `if _is_mutation(actions) or importing is not None:` block (~L800) — so it
  fires on both a change to and a (no-op) import of a service bucket.
- `RULE_DESCRIPTIONS`: add `"service-managed-bucket"` with an operator-grade
  description (≥20 chars, capitalized, no "tuple"), e.g. *"No change may adopt
  or modify a bucket that a Google service auto-manages — Cloud Build, App
  Engine, Cloud Functions, or Cloud Run source-deploy staging buckets."*
- Module docstring: bump "Rule IDs (18)" → 19 and add the new bullet.

### 2. `tools/iac_plan_denylist.py` shim
- Add `SERVICE_MANAGED_BUCKET_SUFFIXES` + `SERVICE_MANAGED_BUCKET_PREFIXES` to
  the explicit re-export import block (`test_constants_are_frozensets_or_tuples`
  imports constants from `tools.iac_plan_denylist`).

### 3. Reason → category — `agent/capabilities.py`
- `CATEGORY_ORDER`: insert `"service-managed"` right after `"control-plane"`
  (groups the two identity-based "untouchable infra" categories).
- `RULE_CATEGORIES`: add `"service-managed-bucket": "service-managed"`.

### 4. Capability-card label — `frontend/src/lib/capabilities.ts`
- Add `'service-managed'` to the `CATEGORY_HEADINGS` Record **type union** and
  object literal with a human label. (Closed Record — without this the category
  renders under its raw key. Safe but unlabeled, so this is required.)

### 5. Infra-graph node flag — `driftscribe_lib/infra_graph.py`
- Import `is_service_managed_bucket_name`.
- Extend the existing `storage.googleapis.com/Bucket` matcher lambda in
  `_CONTROL_PLANE_NODE_MATCHERS` to `name.endswith(CONTROL_PLANE_BUCKET_SUFFIXES)
  or is_service_managed_bucket_name(name)`. Same asset-type key →
  `test_matchers_cover_only_adoptable_types` (exact-key-set pin) stays green.
- `_is_control_plane_node`, the `node["control_plane"] = True` setter, and the
  group-level `adoptable` flag are unchanged (the flag propagates downstream).
- Broaden the `InfraNode`-equivalent JSDoc note only; **leave
  `ADOPTION_CONTROL_PLANE_NOTE` untouched** (YAGNI cut 4a).

### 6. Tool boundary — `driftscribe_lib/adopt_recipe.py`
- Import `is_service_managed_bucket_name`.
- Add a third branch to `_reject_control_plane` for
  `resource_type == "google_storage_bucket" and is_service_managed_bucket_name(name)`
  raising `AdoptRecipeError` with an **honest** message: framed around
  *auto-**created** by a Google service* (Cloud Build, App Engine, Cloud
  Functions, or Cloud Run source deploys), **not** "DriftScribe control plane"
  and **not** overclaiming "Google manages every byte" (per Codex #2 — the
  default `<project>.appspot.com` bucket is app-usable). Ends with
  `FINAL_REFUSAL_MARKER`; keeps the `cannot be adopted` + `denylist` tokens the
  existing tests assert.

### 7. Agent prompt note — `driftscribe_lib/infra_graph.py` + `workloads/`
- Broaden `ADOPTION_CONTROL_PLANE_NOTE` (keep the constant name) to also name
  buckets a Google service auto-creates, and mirror the broadened text into
  `workloads/provision/system_prompt.md` and `workloads/explore/system_prompt.md`
  (hard-wrapped is fine — `test_adoption_order_prompts.py` whitespace-normalizes).

### 8. Operator copy (honesty broadening)
- `frontend/src/components/InfraDiagram.svelte` per-row note (~L582) and
  `frontend/src/lib/tour.ts` all-suppressed fallback (~L199-208): broaden to
  cover both kinds, keeping the `control-plane` and `denylist` tokens so existing
  assertions stay green while adding the Google-service-managed clause, e.g.
  *"…system-managed infrastructure — DriftScribe's own control-plane services
  and IaC state/artifact buckets, or a bucket a Google service auto-manages —
  which the always-on denylist blocks from changes, adoption included."*
- `frontend/src/lib/infra_graph.ts` `InfraNode.control_plane` JSDoc: broaden to
  mention service-managed buckets (comment only).

## Test plan

**Python**
- `test_iac_plan_denylist.py`: new fixtures (`service_managed_bucket_change.json`,
  `import_service_managed_bucket.json`) + a new `test_service_managed_bucket_*`
  asserting `service-managed-bucket` fires on change **and** import; a **near-miss**
  test (`driftscribe-hack-2026-assets` does **not** fire); add the new constants
  to `test_constants_are_frozensets_or_tuples`; add an import row to the
  import-identity parametrize.
- `test_denylist_rule_descriptions.py`: bump count 18 → 19 (AST scan + operator-
  grade test auto-cover the new entry).
- `test_iac_plan_summary.py`: add `"service-managed-bucket"` to the hard-coded
  key-set drift pin.
- `test_capabilities.py`: bump rule-count pin 18 → 19 (set-equality + sort pins
  auto-pass once source constants update).
- `test_infra_graph.py` **parity test**: add a True row (service-managed name)
  and a False near-miss to `test_flag_parity_with_denylist_import_admission`;
  add a fixture + row to `test_flag_parity_on_real_import_fixtures` **and broaden
  its `blocked` predicate** from `v.rule.startswith("control-plane-")` to also
  count `v.rule == "service-managed-bucket"` (else the parity assertion misfires);
  add a `test_service_managed_bucket_node_is_flagged` unit test; add a
  CAI-normalization test that flows a full CAI resource name through
  `build_inventory()` → `build_graph()` and asserts the flag (per Codex #3).
- `test_adopt_recipe.py`: add `test_service_managed_bucket_is_rejected` to
  `TestControlPlaneRefusal`.
- `test_adoption_order_prompts.py`: **auto-covers** the broadened
  `ADOPTION_CONTROL_PLANE_NOTE` once the constant and both `system_prompt.md`
  files are updated (it whitespace-normalizes and pins constant ⊆ prompt).

**Frontend (vitest)**
- `InfraDiagram.test.ts` + `tour.test.ts`: keep existing assertions green
  (copy retains `control-plane`/`denylist`); **add** a service-managed-named
  `control_plane: true` fixture row proving the CTA is suppressed and the note
  shows; add an assertion for the broadened clause.
- `capabilities.test.ts`: optionally add a heading assertion for the new
  `service-managed` category (no existing test breaks).

## Verification after deploy
- Reload the Tour → "Prefill the request" suggests a real adoptable bucket
  (`…-assets` or a probe bucket), **never** `_cloudbuild`.
- Infrastructure panel shows `_cloudbuild` muted with the broadened note.
- `/capabilities` card shows the new `service-managed` category with its label.

## Out of scope
- Object-inside-bucket enforcement for service buckets (YAGNI cut 5 — no
  smuggling threat).
- Per-row distinct copy for the two reasons (shared-flag decision — one
  unified honest note).

## Accepted tradeoffs & known limitations (adversarial review)
- **Bounded-set false positives (accepted).** `endswith("_cloudbuild")` and
  `startswith("run-sources-")` would also match a deliberately-named operator
  bucket (`my-data_cloudbuild`, `run-sources-staging`) and block its adoption.
  This is the SAME tradeoff already accepted for the control-plane suffixes
  (an operator `acme-tofu-state` is likewise blocked). Probability is very low
  (these are unusual, self-inflicted names), and tightening — e.g. requiring a
  project-id shape or trailing digits — would need brittle per-pattern regex
  that breaks the simple, auditable tuple design and risks false NEGATIVES.
  Kept as-is by design.
- **Cloud Composer false negative (known limitation).** Composer's
  auto-created environment bucket is `<region>-<env>-<uuid>-bucket` — the
  embedded UUID is unfit for a prefix/suffix match, so the bounded set cannot
  catch it. It is NOT in DriftScribe's own estate (no Composer), so it is moot
  for this deployment; catching UUID-named service buckets in general would
  need a different signal (CAI labels / owning-service metadata), out of scope
  for this bounded-name approach.

## Codex plan review (thread 019eca9c)
Adopted: tighten `gcf-v2-` → `gcf-v2-sources-`/`gcf-v2-uploads-` (#1);
"auto-created" copy framing, not "Google manages every byte" (#2); parity
wording + CAI-normalization test (#3); prompt-note broadening promoted back into
scope (#4). Partially disagreed: keep the predicate out of `__all__` (#5) —
consistent with the existing constants/predicate convention + the exact-equality
`__all__` pin; constants re-exported from the `tools/` shim instead. Confirmed:
separate rule (not widening) is the honest choice; no other enforcement-bypass
mutation path (adk_tools freehand-import block, fanout, render_adoption funnel).
