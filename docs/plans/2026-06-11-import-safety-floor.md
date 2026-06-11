# Import Safety Floor Implementation Plan (adopt/import design — Phase 1)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or
> superpowers:subagent-driven-development when dispatched per-task) to implement
> this plan task-by-task.

**Goal:** Close the §2 hole in `docs/plans/2026-06-11-adopt-import-design.md` —
OpenTofu `importing` entries are today invisible to the denylist, the routing
classifier, and the apply worker's resource-set guard — by making every import
**visible and denied-by-default** (blanket `import-forbidden-v1`), with no
admission of anything.

**Architecture:** Four small, independent enforcement changes that share one
semantic (`change.importing is not None` ⇒ "this entry imports"; `importing:
null` is treated as absent, mirroring `iac_plan_summary`): (1) the shared
denylist gains a blanket `import-forbidden-v1` floor and runs its identity
checks on importing entries; (2) `plan_has_create` classifies importing entries
as create-class (strict C6 merge-first routing); (3) `resource_set_guard`
refuses importing entries unconditionally; (4) the five consumer drift pins
(AST rule-count, summary key-set pin, blast-radius note, capability-card
categories, capability-card count) are updated in lockstep. The central §4.1
claim — identity checks work on importing rows — is verified against a REAL
provider-generated plan.json, not a hand-written one.

**Tech Stack:** Python 3.12 / pytest (`uv run pytest -q`), ruff, OpenTofu
1.12.0 + google provider 6.50.0 (fixture generation only, read-only against
live GCP). No frontend changes (frontend capability tests use synthetic rule
entries; the card renders whatever the server serves).

**Scope guard (design §7):** Phase 1 admits NOTHING. No
`ADOPTABLE_RESOURCE_TYPES`, no conditional rules, no static-gate import rules,
no `allow_import_of_declared`, no C6 copy branching, no UI. Those are Phase 2+.

---

## Why each change is safe to ship alone

- The denylist floor only *adds* violations, and only for entries carrying
  `importing` — no plan that passes today can contain one (nothing authors
  import blocks in agent files today; `iac/imports.tf` is empty of agent
  content and foundation-protected).
- `plan_has_create` flipping import plans to create-class only *strengthens*
  routing (strict C6 path); existing non-importing plans are untouched.
- `resource_set_guard` refusing importing entries only *adds* a refusal for
  rows that previously slipped through the no-op `continue`.
- Identity checks on importing entries cannot fire on plain no-op entries —
  the gate is `_is_mutation(actions) or importing is not None`, so the
  existing `read_action_is_pass` / `benign_no_op` behavior is unchanged.

---

### Task 1: Generate the REAL import fixtures (live, read-only)

The §4.1 identity-check claim must be verified against a provider-generated
artifact (design §8: "exactly which identity fields populate `before`/`after`
on an import row is provider behavior, not spec"). Generate it BEFORE writing
denylist tests so the derived fixtures copy a real row shape.

**Live-safety:** this is `tofu plan` only, in a scratch dir with a **local
backend** (empty local state ⇒ the import block is active; nothing reads or
writes the prod GCS state, no lock contention, zero cloud mutation; ADC reads
the bucket metadata only).

**Files:**
- Create: `/tmp/import-fixture/main.tf` (scratch, not committed)
- Create: `tests/fixtures/iac_plan_denylist/real_import_bucket_pure_noop.json`

**Step 1: Verify local tooling**

Run: `tofu version && gcloud config get-value project`
Expected: `OpenTofu v1.12.0`, project `driftscribe-hack-2026`. If ADC is stale
(`google.auth` errors later), STOP and ask the operator to run
`! gcloud auth application-default login`.

**Step 2: Write the scratch config**

`/tmp/import-fixture/main.tf` — mirrors `iac/c6e_probe.tf` (the live probe
bucket `driftscribe-hack-2026-c6e-probe`, free, empty, already verified
denylist-clean by construction). Literals instead of vars; **no
`force_destroy`** (provider-local attribute — `true` in config vs the
import-default would surface as a diff):

```hcl
terraform {
  required_version = ">= 1.12"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "6.50.0"
    }
  }
}

provider "google" {
  project = "driftscribe-hack-2026"
  region  = "asia-northeast1"
}

import {
  to = google_storage_bucket.c6e_probe
  id = "driftscribe-hack-2026-c6e-probe"
}

resource "google_storage_bucket" "c6e_probe" {
  name     = "driftscribe-hack-2026-c6e-probe"
  project  = "driftscribe-hack-2026"
  location = "asia-northeast1"

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  labels = {
    purpose    = "c6e-create-class-e2e"
    throwaway  = "true"
    managed-by = "driftscribe-iac"
  }
}
```

**Step 3: Plan and iterate to pure no-op**

```bash
cd /tmp/import-fixture
tofu init
tofu plan -out=plan.tfplan
tofu show -json plan.tfplan > import_bucket.json
python3 -c "import json;d=json.load(open('import_bucket.json'));[print(r['address'], r['change']['actions'], 'importing' in r['change']) for r in d['resource_changes']]"
```

Expected: `google_storage_bucket.c6e_probe ['no-op'] True`. If actions show
`['update']`, diff `change.before` vs `change.after` in the JSON and adjust the
resource block (drop/align the offending attribute) until pure no-op. If an
intermediate `importing`+update plan is produced, ALSO save it as
`real_import_bucket_with_update.json` (free bonus fixture for the Phase-2
matrix; do not block on getting one).

**Step 4: Trim and pin the fixture**

Keep `format_version`, `terraform_version`, and `resource_changes` VERBATIM;
drop the bulky sections none of the three consumers read (`configuration`,
`planned_values`, `prior_state`, `variables`, `timestamp`, …):

```bash
python3 - <<'EOF'
import json
d = json.load(open('/tmp/import-fixture/import_bucket.json'))
out = {k: d[k] for k in ("format_version", "terraform_version", "resource_changes") if k in d}
json.dump(out, open('/home/adi/driftscribe/tests/fixtures/iac_plan_denylist/real_import_bucket_pure_noop.json', 'w'), indent=2)
EOF
```

**Step 5: Record the row shape**

Note (for Task 3's derived fixtures) which identity fields the real row carries
in `change.before` / `change.after` (expect `name` populated on both sides for
a refreshed import row) and the exact `importing` value shape (expect
`{"id": "driftscribe-hack-2026-c6e-probe"}` — OpenTofu may also include
`unknown`). Derived fixtures MUST copy this shape, changing only identity
strings/types. **Honesty bound (Codex round-1 Important #3):** only the bucket
row is provider-real; the other identity classes are hand-derived from its
shape. Phase 1's blanket denial doesn't need more; per-type provider-real
probes belong to the Phase-3 spec. Test docstrings must not claim
provider-real validation beyond the bucket.

**Step 6: Commit**

```bash
git add tests/fixtures/iac_plan_denylist/real_import_bucket_pure_noop.json
git commit -m "test: pin real provider-generated import plan.json fixture"
```

---

### Task 2: `plan_has_create` — importing entries are create-class

**Files:**
- Modify: `driftscribe_lib/iac_plan_classify.py`
- Test: `tests/unit/test_iac_plan_classify.py`

**Step 1: Write the failing tests**

Append to `tests/unit/test_iac_plan_classify.py`:

```python
# --- Adopt/import design §4.3: importing entries are CREATE-CLASS ---


def _pj_importing(actions, importing):
    return {
        "resource_changes": [
            {
                "address": "google_storage_bucket.b",
                "change": {"actions": actions, "importing": importing},
            }
        ]
    }


def test_importing_noop_is_create_class():
    """A pure import plans as ["no-op"] + importing — it writes a NEW address
    into state at apply, so it must route through the strict C6 merge-first
    path (state-without-config is the §2 delete-proposal failure mode)."""
    assert plan_has_create(_pj_importing(["no-op"], {"id": "b-name"})) is True


def test_importing_update_is_create_class():
    assert plan_has_create(_pj_importing(["update"], {"id": "b-name"})) is True


def test_importing_null_is_treated_as_absent():
    """`importing: null` is NOT an import (same semantics as iac_plan_summary)."""
    assert plan_has_create(_pj_importing(["no-op"], None)) is False


def test_importing_malformed_value_is_still_create_class():
    """Even a malformed (non-dict) importing value routes strict — fail-closed."""
    assert plan_has_create(_pj_importing(["no-op"], "not-a-dict")) is True
```

**Step 2: Run to verify failure**

Run: `uv run pytest -q tests/unit/test_iac_plan_classify.py`
Expected: `test_importing_noop_is_create_class` and
`test_importing_malformed_value_is_still_create_class` FAIL (return False);
the null test passes vacuously.

**Step 3: Implement**

In `driftscribe_lib/iac_plan_classify.py`, inside the loop, after the
`actions` isinstance check and BEFORE the no-op/read skip:

```python
        actions = change.get("actions")
        if not isinstance(actions, list):
            return True
        # Adopt/import design §4.3: an entry with `importing` present is
        # CREATE-CLASS regardless of its actions — the apply writes a NEW
        # address into state, and the lenient C5 path would leave state
        # without config on main (next plan from main proposes DELETING the
        # adopted resource). `importing: null` is treated as absent, same
        # semantics as iac_plan_summary.
        if change.get("importing") is not None:
            return True
        if actions in (["no-op"], ["read"]):
            continue
```

Update the function docstring: add a line — `An entry whose ``change`` carries
a non-null ``importing`` value also returns ``True`` (imports are create-class
— adopt/import design §4.3).`

**Step 4: Run tests**

Run: `uv run pytest -q tests/unit/test_iac_plan_classify.py`
Expected: ALL PASS (including the pre-existing truth table — no regression).

**Step 5: Commit**

```bash
git add driftscribe_lib/iac_plan_classify.py tests/unit/test_iac_plan_classify.py
git commit -m "feat: classify importing entries as create-class (adopt design §4.3)"
```

---

### Task 3: Denylist — `import-forbidden-v1` floor + identity checks on importing entries

**Files:**
- Modify: `driftscribe_lib/iac_plan_denylist.py`
- Modify: `tests/unit/test_denylist_rule_descriptions.py`
- Modify: `iac/README.md` (~line 195: "14 rules" enumeration → 15, add
  `import-forbidden-v1` to the action-floor group — Codex round-1 nit #2)
- Test: `tests/unit/test_iac_plan_denylist.py`
- Create: 13 derived fixtures under `tests/fixtures/iac_plan_denylist/` (below;
  the real fixture from Task 1 makes 14 new files total)

**Step 1: Author the derived fixtures**

Each copies the REAL row shape from Task 1 (full `before`/`after` attribute
dicts are not needed — keep the identity fields the checkers read, plus
`importing` exactly as the real artifact shapes it). All are single-row plans
`{"format_version": "1.2", "resource_changes": [ ... ]}` unless noted:

| Fixture | Row | Expected rules |
|---|---|---|
| `import_alongside_unrelated_noops.json` | the REAL import row + two plain no-op rows (one `google_pubsub_topic`, one `google_storage_bucket`, no `importing`) | exactly `["import-forbidden-v1"]` |
| `import_unprotected_topic.json` | `google_pubsub_topic`, actions `["no-op"]`, `importing {"id": "projects/driftscribe-hack-2026/topics/some-old-topic"}`, before/after `{"name": "projects/driftscribe-hack-2026/topics/some-old-topic"}` | exactly `["import-forbidden-v1"]` |
| `import_control_plane_state_bucket.json` | real bucket row with name strings → `driftscribe-hack-2026-tofu-state` (address `google_storage_bucket.smuggle`) | `import-forbidden-v1` + `control-plane-bucket` |
| `import_control_plane_service.json` | `google_cloud_run_v2_service`, no-op+importing, before/after name `driftscribe-agent` | `import-forbidden-v1` + `control-plane-service` |
| `import_control_plane_sa.json` | `google_service_account`, no-op+importing, before/after `account_id: "driftscribe-agent"` | `import-forbidden-v1` + `control-plane-sa` + `iam-change-forbidden-v1` |
| `import_control_plane_secret.json` | `google_secret_manager_secret`, no-op+importing, `secret_id: "plan-hmac-key"` | `import-forbidden-v1` + `control-plane-secret` |
| `import_control_plane_kms.json` | `google_kms_crypto_key`, no-op+importing, name `tofu-state` | `import-forbidden-v1` + `control-plane-kms` |
| `import_wif_pool.json` | `google_iam_workload_identity_pool`, no-op+importing | `import-forbidden-v1` + `wif-config-change` + `iam-change-forbidden-v1` |
| `import_malformed_importing_string.json` | bucket row, no-op, `"importing": "yes"` | `import-forbidden-v1` + `plan-json-malformed-change` |
| `import_sparse_protected_no_identity.json` | `google_cloud_run_v2_service`, no-op+importing, `before: {}`, `after: {}` | `import-forbidden-v1` + `plan-json-malformed-change` (§4.1 sparse-row fail-closed) |
| `importing_null_is_noop_pass.json` | bucket row, no-op, `"importing": null` | `[]` (pass) |
| `import_unknown_action.json` | bucket row, actions `["frobnicate"]`, importing present | `import-forbidden-v1` + `unknown-action-forbidden-v1` (Codex round-1 Important #1: the floor must fire even on unknown-action rows — see Step 4 ordering) |
| `noop_control_plane_service_pass.json` | `google_cloud_run_v2_service`, actions `["no-op"]`, NO `importing` key, before/after name `driftscribe-agent` | `[]` (pass — Codex round-1 Important #2: pins that identity checks did NOT start firing on plain no-op rows) |

**Step 2: Write the failing tests**

Append to `tests/unit/test_iac_plan_denylist.py`:

```python
# --- Phase 1 import floor (adopt/import design §4.1–§4.2, 2026-06-11) ---


def test_real_provider_import_fixture_is_denied_by_the_floor_alone():
    """THE §4.1/§8 anchor: a REAL `tofu show -json` artifact (live import of
    the c6e probe bucket, google provider 6.50.0) is denied by exactly the
    blanket floor — proving (a) the floor sees provider-real `importing`
    rows, (b) identity checks run on the row without false-firing
    control-plane rules on an unprotected bucket."""
    parsed, _ = load_plan_json(_load("real_import_bucket_pure_noop.json"))
    assert parsed is not None
    assert _rules(evaluate(DenylistInput(plan=parsed))) == ["import-forbidden-v1"]


def test_import_alongside_unrelated_noops_fires_the_floor_exactly_once():
    """The D1-wording regression (design §8): OpenTofu lists EVERY configured
    resource in resource_changes, so unrelated no-op rows accompany any real
    import — they must not add violations."""
    parsed, _ = load_plan_json(_load("import_alongside_unrelated_noops.json"))
    assert _rules(evaluate(DenylistInput(plan=parsed))) == ["import-forbidden-v1"]


def test_import_of_unprotected_type_is_still_denied():
    parsed, _ = load_plan_json(_load("import_unprotected_topic.json"))
    assert _rules(evaluate(DenylistInput(plan=parsed))) == ["import-forbidden-v1"]


@pytest.mark.parametrize(
    ("fixture", "extra_rules"),
    [
        ("import_control_plane_state_bucket.json", {"control-plane-bucket"}),
        ("import_control_plane_service.json", {"control-plane-service"}),
        ("import_control_plane_sa.json", {"control-plane-sa", "iam-change-forbidden-v1"}),
        ("import_control_plane_secret.json", {"control-plane-secret"}),
        ("import_control_plane_kms.json", {"control-plane-kms"}),
        ("import_wif_pool.json", {"wif-config-change", "iam-change-forbidden-v1"}),
    ],
)
def test_importing_control_plane_identities_fires_identity_rules(fixture, extra_rules):
    """§4.1: identity checks now run on importing entries even though a pure
    import plans as no-op — adopting DriftScribe into DriftScribe is
    impossible. Each fixture must fire the floor AND its identity rule(s)."""
    parsed, _ = load_plan_json(_load(fixture))
    rules = set(_rules(evaluate(DenylistInput(plan=parsed))))
    assert ({"import-forbidden-v1"} | extra_rules) <= rules, fixture


def test_malformed_importing_value_is_denied_and_malformed():
    """`importing` must be an object (docs) — a non-dict value is BOTH an
    import (floor fires) and structurally malformed (fail-closed)."""
    parsed, _ = load_plan_json(_load("import_malformed_importing_string.json"))
    rules = set(_rules(evaluate(DenylistInput(plan=parsed))))
    assert {"import-forbidden-v1", "plan-json-malformed-change"} <= rules


def test_sparse_protected_import_row_fails_closed():
    """§4.2: a protected-type importing row whose before/after both lack the
    identity field cannot be cleared against the control-plane sets —
    plan-json-malformed-change fires (bias-to-deny), not a silent pass."""
    parsed, _ = load_plan_json(_load("import_sparse_protected_no_identity.json"))
    rules = set(_rules(evaluate(DenylistInput(plan=parsed))))
    assert {"import-forbidden-v1", "plan-json-malformed-change"} <= rules


def test_importing_null_is_treated_as_absent():
    """`importing: null` is NOT an import (mirrors iac_plan_summary) — and an
    inert leftover import block produces no `importing` at all, so later
    unrelated plans stay clean (design §4.5)."""
    parsed, _ = load_plan_json(_load("importing_null_is_noop_pass.json"))
    assert evaluate(DenylistInput(plan=parsed)) == []


def test_importing_with_unknown_action_fires_both_rules():
    """The floor runs BEFORE the unknown-action continue — an importing row
    with an unaudited action tuple is visible as an import, not only as an
    unknown action (Codex round-1 Important #1)."""
    parsed, _ = load_plan_json(_load("import_unknown_action.json"))
    rules = set(_rules(evaluate(DenylistInput(plan=parsed))))
    assert {"import-forbidden-v1", "unknown-action-forbidden-v1"} <= rules


def test_plain_noop_on_control_plane_identity_still_passes():
    """REGRESSION PIN (Codex round-1 Important #2): widening the identity-check
    gate to `_is_mutation(actions) or importing is not None` must NOT start
    firing control-plane rules on plain no-op rows — every real plan lists
    unchanged resources as no-ops."""
    parsed, _ = load_plan_json(_load("noop_control_plane_service_pass.json"))
    assert evaluate(DenylistInput(plan=parsed)) == []
```

In `tests/unit/test_denylist_rule_descriptions.py`, change the count pin:

```python
def test_there_are_exactly_fifteen_rules():
    # The docstring promises 15 rule IDs; pin it so the AST scan can't
    # silently degrade (e.g. a refactor wrapping Violation in a helper).
    assert len(RULE_DESCRIPTIONS) == 15
```

**Step 3: Run to verify failure**

Run: `uv run pytest -q tests/unit/test_iac_plan_denylist.py tests/unit/test_denylist_rule_descriptions.py`
Expected: every new test FAILS (no `import-forbidden-v1` emitted; count still 14).
`test_importing_null_is_noop_pass` may already pass — fine.

**Step 4: Implement**

In `driftscribe_lib/iac_plan_denylist.py`:

(a) Module docstring: `**Rule IDs (14)**` → `**Rule IDs (15)**`; append bullet:

```
- ``import-forbidden-v1`` — any entry whose ``change.importing`` is
  present (an OpenTofu import block adopting an existing resource into
  state). Blanket v1 floor — the adopt flow (design
  2026-06-11-adopt-import-design.md, Phase 2) replaces it with
  conditional admission rules.
```

(b) In `evaluate()`: the import floor must fire **BEFORE** the unknown-action
`continue` (Codex round-1 Important #1 — an importing row with an unaudited
action tuple must still be visible as an import, not only as an unknown
action). Insert immediately after the `actions is None` malformed branch:

```python
        # Import floor (adopt/import design §4.2, Phase 1): ANY entry with
        # `importing` present is denied outright — Phase 2 replaces this
        # blanket rule with conditional admission (zero-change / type /
        # mixed / batch). Runs BEFORE the unknown-action continue so an
        # importing row is always visible as an import. `importing: null`
        # is treated as absent (same semantics as iac_plan_summary); a
        # non-dict value is additionally malformed (the JSON-output docs
        # define an object).
        importing = (rc.get("change") or {}).get("importing")
        if importing is not None and not isinstance(importing, dict):
            violations.append(
                Violation(
                    "plan-json-malformed-change",
                    f"{address}: importing is {type(importing).__name__}, expected object",
                )
            )
        if importing is not None:
            violations.append(
                Violation(
                    "import-forbidden-v1",
                    f"{address}: import of an existing resource into IaC state "
                    f"(actions={list(actions)}) forbidden in v1",
                )
            )
        if actions not in ALL_KNOWN_TUPLES:
            ...existing unknown-action block unchanged (still `continue`s)...
```

and replace the `if _is_mutation(actions):` gate at the bottom of the loop:

```python
        # Identity-based per-resource rules run for mutations AND for
        # importing entries (§4.1 — a pure import plans as no-op, but
        # adopting a control-plane identity must still fire the
        # control-plane rules). A `read` data-source on a control-plane
        # name remains a legitimate pass (read_action_is_pass fixture), and
        # a plain no-op row on a control-plane identity still passes
        # (noop_control_plane_service_pass fixture).
        if _is_mutation(actions) or importing is not None:
            before, after = _identity_dicts(rc)
            ...existing seven _check_* calls unchanged...
```

(c) `RULE_DESCRIPTIONS` — insert between `iam-change-forbidden-v1` and
`delete-action-forbidden-v1`:

```python
    "import-forbidden-v1": (
        "The agent cannot adopt (import) existing resources into IaC "
        "management yet — every import is refused (v1 floor; a gated adopt "
        "flow will admit them deliberately in a later phase)."
    ),
```

(d) `iac/README.md` (~line 195): "**14 rules**" → "**15 rules**" and add
`import-forbidden-v1` to the action-floor parenthetical.

**Step 5: Run tests**

Run: `uv run pytest -q tests/unit/test_iac_plan_denylist.py tests/unit/test_denylist_rule_descriptions.py tests/unit/test_iac_plan_summary.py tests/unit/test_capabilities.py`
Expected: denylist + AST-pin tests PASS; `test_rule_descriptions_key_set_drift_pin`
(summary) and `test_rule_categories_cover_exactly_the_rule_descriptions` +
`len(dto["denylist"]["rules"]) == 14` (capabilities) now FAIL — **that is the
pins doing their job**; Task 4 fixes them.

**Step 6: Commit**

```bash
git add driftscribe_lib/iac_plan_denylist.py tests/unit/test_iac_plan_denylist.py tests/unit/test_denylist_rule_descriptions.py tests/fixtures/iac_plan_denylist/
git commit -m "feat: blanket import-forbidden-v1 denylist floor + identity checks on importing entries"
```

---

### Task 4: Consumer pins — blast note, capability categories, count

**Files:**
- Modify: `driftscribe_lib/iac_plan_summary.py` (BLAST_CANNOT_TOUCH_NOTE)
- Modify: `tests/unit/test_iac_plan_summary.py` (key-set pin)
- Modify: `agent/capabilities.py` (RULE_CATEGORIES)
- Modify: `tests/unit/test_capabilities.py:213` (count 14 → 15)

**Step 1: Update the blast note (honesty contract — the note may now claim the
import denial, because the denylist now enforces it):**

```python
BLAST_CANNOT_TOUCH_NOTE = (
    "It cannot touch DriftScribe's own control plane (its services, "
    "service accounts, state/artifact buckets, secrets, or encryption "
    "keys), cannot change IAM anywhere, cannot delete, replace, or "
    "un-manage any resource, and cannot adopt (import) existing resources "
    "— denylist-enforced, re-checked by the apply worker before apply."
)
```

**Step 2: Update the three pins**

- `tests/unit/test_iac_plan_summary.py::test_rule_descriptions_key_set_drift_pin`:
  add `"import-forbidden-v1",` to the literal set.
- `agent/capabilities.py` `RULE_CATEGORIES`, in the "Global v1 floors" group:
  `"import-forbidden-v1": "global-v1",`
- `tests/unit/test_capabilities.py`: `assert len(dto["denylist"]["rules"]) == 15`

**Step 3: Run tests**

Run: `uv run pytest -q tests/unit/test_iac_plan_summary.py tests/unit/test_capabilities.py tests/unit/test_denylist_rule_descriptions.py`
Expected: ALL PASS.

**Step 4: Commit**

```bash
git add driftscribe_lib/iac_plan_summary.py tests/unit/test_iac_plan_summary.py agent/capabilities.py tests/unit/test_capabilities.py
git commit -m "feat: surface the import floor in the blast note + capability card"
```

---

### Task 5: `resource_set_guard` refuses importing entries

**Files:**
- Modify: `workers/tofu_apply/tofu_runner.py:225` (`resource_set_guard`)
- Test: `workers/tofu_apply/tests/test_tofu_apply.py`

**Step 1: Write the failing tests** (next to the existing
`resource_set_guard` tests; reuse the file's `_plan_json_obj`-style helpers —
read the neighbors first and match their conventions):

```python
def test_guard_refuses_importing_entry_even_as_noop() -> None:
    """Phase-1 import floor (adopt design §4.5): an importing row plans as
    no-op and previously slipped through the no-op `continue`."""
    pj = {
        "resource_changes": [
            {
                "address": "google_storage_bucket.adopted",
                "change": {"actions": ["no-op"], "importing": {"id": "some-bucket"}},
            }
        ]
    }
    reason = tofu_runner.resource_set_guard(pj, {"google_storage_bucket.adopted"})
    assert reason is not None and "import" in reason


def test_guard_refuses_importing_even_with_create_flag() -> None:
    """The C6 create admission (post tree-hash proof) must NOT admit imports —
    there is no allow_import_of_declared in Phase 1."""
    pj = {
        "resource_changes": [
            {
                "address": "google_storage_bucket.adopted",
                "change": {"actions": ["no-op"], "importing": {"id": "some-bucket"}},
            }
        ]
    }
    reason = tofu_runner.resource_set_guard(
        pj, {"google_storage_bucket.adopted"}, allow_create_of_declared=True
    )
    assert reason is not None and "import" in reason


def test_guard_ignores_importing_null() -> None:
    pj = {
        "resource_changes": [
            {
                "address": "google_storage_bucket.plain",
                "change": {"actions": ["no-op"], "importing": None},
            }
        ]
    }
    assert tofu_runner.resource_set_guard(pj, set()) is None
```

And the endpoint-level belt-and-braces (mirror
`test_propose_denylist_violation_refused` at line ~1357 — same monkeypatched
artifact seams, but the plan artifact is the REAL import fixture):

```python
def test_propose_import_plan_refused_by_denylist(client, monkeypatch, tmp_path) -> None:
    """An import plan must 422 at /propose with import-forbidden-v1 in the
    detail — the worker-side denylist re-run catches it before any gate."""
    # ...same harness as test_propose_denylist_violation_refused, plan bytes =
    # tests/fixtures/iac_plan_denylist/real_import_bucket_pure_noop.json...
    assert resp.status_code == 422
    assert "import-forbidden-v1" in resp.json()["detail"]
```

**Step 2: Run to verify failure**

Run: `uv run pytest -q workers/tofu_apply/tests/test_tofu_apply.py`
Expected: the two refusal tests FAIL (guard returns None); null test passes.

**Step 3: Implement**

In `resource_set_guard`, after the `actions` isinstance check and BEFORE the
no-op/read `continue`:

```python
        # Import refusal (adopt/import design §4.5, Phase-1 floor): an
        # `importing` entry writes a NEW address into state at apply even
        # when its actions are pure no-op — refuse UNCONDITIONALLY (no
        # admission flag exists yet; Phase 2 adds allow_import_of_declared
        # behind the C6 tree-hash proof). `importing: null` is absent. A
        # leftover-inert import block on a later plan carries no
        # `importing` in resource_changes and stays a plain no-op.
        if change.get("importing") is not None:
            return (
                f"{rc.get('address', '<unknown>')}: plan imports a resource "
                f"into state — imports are not admitted (v1 import floor)"
            )
        if actions in (["no-op"], ["read"]):
            continue
```

Update the docstring's ordered list: prepend `(0) an ``importing`` entry is
refused UNCONDITIONALLY (Phase-1 adopt-design floor — even
``allow_create_of_declared`` does not admit it);`.

**Step 4: Run tests**

Run: `uv run pytest -q workers/tofu_apply/tests/`
Expected: ALL PASS.

**Step 5: Commit**

```bash
git add workers/tofu_apply/tofu_runner.py workers/tofu_apply/tests/test_tofu_apply.py
git commit -m "feat: resource_set_guard refuses importing entries (v1 import floor)"
```

---

### Task 6: Full gates

**Step 1:** `uv run pytest -q` — expected ~2412 + ~20 new, 0 failures.
**Step 2:** `uv run ruff check .` — clean.
**Step 3:** `cd frontend && npm run test:unit && npm run check && npm run build` —
unchanged (no frontend edits), but run anyway; CI will.
**Step 4:** Fix anything; commit.

---

### Task 7: Ship (established item workflow)

1. Branch `feat/import-safety-floor`, push, open PR titled
   `feat: import safety floor — imports visible and denied-by-default (adopt design Phase 1)`,
   body links the design doc §2/§7 and names the four enforcement points + five pins.
2. CI green (plan-builder skip expected).
3. `mcp__codex__codex-reply` on the Phase-1 plan-review thread for the
   completed-work review; fold any must-fix.
4. Autonomous squash-merge (`--delete-branch`), per deploy-autonomy memory.
5. Deploy BOTH consumers of the changed lib:
   - coordinator: Cloud Build rebake, then **`gcloud run services
     update-traffic driftscribe-agent --to-revisions=<new>=100`** (traffic is
     pinned — coordinator_deploy_traffic_pinning memory).
   - tofu-apply worker: `gcloud builds submit --config cloudbuild.tofu-apply.yaml`
     (verify the file's exact invocation/substitutions before running).
6. Live verify: `/capabilities` lists 15 denylist rules including
   `import-forbidden-v1` (category `global-v1`); approval page blast note shows
   the new sentence on the next plan; coordinator + worker revision SHAs match
   the merge commit (verify with `gcloud run revisions describe` — never trust
   agent-reported SHAs).
7. Update memory (`clickops_audience_initiative.md` + MEMORY.md index): Phase 1
   shipped, rev pointers, next = Phase 2 (admission + static gate, one PR).

---

## Out of scope (Phase 2+, deliberately)

`ADOPTABLE_RESOURCE_TYPES`; conditional rules (`import-with-changes-` /
`import-type-not-adoptable-` / `import-mixed-plan-` / `import-batch-forbidden-v1`);
all six static-gate authoring rules; `allow_import_of_declared`; import-aware
C6 lifecycle copy in `agent/main.py`; `iac_hcl` stale-comment update; the adopt
recipe; all UI. The RULE_DESCRIPTIONS / blast-note / category pins will be
touched AGAIN in Phase 2 — twice is deliberate (design §7).

---

## Plan-review record (Codex)

Thread `019eb43d-e5d1-7893-b85b-b9ef928aac83`. Round 1: **GO**, zero must-fix.
Three Importants folded: (1) import floor moved BEFORE the unknown-action
`continue` so an importing row with an unaudited action tuple still emits
`import-forbidden-v1` (+ `import_unknown_action.json` fixture); (2) added
`noop_control_plane_service_pass.json` — pins that widening the identity-check
gate does not start firing control-plane rules on plain no-op rows; (3) honesty
bound on the real fixture — only the bucket row is provider-real, derived
fixtures are hand-shaped from it, per-type provider-real probes deferred to the
Phase-3 spec. Nits folded: fixture count corrected; `iac/README.md` "14 rules"
enumeration added to scope; worker `/propose` test harness confirmed sound
(denylist runs before any gate at `workers/tofu_apply/main.py:482`).
