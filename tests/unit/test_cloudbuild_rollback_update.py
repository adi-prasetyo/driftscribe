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

WHY THE STRUCTURE IS PINNED BY SHAPE, NOT BY SUBSTRING SEARCH: an earlier
cut scanned step text for banned flags, and a step of the plain form
``args: [run, services, update, driftscribe-rollback, --concurrency=80]``
slipped through every scan because no single argument contained both the
service name and the command. Substring bans are an open-ended arms race;
pinning the exact five-step shape closes it, and a deliberate sixth step
then has to come with a deliberate test change.
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
_CALLERS = f"driftscribe-agent@{_PROJECT}.iam.gserviceaccount.com"
_REV = "driftscribe-rollback-00005-abc"

_SDK = "gcr.io/google.com/cloudsdktool/cloud-sdk"
_DOCKER = "gcr.io/cloud-builders/docker"


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
                                {"name": "ALLOWED_CALLERS", "value": _CALLERS},
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
            "latestReadyRevisionName": _REV,
            "latestCreatedRevisionName": _REV,
            "traffic": [
                {"latestRevision": True, "percent": 100, "revisionName": _REV}
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


def _set_env(doc: dict, name: str, value) -> dict:
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
# check_service — happy path
# --------------------------------------------------------------------------


@pytest.mark.parametrize("phase", ["preflight", "postcondition"])
def test_healthy_service_passes(phase):
    assert _check(_doc(), phase=phase) == []


# --------------------------------------------------------------------------
# check_service — the regression this config exists to prevent
# --------------------------------------------------------------------------


def test_coordinator_url_repointed_at_runapp_is_caught():
    """Note the name is still present and non-empty — a substring check
    would pass this."""
    doc = _set_env(
        _doc(), "COORDINATOR_URL", "https://driftscribe-agent-u272wv52kq-an.a.run.app"
    )
    assert any("COORDINATOR_URL" in f and "run.app" in f for f in _check(doc))


def test_renamed_coordinator_url_is_caught():
    doc = _drop_env(_doc(), "COORDINATOR_URL")
    doc["spec"]["template"]["spec"]["containers"][0]["env"].append(
        {"name": "OLD_COORDINATOR_URL", "value": _COORD}
    )
    assert any("COORDINATOR_URL missing" in f for f in _check(doc))


# --------------------------------------------------------------------------
# check_service — absence must never be a pass
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", _mod.REQUIRED_ENV)
def test_each_required_env_var_is_required(name):
    assert any(f.startswith(f"{name} missing") for f in _check(_drop_env(_doc(), name)))


@pytest.mark.parametrize("name", _mod.REQUIRED_ENV)
@pytest.mark.parametrize("bad", [None, "", "   "])
def test_null_or_empty_env_value_is_not_a_pass(name, bad):
    """A guard of the form `if value is not None` let a null through and
    called it verified."""
    failures = _check(_set_env(_doc(), name, bad))
    assert any("null or empty" in f and name in f for f in failures), failures


def test_missing_status_block_fails_rather_than_abstains():
    doc = _doc()
    doc["status"] = {}
    failures = _check(doc, phase="postcondition")
    assert any("status.url is missing" in f for f in failures), failures
    assert any("latestReadyRevisionName is missing" in f for f in failures), failures


def test_postcondition_requires_a_latest_ready_revision():
    doc = _doc()
    doc["status"].pop("latestReadyRevisionName")
    assert any("cannot prove the new revision is serving" in f for f in _check(doc))


def test_postcondition_rejects_a_tagged_zero_percent_entry():
    """Membership in status.traffic is not enough — a tagged entry can sit
    at 0% while an older revision serves."""
    doc = _doc()
    doc["status"]["traffic"] = [
        {"revisionName": _REV, "percent": 0, "tag": "new"},
        {"revisionName": "driftscribe-rollback-00004-rnn", "percent": 100},
    ]
    assert any("is not serving 100%" in f for f in _check(doc))


def test_postcondition_catches_a_new_revision_that_never_became_ready():
    doc = _doc()
    doc["status"]["latestCreatedRevisionName"] = "driftscribe-rollback-00006-new"
    assert any("did not come up" in f for f in _check(doc))


@pytest.mark.parametrize("bad", [None, "", "   "])
def test_postcondition_requires_latest_created_rather_than_comparing_it_if_present(bad):
    """A comparison guarded by `if _nonempty_str(latest_created)` abstains on
    an absent field and reports success — the same defect as the null-value
    guards, one level deeper."""
    doc = _doc()
    if bad is None:
        doc["status"].pop("latestCreatedRevisionName")
    else:
        doc["status"]["latestCreatedRevisionName"] = bad
    failures = _check(doc)
    assert any("latestCreatedRevisionName is missing" in f for f in failures), failures


# --------------------------------------------------------------------------
# check_service — remaining invariants
# --------------------------------------------------------------------------


def test_hmac_key_as_literal_is_caught():
    assert any("LITERAL" in f for f in _check(_set_env(_doc(), "APPROVAL_HMAC_KEY", "hunter2")))


def test_hmac_key_missing_is_caught():
    assert any("secretKeyRef" in f for f in _check(_drop_env(_doc(), "APPROVAL_HMAC_KEY")))


def test_hmac_key_pointing_at_the_wrong_secret_is_caught():
    doc = _doc()
    for entry in doc["spec"]["template"]["spec"]["containers"][0]["env"]:
        if entry["name"] == "APPROVAL_HMAC_KEY":
            entry["valueFrom"]["secretKeyRef"]["name"] = "some-other-secret"
    assert any("secretKeyRef" in f for f in _check(doc))


@pytest.mark.parametrize("bad", ["", "unexpected@example.com"])
def test_allowed_callers_is_compared_exactly(bad):
    """Presence-only would accept an empty or foreign caller allowlist —
    a live auth misconfiguration."""
    assert _check(_set_env(_doc(), "ALLOWED_CALLERS", bad)) != []


def test_pinned_traffic_is_caught():
    doc = _doc()
    doc["spec"]["traffic"] = [
        {"revisionName": "driftscribe-rollback-00004-rnn", "percent": 100}
    ]
    assert any("latestRevision=100%" in f for f in _check(doc))


def test_own_url_placeholder_is_caught():
    doc = _set_env(_doc(), "OWN_URL", "https://placeholder.invalid")
    assert any("placeholder" in f for f in _check(doc))


def test_own_url_pointing_at_another_service_is_caught():
    doc = _set_env(_doc(), "OWN_URL", "https://driftscribe-agent-u272wv52kq-an.a.run.app")
    assert any("but the service serves" in f for f in _check(doc))


def test_wrong_target_service_is_caught():
    assert any("TARGET_SERVICE" in f for f in _check(_set_env(_doc(), "TARGET_SERVICE", "prod-api")))


def test_postcondition_requires_the_deployed_tag():
    """Preflight sees the OLD image and must not care; postcondition must."""
    assert _check(_doc(), phase="preflight", tag="newtag") == []
    assert any("does not carry tag" in f for f in _check(_doc(), phase="postcondition", tag="newtag"))


def test_malformed_document_raises_rather_than_passing():
    with pytest.raises(_mod.AssertionFailure):
        _check({"spec": {}})


def test_malformed_env_entry_raises_rather_than_passing():
    doc = _doc()
    doc["spec"]["template"]["spec"]["containers"][0]["env"].append({"value": "orphan"})
    with pytest.raises(_mod.AssertionFailure):
        _check(doc)


# --------------------------------------------------------------------------
# config structure — pinned by exact shape
# --------------------------------------------------------------------------


def _config() -> dict:
    return yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))


def _deploy_step(doc: dict) -> dict:
    steps = [
        s
        for s in doc["steps"]
        if s.get("entrypoint") == "gcloud" and "deploy" in (s.get("args") or [])
    ]
    assert len(steps) == 1, f"expected exactly one gcloud deploy step, got {len(steps)}"
    return steps[0]


def test_config_has_exactly_the_five_expected_steps():
    """Pinning the shape is what closes the `args: [run, services, update,
    driftscribe-rollback, ...]` bypass — a sixth step fails here regardless
    of how its command is spelled."""
    expected = [
        (_SDK, "bash"),  # preflight
        (_DOCKER, None),  # build
        (_DOCKER, None),  # push
        (_SDK, "gcloud"),  # image-only deploy
        (_SDK, "bash"),  # postcondition
    ]
    actual = [(s.get("name"), s.get("entrypoint")) for s in _config()["steps"]]
    assert actual == expected


#: Cloud Build fields that change whether/when a step's failure counts.
#: `allowFailure: true` on the preflight, or `allowExitCodes: [1]` on the
#: postcondition, turns either assertion into decoration without touching a
#: single flag or step shape. `waitFor` lets the deploy race the build/push.
_EXECUTION_CONTROL_FIELDS = ("allowFailure", "allowExitCodes", "waitFor")

#: The ONLY logical commands either bash body may run. Pinning the bodies is
#: what stops `gcloud run services update driftscribe-rollback --concurrency=80`
#: being appended to the postcondition: it keeps the five-step shape and uses
#: no banned flag, so every structural and flag pin passed it.
_ALLOWED_BASH_COMMANDS = (
    "set -euo pipefail",
    "gcloud run services describe driftscribe-rollback",
    "python3 infra/scripts/assert_rollback_service.py",
)


def _bash_bodies(doc: dict) -> list[str]:
    return [
        s["args"][-1]
        for s in doc["steps"]
        if s.get("entrypoint") == "bash" and s.get("args")
    ]


def _logical_commands(body: str) -> list[str]:
    """Split a bash body into logical commands: join backslash continuations,
    drop comments and blanks."""
    joined, buf = [], ""
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            buf += line[:-1].strip() + " "
            continue
        joined.append((buf + line).strip())
        buf = ""
    if buf.strip():
        joined.append(buf.strip())
    return joined


@pytest.mark.parametrize("field", _EXECUTION_CONTROL_FIELDS)
def test_no_step_relaxes_failure_or_ordering(field):
    offenders = [s for s in _config()["steps"] if field in s]
    assert offenders == [], f"{field} would neuter an assertion or race the build"


def test_bash_bodies_run_only_the_allowed_commands():
    bodies = _bash_bodies(_config())
    assert len(bodies) == 2, f"expected preflight + postcondition, got {len(bodies)}"
    for body in bodies:
        for command in _logical_commands(body):
            assert command.startswith(_ALLOWED_BASH_COMMANDS), (
                f"unexpected command in a bash step: {command!r}"
            )


def test_each_bash_body_makes_exactly_one_gcloud_call_and_it_is_read_only():
    for body in _bash_bodies(_config()):
        calls = [c for c in _logical_commands(body) if c.startswith("gcloud")]
        assert len(calls) == 1, f"expected one gcloud call per bash step, got {calls}"
        assert calls[0].startswith("gcloud run services describe")


def test_no_step_uses_the_script_field():
    """Cloud Build's `script:` is an alternative to `args:` that every
    args-based scan would miss entirely."""
    assert [s for s in _config()["steps"] if "script" in s] == []


def test_the_only_gcloud_entrypoint_step_is_the_deploy():
    for step in _config()["steps"]:
        if step.get("entrypoint") == "gcloud":
            assert step["args"][:3] == ["run", "deploy", _SERVICE]


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
        "--remove-secrets",
        "--clear-secrets",
        "--service-account",
    ],
)
def test_no_step_anywhere_mutates_env_or_identity(flag):
    """Belt to the shape pin's braces. Scans inline bash bodies too."""
    text = "\n".join(
        a
        for step in _config()["steps"]
        for a in (step.get("args") or [])
        if isinstance(a, str)
    )
    assert flag not in text


def test_no_step_assigns_the_coordinator_url():
    body = _CONFIG.read_text(encoding="utf-8")
    offenders = [
        line
        for line in body.splitlines()
        if not line.lstrip().startswith("#")
        and "COORDINATOR_URL=" in line
        # the substitution DECLARATION and the two assertion invocations pass
        # it as an EXPECTED value; only an assignment onto the service is a bug
        and "_EXPECT_COORDINATOR_URL" not in line
    ]
    assert offenders == [], f"config assigns COORDINATOR_URL: {offenders}"


def test_both_assertion_phases_run():
    text = "\n".join(
        a
        for step in _config()["steps"]
        for a in (step.get("args") or [])
        if isinstance(a, str)
    )
    assert "assert_rollback_service.py" in text
    for phase in ("preflight", "postcondition"):
        assert phase in text, phase
    assert _SCRIPT.exists()


def test_preflight_runs_before_anything_is_built():
    first = _config()["steps"][0]
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
