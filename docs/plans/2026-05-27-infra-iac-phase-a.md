# Infra-IaC Phase A — OpenTofu Layer + Static HCL Gate — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task (fresh subagent per task + code review between tasks).

**Goal:** Stand up DriftScribe's OpenTofu IaC layer (`iac/`) with a GCS backend + `gcp_kms` state/plan encryption, an authoritative **static HCL gate** that closes the PR-controlled-HCL code-execution surface, and a CI workflow — with NO agents and NO apply automation. Foundation only.

**Architecture:** A new top-level `iac/` directory holds HCL for the GCP project `driftscribe-hack-2026` (region `asia-northeast1`), adopting the existing `payment-demo` Cloud Run service via an `import {}` block (brownfield). A pure-Python static gate (`tools/iac_static_gate.py`, unit-tested) enforces the §5.1 rules from the design doc; a CI workflow (`.github/workflows/iac.yml`) runs the gate + `tofu init -lockfile=readonly` + `fmt -check` + `validate` + `plan` on PRs touching `iac/`. The live bucket/KMS/WIF provisioning is scripted but **operator-run**.

**Tech Stack:** OpenTofu 1.12.x (`tofu` CLI), `python-hcl2` (HCL parsing in the gate), GitHub Actions + `opentofu/setup-opentofu`, Workload Identity Federation (no long-lived keys), GCS backend + Cloud KMS, `uv` + pytest + ruff.

**Design reference:** `docs/plans/2026-05-27-infra-iac-agent-design.md` (rev 2 final, two Codex rounds). This plan implements **Phase A only** (§8 of the design).

---

## Conventions & guardrails for the implementer

- **TDD:** for the gate (Tasks 1–6), write the failing test first, watch it fail, implement minimally, watch it pass, commit. Use @superpowers:test-driven-development.
- **`[AGENT]` vs `[OPERATOR]`:** `[AGENT]` tasks are implemented + verified autonomously in this session. `[OPERATOR]` tasks are **authored** here but **run by the operator** because they mutate the live GCP project / IAM (buckets, KMS, WIF) — never executed autonomously. Do NOT run any `gcloud`/`gsutil`/live `tofu init|import|apply` against `driftscribe-hack-2026`.
- **Commands run from the worktree root:** `/home/adi/driftscribe/.worktrees/infra-iac-phase-a`. Python via `uv run`.
- **Keep the existing suite green:** the full suite is fast (`uv run pytest -q` ≈ 5s, 1040 tests). Run it before the final commit.
- **Secret hygiene:** never echo secret values; any credential-shaped test fixture must be FAKE and, if it trips GitGuardian, added to `.gitguardian.yaml`.
- **No new coordinator/worker runtime deps:** `python-hcl2` goes in the `dev` extra only (CI tooling), not the `[project].dependencies` that ship in worker/coordinator images.

---

## Task 0: Scaffold the `tools` package + add `python-hcl2` dev dep [AGENT]

**Files:**
- Create: `tools/__init__.py` (empty)
- Create: `tools/iac_static_gate.py` (stub — just a module docstring for now)
- Modify: `pyproject.toml` (add `python-hcl2` to the `dev` extra)
- Create: `tests/unit/test_iac_static_gate.py` (empty placeholder importing the module)

**Step 1 — add the dev dependency.** In `pyproject.toml`, under `[project.optional-dependencies]` `dev = [ ... ]`, add `"python-hcl2>=4.3"` (import name is `hcl2`). Keep the list alphabetical-ish near `ruff`.

**Step 2 — sync + confirm import.** Run:
```bash
uv sync --all-extras
uv run python -c "import hcl2; print(hcl2.__name__)"
```
Expected: prints `hcl2`. **If `python-hcl2` fails to build/import on Python 3.14**, record the error and fall back: pin a version that supports 3.14, or (last resort) note in the gate module that parsing uses a constrained regex tokenizer instead — but try hcl2 first; the gate's robustness depends on real parsing.

**Step 3 — create `tools/__init__.py`** (empty) and a stub `tools/iac_static_gate.py`:
```python
"""Static HCL gate for DriftScribe infra PRs (design doc §5.1).

Pre-`tofu init` policy that closes the PR-controlled-HCL code-execution
surface: agent-authored infra PRs may only touch ``iac/``, may not add
providers, may not declare modules, and may not contain provisioners or
other arbitrary-execution constructs. Pure functions here; the CLI wrapper
(``python -m tools.iac_static_gate``) supplies the git diff in CI.
"""
from __future__ import annotations
```

**Step 4 — confirm collection.** Create `tests/unit/test_iac_static_gate.py`:
```python
from tools import iac_static_gate  # noqa: F401


def test_module_imports():
    assert iac_static_gate is not None
```
Run: `uv run pytest tests/unit/test_iac_static_gate.py -q` → expected PASS (confirms `tools.` is importable under pytest).

**Step 5 — commit.**
```bash
git add pyproject.toml uv.lock tools/__init__.py tools/iac_static_gate.py tests/unit/test_iac_static_gate.py
git commit -m "chore(iac): scaffold tools.iac_static_gate package + python-hcl2 dev dep"
```

---

## Task 1: Gate data model + policy modes [AGENT]

The gate is a **pure function** over an explicit input model so it's trivially testable without git or a filesystem.

**Files:** Modify `tools/iac_static_gate.py`; Test `tests/unit/test_iac_static_gate.py`.

**Design of the pure API (implement incrementally across Tasks 1–5):**
```python
import enum
from dataclasses import dataclass

IAC_PREFIX = "iac/"
# Foundation files only the operator/bootstrap mode may touch (Codex rev: must
# cover everything that sets authority — backend, encryption, provider
# project/region/creds, variables, and import targets — or an agent PR could
# redirect the project without touching versions.tf).
PROTECTED_FOUNDATION = (
    "iac/.terraform.lock.hcl",
    "iac/versions.tf",      # backend + encryption config
    "iac/providers.tf",     # provider project/region/credentials
    "iac/variables.tf",     # variable definitions/defaults
    "iac/imports.tf",       # import targets
)
# In AGENT mode, ONLY plain `.tf` files may be changed under iac/. Everything
# else OpenTofu also loads is a bypass surface and is rejected outright (Codex
# rev): `.tofu`/`.tofu.json` (and `.tofu` OVERRIDES a same-named `.tf`),
# `.tf.json`, and any `*.tfvars`/`*.auto.tfvars` (auto-loaded variable values).
ALLOWED_AGENT_SUFFIX = ".tf"
REJECTED_IAC_SUFFIXES = (".tofu", ".tofu.json", ".tf.json", ".tfvars")  # + *.auto.tfvars
ALLOWED_PROVIDERS = frozenset({"google"})  # + builtin (terraform/tofu) names
# When a provider is declared with a source, it must be the canonical one
# (Codex rev: name-only check lets `google = { source = "evil/google" }` pass).
REQUIRED_PROVIDER_SOURCES = {"google": "hashicorp/google"}

class GateMode(enum.Enum):
    AGENT = "agent"        # driftscribe-infra label + infra/ branch — strict rules
    OPERATOR = "operator"  # human-authored bootstrap — foundation edits allowed

@dataclass(frozen=True)
class Violation:
    rule: str       # short machine id, e.g. "path-outside-iac"
    detail: str     # human message (file/line/context)

@dataclass(frozen=True)
class GateInput:
    mode: GateMode
    changed_paths: tuple[str, ...]           # repo-relative, from `git diff --name-only`
    hcl_files: dict[str, str]                # path -> file content, only iac/*.tf{,.json}

def evaluate(gi: GateInput) -> list[Violation]:
    """Return all violations (empty = pass). Fail-closed: a parse error is a Violation, not an exception."""
```

**Step 1 — failing test for the model + empty-input pass:**
```python
from tools.iac_static_gate import GateInput, GateMode, evaluate

def test_clean_agent_pr_passes():
    gi = GateInput(
        mode=GateMode.AGENT,
        changed_paths=("iac/cloudrun.tf",),
        hcl_files={"iac/cloudrun.tf": 'resource "google_cloud_run_v2_service" "x" {}\n'},
    )
    assert evaluate(gi) == []
```
Run it → FAIL (evaluate undefined).

**Step 2 — implement** the dataclasses, enums, constants, and an `evaluate()` that returns `[]` for now. Run → PASS.

**Step 3 — commit:** `feat(iac-gate): gate data model + policy modes`.

---

## Task 2: Path / file-type checks — `iac/`-only, allowed suffixes, foundation protected (AGENT mode) [AGENT]

**Rule ids:** `path-outside-iac`, `disallowed-file-type`, `foundation-edit-agent-mode`.

**Step 1 — failing tests:**
```python
import pytest
from tools.iac_static_gate import GateInput, GateMode, evaluate

def test_agent_pr_touching_outside_iac_is_rejected():
    gi = GateInput(GateMode.AGENT, ("iac/cloudrun.tf", ".github/workflows/ci.yml"), {})
    assert any(v.rule == "path-outside-iac" for v in evaluate(gi))

@pytest.mark.parametrize("path", [
    "iac/main.tofu", "iac/main.tofu.json", "iac/main.tf.json",
    "iac/prod.tfvars", "iac/x.auto.tfvars",
])
def test_agent_pr_non_tf_iac_file_is_rejected(path):
    # .tofu OVERRIDES a same-named .tf; .tf.json/.tfvars/.auto.tfvars are all
    # loaded by OpenTofu and would bypass a .tf-only gate.
    gi = GateInput(GateMode.AGENT, (path,), {})
    assert any(v.rule == "disallowed-file-type" for v in evaluate(gi)), path

def test_agent_pr_touching_lockfile_is_rejected():
    gi = GateInput(GateMode.AGENT, ("iac/.terraform.lock.hcl",), {})
    assert any(v.rule == "foundation-edit-agent-mode" for v in evaluate(gi))

@pytest.mark.parametrize("path", [
    "iac/versions.tf", "iac/providers.tf", "iac/variables.tf", "iac/imports.tf",
])
def test_agent_pr_touching_foundation_is_rejected(path):
    gi = GateInput(GateMode.AGENT, (path,), {})
    assert any(v.rule == "foundation-edit-agent-mode" for v in evaluate(gi)), path

def test_operator_mode_may_touch_foundation():
    gi = GateInput(GateMode.OPERATOR, ("iac/.terraform.lock.hcl", "iac/versions.tf"), {})
    assert evaluate(gi) == []

def test_operator_mode_still_governs_only_iac():
    # The gate only governs iac/; a .github edit in operator mode raises no
    # path-outside-iac (CODEOWNERS governs that file).
    gi = GateInput(GateMode.OPERATOR, ("iac/versions.tf", ".github/workflows/iac.yml"), {})
    assert all(v.rule != "path-outside-iac" for v in evaluate(gi))
```
Run → FAIL.

**Step 2 — implement** the path checks in `evaluate()`:
- For every `p` in `changed_paths`: in AGENT mode, if not `p.startswith(IAC_PREFIX)` → `Violation("path-outside-iac", p)` and skip the per-file checks below for it. (OPERATOR mode skips this — the gate only governs `iac/`.)
- For each AGENT-mode `iac/` path: reject by suffix — if `p` ends with any of `REJECTED_IAC_SUFFIXES` or matches `*.auto.tfvars`, or does NOT end with `ALLOWED_AGENT_SUFFIX` (and isn't an allowed non-`.tf` like a `.md` README — decide: simplest is to allow `.tf` and `.md` only, reject the rest) → `Violation("disallowed-file-type", p)`. Note `.tofu.json`/`.tf.json` must be checked before the generic `.tf`/`.json` suffix logic so the longer suffix wins.
- In AGENT mode, if `p` in `PROTECTED_FOUNDATION` → `Violation("foundation-edit-agent-mode", p)`.

Run → PASS.

**Step 3 — commit:** `feat(iac-gate): path + file-type checks (iac/ .tf-only, foundation protected)`.

---

## Task 3: Provider allowlist + source pinning [AGENT]

Parse each HCL file; collect declared providers from (a) `terraform.required_providers` and (b) top-level `provider "<name>"` blocks. Anything outside `ALLOWED_PROVIDERS` (and the builtin `terraform`/`tofu` pseudo-providers) → violation. **Also check the `source`** — an allowed name with a spoofed source (`google = { source = "evil/google" }`) must be rejected (Codex rev). **Fail-closed:** parse error → `Violation("hcl-parse-error", path)`. Provider checks run in BOTH modes (foundation files are where providers legitimately live; a clean operator PR with `google`/`hashicorp/google` passes).

**Rule ids:** `disallowed-provider`, `disallowed-provider-source`, `hcl-parse-error`.

**Step 1 — failing tests** (OPERATOR mode so Task 2's foundation-edit rule doesn't mask the provider assertions):
```python
def test_disallowed_provider_required_providers_block():
    hcl = 'terraform { required_providers { aws = { source = "hashicorp/aws" } } }'
    gi = GateInput(GateMode.OPERATOR, ("iac/versions.tf",), {"iac/versions.tf": hcl})
    assert any(v.rule == "disallowed-provider" for v in evaluate(gi))

def test_spoofed_google_source_is_rejected():
    hcl = 'terraform { required_providers { google = { source = "evil/google" } } }'
    gi = GateInput(GateMode.OPERATOR, ("iac/versions.tf",), {"iac/versions.tf": hcl})
    assert any(v.rule == "disallowed-provider-source" for v in evaluate(gi))

def test_canonical_google_provider_is_allowed():
    hcl = '''
    terraform { required_providers { google = { source = "hashicorp/google" } } }
    provider "google" { project = "p" }
    '''
    gi = GateInput(GateMode.OPERATOR, ("iac/versions.tf",), {"iac/versions.tf": hcl})
    assert evaluate(gi) == []

def test_unparseable_hcl_fails_closed():
    gi = GateInput(GateMode.OPERATOR, ("iac/versions.tf",), {"iac/versions.tf": 'resource "x" { = = = }'})
    assert any(v.rule == "hcl-parse-error" for v in evaluate(gi))
```
Run → FAIL.

**Step 2 — implement** an internal `_parse(path, content) -> dict | None` using `hcl2.loads(content)`, catching parse errors → record `hcl-parse-error` and skip structural checks for that file. Walk `required_providers` entries + top-level `provider` block names; anything not in `ALLOWED_PROVIDERS` → `disallowed-provider`. For an allowed provider declaring a `source`, if it != `REQUIRED_PROVIDER_SOURCES[name]` → `disallowed-provider-source`. (Builtin `terraform_data` is a resource, not a provider — don't confuse them.)

Run → PASS.

**Step 3 — commit:** `feat(iac-gate): provider allowlist + source pinning, fail-closed parse`.

---

## Task 4: Module ban — no `module` blocks at all in v1 [AGENT]

**Rule id:** `module-block-forbidden`. Rationale (design §5.1): banning only *remote* modules would force recursive parsing of local modules; v1 bans all modules.

**Step 1 — failing tests:**
```python
def test_any_module_block_is_rejected():
    hcl = 'module "vpc" { source = "./vpc" }'
    gi = GateInput(GateMode.AGENT, ("iac/x.tf",), {"iac/x.tf": hcl})
    assert any(v.rule == "module-block-forbidden" for v in evaluate(gi))

def test_no_module_block_passes():
    gi = GateInput(GateMode.AGENT, ("iac/x.tf",), {"iac/x.tf": 'resource "google_x" "y" {}'})
    assert all(v.rule != "module-block-forbidden" for v in evaluate(gi))
```
Run → FAIL.

**Step 2 — implement:** if parsed dict contains a top-level `module` key (hcl2 returns a list of module blocks) → one `module-block-forbidden` per module name.

**Step 3 — commit:** `feat(iac-gate): forbid all module blocks (v1)`.

---

## Task 5: Provisioner / arbitrary-execution ban [AGENT]

Forbid: `provisioner` blocks (incl. `local-exec`/`remote-exec`), `connection` blocks, **`dynamic` blocks** (v1 — a `dynamic "provisioner"` would smuggle execution past a naive check; payment-demo needs none), `data "external"`, `data "terraform_remote_state"` (cross-state read), and the `null_resource` / `terraform_data` resource types (their purpose with provisioners is to run commands). **Rule ids:** `arbitrary-execution`, `forbidden-data-source`, `dynamic-block-forbidden`.

**Step 1 — failing tests:**
```python
import pytest

@pytest.mark.parametrize("hcl", [
    'resource "google_x" "y" { provisioner "local-exec" { command = "echo hi" } }',
    'resource "google_x" "y" { provisioner "remote-exec" { inline = ["id"] } }',
    'resource "google_x" "y" { connection { host = "h" } }',
    'resource "null_resource" "r" {}',
    'resource "terraform_data" "r" {}',
])
def test_arbitrary_execution_constructs_rejected(hcl):
    gi = GateInput(GateMode.AGENT, ("iac/x.tf",), {"iac/x.tf": hcl})
    assert any(v.rule == "arbitrary-execution" for v in evaluate(gi)), hcl

@pytest.mark.parametrize("hcl", [
    'data "external" "e" { program = ["bash", "x.sh"] }',
    'data "terraform_remote_state" "s" { backend = "gcs" }',
])
def test_forbidden_data_sources_rejected(hcl):
    gi = GateInput(GateMode.AGENT, ("iac/x.tf",), {"iac/x.tf": hcl})
    assert any(v.rule == "forbidden-data-source" for v in evaluate(gi)), hcl

def test_dynamic_block_rejected():
    hcl = 'resource "google_x" "y" { dynamic "provisioner" { for_each = [1] content {} } }'
    gi = GateInput(GateMode.AGENT, ("iac/x.tf",), {"iac/x.tf": hcl})
    assert any(v.rule == "dynamic-block-forbidden" for v in evaluate(gi))

def test_plain_google_resource_has_no_execution_violation():
    hcl = 'resource "google_cloud_run_v2_service" "s" { name = "payment-demo" }'
    gi = GateInput(GateMode.AGENT, ("iac/x.tf",), {"iac/x.tf": hcl})
    assert evaluate(gi) == []
```
Run → FAIL.

**Step 2 — implement:** walk parsed `resource` blocks; if a resource body contains a `provisioner`, `connection`, or `dynamic` key → respective violation. If resource type is `null_resource`/`terraform_data` → `arbitrary-execution`. Walk `data` blocks; type `external`/`terraform_remote_state` → `forbidden-data-source`. (hcl2 represents `resource`/`data` as a list of `{type: {name: {body...}}}`; nested keys may be dict or list — handle both. Recurse one level into block bodies so a `dynamic`/`provisioner` nested in any block is caught.)

**Step 3 — commit:** `feat(iac-gate): forbid provisioners/connection/dynamic/external/remote-state/null_resource`.

---

## Task 6: CLI entrypoint — git diff → evaluate → exit code [AGENT]

`python -m tools.iac_static_gate --base <sha> --head <sha> --mode <agent|operator>` (mode may also be derived in CI from the PR label/branch; the CLI takes it explicitly for testability). It computes changed paths via `git diff --name-only <base>...<head>`, reads the post-change content of changed `iac/*.tf`/`*.tf.json` files at `<head>` (via `git show <head>:<path>`), builds a `GateInput`, runs `evaluate`, prints violations, and exits non-zero if any.

**Files:** Modify `tools/iac_static_gate.py` (add `main(argv)` + `if __name__ == "__main__"`); Test `tests/unit/test_iac_static_gate_cli.py`.

**Step 1 — failing test** using a real temp git repo (most robust; `tmp_path` + `subprocess`):
```python
import subprocess, sys
from pathlib import Path

def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

def test_cli_rejects_provisioner_pr(tmp_path: Path):
    repo = tmp_path
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t"); _git(repo, "config", "user.name", "t")
    (repo / "iac").mkdir()
    (repo / "iac" / "main.tf").write_text('resource "google_x" "y" {}\n')
    _git(repo, "add", "."); _git(repo, "commit", "-qm", "base")
    base = subprocess.run(["git","rev-parse","HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    (repo / "iac" / "main.tf").write_text(
        'resource "google_x" "y" { provisioner "local-exec" { command = "id" } }\n')
    _git(repo, "add", "."); _git(repo, "commit", "-qm", "bad")
    head = subprocess.run(["git","rev-parse","HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()

    proc = subprocess.run(
        [sys.executable, "-m", "tools.iac_static_gate", "--base", base, "--head", head, "--mode", "agent"],
        cwd=repo, capture_output=True, text=True,
        env={**__import__("os").environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
    )
    assert proc.returncode != 0
    assert "arbitrary-execution" in (proc.stdout + proc.stderr)
```
(Also add a positive test: a clean `iac/`-only change exits 0.)
Run → FAIL.

**Step 2 — implement** `main(argv)` with `argparse`, the git plumbing (`git diff --name-only base...head`, `git show head:path` for content of changed `iac/` HCL files; tolerate deleted files), `evaluate`, print, `sys.exit(1 if violations else 0)`.

**Step 3 — run full gate test file + the CLI test** → PASS. Run `uv run ruff check tools/ tests/unit/test_iac_static_gate*.py` → clean.

**Step 4 — commit:** `feat(iac-gate): CLI entrypoint (git diff → evaluate → exit code)`.

---

## Task 7: `iac/` HCL scaffold — versions/providers/variables [AGENT]

Install `tofu` locally for `fmt`/`validate` (no root needed — standalone binary):
```bash
mkdir -p "$HOME/.local/bin"
TOFU_VER=1.12.0
curl -fsSL -o /tmp/tofu.zip "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VER}/tofu_${TOFU_VER}_linux_amd64.zip"
unzip -o /tmp/tofu.zip tofu -d "$HOME/.local/bin" && "$HOME/.local/bin/tofu" version
```
(If download is blocked, skip local validate and rely on CI; note it.)

**Files:** Create `iac/versions.tf`, `iac/providers.tf`, `iac/variables.tf`.

`iac/versions.tf` (backend + encryption are foundation — protected from agent edits):
```hcl
terraform {
  required_version = ">= 1.12"

  required_providers {
    google = {
      source  = "hashicorp/google"   # resolves via registry.opentofu.org
      version = "~> 6.0"
    }
  }

  backend "gcs" {
    bucket = "driftscribe-hack-2026-tofu-state"  # MUST pre-exist (bootstrap)
    prefix = "prod"
  }

  encryption {
    key_provider "gcp_kms" "main" {
      kms_encryption_key = var.tofu_state_kms_key  # full key resource path
      key_length         = 32                      # AES-256
    }
    method "aes_gcm" "primary" {
      keys = key_provider.gcp_kms.main
    }
    state {
      method   = method.aes_gcm.primary
      enforced = true
    }
    plan {
      method   = method.aes_gcm.primary
      enforced = true
    }
  }
}
```
> NOTE: HCL block attributes are separate lines — **no commas** (`method = ...` then `enforced = true`). A comma here is a syntax error.

`iac/providers.tf`:
```hcl
provider "google" {
  project = var.project_id
  region  = var.region
}
```
`iac/variables.tf` (each attribute on its own line — no commas):
```hcl
variable "project_id" {
  type    = string
  default = "driftscribe-hack-2026"
}
variable "region" {
  type    = string
  default = "asia-northeast1"
}
variable "tofu_state_kms_key" {
  type        = string
  description = "Full Cloud KMS key resource path for OpenTofu state/plan encryption (early-eval: var only)."
}
```

**Verify (if tofu installable locally):** `-backend=false` is an `init` flag, NOT a `validate` flag (Codex rev). Correct flow — and this is also where the **committed lockfile** comes from (Task 10's `init -lockfile=readonly` needs it to already exist):
```bash
cd iac
"$HOME/.local/bin/tofu" init -backend=false   # downloads google provider, writes .terraform.lock.hcl
"$HOME/.local/bin/tofu" fmt -check
"$HOME/.local/bin/tofu" validate              # now succeeds (provider schema present, no backend/state needed)
cd ..
```
Expected: `init -backend=false` succeeds and creates `iac/.terraform.lock.hcl`; `fmt` clean; `validate` succeeds. (If the tofu binary can't be downloaded in this env, skip local verify and note it — CI validates; but then the lockfile must be generated some other way before Task 10. Prefer getting tofu working locally so the lockfile is committed here.)

**Commit (include the lockfile):**
```bash
git add iac/versions.tf iac/providers.tf iac/variables.tf iac/.terraform.lock.hcl
git commit -m "feat(iac): OpenTofu layer scaffold (gcs backend + gcp_kms encryption + google provider + lockfile)"
```

> NOTE: `var.tofu_state_kms_key` has no default by design — the operator supplies it at `init`/`plan` time (`-var` or a tfvars the operator keeps out of git). Document in README.

---

## Task 8: `iac/` payment-demo resource + import block [AGENT]

**Files:** Create `iac/cloudrun.tf`, `iac/imports.tf`.

`iac/cloudrun.tf` — a minimal `google_cloud_run_v2_service` matching the live `payment-demo` (project `driftscribe-hack-2026`, region `asia-northeast1`, env `PAYMENT_MODE=mock`, `FEATURE_NEW_CHECKOUT=false`). Author the resource from the live shape documented in the design doc + memory; mark fields likely to need reconciliation with a comment. The goal is a config that, after `tofu import`, yields a **near-empty plan** (the operator iterates to zero-diff).

`iac/imports.tf` (declarative, reviewable; removable after first apply):
```hcl
import {
  to = google_cloud_run_v2_service.payment_demo
  id = "projects/driftscribe-hack-2026/locations/asia-northeast1/services/payment-demo"
}
```

**Verify:** `tofu -chdir=iac fmt -check` + `tofu -chdir=iac validate` (lockfile already present from Task 7, so no re-init needed unless providers changed — they didn't). The import block validates structurally without backend/live access.

**Commit:** `feat(iac): payment-demo cloud run resource + import block (brownfield adoption)`.

> Reaching an exactly-empty plan requires the live state — that is an **[OPERATOR]** iteration step (Task 11), not done autonomously.

---

## Task 9: Bootstrap script — state/artifact buckets, KMS, WIF [OPERATOR-RUN, agent-authored]

**Files:** Create `infra/scripts/setup_iac_backend.sh` (+ reuse `infra/scripts/_setup_lib.sh` helpers if present).

Author an **idempotent** bash script (follow the existing `infra/scripts/setup_secrets.sh` / `_setup_lib.sh` style and the invariants in memory `project_structure.md`) that:
1. Creates the **state bucket** `gs://driftscribe-hack-2026-tofu-state` with **Object Versioning ON** and uniform bucket-level access.
2. Creates a separate **artifact bucket** `gs://driftscribe-hack-2026-tofu-artifacts` (versioned) — reserved for Phase C plan artifacts; created now so the denylist target exists.
3. Creates a **Cloud KMS** keyring + key for state/plan encryption; prints the full key resource path (for `var.tofu_state_kms_key`).
4. Creates a **Workload Identity Federation** pool + GitHub OIDC provider with **attribute conditions** pinning `repository` + `workflow` + `ref` + `event_name`; binds a CI service account that has **only**: `roles/storage.objectAdmin` on the state bucket (locking needs object write — design §3.2), KMS encrypt/decrypt on the key, and the per-API **read** roles needed for `tofu plan` of Cloud Run. **Fork PRs get no credentials** (the attribute condition restricts to the canonical repo). NOTE (Codex rev): WIF is **not used by Phase A CI** (which runs no authenticated plan) — it's scripted now so the bootstrap is complete, but **wiring CI creds is a Phase C activation step, NOT a Phase A done-condition**. The buckets + KMS, by contrast, ARE needed for the Phase A operator import (`tofu init` with gcs backend + encryption).
5. Prints a summary of values the operator must wire in: `tofu_state_kms_key` for `iac/` now (for the import); WIF provider resource name + CI SA email later (Phase C).

**Do NOT run this script.** Add `set -euo pipefail`, comments explaining each grant's least-privilege rationale, and a top banner: `# OPERATOR-RUN: creates live GCP infra/IAM in driftscribe-hack-2026. Review before running.`

**Verify (agent):** `shellcheck infra/scripts/setup_iac_backend.sh` (install via the script's own pattern or `uv`-independent). Fix all warnings. Do a `bash -n` syntax check.

**Commit:** `feat(iac): operator bootstrap script for state/artifact buckets + KMS + WIF`.

---

## Task 10: CI workflow — static gate + tofu fmt/validate/plan on `iac/` PRs [AGENT-authored, CI-run]

**Files:** Create `.github/workflows/iac.yml`.

Workflow (triggered on `pull_request` paths `iac/**` and on the gate's own files):
- **NEVER `pull_request_target`** with PR code (design §11.8). Use `pull_request`.
- **No `tofu plan` in Phase A CI** (Codex rev): a meaningful plan needs live GCP creds + API access, which Phase A CI deliberately does not have; a `continue-on-error` plan would be noise, not a security check. The authoritative WIF-authenticated plan + artifact upload is **Phase C**. Phase A CI is: static gate → `init -backend=false -lockfile=readonly` → `fmt -check` → `validate`. (The real plan against live state is an operator runbook step.)
- Job `static-gate`: checkout with `fetch-depth: 0`; derive `--mode` (`agent` if the PR has label `driftscribe-infra` or head branch starts with `infra/`, else `operator`); run `uv sync --all-extras` then `uv run python -m tools.iac_static_gate --base "${{ github.event.pull_request.base.sha }}" --head "${{ github.event.pull_request.head.sha }}" --mode "$MODE"`.
- Job `tofu` (needs `static-gate`): `opentofu/setup-opentofu@<pinned-sha>`; `tofu -chdir=iac init -backend=false -lockfile=readonly` (fails if the PR would change the committed lockfile — the provider-add guard); `tofu -chdir=iac fmt -check`; `tofu -chdir=iac validate`.
- Name the gate job so it can later be a **required check** (mirrors the `lint-test` required-check pattern the upgrade-merge gate depends on).

**Verify (agent):** validate YAML (`uv run python -c "import yaml; yaml.safe_load(open('.github/workflows/iac.yml'))"`); confirm `actions/checkout` + `opentofu/setup-opentofu` are pinned to commit SHAs; confirm no `pull_request_target` and no `plan` step.

**Commit:** `ci(iac): static gate + tofu fmt/validate/plan workflow on iac/ PRs`.

---

## Task 11: Docs, runbook, and final verification [AGENT + OPERATOR handoff]

**Files:** Create `iac/README.md`; create/update `docs/runbooks/iac-bootstrap.md`.

`iac/README.md`: what `iac/` is, the GitOps flow (plan→PR→gated apply lands in Phase C), how to run a local plan (`tofu init` with `-var tofu_state_kms_key=...`, never commit a real tfvars with the key path if considered sensitive), what is foundation (operator-only) vs agent-editable, and the static-gate rules.

`docs/runbooks/iac-bootstrap.md` — the **[OPERATOR] runbook** (the live steps I will not run):
1. `bash infra/scripts/setup_iac_backend.sh` (review first).
2. Wire the WIF provider + CI SA into repo secrets/vars; set `tofu_state_kms_key`.
3. `cd iac && tofu init` (real backend, encryption active from t=0).
4. `tofu plan` — review the import; iterate `cloudrun.tf` until the plan is **empty** (state == live).
5. `tofu apply` the import (adopts payment-demo into state; no resource changes).
6. Confirm CI green on a no-op `iac/` PR.

**Step — final verification (agent):**
```bash
uv run ruff check .
uv run pytest -q          # expect: all pass (1040 existing + new gate tests)
```
Both must be clean. Then **stop** — the remaining exit criteria (clean empty plan in CI, live import) are operator steps in the runbook.

**Commit:** `docs(iac): README + operator bootstrap runbook`.

---

## Phase A "done" definition

**Autonomous (this session) — must all be true:**
- Static gate implemented + unit-tested (path / provider / module / provisioner rules + fail-closed parse + CLI), all green.
- `iac/` HCL scaffold + payment-demo resource + import block present; `iac/.terraform.lock.hcl` committed; `tofu init -backend=false` + `tofu fmt -check` + `tofu validate` clean (if tofu installable locally; else CI-verified).
- Bootstrap script authored + shellcheck-clean (NOT run).
- CI workflow authored (no `pull_request_target`, actions SHA-pinned).
- Full existing suite still green; ruff clean.
- Codex review of the completed Phase A code (mcp__codex__codex-reply on the design thread).

**Operator (handed off via runbook) — NOT done autonomously:**
- Run bootstrap (buckets/KMS/WIF); wire CI creds; `tofu init/import/plan/apply`; confirm empty plan + CI green.

> Phasing note: the trusted plan-artifact protocol, denylist, `tofu-apply`/`tofu-editor` workers, plan-bound HMAC, reader, and agent fan-out are **Phases B–D** — explicitly NOT in this plan.
