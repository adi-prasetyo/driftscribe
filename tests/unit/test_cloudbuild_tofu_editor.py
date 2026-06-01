"""Guards on the targeted tofu-editor Cloud Build config (Phase D1-5).

`infra/cloudbuild.tofu-editor.yaml` exists precisely so an operator can first-
deploy the Phase D tofu-editor WITHOUT running the full-stack
`infra/cloudbuild.yaml` (which would redeploy payment-demo and break the Phase A
OpenTofu zero-diff). These tests pin that scope boundary, plus the worker's
write-side safety wiring (--no-allow-unauthenticated, the dedicated
tofu-editor-sa, the github-pat secret mount, the env pins, the OWN_URL
write-back), so a future edit can't silently widen the build's blast radius.

The assertions operate on the YAML-*parsed* structure (step args, images,
substitution values) rather than the raw file text, so the explanatory comments
in the config — which deliberately mention payment-demo / the coordinator to
explain why they are avoided — don't trip the guards. What matters is what the
build actually *executes*, not what its comments say.
"""
from pathlib import Path

import yaml

CONFIG = Path("infra/cloudbuild.tofu-editor.yaml")


def _load() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def _executable_text() -> str:
    """All comment-free, build-effective strings: step name/entrypoint/args,
    image targets, and substitution values, joined for substring checks."""
    data = _load()
    parts: list[str] = []
    for step in data.get("steps", []):
        for key in ("name", "entrypoint"):
            if key in step:
                parts.append(str(step[key]))
        args = step.get("args", [])
        if isinstance(args, list):
            parts.extend(str(a) for a in args)
        elif args:
            parts.append(str(args))
    parts.extend(str(i) for i in data.get("images", []))
    parts.extend(str(v) for v in data.get("substitutions", {}).values())
    return "\n".join(parts)


def test_config_parses_as_yaml():
    data = _load()
    assert isinstance(data, dict)
    assert data.get("steps")


def test_does_not_touch_payment_demo_or_other_workers():
    # The whole point of this file: it must never build or deploy payment-demo
    # (directly or via the ${_TARGET_SERVICE} substitution the full build uses),
    # nor any other service.
    text = _executable_text()
    assert "payment-demo" not in text
    assert "_TARGET_SERVICE" not in text
    for other in (
        "driftscribe-reader",
        "driftscribe-docs",
        "driftscribe-rollback",
        "driftscribe-notifier",
        "driftscribe-upgrade-reader",
        "driftscribe-upgrade-docs",
        "driftscribe-infra-reader",
        "driftscribe-tofu-apply",
    ):
        assert other not in text, other


def test_does_not_rebuild_or_redeploy_the_coordinator():
    # Scope boundary: this config deploys ONLY the worker. It must NOT rebuild
    # the coordinator image (no Dockerfile.agent build step) and must NOT mutate
    # the coordinator service. (The coordinator SA email DOES legitimately appear
    # as the ALLOWED_CALLERS value — `${_ALLOWED_CALLER}@...` resolves to
    # driftscribe-agent@... — so the guard checks the *deploy/update commands*,
    # not a bare "driftscribe-agent" substring.)
    text = _executable_text()
    assert "Dockerfile.agent" not in text
    assert "deploy\ndriftscribe-agent" not in text
    assert "gcloud run services update driftscribe-agent" not in text
    assert "run deploy driftscribe-agent" not in text


def test_only_pushes_the_tofu_editor_image():
    images = _load().get("images", [])
    assert len(images) == 1
    assert images[0].endswith("driftscribe-tofu-editor:${_TAG}")


def test_deploys_tofu_editor_with_dedicated_sa_and_no_public_ingress():
    text = _executable_text()
    assert "driftscribe-tofu-editor" in text
    assert "--no-allow-unauthenticated" in text
    assert "--service-account=tofu-editor-sa@$PROJECT_ID.iam.gserviceaccount.com" in text


def test_mounts_the_github_pat_secret():
    # The worker's only GitHub authority is the write-scoped, single-repo
    # fine-grained PAT, mounted as GITHUB_TOKEN from the tofu-editor-github-pat
    # secret. It must never be passed as a plaintext env var.
    text = _executable_text()
    assert "--set-secrets=GITHUB_TOKEN=tofu-editor-github-pat:latest" in text


def test_pins_target_repo_and_allowed_caller_env():
    # IAC_EDITOR_TARGET_REPO pins the single repo; ALLOWED_CALLERS names only the
    # coordinator SA. Both flow from substitutions (parameterized, not hardcoded
    # in the deploy step).
    text = _executable_text()
    assert "IAC_EDITOR_TARGET_REPO=${_IAC_EDITOR_TARGET_REPO}" in text
    assert "ALLOWED_CALLERS=${_ALLOWED_CALLER}@$PROJECT_ID.iam.gserviceaccount.com" in text
    subs = _load().get("substitutions", {})
    assert "_IAC_EDITOR_TARGET_REPO" in subs
    assert "_ALLOWED_CALLER" in subs


def test_writes_own_url_back_after_deploy():
    # Post-deploy resolves the assigned URL and writes it back into OWN_URL so
    # verify_caller can validate the inbound ID-token audience. Fail-closed if the
    # URL can't be resolved.
    text = _executable_text()
    assert "OWN_URL=https://placeholder.invalid" in text
    assert "gcloud run services update driftscribe-tofu-editor" in text
    assert "--update-env-vars=OWN_URL=$${URL}" in text


def test_runs_as_cloudbuild_deploy_sa_with_cloud_logging_only():
    data = _load()
    assert "cloudbuild-deploy-sa@" in data.get("serviceAccount", "")
    assert data.get("options", {}).get("logging") == "CLOUD_LOGGING_ONLY"


def test_no_ci_trigger_config():
    # Operator-run first deploy, like the sibling targeted builds — a cloudbuild
    # *config* carries no trigger; a stray `trigger`/`github` block would signal
    # an accidental CI wiring.
    data = _load()
    assert "trigger" not in data
    assert "github" not in data
