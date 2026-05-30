"""Guards on the tofu-apply (sole-mutator) Cloud Build deploy config.

`infra/cloudbuild.tofu-apply.yaml` deploys the ONE service that mutates live
infrastructure. These tests pin the security-load-bearing deploy flags so a
future edit can't silently weaken them:

* `--ingress=internal` — the worker must never be publicly reachable (the
  coordinator reaches it over the C5c VPC). `gcloud run deploy` is not guaranteed
  to preserve the live ingress, so the flag is pinned in the config (Phase C5f /
  Codex IMPORTANT-3).
* `--no-allow-unauthenticated` — invoker IAM, not anonymous.
* CF Access + enforce mode — the C5b-2 operator-JWT re-verify env.
* `PLAN_APPROVALS_DB=${_PLAN_APPROVALS_DB}` — the C5f named-DB isolation env,
  threaded from a substitution (default `plan-approvals`).

Assertions operate on the YAML-parsed structure (step args / substitutions),
not the raw text, so explanatory comments can't trip the guards.
"""
from pathlib import Path

import yaml

CONFIG = Path("infra/cloudbuild.tofu-apply.yaml")


def _load() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def _deploy_args() -> list[str]:
    """The args list of the `gcloud run deploy driftscribe-tofu-apply` step."""
    data = _load()
    for step in data.get("steps", []):
        args = step.get("args", [])
        if isinstance(args, list) and "deploy" in args and "driftscribe-tofu-apply" in args:
            return [str(a) for a in args]
    raise AssertionError("no `gcloud run deploy driftscribe-tofu-apply` step found")


def test_worker_deploy_pins_internal_ingress() -> None:
    assert "--ingress=internal" in _deploy_args()


def test_worker_deploy_is_not_public() -> None:
    assert "--no-allow-unauthenticated" in _deploy_args()


def test_worker_deploy_runs_as_tofu_apply_sa() -> None:
    args = _deploy_args()
    assert any(
        a.startswith("--service-account=tofu-apply-sa@") for a in args
    ), args


def test_plan_approvals_db_substitution_default() -> None:
    """The named-DB substitution exists and defaults to the prod named DB."""
    subs = _load().get("substitutions", {})
    assert subs.get("_PLAN_APPROVALS_DB") == "plan-approvals"


def test_plan_approvals_db_threaded_into_env() -> None:
    """PLAN_APPROVALS_DB is wired into --set-env-vars from the substitution, and
    the C5b-2 CF-Access/enforce env is still present alongside it."""
    env_arg = next(
        (a for a in _deploy_args() if a.startswith("--set-env-vars=")), None
    )
    assert env_arg is not None
    assert "PLAN_APPROVALS_DB=${_PLAN_APPROVALS_DB}" in env_arg
    assert "IAC_OPERATOR_AUTH_MODE=enforce" in env_arg
    assert "CF_ACCESS_TEAM_DOMAIN=" in env_arg
    assert "CF_ACCESS_AUD_TAG=" in env_arg


def test_plan_hmac_secret_still_mounted() -> None:
    assert any(
        a.startswith("--set-secrets=") and "PLAN_APPROVAL_HMAC_KEY=plan-hmac-key" in a
        for a in _deploy_args()
    )
