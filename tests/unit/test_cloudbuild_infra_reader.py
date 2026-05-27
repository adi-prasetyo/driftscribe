"""Guards on the targeted infra-reader Cloud Build config.

`infra/cloudbuild.infra-reader.yaml` exists precisely so an operator can
redeploy the Phase B infra-reader WITHOUT running the full-stack
`infra/cloudbuild.yaml` (which would redeploy payment-demo and break the
Phase A OpenTofu zero-diff). These tests pin that scope boundary so a future
edit can't silently turn the targeted config back into a payment-demo-touching
build.

The assertions operate on the YAML-*parsed* structure (step args, images,
substitution values) rather than the raw file text, so the explanatory comments
in the config — which deliberately mention payment-demo / $COMMIT_SHA to explain
why they are avoided — don't trip the guards. What matters is what the build
actually *executes*, not what its comments say.
"""
from pathlib import Path

import yaml

CONFIG = Path("infra/cloudbuild.infra-reader.yaml")


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
    # nor any other worker service.
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
    ):
        assert other not in text, other


def test_does_not_rebuild_the_coordinator_image():
    # Scope boundary: this config wires INFRA_READER_URL onto the running
    # coordinator but must NOT rebuild the coordinator image (that is owned by
    # cloudbuild.coordinator-update.yaml). So no Dockerfile.agent build step.
    assert "Dockerfile.agent" not in _executable_text()


def test_only_pushes_the_infra_reader_image():
    images = _load().get("images", [])
    assert len(images) == 1
    assert images[0].endswith("driftscribe-infra-reader:${_TAG}")


def test_deploys_infra_reader_with_readonly_sa_and_no_public_ingress():
    text = _executable_text()
    assert "driftscribe-infra-reader" in text
    assert "--no-allow-unauthenticated" in text
    assert "--service-account=infra-reader-sa@$PROJECT_ID.iam.gserviceaccount.com" in text


def test_wires_infra_reader_url_onto_coordinator():
    text = _executable_text()
    assert "INFRA_READER_URL" in text
    assert "gcloud run services update driftscribe-agent" in text


def test_iac_snapshot_sha_is_an_explicit_substitution():
    # Cloud Build does not recursively expand user substitutions, and a manual
    # `gcloud builds submit` has no $COMMIT_SHA — so the provenance string must
    # come from an explicit _IAC_SNAPSHOT_SHA substitution, not $COMMIT_SHA.
    assert "_IAC_SNAPSHOT_SHA" in _load().get("substitutions", {})
    text = _executable_text()
    assert "IAC_SNAPSHOT_SHA=${_IAC_SNAPSHOT_SHA}" in text
    assert "$COMMIT_SHA" not in text
