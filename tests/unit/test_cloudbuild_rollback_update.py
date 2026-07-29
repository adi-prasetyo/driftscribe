"""Invariant pins for infra/cloudbuild.rollback-update.yaml.

The other targeted worker config (cloudbuild.infra-reader.yaml) MIRRORS the
full build's deploy step, and test_worker_deploy_flags.py pins that parity.
This file pins the opposite property for the rollback worker, on purpose.

WHY THE ASYMMETRY: the full build's rollback post-deploy step re-derives
``COORDINATOR_URL`` from ``gcloud run services describe driftscribe-agent
--format='value(status.url)'`` — the *run.app* host. Prod runs
``COORDINATOR_URL=https://driftscribe.adp-app.com`` (the Cloudflare-fronted
demo domain), and that value is what every emitted approval link is built
from. Mirroring the full build would therefore silently repoint operator
approval links off the demo domain. So the targeted config is IMAGE-ONLY and
inherits every other setting from the running service.

That is a subtle, easily-"corrected" decision, which is exactly why it is
pinned here rather than left to the header comment.
"""
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = _REPO_ROOT / "infra" / "cloudbuild.rollback-update.yaml"
_SERVICE = "driftscribe-rollback"


def _doc() -> dict:
    return yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))


def _deploy_step(doc: dict) -> dict:
    for step in doc["steps"]:
        args = step.get("args") or []
        if step.get("entrypoint") == "gcloud" and "deploy" in args and _SERVICE in args:
            return step
    raise AssertionError(f"no {_SERVICE} gcloud run deploy step in {_CONFIG.name}")


def _flags(step: dict) -> dict[str, str]:
    flags: dict[str, str] = {}
    for a in step.get("args") or []:
        if isinstance(a, str) and a.startswith("--"):
            key, _, value = a.partition("=")
            flags[key] = value
    return flags


def test_deploy_step_is_image_only():
    """Only --image and --region. Any other service flag would OVERWRITE the
    live service's inherited config instead of preserving it."""
    assert set(_flags(_deploy_step(_doc()))) == {"--image", "--region"}


def test_deploy_step_never_sets_env_vars():
    """--set-env-vars REPLACES the whole env block, dropping anything set out
    of band; --update-env-vars would be needed for a genuinely new var (see
    the config header). Neither belongs on the plain image swap."""
    flags = _flags(_deploy_step(_doc()))
    assert "--set-env-vars" not in flags
    assert "--update-env-vars" not in flags
    assert "--set-secrets" not in flags


def test_no_step_rederives_the_coordinator_url():
    """The trap this file exists to prevent: resolving driftscribe-agent's
    run.app URL and writing it onto the rollback worker as COORDINATOR_URL."""
    body = _CONFIG.read_text(encoding="utf-8")
    directives = [
        line
        for line in body.splitlines()
        if not line.lstrip().startswith("#") and "COORDINATOR_URL=" in line
    ]
    assert directives == [], f"config assigns COORDINATOR_URL: {directives}"


def test_builds_and_pushes_only_the_rollback_image():
    doc = _doc()
    images = doc["images"]
    assert len(images) == 1
    assert f"/{_SERVICE}:" in images[0]
    built = [
        a
        for step in doc["steps"]
        for a in (step.get("args") or [])
        if isinstance(a, str) and "docker.pkg.dev" in a
    ]
    assert built, "no image reference found in any step"
    for ref in built:
        assert f"/{_SERVICE}:" in ref, f"targeted config touches another image: {ref}"


def test_runs_as_the_dedicated_deploy_sa_with_explicit_logging():
    """The default compute SA had roles/editor stripped (Phase 4/6); a
    user-specified serviceAccount also REQUIRES an explicit logging option."""
    doc = _doc()
    assert "cloudbuild-deploy-sa@" in doc["serviceAccount"]
    assert doc["options"]["logging"] == "CLOUD_LOGGING_ONLY"
