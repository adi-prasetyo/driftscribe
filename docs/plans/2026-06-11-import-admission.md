# Import admission (adopt design Phase 2) — implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or
> subagent-driven execution) to implement this plan task-by-task.

**Goal:** Replace the Phase-1 blanket `import-forbidden-v1` with the design
§4.2 conditional admission rules, in the SAME PR that adds the §5 static-gate
authoring rules, the `resource_set_guard` `allow_import_of_declared` flag, and
the import-aware C6 lifecycle copy — so admission and authoring controls are
never live separately (the §7 phase boundary, Codex must-fix #1 on the design).

**Architecture:** All plan-level policy stays in the shared
`driftscribe_lib/iac_plan_denylist.py` (coordinator + worker re-run it
unchanged). Authoring policy is new AGENT-mode content rules in
`tools/iac_static_gate.py` over the PR's changed files only. The worker's
`resource_set_guard` gains an import admission flag coupled to the existing
C6 tree-hash proof. Routing (`plan_has_create` ⇒ imports are create-class)
shipped in Phase 1 and is untouched.

**Tech stack:** Python (stdlib + hcl2 via `driftscribe_lib.iac_hcl`), pytest;
one small Svelte/TS touch (capability card adoptable-types line) + vitest.

**Parent design:** `docs/plans/2026-06-11-adopt-import-design.md` §4.2, §4.3
(copy), §4.5, §5, §7 (Phase 2), §8. Phase 1 shipped as PR #91 → `1e35164`
(plan: `docs/plans/2026-06-11-import-safety-floor.md`).

**Status: COMPLETE** — Tasks 1–8 all done on branch `feat/import-admission`
(2026-06-11). 2487 pytest green, ruff clean, 424 vitest green, build clean.
Commits: bff8b5f (T1) → 8a16cdc (T2) → 831bf5d (T3) → 5f6ec9d (T4) →
e2f94c8 (T5) → 5b9f4bb (T6) → 1bc6558 (T7) → pending (T8).
Awaiting: Codex completed-work review, PR open, CI green, squash-merge,
THREE rebakes (coordinator + tofu-apply + tofu-editor).

---

## 0. Grounded facts (verified against main @ `1e35164`, 2026-06-11)

- **Denylist** (`driftscribe_lib/iac_plan_denylist.py`): blanket
  `import-forbidden-v1` fires for any non-null `change.importing`, placed
  BEFORE the unknown-action `continue`; non-dict `importing` additionally
  emits `plan-json-malformed-change`; identity gate is
  `_is_mutation(actions) or importing is not None`. `RULE_DESCRIPTIONS` has
  15 keys; module docstring says "Rule IDs (15)"; `Violation` docstring says
  "15 rule IDs".
- **Worker coupling** (`workers/tofu_apply/main.py:486-493` propose,
  `:622-629` apply): `has_create = plan_has_create(parsed_plan_json)`; when
  True, `_verify_iac_tree_or_raise(...)` runs (raises ⇒ 422 pre-mint / the
  C6 refusal class post-claim), THEN
  `_fidelity_or_raise(..., allow_create_of_declared=has_create)`. Since
  Phase 1 made imports create-class, **the tree-hash gate already runs for
  import plans** — the proof the new flag couples to already executes.
- **Guard** (`workers/tofu_apply/tofu_runner.py:263-267`): importing refused
  unconditionally, before the no-op `continue`. `_normalize_address` strips
  `[...]` index suffixes; module addresses refused; creates need
  `allow_create_of_declared` + declared membership.
- **Static gate** (`tools/iac_static_gate.py`): no opinion on `import`
  blocks. Content checks run in BOTH modes; AGENT-only rules are gated on
  `gi.mode is GateMode.AGENT` (precedent: `secret-material-forbidden`).
  `GateInput.hcl_files` = changed `iac/*.tf` content at head only
  (`_git_diff_names` + `_git_show`; deleted files omitted).
  `iac/imports.tf` ∈ `PROTECTED_FOUNDATION` (unchanged by this phase).
- **hcl2 parse shapes** (probed with the repo's hcl2 this session):
  - `import { to = google_storage_bucket.x  id = "n" }` →
    `{"to": "${google_storage_bucket.x}", "id": "\"n\"", "__is_block__": true}`
  - indexed target → `"to": "${google_storage_bucket.indexed[0]}"`
  - expression id → `"id": "${var.bucket_name}"`; embedded interpolation →
    `"id": "\"projects/p/topics/${each.key}\""` (quote-wrapped but contains `${`)
  - `identity = {...}` → an `"identity"` dict key in the block
  - `for_each` on the import block → a `"for_each"` key
  - resource bodies carry `count` / `for_each` as plain body keys
  - `iter_blocks(parsed, "import")` yields these dicts;
    `iac_hcl._unwrap_ref` unwraps `to`; `iac_hcl.unwrap` strips id quotes.
- **iac_hcl** (`driftscribe_lib/iac_hcl.py`):
  `extract_declared_identities` consumes import blocks' `id` VERBATIM
  (lines 277-296) — comment at ~285 ("imports.tf is operator-only … agents
  cannot add imports") goes stale this phase.
  `_SUPPORTED_RESOURCE_ASSET_TYPES` = the D2 four + `google_service_account`.
- **classify** (`driftscribe_lib/iac_plan_classify.py`): importing ⇒
  create-class (Phase 1); fail-closed True on malformed.
- **C6 copy** (`agent/main.py`): create-specific strings at `~3644`
  ("CREATES a resource, so the worker must be RE-BAKED…"), `~3759-3766`
  (5xx failure: "A created resource may exist out of state — … orphan
  check"), `~3786` ("Applied (create) — …"). `IacPlanView.has_create`
  property exists (`agent/iac_artifacts.py:439`); the view stashes
  `self._plan_json`. **No existing test pins these strings** (grep:
  only `agent/main.py` contains them) — the new branches need new tests.
- **Capabilities**: `RULE_CATEGORIES` (agent/capabilities.py:202) has
  `import-forbidden-v1: "global-v1"`; DTO `denylist` =
  {summary, enforced_at, rules}; pins:
  `test_rule_categories_cover_exactly_the_rule_descriptions` (set equality
  with RULE_DESCRIPTIONS), `test_capabilities.py:213`
  `len(dto["denylist"]["rules"]) == 15`. Frontend
  (`frontend/src/lib/capabilities.ts` + `CapabilityCard.svelte`) renders
  denylist rules grouped by category; validator only requires `rules` to be
  an array — an additive optional field is safe.
- **Drift pins to touch**: `test_denylist_rule_descriptions.py`
  (`test_there_are_exactly_fifteen_rules`, AST literal scan),
  `test_iac_plan_summary.py::test_rule_descriptions_key_set_drift_pin`
  (~662, exact set), `BLAST_CANNOT_TOUCH_NOTE`
  (`iac_plan_summary.py` ~52: "…cannot adopt (import) existing resources…"),
  `iac/README.md` ~196 ("**15 rules**" + enumeration), module/Violation
  docstrings.
- **Fixtures**: `tests/fixtures/iac_plan_denylist/` has 15 import fixtures
  from Phase 1, incl. the provider-real `real_import_bucket_pure_noop.json`
  (bucket `driftscribe-hack-2026-c6e-probe`, pure no-op import) and the
  **deliberately-reserved** `real_import_bucket_with_update.json`
  (provider-real importing+update — committed in Phase 1, referenced by no
  test, exactly for this phase's matrix).
- **Worker endpoint test**: `test_propose_import_plan_refused_by_denylist`
  posts the real pure-no-op fixture and expects 422 + "import-forbidden-v1"
  — Phase 2 changes that fixture's verdict, so this test is REWORKED (Task 5).
- **Lib API pins (Codex must-fix #1)**: `tests/unit/test_iac_plan_denylist_lib.py:34`
  pins `lib.__all__` as an EXACT list, and `:70`
  (`test_tools_shim_reexports_the_same_objects`) pins the `tools` shim
  re-export identity; `tools/iac_plan_denylist.py:23` re-exports every lib
  constant explicitly. Adding `ADOPTABLE_RESOURCE_TYPES` to `__all__` without
  updating all three fails CI.
- **tofu-editor runs the gate in-process (Codex important #2)**:
  `workers/tofu_editor/main.py:283` calls the SAME
  `evaluate(GateInput(mode=GateMode.AGENT, ...))` before any GitHub call —
  the new import rules change the AUTHORING worker's behavior too, so it
  needs endpoint tests (`workers/tofu_editor/tests/test_static_gate_precheck.py`)
  AND a redeploy (its image bakes `tools/` + `driftscribe_lib/` —
  `infra/cloudbuild.tofu-editor.yaml`). Its Dockerfile comment (~:65)
  claims the gate imports ONLY `driftscribe_lib.iac_hcl` + stdlib — goes
  stale when the gate imports `ADOPTABLE_RESOURCE_TYPES` (Codex important #3;
  runtime unaffected, `driftscribe_lib/` is fully copied).

## 1. New rule inventory

**Denylist (plan-level, shared lib) — `import-forbidden-v1` REMOVED, four added (15 → 18):**

| Rule | Fires when |
|---|---|
| `import-with-changes-forbidden-v1` | an importing entry's actions ≠ exactly `("no-op",)` (this IS D1) |
| `import-type-not-adoptable-v1` | an importing entry's `type` ∉ `ADOPTABLE_RESOURCE_TYPES` |
| `import-mixed-plan-forbidden-v1` | plan has ≥1 importing entry AND ≥1 non-importing entry whose actions ∉ `NO_OP_ACTION_TUPLES` (unrelated no-op/read entries do NOT trip — D1 wording) |
| `import-batch-forbidden-v1` | >1 importing entry in one plan (D3) |

**Static gate (authoring-level, AGENT mode ONLY; OPERATOR unchanged) — six added:**

| Rule | Fires when |
|---|---|
| `import-target-undeclared` | `to` is missing/unparseable/not a plain `type.name`, or no matching `resource` block exists in the PR's changed files (the §5 invariant; main-only targets refused = deliberate false-negative) |
| `import-type-not-adoptable` | `to`'s type ∉ `ADOPTABLE_RESOURCE_TYPES` |
| `import-id-not-literal` | `id` missing / not a plain literal string / contains `${` / the OpenTofu-1.12 `identity` attribute is present / literal doesn't match the CAI-normalized shape for the type |
| `import-target-indexed` | `to` contains `[`…`]`, or the target resource block declares `count`/`for_each` |
| `import-foreach-forbidden` | the import block itself has `for_each` (or `count` — fail-closed) |
| `import-batch-forbidden` | >1 import block total across the PR's changed files |

**Guard (worker)**: importing entries handled by an explicit branch —
non-no-op importing refused always; no-op importing admitted ONLY with
`allow_import_of_declared=True` AND normalized address ∈ declared AND not
`module.*` AND not indexed (`[` in address → refuse; the static gate bans
indexed targets, and `_normalize_address` would otherwise erase the index and
admit one — the guard must not silently undo the ban).

## 2. Task list

### Task 1: `ADOPTABLE_RESOURCE_TYPES` + subset drift pin

**Files:** Modify `driftscribe_lib/iac_plan_denylist.py`,
`tests/unit/test_iac_plan_denylist.py`.

After the WIF/IAM constants in the denylist module:

```python
# D2 (adopt design §1): the v1 adoptable-type allowlist — exactly the types
# both the plan-builder WIF SA and tofu-apply-sa can already read. DELIBERATELY
# a separate constant from iac_hcl._SUPPORTED_RESOURCE_ASSET_TYPES, which also
# contains google_service_account (excluded by D2: identities are the most
# sensitive type). A drift pin asserts this is a STRICT subset of the template
# types (every adoptable type must be authorable from a template).
ADOPTABLE_RESOURCE_TYPES: frozenset[str] = frozenset(
    {
        "google_storage_bucket",
        "google_pubsub_topic",
        "google_pubsub_subscription",
        "google_cloud_run_v2_service",
    }
)
```

Add to `__all__` AND (Codex must-fix #1) to the `tools/iac_plan_denylist.py`
re-export list, the exact-`__all__` assertion in
`tests/unit/test_iac_plan_denylist_lib.py::test_canonical_api_importable_from_lib`,
and the identity assertions in `test_tools_shim_reexports_the_same_objects`
(`assert shim.ADOPTABLE_RESOURCE_TYPES is lib.ADOPTABLE_RESOURCE_TYPES`).

Tests (write first, watch fail, then add the constant):

```python
def test_adoptable_types_exact_set():
    assert ADOPTABLE_RESOURCE_TYPES == {
        "google_storage_bucket", "google_pubsub_topic",
        "google_pubsub_subscription", "google_cloud_run_v2_service",
    }

def test_adoptable_types_strict_subset_of_identity_templates():
    from driftscribe_lib.iac_hcl import _SUPPORTED_RESOURCE_ASSET_TYPES
    assert ADOPTABLE_RESOURCE_TYPES < set(_SUPPORTED_RESOURCE_ASSET_TYPES)
    assert "google_service_account" not in ADOPTABLE_RESOURCE_TYPES  # D2
```

### Task 2: denylist — conditional rules replace the blanket

**Files:** Modify `driftscribe_lib/iac_plan_denylist.py`,
`tests/unit/test_iac_plan_denylist.py`,
`tests/fixtures/iac_plan_denylist/` (3 new fixtures, several re-pinned).

In `evaluate()`, REPLACE the Phase-1 blanket block (keep the malformed-importing
emission and the position BEFORE the unknown-action `continue`) and add two
plan-level accumulators:

```python
    violations: list[Violation] = []
    importing_addresses: list[str] = []      # batch rule (D3)
    other_mutation_addresses: list[str] = [] # mixed rule (D1 plan-wide)
    ...
    for rc, rtype, actions in _iter_resource_changes(di.plan):
        ...existing malformed continue...
        # Adopt admission floors (design §4.2, Phase 2 — replaces the Phase-1
        # blanket import-forbidden-v1). Run BEFORE the unknown-action continue
        # so an importing row is always visible as an import. `importing: null`
        # is absent (iac_plan_summary semantics); a non-dict value is
        # additionally malformed.
        importing = (rc.get("change") or {}).get("importing")
        if importing is not None and not isinstance(importing, dict):
            violations.append(Violation(
                "plan-json-malformed-change",
                f"{address}: importing is {type(importing).__name__}, expected object",
            ))
        if importing is not None:
            importing_addresses.append(address)
            if actions != ("no-op",):
                violations.append(Violation(
                    "import-with-changes-forbidden-v1",
                    f"{address}: import would also CHANGE the resource "
                    f"(actions={list(actions)}) — only zero-change adopts are "
                    f"admitted; regenerate the config to match live reality (D1)",
                ))
            if rtype not in ADOPTABLE_RESOURCE_TYPES:
                violations.append(Violation(
                    "import-type-not-adoptable-v1",
                    f"{address}: type {rtype!r} is not in the v1 adoptable allowlist",
                ))
        elif actions not in NO_OP_ACTION_TUPLES:
            other_mutation_addresses.append(address)
        if actions not in ALL_KNOWN_TUPLES:
            ...existing unknown-action continue...
```

After the loop, before `return violations`:

```python
    if len(importing_addresses) > 1:
        violations.append(Violation(
            "import-batch-forbidden-v1",
            f"plan imports {len(importing_addresses)} resources "
            f"({', '.join(importing_addresses)}) — one adoption per PR (D3)",
        ))
    if importing_addresses and other_mutation_addresses:
        violations.append(Violation(
            "import-mixed-plan-forbidden-v1",
            f"plan mixes an import ({', '.join(importing_addresses)}) with other "
            f"mutations ({', '.join(other_mutation_addresses)}) — an adoption "
            f"plan may contain nothing but the adoption",
        ))
```

Notes pinned by tests:
- The identity gate `if _is_mutation(actions) or importing is not None:` is
  UNCHANGED (§4.1 — control-plane imports still fire `control-plane-*`).
- `other_mutation_addresses` counts unknown-action entries too (fail-closed:
  an unknown verb coexisting with an import is "import + possibly-mutating
  other entry"); entries that died at the malformed `continue` never reach the
  accumulators (they are already denied).
- `rtype` is guaranteed `str` at the importing checks (non-str rtype ⇒
  `actions is None` ⇒ malformed `continue` — the documented Phase-1 labelling
  edge for structurally-malformed importing entries is unchanged).

`RULE_DESCRIPTIONS`: remove `import-forbidden-v1`, insert the four (keep the
iam→delete neighborhood ordering):

```python
    "import-with-changes-forbidden-v1": (
        "Adopting a resource must change nothing: if importing it would also "
        "modify it, the plan is refused and the agent must regenerate config "
        "that matches live reality exactly."
    ),
    "import-type-not-adoptable-v1": (
        "Only Cloud Storage buckets, Pub/Sub topics and subscriptions, and "
        "Cloud Run services can be adopted (imported) in v1 — every other "
        "type is refused."
    ),
    "import-mixed-plan-forbidden-v1": (
        "An adoption plan may contain nothing but the adoption — any other "
        "change in the same plan is refused."
    ),
    "import-batch-forbidden-v1": (
        "One adoption at a time: a plan importing more than one resource is "
        "refused."
    ),
```

Module docstring: "Rule IDs (15)" → "(18)", replace the
`import-forbidden-v1` bullet with four bullets. `Violation` docstring:
"15 rule IDs" → "18 rule IDs".

**Fixture matrix (TDD: re-pin expectations FIRST, watch them fail, then code):**

| Fixture | Phase-1 verdict | Phase-2 verdict (exact `_rules` set unless noted) |
|---|---|---|
| `real_import_bucket_pure_noop.json` (provider-real) | `["import-forbidden-v1"]` | `[]` — **ADMITTED** (allowlisted + zero-change + single) |
| `real_import_bucket_with_update.json` (provider-real, reserved) | unused | `["import-with-changes-forbidden-v1"]` (verify nothing else fires) |
| `import_alongside_unrelated_noops.json` | exactly-once floor | `[]` — the D1-wording regression (unrelated no-op/read entries don't trip mixed) |
| `import_unprotected_topic.json` | floor | `[]` (topic is allowlisted) |
| `import_control_plane_state_bucket.json` | floor ⊆ | `["control-plane-bucket"]` |
| `import_control_plane_service.json` | floor ⊆ | `["control-plane-service"]` |
| `import_control_plane_sa.json` | floor ⊆ | `{"control-plane-sa", "iam-change-forbidden-v1", "import-type-not-adoptable-v1"}` |
| `import_control_plane_secret.json` | floor ⊆ | `{"control-plane-secret", "import-type-not-adoptable-v1"}` |
| `import_control_plane_kms.json` | floor ⊆ | `{"control-plane-kms", "import-type-not-adoptable-v1"}` |
| `import_wif_pool.json` | floor ⊆ | `{"wif-config-change", "iam-change-forbidden-v1", "import-type-not-adoptable-v1"}` |
| `import_unknown_action.json` | dual-emit | `{"import-with-changes-forbidden-v1", "unknown-action-forbidden-v1"}` |
| `import_malformed_importing_string.json` | malformed + floor | `["plan-json-malformed-change"]` (string importing; conditionals may not fire — denial preserved via malformed) |
| `import_sparse_protected_no_identity.json` | malformed | unchanged (`plan-json-malformed-change`) |
| `importing_null_is_noop_pass.json` | `[]` | `[]` (unchanged) |
| `noop_control_plane_service_pass.json` | `[]` | `[]` (unchanged) |
| NEW `import_type_not_adoptable.json` (e.g. `google_compute_instance` importing, pure no-op) | — | `["import-type-not-adoptable-v1"]` |
| NEW `import_mixed_with_update.json` (bucket import no-op + unrelated topic `update`) | — | `["import-mixed-plan-forbidden-v1"]` |
| NEW `import_batch_two.json` (two bucket imports, both pure no-op) | — | `["import-batch-forbidden-v1"]` |

Run: `uv run pytest tests/unit/test_iac_plan_denylist.py -q` → green.

### Task 3: drift-pin sweep (denylist-adjacent)

**Files:** Modify `tests/unit/test_denylist_rule_descriptions.py`,
`tests/unit/test_iac_plan_summary.py`, `driftscribe_lib/iac_plan_summary.py`,
`agent/capabilities.py`, `tests/unit/test_capabilities.py`, `iac/README.md`.

1. `test_there_are_exactly_fifteen_rules` → rename
   `test_there_are_exactly_eighteen_rules`, assert 18. (The AST literal scan
   needs no change — all new `Violation(...)` first args are literals.)
2. `test_rule_descriptions_key_set_drift_pin` (~662): −`import-forbidden-v1`,
   +the four new IDs.
3. `BLAST_CANNOT_TOUCH_NOTE` — honest conditional rewording (claims ONLY what
   the denylist enforces):

```python
BLAST_CANNOT_TOUCH_NOTE = (
    "It cannot touch DriftScribe's own control plane (its services, "
    "service accounts, state/artifact buckets, secrets, or encryption "
    "keys), cannot change IAM anywhere, cannot delete, replace, or "
    "un-manage any resource, and can adopt (import) an existing resource "
    "only one at a time, from a small allowlist of types, and only when "
    "nothing would be modified — denylist-enforced, re-checked by the "
    "apply worker before apply."
)
```

   Update its companion tests (`test_blast_cannot_touch_note_matches_rule_set`
   key-set, `test_note_never_mentions_networks_or_databases` still passes,
   any literal-substring assertions on the old "cannot adopt" wording).
4. `RULE_CATEGORIES`: −`import-forbidden-v1`, +four new IDs, all
   `"global-v1"` (they are v1 floors/conditions; no new category — the
   frontend grouping stays untouched).
5. `test_capabilities.py:213` → `== 18`.
6. `iac/README.md` ~196: "**15 rules**" → "**18 rules**"; in the action-floor
   enumeration replace `import-forbidden-v1` with the four new IDs.

Run: `uv run pytest tests/unit/test_denylist_rule_descriptions.py tests/unit/test_iac_plan_summary.py tests/unit/test_capabilities.py -q`.

### Task 4: static gate — six AGENT-mode import rules

**Files:** Modify `tools/iac_static_gate.py`,
`tests/unit/test_iac_static_gate.py`.

Imports: add `re`, extend the `driftscribe_lib.iac_hcl` import with
`_unwrap_ref as _unwrap_ref` (precedent: the gate already imports private-ish
helpers with aliases), and add
`from driftscribe_lib.iac_plan_denylist import ADOPTABLE_RESOURCE_TYPES`
(direction tools → lib, established).

Constants (near `SECRET_MATERIAL_*`):

```python
# Adopt/import authoring rules (design §5, AGENT mode only). The import `id`
# must be a plain literal in the CAI-normalized form the declared-identity
# resolver consumes verbatim (iac_hcl.extract_declared_identities) — any other
# spelling renders the adopted resource as format-mismatch false-drift.
ADOPT_IMPORT_ID_SHAPES: dict[str, re.Pattern[str]] = {
    "google_storage_bucket": re.compile(r"^[^/\s]+$"),  # bare global bucket name
    "google_pubsub_topic": re.compile(r"^projects/[^/\s]+/topics/[^/\s]+$"),
    "google_pubsub_subscription": re.compile(r"^projects/[^/\s]+/subscriptions/[^/\s]+$"),
    "google_cloud_run_v2_service": re.compile(
        r"^projects/[^/\s]+/locations/[^/\s]+/services/[^/\s]+$"
    ),
}
# A plain `type.name` address: exactly one dot, no index, no module./data. path.
_PLAIN_ADDRESS_RE = re.compile(r"^[A-Za-z_][\w-]*\.[A-Za-z_][\w-]*$")
```

A drift pin asserts `set(ADOPT_IMPORT_ID_SHAPES) == set(ADOPTABLE_RESOURCE_TYPES)`
(test below), so the two can never drift.

`evaluate()` restructure: the content loop already parses each file — keep a
`parsed_by_path: dict[str, dict]` of successful parses, then AFTER the loop run
the import pass (AGENT mode only; in OPERATOR mode operators keep full import
freedom — zero new checks):

```python
    if gi.mode is GateMode.AGENT:
        declared: dict[str, dict] = {}   # "type.name" -> resource body
        for parsed in parsed_by_path.values():
            for rtype, rname, body in _iter_typed_blocks_named(parsed):
                declared[f"{rtype}.{rname}"] = body
        import_blocks: list[tuple[str, dict]] = []
        for path, parsed in parsed_by_path.items():
            for imp in _iter_blocks(parsed, "import"):
                import_blocks.append((path, imp))
        for path, imp in import_blocks:
            _check_import_block(path, imp, declared, violations)
        if len(import_blocks) > 1:
            violations.append(Violation(
                "import-batch-forbidden",
                f"{len(import_blocks)} import blocks across the changed files "
                "(at most one adoption per PR)",
            ))
```

(`_iter_typed_blocks_named` = thin adapter over
`driftscribe_lib.iac_hcl.iter_typed_blocks(parsed, "resource")` yielding the
3-tuple — the existing `_iter_typed_blocks` drops the name.) Files that fail
to parse are excluded from `declared` — fail-closed in the deny direction (a
target declared only in an unparseable file reads as undeclared).

`_check_import_block` (rule interplay pinned by tests — each block emits ALL
independently-detectable violations, EXCEPT: an indexed/unparseable `to` skips
the undeclared/type/shape checks it makes meaningless):

```python
def _check_import_block(
    path: str, imp: dict, declared: dict[str, dict], violations: list[Violation]
) -> None:
    if "for_each" in imp or "count" in imp:
        violations.append(Violation(
            "import-foreach-forbidden",
            f"{path}: import block declares for_each/count (imports must be "
            "statically analyzable)",
        ))
    # --- target address (`to`) ---
    to_ref = _unwrap_ref(imp.get("to"))
    rtype: str | None = None
    if to_ref is None:
        violations.append(Violation(
            "import-target-undeclared", f"{path}: import block has no parseable 'to'"
        ))
    elif "[" in to_ref or "]" in to_ref:
        violations.append(Violation(
            "import-target-indexed",
            f"{path}: import target {to_ref!r} is indexed — v1 adopts plain "
            "type.name addresses only",
        ))
    elif not _PLAIN_ADDRESS_RE.fullmatch(to_ref):
        violations.append(Violation(
            "import-target-undeclared",
            f"{path}: import target {to_ref!r} is not a plain type.name "
            "resource address",
        ))
    else:
        rtype = to_ref.split(".", 1)[0]
        if rtype not in ADOPTABLE_RESOURCE_TYPES:
            violations.append(Violation(
                "import-type-not-adoptable",
                f"{path}: import target type {rtype!r} is not adoptable in v1",
            ))
        body = declared.get(to_ref)
        if body is None:
            violations.append(Violation(
                "import-target-undeclared",
                f"{path}: import target {to_ref!r} has no resource block in "
                "the PR's changed files (the adopt pair must travel together)",
            ))
        elif "count" in body or "for_each" in body:
            violations.append(Violation(
                "import-target-indexed",
                f"{path}: import target {to_ref!r} resource block uses "
                "count/for_each",
            ))
    # --- import id ---
    if "identity" in imp:
        violations.append(Violation(
            "import-id-not-literal",
            f"{path}: import block uses the 'identity' attribute — unsupported "
            "(the declared-identity resolver consumes 'id' only)",
        ))
    raw_id = imp.get("id")
    is_literal = (
        isinstance(raw_id, str) and len(raw_id) >= 2
        and raw_id[0] == '"' and raw_id[-1] == '"' and "${" not in raw_id
    )
    if not is_literal:
        violations.append(Violation(
            "import-id-not-literal",
            f"{path}: import id must be a plain literal string "
            f"(got {raw_id!r})",
        ))
    elif rtype in ADOPT_IMPORT_ID_SHAPES:
        ident = _unwrap(raw_id)
        if not ADOPT_IMPORT_ID_SHAPES[rtype].fullmatch(ident):
            violations.append(Violation(
                "import-id-not-literal",
                f"{path}: import id {ident!r} does not match the "
                f"CAI-normalized shape for {rtype!r}",
            ))
```

Module docstring: add one sentence ("agent PRs may carry at most one
zero-change adopt `import` block, target+resource co-located, under the
import-* rules — design 2026-06-11-adopt-import-design.md §5").

**Tests** (in `tests/unit/test_iac_static_gate.py`, new class, following the
file's existing `GateInput(...)` style; write the matrix FIRST):

- happy adopt pair (import + matching resource in one changed file; bucket,
  literal bare-name id) → `[]` in AGENT mode — **the admission test**.
- pair split across two changed files → `[]`.
- each shape from §1's gate table → exact rule(s):
  no-`to` / `module.x.y` / 3-component address → `import-target-undeclared`;
  `to` with `[0]` → `import-target-indexed` only (plus id rules if also bad);
  target resource with `count` / `for_each` → `import-target-indexed`;
  `google_service_account` pair → `import-type-not-adoptable`;
  `id = var.x` / id missing / embedded `${}` / `identity` attr →
  `import-id-not-literal`;
  bucket id containing `/`, topic id as bare name, run-service id missing
  `locations` → `import-id-not-literal` (shape);
  `for_each` on the import block → `import-foreach-forbidden`;
  two import blocks (same file AND split files) → `import-batch-forbidden`;
  import targeting a resource declared only in an UNPARSEABLE changed file →
  `hcl-parse-error` + `import-target-undeclared` (fail-closed).
- OPERATOR mode: the same multi-violation files → no `import-*` violations.
- drift pin: `set(ADOPT_IMPORT_ID_SHAPES) == set(ADOPTABLE_RESOURCE_TYPES)`.
- regression: a non-import agent PR (existing fixtures) emits no import rules.

**tofu-editor coverage (Codex important #2)** — the authoring worker runs
this same gate in-process (`workers/tofu_editor/main.py:283`). Extend
`workers/tofu_editor/tests/test_static_gate_precheck.py`:

- happy adopt pair (import block + matching resource, literal id) → request
  reaches the fake GitHub client (PR opened);
- a bad import block (e.g. `id = var.x`) → 422 whose detail names
  `import-id-not-literal`, and NO GitHub call was made.

**Dockerfile comment (Codex important #3)** — update the stale prose at
`workers/tofu_editor/Dockerfile` ~:65: the gate now imports
`driftscribe_lib.iac_hcl` AND `driftscribe_lib.iac_plan_denylist`
(`ADOPTABLE_RESOURCE_TYPES`) + stdlib; runtime already copies all of
`driftscribe_lib/`.

Run: `uv run pytest tests/unit/test_iac_static_gate.py tests/unit/test_iac_static_gate_cli.py tests/unit/test_iac_static_gate_parity.py workers/tofu_editor/tests -q`.

### Task 5: guard `allow_import_of_declared` + worker threading

**Files:** Modify `workers/tofu_apply/tofu_runner.py`,
`workers/tofu_apply/main.py`, `workers/tofu_apply/tests/test_tofu_apply.py`.

`resource_set_guard` signature:

```python
def resource_set_guard(
    plan_json: dict,
    declared: set[str],
    *,
    allow_create_of_declared: bool = False,
    allow_import_of_declared: bool = False,
) -> str | None:
```

Replace the Phase-1 unconditional importing refusal with (same position,
before the no-op `continue`):

```python
        # Import admission (adopt design §4.5, Phase 2): an importing entry
        # writes a NEW address into state at apply even when its actions are
        # pure no-op. Admitted ONLY when (i) allow_import_of_declared — which
        # the worker sets ONLY after the C6 tree-hash proof — AND (ii) the
        # address is declared in the baked iac/. Anything else refuses:
        # import-with-changes and indexed/module addresses are refused even
        # WITH the flag (the denylist + static gate ban them; the guard must
        # not silently undo that — defense in depth). `importing: null` is
        # absent; a leftover-inert import block on a later plan carries no
        # `importing` and stays a plain no-op.
        if change.get("importing") is not None:
            address = rc.get("address")
            if not isinstance(address, str):
                return "importing resource_changes entry has no address"
            if actions != ["no-op"]:
                return (
                    f"{address}: import with changes (actions={actions}) — "
                    "only zero-change imports are admitted"
                )
            if address.startswith("module."):
                return f"{address}: module-nested import not supported by the baked-config guard"
            if "[" in address:
                return f"{address}: indexed import target not admitted (v1 adopts plain addresses)"
            if not allow_import_of_declared:
                return (
                    f"{address}: plan imports a resource into state "
                    "(needs the head config — re-bake from main, C6)"
                )
            if _normalize_address(address) not in declared:
                return f"{address}: imported address not declared in the baked iac/"
            continue
```

Update the guard docstring item (0) accordingly. `assert_fidelity` gains and
forwards `allow_import_of_declared: bool = False`; `_fidelity_or_raise`
(workers/tofu_apply/main.py:280) likewise. Both call sites
(propose `:493`, apply `:629`) become:

```python
        _fidelity_or_raise(
            fetched_md, parsed_plan_json,
            allow_create_of_declared=has_create,
            allow_import_of_declared=has_create,
        )
```

(`has_create` is True for import plans since Phase 1, so the tree-hash gate
at `:487-492` has already PASSED whenever the flag is True — the §4.5
coupling. Passing both flags from the one proven signal is deliberate: each
flag only ever matters for its own entry kind, and the denylist's mixed-plan
rule refuses import+create coexistence anyway.)

**Guard unit tests** (extend the Phase-1 section):

- importing+no-op, flag False → refusal mentions "re-bake from main".
- importing+no-op, flag True, address declared → `None` — **the admission**.
- importing+no-op, flag True, address NOT declared → refusal.
- importing+no-op, `allow_create_of_declared=True` but import flag False →
  refusal (flags are independent — the Phase-1 regression, re-pinned).
- importing+update, BOTH flags True, declared → refusal ("import with changes").
- importing on `module.x.y` / on `google_storage_bucket.b[0]`, flag True →
  refusal.
- importing entry with no address, flag True → refusal.
- `importing: null` → ignored (plain no-op) — unchanged.

**Endpoint tests** (rework + add):

- REWORK `test_propose_import_plan_refused_by_denylist` →
  `test_propose_pure_import_passes_denylist_then_tree_gate`: the real
  pure-no-op fixture now clears the denylist and (with no
  `generation_iac_tree` supplied) is refused 422 with detail matching
  `iac-tree gate (re-bake required)` — pinning that imports route through the
  C6 proof, not the lenient path.
- NEW `test_propose_import_with_update_refused_by_denylist`: same harness,
  `real_import_bucket_with_update.json` bytes → 422 + detail contains
  `import-with-changes-forbidden-v1` (endpoint-level denylist pin survives).

Run: `uv run pytest workers/tofu_apply/tests -q`.

### Task 6: `plan_has_import` + import-aware C6 copy

**Files:** Modify `driftscribe_lib/iac_plan_classify.py`,
`tests/unit/test_iac_plan_classify.py`, `agent/iac_artifacts.py`,
`agent/main.py`, plus the existing C6-flow tests for `agent/main.py`
(locate via `grep -rn "waiting_for_rebake" tests/` — extend, don't fork).

New predicate (classify module):

```python
def plan_has_import(plan_json: Any) -> bool:
    """True iff any well-formed ``resource_changes`` entry carries a non-null
    ``importing`` value. COPY-SELECTION ONLY (the C6 lifecycle text says
    "adopts" instead of "creates") — never a gate. Unlike
    :func:`plan_has_create` this is NOT fail-closed: malformed structures
    return ``False`` (the create copy is the safe default, and routing/gating
    still use the fail-closed predicate)."""
    if not isinstance(plan_json, dict):
        return False
    rcs = plan_json.get("resource_changes")
    if not isinstance(rcs, list):
        return False
    for rc in rcs:
        if not isinstance(rc, dict):
            continue
        change = rc.get("change")
        if not isinstance(change, dict):
            continue
        if change.get("importing") is not None:
            return True
    return False
```

Add to `__all__`. `IacPlanView.has_import` property mirrors `has_create`
(`agent/iac_artifacts.py:439`).

`agent/main.py` copy branches (all on `view.has_import`):

1. `_iac_merge_then_wait` outcome (~3644): import variant —
   "Merged to main (PR #N, head …). This plan ADOPTS (imports) an existing
   resource into IaC management — nothing in your infrastructure will be
   created or modified — but the worker must still be RE-BAKED from the new
   main before it can apply. Operator: run `gcloud builds submit …` , then
   RELOAD this page and click Apply to complete. Expected iac_tree_hash: …"
   (same command + hash tail as the create variant).
2. `_iac_resume_apply` 5xx notifier + raised detail (~3759-3766): import
   variant — "C6 adopt-class apply {status} for PR #N (already MERGED to
   main, head …). An import that fails normally writes no state and creates
   nothing, but that is verified, never assumed — run the apply-failure
   recovery runbook before any retry." (honest §4.4 framing: no orphan-check
   claim about a "created resource"; the suspicion model is the worker's
   existing one).
3. Success outcome (~3786): "Applied (adopt) — the existing resource is now
   under IaC management; nothing was modified. The PR was already merged to
   main. Done."

**Tests:**
- classify: importing+no-op ⇒ True; no importing ⇒ False; `importing: null` ⇒
  False; malformed plan / non-dict rc / non-list rcs ⇒ False (the
  NOT-fail-closed contract, asserted explicitly with a comment);
  cross-predicate pin: a malformed plan ⇒ `plan_has_create` True AND
  `plan_has_import` False.
- main C6 flow: clone the existing create-class merge test with an
  import-shaped plan view → outcome contains "ADOPTS" and NOT "CREATES a
  resource"; the create-shaped case still says "CREATES" (regression);
  the 5xx-failure import case notifies with "adopt-class … verified, never
  assumed" and not "created resource may exist out of state"; the success
  import case says "Applied (adopt)".

### Task 7: stale comment + capability card adoptable types

**Files:** Modify `driftscribe_lib/iac_hcl.py` (~285),
`agent/capabilities.py`, `tests/unit/test_capabilities.py`,
`frontend/src/lib/capabilities.ts`,
`frontend/src/components/CapabilityCard.svelte`, frontend vitest.

1. `iac_hcl.py` comment: replace "imports.tf is operator-only foundation
   (gate-locked; agents cannot add imports), so this is an operator-authoring
   contract" with: "imports.tf stays operator-only foundation (gate-locked);
   agent adopt PRs may carry ONE co-located import block under the static
   gate's import-* rules (Phase 2), and the gate enforces this same
   CAI-normalized id shape pre-plan (`ADOPT_IMPORT_ID_SHAPES`)."
2. `agent/capabilities.py`: import `ADOPTABLE_RESOURCE_TYPES`; add a label
   map + DTO field:

```python
ADOPTABLE_TYPE_LABELS: Final[Mapping[str, str]] = MappingProxyType({
    "google_storage_bucket": "Cloud Storage bucket",
    "google_pubsub_topic": "Pub/Sub topic",
    "google_pubsub_subscription": "Pub/Sub subscription",
    "google_cloud_run_v2_service": "Cloud Run service",
})
```

   In `build_capabilities()`'s `denylist` dict, after `rules`:

```python
            "adoptable_resource_types": [
                {"type": t, "label": ADOPTABLE_TYPE_LABELS[t]}
                for t in sorted(ADOPTABLE_RESOURCE_TYPES)
            ],
```

   Drift pin: `test_adoptable_type_labels_cover_exactly_the_allowlist`
   (`set(ADOPTABLE_TYPE_LABELS) == set(ADOPTABLE_RESOURCE_TYPES)`), and the
   shape test asserts the DTO field (sorted, four entries, str fields).
3. Frontend: `capabilities.ts` denylist type gains
   `adoptable_resource_types?: { type: string; label: string }[]` (optional —
   validator untouched, old payloads stay valid). `CapabilityCard.svelte`,
   in the denylist section after the rule groups:

```svelte
      {#if data.denylist.adoptable_resource_types?.length}
        <p class="ds-subtle cap-denylist__adoptable">
          Adoptable (import) types: {data.denylist.adoptable_resource_types.map((t) => t.label).join(', ')}
        </p>
      {/if}
```

   Vitest: field present → the labels render; field absent → card renders
   without the line (no crash).

Run: `uv run pytest tests/unit/test_capabilities.py -q` and
`cd frontend && npm run test:unit && npm run check && npm run build`.

### Task 8: full gates + plan-doc status

- `uv run pytest -q` (expect ~2434 + new), `uv run ruff check .`
- frontend `npm run test:unit` / `check` / `build`
- grep sweep for stragglers:
  `grep -rn "import-forbidden-v1" --include=*.py --include=*.md --include=*.ts --include=*.svelte .`
  — remaining hits must be historical-record docs only (design doc, Phase-1
  plan, memory) plus this plan.
- Update this doc's status line; commit.

## 3. Out of scope (Phase 3+/4, deliberately)

- The adopt recipe (CAI `/describe` → `iac_hcl` template → tofu-editor PR),
  agent prompt/tool changes, per-type adoption-fidelity probes (Phase 3).
- Adopt button / prefilled chat / approval-page adoption framing /
  blast-radius reframe / meter note / live e2e adopting a hand-made bucket
  (Phase 4). The §6 approval copy is NOT touched here beyond the blast note's
  honesty rewording.
- Batch adoption (item 10), `identity`-attribute support, indexed-target
  support, importing onto main-only config (the §5 deliberate false-negative).
- No IAM changes anywhere; `iac/imports.tf` protection unchanged.

## 4. Ship sequence

Branch `feat/import-admission` → Codex plan review (NEW thread) → subagent
TDD per tasks → Opus whole-branch review → PR (body: admission proof — the
provider-real fixture flipping from denied to admitted-at-denylist while the
worker still refuses without the tree proof) → CI green → Codex
completed-work (same thread) → squash-merge → rebake **THREE** services
(this phase touches the gate + denylist, baked into all three):
coordinator (`cloudbuild.coordinator-update.yaml` + MANDATORY
`update-traffic`), tofu-apply worker (`cloudbuild.tofu-apply.yaml`), and
tofu-editor worker (`cloudbuild.tofu-editor.yaml` — it runs the static gate
in-process, Codex important #2) → live verify `/capabilities` (18 rules,
four import rules, adoptable types present; `X-DriftScribe-Token` header)
→ memory update.

---

## Plan-review record (Codex)

Thread `019eb469-f0a8-7692-9e7d-2c3ea9e69e83`. Round 1: **NO-GO**, 1 must-fix
+ 2 important — all verified against the code and folded:

1. **(must-fix)** `ADOPTABLE_RESOURCE_TYPES` was missing from the lib-API/shim
   sweep: `tools/iac_plan_denylist.py:23` re-exports constants explicitly, and
   `tests/unit/test_iac_plan_denylist_lib.py` pins the exact `__all__` list
   (`:34`) + shim re-export identity (`:70`) → folded into Task 1.
2. **(important)** The tofu-editor worker runs the SAME gate in-process
   (`workers/tofu_editor/main.py:283`) before any GitHub call — endpoint tests
   added to Task 4 and a THIRD rebake (tofu-editor) added to the ship
   sequence (its image bakes `tools/` + `driftscribe_lib/`).
3. **(important)** `workers/tofu_editor/Dockerfile` ~:65 prose ("gate imports
   ONLY driftscribe_lib.iac_hcl + stdlib") goes stale → comment update in
   Task 4 (runtime unaffected — `driftscribe_lib/` fully copied).

Explicitly verified clean by Codex: rule placement/accumulators vs the
unknown-action continue + malformed path; the Task-2 fixture matrix verdicts
(incl. `real_import_bucket_with_update.json` ⇒ exactly
`import-with-changes-forbidden-v1`); the `allow_import_of_declared=has_create`
coupling at both worker call sites (the tree gate provably runs first); and
the §1 three-layer admission story (no path admits an import without
denylist + static gate + tree-hash/declared membership).
