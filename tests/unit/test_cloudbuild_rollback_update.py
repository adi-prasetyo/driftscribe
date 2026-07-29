"""Invariant pins for the targeted rollback-worker deploy.

Two halves:

1. ``check_service`` (infra/scripts/assert_rollback_service.py) exercised
   against crafted service documents, so the ASSERTIONS THEMSELVES are
   verified rather than trusted. The base document mirrors the live service
   read on 2026-07-29.
2. Structural pins on infra/cloudbuild.rollback-update.yaml.

WHY THE STRUCTURAL PINS ARE ASYMMETRIC WITH THE OTHER WORKER CONFIG:
cloudbuild.infra-reader.yaml MIRRORS the full build's deploy step and
test_worker_deploy_flags.py pins that parity. The rollback config
deliberately does the opposite — it is image-only — because the full
build's rollback post-deploy step re-derives COORDINATOR_URL from
driftscribe-agent's run.app URL, while prod runs
COORDINATOR_URL=https://driftscribe.adp-app.com and that value is what
every emitted operator approval link is built from. That is a subtle,
easily-"corrected" decision, which is why it is pinned here rather than
left to a header comment.
"""
import importlib.util
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = _REPO_ROOT / "infra" / "cloudbuild.rollback-update.yaml"
_SCRIPT = _REPO_ROOT / "infra" / "scripts" / "assert_rollback_service.py"
_SERVICE = "driftscribe-rollback"
_COORD = "https://driftscribe.adp-app.com"
_REGION = "asia-northeast1"
_PROJECT = "driftscribe-hack-2026"
_OWN = "https://driftscribe-rollback-u272wv52kq-an.a.run.app"


def _load_module():
    spec = importlib.util.spec_from_file_location("assert_rollback_service", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()


def _doc() -> dict:
    """A service document matching the live shape read 2026-07-29."""
    return {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "image": (
                                "asia-northeast1-docker.pkg.dev/driftscribe-hack-2026/"
                                "driftscribe/driftscribe-rollback:f72ef29"
                            ),
                            "env": [
                                {"name": "GCP_PROJECT", "value": _PROJECT},
                                {"name": "TARGET_SERVICE", "value": "payment-demo"},
                                {"name": "TARGET_REGION", "value": _REGION},
                                {"name": "OWN_URL", "value": _OWN},
                                {"name": "COORDINATOR_URL", "value": _COORD},
                                {
                                    "name": "ALLOWED_CALLERS",
                                    "value": f"driftscribe-agent@{_PROJECT}.iam.gserviceaccount.com",
                                },
                                {
                                    "name": "APPROVAL_HMAC_KEY",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "key": "latest",
                                            "name": "approval-hmac-key",
                                        }
                                    },
                                },
                            ],
                        }
                    ]
                }
            },
            "traffic": [{"latestRevision": True, "percent": 100}],
        },
        "status": {
            "url": _OWN,
            "latestReadyRevisionName": "driftscribe-rollback-00005-abc",
            "traffic": [
                {
                    "latestRevision": True,
                    "percent": 100,
                    "revisionName": "driftscribe-rollback-00005-abc",
                }
            ],
        },
    }


def _check(doc: dict, phase: str = "postcondition", tag: str = "f72ef29") -> list[str]:
    return _mod.check_service(
        doc,
        phase=phase,
        expect_coordinator_url=_COORD,
        expect_target_service="payment-demo",
        region=_REGION,
        project=_PROJECT,
        tag=tag,
    )


def _set_env(doc: dict, name: str, value: str) -> dict:
    for entry in doc["spec"]["template"]["spec"]["containers"][0]["env"]:
        if entry["name"] == name:
            entry.pop("valueFrom", None)
            entry["value"] = value
            return doc
    raise AssertionError(f"{name} not in fixture")


def _drop_env(doc: dict, name: str) -> dict:
    c = doc["spec"]["template"]["spec"]["containers"][0]
    c["env"] = [e for e in c["env"] if e["name"] != name]
    return doc


# --------------------------------------------------------------------------
# check_service
# --------------------------------------------------------------------------


@pytest.mark.parametrize("phase", ["preflight", "postcondition"])
def test_healthy_service_passes(phase):
    assert _check(_doc(), phase=phase) == []


def test_coordinator_url_repointed_at_runapp_is_caught():
    """THE regression this whole config exists to prevent. Note the name is
    still present and non-empty — a substring check would pass this."""
    doc = _set_env(
        _doc(), "COORDINATOR_URL", "https://driftscribe-agent-u272wv52kq-an.a.run.app"
    )
    failures = _check(doc)
    assert any("COORDINATOR_URL" in f and "run.app" in f for f in failures), failures


def test_renamed_coordinator_url_is_caught():
    """`OLD_COORDINATOR_URL=...` leaves the substring present but the real
    variable absent."""
    doc = _drop_env(_doc(), "COORDINATOR_URL")
    doc["spec"]["template"]["spec"]["containers"][0]["env"].append(
        {"name": "OLD_COORDINATOR_URL", "value": _COORD}
    )
    assert any("COORDINATOR_URL missing" in f for f in _check(doc))


@pytest.mark.parametrize(
    "name", ["ALLOWED_CALLERS", "GCP_PROJECT", "OWN_URL", "TARGET_REGION", "TARGET_SERVICE"]
)
def test_each_required_env_var_is_required(name):
    assert any(f.startswith(f"{name} missing") for f in _check(_drop_env(_doc(), name)))


def test_hmac_key_as_literal_is_caught():
    doc = _set_env(_doc(), "APPROVAL_HMAC_KEY", "hunter2")
    failures = _check(doc)
    assert any("LITERAL" in f for f in failures), failures


def test_hmac_key_missing_is_caught():
    assert any("secretKeyRef" in f for f in _check(_drop_env(_doc(), "APPROVAL_HMAC_KEY")))


def test_hmac_key_pointing_at_the_wrong_secret_is_caught():
    doc = _doc()
    for entry in doc["spec"]["template"]["spec"]["containers"][0]["env"]:
        if entry["name"] == "APPROVAL_HMAC_KEY":
            entry["valueFrom"]["secretKeyRef"]["name"] = "some-other-secret"
    assert any("secretKeyRef" in f for f in _check(doc))


def test_pinned_traffic_is_caught():
    """If the service is ever pinned to a named revision, a plain deploy
    creates a revision that serves 0% while the build reports success."""
    doc = _doc()
    doc["spec"]["traffic"] = [
        {"revisionName": "driftscribe-rollback-00004-rnn", "percent": 100}
    ]
    assert any("latestRevision=100%" in f for f in _check(doc))


def test_own_url_placeholder_is_caught():
    assert any(
        "placeholder" in f for f in _check(_set_env(_doc(), "OWN_URL", "https://placeholder.invalid"))
    )


def test_own_url_pointing_at_another_service_is_caught():
    doc = _set_env(_doc(), "OWN_URL", "https://driftscribe-agent-u272wv52kq-an.a.run.app")
    assert any("but the service serves" in f for f in _check(doc))


def test_wrong_target_service_is_caught():
    assert any("TARGET_SERVICE" in f for f in _check(_set_env(_doc(), "TARGET_SERVICE", "prod-api")))


def test_postcondition_requires_the_deployed_tag():
    """Preflight sees the OLD image and must not care; postcondition must."""
    doc = _doc()
    assert _check(doc, phase="preflight", tag="newtag") == []
    assert any("does not carry tag" in f for f in _check(doc, phase="postcondition", tag="newtag"))


def test_postcondition_requires_the_latest_revision_to_be_serving():
    doc = _doc()
    doc["status"]["latestReadyRevisionName"] = "driftscribe-rollback-00006-new"
    failures = _check(doc, phase="postcondition")
    assert any("is not serving" in f for f in failures), failures


def test_malformed_document_raises_rather_than_passing():
    with pytest.raises(_mod.AssertionFailure):
        _check({"spec": {}})


# --------------------------------------------------------------------------
# config structure
# --------------------------------------------------------------------------


def _config() -> dict:
    return yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))


def _all_step_text(doc: dict) -> str:
    """Every arg of every step, including inline bash bodies — so a pin cannot
    be dodged by moving the offending command into a script step."""
    return "\n".join(
        a for step in doc["steps"] for a in (step.get("args") or []) if isinstance(a, str)
    )


def _deploy_step(doc: dict) -> dict:
    for step in doc["steps"]:
        args = step.get("args") or []
        if step.get("entrypoint") == "gcloud" and "deploy" in args and _SERVICE in args:
            return step
    raise AssertionError(f"no {_SERVICE} gcloud run deploy step in {_CONFIG.name}")


def test_deploy_step_is_image_only():
    flags = {
        a.partition("=")[0]
        for a in _deploy_step(_config())["args"]
        if isinstance(a, str) and a.startswith("--")
    }
    assert flags == {"--image", "--region"}


def test_deploy_step_ships_the_tagged_rollback_image():
    image = next(
        a.partition("=")[2]
        for a in _deploy_step(_config())["args"]
        if isinstance(a, str) and a.startswith("--image=")
    )
    assert image.endswith(f"/{_SERVICE}:${{_TAG}}")


@pytest.mark.parametrize(
    "flag",
    [
        "--set-env-vars",
        "--update-env-vars",
        "--remove-env-vars",
        "--clear-env-vars",
        "--env-vars-file",
        "--set-secrets",
        "--update-secrets",
        "--clear-secrets",
        "--service-account",
    ],
)
def test_no_step_anywhere_mutates_env_or_identity(flag):
    """Scans inline bash too: a later `gcloud run services update
    --update-env-vars=...` would bypass a pin that only read the deploy step."""
    assert flag not in _all_step_text(_config())


def test_only_one_step_mutates_the_service():
    doc = _config()
    mutating = [
        step
        for step in doc["steps"]
        for a in (step.get("args") or [])
        if isinstance(a, str)
        and _SERVICE in a
        and ("run services update" in a or "run deploy" in a)
    ]
    assert mutating == [], f"extra service-mutating step(s): {mutating}"
    gcloud_deploys = [
        step
        for step in doc["steps"]
        if step.get("entrypoint") == "gcloud" and "deploy" in (step.get("args") or [])
    ]
    assert len(gcloud_deploys) == 1


def test_no_step_assigns_the_coordinator_url():
    body = _CONFIG.read_text(encoding="utf-8")
    offenders = [
        line
        for line in body.splitlines()
        if not line.lstrip().startswith("#") and "COORDINATOR_URL=" in line
        # the substitution DECLARATION and the two assertion invocations pass
        # it as an EXPECTED value; only an assignment onto the service is a bug
        and "_EXPECT_COORDINATOR_URL" not in line
    ]
    assert offenders == [], f"config assigns COORDINATOR_URL: {offenders}"


def test_both_assertion_phases_run():
    text = _all_step_text(_config())
    assert "assert_rollback_service.py" in text
    for phase in ("preflight", "postcondition"):
        assert phase in text, phase
    assert _SCRIPT.exists()


def test_preflight_runs_before_anything_is_built():
    """A create-or-update deploy against a deleted service would build a whole
    image before discovering the target is wrong."""
    doc = _config()
    first = doc["steps"][0]
    assert "preflight" in "\n".join(a for a in first["args"] if isinstance(a, str))


def test_builds_and_pushes_only_the_rollback_image():
    doc = _config()
    assert len(doc["images"]) == 1
    assert f"/{_SERVICE}:" in doc["images"][0]
    refs = [
        a
        for step in doc["steps"]
        for a in (step.get("args") or [])
        if isinstance(a, str) and "docker.pkg.dev" in a
    ]
    assert refs
    for ref in refs:
        assert f"/{_SERVICE}:" in ref, f"targeted config touches another image: {ref}"


def test_runs_as_the_dedicated_deploy_sa_with_explicit_logging():
    doc = _config()
    assert "cloudbuild-deploy-sa@" in doc["serviceAccount"]
    assert doc["options"]["logging"] == "CLOUD_LOGGING_ONLY"
