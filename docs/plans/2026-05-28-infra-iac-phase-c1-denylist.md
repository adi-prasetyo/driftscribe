# Phase C1 — Self-protection denylist (plan-JSON policy) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship `tools/iac_plan_denylist.py` — a pure, deterministic, fail-closed Python module that inspects a `tofu show -json plan.tfplan` document and refuses any non-no-op change targeting DriftScribe's own control plane, any IAM/WIF change, or any state-mutating action (`delete` / `forget` / replace). Plus a `python -m tools.iac_plan_denylist <plan.json>` CLI and exhaustive table-driven tests over hand-authored plan-JSON fixtures. **No CI wiring in C1** — wiring waits for C2 (the trusted plan-builder workflow that produces the `plan.json`).

**Architecture:** Mirrors the shape of `tools/iac_static_gate.py` (frozen `Violation(rule, detail)` dataclass, `evaluate(d: DenylistInput) -> list[Violation]`, fail-closed on parse error). Pure functions; CLI wrapper handles I/O. No new third-party dependencies (stdlib `json` only). Module is the v1 floor: intentionally over-inclusive (hard-deny *all* IAM, *all* delete/forget/replace) until a positive allowlist lands in a later phase, per design doc §5.2.

**Tech Stack:** Python 3.12, stdlib `json` + `dataclasses` + `enum`, ruff lint, pytest table-driven. No new runtime dependencies.

> **Codex plan-review trail:** This plan was reviewed once on Codex thread `019e6ce0` (no model parameter — current recommended). Codex returned five blocking findings (incomplete action vocabulary missing `forget`; hallucinated SA account-ids; missing `google_secret_manager_secret_version` coverage; under-specified IAM matching; missing protected-bucket-OBJECT coverage) plus seven importants (legacy `google_cloud_run_service`; before+after identity checking; v1 false-positive documentation; operational secrets list; more edge fixtures; Task-7 too coarse; C4-contract doc). All 5 blockers and all 7 importants are folded into this rev; nits applied. The completed implementation will be re-reviewed on the same thread.

---

## 0. Background — what the design locked

Anchored in `docs/plans/2026-05-27-infra-iac-agent-design.md` §5.2 ("Self-protection denylist") and §6.5 ("re-run `tofu show -json` + denylist" inside the apply worker). The **non-negotiable invariants** from the design (do NOT relitigate):

- **Engine = custom Python over `plan.json`** — no new runtime dep, matches the existing validator idiom (`workers/upgrade_docs/validator.py`). Conftest/OPA is revisited only if policy outgrows it.
- **Runs in two places** — as a required CI check (added in C2, not C1) AND inside the `tofu-apply` worker before apply (added in C4). The library shape MUST work for both — both call `evaluate()` on a parsed plan-JSON dict.
- **Fail-closed** — unparseable JSON, missing `resource_changes` key, malformed entry, or any unknown action vocabulary = a denial Violation, not an exception. A no-op plan (`resource_changes: []`) is legitimate and passes; the file being malformed is not.
- **Hard-deny ALL IAM changes in v1** (Codex rev-2 catch: the rev-1 narrow IAM rule was too permissive). A positive IAM allowlist is a later-phase decision.
- **Hard-deny ALL `delete`, `forget`, and replace actions in v1** — an extra floor; relax later behind the same human gate. `forget` is OpenTofu's "remove from state without destroying" action and is still a state-changing apply step, so it MUST be on the deny list (verified against OpenTofu's JSON-output-format docs).
- **Control-plane self-protection** is the central rule: any non-no-op change targeting the coordinator, the apply/editor/plan-builder services and their SAs, the state/artifact buckets *or their objects*, the approval-HMAC + coordinator-token secrets (incl. `_version` resources), the plan-builder KMS key, or the WIF/CI configuration is denied — even on a green PR.

The module's contract is **read-only and side-effect-free**. It takes a parsed dict (or raw JSON string) and returns a list of `Violation` records. The CLI is a thin wrapper that reads a file, calls `evaluate`, prints violations to stderr, and exits 0 (pass) or 1 (violations) or 2 (usage / I/O error).

**What this C1 slice does NOT do** (deferred to later C sub-phases — called out explicitly so the reviewer doesn't expect them):

- ❌ No CI workflow change — C2 wires the denylist into the trusted plan-builder job once that exists.
- ❌ No `tofu-apply` worker — C4.
- ❌ No HMAC schema, no approval page — C3 / C5.
- ❌ No live GCP interaction — pure library + CLI.
- ❌ No `tofu show -json` invocation — the denylist consumes whatever JSON it's handed; the *producer* (CI / apply worker) runs `tofu show -json`.

---

## 1. Canonical control-plane identities (the targets the denylist protects)

**SA names + service names were re-derived from the codebase after Codex's blocking review** — the rev-1 list of this plan was wrong on both counts.

Grep-verified against `infra/cloudbuild.yaml`, `infra/scripts/setup_prod_project.sh`, `infra/scripts/setup_secrets.sh`, and `infra/scripts/setup_iac_backend.sh` at `main` HEAD `6545be9`.

**Cloud Run services (canonical names)** — `google_cloud_run_v2_service` OR `google_cloud_run_service` resources whose `change.after.name` (or `before.name` for delete/forget) matches any of:
- `driftscribe-agent` (coordinator)
- `driftscribe-reader` (drift reader — Phase < B; **NOT** `driftscribe-infra-reader`)
- `driftscribe-docs`
- `driftscribe-rollback`
- `driftscribe-notifier`
- `driftscribe-upgrade-reader`
- `driftscribe-upgrade-docs`
- `driftscribe-infra-reader` (Phase B)
- Forward-compat (not yet built, additive-safe): `tofu-apply`, `tofu-editor`, `driftscribe-plan-builder`

**Pattern v1**: match on `change.after.name ∈ allowlist` (and `change.before.name` for delete/forget — Codex Important #2). Forward-compat captured by including not-yet-built names in the literal set.

Both `google_cloud_run_v2_service` AND legacy `google_cloud_run_service` are matched (Codex Important #3 — the static gate doesn't ban the legacy resource type, so the denylist must guard it explicitly).

**Service accounts (canonical account_ids)** — `google_service_account` resources, OR via the IAM-rule path. Match by `change.after.account_id` (or by the local part of `change.after.email` if `account_id` is absent — `<aid>@<proj>.iam.gserviceaccount.com`):

- `driftscribe-agent` (coordinator)
- `reader-agent-sa` (drift reader)
- `docs-agent-sa`
- `rollback-agent-sa`
- `notifier-agent-sa`
- `upgrade-reader-sa`
- `upgrade-docs-sa`
- `infra-reader-sa` (Phase B)
- `tofu-plan-builder` (Phase A WIF CI SA)
- `eventarc-trigger-sa`
- Forward-compat: `tofu-apply-sa`, `tofu-editor-sa`

Note: `google_service_account` ALSO matches the IAM-rule (it's an identity resource), so a control-plane SA change emits BOTH `control-plane-sa` AND `iam-change-forbidden-v1`. That's intentional defense-in-depth — if a later phase relaxes the blanket IAM rule, the control-plane-sa rule remains.

**GCS buckets and bucket objects** — `google_storage_bucket` OR `google_storage_bucket_object` (Codex Blocker #5) whose identity references the protected buckets:
- For `google_storage_bucket`: `change.after.name` ends with `-tofu-state` or `-tofu-artifacts`
- For `google_storage_bucket_object`: `change.after.bucket` ends with `-tofu-state` or `-tofu-artifacts` (writing an object INTO a protected bucket is also a control-plane mutation)

**Secrets** — `google_secret_manager_secret` OR `google_secret_manager_secret_version` (Codex Blocker #3) whose identity matches the protected set:
- For `google_secret_manager_secret`: `change.after.secret_id ∈ CONTROL_PLANE_SECRET_IDS`
- For `google_secret_manager_secret_version`: the parent secret id, extracted from `change.after.secret` or `change.after.name`. OpenTofu emits these as full resource paths like `projects/<proj>/secrets/<secret_id>` or `projects/<proj>/secrets/<secret_id>/versions/<n>`. Extraction helper splits on `/secrets/` and takes the second segment up to the next `/`.

`CONTROL_PLANE_SECRET_IDS` (codebase-derived per Codex Important #4 — include all operational secrets, not just design-mandated ones; biasing toward "any operational secret should not be redirected by IaC without explicit design intent"):
- `approval-hmac-key` (rollback / will be reused or paralleled by C3)
- `coordinator-shared-token`
- `plan-hmac-key` (forward-compat for C3)
- `github-pat`
- `docs-agent-github-pat`
- `upgrade-reader-github-pat`
- `upgrade-docs-github-pat`
- `developer-knowledge-api-key`
- `driftscribe-webhook-url`

**KMS** — `google_kms_crypto_key` OR `google_kms_key_ring` whose identity matches:
- For crypto keys: `change.after.name == "tofu-state"` (Phase A bootstrap key), or future `plan-hmac-key` if KMS-backed
- For key rings: `change.after.name == "driftscribe-tofu"` (the ring that holds tofu-state)

**WIF** — any resource of type `google_iam_workload_identity_pool` or `google_iam_workload_identity_pool_provider`. These also match the IAM rule (intentional dual-emit).

**IAM (the catch-all)** — Codex Blocker #4 made this more general. v1 matches:
- Any resource type that satisfies `rtype.startswith("google_") and "_iam_" in rtype` — catches `google_*_iam_member`/`_binding`/`_policy` for project, service_account, storage_bucket, kms_*, secret_manager_secret, cloud_run_v2_service, cloud_run_service, pubsub_topic, pubsub_subscription, artifact_registry_repository, folder, organization, billing_account, compute_*, dns_*, datastore_*, etc.
- Plus an explicit literal set for IAM-identity types that don't carry `_iam_` in their name: `google_service_account`, `google_service_account_key`, `google_project_iam_audit_config`, `google_project_iam_custom_role`, `google_organization_iam_custom_role`, `google_iam_workload_identity_pool`, `google_iam_workload_identity_pool_provider`.

---

## 2. plan.json structure the denylist consumes (canonical, from OpenTofu docs)

```json
{
  "format_version": "1.2",
  "terraform_version": "1.12.0",
  "resource_changes": [
    {
      "address": "google_cloud_run_v2_service.payment_demo",
      "mode": "managed",
      "type": "google_cloud_run_v2_service",
      "name": "payment_demo",
      "provider_name": "registry.opentofu.org/hashicorp/google",
      "change": {
        "actions": ["update"],
        "before": { "...": "..." },
        "after":  { "name": "payment-demo", "...": "..." },
        "after_unknown": {}
      }
    }
  ]
}
```

**Critical fields the denylist reads** (and nothing else):
- `resource_changes[].type` — resource type string.
- `resource_changes[].address` — full HCL address (used in violation `detail`).
- `resource_changes[].change.actions` — list of action strings.
- `resource_changes[].change.after` — post-change attribute dict (used to read e.g. `name`, `email`, `account_id`, `secret_id`, `secret`, `bucket` for identity matching). May be `null` (delete/forget) or partly `null` (computed).
- `resource_changes[].change.before` — pre-change attribute dict (used for `delete`/`forget` identity).

**v1 simplification**: the denylist does NOT inspect `prior_state`, `planned_values`, `configuration`, or `output_changes` — only `resource_changes`.

### OpenTofu action vocabulary (verified per Codex Blocker #1)

Per the OpenTofu JSON-output-format docs:
- `["no-op"]` — no change
- `["read"]` — data-source refresh (read-only)
- `["create"]` — resource will be created
- `["update"]` — resource will be updated in place
- `["delete"]` — resource will be destroyed
- `["forget"]` — resource will be removed from state WITHOUT being destroyed (new in OpenTofu ≥ 1.7; ours is 1.12.0)
- `["delete", "create"]` — replace (delete-first ordering)
- `["create", "delete"]` — replace (create-first ordering)

**Any tuple not in this set is treated as an unknown action and emits `unknown-action-forbidden-v1`** — defense-in-depth against a future OpenTofu version emitting a new action shape we haven't audited.

`no-op` and `read` are the only non-mutation actions. Everything else (`create`, `update`, `delete`, `forget`, both replaces) is a mutation; all but `create` and `update` are categorically banned in v1 even on unrelated resources.

---

## 3. Module shape (target API)

```python
# tools/iac_plan_denylist.py
from dataclasses import dataclass
import json
import sys
from typing import Iterable

@dataclass(frozen=True)
class Violation:
    rule: str       # short machine id
    detail: str     # human message naming the resource address + action

@dataclass(frozen=True)
class DenylistInput:
    plan: dict      # parsed plan.json (the entire document)

def evaluate(di: DenylistInput) -> list[Violation]:
    """Return all violations (empty = pass). Fail-closed: any structural
    surprise yields a Violation, never an exception."""
    ...

def load_plan_json(text: str) -> tuple[dict | None, Violation | None]:
    """Parse plan.json. Returns (parsed, None) on success or
    (None, Violation('plan-json-unparseable', ...)) on failure."""
    ...
```

Constants (module-level, frozensets and tuples; one source of truth — tests guard their identity, not content):

```python
CLOUD_RUN_SERVICE_TYPES = frozenset({
    "google_cloud_run_v2_service",
    "google_cloud_run_service",   # legacy v1 — explicitly covered
})

CONTROL_PLANE_SERVICE_NAMES = frozenset({
    "driftscribe-agent",
    "driftscribe-reader",          # drift reader — distinct from infra-reader
    "driftscribe-docs",
    "driftscribe-rollback",
    "driftscribe-notifier",
    "driftscribe-upgrade-reader",
    "driftscribe-upgrade-docs",
    "driftscribe-infra-reader",    # Phase B
    # Forward-compat (additive-safe — names listed before they exist):
    "tofu-apply", "tofu-editor", "driftscribe-plan-builder",
})

CONTROL_PLANE_SA_ACCOUNT_IDS = frozenset({
    "driftscribe-agent",
    "reader-agent-sa", "docs-agent-sa", "rollback-agent-sa",
    "notifier-agent-sa", "upgrade-reader-sa", "upgrade-docs-sa",
    "infra-reader-sa", "tofu-plan-builder", "eventarc-trigger-sa",
    # Forward-compat:
    "tofu-apply-sa", "tofu-editor-sa",
})

CONTROL_PLANE_BUCKET_SUFFIXES = ("-tofu-state", "-tofu-artifacts")

GCS_BUCKET_TYPES = frozenset({"google_storage_bucket"})
GCS_BUCKET_OBJECT_TYPES = frozenset({"google_storage_bucket_object"})

CONTROL_PLANE_SECRET_IDS = frozenset({
    "approval-hmac-key", "coordinator-shared-token",
    "plan-hmac-key",  # forward-compat (C3)
    "github-pat", "docs-agent-github-pat",
    "upgrade-reader-github-pat", "upgrade-docs-github-pat",
    "developer-knowledge-api-key", "driftscribe-webhook-url",
})
SECRET_TYPES = frozenset({"google_secret_manager_secret"})
SECRET_VERSION_TYPES = frozenset({"google_secret_manager_secret_version"})

CONTROL_PLANE_KMS_KEY_NAMES = frozenset({"tofu-state"})
CONTROL_PLANE_KMS_KEYRING_NAMES = frozenset({"driftscribe-tofu"})
KMS_KEY_TYPES = frozenset({"google_kms_crypto_key"})
KMS_KEYRING_TYPES = frozenset({"google_kms_key_ring"})

WIF_RESOURCE_TYPES = frozenset({
    "google_iam_workload_identity_pool",
    "google_iam_workload_identity_pool_provider",
})

IAM_EXTRA_TYPES = frozenset({
    "google_service_account", "google_service_account_key",
    "google_project_iam_audit_config",
    "google_project_iam_custom_role",
    "google_organization_iam_custom_role",
}) | WIF_RESOURCE_TYPES

# OpenTofu action vocabulary — anything not in here is "unknown" (fail-closed).
NO_OP_ACTION_TUPLES   = frozenset({("no-op",), ("read",)})
MUTATION_KNOWN_TUPLES = frozenset({
    ("create",), ("update",),
    ("delete",), ("forget",),
    ("delete", "create"), ("create", "delete"),
})
DELETE_ACTION_TUPLES  = frozenset({("delete",)})
FORGET_ACTION_TUPLES  = frozenset({("forget",)})
REPLACE_ACTION_TUPLES = frozenset({("delete", "create"), ("create", "delete")})
ALL_KNOWN_TUPLES      = NO_OP_ACTION_TUPLES | MUTATION_KNOWN_TUPLES
```

### Rule IDs (14 total)

- `plan-json-unparseable` — JSON parse failed, or top-level is not an object
- `plan-json-missing-resource-changes` — no `resource_changes` key, or it's not a list
- `plan-json-malformed-change` — a `resource_changes[]` entry is missing required fields (`type`, `change`, `change.actions`) OR a protected resource type has no identity field in `before`/`after`
- `control-plane-service` — non-no-op change to a Cloud Run service in the protected set
- `control-plane-sa` — non-no-op change to a control-plane service account
- `control-plane-bucket` — non-no-op change to a `-tofu-state` / `-tofu-artifacts` bucket or an object in one
- `control-plane-secret` — non-no-op change to a protected secret (incl. `_version`)
- `control-plane-kms` — non-no-op change to a protected KMS key or key ring
- `wif-config-change` — non-no-op change to a WIF pool/provider
- `iam-change-forbidden-v1` — non-no-op change to any IAM resource type
- `delete-action-forbidden-v1` — action is `["delete"]`
- `forget-action-forbidden-v1` — action is `["forget"]`
- `replace-action-forbidden-v1` — action is replace (delete+create or create+delete)
- `unknown-action-forbidden-v1` — action tuple not in the documented OpenTofu vocabulary

---

## 4. Identity-matching strategy

This is the load-bearing edge case. Documented once so reviewers don't re-derive it:

- **On `create`** (`after` populated, `before` null): match identity from `after`. If a required key (`name`/`email`/`account_id`/`secret_id`/`secret`/`bucket`) is missing from `after` AND the resource type is one we identity-match on, that's `plan-json-malformed-change` — defensive bias-to-deny (Codex Important #1: this is an accepted v1 false-positive — unknown identity on an identity-matched type denies because it could hide a protected suffix/name).
- **On `update`**: match identity from `after`. **Also check `before`** for control-plane rules so a "rename away from a protected name" doesn't escape the rule via `after` alone (Codex Important #2). The `before+after` check is cheap: the helper returns `(before_match, after_match)` and the rule fires if either matches.
- **On `delete` / `forget`** (`after` null, `before` populated): match identity from `before`. The `delete-action-forbidden-v1` / `forget-action-forbidden-v1` rule fires regardless of identity (we hard-deny in v1), but identity match enriches the violation `detail` text.
- **On `replace`**: both `before` and `after` are populated. Check both. The `replace-action-forbidden-v1` rule fires regardless.
- **Unknown values** in `after_unknown`: if the protected identity field is in `after_unknown` (i.e. `after.<field>` is missing / unknown), defensive bias-to-deny — emit `plan-json-malformed-change`.

This bias is the v1 floor. C4 (the apply worker) re-runs the denylist against the same `plan.json` with the same bias; the plan-builder in C2 runs it too; so a malformed plan is rejected three times before harming anything.

---

## 5. Test fixtures — hand-authored minimal plan-JSON files

Location: `tests/fixtures/iac_plan_denylist/`. Each single-rule fixture exercises one rule (positive or negative-pass). Dedicated multi-rule fixtures exist for explicit aggregation (Codex nit on §5 wording — the "no shared fixtures" principle is for single-rule fixtures; multi-rule cases have their own files). Tests reference fixtures by filename.

Each fixture carries a top-level `_test_intent` field (string, JSON-safe) documenting purpose.

| Fixture | Purpose | Expected result |
|---|---|---|
| `benign_no_op.json` | `resource_changes: []` (empty list) | pass |
| `benign_payment_demo_update.json` | `update` to `google_cloud_run_v2_service.payment_demo` (name `payment-demo`, not in protected set), no IAM, not delete/forget/replace | pass |
| `benign_create_unprotected_secret.json` | `create` of `google_secret_manager_secret` with `secret_id="payment-demo-test-flag"` | pass |
| `benign_create_unprotected_bucket.json` | `create` of `google_storage_bucket` with `name="payment-demo-uploads"` | pass |
| `unparseable_empty_file.json` | zero bytes | `plan-json-unparseable` |
| `unparseable_not_object.json` | `[]` (JSON array) | `plan-json-unparseable` |
| `missing_resource_changes.json` | `{"format_version": "1.2"}` | `plan-json-missing-resource-changes` |
| `resource_changes_not_list.json` | `resource_changes` as object | `plan-json-missing-resource-changes` |
| `resource_changes_entry_not_dict.json` | `resource_changes: [null]` | `plan-json-malformed-change` |
| `change_not_dict.json` | `resource_changes[].change` is a string | `plan-json-malformed-change` |
| `malformed_change_missing_actions.json` | entry missing `change.actions` | `plan-json-malformed-change` |
| `malformed_change_missing_type.json` | entry missing `type` | `plan-json-malformed-change` |
| `actions_not_all_strings.json` | `actions: ["update", 7]` | `plan-json-malformed-change` |
| `unknown_action_vocabulary.json` | `actions: ["yeet"]` (not in OpenTofu set) | `unknown-action-forbidden-v1` |
| `read_action_is_pass.json` | `read` on a control-plane service | pass (no-op) |
| `control_plane_coordinator_update.json` | `update` Cloud Run service `driftscribe-agent` | `control-plane-service` |
| `control_plane_reader_update.json` | `update` Cloud Run service `driftscribe-reader` | `control-plane-service` |
| `control_plane_infra_reader_update.json` | `update` Cloud Run service `driftscribe-infra-reader` | `control-plane-service` |
| `control_plane_legacy_v1_service_update.json` | `update` to legacy `google_cloud_run_service` with `name="driftscribe-agent"` | `control-plane-service` |
| `control_plane_cloudrun_delete_via_before.json` | `delete` of a Cloud Run service — identity in `before.name="driftscribe-agent"` only | `control-plane-service` AND `delete-action-forbidden-v1` |
| `control_plane_sa_update_account_id.json` | `update` to `google_service_account` with `after.account_id="reader-agent-sa"` | `control-plane-sa` AND `iam-change-forbidden-v1` |
| `control_plane_sa_update_email_only.json` | `update` to `google_service_account` with `after.email="rollback-agent-sa@…"` (no `account_id`) | `control-plane-sa` AND `iam-change-forbidden-v1` |
| `control_plane_state_bucket_update.json` | `update` bucket `driftscribe-hack-2026-tofu-state` | `control-plane-bucket` |
| `control_plane_artifact_bucket_create.json` | `create` bucket `driftscribe-hack-2026-tofu-artifacts` | `control-plane-bucket` |
| `control_plane_state_bucket_object_create.json` | `create` of `google_storage_bucket_object` with `after.bucket="driftscribe-hack-2026-tofu-state"` | `control-plane-bucket` |
| `control_plane_artifact_bucket_object_update.json` | `update` of `google_storage_bucket_object` in artifact bucket | `control-plane-bucket` |
| `benign_unprotected_bucket_object.json` | object in `payment-demo-uploads` bucket | pass |
| `control_plane_hmac_secret_update.json` | `update` `google_secret_manager_secret` with `secret_id="approval-hmac-key"` | `control-plane-secret` |
| `control_plane_secret_version_create.json` | `create` of `google_secret_manager_secret_version` for `approval-hmac-key` (resource path in `after.secret`) | `control-plane-secret` |
| `benign_unprotected_secret_version.json` | secret_version for `payment-demo-test-flag` | pass |
| `control_plane_kms_update.json` | `update` `google_kms_crypto_key` with `name="tofu-state"` | `control-plane-kms` |
| `control_plane_kms_keyring_update.json` | `update` `google_kms_key_ring` with `name="driftscribe-tofu"` | `control-plane-kms` |
| `wif_pool_update.json` | `update` to `google_iam_workload_identity_pool` | `wif-config-change` AND `iam-change-forbidden-v1` |
| `wif_provider_create.json` | `create` of `google_iam_workload_identity_pool_provider` | `wif-config-change` AND `iam-change-forbidden-v1` |
| `iam_project_binding_create.json` | `create` of `google_project_iam_binding` | `iam-change-forbidden-v1` |
| `iam_storage_binding_update.json` | `update` to `google_storage_bucket_iam_member` | `iam-change-forbidden-v1` |
| `iam_run_invoker_grant.json` | `create` of `google_cloud_run_v2_service_iam_member` | `iam-change-forbidden-v1` |
| `iam_folder_binding_create.json` | `create` of `google_folder_iam_binding` (covered by `_iam_` substring) | `iam-change-forbidden-v1` |
| `delete_unprotected_resource.json` | `delete` of `google_storage_bucket_object.demo_uploads` (unprotected bucket) | `delete-action-forbidden-v1` |
| `forget_unprotected_resource.json` | `forget` of an unrelated resource | `forget-action-forbidden-v1` |
| `replace_unprotected_resource.json` | `["delete","create"]` of unrelated resource | `replace-action-forbidden-v1` |
| `replace_create_first_unprotected.json` | `["create","delete"]` | `replace-action-forbidden-v1` |
| `multi_violations_sa_delete.json` | SA delete with `before.account_id="driftscribe-agent"` — fires `control-plane-sa` + `iam-change-forbidden-v1` + `delete-action-forbidden-v1` | three rules |
| `malformed_protected_cloud_run_no_name.json` | `create` of `google_cloud_run_v2_service` with `after={}` | `plan-json-malformed-change` |
| `malformed_protected_secret_version_no_path.json` | `secret_version` create with `after={"name":"something-without-/secrets/-in-it"}` | `plan-json-malformed-change` |
| `update_rename_away_from_protected.json` | `update` with `before.name="driftscribe-agent"`, `after.name="something-else"` — proves `before` is also checked | `control-plane-service` |

**35 fixtures total** (up from 25 in rev-1). Each is ~15-40 lines of minimal valid plan.json shape; no fixture is generated programmatically.

---

## 6. Step-by-step tasks (TDD; each 2–5 minutes; Task 7 split per Codex Important #6)

### Task 1: Worktree baseline + plan committed

**Files:**
- Already at: `/home/adi/driftscribe/.worktrees/phase-c1-denylist/` on branch `infra/phase-c1-denylist`
- Plan: `docs/plans/2026-05-28-infra-iac-phase-c1-denylist.md` (this file)

**Step 1: Sync deps**

```bash
cd /home/adi/driftscribe/.worktrees/phase-c1-denylist
uv sync --all-extras
```

Verify: `uv run pytest tests/unit/test_iac_static_gate.py -q` → prior gate tests green.

**Step 2: Commit the plan**

```bash
git add docs/plans/2026-05-28-infra-iac-phase-c1-denylist.md
git commit -m "docs(plan): Phase C1 — self-protection denylist (plan-JSON policy)

Anchored in 2026-05-27-infra-iac-agent-design.md §5.2. Scope: pure
module + CLI + table-driven fixtures only; CI wiring lands in C2 with
the trusted plan-builder job that produces plan.json. Reviewed by Codex
once (thread 019e6ce0); all five blockers folded in.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 2: Failing module test — Violation dataclass + evaluate signature + empty-plan pass

**Files:**
- Create: `tests/unit/test_iac_plan_denylist.py`
- Create: `tools/iac_plan_denylist.py`

**Step 1: Write the failing test**

```python
# tests/unit/test_iac_plan_denylist.py
import dataclasses
import pytest

from tools import iac_plan_denylist  # noqa: F401
from tools.iac_plan_denylist import DenylistInput, Violation, evaluate


def test_module_imports():
    assert iac_plan_denylist is not None


def test_violation_is_frozen_dataclass():
    v = Violation(rule="x", detail="y")
    assert dataclasses.is_dataclass(v)
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.rule = "z"  # type: ignore[misc]


def test_empty_plan_with_empty_resource_changes_passes():
    di = DenylistInput(plan={"format_version": "1.2", "resource_changes": []})
    assert evaluate(di) == []
```

**Step 2: Verify failure**

```bash
uv run pytest tests/unit/test_iac_plan_denylist.py -q
```

Expected: `ModuleNotFoundError`.

**Step 3: Skeleton module**

```python
# tools/iac_plan_denylist.py
"""Self-protection denylist for DriftScribe IaC plan.json (design §5.2)."""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class Violation:
    rule: str
    detail: str


@dataclass(frozen=True)
class DenylistInput:
    plan: dict


def evaluate(di: DenylistInput) -> list[Violation]:
    return []
```

**Step 4: Verify pass** → 3 passed.

**Step 5: Commit**

```bash
git commit -m "feat(c1): scaffold iac_plan_denylist module (skeleton + empty evaluate)"
```

---

### Task 3: load_plan_json + structural rules

**Files:** fixtures `unparseable_empty_file.json`, `unparseable_not_object.json`, `missing_resource_changes.json`, `resource_changes_not_list.json`, `resource_changes_entry_not_dict.json`, `change_not_dict.json`; tests + module.

**Step 1: Fixtures**

```bash
mkdir -p tests/fixtures/iac_plan_denylist
```

Write each fixture per §5. `unparseable_empty_file.json` is created via `printf '' > .../unparseable_empty_file.json`.

**Step 2: Failing tests**

Parametrized `test_load_plan_json_handles_unparseable` over the two `unparseable_*` fixtures; `test_missing_resource_changes_*` over the next two; `test_entry_or_change_not_dict_is_malformed` over the last two.

**Step 3: Implement**

```python
import json


def load_plan_json(text: str) -> tuple[dict | None, Violation | None]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        return None, Violation("plan-json-unparseable", f"json decode error: {e}")
    if not isinstance(parsed, dict):
        return None, Violation(
            "plan-json-unparseable", f"root is {type(parsed).__name__}, expected object",
        )
    return parsed, None


def evaluate(di: DenylistInput) -> list[Violation]:
    violations: list[Violation] = []
    rcs = di.plan.get("resource_changes")
    if not isinstance(rcs, list):
        violations.append(Violation(
            "plan-json-missing-resource-changes",
            f"resource_changes is {type(rcs).__name__}, expected list",
        ))
        return violations
    for rc in rcs:
        if not isinstance(rc, dict):
            violations.append(Violation(
                "plan-json-malformed-change",
                f"resource_changes entry is {type(rc).__name__}, expected object",
            ))
            continue
        change = rc.get("change")
        if not isinstance(change, dict):
            address = rc.get("address", "<unknown>")
            violations.append(Violation(
                "plan-json-malformed-change",
                f"{address}: change is {type(change).__name__}, expected object",
            ))
            continue
    return violations
```

**Step 4: Verify pass** → ~10 tests green.

**Step 5: Commit** `feat(c1): load_plan_json + structural rules`.

---

### Task 4: malformed-change + unknown-action + helper for iterating

**Files:** fixtures `malformed_change_missing_actions.json`, `malformed_change_missing_type.json`, `actions_not_all_strings.json`, `unknown_action_vocabulary.json`; tests + module.

**Step 1: Fixtures** per §5.

**Step 2: Failing tests** assert respective rules emit.

**Step 3: Implement**

```python
NO_OP_ACTION_TUPLES = frozenset({("no-op",), ("read",)})
DELETE_ACTION_TUPLES = frozenset({("delete",)})
FORGET_ACTION_TUPLES = frozenset({("forget",)})
REPLACE_ACTION_TUPLES = frozenset({("delete", "create"), ("create", "delete")})
MUTATION_KNOWN_TUPLES = frozenset({("create",), ("update",)}) | DELETE_ACTION_TUPLES | FORGET_ACTION_TUPLES | REPLACE_ACTION_TUPLES
ALL_KNOWN_TUPLES = NO_OP_ACTION_TUPLES | MUTATION_KNOWN_TUPLES


def _iter_resource_changes(plan: dict):
    """Yield (rc, type, actions_tuple_or_None) per well-formed entry.
    None actions_tuple means malformed; caller emits the rule."""
    for rc in plan.get("resource_changes", []) or []:
        if not isinstance(rc, dict):
            yield rc, None, None
            continue
        rtype = rc.get("type")
        change = rc.get("change") if isinstance(rc.get("change"), dict) else None
        if change is None or not isinstance(rtype, str):
            yield rc, rtype, None
            continue
        actions = change.get("actions")
        if not isinstance(actions, list) or not all(isinstance(a, str) for a in actions):
            yield rc, rtype, None
            continue
        yield rc, rtype, tuple(actions)
```

Extend `evaluate`'s loop (now using the helper):

```python
def _is_mutation(actions: tuple[str, ...]) -> bool:
    return actions not in NO_OP_ACTION_TUPLES


def evaluate(di: DenylistInput) -> list[Violation]:
    violations: list[Violation] = []
    rcs = di.plan.get("resource_changes")
    if not isinstance(rcs, list):
        violations.append(Violation("plan-json-missing-resource-changes", ...))
        return violations
    for rc, rtype, actions in _iter_resource_changes(di.plan):
        address = rc.get("address", "<unknown>") if isinstance(rc, dict) else "<unknown>"
        if actions is None:
            violations.append(Violation(
                "plan-json-malformed-change",
                f"{address}: missing or malformed type / change.actions",
            ))
            continue
        if actions not in ALL_KNOWN_TUPLES:
            violations.append(Violation(
                "unknown-action-forbidden-v1",
                f"{address}: action {list(actions)!r} not in OpenTofu vocabulary",
            ))
            continue
    return violations
```

**Step 4: Verify pass** (~14 tests green).

**Step 5: Commit** `feat(c1): malformed-change + unknown-action rules`.

---

### Task 5: delete / forget / replace hard-deny rules

**Fixtures:** per §5 — `delete_unprotected_resource.json`, `forget_unprotected_resource.json`, `replace_unprotected_resource.json`, `replace_create_first_unprotected.json`, `benign_payment_demo_update.json`, `read_action_is_pass.json`, `benign_no_op.json`, `benign_create_unprotected_secret.json`, `benign_create_unprotected_bucket.json`.

**Failing tests:** parametrized benign-fixture pass, then delete/forget/replace assertions.

**Implementation:** in the main loop, after the unknown-action filter:

```python
if actions in DELETE_ACTION_TUPLES:
    violations.append(Violation("delete-action-forbidden-v1",
                                f"{address}: action {list(actions)!r} forbidden in v1"))
    # fall through to identity-based rules
if actions in FORGET_ACTION_TUPLES:
    violations.append(Violation("forget-action-forbidden-v1",
                                f"{address}: action {list(actions)!r} forbidden in v1"))
    # fall through
if actions in REPLACE_ACTION_TUPLES:
    violations.append(Violation("replace-action-forbidden-v1",
                                f"{address}: action {list(actions)!r} (replace) forbidden in v1"))
    # fall through
```

**Important:** do NOT `continue` after firing one of these — fall through to identity rules (Tasks 6a–6d) so the same RC can emit multiple violations. `multi_violations_sa_delete.json` (Task 9) catches a regression.

**Verify + commit**: `feat(c1): delete / forget / replace hard-deny`.

---

### Task 6a: Control-plane Cloud Run service rule (incl. legacy v1 + before-check + delete-via-before)

**Fixtures:** `control_plane_coordinator_update.json`, `control_plane_reader_update.json`, `control_plane_infra_reader_update.json`, `control_plane_legacy_v1_service_update.json`, `control_plane_cloudrun_delete_via_before.json`, `update_rename_away_from_protected.json`.

**Failing tests:** parametrized — each fixture must emit `control-plane-service` (the delete fixture also emits `delete-action-forbidden-v1`).

**Implementation:**

```python
CLOUD_RUN_SERVICE_TYPES = frozenset({
    "google_cloud_run_v2_service", "google_cloud_run_service",
})
CONTROL_PLANE_SERVICE_NAMES = frozenset({
    "driftscribe-agent", "driftscribe-reader", "driftscribe-docs",
    "driftscribe-rollback", "driftscribe-notifier",
    "driftscribe-upgrade-reader", "driftscribe-upgrade-docs",
    "driftscribe-infra-reader",
    "tofu-apply", "tofu-editor", "driftscribe-plan-builder",
})


def _identity_dicts(rc: dict, actions: tuple[str, ...]) -> tuple[dict, dict]:
    """Return (before_dict, after_dict), each possibly empty."""
    change = rc.get("change") or {}
    before = change.get("before") if isinstance(change.get("before"), dict) else {}
    after  = change.get("after")  if isinstance(change.get("after"), dict) else {}
    return before, after


def _check_control_plane_service(rc, rtype, actions, before, after, violations):
    if rtype not in CLOUD_RUN_SERVICE_TYPES: return
    before_name = before.get("name") if isinstance(before, dict) else None
    after_name = after.get("name") if isinstance(after, dict) else None
    if before_name is None and after_name is None:
        # Protected type with no identity in either direction — defensive deny.
        violations.append(Violation(
            "plan-json-malformed-change",
            f"{rc.get('address')}: {rtype} has no name in before/after",
        ))
        return
    matched = ((before_name in CONTROL_PLANE_SERVICE_NAMES) or
               (after_name in CONTROL_PLANE_SERVICE_NAMES))
    if matched:
        violations.append(Violation(
            "control-plane-service",
            f"{rc.get('address')}: Cloud Run service {(after_name or before_name)!r} "
            f"is control plane (actions={list(actions)})",
        ))
```

Call from main loop only when `_is_mutation(actions)`.

**Verify + commit**: `feat(c1): control-plane Cloud Run service rule (+ legacy v1, +before)`.

---

### Task 6b: Control-plane SA rule (account_id + email-only matching)

**Fixtures:** `control_plane_sa_update_account_id.json`, `control_plane_sa_update_email_only.json`.

**Tests + impl:**

```python
CONTROL_PLANE_SA_ACCOUNT_IDS = frozenset({
    "driftscribe-agent", "reader-agent-sa", "docs-agent-sa",
    "rollback-agent-sa", "notifier-agent-sa",
    "upgrade-reader-sa", "upgrade-docs-sa",
    "infra-reader-sa", "tofu-plan-builder", "eventarc-trigger-sa",
    "tofu-apply-sa", "tofu-editor-sa",
})


def _sa_account_id(identity: dict) -> str | None:
    if not isinstance(identity, dict): return None
    aid = identity.get("account_id")
    if isinstance(aid, str): return aid
    email = identity.get("email")
    if isinstance(email, str) and "@" in email:
        return email.split("@", 1)[0]
    return None


def _check_control_plane_sa(rc, rtype, actions, before, after, violations):
    if rtype != "google_service_account": return
    bid, aid = _sa_account_id(before), _sa_account_id(after)
    if bid is None and aid is None:
        violations.append(Violation(
            "plan-json-malformed-change",
            f"{rc.get('address')}: google_service_account has no account_id/email",
        ))
        return
    if (bid in CONTROL_PLANE_SA_ACCOUNT_IDS) or (aid in CONTROL_PLANE_SA_ACCOUNT_IDS):
        violations.append(Violation(
            "control-plane-sa",
            f"{rc.get('address')}: SA {(aid or bid)!r} is control plane (actions={list(actions)})",
        ))
```

**Verify + commit**: `feat(c1): control-plane SA rule (incl. email-only match)`.

---

### Task 6c: Control-plane bucket + bucket-object rules

**Fixtures:** `control_plane_state_bucket_update.json`, `control_plane_artifact_bucket_create.json`, `control_plane_state_bucket_object_create.json`, `control_plane_artifact_bucket_object_update.json`, `benign_unprotected_bucket_object.json`, `benign_create_unprotected_bucket.json`.

**Tests + impl:**

```python
CONTROL_PLANE_BUCKET_SUFFIXES = ("-tofu-state", "-tofu-artifacts")


def _is_protected_bucket_name(name) -> bool:
    return isinstance(name, str) and any(name.endswith(s) for s in CONTROL_PLANE_BUCKET_SUFFIXES)


def _check_control_plane_bucket(rc, rtype, actions, before, after, violations):
    if rtype == "google_storage_bucket":
        before_name, after_name = before.get("name"), after.get("name")
        if not isinstance(before_name, str) and not isinstance(after_name, str):
            violations.append(Violation("plan-json-malformed-change",
                f"{rc.get('address')}: google_storage_bucket has no name"))
            return
        if _is_protected_bucket_name(before_name) or _is_protected_bucket_name(after_name):
            violations.append(Violation("control-plane-bucket",
                f"{rc.get('address')}: protected bucket "
                f"{(after_name or before_name)!r} (actions={list(actions)})"))
    elif rtype == "google_storage_bucket_object":
        before_b, after_b = before.get("bucket"), after.get("bucket")
        if not isinstance(before_b, str) and not isinstance(after_b, str):
            violations.append(Violation("plan-json-malformed-change",
                f"{rc.get('address')}: google_storage_bucket_object has no bucket"))
            return
        if _is_protected_bucket_name(before_b) or _is_protected_bucket_name(after_b):
            violations.append(Violation("control-plane-bucket",
                f"{rc.get('address')}: object in protected bucket "
                f"{(after_b or before_b)!r} (actions={list(actions)})"))
```

**Verify + commit**: `feat(c1): control-plane bucket + bucket-object rules`.

---

### Task 6d: Control-plane secret (+ secret_version) + KMS (+ key_ring)

**Fixtures:** `control_plane_hmac_secret_update.json`, `control_plane_secret_version_create.json`, `benign_unprotected_secret_version.json`, `benign_create_unprotected_secret.json`, `control_plane_kms_update.json`, `control_plane_kms_keyring_update.json`, `malformed_protected_secret_version_no_path.json`.

**Tests + impl:**

```python
CONTROL_PLANE_SECRET_IDS = frozenset({
    "approval-hmac-key", "coordinator-shared-token", "plan-hmac-key",
    "github-pat", "docs-agent-github-pat",
    "upgrade-reader-github-pat", "upgrade-docs-github-pat",
    "developer-knowledge-api-key", "driftscribe-webhook-url",
})
CONTROL_PLANE_KMS_KEY_NAMES = frozenset({"tofu-state"})
CONTROL_PLANE_KMS_KEYRING_NAMES = frozenset({"driftscribe-tofu"})


def _secret_id_from_version_path(value) -> str | None:
    """OpenTofu emits secret_version `secret` / `name` as
    `projects/<p>/secrets/<id>` or `.../secrets/<id>/versions/<n>`."""
    if not isinstance(value, str) or "/secrets/" not in value: return None
    rest = value.split("/secrets/", 1)[1]
    return rest.split("/", 1)[0] or None


def _check_control_plane_secret(rc, rtype, actions, before, after, violations):
    if rtype == "google_secret_manager_secret":
        bid, aid = before.get("secret_id"), after.get("secret_id")
        if not isinstance(bid, str) and not isinstance(aid, str):
            violations.append(Violation("plan-json-malformed-change",
                f"{rc.get('address')}: google_secret_manager_secret has no secret_id"))
            return
        if (bid in CONTROL_PLANE_SECRET_IDS) or (aid in CONTROL_PLANE_SECRET_IDS):
            violations.append(Violation("control-plane-secret",
                f"{rc.get('address')}: secret {(aid or bid)!r} (actions={list(actions)})"))
    elif rtype == "google_secret_manager_secret_version":
        before_id = (_secret_id_from_version_path(before.get("secret")) or
                     _secret_id_from_version_path(before.get("name")))
        after_id = (_secret_id_from_version_path(after.get("secret")) or
                    _secret_id_from_version_path(after.get("name")))
        if before_id is None and after_id is None:
            violations.append(Violation("plan-json-malformed-change",
                f"{rc.get('address')}: secret_version path has no /secrets/<id>"))
            return
        if (before_id in CONTROL_PLANE_SECRET_IDS) or (after_id in CONTROL_PLANE_SECRET_IDS):
            violations.append(Violation("control-plane-secret",
                f"{rc.get('address')}: version of {(after_id or before_id)!r} "
                f"(actions={list(actions)})"))


def _check_control_plane_kms(rc, rtype, actions, before, after, violations):
    if rtype == "google_kms_crypto_key":
        names = CONTROL_PLANE_KMS_KEY_NAMES
    elif rtype == "google_kms_key_ring":
        names = CONTROL_PLANE_KMS_KEYRING_NAMES
    else:
        return
    bn, an = before.get("name"), after.get("name")
    if not isinstance(bn, str) and not isinstance(an, str):
        violations.append(Violation("plan-json-malformed-change",
            f"{rc.get('address')}: {rtype} has no name"))
        return
    if (bn in names) or (an in names):
        violations.append(Violation("control-plane-kms",
            f"{rc.get('address')}: protected KMS resource {(an or bn)!r} "
            f"(actions={list(actions)})"))
```

**Verify + commit**: `feat(c1): control-plane secret (+_version) + KMS rules`.

---

### Task 7: WIF + IAM rules

**Fixtures:** `wif_pool_update.json`, `wif_provider_create.json`, `iam_project_binding_create.json`, `iam_storage_binding_update.json`, `iam_run_invoker_grant.json`, `iam_folder_binding_create.json`.

**Impl:**

```python
WIF_RESOURCE_TYPES = frozenset({
    "google_iam_workload_identity_pool",
    "google_iam_workload_identity_pool_provider",
})
IAM_EXTRA_TYPES = frozenset({
    "google_service_account", "google_service_account_key",
    "google_project_iam_audit_config",
    "google_project_iam_custom_role",
    "google_organization_iam_custom_role",
}) | WIF_RESOURCE_TYPES


def _is_iam_type(rtype: str) -> bool:
    if rtype in IAM_EXTRA_TYPES: return True
    return rtype.startswith("google_") and "_iam_" in rtype


def _check_wif(rc, rtype, actions, violations):
    if rtype in WIF_RESOURCE_TYPES:
        violations.append(Violation("wif-config-change",
            f"{rc.get('address')}: WIF {rtype!r} (actions={list(actions)})"))


def _check_iam(rc, rtype, actions, violations):
    if _is_iam_type(rtype):
        violations.append(Violation("iam-change-forbidden-v1",
            f"{rc.get('address')}: IAM {rtype!r} (actions={list(actions)}) — v1 hard-deny"))
```

Wire all checks in `evaluate`'s loop after the delete/forget/replace block; pass `before, after = _identity_dicts(rc, actions)`. Each helper is idempotent; multiple fire on the same RC.

**Verify + commit**: `feat(c1): WIF + IAM hard-deny rules`.

---

### Task 8: defensive bias-to-deny + multi-violation aggregation

**Fixtures:** `multi_violations_sa_delete.json`, `malformed_protected_cloud_run_no_name.json`, plus the unknown-action and not-all-strings ones from Task 4.

**Tests:**

```python
def test_one_plan_can_fire_multiple_rules():
    parsed, _ = load_plan_json(_load("multi_violations_sa_delete.json"))
    rules = sorted({v.rule for v in evaluate(DenylistInput(plan=parsed))})
    assert {"control-plane-sa", "iam-change-forbidden-v1", "delete-action-forbidden-v1"} <= set(rules)


def test_protected_type_with_no_identity_is_malformed():
    parsed, _ = load_plan_json(_load("malformed_protected_cloud_run_no_name.json"))
    rules = [v.rule for v in evaluate(DenylistInput(plan=parsed))]
    assert "plan-json-malformed-change" in rules
```

If failing: fix the loop's fall-through (no `continue` after firing a rule unless we're skipping the RC entirely).

**Verify + commit**: `feat(c1): multi-rule aggregation + bias-to-deny coverage`.

---

### Task 9: CLI wrapper `python -m tools.iac_plan_denylist`

**Files:** Modify `tools/iac_plan_denylist.py`; create `tests/unit/test_iac_plan_denylist_cli.py`.

**Failing CLI tests:** invoke `python -m tools.iac_plan_denylist` over fixtures; assert exit codes (0 / 1 / 2) and that stderr names violations.

**Impl:**

```python
import argparse
import sys


def _main(argv: list[str]) -> int:
    if not argv:
        print("usage: python -m tools.iac_plan_denylist <plan.json>", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(
        prog="python -m tools.iac_plan_denylist",
        description="Self-protection denylist for OpenTofu plan.json (design §5.2). "
                    "Exits 0 on pass, 1 on violations, 2 on usage/I/O error.",
    )
    parser.add_argument("plan_json", help="Path to plan.json")
    ns = parser.parse_args(argv)
    try:
        text = open(ns.plan_json, encoding="utf-8").read()
    except OSError as e:
        print(f"error: cannot read {ns.plan_json}: {e}", file=sys.stderr)
        return 2
    parsed, parse_violation = load_plan_json(text)
    if parsed is None:
        assert parse_violation is not None
        print(f"DENY [{parse_violation.rule}] {parse_violation.detail}", file=sys.stderr)
        return 1
    violations = evaluate(DenylistInput(plan=parsed))
    if not violations:
        print(f"OK: {ns.plan_json} - 0 violations")
        return 0
    for v in violations:
        print(f"DENY [{v.rule}] {v.detail}", file=sys.stderr)
    print(f"FAIL: {ns.plan_json} - {len(violations)} violation(s)", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover (covered by CLI tests)
    sys.exit(_main(sys.argv[1:]))
```

ASCII-only CLI output (Codex nit).

**Verify + commit**: `feat(c1): CLI wrapper for iac_plan_denylist`.

---

### Task 10: README + docstring + CODEOWNERS + C4-contract doc + ruff

**Files:**
- Modify: `iac/README.md` — add "Phase C1: plan-JSON denylist" subsection.
- Modify: `tools/iac_plan_denylist.py` — fill module docstring (rule list, fail-closed semantics, "runs in three places: CI plan-builder (C2) + this CLI for local dev + the tofu-apply worker (C4)", v1 floor warning).
- **Add C4 worker-contract section in the module docstring** (Codex Important #7): explicit "C4 callers MUST use load_plan_json(text) to parse the raw artifact, then evaluate(DenylistInput(plan=parsed)); BOTH a non-None parse Violation AND a non-empty evaluate result MUST be treated as 'deny'. The library never raises on policy concerns."
- Modify: `.github/CODEOWNERS` — add `/tools/iac_plan_denylist.py @adi-prasetyo` (mirrors the static-gate line).
- Run: `uv run ruff check tools/iac_plan_denylist.py tests/unit/test_iac_plan_denylist*.py`
- Run: `uv run ruff format --check ...`
- Run: full `uv run pytest -q`

**Commit:** `docs(c1): README pointer + module docstring + C4 contract + CODEOWNERS + ruff`.

---

### Task 11: Pre-PR validation matrix

Mirror CI exactly:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q                # expect new tests + all prior green
tofu -chdir=iac fmt -check
tofu -chdir=iac init -backend=false -lockfile=readonly
tofu -chdir=iac validate
uv run python -m tools.iac_static_gate --base origin/main --head HEAD --mode operator
```

Each must exit 0. Capture the test-count delta in PR body.

---

### Task 12 (post-implementation): Codex completed-work review + PR + merge + cleanup

Out of plan scope (the PR-loop step itself executes the review). Plan exit criterion: all 11 tasks committed, full local validation green, ready for `gh pr create`.

---

## 7. Out of scope (do NOT add, even if tempting)

- ❌ Inspecting `prior_state` / `planned_values` / `configuration` / `output_changes` — v1 is `resource_changes`-only.
- ❌ Reading `.terraform.lock.hcl` — that's the static gate's job.
- ❌ Running `tofu show -json` from inside the denylist — producer's job.
- ❌ Network calls — pure module, no I/O outside the CLI's single file read.
- ❌ Logging frameworks — `print` to stderr/stdout is fine.
- ❌ Custom exit codes beyond 0/1/2.
- ❌ JSON-output mode for the CLI — add in C4 if the apply worker needs structured output.
- ❌ Whitelist / allowlist support — v1 is denylist-only.
- ❌ Configuration files — constants are hardcoded; tests guard them.

## 8. Exit criteria

- [ ] `tools/iac_plan_denylist.py` exists, all 14 rules implemented, fail-closed everywhere
- [ ] CLI `python -m tools.iac_plan_denylist <plan.json>` works with exit codes 0/1/2
- [ ] **35** test fixtures in `tests/fixtures/iac_plan_denylist/` covering every rule
- [ ] **~45** unit tests in `test_iac_plan_denylist.py` + `test_iac_plan_denylist_cli.py` — all green
- [ ] Full prior suite still green
- [ ] `iac/README.md` mentions the denylist + CLI + "wired in C2"
- [ ] Module docstring matches the `iac_static_gate.py` standard + includes the explicit C4 contract
- [ ] `.github/CODEOWNERS` includes `iac_plan_denylist.py`
- [ ] ruff check + format clean
- [ ] Codex completed-work review = no blocking findings
- [ ] PR open, all CI required checks green, squash-merge eligible

---

**END OF PLAN.**
