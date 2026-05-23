# DriftScribe Phase 20 — End-to-End Test Suite Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` to execute this plan task-by-task. Each task gets a fresh implementer + spec reviewer + code-quality reviewer cycle.

**Goal:** Add an assertive end-to-end test suite that exercises both workloads (drift + upgrade), the HITL approval path, and the transparency UI against a real deployed coordinator. Catch IAM/MCP/worker-boundary/Cloud-Logging-tail regressions before each demo recording and before the 2026-07-10 submission.

**Architecture:**
- **Separate `driftscribe-e2e` GCP project**, provisioned via `infra/scripts/setup_e2e_project.sh`. Mirrors the prod IAM topology + secret list but never touches `driftscribe-hack-2026`. Cost covered by $300 GCP coupon.
- **Separate `driftscribe-e2e-target` GitHub repo** for E2E-generated PRs/issues. Cloudbuild substitutions point all four `*_REPO` env vars at this repo for the E2E deploy; production deploy is unchanged.
- `tests/e2e/` directory, pytest with marker `e2e`, skipped by default. Activated by setting `DRIFTSCRIBE_E2E_URL` + `DRIFTSCRIBE_E2E_TOKEN` env vars. Sequential by design (shared GCP+GitHub state).
- Playwright UI tests in `tests/e2e/ui/` with their own `package.json`. Selectors keyed off stable `data-testid` attributes added in Task 20.6.0.
- GitHub Actions workflow `.github/workflows/e2e.yml` — manual dispatch only, gated behind a GitHub Environment with required reviewer. Auth via Workload Identity Federation (no long-lived service-account keys).

**Tech Stack:** pytest, pytest-asyncio, httpx (already in deps), PyGithub, `driftscribe_lib.cloud_run` (existing — `read_live_state` / `read_live_env`), google-cloud-firestore, google-cloud-run, Playwright (TypeScript), GitHub Actions, Workload Identity Federation, GitHub Environments.

**Existing infrastructure to extend, not replace:**
- `infra/scripts/e2e_smoke.sh` (482 lines, auth + IAM + prompt-injection probe — keep as-is; it's complementary, not redundant)
- `scripts/demo.sh` (~500 lines, beat-driver — Task 20.2 adds light smoke assertions; otherwise untouched)
- `driftscribe_lib.cloud_run.read_live_state` (returns `{env, revision}` of the *traffic-serving* revision — the correct read shape for E2E assertions, NOT `read_live_env` which reads the next-deploy template)

**Coordinator contract reality (verified against the codebase):**
- Auth header is **`X-DriftScribe-Token`** (NOT `X-Operator-Token`). `agent/auth.py:30`.
- Eight prod secrets (verified from `infra/cloudbuild.yaml`):
  `coordinator-shared-token`, `github-pat`, `developer-knowledge-api-key`, `docs-agent-github-pat`, `approval-hmac-key`, `driftscribe-webhook-url`, `upgrade-reader-github-pat`, `upgrade-docs-github-pat`.
- Service account names (verified from `infra/cloudbuild.yaml`):
  `driftscribe-agent`, `reader-agent-sa`, `docs-agent-sa`, `rollback-agent-sa`, `notifier-agent-sa`, `upgrade-reader-sa`, `upgrade-docs-sa`. (E2E adds one: `e2e-runner-sa` for GitHub Actions.)
- Gemini auth is **Vertex AI ADC** (no `GEMINI_API_KEY` secret — quota is per-project/region). `agent/main.py:1887-1890`.
- **`POST /chat`** body is `ChatRequest(prompt: str, session_id: str | None, workload: Literal["drift","upgrade"])`, `extra="forbid"`. Returns free-form `{reply, tool_calls, session_id}` — NO `action` field. `agent/main.py:1838-1859`.
- **`POST /recheck workload=drift`** returns a structured `DecisionProposal` with `action` — drift action assertions live here. `agent/main.py:1186-1196`.
- **`POST /recheck workload=upgrade`** returns **503 (not implemented)**. Upgrade action assertions must come from observable side effects (GitHub PR presence/absence) via `/chat`. `agent/main.py:923-933`.
- **`/trace/{id}`** returns `{trace_id, events, decision, complete, fetched_from_cache}` — NOT `stable`, NOT `groups`. Grouping is UI-side. `agent/main.py:1491-1497`.
- Approval URL shape: `{COORDINATOR_URL}/approvals/{id}?t=<token>`. Approve/Reject is a **form-POST** with `t` + `decision` fields. **Replays return 403, not 410**. `agent/main.py:1662, 1746-1794`.
- Drift contract declares **`PAYMENT_MODE`** (expected `"mock"`) + **`FEATURE_NEW_CHECKOUT`** (expected `"false"`). To force `drift_issue`, mutate `PAYMENT_MODE=live`. `demo/ops-contract.yaml`.
- Upgrade PR title format: `upgrade(<package>): <target_version>`. Branch: `upgrade/<package>-<dashed-version>`. NO `E2E:` prefix. `agent/adk_tools.py:328-334`.
- HITL rollback requires the operator (or test) to **name a concrete `target_revision`** in the /chat prompt — agent has no revision-enumeration tool. `scripts/demo.sh:328-364` documents this; `propose_rollback_tool(target_revision, reason)` accepts it as a string arg.
- Transparency UI sessionStorage key: **`driftscribe_token`** (underscore). `agent/templates/transparency.html:609`.

---

## Pre-flight (one-time, before Task 20.0)

These are **operator actions** done outside the plan. The plan tasks assume they are complete.

1. **Create the `driftscribe-e2e` GCP project** under the same billing account that owns `driftscribe-hack-2026`. Apply the $300 coupon to this billing account if not already.
2. **Create the `adi-prasetyo/driftscribe-e2e-target` GitHub repo** (private). Will be seeded by Task 20.0's runbook with the lodash 4.17.20 lockfile.
3. **Generate fine-grained PATs**:
   - `e2e-coordinator-pat` — scoped to `driftscribe-e2e-target`, `contents:write`, `pull_requests:write`, `issues:write`. (Mirrors prod `github-pat`.)
   - `e2e-docs-pat` — same scope as above. (Mirrors prod `docs-agent-github-pat`.)
   - `e2e-upgrade-reader-pat` — `contents:read` on `driftscribe-e2e-target`.
   - `e2e-upgrade-docs-pat` — `contents:write`, `pull_requests:write` on `driftscribe-e2e-target`.
   Operator holds these for Task 20.0 to paste into Secret Manager.
4. **Provision a Developer Knowledge MCP API key** for the e2e project (same shape as prod's `developer-knowledge-api-key`).
5. **Provision a webhook receiver** for the notifier (any URL that returns 2xx — `https://httpbin.org/post` or a Discord webhook is fine for E2E; no message assertions are made on it).
6. **Verify Vertex AI Gemini quota** for `gemini-2.5-flash` in `asia-northeast1` on the `driftscribe-e2e` project.

WIF setup lives in Task 20.7a (runbook the operator runs once before turning on CI).

---

## Task 20.0: Provision the `driftscribe-e2e` project — secrets, IAM, Firestore, parameterized cloudbuild

**Files:**
- Create: `infra/scripts/setup_e2e_project.sh`
- Create: `infra/scripts/_setup_lib.sh`
- Modify: `infra/scripts/setup_secrets.sh:1-30` (extract shared functions into `_setup_lib.sh`, re-source — no behavior change)
- Modify: `infra/cloudbuild.yaml` (add `_TARGET_SERVICE` + `_TARGET_GITHUB_REPO` + `_UPGRADE_TARGET_REPO` substitutions; replace literals; add `UPGRADE_TARGET_REPO_OVERRIDE=$_UPGRADE_TARGET_REPO` to coordinator env)
- Modify: `agent/workloads/registry.py:507-525` (env-override path for upgrade `target_repo` so E2E redirects without forking the registry)
- Modify: `tests/integration/test_upgrade_deploy_pin.py:280-352` (update `_read_cloudbuild_upgrade_target_repo_envs` to resolve the `_UPGRADE_TARGET_REPO` substitution default before comparing; this preserves the deploy-vs-registry safety property under the new substitution shape)
- Create: `docs/runbooks/e2e-environment.md`
- Create: `tests/unit/test_setup_e2e_project_script.py`
- Create: `tests/unit/test_upgrade_target_env_override.py`

**Step 1: Extract shared `setup_secrets.sh` helpers into `_setup_lib.sh`**

Create `infra/scripts/_setup_lib.sh` containing the idempotent describe-then-act helpers (`grant_role_idempotent`, `create_secret_idempotent`, `enable_api_idempotent`, etc.). Both `setup_secrets.sh` and the new `setup_e2e_project.sh` source it. `setup_secrets.sh` behavior is unchanged.

**Step 2: Parameterize `infra/cloudbuild.yaml` — env vars AND deploy targets**

Add four substitutions (defaults preserve prod behavior):

```yaml
substitutions:
  _TAG: 'manual'                                # existing
  _TARGET_SERVICE: payment-demo                 # new
  _TARGET_GITHUB_REPO: adi-prasetyo/driftscribe # new
  _UPGRADE_TARGET_REPO: adi-prasetyo/driftscribe # new
  _USE_ADK: 'false'                             # new — E2E build overrides to 'true'
```

**Why `_USE_ADK`:** the coordinator deploy step currently pins `USE_ADK=false` (line 161). `/chat` hard-503s when ADK is off (`agent/main.py:1884`). Every plan task that hits `/chat` (drift-chat tests in 20.3, HITL in 20.4, upgrade in 20.5, UI in 20.6) would fail on a fresh E2E deploy unless ADK is enabled. Parameterizing keeps the prod default (`false` → operator must opt-in to ADK on prod after verifying Vertex quota) while letting the E2E build pass `_USE_ADK=true`.

Replace literals at the actual deploy + env-var + IAM sites:

**Deploy step for the drift target (lines ~101-113):**
- Line 106: `- payment-demo` → `- $_TARGET_SERVICE`
- Line 107: `--image=...driftscribe/payment-demo:${_TAG}` → `--image=...driftscribe/$_TARGET_SERVICE:${_TAG}`. **Note:** this requires the Docker build/push step for the demo target (early in the file) to push to the parameterized image tag too — find every `docker push` line referencing `driftscribe/payment-demo:` and replace `payment-demo` with `$_TARGET_SERVICE`.

**Env-var lines:**
- Coordinator (line 161): `TARGET_SERVICE=payment-demo` → `TARGET_SERVICE=$_TARGET_SERVICE`; `GITHUB_REPO=adi-prasetyo/driftscribe` → `GITHUB_REPO=$_TARGET_GITHUB_REPO`; `USE_ADK=false` → `USE_ADK=$_USE_ADK`. **Also add a new env var** `UPGRADE_TARGET_REPO_OVERRIDE=$_UPGRADE_TARGET_REPO` so the agent-side registry override matches the worker-side authority.
- Reader worker (line 209): `TARGET_SERVICE=payment-demo` → `TARGET_SERVICE=$_TARGET_SERVICE`.
- Docs worker (line 262): `TARGET_REPO=adi-prasetyo/driftscribe` → `TARGET_REPO=$_TARGET_GITHUB_REPO`.
- Rollback worker (line 324): `TARGET_SERVICE=payment-demo` → `TARGET_SERVICE=$_TARGET_SERVICE`.
- Upgrade-reader (line 442): `UPGRADE_TARGET_REPO=adi-prasetyo/driftscribe` → `UPGRADE_TARGET_REPO=$_UPGRADE_TARGET_REPO`.
- Upgrade-docs (line 488): same.

**Rollback worker IAM grant** (search for the post-deploy step that grants `rollback-agent-sa` permission on the drift target): replace `payment-demo` with `$_TARGET_SERVICE` so the e2e build grants the right service.

**Post-deploy URL-resolution steps** (the trailing `gcloud run services describe` block that writes worker URLs back to the coordinator): any reference to `payment-demo` here also gets `$_TARGET_SERVICE`. The other worker names (`driftscribe-reader`, etc.) stay as literals — only the drift target service name varies.

The E2E build invokes:
```bash
gcloud builds submit --config infra/cloudbuild.yaml \
  --substitutions=_TARGET_SERVICE=payment-demo-e2e,_TARGET_GITHUB_REPO=adi-prasetyo/driftscribe-e2e-target,_UPGRADE_TARGET_REPO=adi-prasetyo/driftscribe-e2e-target,_USE_ADK=true
```

`_USE_ADK=true` is mandatory for E2E: every `/chat` test depends on it. Prod deploys omit the substitution so they stay at the safer `false` default until the operator opts in via `gcloud run services update`.

**Implementer note:** Re-grep `infra/cloudbuild.yaml` for every literal `payment-demo` and `adi-prasetyo/driftscribe` and decide per-occurrence: replace with a substitution, or leave (e.g., comments, the worker service name `driftscribe-*`). The meta-tests below cover the env-var spots; the deploy/image/IAM spots need careful review.

**Step 3: Add env-override for the upgrade target registry**

`agent/workloads/registry.py:507-515`: keep `_UPGRADE_TARGET_REGISTRY` as the default, but resolve `target_repo` through an env override at resolution time so the agent-side intent matches the worker-side authority.

In `resolve_upgrade_target` (around line 525):

```python
def resolve_upgrade_target(name: str) -> UpgradeTarget:
    base = UPGRADE_TARGET_REGISTRY.get(name)
    if base is None:
        raise UnknownUpgradeTargetError(name)
    override = os.environ.get("UPGRADE_TARGET_REPO_OVERRIDE")
    if override:
        # Phase 20: parity with the worker-side UPGRADE_TARGET_REPO env pin.
        # Required so the agent's tool args match what the worker accepts.
        return UpgradeTarget(
            target_repo=override,
            lockfile_path=base.lockfile_path,
            advisory_source=base.advisory_source,
        )
    return base
```

The coordinator's cloudbuild step receives `UPGRADE_TARGET_REPO_OVERRIDE=$_UPGRADE_TARGET_REPO` (same substitution as the worker pin). Prod default is unchanged (env var unset → original registry value).

**Step 4: Write the failing meta-tests**

`tests/unit/test_setup_e2e_project_script.py`:

```python
import re
from pathlib import Path


def test_setup_e2e_project_script_exists():
    script = Path("infra/scripts/setup_e2e_project.sh")
    assert script.exists()
    assert script.stat().st_mode & 0o111


def test_setup_e2e_project_sources_shared_lib():
    body = Path("infra/scripts/setup_e2e_project.sh").read_text()
    assert "source" in body and "_setup_lib.sh" in body


def test_setup_e2e_project_creates_all_eight_prod_secrets():
    """Phase 20 fix: setup must create every secret cloudbuild.yaml mounts."""
    body = Path("infra/scripts/setup_e2e_project.sh").read_text()
    for secret in (
        "coordinator-shared-token",
        "github-pat",
        "developer-knowledge-api-key",
        "docs-agent-github-pat",
        "approval-hmac-key",
        "driftscribe-webhook-url",
        "upgrade-reader-github-pat",
        "upgrade-docs-github-pat",
    ):
        assert secret in body, f"missing secret: {secret}"


def test_setup_e2e_project_uses_real_sa_names():
    """Use the actual SA names from cloudbuild.yaml, not invented short names."""
    body = Path("infra/scripts/setup_e2e_project.sh").read_text()
    for sa in (
        "driftscribe-agent",
        "reader-agent-sa",
        "docs-agent-sa",
        "rollback-agent-sa",
        "notifier-agent-sa",
        "upgrade-reader-sa",
        "upgrade-docs-sa",
        "e2e-runner-sa",
    ):
        assert sa in body, f"missing SA: {sa}"
    # No invented short names.
    assert "coord-sa" not in body
    assert "reader-sa" not in body or "reader-sa@" not in body  # must be reader-agent-sa
    # No old/wrong names.
    assert "OPERATOR_TOKEN" not in body
    assert "HITL_HMAC_KEY" not in body
    assert "GEMINI_API_KEY" not in body


def test_setup_e2e_project_grants_logging_viewer():
    body = Path("infra/scripts/setup_e2e_project.sh").read_text()
    assert "roles/logging.viewer" in body


def test_setup_e2e_project_initializes_firestore():
    body = Path("infra/scripts/setup_e2e_project.sh").read_text()
    assert "firestore" in body.lower()
    assert "asia-northeast1" in body


def test_setup_e2e_project_extends_log_retention_to_365():
    body = Path("infra/scripts/setup_e2e_project.sh").read_text()
    assert "365" in body


def test_cloudbuild_has_target_service_substitution():
    body = Path("infra/cloudbuild.yaml").read_text()
    assert "_TARGET_SERVICE:" in body
    assert "$_TARGET_SERVICE" in body or "${_TARGET_SERVICE}" in body


def test_cloudbuild_has_target_github_repo_substitution():
    body = Path("infra/cloudbuild.yaml").read_text()
    assert "_TARGET_GITHUB_REPO:" in body
    assert "_UPGRADE_TARGET_REPO:" in body


def test_cloudbuild_deploys_parameterized_service_name():
    """Phase 20 fix: the 'gcloud run deploy <name>' line must use the substitution,
    not the literal 'payment-demo'. Otherwise the e2e build still deploys prod's
    target. We verify by counting literal occurrences in the deploy block region.
    """
    body = Path("infra/cloudbuild.yaml").read_text()
    # The substitution appears wherever the drift target service is named.
    assert "$_TARGET_SERVICE" in body
    # No `- payment-demo` literal as a deploy arg (the substitution replaces it).
    # Allow the literal default in the substitutions: block at the top.
    deploy_block_starts = [i for i, line in enumerate(body.splitlines())
                            if line.strip() == "- deploy"]
    for idx in deploy_block_starts:
        # The next non-blank line is the service name arg.
        for j in range(idx + 1, min(idx + 4, len(body.splitlines()))):
            line = body.splitlines()[j].strip()
            if line.startswith("- ") and line != "- deploy":
                # If this deploy block targets the drift demo, it must use the substitution.
                if "payment-demo" in line and "$_TARGET_SERVICE" not in line:
                    raise AssertionError(
                        f"Found literal payment-demo as deploy arg (line {j+1}); "
                        f"expected $_TARGET_SERVICE"
                    )
                break


def test_cloudbuild_coordinator_sets_upgrade_target_repo_override():
    """The coordinator's --set-env-vars must include UPGRADE_TARGET_REPO_OVERRIDE
    so the registry override matches the worker-side authority."""
    body = Path("infra/cloudbuild.yaml").read_text()
    assert "UPGRADE_TARGET_REPO_OVERRIDE=$_UPGRADE_TARGET_REPO" in body \
        or "UPGRADE_TARGET_REPO_OVERRIDE=${_UPGRADE_TARGET_REPO}" in body


def test_cloudbuild_use_adk_is_parameterized():
    """USE_ADK must be a substitution so E2E can flip to true while prod stays false."""
    body = Path("infra/cloudbuild.yaml").read_text()
    assert "_USE_ADK:" in body, "missing _USE_ADK substitution"
    assert "USE_ADK=$_USE_ADK" in body or "USE_ADK=${_USE_ADK}" in body, \
        "coordinator env must use the substitution, not a literal"
    assert "USE_ADK=false" not in body, "literal USE_ADK=false would shadow the substitution"


def test_cloudbuild_demo_target_image_tag_parameterized():
    """Phase 20: payment-demo image push/deploy must reference $_TARGET_SERVICE,
    not the literal 'payment-demo' in the image tag.
    """
    body = Path("infra/cloudbuild.yaml").read_text()
    # The substitution default IS 'payment-demo', so 'driftscribe/payment-demo:' may
    # appear only as a comment or as the default expansion. The literal in deploy
    # args is what we guard against.
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        # Match the image arg shape that's specifically the drift target.
        if "driftscribe/payment-demo:" in stripped and "$_TARGET_SERVICE" not in stripped:
            raise AssertionError(
                f"Found literal driftscribe/payment-demo: image ref outside a comment; "
                f"line: {line!r}"
            )


def test_e2e_runbook_documents_use_adk_true():
    """The e2e-environment runbook MUST tell operators to pass _USE_ADK=true."""
    body = Path("docs/runbooks/e2e-environment.md").read_text()
    assert "_USE_ADK=true" in body, \
        "runbook must include _USE_ADK=true in the E2E build command"
```

`tests/unit/test_upgrade_target_env_override.py`:

```python
"""Phase 20: UPGRADE_TARGET_REPO_OVERRIDE redirects the registry-resolved target_repo."""
import os
from unittest.mock import patch

from agent.workloads.registry import resolve_upgrade_target


def test_resolve_uses_registry_default_without_override():
    if "UPGRADE_TARGET_REPO_OVERRIDE" in os.environ:
        del os.environ["UPGRADE_TARGET_REPO_OVERRIDE"]
    target = resolve_upgrade_target("phase17_demo")
    assert target.target_repo == "adi-prasetyo/driftscribe"


def test_resolve_uses_override_when_set():
    with patch.dict(os.environ, {"UPGRADE_TARGET_REPO_OVERRIDE": "acme/driftscribe-e2e-target"}):
        target = resolve_upgrade_target("phase17_demo")
        assert target.target_repo == "acme/driftscribe-e2e-target"
        # lockfile_path + advisory_source are untouched.
        assert target.lockfile_path == "demo/upgrade-target/package.json"
```

Run: both test files FAIL.

**Step 4.5: Update `tests/integration/test_upgrade_deploy_pin.py` for the new substitution shape**

The existing test `test_cloudbuild_upgrade_target_repo_matches_registry` parses `UPGRADE_TARGET_REPO=([^,\s]+)` from `cloudbuild.yaml` and compares it to `UPGRADE_TARGET_REGISTRY["phase17_demo"].target_repo`. After parameterization the regex captures `$_UPGRADE_TARGET_REPO` literally and the test fails.

Update `_read_cloudbuild_upgrade_target_repo_envs` to **resolve the substitution default before returning**:

```python
def _resolve_substitution(body: str, name: str) -> str:
    """Read the default value of a Cloud Build substitution from the
    substitutions: block at the top of cloudbuild.yaml.
    """
    m = re.search(rf"^\s*{re.escape(name)}:\s*['\"]?([^'\"\n]+)['\"]?\s*$",
                  body, flags=re.MULTILINE)
    assert m, f"could not find substitution default for {name!r}"
    return m.group(1).strip()


def _read_cloudbuild_upgrade_target_repo_envs() -> dict[str, str]:
    text = (Path(__file__).resolve().parents[2] / "infra" / "cloudbuild.yaml").read_text()
    out: dict[str, str] = {}
    for service in ("driftscribe-upgrade-reader", "driftscribe-upgrade-docs"):
        m = re.search(
            rf"-\s+{re.escape(service)}\b.*?--set-env-vars=([^\n]+)",
            text, re.DOTALL,
        )
        assert m, f"could not locate deploy block for {service}"
        env_line = m.group(1)
        kv = re.search(r"UPGRADE_TARGET_REPO=([^,\s]+)", env_line)
        assert kv, f"could not extract UPGRADE_TARGET_REPO= from {service}'s env"
        raw = kv.group(1)
        # Phase 20: value may be the literal slug (pre-20) OR a substitution
        # reference like $_UPGRADE_TARGET_REPO (post-20). Resolve substitutions
        # against the substitutions: block default so the deploy-vs-registry
        # equality invariant still holds in the default (prod) case.
        if raw.startswith("$"):
            sub_name = raw.lstrip("${").rstrip("}")
            raw = _resolve_substitution(text, sub_name)
        out[service] = raw
    return out
```

`tests/unit/test_upgrade_target_registry.py` is unaffected — it asserts on the in-process registry (the default value when `UPGRADE_TARGET_REPO_OVERRIDE` is unset). The new `tests/unit/test_upgrade_target_env_override.py` (Step 4) covers the override path.

**Step 5: Implement `setup_e2e_project.sh`**

Required env: `PROJECT_E2E` (e.g. `driftscribe-e2e`).

Script body:
- Validate project exists + `roles/owner` on it.
- **Enable APIs** (must match prod's full set — see `setup_secrets.sh:66-86`):
  `run.googleapis.com`, `firestore.googleapis.com`, `secretmanager.googleapis.com`, `logging.googleapis.com`, `cloudbuild.googleapis.com`, `iam.googleapis.com`, `iamcredentials.googleapis.com`, `aiplatform.googleapis.com`, `artifactregistry.googleapis.com` (Docker push target), `developerknowledge.googleapis.com` (MCP backend), `eventarc.googleapis.com` and `eventarcpublishing.googleapis.com` (auto-trigger path — not required by the Phase 20 test set but mirrors prod parity so the e2e project doesn't drift from `setup_secrets.sh`).
- **Enable Developer Knowledge MCP** specifically:
  `gcloud beta services mcp enable developerknowledge.googleapis.com --project=$PROJECT_E2E` — mirrors `setup_secrets.sh:86`.
- **Create the Artifact Registry repo** that `cloudbuild.yaml` pushes to:
  ```bash
  gcloud artifacts repositories create driftscribe \
    --project=$PROJECT_E2E --repository-format=docker --location=asia-northeast1
  ```
- Create the 8 service accounts: `driftscribe-agent`, `reader-agent-sa`, `docs-agent-sa`, `rollback-agent-sa`, `notifier-agent-sa`, `upgrade-reader-sa`, `upgrade-docs-sa`, `e2e-runner-sa`.
- Grant IAM roles per service (same matrix as prod `setup_secrets.sh`; the cloudbuild SA needs `roles/artifactregistry.writer` to push images — mirrors `setup_secrets.sh:106`).
- Grant `roles/logging.viewer` on the project to `driftscribe-agent`.
- **`e2e-runner-sa` permissions** (the GitHub Actions runner identity):
  - `roles/run.viewer` (read service URLs, project-wide)
  - `roles/secretmanager.secretAccessor` (bound **per-secret** on `coordinator-shared-token` and `upgrade-docs-github-pat`, not project-wide)
  - `roles/run.developer` on `payment-demo-e2e` only (resource-scoped — for the E2E env-mutation fixtures). Bind via:
    ```bash
    gcloud run services add-iam-policy-binding payment-demo-e2e \
      --project=$PROJECT_E2E --region=asia-northeast1 \
      --member="serviceAccount:e2e-runner-sa@$PROJECT_E2E.iam.gserviceaccount.com" \
      --role="roles/run.developer"
    ```
    **Note:** binding only works after `payment-demo-e2e` is deployed; this is a post-first-deploy step. Document accordingly in the runbook ("run this after the first `gcloud builds submit`").
  - `roles/iam.serviceAccountUser` on the Cloud Run runtime SA the `payment-demo-e2e` service runs under (so the runner can `act-as` it during updates). Default Cloud Run service identity is the project's Compute Engine default SA unless `payment-demo-e2e`'s deploy step specifies `--service-account=...`. Check the deploy step (lines ~101-113 of cloudbuild) — if no `--service-account=` is set, target the default compute SA: `<PROJECT_NUMBER>-compute@developer.gserviceaccount.com`. Document this in the runbook with the exact `gcloud iam service-accounts add-iam-policy-binding` command.
  - `roles/datastore.user` (Firestore writes for the cleanup tracker fixture):
    ```bash
    gcloud projects add-iam-policy-binding $PROJECT_E2E \
      --member="serviceAccount:e2e-runner-sa@$PROJECT_E2E.iam.gserviceaccount.com" \
      --role="roles/datastore.user"
    ```
- Initialize Firestore in `asia-northeast1` (Native mode).
- Create the 8 secrets (placeholders — operator populates via `gcloud secrets versions add` afterward):
  `coordinator-shared-token`, `github-pat`, `developer-knowledge-api-key`, `docs-agent-github-pat`, `approval-hmac-key`, `driftscribe-webhook-url`, `upgrade-reader-github-pat`, `upgrade-docs-github-pat`.
- Set Cloud Logging `_Default` bucket retention to 365 days.
- Print a "next steps" block: how to populate secrets (one block per secret), how to deploy, and the post-deploy IAM binding commands above.

**Step 6: Run tests — expect PASS**

```bash
pytest tests/unit/test_setup_e2e_project_script.py tests/unit/test_upgrade_target_env_override.py -v
```

**Step 7: Write `docs/runbooks/e2e-environment.md`**

Documents:
- One-time provisioning (run `setup_e2e_project.sh`, populate the 8 secrets, deploy via the parameterized `cloudbuild.yaml`).
- Per-secret operator action (which PAT goes where, what the webhook URL points at).
- How to seed `driftscribe-e2e-target` with the lodash 4.17.20 lockfile.
- How to verify the deploy is healthy.
- Manual baseline-reset command for `payment-demo-e2e` (in case a crashed test left it dirty).
- Teardown via `gcloud projects delete`.
- Cross-link to `e2e-ci.md` (Task 20.7a).

**Step 8: Commit**

```bash
git add infra/scripts/_setup_lib.sh infra/scripts/setup_e2e_project.sh \
        infra/scripts/setup_secrets.sh infra/cloudbuild.yaml \
        agent/workloads/registry.py \
        tests/unit/test_setup_e2e_project_script.py \
        tests/unit/test_upgrade_target_env_override.py \
        docs/runbooks/e2e-environment.md
git commit -m "feat(infra): driftscribe-e2e provisioning + cloudbuild substitutions + registry override (20.0)"
```

---

## Task 20.1: pytest `e2e` marker + harness + session-scoped baseline guard + Firestore tracker

**Files:**
- Modify: `pyproject.toml` (register marker; keep `workers` in testpaths)
- Create: `tests/e2e/__init__.py`
- Create: `tests/e2e/conftest.py`
- Create: `tests/e2e/_helpers.py`
- Create: `tests/unit/test_e2e_harness_gating.py`

**Step 1: Register the marker, preserve workers/ in testpaths**

`[tool.pytest.ini_options]`:

```toml
testpaths = ["tests/unit", "tests/integration", "workers"]
markers = [
    "e2e: end-to-end tests requiring a deployed coordinator (skip by default)",
]
addopts = "--strict-markers"
```

`tests/e2e/` is omitted; to run E2E: `pytest tests/e2e -m e2e`.

**Step 2: Write failing harness gating tests**

`tests/unit/test_e2e_harness_gating.py`:

```python
import subprocess
import sys
from pathlib import Path


def test_e2e_marker_registered():
    body = Path("pyproject.toml").read_text()
    assert 'e2e: end-to-end tests' in body


def test_workers_still_in_testpaths():
    body = Path("pyproject.toml").read_text()
    assert '"workers"' in body


def test_default_pytest_does_not_collect_e2e_dir():
    body = Path("pyproject.toml").read_text()
    assert '"tests/e2e"' not in body


def test_e2e_conftest_skips_without_env(monkeypatch):
    monkeypatch.delenv("DRIFTSCRIBE_E2E_URL", raising=False)
    monkeypatch.delenv("DRIFTSCRIBE_E2E_TOKEN", raising=False)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/e2e", "-m", "e2e", "--collect-only"],
        capture_output=True, text=True,
    )
    combined = (result.stdout + result.stderr).lower()
    assert ("0 tests collected" in combined
            or "skipping" in combined
            or result.returncode == 5)
```

**Step 3: Implement `tests/e2e/conftest.py`**

```python
"""E2E test harness — skip by default; require explicit env vars.

Key contracts (verified against agent/ and driftscribe_lib/):
- Auth header is X-DriftScribe-Token (NOT X-Operator-Token).
- Baseline reads use read_live_state (serving revision), NOT read_live_env (template).
- Cloud Run mutations use update_mask + LRO .result(timeout=) wait.
"""
import os

import httpx
import pytest

from driftscribe_lib.cloud_run import read_live_state


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"E2E disabled: ${name} not set", allow_module_level=False)
    return value


@pytest.fixture(scope="session")
def e2e_base_url() -> str:
    return _require_env("DRIFTSCRIBE_E2E_URL").rstrip("/")


@pytest.fixture(scope="session")
def e2e_operator_token() -> str:
    return _require_env("DRIFTSCRIBE_E2E_TOKEN")


@pytest.fixture(scope="session")
def e2e_github_repo() -> str:
    return os.environ.get("DRIFTSCRIBE_E2E_GITHUB_REPO", "adi-prasetyo/driftscribe-e2e-target")


@pytest.fixture(scope="session")
def e2e_gcp_project() -> str:
    return _require_env("DRIFTSCRIBE_E2E_PROJECT")


@pytest.fixture
def coordinator_client(e2e_base_url, e2e_operator_token):
    headers = {"X-DriftScribe-Token": e2e_operator_token}
    with httpx.Client(base_url=e2e_base_url, headers=headers, timeout=60.0) as client:
        yield client


@pytest.fixture(scope="session", autouse=True)
def _payment_demo_e2e_baseline_guard(e2e_gcp_project):
    """Session-scoped serving-revision snapshot + force-restore.

    Uses read_live_state (NOT read_live_env) so the snapshot reflects what
    traffic is actually being served, not what the next deploy would be.
    Restore uses google.cloud.run_v2 with update_mask + LRO wait.
    """
    from google.cloud import run_v2
    from google.protobuf.field_mask_pb2 import FieldMask

    service = "payment-demo-e2e"
    region = "asia-northeast1"
    try:
        baseline = read_live_state(service, region, e2e_gcp_project)
    except Exception as exc:
        pytest.skip(f"E2E disabled: cannot read {service} serving state: {exc}")

    yield baseline

    # Teardown — force-restore the serving env via Cloud Run SDK with mask + wait.
    services_client = run_v2.ServicesClient()
    name = f"projects/{e2e_gcp_project}/locations/{region}/services/{service}"
    svc = services_client.get_service(name=name)
    container = svc.template.containers[0]
    while len(container.env):
        container.env.pop()
    for k, v in baseline["env"].items():
        container.env.append(run_v2.EnvVar(name=k, value=v))
    op = services_client.update_service(
        service=svc,
        update_mask=FieldMask(paths=["template"]),
    )
    op.result(timeout=180.0)


@pytest.fixture(scope="session", autouse=True)
def _firestore_cleanup_tracker(e2e_gcp_project):
    """Track-and-delete E2E-created Firestore docs at session end."""
    tracked: dict[str, list[str]] = {"decisions": [], "approvals": []}
    yield tracked

    from google.cloud import firestore
    db = firestore.Client(project=e2e_gcp_project)
    for collection, ids in tracked.items():
        for doc_id in ids:
            try:
                db.collection(collection).document(doc_id).delete()
            except Exception:
                pass
```

**Step 4: Implement `tests/e2e/_helpers.py`**

```python
"""Polling + parsing helpers for E2E tests."""
import time
from typing import Any, Callable
from urllib.parse import urlparse, parse_qs

import httpx


class PollTimeout(AssertionError):
    pass


def wait_for(
    predicate: Callable[[], Any],
    *,
    timeout: float = 60.0,
    interval: float = 2.0,
    description: str = "condition",
) -> Any:
    deadline = time.monotonic() + timeout
    last_value = None
    while time.monotonic() < deadline:
        last_value = predicate()
        if last_value:
            return last_value
        time.sleep(interval)
    raise PollTimeout(f"timed out waiting for {description} (last={last_value!r})")


def wait_for_trace_complete(
    client: httpx.Client, trace_id: str, *, timeout: float = 120.0
) -> dict:
    """Poll GET /trace/{trace_id} until response['complete'] is True.

    /trace/{id} returns {trace_id, events, decision, complete, fetched_from_cache}.
    The 'complete' flag means observed-stability has elapsed (_STABILITY_GRACE_S=30s
    in the coordinator); 120s timeout = grace + log tail + ADK slow path headroom.
    """
    def _check():
        resp = client.get(f"/trace/{trace_id}")
        if resp.status_code != 200:
            return None
        body = resp.json()
        return body if body.get("complete") else None

    return wait_for(_check, timeout=timeout, interval=3.0, description=f"trace {trace_id} complete")


def parse_approval_url(text: str) -> tuple[str, str, str]:
    """Extract (full_url, approval_id, token) from coordinator response text.

    URL shape: {COORDINATOR_URL}/approvals/{id}?t=<token>.
    Both id and t are required. Uses urlparse (clearer than regex).
    """
    import re
    match = re.search(r"https?://\S+/approvals/[A-Za-z0-9_-]+\?t=[A-Za-z0-9_.\-]+", text)
    if not match:
        raise AssertionError(
            f"expected approval URL '.../approvals/<id>?t=<token>'; got: {text[:500]}"
        )
    full_url = match.group(0).rstrip(".,)\"'")
    parsed = urlparse(full_url)
    path_parts = parsed.path.rstrip("/").split("/")
    approval_id = path_parts[-1]
    token_list = parse_qs(parsed.query).get("t", [])
    if not token_list:
        raise AssertionError(f"approval URL missing ?t= query param: {full_url}")
    return full_url, approval_id, token_list[0]


def find_approval_url_in_trace_events(events: list[dict]) -> tuple[str, str, str] | None:
    """Walk a /trace events list looking for an approval URL.

    Preferred over reading from /chat reply because:
    - tool_result events carry the worker's structured output (where the URL is
      synthesized);
    - the LLM's free-form reply may paraphrase or omit it.
    """
    for ev in events:
        # Stringify the event and scan — keeps the helper resilient to schema
        # tweaks in the events shape.
        try:
            text = repr(ev)
        except Exception:
            continue
        if "/approvals/" in text and "?t=" in text:
            try:
                return parse_approval_url(text)
            except AssertionError:
                continue
    return None
```

**Step 5: Run gating tests; confirm e2e dir is skipped on a bare pytest run**

```bash
pytest tests/unit/test_e2e_harness_gating.py -v
pytest -m e2e tests/e2e -v   # 0 collected or skipped
pytest tests/unit tests/integration workers -v   # workers/ still collected; full suite green
```

**Step 6: Commit**

```bash
git add pyproject.toml tests/e2e/ tests/unit/test_e2e_harness_gating.py
git commit -m "feat(e2e): pytest e2e marker + harness with baseline guard + tracker (20.1)"
```

---

## Task 20.2: `demo.sh` smoke assertions — split `assert_recheck_action` vs `assert_chat_reply`

**Files:**
- Modify: `scripts/demo.sh`
- Create: `tests/unit/test_demo_sh_assertions.py`

**Beat → endpoint map (verified):**
- `beat-a`, `beat-b`, `beat-c`, `beat-d` → `/recheck` (returns `DecisionProposal` with `.action`).
- `beat-e` → `/chat` (returns `.reply` + `.tool_calls`).
- `upgrade-a`, `upgrade-b`, `upgrade-c` → `/chat`.

Earlier drafts asserted `.reply` against `/recheck` (which has no reply) and `.action` against `/chat` (which has no action). Both fail-by-construction. The fix is two helpers, one per response shape.

**Step 1: Write the failing meta-test**

`tests/unit/test_demo_sh_assertions.py`:

```python
from pathlib import Path


def test_demo_sh_has_both_assertion_helpers():
    body = Path("scripts/demo.sh").read_text()
    assert "assert_recheck_action" in body
    assert "assert_chat_reply" in body


def test_demo_sh_assert_is_opt_in_default_off():
    body = Path("scripts/demo.sh").read_text()
    assert 'ASSERT="${ASSERT:-0}"' in body or 'ASSERT=${ASSERT:-0}' in body


def test_recheck_beats_use_recheck_assertion():
    """beat-a..d hit /recheck — they must assert on .action, not .reply."""
    body = Path("scripts/demo.sh").read_text()
    # Each beat function should call assert_recheck_action with the expected action.
    assert "assert_recheck_action no_op" in body            # beat-a
    assert "assert_recheck_action drift_issue" in body      # beat-b
    assert "assert_recheck_action docs_pr" in body          # beat-d
    # beat-c is ADK-non-deterministic — assert any valid action via the helper's
    # "ANY" sentinel.
    assert "assert_recheck_action ANY" in body              # beat-c


def test_chat_beats_use_chat_reply_assertion():
    """beat-e + upgrade-* hit /chat — assert on .reply present."""
    body = Path("scripts/demo.sh").read_text()
    # Count call sites (one per chat beat).
    chat_assert_count = body.count("assert_chat_reply")
    # beat-e + upgrade-a + upgrade-b + upgrade-c = 4 minimum (one helper definition
    # + ≥4 call sites).
    assert chat_assert_count >= 5, f"expected ≥5 (definition + 4 calls), got {chat_assert_count}"
```

**Step 2: Add both helpers in `scripts/demo.sh`**

```bash
ASSERT="${ASSERT:-0}"

assert_recheck_action() {
  # Usage: assert_recheck_action <expected|ANY> <response_body>
  # Verifies the /recheck DecisionProposal has the expected .action.
  # Pass "ANY" to assert only that .action exists (for non-deterministic ADK paths).
  local expected="$1"
  local body="$2"
  if [ "$ASSERT" != "1" ]; then return 0; fi
  if ! command -v jq >/dev/null; then
    echo "ASSERT=1 requires jq; skipping" >&2
    return 0
  fi
  local got
  got="$(echo "$body" | jq -r '.action // empty')"
  if [ -z "$got" ]; then
    echo "ASSERT FAIL: /recheck response missing .action. Body: $body" >&2
    exit 1
  fi
  if [ "$expected" != "ANY" ] && [ "$got" != "$expected" ]; then
    echo "ASSERT FAIL: expected action=$expected, got action=$got" >&2
    exit 1
  fi
  echo "ASSERT OK: action=$got" >&2
}

assert_chat_reply() {
  # Usage: assert_chat_reply <response_body>
  # Verifies the /chat response has a non-empty .reply field.
  local body="$1"
  if [ "$ASSERT" != "1" ]; then return 0; fi
  if ! command -v jq >/dev/null; then
    echo "ASSERT=1 requires jq; skipping" >&2
    return 0
  fi
  local reply
  reply="$(echo "$body" | jq -r '.reply // empty')"
  if [ -z "$reply" ]; then
    echo "ASSERT FAIL: /chat response missing .reply. Body: $body" >&2
    exit 1
  fi
  echo "ASSERT OK: .reply present (${#reply} chars)" >&2
}
```

**Step 3: Wire helpers into each beat**

Capture the response body into `RESPONSE_BODY`, then call the right assertion:

| Beat | Endpoint | Assertion |
|------|----------|-----------|
| beat-a | `/recheck` | `assert_recheck_action no_op "$RESPONSE_BODY"` |
| beat-b | `/recheck` | `assert_recheck_action drift_issue "$RESPONSE_BODY"` |
| beat-c | `/recheck` | `assert_recheck_action ANY "$RESPONSE_BODY"` |
| beat-d | `/recheck` | `assert_recheck_action docs_pr "$RESPONSE_BODY"` |
| beat-e | `/chat` | `assert_chat_reply "$RESPONSE_BODY"` |
| upgrade-a | `/chat` | `assert_chat_reply "$RESPONSE_BODY"` |
| upgrade-b | `/chat` | `assert_chat_reply "$RESPONSE_BODY"` |
| upgrade-c | `/chat` | `assert_chat_reply "$RESPONSE_BODY"` |

**Step 4: Run meta-tests, then smoke against prod**

```bash
pytest tests/unit/test_demo_sh_assertions.py -v
PROJECT=driftscribe-hack-2026 ASSERT=1 ./scripts/demo.sh beat-a
PROJECT=driftscribe-hack-2026 ASSERT=1 ./scripts/demo.sh beat-b
# etc.
```

**Step 5: Commit**

```bash
git add scripts/demo.sh tests/unit/test_demo_sh_assertions.py
git commit -m "feat(demo): opt-in ASSERT=1 splits recheck-action vs chat-reply (20.2)"
```

---

## Task 20.3: Drift workload E2E — `/recheck` for action, `/chat` for tool_calls + trace

**Files:**
- Create: `tests/e2e/test_drift_workload.py`
- Modify: `tests/e2e/conftest.py` (add `drift_e2e_target` fixture with update_mask + LRO wait)

**Step 1: Add `drift_e2e_target` fixture in `tests/e2e/conftest.py`**

```python
from google.cloud import run_v2
from google.protobuf.field_mask_pb2 import FieldMask

from driftscribe_lib.cloud_run import read_live_state


@pytest.fixture
def drift_e2e_target(e2e_gcp_project, _payment_demo_e2e_baseline_guard):
    """Per-test env mutator with proper update_mask + LRO wait + serving-state polling."""
    service = "payment-demo-e2e"
    region = "asia-northeast1"
    baseline = _payment_demo_e2e_baseline_guard  # {"env": {...}, "revision": "..."}

    class _Target:
        def __init__(self) -> None:
            self.client = run_v2.ServicesClient()
            self.name = f"projects/{e2e_gcp_project}/locations/{region}/services/{service}"

        def baseline_revision(self) -> str:
            return baseline["revision"]

        def _update_env(self, env_dict: dict[str, str]) -> None:
            svc = self.client.get_service(name=self.name)
            container = svc.template.containers[0]
            while len(container.env):
                container.env.pop()
            for k, v in env_dict.items():
                container.env.append(run_v2.EnvVar(name=k, value=v))
            op = self.client.update_service(
                service=svc, update_mask=FieldMask(paths=["template"])
            )
            op.result(timeout=180.0)
            # Wait for serving revision to actually pick up the new env.
            self._wait_for_serving_env(env_dict)

        def _wait_for_serving_env(self, expected: dict[str, str], timeout: float = 120.0) -> None:
            import time
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                live = read_live_state(service, region, e2e_gcp_project)
                # Compare only the keys we care about; other env may be present.
                if all(live["env"].get(k) == v for k, v in expected.items()):
                    return
                time.sleep(3.0)
            raise AssertionError(f"serving env did not converge to {expected} within {timeout}s")

        def set_env(self, key: str, value: str) -> None:
            live = read_live_state(service, region, e2e_gcp_project)
            new_env = dict(live["env"])
            new_env[key] = value
            self._update_env(new_env)

        def restore_baseline(self) -> None:
            self._update_env(baseline["env"])

        def read_serving_env(self) -> dict[str, str]:
            return read_live_state(service, region, e2e_gcp_project)["env"]

        def is_at_baseline(self) -> bool:
            return self.read_serving_env() == baseline["env"]

    target = _Target()
    yield target
    target.restore_baseline()
```

**Step 2: Write `tests/e2e/test_drift_workload.py`**

```python
"""E2E: drift workload — action via /recheck, reasoning via /chat.

Env-var choice: the ops contract declares PAYMENT_MODE='mock' and
FEATURE_NEW_CHECKOUT='false'. Mutating PAYMENT_MODE='live' is the canonical
drift signal (matches scripts/demo.sh beat-b). Mutating an unknown var
(e.g. FEATURE_FLAG_NEW_PAYMENT) does NOT reliably yield drift_issue — the
contract only checks declared vars.
"""
import pytest

from tests.e2e._helpers import wait_for_trace_complete


def _track_decision(body: dict, tracker: dict) -> None:
    """Append the decision_id from a /recheck response to the cleanup tracker.

    The DecisionProposal returned by /recheck carries decision_id (the Firestore
    doc ID). If a future contract change drops it, the helper silently no-ops —
    we don't want a cleanup-bookkeeping miss to fail the actual test.
    """
    decision_id = body.get("decision_id")
    if decision_id:
        tracker["decisions"].append(decision_id)


@pytest.mark.e2e
def test_baseline_recheck_returns_no_op(coordinator_client, drift_e2e_target, _firestore_cleanup_tracker):
    """payment-demo-e2e env matches contract → /recheck returns no_op."""
    assert drift_e2e_target.is_at_baseline(), (
        "test pre-condition: payment-demo-e2e must start at baseline"
    )
    resp = coordinator_client.post("/recheck", json={"workload": "drift"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    _track_decision(body, _firestore_cleanup_tracker)
    assert body["action"] == "no_op", f"expected no_op, got {body['action']}"


@pytest.mark.e2e
def test_drift_recheck_returns_drift_issue(coordinator_client, drift_e2e_target, _firestore_cleanup_tracker):
    """PAYMENT_MODE=live (drift) → /recheck returns drift_issue."""
    drift_e2e_target.set_env("PAYMENT_MODE", "live")
    resp = coordinator_client.post("/recheck?force=true", json={"workload": "drift"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    _track_decision(body, _firestore_cleanup_tracker)
    assert body["action"] == "drift_issue", \
        f"expected drift_issue after PAYMENT_MODE drift, got {body['action']}"


@pytest.mark.e2e
def test_chat_drift_returns_reply_and_tool_calls(coordinator_client):
    """/chat workload=drift returns the documented free-form shape."""
    resp = coordinator_client.post(
        "/chat",
        json={"workload": "drift", "prompt": "Check payment-demo-e2e for drift"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body.get("reply"), str) and body["reply"]
    assert isinstance(body.get("tool_calls"), list)
    assert body["tool_calls"], "expected at least one tool call from the ADK runner"
    # /chat doesn't return a decision_id (per Phase 17 contract — /chat is
    # free-form, /recheck is the structured surface). No tracker append needed.


@pytest.mark.e2e
def test_chat_trace_id_round_trips_via_events(coordinator_client, _firestore_cleanup_tracker):
    """X-Trace-Id header round-trips via /trace/{id}; response shape is {events, complete, ...}."""
    resp = coordinator_client.post(
        "/chat",
        json={"workload": "drift", "prompt": "Check payment-demo-e2e for drift"},
    )
    assert resp.status_code == 200
    trace_id = resp.headers.get("X-Trace-Id")
    assert trace_id, "X-Trace-Id header missing from /chat response"

    trace = wait_for_trace_complete(coordinator_client, trace_id)
    assert trace["complete"] is True
    # /trace returns events (UI groups them by reading event metadata).
    assert isinstance(trace.get("events"), list)
    assert trace["events"], "expected at least one redacted event in the timeline"
    # The trace may carry an associated decision document if the ADK reasoning
    # loop happened to record one (drift /chat itself does not always persist
    # a DecisionProposal — /recheck is the structured surface). Track if present.
    decision = trace.get("decision") or {}
    if decision.get("decision_id"):
        _firestore_cleanup_tracker["decisions"].append(decision["decision_id"])
```

**Step 3: Operator-side run**

```bash
export DRIFTSCRIBE_E2E_URL="$(gcloud run services describe driftscribe-agent \
  --project driftscribe-e2e --region asia-northeast1 --format='value(status.url)')"
export DRIFTSCRIBE_E2E_TOKEN="$(gcloud secrets versions access latest \
  --secret=coordinator-shared-token --project driftscribe-e2e)"
export DRIFTSCRIBE_E2E_PROJECT=driftscribe-e2e

pytest tests/e2e/test_drift_workload.py -m e2e -v
```

**Step 4: Commit**

```bash
git add tests/e2e/test_drift_workload.py tests/e2e/conftest.py
git commit -m "feat(e2e): drift workload E2E via /recheck + /chat (20.3)"
```

---

## Task 20.4: HITL approval E2E — explicit target_revision + parse URL from /trace events

**Files:**
- Create: `tests/e2e/test_hitl_rollback.py`

**Two crucial fixes vs. earlier draft:**
1. The agent has no revision-enumeration tool — `propose_rollback_tool(target_revision, reason)` needs the operator (or test) to **name** the revision. The fixture captures the baseline revision string via `_payment_demo_e2e_baseline_guard["revision"]` and the test includes it verbatim in the prompt (matches `scripts/demo.sh:359-364`).
2. The approval URL is more reliably found in the `/trace` events stream (carrying tool_result payloads from the rollback worker) than in the LLM's free-form `reply`. Use `find_approval_url_in_trace_events`.

**Step 1: Write the failing test**

```python
"""E2E: HITL rollback — explicit revision, form-POST, 403 on replay."""
import httpx
import pytest

from tests.e2e._helpers import (
    find_approval_url_in_trace_events,
    parse_approval_url,
    wait_for,
    wait_for_trace_complete,
)


@pytest.mark.e2e
def test_rollback_mints_approval_url_and_is_single_use(
    coordinator_client, drift_e2e_target, e2e_base_url, _firestore_cleanup_tracker
):
    """Force rollback via /chat naming an explicit baseline revision."""
    baseline_revision = drift_e2e_target.baseline_revision()
    assert baseline_revision, "baseline revision must be captured pre-test"

    # Drift the payment-demo-e2e env so a rollback is meaningful.
    drift_e2e_target.set_env("PAYMENT_MODE", "live")

    resp = coordinator_client.post(
        "/chat",
        json={
            "workload": "drift",
            "prompt": (
                f"payment mode drifted. roll us back to revision "
                f"{baseline_revision}."
            ),
        },
        timeout=180.0,
    )
    assert resp.status_code == 200, resp.text
    trace_id = resp.headers.get("X-Trace-Id")
    assert trace_id, "X-Trace-Id required to locate approval URL in events"

    trace = wait_for_trace_complete(coordinator_client, trace_id, timeout=180.0)
    found = find_approval_url_in_trace_events(trace["events"])
    assert found is not None, (
        f"no approval URL found in /trace events; "
        f"reply preview={resp.json().get('reply','')[:300]!r}"
    )
    full_url, approval_id, token = found
    # Track the approval doc for session-scoped Firestore cleanup.
    _firestore_cleanup_tracker["approvals"].append(approval_id)
    # The /chat that minted the rollback also writes a decision; track it too.
    decision = trace.get("decision") or {}
    if decision.get("decision_id"):
        _firestore_cleanup_tracker["decisions"].append(decision["decision_id"])

    # Approve via form-POST — NO X-DriftScribe-Token (the token IS the auth).
    plain = httpx.Client(base_url=e2e_base_url, timeout=30.0)
    try:
        approve_1 = plain.post(
            f"/approvals/{approval_id}",
            data={"t": token, "decision": "approve"},
        )
        assert approve_1.status_code == 200, \
            f"first approve should succeed, got {approve_1.status_code}: {approve_1.text[:300]}"

        wait_for(
            lambda: drift_e2e_target.is_at_baseline(),
            timeout=180.0,
            description="rollback to restore baseline env",
        )

        # Replay: 403 (NOT 410).
        approve_2 = plain.post(
            f"/approvals/{approval_id}",
            data={"t": token, "decision": "approve"},
        )
        assert approve_2.status_code == 403, \
            f"single-use violated: replay returned {approve_2.status_code}, want 403"
    finally:
        plain.close()


@pytest.mark.e2e
def test_tampered_token_returns_403(e2e_base_url):
    """Tampered token + nonexistent approval id → 403 (collapsed catch-all)."""
    plain = httpx.Client(base_url=e2e_base_url, timeout=30.0)
    try:
        resp = plain.post(
            "/approvals/nonexistent_id",
            data={"t": "totally-fake-token", "decision": "approve"},
        )
        assert resp.status_code == 403
    finally:
        plain.close()
```

**Step 2: Run E2E + commit**

```bash
pytest tests/e2e/test_hitl_rollback.py -m e2e -v
git add tests/e2e/test_hitl_rollback.py
git commit -m "feat(e2e): HITL rollback E2E with explicit revision + /trace URL extraction (20.4)"
```

---

## Task 20.5: Upgrade workload E2E — observe via GitHub branch + title

**Files:**
- Create: `tests/e2e/test_upgrade_workload.py`
- Create: `tests/e2e/_github_helpers.py`
- Modify: `tests/e2e/conftest.py` (add session-scoped GitHub pre-run sweep)

**Why GitHub-side observation:** `/recheck workload=upgrade` returns 503. So upgrade actions are observed via PR presence/absence in `driftscribe-e2e-target`.

**PR-matching strategy (verified against `agent/adk_tools.py:328-334`):**
- Worker creates a PR with title `upgrade(<package>): <target_version>` and branch `upgrade/<package>-<dashed-version>` (e.g. `upgrade/lodash-4-17-21`).
- Sweep: match branches starting with `upgrade/` (the worker's stable prefix) — NOT title prefix `E2E:` (which doesn't exist).
- Per-test match: branch starts with `upgrade/lodash-` for the minor-bump test.

**Step 1: Pre-run GitHub sweep fixture in `conftest.py`**

```python
@pytest.fixture(scope="session", autouse=True)
def _github_target_pre_run_sweep(e2e_github_repo):
    """Close any leftover upgrade PRs at session start.

    The upgrade-docs worker creates branches matching ^upgrade/ — that
    prefix is the sweep filter. Production never targets the e2e repo
    (parameterized via _UPGRADE_TARGET_REPO), so this filter is safe.
    """
    try:
        from tests.e2e._github_helpers import sweep_upgrade_prs
    except ImportError:
        return
    sweep_upgrade_prs(e2e_github_repo)
    yield
```

**Step 2: Write `tests/e2e/test_upgrade_workload.py`**

```python
"""E2E: upgrade workload — observe via GitHub branch + title pattern."""
import time

import pytest

from tests.e2e._github_helpers import (
    count_open_upgrade_prs,
    github_cleanup_pr,
    list_open_upgrade_prs,
)


@pytest.mark.e2e
def test_upgrade_reader_chat_returns_reply(coordinator_client):
    """Read-only: /chat workload=upgrade returns a non-empty reply + tool_calls."""
    resp = coordinator_client.post(
        "/chat",
        json={"workload": "upgrade", "prompt": "What advisories exist for lodash?"},
        timeout=180.0,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("reply"), "expected non-empty reply"
    assert isinstance(body.get("tool_calls"), list)


@pytest.mark.e2e
def test_minor_bump_opens_real_pr(coordinator_client, e2e_github_repo):
    """Minor bump (4.17.20 → 4.17.21) → PR with branch upgrade/lodash-4-17-21 appears."""
    pre_count = count_open_upgrade_prs(e2e_github_repo)

    resp = coordinator_client.post(
        "/chat",
        json={
            "workload": "upgrade",
            "prompt": (
                "please open a PR bumping lodash to the latest patch version "
                "per the advisory. CONFIRM_UPGRADE_PR=1."
            ),
        },
        timeout=300.0,
    )
    assert resp.status_code == 200, resp.text

    deadline = time.monotonic() + 60.0
    new_pr = None
    while time.monotonic() < deadline:
        for pr in list_open_upgrade_prs(e2e_github_repo):
            if pr.head.ref.startswith("upgrade/lodash-"):
                new_pr = pr
                break
        if new_pr:
            break
        time.sleep(3.0)

    assert new_pr is not None, (
        f"expected lodash upgrade PR in {e2e_github_repo}; "
        f"pre_count={pre_count}, post_count={count_open_upgrade_prs(e2e_github_repo)}"
    )
    # Title contract check.
    assert new_pr.title.startswith("upgrade(lodash):"), \
        f"unexpected title: {new_pr.title!r}"

    github_cleanup_pr(f"https://github.com/{e2e_github_repo}/pull/{new_pr.number}")


@pytest.mark.e2e
def test_major_bump_refused_no_pr_opened(coordinator_client, e2e_github_repo):
    """Major bump (→5.0.0) → validator refuses → NO upgrade/lodash- PR."""
    pre_count = count_open_upgrade_prs(e2e_github_repo)
    resp = coordinator_client.post(
        "/chat",
        json={
            "workload": "upgrade",
            "prompt": (
                "please open a PR bumping lodash to 5.0.0 (major release). "
                "CONFIRM_UPGRADE_PR=1."
            ),
        },
        timeout=180.0,
    )
    assert resp.status_code == 200, resp.text

    time.sleep(30.0)
    post_count = count_open_upgrade_prs(e2e_github_repo)
    assert post_count == pre_count, (
        f"validator did not block major bump: upgrade PR count {pre_count} -> {post_count}"
    )
```

**Step 3: Implement `tests/e2e/_github_helpers.py`**

```python
"""GitHub helpers for E2E. PR-matching keyed off branch prefix 'upgrade/'."""
import os
import re

from github import Github


def _github_client():
    token = os.environ.get("DRIFTSCRIBE_E2E_GITHUB_TOKEN")
    if not token:
        raise RuntimeError("DRIFTSCRIBE_E2E_GITHUB_TOKEN required for GitHub E2E")
    return Github(token)


def _is_upgrade_pr(pr) -> bool:
    """True if this PR's branch matches the worker's stable prefix."""
    return pr.head.ref.startswith("upgrade/")


def list_open_upgrade_prs(repo: str):
    repo_obj = _github_client().get_repo(repo)
    return [pr for pr in repo_obj.get_pulls(state="open") if _is_upgrade_pr(pr)]


def count_open_upgrade_prs(repo: str) -> int:
    return len(list_open_upgrade_prs(repo))


def sweep_upgrade_prs(repo: str) -> None:
    """Close every open PR whose branch starts with 'upgrade/' and delete the branch."""
    repo_obj = _github_client().get_repo(repo)
    for pr in repo_obj.get_pulls(state="open"):
        if not _is_upgrade_pr(pr):
            continue
        branch = pr.head.ref
        try:
            pr.edit(state="closed")
        except Exception:
            pass
        try:
            repo_obj.get_git_ref(f"heads/{branch}").delete()
        except Exception:
            pass


def github_cleanup_pr(pr_url: str) -> None:
    match = re.match(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", pr_url)
    if not match:
        return
    repo_name, pr_num = match.group(1), int(match.group(2))
    repo = _github_client().get_repo(repo_name)
    pr = repo.get_pull(pr_num)
    branch = pr.head.ref
    try:
        pr.edit(state="closed")
    except Exception:
        pass
    try:
        repo.get_git_ref(f"heads/{branch}").delete()
    except Exception:
        pass
```

**Step 4: Run E2E + commit**

```bash
export DRIFTSCRIBE_E2E_GITHUB_TOKEN=$(gcloud secrets versions access latest \
  --secret=upgrade-docs-github-pat --project driftscribe-e2e)

pytest tests/e2e/test_upgrade_workload.py -m e2e -v
git add tests/e2e/test_upgrade_workload.py tests/e2e/_github_helpers.py tests/e2e/conftest.py
git commit -m "feat(e2e): upgrade workload E2E via GitHub branch observation (20.5)"
```

---

## Task 20.6.0: Audit + add stable `data-testid` selectors to transparency.html (Playwright precondition)

**Files:**
- Modify: `agent/templates/transparency.html`
- Modify: `agent/templates/approval.html`
- Create: `tests/unit/test_transparency_template_testids.py`

**Required test IDs in `transparency.html`:**
- `chat-prompt` — the prompt input field (currently `<input name="prompt">`)
- `chat-submit` — the Send button (currently `<button id="send-btn">`)
- `final-response` — the rendered final assistant reply card
- `past-decisions-pane` — the historical decisions list container
- `past-decision-item` — each `<li.decision-row>` in the list
- `open-trace-button` — each `.open-trace-btn` (the explicit click target for historical mode — clicking the `<li>` *may* also open trace per CSS comment, but the button is the stable selector)
- `historical-banner` — the banner shown in historical mode (the page applies the `.historical` class on the body/main container; the banner element gets the test id)

**Required test IDs in `approval.html`:**
- `approve-button`, `reject-button`, `token-field`

**Do not change** the existing `data-group="coordinator|tools|mcp"` attributes from Phase 19.B.

**Step 1: Write the failing meta-test**

```python
"""Phase 20.6.0: stable data-testid selectors required for Playwright UI E2E."""
from pathlib import Path

REQUIRED_TESTIDS_TRANSPARENCY = {
    "chat-prompt",
    "chat-submit",
    "final-response",
    "past-decisions-pane",
    "past-decision-item",
    "open-trace-button",
    "historical-banner",
}


def test_transparency_template_has_required_testids():
    body = Path("agent/templates/transparency.html").read_text()
    missing = [tid for tid in REQUIRED_TESTIDS_TRANSPARENCY
               if f'data-testid="{tid}"' not in body]
    assert not missing, f"missing data-testids: {missing}"


def test_approval_template_has_testids():
    body = Path("agent/templates/approval.html").read_text()
    for tid in ("approve-button", "reject-button", "token-field"):
        assert f'data-testid="{tid}"' in body, f"approval.html missing data-testid={tid!r}"


def test_data_group_unchanged():
    body = Path("agent/templates/transparency.html").read_text()
    for group in ("coordinator", "tools", "mcp"):
        assert f'data-group="{group}"' in body


def test_sessionstorage_key_documented():
    """Phase 20 reminder: Playwright will set sessionStorage['driftscribe_token']."""
    body = Path("agent/templates/transparency.html").read_text()
    assert 'driftscribe_token' in body  # underscore, NOT dot
```

**Step 2: Add the attributes (do NOT change layout/styles)**

Two non-obvious spots:
- The historical banner: the page already applies a `.historical` class on a top-level container when in historical mode (search for `.historical` in the file). Pick that element (or the visible banner text within it) and add `data-testid="historical-banner"`.
- The past-decision-item ↔ open-trace-button relationship: `data-testid="past-decision-item"` goes on the `<li.decision-row>`; `data-testid="open-trace-button"` goes on its `.open-trace-btn` child. Playwright tests should target the button — clicking the row is a UX nicety but the button is the canonical hook.

**Step 3: Run tests — expect PASS**

```bash
pytest tests/unit/test_transparency_template_testids.py -v
```

**Step 4: Operator-side visual check** — load `/ui/transparency` and `/approvals/<id>?t=...`; confirm no visual change.

**Step 5: Commit**

```bash
git add agent/templates/transparency.html agent/templates/approval.html \
        tests/unit/test_transparency_template_testids.py
git commit -m "feat(ui): data-testid selectors for Playwright E2E (20.6.0)"
```

---

## Task 20.6: Transparency UI E2E (Playwright) — correct sessionStorage key + open-trace-button

**Files:**
- Create: `tests/e2e/ui/package.json`
- Create: `tests/e2e/ui/playwright.config.ts`
- Create: `tests/e2e/ui/tests/transparency.spec.ts`
- Create: `tests/e2e/ui/README.md`
- Create: `tests/unit/test_playwright_config.py`

**Step 1: Write the failing meta-test**

```python
from pathlib import Path


def test_playwright_config_exists():
    assert Path("tests/e2e/ui/playwright.config.ts").exists()


def test_playwright_targets_chromium_only():
    body = Path("tests/e2e/ui/playwright.config.ts").read_text()
    assert "chromium" in body
    assert "webkit" not in body
    assert "firefox" not in body


def test_transparency_spec_exists():
    assert Path("tests/e2e/ui/tests/transparency.spec.ts").exists()


def test_transparency_spec_uses_correct_sessionstorage_key():
    """Phase 20 fix: key is driftscribe_token (underscore), not driftscribe.token."""
    body = Path("tests/e2e/ui/tests/transparency.spec.ts").read_text()
    assert "driftscribe_token" in body
    assert "driftscribe.token" not in body


def test_transparency_spec_does_not_use_old_auth_header():
    body = Path("tests/e2e/ui/tests/transparency.spec.ts").read_text()
    assert "X-Operator-Token" not in body


def test_transparency_spec_uses_data_testid_selectors():
    body = Path("tests/e2e/ui/tests/transparency.spec.ts").read_text()
    for tid in ("chat-prompt", "chat-submit", "final-response",
                "past-decisions-pane", "past-decision-item",
                "open-trace-button", "historical-banner"):
        assert f'data-testid="{tid}"' in body, f"spec missing selector for {tid}"
```

**Step 2: `tests/e2e/ui/package.json`**

```json
{
  "name": "driftscribe-ui-e2e",
  "private": true,
  "scripts": {
    "test": "playwright test",
    "test:headed": "playwright test --headed"
  },
  "devDependencies": {
    "@playwright/test": "^1.49.0",
    "typescript": "^5.6.0"
  }
}
```

**Step 3: `tests/e2e/ui/playwright.config.ts`**

```typescript
import { defineConfig, devices } from '@playwright/test';

const baseURL = process.env.DRIFTSCRIBE_E2E_URL;
if (!baseURL) {
  console.warn('DRIFTSCRIBE_E2E_URL not set; UI tests will fail.');
}

export default defineConfig({
  testDir: './tests',
  timeout: 90_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  retries: 1,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
});
```

**Step 4: `tests/e2e/ui/tests/transparency.spec.ts`**

```typescript
import { test, expect } from '@playwright/test';

const TOKEN = process.env.DRIFTSCRIBE_E2E_TOKEN ?? '';

test.describe('transparency UI', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/ui/transparency');
    // Phase 19.B stores the token under sessionStorage['driftscribe_token']
    // (verified agent/templates/transparency.html:609). NOT driftscribe.token.
    await page.evaluate((t) => {
      sessionStorage.setItem('driftscribe_token', t);
    }, TOKEN);
    await page.reload();
  });

  test('renders three reasoning groups after /chat fires', async ({ page }) => {
    await page.locator('[data-testid="chat-prompt"]').fill('Check payment-demo-e2e for drift');
    await page.locator('[data-testid="chat-submit"]').click();

    // Phase 19.B data-group attrs (unchanged in 20.6.0).
    await expect(page.locator('[data-group="coordinator"]')).toBeVisible({ timeout: 45_000 });
    await expect(page.locator('[data-group="tools"]')).toBeVisible();
    await expect(page.locator('[data-group="mcp"]')).toBeVisible();

    await expect(page.locator('[data-testid="final-response"]')).toBeVisible({ timeout: 60_000 });
  });

  test('past-decisions pane renders with at least one item (seeded)', async ({ page, request }) => {
    // Seed a decision via /recheck so the pane is non-empty independent of
    // whether the Python E2E job ran. Avoids order-dependence between jobs.
    // NOTE: this seeded decision lands in Firestore outside the Python
    // _firestore_cleanup_tracker. For the manual-dispatch cadence this is
    // acceptable (few extra docs per run); for a future nightly cadence,
    // add a periodic sweep — see "Risks & open questions" below.
    await request.post(`${process.env.DRIFTSCRIBE_E2E_URL}/recheck`, {
      headers: { 'X-DriftScribe-Token': TOKEN, 'Content-Type': 'application/json' },
      data: { workload: 'drift' },
    });
    await page.reload();

    await expect(page.locator('[data-testid="past-decisions-pane"]')).toBeVisible();
    await expect(page.locator('[data-testid="past-decision-item"]').first())
      .toBeVisible({ timeout: 15_000 });
  });

  test('open-trace button opens historical mode', async ({ page, request }) => {
    // Seed (same reason as above — ensure ≥1 past-decision-item exists).
    await request.post(`${process.env.DRIFTSCRIBE_E2E_URL}/recheck`, {
      headers: { 'X-DriftScribe-Token': TOKEN, 'Content-Type': 'application/json' },
      data: { workload: 'drift' },
    });
    await page.reload();

    // Click the explicit button — the row itself may also be clickable, but the
    // button is the stable hook.
    await page.locator('[data-testid="open-trace-button"]').first().click();
    await expect(page.locator('[data-testid="historical-banner"]')).toBeVisible({ timeout: 10_000 });
  });
});
```

**Step 5: `tests/e2e/ui/README.md`** — install + run instructions.

**Step 6: Run meta-tests + Playwright (operator)**

```bash
pytest tests/unit/test_playwright_config.py -v
cd tests/e2e/ui && npm install && npx playwright install chromium && npm test
```

**Step 7: Commit**

```bash
git add tests/e2e/ui/ tests/unit/test_playwright_config.py
git commit -m "feat(e2e): Playwright UI E2E with correct selectors + sessionStorage key (20.6)"
```

---

## Task 20.7a: Workload Identity Federation + GitHub Environment runbook

**Files:**
- Create: `docs/runbooks/e2e-ci.md`
- Modify: `docs/runbooks/e2e-environment.md` (cross-link)

**Step 1: WIF setup in `docs/runbooks/e2e-ci.md`**

```bash
# 1. WIF pool + provider, pinned to repo + environment.
gcloud iam workload-identity-pools create gha-pool \
  --project=driftscribe-e2e --location=global

gcloud iam workload-identity-pools providers create-oidc gha-provider \
  --project=driftscribe-e2e --location=global \
  --workload-identity-pool=gha-pool \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.environment=assertion.environment" \
  --attribute-condition="assertion.repository == 'adi-prasetyo/driftscribe' && assertion.environment == 'e2e'"

# 2. Bind e2e-runner-sa via the environment principal set.
gcloud iam service-accounts add-iam-policy-binding \
  e2e-runner-sa@driftscribe-e2e.iam.gserviceaccount.com \
  --project=driftscribe-e2e \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/gha-pool/attribute.environment/e2e"

# 3. Least-privilege project + resource grants for e2e-runner-sa (set up by
#    setup_e2e_project.sh — listed here so the runbook is self-contained):
#    - roles/run.viewer  (read driftscribe-agent URL, project-wide)
#    - roles/secretmanager.secretAccessor  (BIND PER-SECRET, not project-wide):
#         coordinator-shared-token, upgrade-docs-github-pat
#    - roles/run.developer on the payment-demo-e2e service (resource-scoped —
#      required so the E2E fixture can mutate env vars):
#         gcloud run services add-iam-policy-binding payment-demo-e2e \
#           --project=driftscribe-e2e --region=asia-northeast1 \
#           --member="serviceAccount:e2e-runner-sa@driftscribe-e2e.iam.gserviceaccount.com" \
#           --role="roles/run.developer"
#    - roles/iam.serviceAccountUser on payment-demo-e2e's Cloud Run runtime SA
#      (the default compute SA <PROJECT_NUMBER>-compute@developer.gserviceaccount.com,
#      unless the deploy step specified --service-account=...). Required so the
#      runner can act-as the service identity during update_service calls.
#    - roles/datastore.user (Firestore writes for the cleanup tracker)
```

**Step 2: GitHub Environment setup**

Repo settings → Environments → new env `e2e`:
- `Required reviewers`: your own username (single-reviewer is fine for hackathon).
- Two environment secrets:
  - `GCP_WIF_PROVIDER` = `projects/<NUMBER>/locations/global/workloadIdentityPools/gha-pool/providers/gha-provider`
  - `GCP_E2E_RUNNER_SA` = `e2e-runner-sa@driftscribe-e2e.iam.gserviceaccount.com`

The attribute-condition pinning `environment=='e2e'` + the GitHub Environment's reviewer requirement means a workflow run from a feature branch cannot mint a GCP token without explicit human approval.

**Step 3: Cross-link from `e2e-environment.md`** — add a `## CI` section pointing to `e2e-ci.md`.

**Step 4: Commit**

```bash
git add docs/runbooks/e2e-ci.md docs/runbooks/e2e-environment.md
git commit -m "docs(e2e): WIF + GitHub Environment runbook (20.7a)"
```

---

## Task 20.7b: GitHub Actions E2E workflow (manual dispatch, environment-gated)

**Files:**
- Create: `.github/workflows/e2e.yml`
- Create: `tests/unit/test_e2e_workflow_file.py`

**Pre-condition:** Task 20.7a complete.

**Step 1: Failing meta-tests**

```python
from pathlib import Path
import yaml


def test_e2e_workflow_exists():
    assert Path(".github/workflows/e2e.yml").exists()


def test_e2e_workflow_is_manual_dispatch_only():
    data = yaml.safe_load(Path(".github/workflows/e2e.yml").read_text())
    triggers = data.get("on") or data.get(True)
    assert isinstance(triggers, dict)
    assert "workflow_dispatch" in triggers
    assert "push" not in triggers
    assert "pull_request" not in triggers


def test_e2e_workflow_uses_wif_not_long_lived_keys():
    body = Path(".github/workflows/e2e.yml").read_text()
    assert "workload_identity_provider" in body
    assert "credentials_json" not in body
    assert "id-token: write" in body


def test_e2e_workflow_uses_environment_gate():
    body = Path(".github/workflows/e2e.yml").read_text()
    assert "environment: e2e" in body


def test_e2e_workflow_uses_correct_secret_names():
    body = Path(".github/workflows/e2e.yml").read_text()
    assert "coordinator-shared-token" in body
    assert "upgrade-docs-github-pat" in body
    assert "OPERATOR_TOKEN" not in body
    assert "HITL_HMAC_KEY" not in body


def test_ui_job_runs_even_if_python_fails():
    body = Path(".github/workflows/e2e.yml").read_text()
    assert "needs: python-e2e" in body
    assert "if: ${{ always()" in body or "if: always()" in body
```

**Step 2: `.github/workflows/e2e.yml`**

```yaml
name: e2e

on:
  workflow_dispatch:
    inputs:
      python_e2e:
        description: "Run Python E2E tests"
        type: boolean
        default: true
      ui_e2e:
        description: "Run Playwright UI tests"
        type: boolean
        default: true

permissions:
  contents: read
  id-token: write

jobs:
  python-e2e:
    if: ${{ github.event.inputs.python_e2e == 'true' }}
    runs-on: ubuntu-latest
    environment: e2e
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - uses: google-github-actions/auth@v2
        with:
          workload_identity_provider: ${{ secrets.GCP_WIF_PROVIDER }}
          service_account: ${{ secrets.GCP_E2E_RUNNER_SA }}
      - uses: google-github-actions/setup-gcloud@v2
      - name: Install deps
        run: |
          pip install uv
          uv sync --all-extras
      - name: Resolve coordinator URL + tokens
        id: env
        run: |
          URL=$(gcloud run services describe driftscribe-agent \
            --project driftscribe-e2e --region asia-northeast1 \
            --format='value(status.url)')
          TOKEN=$(gcloud secrets versions access latest \
            --secret=coordinator-shared-token --project driftscribe-e2e)
          GH_TOKEN=$(gcloud secrets versions access latest \
            --secret=upgrade-docs-github-pat --project driftscribe-e2e)
          echo "::add-mask::$TOKEN"
          echo "::add-mask::$GH_TOKEN"
          echo "url=$URL"             >> "$GITHUB_OUTPUT"
          echo "token=$TOKEN"         >> "$GITHUB_OUTPUT"
          echo "gh_token=$GH_TOKEN"   >> "$GITHUB_OUTPUT"
      - name: Run Python E2E
        env:
          DRIFTSCRIBE_E2E_URL: ${{ steps.env.outputs.url }}
          DRIFTSCRIBE_E2E_TOKEN: ${{ steps.env.outputs.token }}
          DRIFTSCRIBE_E2E_GITHUB_TOKEN: ${{ steps.env.outputs.gh_token }}
          DRIFTSCRIBE_E2E_PROJECT: driftscribe-e2e
        run: uv run pytest tests/e2e -m e2e -v --junitxml=e2e-results.xml
      - uses: actions/upload-artifact@v4
        if: always()
        with: { name: e2e-python-results, path: e2e-results.xml }

  ui-e2e:
    if: ${{ always() && github.event.inputs.ui_e2e == 'true' }}
    needs: python-e2e
    runs-on: ubuntu-latest
    environment: e2e
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: "20" }
      - uses: actions/cache@v4
        with:
          path: ~/.cache/ms-playwright
          key: playwright-chromium-${{ runner.os }}-v1
      - uses: google-github-actions/auth@v2
        with:
          workload_identity_provider: ${{ secrets.GCP_WIF_PROVIDER }}
          service_account: ${{ secrets.GCP_E2E_RUNNER_SA }}
      - uses: google-github-actions/setup-gcloud@v2
      - name: Install Playwright
        working-directory: tests/e2e/ui
        run: |
          npm install
          npx playwright install --with-deps chromium
      - name: Resolve env
        id: env
        run: |
          URL=$(gcloud run services describe driftscribe-agent \
            --project driftscribe-e2e --region asia-northeast1 \
            --format='value(status.url)')
          TOKEN=$(gcloud secrets versions access latest \
            --secret=coordinator-shared-token --project driftscribe-e2e)
          echo "::add-mask::$TOKEN"
          echo "url=$URL"     >> "$GITHUB_OUTPUT"
          echo "token=$TOKEN" >> "$GITHUB_OUTPUT"
      - name: Run UI E2E
        working-directory: tests/e2e/ui
        env:
          DRIFTSCRIBE_E2E_URL: ${{ steps.env.outputs.url }}
          DRIFTSCRIBE_E2E_TOKEN: ${{ steps.env.outputs.token }}
        run: npm test
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: e2e-ui-results
          path: tests/e2e/ui/playwright-report/
```

**Step 3: Run meta-tests + commit**

```bash
pytest tests/unit/test_e2e_workflow_file.py -v
git add .github/workflows/e2e.yml tests/unit/test_e2e_workflow_file.py
git commit -m "feat(ci): manual-dispatch E2E workflow via WIF + Environment gate (20.7b)"
```

---

## Task 20.8: README badge + demo-script pre-recording gate + Status section

**Files:**
- Modify: `README.md` (Status section + add E2E badge)
- Modify: `README.ja.md`
- Modify: `docs/demo-script.md`
- Modify: `docs/demo-script.ja.md`

**Step 1: Add the E2E badge next to the CI badge in both READMEs**

```markdown
[![CI](https://github.com/adi-prasetyo/driftscribe/actions/workflows/ci.yml/badge.svg)](https://github.com/adi-prasetyo/driftscribe/actions/workflows/ci.yml)
[![E2E](https://github.com/adi-prasetyo/driftscribe/actions/workflows/e2e.yml/badge.svg?event=workflow_dispatch)](https://github.com/adi-prasetyo/driftscribe/actions/workflows/e2e.yml)
```

**Step 2: Update `README.md` Status section**

```markdown
Phase 20 (assertive E2E suite — drift via `/recheck`, upgrade via GitHub branch
observation, HITL form-POST flow with explicit revision capture, Playwright UI
on stable `data-testid` selectors — running in a dedicated `driftscribe-e2e`
GCP project under WIF + Required-reviewer gate) complete on top of Phase 19.B
(transparency UI), Phase 18.A (365-day logging), and Phase 17 (multi-agent
framework). Hackathon submission deadline 2026-07-10. Latest implementation
plan: [`docs/plans/2026-05-24-driftscribe-phase20-e2e-testing.md`](docs/plans/2026-05-24-driftscribe-phase20-e2e-testing.md).

E2E runbooks: [`docs/runbooks/e2e-environment.md`](docs/runbooks/e2e-environment.md)
(project + secrets + cloudbuild) and [`docs/runbooks/e2e-ci.md`](docs/runbooks/e2e-ci.md)
(WIF + GitHub Environment).
```

**Step 3: Mirror in `README.ja.md`** (translate).

**Step 4: Add "Before recording" callout to `docs/demo-script.md`** (top of file, after prerequisites)

```markdown
> **Before recording:** trigger the manual-dispatch E2E workflow and wait for green.
> `gh workflow run e2e.yml` then `gh run watch`. Requires reviewer approval on
> the `e2e` GitHub Environment. The E2E run is the fail-fast signal that the
> demo path is intact — running it before each recording catches IAM / MCP /
> worker-boundary regressions that would otherwise surface on camera.
```

**Step 5: Mirror in `docs/demo-script.ja.md`**.

**Step 6: Commit**

```bash
git add README.md README.ja.md docs/demo-script.md docs/demo-script.ja.md
git commit -m "docs: Phase 20 E2E badge + pre-recording gate + status (20.8)"
```

---

## Verification (after all tasks)

1. **Unit + integration + worker tests still green:**
   `pytest tests/unit tests/integration workers -v` → 586+ tests pass.

2. **E2E suite runs end-to-end against `driftscribe-e2e`:**
   ```bash
   export DRIFTSCRIBE_E2E_URL=...
   export DRIFTSCRIBE_E2E_TOKEN=...    # from coordinator-shared-token
   export DRIFTSCRIBE_E2E_PROJECT=driftscribe-e2e
   export DRIFTSCRIBE_E2E_GITHUB_TOKEN=...   # from upgrade-docs-github-pat

   pytest tests/e2e -m e2e -v
   cd tests/e2e/ui && npm test
   ```

3. **Manual GitHub Actions dispatch is green** (approve the `e2e` Environment): `gh workflow run e2e.yml`.

4. **`driftscribe-e2e-target` repo is clean** post-run (session-scoped sweep + per-test cleanup).

5. **README E2E badge** green on github.com.

---

## Risks & open questions

- **Cloud Logging tail vs `_STABILITY_GRACE_S=30s`.** `wait_for_trace_complete` allows 120s by default. If flaky, raise the in-coordinator grace as a separate Phase 19 follow-up.
- **Cost monitoring.** $300 coupon comfortably covers daily runs through 2026-07-10. Add billing alerts at $50 / $100 / $200.
- **Baseline restore failure path.** The session-scoped autouse fixture can itself raise. Runbook documents a manual "reset to baseline" command.
- **HITL revision capture timing.** The fixture reads the baseline revision from `read_live_state` at session start. If the operator redeploys `payment-demo-e2e` mid-session, the captured revision goes stale. Re-run the session from scratch after redeploys.
- **Major-bump test relies on validator behavior.** If the LLM happens to refuse the major bump on its own (without invoking the validator), the test still passes via the same observable outcome (no PR). That's intentional — we're asserting on the safety property, not the path.
- **Playwright cold-start in CI.** `actions/cache@v4` step keeps `~/.cache/ms-playwright` warm.
- **Single-reviewer Environment.** Sufficient for a solo hackathon; expand if more operators join.
- **Playwright-seeded Firestore decisions.** The two UI tests seed decisions via `/recheck` and those docs land outside the Python `_firestore_cleanup_tracker`. Fine for manual-dispatch cadence (≤2 extra docs/run). If the workflow becomes nightly, add a periodic Firestore sweep job (e.g., a separate scheduled Cloud Run job that deletes E2E-tagged decisions older than 24h) or a Node-side cleanup hook in the Playwright global teardown.

---

## Execution handoff

Plan complete and saved. Two execution options:

1. **Subagent-Driven (this session)** — fresh subagent per task with two-stage review (spec then quality).
2. **Parallel Session (separate)** — open new session using `superpowers:executing-plans`.

Recommend **Subagent-Driven** — same pattern that worked for Phase 19.
