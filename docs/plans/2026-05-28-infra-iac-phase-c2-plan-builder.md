# Phase C2 — Trusted Plan-Builder Workflow Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship the WIF-authenticated GitHub Actions job that produces an immutable `tofu plan` artifact (plan.tfplan + plan.json + metadata) for a DriftScribe IaC PR, gated by the C1 denylist, posted as a human-readable diff to the PR, and ready for C3/C4 to bind an approval to and apply.

**Architecture:**
The plan-builder runs as a NEW `plan-builder` job inside the EXISTING `.github/workflows/iac.yml` file (the WIF OIDC condition already pins `workflow_ref` to that exact path — putting it in a new workflow file would require re-running bootstrap to relax the condition). It triggers ONLY on `workflow_dispatch` (maintainer-initiated) with a required `pr_number` input — never on `pull_request`, because fork-PR OIDC tokens carry the BASE repository claim and cannot be filtered out via `repository ==`. The job resolves the PR's head+base SHAs, re-runs the static gate against that pinned head SHA (defense-in-depth), authenticates via WIF to the existing `tofu-plan-builder@driftscribe-hack-2026.iam.gserviceaccount.com` SA, runs `tofu init` (with backend) and `tofu plan -out=plan.tfplan`, JSON-renders the plan, re-runs the C1 denylist on the JSON (fails workflow on violation BEFORE upload), uploads the binary plan + JSON + immutable metadata to `gs://driftscribe-hack-2026-tofu-artifacts/pr-<N>/<head_sha>/`, and posts a truncated `tofu show` diff to the PR. Two new pure-Python helpers (`tools/iac_plan_metadata.py`, `tools/iac_plan_diff_summary.py`) carry all logic that needs unit tests; the workflow YAML is glue around them plus the existing `tools/iac_plan_denylist.py` and `tools/iac_static_gate.py` modules.

**Tech Stack:** Python 3.12 (pure-stdlib helpers — no new deps), uv for sync, OpenTofu 1.12.x, `google-github-actions/auth@v2` for WIF, `google-cloud-storage` SDK (already in deps via the infra-reader worker) for the artifact upload, `gh` CLI for PR metadata + comment posting, GitHub Actions `workflow_dispatch` for the trusted-trigger boundary.

**Codex plan-review trail:** Codex thread `019e6e11-a3d0-78d3-9040-d0f53b60947b`. Rev-1 surfaced 3 blockers + 8 importants + 3 nits — folded into this rev-2 (which `mcp__codex__codex-reply` re-reviewed on the same thread). After implementation completes, the same thread is replied to a third time for completed-work review.

---

## §0  Hard invariants (must hold at end of slice)

These are the assertions that make this plan-builder "trusted" rather than "another CI job". Any task that violates one of these must be redesigned, not waived.

1. **The OIDC `workflow_ref` claim still resolves to `.github/workflows/iac.yml`.** The bootstrap-provisioned WIF attribute condition pins `workflow_ref` to that exact path; the plan-builder job MUST live in that file. (Verify by reading `infra/scripts/setup_iac_backend.sh` line 94: `GITHUB_WORKFLOW:-.github/workflows/iac.yml`.)
2. **The `pull_request` event NEVER receives GCP credentials.** Fork-PR tokens carry the base repo's `repository` claim and so cannot be filtered. The plan-builder job MUST be gated `if: github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'`.
3. **`workflow_dispatch` is admitted ONLY when the dispatched ref is `refs/heads/main`.** Without a ref pin, a maintainer (or anyone with PR write) could dispatch the workflow from a feature branch whose `iac.yml` was modified to exfiltrate creds. Defense-in-depth at TWO layers: (a) the WIF attribute condition itself (bootstrap-side, see Task 6's update to `setup_iac_backend.sh`) admits dispatch ONLY when `assertion.ref == 'refs/heads/main'`; (b) the job-level `if:` repeats `github.ref == 'refs/heads/main'`.
4. **The PR being planned MUST touch ONLY `iac/` paths.** This is the hard-stop against the "PR-controlled Python after WIF" attack — once the PR is confirmed `iac/`-only, the checked-out `tools/`/`tests/`/`uv.lock`/`pyproject.toml` are byte-identical to `main` (the PR didn't touch them), so the Python helpers that run after WIF auth are trusted code. Enforcement at TWO layers, BOTH after the pinned checkout but BEFORE any `uv sync` / Python invocation: (a) a pure-shell `git diff --name-only --no-renames -z "$BASE_SHA" "$HEAD_SHA"` step refuses if any path is outside `iac/` (`--no-renames` so a `tools/x.py` → `iac/x.py` rename cannot slip through); (b) the static gate re-runs in **hardcoded `MODE=agent`** (not derived from PR labels/branch) and agent-mode rejects all non-`iac/` paths. The local git diff operates on immutable commit objects — no API call that could observe a later force-push, so there is no TOCTOU window between resolving HEAD_SHA and validating its diff.
5. **The PR being planned MUST target base `main` and be same-repo (`isCrossRepository == false`).** Cross-repo (fork) PRs and PRs against non-main bases are refused at the PR-resolution step, BEFORE WIF auth.
6. **The C1 denylist runs against `plan.json` BEFORE any upload to the artifact bucket.** A denied plan never becomes an artifact. Workflow step ordering is part of the contract.
7. **`plan.tfplan`, `plan.json` AND `metadata.json` are uploaded via the `google-cloud-storage` Python SDK; the generation of each is read directly from the upload response (no second `objects.get` call).** This lets the CI SA's bucket IAM stay at `roles/storage.objectCreator` ALONE (no `objectViewer`). The plan SDK call is `blob.upload_from_filename(); blob.generation` — `.generation` is populated from the create response in-band. Generations of plan.tfplan AND plan.json AND metadata.json all flow into the PR comment so C3 can pin to a specific metadata generation.
8. **The plan-builder identity has no project-wide write grants.** Bucket-scoped IAM only: `roles/storage.objectAdmin` on `*-tofu-state` (state lock, already present), `roles/storage.objectCreator` on `*-tofu-artifacts` (write-only, this slice adds), `roles/cloudkms.cryptoKeyEncrypterDecrypter` on the single KMS key (already present), `roles/run.viewer` project-scoped (read refresh, already present).
9. **No long-lived service-account JSON keys anywhere.** WIF only. Pre-existing.
10. **No `pull_request_target` triggers in this workflow or any workflow that this slice adds.** `pull_request_target` runs PR-controlled code with repo secrets and would defeat every protection here.
11. **The metadata.json schema is the contract C3 reads.** Field names + types in this slice ARE the C3 input shape. Renaming any field later is a breaking change. The 15 keys (`schema_version` + 14 data fields) are listed verbatim in §3 below.
12. **C2 mints no approval token, signs no HMAC, and reads no HMAC key.** All HMAC machinery is C3 (owned by the apply worker). C2's output is purely the artifact + metadata.
13. **Concurrency.** Workflow-level concurrency `cancel-in-progress` is **conditional on event_name**: `true` on `pull_request` (PR-side gate runs are cheap to cancel/restart), `false` on `workflow_dispatch` (cancelling a plan-builder mid-upload could leave a partial artifact). Job-level concurrency for the plan-builder is `iac-plan-builder-pr-<N>` with `cancel-in-progress: false` — at most one plan-builder per PR, never cancelled mid-upload.
14. **The OpenTofu binary version is pinned via `with: tofu_version: <pinned>` on `setup-opentofu`.** Not pinned = action default = floating. C4 will reject a plan whose `metadata.opentofu_version` doesn't match the configured apply-side version; a floating version would make that comparison brittle.

---

## §1  Existing infrastructure this slice plugs into (verified by grep)

**Already provisioned by `infra/scripts/setup_iac_backend.sh` (Phase A):**

| Resource | Identifier | Notes |
|---|---|---|
| State bucket | `gs://driftscribe-hack-2026-tofu-state` | Versioning ON, UBLA, PAP, `prefix=prod` |
| Artifact bucket | `gs://driftscribe-hack-2026-tofu-artifacts` | Versioning ON, UBLA, PAP. **No IAM yet** — this slice adds. |
| KMS keyring | `projects/driftscribe-hack-2026/locations/asia-northeast1/keyRings/driftscribe-tofu` | |
| KMS key | `…/cryptoKeys/tofu-state` | Symmetric encrypt/decrypt |
| WIF pool | `projects/<num>/locations/global/workloadIdentityPools/github-actions` | |
| WIF provider | `…/providers/github-oidc` | Issuer `https://token.actions.githubusercontent.com`. **As provisioned by Phase A** the attribute condition restricts to repo `adi-prasetyo/driftscribe`, `workflow_ref` prefix `…/iac.yml@`, and event ∈ {push to refs/heads/main, workflow_dispatch} — **but workflow_dispatch is NOT yet ref-pinned** (Codex rev-1 blocker). **Task 6 of this slice updates the bootstrap to also pin `assertion.ref == 'refs/heads/main'` for `workflow_dispatch`**; operator re-runs the bootstrap after merge to apply the tightened condition. Until that re-run lands, the workflow-side `if:` guard (Task 7b) provides the only protection. |
| CI SA | `tofu-plan-builder@driftscribe-hack-2026.iam.gserviceaccount.com` | Bound: `storage.objectAdmin` on state bucket, `cryptoKeyEncrypterDecrypter` on the KMS key, `run.viewer` project, `workloadIdentityUser` for `principalSet://…/attribute.repository/adi-prasetyo/driftscribe` |

**Already shipped in `tools/` (Phases A + C1):**

- `tools/iac_static_gate.py` — CLI `python -m tools.iac_static_gate --base <SHA> --head <SHA> --mode {operator|agent}`, exits non-zero on violation.
- `tools/iac_plan_denylist.py` — CLI `python -m tools.iac_plan_denylist <plan.json>`, exits 0/1/2.

**Existing GitHub secrets the operator must set BEFORE the workflow runs (operator follow-up):**

- `GCP_WIF_PROVIDER` — full resource name `projects/<num>/locations/global/workloadIdentityPools/github-actions/providers/github-oidc` (already used by `e2e.yml`).
- `GCP_TOFU_PLAN_BUILDER_SA` — `tofu-plan-builder@driftscribe-hack-2026.iam.gserviceaccount.com` (new secret name — verify it doesn't already exist before creating).
- `GCP_TOFU_STATE_KMS_KEY` — `projects/driftscribe-hack-2026/locations/asia-northeast1/keyRings/driftscribe-tofu/cryptoKeys/tofu-state` (new secret).

These are NOT created by this slice (they're operator secrets, not code). The plan ships the workflow that USES them; the operator wires them after merge.

---

## §2  Out of scope for this slice (explicit, to keep boundaries clear)

- **C3 plan-approval schema / HMAC.** No HMAC key handling. No approval record produced. The metadata.json this slice writes IS the input to C3; the C3 slice will add the approval-record schema layered on top.
- **C4 apply worker.** No `tofu apply`. No artifact READ. C2 only writes.
- **Auto-trigger on PR.** v1 is `workflow_dispatch` only. A later slice may add `workflow_run` after the PR-side static gate completes, but the simpler boundary in v1 is "maintainer clicks Run workflow".
- **GitHub check-run integration.** The plan-builder posts a comment, not a check. Check-run wiring can come later.
- **Drift detection on `push` to main.** The OIDC condition already admits `push refs/heads/main`, but this slice does not add a push-trigger job. A later slice can.
- **`infra/scripts/setup_iac_backend.sh` re-architecture.** This slice adds ONE block (artifact-bucket IAM grant) to the existing script. No other changes.
- **Documentation polish beyond the iac/README.md update.** No new top-level docs page.

---

## §3  metadata.json schema (the C3 contract)

This is the on-disk artifact shape. ALL 15 keys (`schema_version` + 14 data fields) are required. Strings are ASCII unless explicitly noted. Build via `tools.iac_plan_metadata.build_metadata`; serialize with `json.dumps(..., sort_keys=True, indent=2)` + trailing newline so the on-disk bytes are deterministic.

```json
{
  "schema_version": "c2.v1",
  "repo": "adi-prasetyo/driftscribe",
  "pr_number": 42,
  "head_sha": "0123abcd…",        // exact 40-hex commit that was planned
  "base_sha": "deadbeef…",        // PR base branch HEAD at plan time, 40-hex
  "workflow_run_id": "1234567890", // GHA run id, string (GH API returns int but we serialize string)
  "workflow_run_attempt": "1",     // GHA run attempt counter, string — pinning this in the path keeps re-runs of the SAME dispatch isolated
  "artifact_uri_plan": "gs://driftscribe-hack-2026-tofu-artifacts/pr-42/<head_sha>/run-<run_id>-<run_attempt>/plan.tfplan",
  "artifact_uri_json": "gs://driftscribe-hack-2026-tofu-artifacts/pr-42/<head_sha>/run-<run_id>-<run_attempt>/plan.json",
  "generation_plan": "1700000000000000",  // GCS generation, string (the API returns int but we serialize string to avoid JS-number precision loss)
  "generation_json": "1700000000000001",
  "plan_sha256": "abc…",          // 64-hex SHA-256 of plan.tfplan BYTES
  "plan_json_sha256": "def…",     // 64-hex SHA-256 of plan.json BYTES
  "opentofu_version": "1.12.0",   // from `tofu version -json`; pinned via setup-opentofu's tofu_version
  "provider_lockfile_sha256": "fed…" // 64-hex SHA-256 of iac/.terraform.lock.hcl bytes
}
```

**Why 15 keys (incl. `schema_version`), flat, no nesting:**
- Flat = trivial to verify in C3/C4 (no pointer-chasing).
- Strings for all IDs that GitHub/GCS could return as variable-precision integers (workflow_run_id, workflow_run_attempt, generations) — eliminates JS-number precision risk in any future JS consumer.
- `schema_version` is the version handshake so C3 can reject unknown schemas fail-closed.
- `repo` is recorded (not just inferred from environment) so a stolen artifact from a different repo cannot impersonate.
- BOTH `head_sha` AND `base_sha` recorded so re-running the static gate or recomputing the diff at apply time is deterministic.
- `workflow_run_attempt` recorded so a re-run of the same dispatch (GitHub increments `run_attempt` on "Re-run" clicks) produces a NEW path segment and never overwrites the prior plan — every plan-builder execution gets its own URI, eliminating the "C3 must pin to a generation to disambiguate concurrent metadata writes" problem.
- `provider_lockfile_sha256` IS the C4 freshness check seed: if the lockfile changed between plan and apply, the apply must refuse.

**Metadata.json generation is also captured.** After uploading metadata.json the workflow reads back `metadata_blob.generation` from the upload response and surfaces it in the PR comment (NOT inside metadata.json itself — that would be self-referential — but printed alongside in the comment body and the workflow summary). C3 records all three generations: plan, json, metadata. C4 fetches each by pinned generation.

---

## §4  Artifact path scheme

```
gs://driftscribe-hack-2026-tofu-artifacts/
└── pr-<N>/
    └── <head_sha>/
        └── run-<run_id>-<run_attempt>/
            ├── plan.tfplan      (binary)
            ├── plan.json        (text)
            └── metadata.json    (text — written LAST so it carries the generations of the other two)
```

Four rules:
- **`pr-` prefix on the PR-number directory** so a PR named `42` does not collide with any other top-level key.
- **`<head_sha>` is the FULL 40-character lowercase hex** — never truncated.
- **`run-<run_id>-<run_attempt>` segment** so EVERY plan-builder execution gets its own URI. Re-planning the same PR at the same commit (e.g., the operator re-dispatches the workflow after a transient `tofu plan` flake) writes into a different folder, never overwrites a prior plan. C3 binds an approval to a specific (head_sha, run_id, run_attempt) → unique URI → unique generations. The `run_id` is monotonic per repo; `run_attempt` increments on the "Re-run jobs" button.
- **`metadata.json` last** so its `generation_plan`/`generation_json` fields reflect the actual GCS generations the upload created. Metadata.json's own generation is captured from the upload response and surfaced in the PR comment / workflow output.

---

## §5  Tasks

17 bite-sized tasks (1–6, 6b, 7a–7e, 8–12). Commit after each. TDD where applicable.

---

### Task 1: Scaffold the metadata module + first failing test

**Files:**
- Create: `tools/iac_plan_metadata.py`
- Create: `tests/unit/test_iac_plan_metadata.py`

**Step 1: Write the failing test for the empty-module placeholder**

`tests/unit/test_iac_plan_metadata.py`:

```python
"""Tests for tools.iac_plan_metadata — the C2 metadata builder."""

from tools import iac_plan_metadata


def test_module_imports():
    """The module must be importable."""
    assert iac_plan_metadata is not None
```

**Step 2: Run test to verify it fails**

```bash
cd /home/adi/driftscribe/.worktrees/phase-c2-plan-builder
uv run pytest tests/unit/test_iac_plan_metadata.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'tools.iac_plan_metadata'`.

**Step 3: Write minimal module to make it pass**

`tools/iac_plan_metadata.py`:

```python
"""Build + serialize the C2 plan-builder metadata.json artifact.

Pure-stdlib helper called by the plan-builder workflow. The metadata
record is the input contract for the C3 plan-approval schema (see
docs/plans/2026-05-28-infra-iac-phase-c2-plan-builder.md §3) — DO NOT
rename a field without updating C3.

Determinism: every public function in this module is a pure function of
its arguments. ``serialize_metadata`` round-trips byte-identically given
the same input (sort_keys + fixed indent + no trailing whitespace).
"""
from __future__ import annotations
```

**Step 4: Run test to verify it passes**

```bash
uv run pytest tests/unit/test_iac_plan_metadata.py -v
```

Expected: PASS, 1 test.

**Step 5: Commit**

```bash
git add tools/iac_plan_metadata.py tests/unit/test_iac_plan_metadata.py
git commit -m "feat(iac): scaffold C2 plan-metadata module"
```

---

### Task 2: Pure-function metadata builder + serializer

**Files:**
- Modify: `tools/iac_plan_metadata.py`
- Modify: `tests/unit/test_iac_plan_metadata.py`

**Step 1: Write failing tests for the schema + serializer**

Add to `tests/unit/test_iac_plan_metadata.py`:

```python
import json

import pytest

from tools.iac_plan_metadata import (
    METADATA_SCHEMA_VERSION,
    MetadataInput,
    build_metadata,
    serialize_metadata,
)


_RUN_DIR = "run-1234567890-1"
_HEAD = "a" * 40


def _valid_input(**overrides):
    base = dict(
        repo="adi-prasetyo/driftscribe",
        pr_number=42,
        head_sha=_HEAD,
        base_sha="b" * 40,
        workflow_run_id="1234567890",
        workflow_run_attempt="1",
        artifact_uri_plan=f"gs://driftscribe-hack-2026-tofu-artifacts/pr-42/{_HEAD}/{_RUN_DIR}/plan.tfplan",
        artifact_uri_json=f"gs://driftscribe-hack-2026-tofu-artifacts/pr-42/{_HEAD}/{_RUN_DIR}/plan.json",
        generation_plan="1700000000000000",
        generation_json="1700000000000001",
        plan_sha256="c" * 64,
        plan_json_sha256="d" * 64,
        opentofu_version="1.12.0",
        provider_lockfile_sha256="e" * 64,
    )
    base.update(overrides)
    return MetadataInput(**base)


def test_schema_version_constant():
    assert METADATA_SCHEMA_VERSION == "c2.v1"


def test_build_metadata_returns_all_fifteen_keys():
    md = build_metadata(_valid_input())
    expected_keys = {
        "schema_version",
        "repo", "pr_number",
        "head_sha", "base_sha",
        "workflow_run_id", "workflow_run_attempt",
        "artifact_uri_plan", "artifact_uri_json",
        "generation_plan", "generation_json",
        "plan_sha256", "plan_json_sha256",
        "opentofu_version", "provider_lockfile_sha256",
    }
    assert set(md.keys()) == expected_keys


def test_build_metadata_schema_version_value():
    md = build_metadata(_valid_input())
    assert md["schema_version"] == "c2.v1"


def test_pr_number_is_int_in_serialized_form():
    md = build_metadata(_valid_input(pr_number=42))
    assert md["pr_number"] == 42
    blob = serialize_metadata(md)
    parsed = json.loads(blob)
    assert parsed["pr_number"] == 42
    assert isinstance(parsed["pr_number"], int)


def test_workflow_run_id_and_attempt_and_generations_are_strings():
    md = build_metadata(_valid_input())
    assert isinstance(md["workflow_run_id"], str)
    assert isinstance(md["workflow_run_attempt"], str)
    assert isinstance(md["generation_plan"], str)
    assert isinstance(md["generation_json"], str)


def test_workflow_run_attempt_must_be_positive_digits():
    with pytest.raises(ValueError, match="workflow_run_attempt"):
        build_metadata(_valid_input(workflow_run_attempt="0"))
    with pytest.raises(ValueError, match="workflow_run_attempt"):
        build_metadata(_valid_input(workflow_run_attempt="abc"))


def test_serialize_metadata_is_deterministic():
    md1 = build_metadata(_valid_input())
    md2 = build_metadata(_valid_input())
    assert serialize_metadata(md1) == serialize_metadata(md2)


def test_serialize_metadata_ends_with_newline():
    blob = serialize_metadata(build_metadata(_valid_input()))
    assert blob.endswith("\n")


def test_serialize_metadata_uses_sorted_keys():
    blob = serialize_metadata(build_metadata(_valid_input()))
    # The first key after the opening brace must be lexicographically
    # smallest among ours: "artifact_uri_json".
    first_line_with_key = blob.split("\n")[1].lstrip()
    assert first_line_with_key.startswith('"artifact_uri_json"'), blob


@pytest.mark.parametrize("field", [
    "head_sha", "base_sha", "plan_sha256", "plan_json_sha256", "provider_lockfile_sha256",
])
def test_hex_fields_rejected_when_wrong_length(field):
    with pytest.raises(ValueError, match=field):
        build_metadata(_valid_input(**{field: "abc"}))


@pytest.mark.parametrize("field", [
    "head_sha", "base_sha",  # 40-hex SHA-1
])
def test_sha1_fields_must_be_lowercase_hex(field):
    with pytest.raises(ValueError, match=field):
        build_metadata(_valid_input(**{field: "G" * 40}))


@pytest.mark.parametrize("field", [
    "plan_sha256", "plan_json_sha256", "provider_lockfile_sha256",  # 64-hex SHA-256
])
def test_sha256_fields_must_be_lowercase_hex(field):
    with pytest.raises(ValueError, match=field):
        build_metadata(_valid_input(**{field: "G" * 64}))


def test_pr_number_must_be_positive():
    with pytest.raises(ValueError, match="pr_number"):
        build_metadata(_valid_input(pr_number=0))
    with pytest.raises(ValueError, match="pr_number"):
        build_metadata(_valid_input(pr_number=-1))


def test_repo_must_match_owner_slash_repo():
    with pytest.raises(ValueError, match="repo"):
        build_metadata(_valid_input(repo="adi-prasetyo"))
    with pytest.raises(ValueError, match="repo"):
        build_metadata(_valid_input(repo="adi-prasetyo/driftscribe/extra"))


def test_artifact_uri_plan_must_match_pr_head_and_run_dir():
    bad = _valid_input(artifact_uri_plan="gs://other-bucket/pr-1/aaa/run-1-1/plan.tfplan")
    with pytest.raises(ValueError, match="artifact_uri_plan"):
        build_metadata(bad)
    # Wrong run_id segment also fails — the path must reflect THIS dispatch's run.
    bad2 = _valid_input(
        artifact_uri_plan=f"gs://driftscribe-hack-2026-tofu-artifacts/pr-42/{_HEAD}/run-999-1/plan.tfplan",
    )
    with pytest.raises(ValueError, match="artifact_uri_plan"):
        build_metadata(bad2)


def test_artifact_uri_must_omit_or_include_run_attempt_correctly():
    # The path scheme is `run-<run_id>-<run_attempt>`: a path that drops the
    # attempt segment must be rejected.
    bad = _valid_input(
        artifact_uri_plan=f"gs://driftscribe-hack-2026-tofu-artifacts/pr-42/{_HEAD}/run-1234567890/plan.tfplan",
    )
    with pytest.raises(ValueError, match="artifact_uri_plan"):
        build_metadata(bad)


def test_generation_must_be_numeric_string():
    with pytest.raises(ValueError, match="generation_plan"):
        build_metadata(_valid_input(generation_plan="abc"))
    with pytest.raises(ValueError, match="generation_plan"):
        build_metadata(_valid_input(generation_plan=""))


def test_opentofu_version_must_be_semver_like():
    with pytest.raises(ValueError, match="opentofu_version"):
        build_metadata(_valid_input(opentofu_version=""))
    with pytest.raises(ValueError, match="opentofu_version"):
        build_metadata(_valid_input(opentofu_version="1.12"))  # only 2 segments
```

**Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/unit/test_iac_plan_metadata.py -v
```

Expected: 16 FAIL (missing constants + functions).

**Step 3: Implement the module**

Replace `tools/iac_plan_metadata.py` with the full implementation. Body:

```python
"""Build + serialize the C2 plan-builder metadata.json artifact.

Pure-stdlib helper called by the plan-builder workflow. The metadata
record is the input contract for the C3 plan-approval schema (see
docs/plans/2026-05-28-infra-iac-phase-c2-plan-builder.md §3) — DO NOT
rename a field without updating C3.

Determinism: every public function in this module is a pure function of
its arguments. ``serialize_metadata`` round-trips byte-identically given
the same input (sort_keys + fixed indent + no trailing whitespace).

Validation: ``build_metadata`` validates input shapes and raises
``ValueError`` on malformed inputs. The CLI wrapper (in a later task)
converts ValueError into a non-zero exit so the workflow fails BEFORE
the artifact gets uploaded.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

METADATA_SCHEMA_VERSION = "c2.v1"

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_REPO  = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_DIGITS = re.compile(r"^[0-9]+$")
_SEMVER_3 = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?$")


@dataclass(frozen=True)
class MetadataInput:
    """Input to :func:`build_metadata`. All fields required."""
    repo: str
    pr_number: int
    head_sha: str
    base_sha: str
    workflow_run_id: str
    workflow_run_attempt: str
    artifact_uri_plan: str
    artifact_uri_json: str
    generation_plan: str
    generation_json: str
    plan_sha256: str
    plan_json_sha256: str
    opentofu_version: str
    provider_lockfile_sha256: str


_POSITIVE_DIGITS = re.compile(r"^[1-9][0-9]*$")


def _check(name: str, value: Any, predicate, message: str) -> None:
    if not predicate(value):
        raise ValueError(f"{name}: {message} (got {value!r})")


def build_metadata(inp: MetadataInput) -> dict[str, Any]:
    """Validate input + return the metadata dict.

    Raises ValueError on any malformed field. NEVER produces a partial /
    half-validated record — either every field is sane or no dict is
    returned at all.
    """
    _check("repo", inp.repo, lambda v: bool(_REPO.fullmatch(v)), "must be 'owner/repo'")
    _check("pr_number", inp.pr_number, lambda v: isinstance(v, int) and v > 0, "must be a positive int")
    _check("head_sha", inp.head_sha, lambda v: bool(_HEX40.fullmatch(v)), "must be 40 lowercase hex")
    _check("base_sha", inp.base_sha, lambda v: bool(_HEX40.fullmatch(v)), "must be 40 lowercase hex")
    _check("workflow_run_id", inp.workflow_run_id, lambda v: isinstance(v, str) and bool(_DIGITS.fullmatch(v)), "must be a numeric string")
    _check("workflow_run_attempt", inp.workflow_run_attempt, lambda v: isinstance(v, str) and bool(_POSITIVE_DIGITS.fullmatch(v)), "must be a positive numeric string (GHA run_attempt is 1-indexed)")
    _check("generation_plan", inp.generation_plan, lambda v: isinstance(v, str) and bool(_DIGITS.fullmatch(v)), "must be a numeric string")
    _check("generation_json", inp.generation_json, lambda v: isinstance(v, str) and bool(_DIGITS.fullmatch(v)), "must be a numeric string")
    _check("plan_sha256", inp.plan_sha256, lambda v: bool(_HEX64.fullmatch(v)), "must be 64 lowercase hex")
    _check("plan_json_sha256", inp.plan_json_sha256, lambda v: bool(_HEX64.fullmatch(v)), "must be 64 lowercase hex")
    _check("provider_lockfile_sha256", inp.provider_lockfile_sha256, lambda v: bool(_HEX64.fullmatch(v)), "must be 64 lowercase hex")
    _check("opentofu_version", inp.opentofu_version, lambda v: bool(_SEMVER_3.fullmatch(v)), "must be three-segment semver")

    run_dir = f"run-{inp.workflow_run_id}-{inp.workflow_run_attempt}"
    expected_prefix = f"gs://driftscribe-hack-2026-tofu-artifacts/pr-{inp.pr_number}/{inp.head_sha}/{run_dir}/"
    _check(
        "artifact_uri_plan", inp.artifact_uri_plan,
        lambda v: v == expected_prefix + "plan.tfplan",
        f"must be exactly {expected_prefix}plan.tfplan",
    )
    _check(
        "artifact_uri_json", inp.artifact_uri_json,
        lambda v: v == expected_prefix + "plan.json",
        f"must be exactly {expected_prefix}plan.json",
    )

    return {
        "schema_version": METADATA_SCHEMA_VERSION,
        "repo": inp.repo,
        "pr_number": inp.pr_number,
        "head_sha": inp.head_sha,
        "base_sha": inp.base_sha,
        "workflow_run_id": inp.workflow_run_id,
        "workflow_run_attempt": inp.workflow_run_attempt,
        "artifact_uri_plan": inp.artifact_uri_plan,
        "artifact_uri_json": inp.artifact_uri_json,
        "generation_plan": inp.generation_plan,
        "generation_json": inp.generation_json,
        "plan_sha256": inp.plan_sha256,
        "plan_json_sha256": inp.plan_json_sha256,
        "opentofu_version": inp.opentofu_version,
        "provider_lockfile_sha256": inp.provider_lockfile_sha256,
    }


def serialize_metadata(md: dict[str, Any]) -> str:
    """Stable canonical JSON encoding — sorted keys, 2-space indent, trailing newline."""
    return json.dumps(md, sort_keys=True, indent=2) + "\n"
```

**Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/unit/test_iac_plan_metadata.py -v
```

Expected: 17 PASS.

**Step 5: Commit**

```bash
git add tools/iac_plan_metadata.py tests/unit/test_iac_plan_metadata.py
git commit -m "feat(iac): add C2 plan-metadata builder + validator (pure-stdlib)"
```

---

### Task 3: CLI for the metadata builder

The workflow YAML calls this via `python -m tools.iac_plan_metadata` with input from environment variables. We expose a small CLI for testability.

**Files:**
- Modify: `tools/iac_plan_metadata.py`
- Create: `tests/unit/test_iac_plan_metadata_cli.py`

**Step 1: Write the failing CLI tests**

`tests/unit/test_iac_plan_metadata_cli.py`:

```python
"""CLI tests for `python -m tools.iac_plan_metadata`."""

import json
import subprocess
import sys


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tools.iac_plan_metadata"],
        env=env,
        capture_output=True,
        text=True,
    )


def _valid_env(**overrides) -> dict[str, str]:
    head = "a" * 40
    run_dir = "run-1234567890-1"
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": ".",
        "META_REPO": "adi-prasetyo/driftscribe",
        "META_PR_NUMBER": "42",
        "META_HEAD_SHA": head,
        "META_BASE_SHA": "b" * 40,
        "META_WORKFLOW_RUN_ID": "1234567890",
        "META_WORKFLOW_RUN_ATTEMPT": "1",
        "META_ARTIFACT_URI_PLAN": f"gs://driftscribe-hack-2026-tofu-artifacts/pr-42/{head}/{run_dir}/plan.tfplan",
        "META_ARTIFACT_URI_JSON": f"gs://driftscribe-hack-2026-tofu-artifacts/pr-42/{head}/{run_dir}/plan.json",
        "META_GENERATION_PLAN": "1700000000000000",
        "META_GENERATION_JSON": "1700000000000001",
        "META_PLAN_SHA256": "c" * 64,
        "META_PLAN_JSON_SHA256": "d" * 64,
        "META_OPENTOFU_VERSION": "1.12.0",
        "META_PROVIDER_LOCKFILE_SHA256": "e" * 64,
    }
    env.update(overrides)
    return env


def test_cli_emits_canonical_json_on_stdout():
    res = _run(_valid_env())
    assert res.returncode == 0, res.stderr
    parsed = json.loads(res.stdout)
    assert parsed["schema_version"] == "c2.v1"
    assert parsed["pr_number"] == 42


def test_cli_exit_2_on_missing_env():
    env = _valid_env()
    del env["META_HEAD_SHA"]
    res = _run(env)
    assert res.returncode == 2
    assert "META_HEAD_SHA" in res.stderr


def test_cli_exit_1_on_invalid_field():
    env = _valid_env(META_HEAD_SHA="not-a-sha")
    res = _run(env)
    assert res.returncode == 1
    assert "head_sha" in res.stderr
```

**Step 2: Run to verify fail**

```bash
uv run pytest tests/unit/test_iac_plan_metadata_cli.py -v
```

Expected: 3 FAIL — the module has no `__main__`.

**Step 3: Add the CLI**

Append to `tools/iac_plan_metadata.py`:

```python
def _read_env(name: str, env: dict[str, str]) -> str:
    value = env.get(name)
    if value is None or value == "":
        raise SystemExit(f"missing required env var: {name}")
    return value


def _main(env: dict[str, str]) -> int:
    """CLI entrypoint: read META_* env vars, emit canonical metadata JSON on stdout."""
    try:
        pr_str = _read_env("META_PR_NUMBER", env)
        try:
            pr_number = int(pr_str)
        except ValueError:
            raise SystemExit(f"META_PR_NUMBER: must be int (got {pr_str!r})")
        inp = MetadataInput(
            repo=_read_env("META_REPO", env),
            pr_number=pr_number,
            head_sha=_read_env("META_HEAD_SHA", env),
            base_sha=_read_env("META_BASE_SHA", env),
            workflow_run_id=_read_env("META_WORKFLOW_RUN_ID", env),
            workflow_run_attempt=_read_env("META_WORKFLOW_RUN_ATTEMPT", env),
            artifact_uri_plan=_read_env("META_ARTIFACT_URI_PLAN", env),
            artifact_uri_json=_read_env("META_ARTIFACT_URI_JSON", env),
            generation_plan=_read_env("META_GENERATION_PLAN", env),
            generation_json=_read_env("META_GENERATION_JSON", env),
            plan_sha256=_read_env("META_PLAN_SHA256", env),
            plan_json_sha256=_read_env("META_PLAN_JSON_SHA256", env),
            opentofu_version=_read_env("META_OPENTOFU_VERSION", env),
            provider_lockfile_sha256=_read_env("META_PROVIDER_LOCKFILE_SHA256", env),
        )
    except SystemExit as e:
        # missing env -> exit 2
        print(str(e), file=__import__("sys").stderr)
        return 2

    try:
        md = build_metadata(inp)
    except ValueError as e:
        print(str(e), file=__import__("sys").stderr)
        return 1

    print(serialize_metadata(md), end="")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    import os
    import sys as _sys
    _sys.exit(_main(dict(os.environ)))
```

**Step 4: Run to verify pass**

```bash
uv run pytest tests/unit/test_iac_plan_metadata_cli.py -v
```

Expected: 3 PASS.

**Step 5: Commit**

```bash
git add tools/iac_plan_metadata.py tests/unit/test_iac_plan_metadata_cli.py
git commit -m "feat(iac): add CLI for plan-metadata builder"
```

---

### Task 4: PR-comment diff summary helper — failing tests

**Files:**
- Create: `tools/iac_plan_diff_summary.py`
- Create: `tests/unit/test_iac_plan_diff_summary.py`

**Step 1: Write the failing tests**

`tests/unit/test_iac_plan_diff_summary.py`:

```python
"""Tests for tools.iac_plan_diff_summary — the C2 PR-comment formatter."""

import pytest

from tools.iac_plan_diff_summary import (
    GH_COMMENT_BUDGET,
    SummaryInput,
    format_summary,
)


def _valid_input(**overrides):
    head = "a" * 40
    run_dir = "run-1234567890-1"
    base = dict(
        plan_text="Plan: 1 to add, 0 to change, 0 to destroy.\n",
        head_sha=head,
        plan_sha256="c" * 64,
        plan_json_sha256="d" * 64,
        generation_plan="1700000000000000",
        generation_json="1700000000000001",
        generation_metadata="1700000000000002",
        artifact_uri_plan=f"gs://driftscribe-hack-2026-tofu-artifacts/pr-42/{head}/{run_dir}/plan.tfplan",
        artifact_uri_json=f"gs://driftscribe-hack-2026-tofu-artifacts/pr-42/{head}/{run_dir}/plan.json",
        artifact_uri_metadata=f"gs://driftscribe-hack-2026-tofu-artifacts/pr-42/{head}/{run_dir}/metadata.json",
        opentofu_version="1.12.0",
    )
    base.update(overrides)
    return SummaryInput(**base)


def test_summary_contains_canonical_header_fields():
    out = format_summary(_valid_input())
    # The header is the part BEFORE the collapsible block — must surface
    # head_sha, BOTH content hashes, ALL THREE generations, ALL THREE URIs,
    # tofu version. C3 reads this comment as the canonical artifact pointer.
    assert "a" * 40 in out, "head_sha missing"
    assert "c" * 64 in out, "plan_sha256 missing"
    assert "d" * 64 in out, "plan_json_sha256 missing"
    assert "1700000000000000" in out, "generation_plan missing"
    assert "1700000000000001" in out, "generation_json missing"
    assert "1700000000000002" in out, "generation_metadata missing"
    assert "plan.tfplan" in out, "plan.tfplan URI missing"
    assert "plan.json" in out, "plan.json URI missing"
    assert "metadata.json" in out, "metadata.json URI missing"
    assert "1.12.0" in out, "opentofu_version missing"


def test_summary_wraps_plan_text_in_details_element():
    out = format_summary(_valid_input(plan_text="ADD resource.foo\n"))
    assert "<details>" in out and "</details>" in out
    assert "ADD resource.foo" in out


def test_summary_uses_code_fence_inside_details():
    out = format_summary(_valid_input(plan_text="x\n"))
    assert "```" in out


def test_summary_picks_fence_longer_than_any_backtick_run_in_plan():
    # tofu show output rarely contains backticks, but a PR could include a
    # description / comment with backticks that ends up in the plan text.
    # Fixed 3-backtick fence would break the code block. We must use a fence
    # longer than the longest backtick run in plan_text.
    plan = "Some text with ```triple``` and ````four``` backticks\n"
    out = format_summary(_valid_input(plan_text=plan))
    # The longest run in input is 4 backticks; fence must be >=5.
    fence_lines = [line for line in out.splitlines() if line and all(ch == "`" for ch in line)]
    assert fence_lines, "no fence found in output"
    assert min(len(f) for f in fence_lines) >= 5, fence_lines


def test_summary_short_plan_is_not_truncated():
    out = format_summary(_valid_input(plan_text="short\n"))
    assert "short" in out
    assert "(truncated" not in out


def test_summary_long_plan_is_truncated_to_budget():
    # Generate plan text larger than the budget.
    huge = ("X" * 1000 + "\n") * 100  # ~100KB
    out = format_summary(_valid_input(plan_text=huge))
    assert len(out) <= GH_COMMENT_BUDGET
    assert "(truncated" in out
    # The truncation marker must include the original size so reviewers can
    # see how much was dropped.
    assert str(len(huge)) in out


def test_summary_is_idempotent_for_same_input():
    a = format_summary(_valid_input(plan_text="x\n"))
    b = format_summary(_valid_input(plan_text="x\n"))
    assert a == b


def test_summary_strips_ansi_escapes_from_plan_text():
    # `tofu show -no-color` should already be clean, but defense-in-depth.
    out = format_summary(_valid_input(plan_text="\x1b[31mRED\x1b[0m\n"))
    assert "\x1b[" not in out
    assert "RED" in out


def test_gh_comment_budget_constant_is_reasonable():
    # GitHub's hard PR-comment limit is ~65,536 chars; we budget below it.
    assert 50000 <= GH_COMMENT_BUDGET <= 65000


@pytest.mark.parametrize("field", ["head_sha"])
def test_format_rejects_malformed_head_sha(field):
    with pytest.raises(ValueError, match=field):
        format_summary(_valid_input(**{field: "G" * 40}))


@pytest.mark.parametrize("field", ["plan_sha256", "plan_json_sha256"])
def test_format_rejects_malformed_sha256(field):
    with pytest.raises(ValueError, match=field):
        format_summary(_valid_input(**{field: "G" * 64}))
```

**Step 2: Run to verify fail**

```bash
uv run pytest tests/unit/test_iac_plan_diff_summary.py -v
```

Expected: 11 FAIL — module missing.

**Step 3: Implement the module**

`tools/iac_plan_diff_summary.py`:

```python
"""Format a `tofu show -no-color` diff into a PR comment for the C2 plan-builder.

Produces a Markdown body that GitHub accepts within its ~65 KB PR-comment
limit. The plan text is wrapped in a collapsible <details> block with a
code fence; a leading header surfaces the immutable identifiers (head_sha,
plan_sha256, generation, artifact URI, OpenTofu version) so a reviewer can
copy them straight into the C3 approval form once that exists.

Pure-stdlib. No GitHub API client — the workflow shells `gh pr comment`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# GitHub's documented PR-comment hard limit is 65,536 chars. We leave a
# margin for the header + Markdown wrapper + truncation marker.
GH_COMMENT_BUDGET = 60_000

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ANSI  = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


@dataclass(frozen=True)
class SummaryInput:
    plan_text: str
    head_sha: str
    plan_sha256: str
    plan_json_sha256: str
    generation_plan: str
    generation_json: str
    generation_metadata: str
    artifact_uri_plan: str
    artifact_uri_json: str
    artifact_uri_metadata: str
    opentofu_version: str


_BACKTICK_RUN = re.compile(r"`+")


def _validate(inp: SummaryInput) -> None:
    if not _HEX40.fullmatch(inp.head_sha):
        raise ValueError(f"head_sha: must be 40 lowercase hex (got {inp.head_sha!r})")
    if not _HEX64.fullmatch(inp.plan_sha256):
        raise ValueError(f"plan_sha256: must be 64 lowercase hex (got {inp.plan_sha256!r})")
    if not _HEX64.fullmatch(inp.plan_json_sha256):
        raise ValueError(f"plan_json_sha256: must be 64 lowercase hex (got {inp.plan_json_sha256!r})")


def _pick_fence(text: str) -> str:
    """Choose a backtick fence longer than any backtick run in the text.

    Default fence is 3 backticks; if the text contains a 3- or 4-backtick
    run, the fence must extend to >=5 to avoid early-terminating the code
    block. Markdown does not require equal fence lengths — opening and
    closing fences must be the same width, both at least 3.
    """
    longest_run = 0
    for m in _BACKTICK_RUN.finditer(text):
        longest_run = max(longest_run, len(m.group(0)))
    return "`" * max(3, longest_run + 1)


def format_summary(inp: SummaryInput) -> str:
    _validate(inp)
    clean = _ANSI.sub("", inp.plan_text)

    header_lines = [
        "### DriftScribe IaC — `tofu plan` (Phase C2 plan-builder)",
        "",
        f"- **head_sha:** `{inp.head_sha}`",
        f"- **plan_sha256:** `{inp.plan_sha256}` (generation `{inp.generation_plan}`)",
        f"- **plan_json_sha256:** `{inp.plan_json_sha256}` (generation `{inp.generation_json}`)",
        f"- **metadata generation:** `{inp.generation_metadata}`",
        f"- **artifact plan.tfplan:** `{inp.artifact_uri_plan}`",
        f"- **artifact plan.json:** `{inp.artifact_uri_json}`",
        f"- **artifact metadata.json:** `{inp.artifact_uri_metadata}`",
        f"- **opentofu:** `{inp.opentofu_version}`",
        "",
    ]
    header = "\n".join(header_lines)

    fence = _pick_fence(clean)
    scaffold = (
        "<details><summary>tofu show</summary>\n\n"
        + fence + "\n"
        # placeholder body
        + fence + "\n"
        + "</details>\n"
    )
    # Reserve room for the collapsible scaffold + a truncation notice.
    scaffold_overhead = len(header) + len(scaffold) + 256
    budget_for_plan = GH_COMMENT_BUDGET - scaffold_overhead

    if len(clean) > budget_for_plan:
        truncated = clean[:budget_for_plan]
        notice = (
            f"\n(truncated; original {len(clean)} chars, kept {budget_for_plan} chars; "
            f"fetch full diff via `gcloud storage cat {inp.artifact_uri_plan}` "
            f"or `tofu show <local plan>`)\n"
        )
        body_plan = truncated + notice
    else:
        body_plan = clean

    return (
        header
        + "<details><summary>tofu show</summary>\n\n"
        + fence + "\n"
        + body_plan
        + ("\n" if not body_plan.endswith("\n") else "")
        + fence + "\n"
        + "</details>\n"
    )
```

**Step 4: Run to verify pass**

```bash
uv run pytest tests/unit/test_iac_plan_diff_summary.py -v
```

Expected: 11 PASS.

**Step 5: Commit**

```bash
git add tools/iac_plan_diff_summary.py tests/unit/test_iac_plan_diff_summary.py
git commit -m "feat(iac): add C2 PR-comment formatter (truncating, ANSI-stripping)"
```

---

### Task 5: CLI for the diff-summary helper

The workflow reads `tofu show -no-color plan.tfplan` and pipes it to this CLI; the CLI prints the formatted comment body to stdout, which the workflow then passes to `gh pr comment --body-file -`.

**Files:**
- Modify: `tools/iac_plan_diff_summary.py`
- Create: `tests/unit/test_iac_plan_diff_summary_cli.py`

**Step 1: Write failing CLI tests**

`tests/unit/test_iac_plan_diff_summary_cli.py`:

```python
"""CLI tests for `python -m tools.iac_plan_diff_summary`."""

import subprocess
import sys


def _run(stdin: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tools.iac_plan_diff_summary", *args],
        input=stdin,
        capture_output=True,
        text=True,
    )


def _valid_args(**overrides) -> list[str]:
    head = "a" * 40
    run_dir = "run-1234567890-1"
    base = {
        "--head-sha": head,
        "--plan-sha256": "c" * 64,
        "--plan-json-sha256": "d" * 64,
        "--generation-plan": "1700000000000000",
        "--generation-json": "1700000000000001",
        "--generation-metadata": "1700000000000002",
        "--artifact-uri-plan": f"gs://driftscribe-hack-2026-tofu-artifacts/pr-42/{head}/{run_dir}/plan.tfplan",
        "--artifact-uri-json": f"gs://driftscribe-hack-2026-tofu-artifacts/pr-42/{head}/{run_dir}/plan.json",
        "--artifact-uri-metadata": f"gs://driftscribe-hack-2026-tofu-artifacts/pr-42/{head}/{run_dir}/metadata.json",
        "--opentofu-version": "1.12.0",
    }
    base.update(overrides)
    return [flag + "=" + value for flag, value in base.items()]


def test_cli_round_trips_stdin_into_body():
    res = _run("Plan: 1 to add\n", _valid_args())
    assert res.returncode == 0, res.stderr
    assert "Plan: 1 to add" in res.stdout
    assert "1.12.0" in res.stdout


def test_cli_exit_1_on_malformed_sha():
    res = _run("x\n", _valid_args(**{"--head-sha": "nope"}))
    assert res.returncode == 1
    assert "head_sha" in res.stderr
```

**Step 2: Run to verify fail**

```bash
uv run pytest tests/unit/test_iac_plan_diff_summary_cli.py -v
```

Expected: 2 FAIL.

**Step 3: Add CLI to the module**

Append to `tools/iac_plan_diff_summary.py`:

```python
def _main(argv: list[str], stdin_text: str) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="iac_plan_diff_summary")
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--plan-json-sha256", required=True)
    parser.add_argument("--generation-plan", required=True)
    parser.add_argument("--generation-json", required=True)
    parser.add_argument("--generation-metadata", required=True)
    parser.add_argument("--artifact-uri-plan", required=True)
    parser.add_argument("--artifact-uri-json", required=True)
    parser.add_argument("--artifact-uri-metadata", required=True)
    parser.add_argument("--opentofu-version", required=True)
    ns = parser.parse_args(argv)
    try:
        body = format_summary(SummaryInput(
            plan_text=stdin_text,
            head_sha=ns.head_sha,
            plan_sha256=ns.plan_sha256,
            plan_json_sha256=ns.plan_json_sha256,
            generation_plan=ns.generation_plan,
            generation_json=ns.generation_json,
            generation_metadata=ns.generation_metadata,
            artifact_uri_plan=ns.artifact_uri_plan,
            artifact_uri_json=ns.artifact_uri_json,
            artifact_uri_metadata=ns.artifact_uri_metadata,
            opentofu_version=ns.opentofu_version,
        ))
    except ValueError as e:
        import sys as _sys
        print(str(e), file=_sys.stderr)
        return 1
    print(body, end="")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    import sys as _sys
    _sys.exit(_main(_sys.argv[1:], _sys.stdin.read()))
```

**Step 4: Run to verify pass**

```bash
uv run pytest tests/unit/test_iac_plan_diff_summary_cli.py -v
```

Expected: 2 PASS.

**Step 5: Commit**

```bash
git add tools/iac_plan_diff_summary.py tests/unit/test_iac_plan_diff_summary_cli.py
git commit -m "feat(iac): add CLI for plan-diff-summary formatter"
```

---

### Task 6: Bootstrap-script update — artifact-bucket IAM + WIF condition harden

This task does TWO things in `infra/scripts/setup_iac_backend.sh`:
1. Grant `roles/storage.objectCreator` on the artifact bucket to the CI SA (per §0 invariant 8).
2. Harden the WIF attribute condition so `workflow_dispatch` is admitted ONLY when the dispatched ref is `refs/heads/main` (per §0 invariants 2 + 3 — closes the "dispatch from a feature branch" attack).

**Files:**
- Modify: `infra/scripts/setup_iac_backend.sh`

**Step 1: Add the new 5d IAM block (artifact bucket)**

Find the existing 5a/5b/5c IAM-binding block (around lines 254–284). Add a **new 5d block** immediately after 5c, BEFORE the WIF section starts at the `# 6. Workload Identity Federation` header.

Insert:

```bash
# 5d. Artifact bucket: roles/storage.objectCreator — Phase C2 write grant.
# The plan-builder uploads {plan.tfplan, plan.json, metadata.json} to this
# bucket via the google-cloud-storage Python SDK. objectCreator allows
# storage.objects.create (the only verb needed for a fresh write into a
# versioned bucket — the SDK upload response populates Blob.generation
# in-band so no separate storage.objects.get call is needed; thus no
# objectViewer grant either). DELIBERATELY NOT objectAdmin: we do not want
# the plan-builder to be able to delete an earlier plan, and we do not
# want it to read other PRs' artifacts. Bucket-level IAM, never
# project-wide. Apply-side reads (C4) come from a different SA grant
# added in that slice.
gcloud storage buckets add-iam-policy-binding "gs://${ARTIFACT_BUCKET}" \
  --project="$PROJECT" \
  --member="serviceAccount:${CI_SA}" \
  --role="roles/storage.objectCreator" >/dev/null
echo "  ${CI_SA}: storage.objectCreator on gs://${ARTIFACT_BUCKET} (plan upload, write-only)"
```

**Step 2: Harden the WIF attribute condition**

Find the existing `WIF_ATTR_CONDITION` assignment (around lines 338-341 of `setup_iac_backend.sh`):

```bash
WIF_ATTR_CONDITION="assertion.repository == '${GITHUB_REPO}'"
WIF_ATTR_CONDITION+=" && assertion.workflow_ref.startsWith('${GITHUB_REPO}/${GITHUB_WORKFLOW}@')"
WIF_ATTR_CONDITION+=" && ((assertion.event_name == 'push' && assertion.ref == '${GITHUB_PUSH_REF}')"
WIF_ATTR_CONDITION+=" || assertion.event_name == 'workflow_dispatch')"
```

Replace the last two lines with:

```bash
# workflow_dispatch is admitted ONLY when the dispatched ref is the trusted
# branch — without this clause, a maintainer could dispatch the workflow
# from a feature branch whose iac.yml had been edited to exfiltrate the
# WIF-minted token. Pin BOTH push and dispatch to ${GITHUB_PUSH_REF}.
WIF_ATTR_CONDITION+=" && assertion.ref == '${GITHUB_PUSH_REF}'"
WIF_ATTR_CONDITION+=" && (assertion.event_name == 'push' || assertion.event_name == 'workflow_dispatch')"
```

Also update the in-script comment that explains the condition (around lines 317–337) to document the ref-pinning of workflow_dispatch. Change the bullet list (around line 320) from:

```
#   - AND the event is a TRUSTED TRIGGER, one of:
#       * push to the trusted branch (ref == "refs/heads/<branch>"), OR
#       * workflow_dispatch (a maintainer manually running the workflow)
```

to:

```
#   - AND the dispatched ref equals refs/heads/<trusted-branch>      <- NEW
#   - AND the event is a TRUSTED TRIGGER, one of:
#       * push to the trusted branch, OR
#       * workflow_dispatch (a maintainer manually running the workflow,
#         restricted by the previous clause to the trusted branch only —
#         dispatching from a feature branch is rejected)
```

**Step 3: Update the Phase C summary block at the end of the script**

Find the existing "PHASE C — wire this LATER" section (around lines 407–425) and append after the WIF provider/SA lines:

```bash
  Plan-builder write target (versioned, immutable per generation):
    gs://${ARTIFACT_BUCKET}
  Plan-builder IAM: storage.objectCreator on the bucket above.
  WIF: workflow_dispatch is admitted ONLY for ref=${GITHUB_PUSH_REF}.
```

**Step 4: Lint with shellcheck**

```bash
cd /home/adi/driftscribe/.worktrees/phase-c2-plan-builder
shellcheck infra/scripts/setup_iac_backend.sh
```

Expected: no new warnings.

**Step 5: Commit**

```bash
git add infra/scripts/setup_iac_backend.sh
git commit -m "infra(c2): artifact-bucket IAM + WIF condition pinned to ref=main"
```

> Operator follow-up (NOT in this slice's PR — recorded in PR description): operator re-runs `PROJECT=driftscribe-hack-2026 infra/scripts/setup_iac_backend.sh` after merge. Re-runs are idempotent; the script will UPDATE the existing WIF provider in place (line 347: `update-oidc`).

---

### Task 6b: Artifact uploader using google-cloud-storage SDK (two-step API)

Two-step API rather than one-shot: `upload_plan_and_json()` for the binary plan + JSON (called BEFORE metadata is built), and `upload_metadata()` for the final metadata.json (called AFTER metadata is built with the real generations). No placeholder metadata is ever uploaded — closes Codex rev-2 Important 2. `Blob.generation` is read directly from the upload response so the CI SA's IAM stays at `roles/storage.objectCreator` alone.

**Files:**
- Create: `tools/iac_plan_artifact_upload.py`
- Create: `tests/unit/test_iac_plan_artifact_upload.py`

**Step 1: Write failing tests**

`tests/unit/test_iac_plan_artifact_upload.py`:

```python
"""Tests for tools.iac_plan_artifact_upload — two-step uploader.

Step 1: upload_plan_and_json() — uploads plan.tfplan + plan.json,
returns (generation_plan, generation_json).
Step 2: upload_metadata() — uploads metadata.json, returns
generation_metadata.

The workflow calls Step 1, builds final metadata.json with the returned
generations, then calls Step 2. NO placeholder metadata is ever written.
"""

from __future__ import annotations

import json
import pathlib
from unittest.mock import MagicMock

import pytest

from tools.iac_plan_artifact_upload import (
    MetadataUploadInput,
    PlanJsonUploadInput,
    PlanJsonUploadResult,
    upload_metadata,
    upload_plan_and_json,
)


def _make_mock_bucket(generations: dict[str, int]) -> MagicMock:
    """Build a google-cloud-storage Bucket mock whose blob().upload_from_filename()
    populates blob.generation from `generations[blob_name]`."""
    bucket = MagicMock()

    def blob_factory(name: str) -> MagicMock:
        blob = MagicMock()
        blob.name = name

        def _do_upload(*_a, **_kw):
            blob.generation = generations[name]

        blob.upload_from_filename.side_effect = _do_upload
        return blob

    bucket.blob.side_effect = blob_factory
    return bucket


_HEAD = "a" * 40
_PREFIX = f"pr-42/{_HEAD}/run-1234567890-1"


# --- Step 1 tests ---------------------------------------------------------


def test_plan_and_json_returns_two_generations(tmp_path: pathlib.Path) -> None:
    plan = tmp_path / "plan.tfplan"
    plan.write_bytes(b"\x00binary plan\x00")
    pjson = tmp_path / "plan.json"
    pjson.write_text(json.dumps({"resource_changes": []}))

    bucket = _make_mock_bucket({
        f"{_PREFIX}/plan.tfplan": 1700000000000000,
        f"{_PREFIX}/plan.json":   1700000000000001,
    })

    result = upload_plan_and_json(PlanJsonUploadInput(
        bucket=bucket, object_prefix=_PREFIX,
        local_plan=plan, local_plan_json=pjson,
    ))

    assert isinstance(result, PlanJsonUploadResult)
    assert result.generation_plan == "1700000000000000"
    assert result.generation_json == "1700000000000001"


def test_plan_and_json_upload_order(tmp_path: pathlib.Path) -> None:
    """plan.tfplan first, then plan.json — deterministic for debugging."""
    plan = tmp_path / "plan.tfplan"; plan.write_bytes(b"x")
    pjson = tmp_path / "plan.json"; pjson.write_text("{}")
    bucket = _make_mock_bucket({
        f"{_PREFIX}/plan.tfplan": 1, f"{_PREFIX}/plan.json": 2,
    })
    upload_plan_and_json(PlanJsonUploadInput(
        bucket=bucket, object_prefix=_PREFIX,
        local_plan=plan, local_plan_json=pjson,
    ))
    names_in_order = [c.args[0] for c in bucket.blob.call_args_list]
    assert names_in_order == [f"{_PREFIX}/plan.tfplan", f"{_PREFIX}/plan.json"]


def test_plan_and_json_fails_if_local_file_missing(tmp_path: pathlib.Path) -> None:
    bucket = _make_mock_bucket({})
    with pytest.raises(FileNotFoundError):
        upload_plan_and_json(PlanJsonUploadInput(
            bucket=bucket, object_prefix=_PREFIX,
            local_plan=tmp_path / "nope.tfplan",
            local_plan_json=tmp_path / "nope.json",
        ))


def test_plan_and_json_rejects_unsafe_object_prefix(tmp_path: pathlib.Path) -> None:
    bucket = _make_mock_bucket({})
    # path traversal
    with pytest.raises(ValueError, match="object_prefix"):
        upload_plan_and_json(PlanJsonUploadInput(
            bucket=bucket, object_prefix="../escape/pr-42/aaa/run-1-1",
            local_plan=tmp_path, local_plan_json=tmp_path,
        ))
    # trailing slash
    with pytest.raises(ValueError, match="object_prefix"):
        upload_plan_and_json(PlanJsonUploadInput(
            bucket=bucket, object_prefix="pr-42/aaa/run-1-1/",
            local_plan=tmp_path, local_plan_json=tmp_path,
        ))
    # missing run segment
    with pytest.raises(ValueError, match="object_prefix"):
        upload_plan_and_json(PlanJsonUploadInput(
            bucket=bucket, object_prefix=f"pr-42/{_HEAD}",
            local_plan=tmp_path, local_plan_json=tmp_path,
        ))


# --- Step 2 tests ---------------------------------------------------------


def test_metadata_returns_one_generation(tmp_path: pathlib.Path) -> None:
    meta = tmp_path / "metadata.json"
    meta.write_text('{"schema_version":"c2.v1"}\n')
    bucket = _make_mock_bucket({f"{_PREFIX}/metadata.json": 1700000000000002})
    gen = upload_metadata(MetadataUploadInput(
        bucket=bucket, object_prefix=_PREFIX, local_metadata=meta,
    ))
    assert gen == "1700000000000002"


def test_metadata_uses_correct_blob_path(tmp_path: pathlib.Path) -> None:
    meta = tmp_path / "metadata.json"; meta.write_text("{}")
    bucket = _make_mock_bucket({f"{_PREFIX}/metadata.json": 1})
    upload_metadata(MetadataUploadInput(
        bucket=bucket, object_prefix=_PREFIX, local_metadata=meta,
    ))
    bucket.blob.assert_called_once_with(f"{_PREFIX}/metadata.json")


def test_metadata_fails_if_local_file_missing(tmp_path: pathlib.Path) -> None:
    bucket = _make_mock_bucket({})
    with pytest.raises(FileNotFoundError):
        upload_metadata(MetadataUploadInput(
            bucket=bucket, object_prefix=_PREFIX,
            local_metadata=tmp_path / "nope.json",
        ))


def test_metadata_rejects_unsafe_object_prefix(tmp_path: pathlib.Path) -> None:
    bucket = _make_mock_bucket({})
    with pytest.raises(ValueError, match="object_prefix"):
        upload_metadata(MetadataUploadInput(
            bucket=bucket, object_prefix="../escape", local_metadata=tmp_path,
        ))
```

**Step 2: Run failing tests**

```bash
uv run pytest tests/unit/test_iac_plan_artifact_upload.py -v
```

Expected: 8 FAIL (module missing).

**Step 3: Implement the module**

`tools/iac_plan_artifact_upload.py`:

```python
"""Upload plan artifacts to the C2 artifact bucket via google-cloud-storage SDK.

Two-step API:
- :func:`upload_plan_and_json` — uploads plan.tfplan + plan.json, returns
  their generations. Called by the workflow BEFORE metadata is built.
- :func:`upload_metadata` — uploads the final metadata.json, returns its
  generation. Called AFTER metadata is rebuilt with the real plan/json
  generations.

Why two steps and not one: metadata.json's content depends on the
generations of plan.tfplan + plan.json (Codex rev-2 Important 2 — never
upload a placeholder metadata file). The two-step API keeps both calls
testable without the workflow needing inline Python.

Why the SDK and not `gcloud storage cp`: ``Blob.upload_from_filename()``
populates ``Blob.generation`` from the upload response in-band, so we
do not need ``storage.objects.get`` (the CI SA's IAM stays at
``roles/storage.objectCreator``).
"""
from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass
from typing import Any

_OBJECT_PREFIX_RE = re.compile(r"^pr-[1-9][0-9]*/[0-9a-f]{40}/run-[1-9][0-9]*-[1-9][0-9]*$")


def _check_prefix(prefix: str) -> None:
    if not _OBJECT_PREFIX_RE.fullmatch(prefix):
        raise ValueError(
            f"object_prefix: must match 'pr-<N>/<head_sha>/run-<id>-<attempt>' "
            f"(got {prefix!r})"
        )


def _upload_one(bucket: Any, blob_name: str, local: pathlib.Path) -> str:
    if not local.exists():
        raise FileNotFoundError(str(local))
    blob = bucket.blob(blob_name)
    blob.upload_from_filename(str(local))
    gen = getattr(blob, "generation", None)
    if gen is None:
        raise RuntimeError(f"upload of {blob_name} returned no generation (SDK contract violation)")
    return str(gen)


# --- Step 1: plan.tfplan + plan.json ------------------------------------


@dataclass(frozen=True)
class PlanJsonUploadInput:
    bucket: Any
    object_prefix: str
    local_plan: pathlib.Path
    local_plan_json: pathlib.Path


@dataclass(frozen=True)
class PlanJsonUploadResult:
    generation_plan: str
    generation_json: str


def upload_plan_and_json(inp: PlanJsonUploadInput) -> PlanJsonUploadResult:
    _check_prefix(inp.object_prefix)
    gen_plan = _upload_one(inp.bucket, f"{inp.object_prefix}/plan.tfplan", inp.local_plan)
    gen_json = _upload_one(inp.bucket, f"{inp.object_prefix}/plan.json",   inp.local_plan_json)
    return PlanJsonUploadResult(generation_plan=gen_plan, generation_json=gen_json)


# --- Step 2: metadata.json -----------------------------------------------


@dataclass(frozen=True)
class MetadataUploadInput:
    bucket: Any
    object_prefix: str
    local_metadata: pathlib.Path


def upload_metadata(inp: MetadataUploadInput) -> str:
    _check_prefix(inp.object_prefix)
    return _upload_one(inp.bucket, f"{inp.object_prefix}/metadata.json", inp.local_metadata)


# --- CLI -----------------------------------------------------------------

def _main(argv: list[str]) -> int:
    import argparse
    import sys as _sys
    parser = argparse.ArgumentParser(prog="iac_plan_artifact_upload")
    parser.add_argument("--mode", required=True, choices=["plan-and-json", "metadata"])
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--object-prefix", required=True)
    parser.add_argument("--local-plan", type=pathlib.Path)
    parser.add_argument("--local-plan-json", type=pathlib.Path)
    parser.add_argument("--local-metadata", type=pathlib.Path)
    ns = parser.parse_args(argv)

    # Defer the SDK import so unit tests do not require google-cloud-storage.
    from google.cloud import storage  # type: ignore
    client = storage.Client()
    bucket = client.bucket(ns.bucket)

    try:
        if ns.mode == "plan-and-json":
            if ns.local_plan is None or ns.local_plan_json is None:
                raise ValueError("--mode plan-and-json requires --local-plan and --local-plan-json")
            r = upload_plan_and_json(PlanJsonUploadInput(
                bucket=bucket, object_prefix=ns.object_prefix,
                local_plan=ns.local_plan, local_plan_json=ns.local_plan_json,
            ))
            print(f"GEN_PLAN={r.generation_plan}")
            print(f"GEN_JSON={r.generation_json}")
        else:  # mode == "metadata"
            if ns.local_metadata is None:
                raise ValueError("--mode metadata requires --local-metadata")
            gen = upload_metadata(MetadataUploadInput(
                bucket=bucket, object_prefix=ns.object_prefix,
                local_metadata=ns.local_metadata,
            ))
            print(f"GEN_METADATA={gen}")
    except (ValueError, FileNotFoundError) as e:
        print(str(e), file=_sys.stderr)
        return 1

    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    import sys as _sys
    _sys.exit(_main(_sys.argv[1:]))
```

**Step 4: Run tests to pass**

```bash
uv run pytest tests/unit/test_iac_plan_artifact_upload.py -v
```

Expected: 5 PASS.

**Step 5: Commit**

```bash
git add tools/iac_plan_artifact_upload.py tests/unit/test_iac_plan_artifact_upload.py
git commit -m "feat(iac): C2 artifact uploader via google-cloud-storage SDK"
```

---

### Task 7a: Author the workflow_dispatch trigger + input shape

The plan-builder is a new JOB inside the existing `.github/workflows/iac.yml`. Step 7a only adds the trigger + the input shape + concurrency. The job body is built up in 7b–7d.

**Files:**
- Modify: `.github/workflows/iac.yml`

**Step 1: Add the workflow_dispatch trigger**

Find the existing `on:` block (lines 18–35). Add a `workflow_dispatch:` section AFTER the `pull_request:` block, preserving the existing comment:

```yaml
on:
  pull_request:
    types: [opened, synchronize, reopened, labeled, unlabeled]
    # (existing comment block — leave as-is)
  workflow_dispatch:
    # Phase C2 plan-builder trigger. The WIF OIDC condition (provisioned by
    # infra/scripts/setup_iac_backend.sh §6) admits ONLY push-to-main and
    # workflow_dispatch from this exact workflow file. pull_request is
    # excluded so fork PRs never obtain GCP credentials (design §11.8).
    # Operator-initiated: maintainer clicks "Run workflow" with the PR number.
    inputs:
      pr_number:
        description: 'PR number to plan (e.g. 42 — must be open and from the canonical repo)'
        required: true
        type: string
```

**Step 2: Gate the existing static-gate + tofu jobs to pull_request only**

The static-gate and tofu jobs were written for PR context (they reference `github.event.pull_request.base.sha`). On `workflow_dispatch` those values are empty and the jobs would crash. Add `if: github.event_name == 'pull_request'` to BOTH jobs.

In `static-gate` job header (around line 45), add `if:` immediately under `runs-on:`:

```yaml
  static-gate:
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    timeout-minutes: 10
```

In `tofu` job header (around line 94):

```yaml
  tofu:
    needs: static-gate
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    timeout-minutes: 10
```

**Step 3: Update the comment above `permissions:`**

Find the `permissions: contents: read` block (around lines 37–38). Update the comment to acknowledge the workflow now has two trigger paths:

```yaml
# Top-level permissions are the floor for ALL jobs: only `contents: read`. The
# plan-builder job (Phase C2) overrides its own permissions block to ADD
# `id-token: write` (WIF) and `pull-requests: write` (post diff). No other
# job needs them.
permissions:
  contents: read
```

**Step 4: Make workflow-level cancel-in-progress conditional on event_name**

The current workflow-level concurrency block (lines 40-42) has `cancel-in-progress: true`. That cancels a plan-builder run mid-upload if a second dispatch is kicked off. Fix:

Find:

```yaml
concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
```

Replace with:

```yaml
concurrency:
  # PR runs share a group per ref so a force-push cancels the older run
  # (saves CI cost; the gate is fast to redo). Workflow_dispatch runs
  # carry the event in the group so they NEVER collide with PR runs, and
  # `cancel-in-progress` flips to false so a partial artifact upload
  # cannot be aborted by a second dispatch — the job-level concurrency in
  # the plan-builder job serializes dispatches per PR (see Task 7b).
  group: ${{ github.workflow }}-${{ github.event_name }}-${{ github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}
```

**Step 5: Run `actionlint` to verify the YAML is still valid**

```bash
cd /home/adi/driftscribe/.worktrees/phase-c2-plan-builder
# actionlint is the standard GHA linter; it ships in uv-managed deps if added,
# or via `go install github.com/rhysd/actionlint/cmd/actionlint@latest`.
# If not available, fallback: `python -c 'import yaml; yaml.safe_load(open(".github/workflows/iac.yml"))'`
python -c 'import yaml; yaml.safe_load(open(".github/workflows/iac.yml"))'
```

Expected: no exception.

**Step 6: Commit**

```bash
git add .github/workflows/iac.yml
git commit -m "ci(iac): add workflow_dispatch trigger + gate existing jobs to PR-only"
```

---

### Task 7b: Plan-builder job skeleton — checkout + SHA resolution

**Files:**
- Modify: `.github/workflows/iac.yml`

**Step 1: Add the plan-builder job AFTER the existing `tofu` job**

Add this entire job block at the end of `jobs:` (after the `tofu` job ends at line 116):

```yaml
  plan-builder:
    # Two-layer gate: event AND ref must both pass. The WIF attribute
    # condition (bootstrap-provisioned) ALSO requires ref==refs/heads/main
    # for workflow_dispatch — this `if:` is the workflow-side belt-and-
    # suspenders. Either layer alone closes the "dispatch from feature
    # branch to exfiltrate creds" attack; both layers together keep the
    # WIF token from ever being requested if the ref isn't main.
    if: github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    timeout-minutes: 20
    # WIF needs id-token: write; diff comment needs pull-requests: write.
    # contents: read inherits from the workflow floor. NEVER write contents
    # or expand permissions — this job runs against live state.
    permissions:
      contents: read
      id-token: write
      pull-requests: write
    # One plan-builder per PR at a time; do NOT cancel-in-progress (a
    # cancelled mid-upload could leave a partial artifact in the bucket).
    concurrency:
      group: iac-plan-builder-pr-${{ inputs.pr_number }}
      cancel-in-progress: false
    steps:
      - name: Validate PR number is a positive integer
        env:
          PR_RAW: ${{ inputs.pr_number }}
        run: |
          if ! printf '%s' "$PR_RAW" | grep -Eq '^[1-9][0-9]{0,9}$'; then
            echo "::error::pr_number must be a positive integer (got $PR_RAW)"
            exit 1
          fi
          echo "PR_NUMBER=$PR_RAW" >> "$GITHUB_ENV"

      - name: Resolve PR head/base SHAs + refuse fork/non-main-base
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          set -euo pipefail
          # Pull PR metadata. baseRefOid is the SHA the PR is mergeable INTO at
          # this moment; headRefOid is the PR head. Both are 40-hex.
          PR_JSON=$(gh pr view "$PR_NUMBER" \
            --repo "${GITHUB_REPOSITORY}" \
            --json headRefOid,baseRefOid,baseRefName,headRefName,state,isCrossRepository)
          STATE=$(printf '%s' "$PR_JSON" | jq -r .state)
          CROSS=$(printf '%s' "$PR_JSON" | jq -r .isCrossRepository)
          BASE_REF=$(printf '%s' "$PR_JSON" | jq -r .baseRefName)
          if [ "$STATE" != "OPEN" ]; then
            echo "::error::PR #$PR_NUMBER is not OPEN (state=$STATE) — refusing to plan"
            exit 1
          fi
          if [ "$CROSS" = "true" ]; then
            echo "::error::PR #$PR_NUMBER is from a fork (isCrossRepository=true) — refusing to plan (design §11.8 forbids fork-PR auth)"
            exit 1
          fi
          if [ "$BASE_REF" != "main" ]; then
            echo "::error::PR #$PR_NUMBER base is $BASE_REF, not main — refusing to plan (artifact metadata.repo invariants assume base=main)"
            exit 1
          fi
          HEAD_SHA=$(printf '%s' "$PR_JSON" | jq -r .headRefOid)
          BASE_SHA=$(printf '%s' "$PR_JSON" | jq -r .baseRefOid)
          HEAD_REF=$(printf '%s' "$PR_JSON" | jq -r .headRefName)
          printf '%s' "$HEAD_SHA" | grep -Eq '^[0-9a-f]{40}$' || { echo "::error::headRefOid is not 40-hex"; exit 1; }
          printf '%s' "$BASE_SHA" | grep -Eq '^[0-9a-f]{40}$' || { echo "::error::baseRefOid is not 40-hex"; exit 1; }
          echo "HEAD_SHA=$HEAD_SHA" >> "$GITHUB_ENV"
          echo "BASE_SHA=$BASE_SHA" >> "$GITHUB_ENV"
          echo "BASE_REF=$BASE_REF" >> "$GITHUB_ENV"
          echo "HEAD_REF=$HEAD_REF" >> "$GITHUB_ENV"
          echo "Resolved PR #$PR_NUMBER: head $HEAD_SHA ($HEAD_REF), base $BASE_SHA ($BASE_REF)"

      - name: Checkout the PR head SHA (pinned)
        # We pin to HEAD_SHA resolved server-side. If a force-push happened
        # between resolution and now, the pinned SHA may have been deleted
        # from the branch tip but git still serves it (refs are mutable,
        # commits are immutable). The diff-guard below runs against THIS
        # exact pair (BASE_SHA, HEAD_SHA), not against whatever the PR
        # currently points at — closing the TOCTOU Codex flagged.
        uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5 # v4.3.1
        with:
          ref: ${{ env.HEAD_SHA }}
          fetch-depth: 0

      - name: Fetch the base SHA so git can resolve it locally
        run: |
          set -euo pipefail
          # checkout@v4 with fetch-depth=0 fetches the default branch tip and
          # the ref, but not necessarily ALL named branches. Force-fetch the
          # base SHA so `git diff $BASE_SHA $HEAD_SHA` resolves locally.
          git fetch --no-tags origin "$BASE_SHA" || true
          git rev-parse --verify "$BASE_SHA" >/dev/null
          git rev-parse --verify "$HEAD_SHA" >/dev/null

      - name: PR-touches-only-iac diff-guard (PURE GIT, against pinned SHA pair)
        # CRITICAL: this MUST come before `uv sync` or any Python invocation,
        # because once PR-controlled code runs we cannot trust its output.
        # We use `git diff --name-only $BASE_SHA $HEAD_SHA` — both SHAs are
        # immutable git objects we just fetched; no API call that could
        # observe a later force-push (closes Codex rev-2 blocker on the
        # server-side /files endpoint TOCTOU). If any changed path is
        # outside iac/, refuse to plan. After this passes, the checked-out
        # tools/, tests/, uv.lock, pyproject.toml etc. are byte-identical
        # to main (the PR did not touch them), so the trusted Python
        # helpers running after WIF auth are NOT PR-controlled.
        #
        # Implementation: pure shell + git only (no jq, no curl, no Python).
        #
        # `--no-renames`: forces git to report renames as add+delete pairs.
        # Without it, a file renamed FROM `tools/x.py` TO `iac/x.py` would
        # only surface as `iac/x.py` and slip past this guard. With it, the
        # original `tools/x.py` is reported as deleted — failing the guard.
        # `-z`: NUL-delimited records so filenames with newlines/quotes
        # cannot break the loop.
        run: |
          set -euo pipefail
          BAD=""
          ANY=0
          while IFS= read -r -d '' path; do
            ANY=1
            case "$path" in
              iac/*) ;;
              *) BAD="${BAD}${path}"$'\n' ;;
            esac
          done < <(git diff --name-only --no-renames -z "$BASE_SHA" "$HEAD_SHA")
          if [ "$ANY" -eq 0 ]; then
            echo "::error::No files differ between BASE_SHA=$BASE_SHA and HEAD_SHA=$HEAD_SHA — refusing to plan."
            exit 1
          fi
          if [ -n "$BAD" ]; then
            echo "::error::PR head (SHA $HEAD_SHA) touches paths outside iac/ — refusing to plan."
            echo "Offending paths:"
            printf '%s' "$BAD"
            exit 1
          fi
          # Recount for the success log line (separate pass over the same NUL stream).
          N=$(git diff --name-only --no-renames -z "$BASE_SHA" "$HEAD_SHA" | tr '\0' '\n' | grep -c .)
          echo "diff-guard PASS — $N file(s) changed, all under iac/."
```

**Step 2: Lint YAML**

```bash
python -c 'import yaml; yaml.safe_load(open(".github/workflows/iac.yml"))'
```

Expected: no exception.

**Step 3: Commit**

```bash
git add .github/workflows/iac.yml
git commit -m "ci(iac): add plan-builder job skeleton (PR validation + SHA resolution)"
```

---

### Task 7c: Plan-builder job — static-gate re-run + uv/tofu setup + WIF auth

**Files:**
- Modify: `.github/workflows/iac.yml`

**Step 1: Append steps to the plan-builder job**

Add these steps AT THE END of the `plan-builder` job's `steps:` list:

```yaml
      - name: Setup uv
        uses: astral-sh/setup-uv@38f3f104447c67c051c4a08e39b64a148898af3a # v4.2.0
        with:
          cache: true
          cache-dependency-glob: 'uv.lock'

      - name: Setup Python 3.12
        uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5.6.0
        with:
          python-version: '3.12'

      - name: Sync dependencies
        run: uv sync --all-extras

      - name: Re-run static HCL gate (HARDCODED MODE=agent)
        # Defense-in-depth: the PR-side static gate ran against an earlier
        # snapshot of head. The plan-builder runs against the SHA the
        # maintainer pinned at dispatch. We force MODE=agent regardless of
        # PR label/branch — agent mode rejects ALL non-iac/ paths. Combined
        # with the pure-git diff-guard above, this is the second of two
        # walls keeping PR-controlled tools/ out of the trusted execution
        # surface. If this gate fails for ANY reason, no GCP auth happens.
        run: |
          uv run python -m tools.iac_static_gate \
            --base "$BASE_SHA" \
            --head "$HEAD_SHA" \
            --mode agent

      - name: Setup OpenTofu 1.12.0 (PINNED)
        # Pin the exact binary version. The action's default is "latest"
        # which would drift — C4 compares metadata.opentofu_version to the
        # apply-side configured version and would refuse on mismatch.
        uses: opentofu/setup-opentofu@847eaa4afeb791b06daa46e8eafa8b1b68d7cfb4 # v2.0.1
        with:
          tofu_version: '1.12.0'

      - name: Authenticate to GCP via WIF
        # Short-lived token via Workload Identity Federation. The provider's
        # attribute condition (bootstrap-provisioned) only mints tokens
        # when ALL of: workflow_ref starts with this workflow path, ref ==
        # refs/heads/main, and event_name in {push, workflow_dispatch}.
        # No JSON key on disk.
        id: gcp-auth
        uses: google-github-actions/auth@b7593ed2efd1c1617e1b0254da33b86225adb2a5 # v2.1.7
        with:
          workload_identity_provider: ${{ secrets.GCP_WIF_PROVIDER }}
          service_account: ${{ secrets.GCP_TOFU_PLAN_BUILDER_SA }}

      - name: Setup gcloud CLI (uses the WIF credential from previous step)
        uses: google-github-actions/setup-gcloud@77e7a554d41e2ee56fc945c52dfd3f33d12def9a # v2.1.4
```

**Step 2: Lint YAML**

```bash
python -c 'import yaml; yaml.safe_load(open(".github/workflows/iac.yml"))'
```

**Step 3: Commit**

```bash
git add .github/workflows/iac.yml
git commit -m "ci(iac): add plan-builder static-gate re-run + WIF auth steps"
```

---

### Task 7d: Plan-builder job — tofu plan + denylist + upload + PR comment

**Files:**
- Modify: `.github/workflows/iac.yml`

**Step 1: Append the final block of steps**

Add at the end of the `plan-builder` job's `steps:`:

```yaml
      - name: tofu init (with backend — needs WIF auth)
        env:
          KMS_KEY: ${{ secrets.GCP_TOFU_STATE_KMS_KEY }}
        run: |
          tofu -chdir=iac init -lockfile=readonly \
            -var "tofu_state_kms_key=$KMS_KEY"

      - name: tofu plan → plan.tfplan
        env:
          KMS_KEY: ${{ secrets.GCP_TOFU_STATE_KMS_KEY }}
        run: |
          tofu -chdir=iac plan \
            -out=plan.tfplan \
            -input=false \
            -var "tofu_state_kms_key=$KMS_KEY"

      - name: tofu show -json → plan.json
        run: |
          tofu -chdir=iac show -json plan.tfplan > iac/plan.json

      - name: Run C1 denylist on plan.json (FAIL BEFORE upload on violation)
        run: |
          # Exit 0 = clean, 1 = policy violation, 2 = usage/IO error. We fail
          # the workflow on either nonzero, BEFORE the artifact gets uploaded.
          uv run python -m tools.iac_plan_denylist iac/plan.json

      - name: Compute artifact hashes + read tofu version + lockfile hash
        run: |
          set -euo pipefail
          PLAN_SHA256=$(sha256sum iac/plan.tfplan | awk '{print $1}')
          PLAN_JSON_SHA256=$(sha256sum iac/plan.json | awk '{print $1}')
          LOCKFILE_SHA256=$(sha256sum iac/.terraform.lock.hcl | awk '{print $1}')
          TOFU_VERSION=$(tofu version -json | jq -r .terraform_version)
          echo "PLAN_SHA256=$PLAN_SHA256" >> "$GITHUB_ENV"
          echo "PLAN_JSON_SHA256=$PLAN_JSON_SHA256" >> "$GITHUB_ENV"
          echo "LOCKFILE_SHA256=$LOCKFILE_SHA256" >> "$GITHUB_ENV"
          echo "TOFU_VERSION=$TOFU_VERSION" >> "$GITHUB_ENV"

      - name: Compute the artifact path prefix + URIs
        env:
          ARTIFACT_BUCKET: driftscribe-hack-2026-tofu-artifacts
        run: |
          set -euo pipefail
          OBJECT_PREFIX="pr-${PR_NUMBER}/${HEAD_SHA}/run-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
          echo "OBJECT_PREFIX=$OBJECT_PREFIX" >> "$GITHUB_ENV"
          echo "ARTIFACT_URI_PLAN=gs://${ARTIFACT_BUCKET}/${OBJECT_PREFIX}/plan.tfplan" >> "$GITHUB_ENV"
          echo "ARTIFACT_URI_JSON=gs://${ARTIFACT_BUCKET}/${OBJECT_PREFIX}/plan.json"   >> "$GITHUB_ENV"
          echo "ARTIFACT_URI_METADATA=gs://${ARTIFACT_BUCKET}/${OBJECT_PREFIX}/metadata.json" >> "$GITHUB_ENV"

      - name: Upload plan.tfplan + plan.json (capture generations)
        # Step 1 of the two-step uploader. Reads Blob.generation in-band
        # from the upload response — no storage.objects.get call needed.
        run: |
          set -euo pipefail
          uv run python -m tools.iac_plan_artifact_upload \
            --mode plan-and-json \
            --bucket driftscribe-hack-2026-tofu-artifacts \
            --object-prefix "$OBJECT_PREFIX" \
            --local-plan iac/plan.tfplan \
            --local-plan-json iac/plan.json \
            | tee /tmp/upload-step1.env
          grep '^GEN_PLAN=' /tmp/upload-step1.env >> "$GITHUB_ENV"
          grep '^GEN_JSON=' /tmp/upload-step1.env >> "$GITHUB_ENV"

      - name: Build the final metadata.json (REAL generations, no placeholder)
        env:
          META_REPO: ${{ github.repository }}
          META_PR_NUMBER: ${{ env.PR_NUMBER }}
          META_HEAD_SHA: ${{ env.HEAD_SHA }}
          META_BASE_SHA: ${{ env.BASE_SHA }}
          META_WORKFLOW_RUN_ID: ${{ github.run_id }}
          META_WORKFLOW_RUN_ATTEMPT: ${{ github.run_attempt }}
          META_ARTIFACT_URI_PLAN: ${{ env.ARTIFACT_URI_PLAN }}
          META_ARTIFACT_URI_JSON: ${{ env.ARTIFACT_URI_JSON }}
          META_GENERATION_PLAN: ${{ env.GEN_PLAN }}
          META_GENERATION_JSON: ${{ env.GEN_JSON }}
          META_PLAN_SHA256: ${{ env.PLAN_SHA256 }}
          META_PLAN_JSON_SHA256: ${{ env.PLAN_JSON_SHA256 }}
          META_OPENTOFU_VERSION: ${{ env.TOFU_VERSION }}
          META_PROVIDER_LOCKFILE_SHA256: ${{ env.LOCKFILE_SHA256 }}
        run: |
          uv run python -m tools.iac_plan_metadata > iac/metadata.json
          cat iac/metadata.json

      - name: Upload metadata.json (capture generation)
        # Step 2 of the two-step uploader. No placeholder metadata ever
        # lands on the bucket — metadata.json is built and uploaded ONCE
        # with the real plan/json generations baked in.
        run: |
          set -euo pipefail
          uv run python -m tools.iac_plan_artifact_upload \
            --mode metadata \
            --bucket driftscribe-hack-2026-tofu-artifacts \
            --object-prefix "$OBJECT_PREFIX" \
            --local-metadata iac/metadata.json \
            | tee /tmp/upload-step2.env
          grep '^GEN_METADATA=' /tmp/upload-step2.env >> "$GITHUB_ENV"

      - name: Post tofu show diff to PR
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          set -euo pipefail
          tofu -chdir=iac show -no-color plan.tfplan > /tmp/plan.txt
          uv run python -m tools.iac_plan_diff_summary \
            --head-sha="$HEAD_SHA" \
            --plan-sha256="$PLAN_SHA256" \
            --plan-json-sha256="$PLAN_JSON_SHA256" \
            --generation-plan="$GEN_PLAN" \
            --generation-json="$GEN_JSON" \
            --generation-metadata="$GEN_METADATA" \
            --artifact-uri-plan="$ARTIFACT_URI_PLAN" \
            --artifact-uri-json="$ARTIFACT_URI_JSON" \
            --artifact-uri-metadata="$ARTIFACT_URI_METADATA" \
            --opentofu-version="$TOFU_VERSION" \
            < /tmp/plan.txt > /tmp/comment.md
          gh pr comment "$PR_NUMBER" --repo "$GITHUB_REPOSITORY" --body-file /tmp/comment.md
```

**Step 2: Lint YAML**

```bash
python -c 'import yaml; yaml.safe_load(open(".github/workflows/iac.yml"))'
```

**Step 3: Commit**

```bash
git add .github/workflows/iac.yml
git commit -m "ci(iac): plan-builder body — tofu plan, denylist, upload, PR comment"
```

---

### Task 7e: Workflow YAML structural invariant tests

The plan-builder workflow is the trust boundary. Structural invariants — "no `pull_request_target` anywhere", "only `plan-builder` gets `id-token: write`", "the diff-guard step precedes WIF auth", "denylist precedes upload" — must be asserted by tests, not by reviewer attention.

**Files:**
- Create: `tests/unit/test_iac_workflow_structure.py`

**Step 1: Write failing tests**

`tests/unit/test_iac_workflow_structure.py`:

```python
"""Structural invariants for .github/workflows/iac.yml.

These tests parse the YAML and assert security-critical structural facts
that no amount of "looks-fine" review can keep stable on its own. A red
test here is a release-blocker.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml


@pytest.fixture(scope="module")
def workflow() -> dict:
    p = pathlib.Path(__file__).resolve().parents[2] / ".github" / "workflows" / "iac.yml"
    with p.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _steps_of(workflow: dict, job_name: str) -> list[dict]:
    return list(workflow["jobs"][job_name].get("steps", []))


def _step_text(step: dict) -> str:
    """Render a step to a coarse text blob for keyword searches.

    Concatenates `run`, `uses`, `name`, and env values. Adequate for the
    presence/order assertions we make below.
    """
    parts = [step.get("name", ""), step.get("run", ""), step.get("uses", "")]
    for v in (step.get("env") or {}).values():
        parts.append(str(v))
    return "\n".join(parts)


def test_no_pull_request_target_trigger(workflow: dict):
    on = workflow.get(True) or workflow.get("on")  # PyYAML loads `on:` as bool True
    assert "pull_request_target" not in (on or {}), \
        "pull_request_target runs PR-controlled code with repo secrets — forbidden"


def test_only_plan_builder_has_id_token_write(workflow: dict):
    for name, job in workflow["jobs"].items():
        perms = job.get("permissions") or {}
        has_id = perms.get("id-token") == "write"
        if name == "plan-builder":
            assert has_id, "plan-builder MUST have id-token: write for WIF"
        else:
            assert not has_id, f"{name} must NOT have id-token: write (no WIF outside plan-builder)"


def test_plan_builder_if_pins_workflow_dispatch_and_main_ref(workflow: dict):
    job = workflow["jobs"]["plan-builder"]
    if_clause = job["if"]
    assert "workflow_dispatch" in if_clause
    assert "refs/heads/main" in if_clause


def test_static_gate_and_tofu_are_pr_only(workflow: dict):
    for name in ("static-gate", "tofu"):
        if_clause = workflow["jobs"][name].get("if", "")
        assert "pull_request" in if_clause, f"{name} must be gated to pull_request only"


def test_workflow_level_cancel_in_progress_is_conditional(workflow: dict):
    concurrency = workflow.get("concurrency") or {}
    cip = concurrency.get("cancel-in-progress")
    assert isinstance(cip, str) and "pull_request" in cip, \
        "workflow-level cancel-in-progress must be event-conditional (true on PR, false on dispatch)"


def test_diff_guard_uses_git_not_gh_api(workflow: dict):
    """The diff-guard must use local git against the pinned SHA pair, NOT
    `gh api .../pulls/<N>/files` — the latter reflects current PR state,
    not the resolved HEAD_SHA, leaving a force-push TOCTOU window."""
    for s in _steps_of(workflow, "plan-builder"):
        text = _step_text(s)
        if "diff-guard" in text:
            run = s.get("run", "")
            assert "git diff" in run, "diff-guard must call `git diff`"
            assert "$BASE_SHA" in run and "$HEAD_SHA" in run, \
                "diff-guard must diff between the resolved BASE_SHA and HEAD_SHA"
            assert "/pulls/" not in run and "gh api" not in run, \
                "diff-guard must NOT use the gh API /pulls/<N>/files endpoint (TOCTOU)"
            assert "--no-renames" in run, \
                "diff-guard must use --no-renames so a file renamed FROM outside iac/ TO iac/ does not slip through"
            assert " -z" in run or " -z " in run or "\t-z" in run, \
                "diff-guard must use -z (NUL-delimited) so filenames with newlines/quotes cannot break parsing"
            return
    raise AssertionError("plan-builder has no diff-guard step")


def test_diff_guard_runs_before_uv_sync(workflow: dict):
    """Must precede any Python invocation — otherwise PR-controlled
    uv.lock/pyproject.toml/tools could influence the trusted execution."""
    steps = _steps_of(workflow, "plan-builder")
    diff_idx = None
    uv_idx = None
    for i, s in enumerate(steps):
        text = _step_text(s)
        if "diff-guard" in text and diff_idx is None:
            diff_idx = i
        if ("uv sync" in s.get("run", "") or "astral-sh/setup-uv" in s.get("uses", "")) and uv_idx is None:
            uv_idx = i
    assert diff_idx is not None, "missing diff-guard step"
    assert uv_idx is not None, "missing uv setup/sync step"
    assert diff_idx < uv_idx, \
        "diff-guard MUST run BEFORE uv sync (PR's uv.lock cannot be trusted before)"


def test_static_gate_rerun_uses_hardcoded_agent_mode(workflow: dict):
    """Plan-builder's static-gate re-run must NOT derive MODE from PR labels/branch."""
    for s in _steps_of(workflow, "plan-builder"):
        run = s.get("run", "")
        if "iac_static_gate" in run:
            assert "--mode agent" in run, \
                "plan-builder static-gate re-run must hardcode --mode agent"
            return
    raise AssertionError("plan-builder has no iac_static_gate step")


def test_denylist_precedes_artifact_upload(workflow: dict):
    steps = _steps_of(workflow, "plan-builder")
    denylist_idx = None
    upload_idx = None
    for i, s in enumerate(steps):
        text = _step_text(s)
        if "iac_plan_denylist" in text and denylist_idx is None:
            denylist_idx = i
        if "iac_plan_artifact_upload" in text and upload_idx is None:
            upload_idx = i
    assert denylist_idx is not None, "missing denylist invocation in plan-builder"
    assert upload_idx is not None, "missing artifact upload invocation in plan-builder"
    assert denylist_idx < upload_idx, \
        "denylist MUST run BEFORE the artifact upload (else a denied plan becomes an artifact)"


def test_wif_auth_uses_repo_secrets_not_inline(workflow: dict):
    for s in _steps_of(workflow, "plan-builder"):
        if s.get("uses", "").startswith("google-github-actions/auth@"):
            with_block = s.get("with", {})
            assert with_block.get("workload_identity_provider", "").startswith("${{ secrets."), \
                "workload_identity_provider must be a secret reference, not inline"
            assert with_block.get("service_account", "").startswith("${{ secrets."), \
                "service_account must be a secret reference, not inline"
            return
    raise AssertionError("plan-builder has no google-github-actions/auth step")


def test_setup_opentofu_pins_tofu_version(workflow: dict):
    for s in _steps_of(workflow, "plan-builder"):
        if s.get("uses", "").startswith("opentofu/setup-opentofu@"):
            with_block = s.get("with", {})
            assert "tofu_version" in with_block, \
                "setup-opentofu MUST pin tofu_version (C4 compares this)"
            return
    raise AssertionError("plan-builder has no setup-opentofu step")


def test_plan_builder_concurrency_does_not_cancel(workflow: dict):
    job = workflow["jobs"]["plan-builder"]
    conc = job.get("concurrency") or {}
    assert conc.get("cancel-in-progress") is False, \
        "plan-builder job concurrency must NOT cancel-in-progress (would orphan an upload)"
    assert "inputs.pr_number" in conc.get("group", ""), \
        "plan-builder concurrency group must include inputs.pr_number for per-PR serialization"
```

**Step 2: Run failing tests**

```bash
uv run pytest tests/unit/test_iac_workflow_structure.py -v
```

Expected (BEFORE Tasks 7a-7d are implemented): FAIL (workflow lacks the plan-builder job entirely). These tests are intended to be authored AFTER 7a-7d so they go GREEN at this point.

**Step 3: Run after 7a-7d to verify GREEN**

```bash
uv run pytest tests/unit/test_iac_workflow_structure.py -v
```

Expected: 11 PASS.

**Step 4: Commit**

```bash
git add tests/unit/test_iac_workflow_structure.py
git commit -m "test(iac): structural invariants for plan-builder workflow"
```

---

### Task 8: Update iac/README.md + .github/CODEOWNERS

**Files:**
- Modify: `iac/README.md`
- Modify: `.github/CODEOWNERS`

**Step 1: Add a Phase C2 subsection to iac/README.md**

Find the existing "Phase C1 — Self-protection denylist" section. Add a new subsection AFTER it:

```markdown
### Phase C2 — Trusted plan-builder workflow

The `plan-builder` job in `.github/workflows/iac.yml` produces the authoritative
`tofu plan` artifact for a DriftScribe IaC PR:

- **Trigger:** `workflow_dispatch` only, AND only when the dispatched ref is
  `refs/heads/main`. A maintainer clicks **Run workflow** from the `main` branch
  and enters the PR number. `pull_request` is excluded (the WIF condition
  refuses tokens for that event — fork-PR `repository` claim cannot be
  filtered); dispatch from a non-main branch is also rejected (so a modified
  workflow file on a feature branch cannot mint creds).
- **Identity:** WIF-impersonated `tofu-plan-builder@…` SA. No long-lived keys.
  Bucket-scoped IAM: state lock + KMS encrypt/decrypt + artifact write only.
- **PR eligibility:** same-repo (no forks), base `main`, **changes only `iac/`
  paths**. After checkout of the pinned head SHA, a pure-shell
  `git diff --name-only --no-renames -z $BASE_SHA $HEAD_SHA` against the
  immutable git objects refuses if any path is outside `iac/` — no API
  call, no force-push TOCTOU. The in-checkout static-gate re-run in
  HARDCODED `MODE=agent` is the second line of defense.
- **Steps:** validate PR → resolve head/base SHAs (refuse fork/non-main-base)
  → checkout pinned head → fetch base SHA → diff-guard (`git diff --no-renames`)
  → uv/python setup → static-gate re-run (agent mode) → setup-opentofu
  (pinned 1.12.0) → WIF auth → `tofu init` (backend) → `tofu plan -out`
  → `tofu show -json` → C1 denylist (fails before upload on violation) →
  upload plan.tfplan + plan.json (capture generations) → build final
  metadata.json with real generations → upload metadata.json (no
  placeholder ever lands) → post truncated `tofu show` diff to the PR
  (with all 3 generations + 2 content hashes + 3 URIs).
- **Metadata schema:** `c2.v1` — 15 keys (`schema_version` + 14 data fields),
  validated by `tools.iac_plan_metadata`. The C3 input contract; C4 fetches by
  pinned generation. Per-run path segment means re-plans never collide.
- **Operator preconditions** (one-time after merge):
  - Re-run `infra/scripts/setup_iac_backend.sh` to apply BOTH the new
    `storage.objectCreator` IAM binding on the artifact bucket AND the
    tightened WIF condition (ref-pinned workflow_dispatch).
  - Set GitHub secrets `GCP_WIF_PROVIDER`, `GCP_TOFU_PLAN_BUILDER_SA`,
    `GCP_TOFU_STATE_KMS_KEY` (values printed by the bootstrap script).
- **What it does NOT do:** mint approvals, sign HMAC, apply state, read other
  PRs' artifacts. Those live in C3 (schema) and C4 (apply worker).
```

**Step 2: Add CODEOWNERS entries**

Find the existing CODEOWNERS section that includes `/tools/iac_plan_denylist.py @adi-prasetyo`. Add:

```
/tools/iac_plan_metadata.py @adi-prasetyo
/tools/iac_plan_diff_summary.py @adi-prasetyo
/tools/iac_plan_artifact_upload.py @adi-prasetyo
```

(The workflow file `.github/workflows/iac.yml` is presumably already owned via a broader `.github/workflows/` rule — verify and only add a specific entry if there's no broader coverage.)

**Step 3: Commit**

```bash
git add iac/README.md .github/CODEOWNERS
git commit -m "docs(iac): document Phase C2 plan-builder + add CODEOWNERS entries"
```

---

### Task 9: Local end-to-end dry-run validation (no live GCP, no PR)

The workflow itself cannot be executed locally end-to-end without a GCP project + a real PR. But each Python helper can be exercised, and the YAML can be parse-verified.

**Files:**
- (No new files — this is a verification task)

**Step 1: Run the entire test suite**

```bash
cd /home/adi/driftscribe/.worktrees/phase-c2-plan-builder
uv run pytest -x -q
```

Expected: ALL tests pass (1216 from C1 + the new ones from C2).

**Step 2: Lint everything new with ruff**

```bash
uv run ruff check tools/iac_plan_metadata.py tools/iac_plan_diff_summary.py tests/unit/test_iac_plan_metadata*.py tests/unit/test_iac_plan_diff_summary*.py
uv run ruff format --check tools/iac_plan_metadata.py tools/iac_plan_diff_summary.py tests/unit/test_iac_plan_metadata*.py tests/unit/test_iac_plan_diff_summary*.py
```

Expected: clean.

**Step 3: Parse the workflow YAML**

```bash
python -c 'import yaml; print(list(yaml.safe_load(open(".github/workflows/iac.yml"))["jobs"].keys()))'
```

Expected: `['static-gate', 'tofu', 'plan-builder']`.

**Step 4: Confirm tofu init (no backend) still works on the new HCL untouched**

```bash
tofu -chdir=iac init -backend=false -lockfile=readonly
tofu -chdir=iac fmt -check
tofu -chdir=iac validate
```

Expected: clean (we didn't touch any HCL).

**Step 5: Smoke-test the CLI end-to-end with synthetic inputs**

```bash
# Synthesize a metadata record via the CLI
META_REPO=adi-prasetyo/driftscribe \
META_PR_NUMBER=42 \
META_HEAD_SHA=$(printf 'a%.0s' {1..40}) \
META_BASE_SHA=$(printf 'b%.0s' {1..40}) \
META_WORKFLOW_RUN_ID=99 \
META_ARTIFACT_URI_PLAN="gs://driftscribe-hack-2026-tofu-artifacts/pr-42/$(printf 'a%.0s' {1..40})/plan.tfplan" \
META_ARTIFACT_URI_JSON="gs://driftscribe-hack-2026-tofu-artifacts/pr-42/$(printf 'a%.0s' {1..40})/plan.json" \
META_GENERATION_PLAN=1700000000000000 \
META_GENERATION_JSON=1700000000000001 \
META_PLAN_SHA256=$(printf 'c%.0s' {1..64}) \
META_PLAN_JSON_SHA256=$(printf 'd%.0s' {1..64}) \
META_OPENTOFU_VERSION=1.12.0 \
META_PROVIDER_LOCKFILE_SHA256=$(printf 'e%.0s' {1..64}) \
  uv run python -m tools.iac_plan_metadata

# Synthesize a PR comment body
echo "Plan: 1 to add" | uv run python -m tools.iac_plan_diff_summary \
  --head-sha=$(printf 'a%.0s' {1..40}) \
  --plan-sha256=$(printf 'c%.0s' {1..64}) \
  --generation=1700000000000000 \
  --artifact-uri="gs://x/plan.tfplan" \
  --opentofu-version=1.12.0
```

Expected: both produce valid output without errors.

**Step 6: No commit** — this task is a verification gate. If any step fails, fix the relevant earlier task before proceeding.

---

### Task 10: Self-review against the §0 invariants

Walk through the 10 invariants in §0 and verify each. This is the equivalent of the C1 plan's Task 10 review.

For each invariant, write a one-line "evidence" pointing to file:line where it's enforced. Example deliverable (do NOT commit; this is a mental checklist):

```
Invariant 1 (workflow_ref path): .github/workflows/iac.yml is the FILE — no new workflow file.
Invariant 2 (pull_request no creds): plan-builder job has `if: github.event_name == 'workflow_dispatch'`.
…
```

If any invariant lacks clear evidence, REOPEN the relevant task.

---

### Task 11: Open the PR with `feat/iac-phase-c2-plan-builder` branch label

**Files:**
- (None — this is the ship step)

**Branch naming reminder:** Per the C1 lesson recorded in memory, `infra/` branch prefix triggers AGENT mode on the static gate and would reject everything outside `iac/`. THIS PR touches `tools/`, `tests/`, `.github/workflows/`, `infra/scripts/`, `docs/`, `iac/README.md` — so MUST use `feat/iac-…` branch, NOT `infra/…`.

The worktree branch was created as `feat/iac-phase-c2-plan-builder` (verified at the top of this plan).

**Step 1: Push the branch**

```bash
cd /home/adi/driftscribe/.worktrees/phase-c2-plan-builder
git push -u origin feat/iac-phase-c2-plan-builder
```

**Step 2: Open the PR**

```bash
gh pr create \
  --title "feat(iac): Phase C2 — trusted plan-builder workflow" \
  --body-file docs/plans/2026-05-28-infra-iac-phase-c2-plan-builder.md \
  --base main
```

(Or write a shorter body that points to the plan doc + the Codex review thread + the operator follow-ups.)

**Step 3: Wait for CI green**

The workflow's own `static-gate` + `tofu` jobs will run on the PR (operator mode, since branch is `feat/…`). Lint + tests will run via the existing `ci.yml`. GitGuardian will run.

The `plan-builder` job will NOT run on PR (it's gated to `workflow_dispatch`). That's expected.

**Step 4: Merge with admin override**

```bash
gh pr merge <PR#> --squash --delete-branch --admin
```

**Step 5: Clean up the worktree**

```bash
cd /home/adi/driftscribe
git pull
git worktree remove .worktrees/phase-c2-plan-builder
```

---

### Task 12: Update project memory

**Files:**
- Modify: `/home/adi/.claude/projects/-home-adi-driftscribe/memory/infra_iac_agent.md`
- Modify: `/home/adi/.claude/projects/-home-adi-driftscribe/memory/MEMORY.md`

Append a Phase C2 paragraph to `infra_iac_agent.md` describing:
- What landed (workflow + helpers + bootstrap update)
- The metadata.json schema version `c2.v1` (for C3 to bind to)
- The operator follow-ups still pending (re-run bootstrap, set 3 secrets, optionally trigger first plan via workflow_dispatch to smoke-test)
- The remaining Phase C slices (C3 schema, C4 apply worker, C5 coordinator wiring, C6 e2e)

Update `MEMORY.md` index line to reflect "Phase C2 plan-builder merged".

---

## §6  Operator follow-ups (recorded in PR description, NOT in code)

These actions are required AFTER merge for the workflow to actually function. They are not part of this slice's code changes; they are written here so the operator has a checklist.

1. **Re-run the bootstrap script** to apply BOTH the new `storage.objectCreator` IAM binding on the artifact bucket AND the tightened WIF attribute condition (ref-pinned workflow_dispatch):
   ```bash
   PROJECT=driftscribe-hack-2026 infra/scripts/setup_iac_backend.sh
   ```
   Output should include:
   - `tofu-plan-builder@…: storage.objectCreator on gs://driftscribe-hack-2026-tofu-artifacts (plan upload, write-only)`
   - `WIF provider github-oidc: attribute mapping + condition updated` (this is the existing `update-oidc` line — confirms the script ran in update path, applying the new condition).

   **This re-run is REQUIRED before the first plan-builder dispatch.** Without it, a maintainer dispatching from a feature branch could mint creds. The workflow-side `if:` guard catches it too, but defense-in-depth wants both layers.
2. **Set three GitHub secrets** (Settings → Secrets and variables → Actions):
   - `GCP_WIF_PROVIDER` — full provider resource path printed by the bootstrap.
   - `GCP_TOFU_PLAN_BUILDER_SA` — `tofu-plan-builder@driftscribe-hack-2026.iam.gserviceaccount.com`.
   - `GCP_TOFU_STATE_KMS_KEY` — full KMS key resource path printed by the bootstrap.
3. **Smoke-test the workflow.** Open a trivial IaC PR (e.g. a comment-only change to `iac/cloudrun.tf`), then go to Actions → iac → Run workflow → enter the PR number. Verify:
   - The plan-builder job runs.
   - A `plan.tfplan` + `plan.json` + `metadata.json` triplet lands in `gs://driftscribe-hack-2026-tofu-artifacts/pr-<N>/<head_sha>/run-<run_id>-<run_attempt>/`.
   - The PR has a fresh comment with the truncated `tofu show` body.
4. **(Optional, later)** Promote `iac / plan-builder` to a required status check once C4 is wired so the apply worker can pre-check that a plan exists.

---

## §7  Risks + mitigations baked into the plan

| Risk | Mitigation |
|---|---|
| Fork-PR token impersonation | Bootstrap-provisioned WIF condition rejects `pull_request`; plan-builder job gated `if: workflow_dispatch && ref==main`; PR resolution refuses `isCrossRepository=true`. |
| `workflow_dispatch` from a feature branch with a modified workflow file (creds exfiltration) | Two layers: (a) WIF attribute condition (Task 6) requires `assertion.ref == refs/heads/main` for dispatch; (b) plan-builder `if:` also requires `github.ref == 'refs/heads/main'`. Either layer alone closes it. |
| PR-controlled code (`tools/`, `tests/`, `uv.lock`, `pyproject.toml`) tampering after WIF auth | Pure-shell `git diff --name-only --no-renames -z $BASE_SHA $HEAD_SHA` (against immutable git objects, no API call) AFTER the pinned checkout but BEFORE `uv sync` refuses any non-`iac/` path. `--no-renames` keeps a `tools/x.py` → `iac/x.py` rename from slipping through. Then in-checkout static-gate re-run with HARDCODED `MODE=agent` (not derived from labels/branch) rejects non-`iac/` paths a second time. After both gates pass, the checked-out `tools/` == main's `tools/` (PR didn't touch them). |
| Plan-builder runs against a non-`main` base | PR resolution step refuses `baseRefName != 'main'`. |
| Maintainer dispatches and PR head moves under them | Plan-builder resolves head SHA at dispatch via `gh pr view`, checks it out pinned, and records in `metadata.head_sha`. A second click goes into a different `run-<run_id>-<run_attempt>` folder (Important 2). |
| `plan-builder` partial run leaves bucket inconsistent | `cancel-in-progress: false` at BOTH workflow level (conditional, Blocker 3 fix) AND job level. Per-step failures stop BEFORE the next upload. If `plan.tfplan` upload succeeds but `plan.json` fails, the metadata.json never gets written → C4 refuses to apply. |
| Wrong artifact gets applied | `metadata.json` carries `generation_plan` (immutable GCS pointer) + `plan_sha256` (content hash); the PR comment surfaces ALL THREE generations (plan, json, metadata). C4 pins to a specific metadata generation. |
| Concurrent re-plans collide at same path | `run-<run_id>-<run_attempt>` segment in path — every plan-builder execution gets its own folder. |
| HCL/lockfile drift between PR-gate and plan-builder | Static gate re-runs inside plan-builder against the pinned head SHA. `tofu init -lockfile=readonly` fails closed if the lockfile changed. |
| OpenTofu version skew | `setup-opentofu` pins `tofu_version: '1.12.0'` explicitly. `metadata.opentofu_version` captures it. C4 verifies. |
| Generation read-back fragility (parsing gcloud output) | Eliminated: `tools/iac_plan_artifact_upload.py` uses google-cloud-storage SDK; `Blob.generation` populated in-band from upload response. IAM stays at `roles/storage.objectCreator` (no read role needed). |
| Plan text exceeds GitHub comment limit | `tools.iac_plan_diff_summary` truncates to `GH_COMMENT_BUDGET=60_000` chars with a notice. |
| Markdown injection via backticks in plan text | `format_summary` picks a fence longer than any backtick run in the input (Important 6). |
| Maintainer dispatches without secrets set | The `gcp-auth` step fails fast with a clear error before any GCP call. No partial state. |
| Branch naming triggers AGENT mode like C1 | This PR is on `feat/iac-…` not `infra/…` (Task 11 documents the lesson). |
| Structural workflow regression in a future PR | `tests/unit/test_iac_workflow_structure.py` asserts: no `pull_request_target`, only plan-builder has `id-token: write`, diff-guard precedes checkout, denylist precedes upload, etc. CI catches the regression. |

---

## §8  Codex review trail

Before this plan is presented to the user, it is reviewed by Codex via `mcp__codex__codex`. After implementation completes, the same thread is followed up via `mcp__codex__codex-reply` for completed-work review. Per CLAUDE.md: no `model` parameter is passed (Codex uses its current recommended model).

The Codex thread ID for this plan is recorded in the PR description on open.
