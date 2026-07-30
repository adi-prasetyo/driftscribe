"""Invariant pins for the targeted notifier-worker deploy.

Two halves, mirroring ``test_cloudbuild_rollback_update.py``:

1. ``check_service`` (infra/scripts/assert_notifier_service.py) exercised
   against crafted service documents, so the ASSERTIONS THEMSELVES are
   verified rather than trusted. A checker that never fails is worse than no
   checker, because it reports "all invariants hold". The base document
   mirrors the live service read on 2026-07-30.
2. Structural pins on infra/cloudbuild.notifier-update.yaml.

WHY THIS CONFIG EXISTS AT ALL: the notifier was the last worker with no
targeted deploy path — the only config that built it was the full-stack
infra/cloudbuild.yaml, which is DO-NOT-RUN on prod. So a notifier code change
had no way to ship.

THE INVARIANT THAT MATTERS MOST HERE: ``NOTIFY_WEBHOOK_URL`` must be a
``secretKeyRef``, never a literal. Knowing the URL IS this worker's whole
authorization model, so a literal puts a live credential into the service
document and into deploy logs. While the configured URL was
``httpbin.org/status/200`` that was nearly harmless; it now holds a real
Discord webhook URL, so it is a genuine credential-exposure check.
"""
import importlib.util
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = _REPO_ROOT / "infra" / "cloudbuild.notifier-update.yaml"
_SCRIPT = _REPO_ROOT / "infra" / "scripts" / "assert_notifier_service.py"
_SERVICE = "driftscribe-notifier"
_REGION = "asia-northeast1"
_PROJECT = "driftscribe-hack-2026"
_OWN = "https://driftscribe-notifier-u272wv52kq-an.a.run.app"
_CALLERS = f"driftscribe-agent@{_PROJECT}.iam.gserviceaccount.com"
_SECRET = "driftscribe-webhook-url"
_SA = f"notifier-agent-sa@{_PROJECT}.iam.gserviceaccount.com"
_REV = "driftscribe-notifier-00005-mc6"
_TAG = "346808a"

_SDK = "gcr.io/google.com/cloudsdktool/cloud-sdk"
_DOCKER = "gcr.io/cloud-builders/docker"


def _load_module():
    spec = importlib.util.spec_from_file_location("assert_notifier_service", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()


def _doc() -> dict:
    """A service document matching the live shape read 2026-07-30."""
    return {
        "spec": {
            "template": {
                "spec": {
                    "serviceAccountName": _SA,
                    "containers": [
                        {
                            "image": (
                                "asia-northeast1-docker.pkg.dev/driftscribe-hack-2026/"
                                f"driftscribe/driftscribe-notifier:{_TAG}"
                            ),
                            "env": [
                                {"name": "GCP_PROJECT", "value": _PROJECT},
                                {"name": "OWN_URL", "value": _OWN},
                                {"name": "ALLOWED_CALLERS", "value": _CALLERS},
                                {
                                    "name": "NOTIFY_WEBHOOK_URL",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "key": "latest",
                                            "name": _SECRET,
                                        }
                                    },
                                },
                            ],
                        }
                    ],
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


def _check(doc: dict, phase: str = "postcondition", tag: str = _TAG) -> list[str]:
    return _mod.check_service(
        doc,
        phase=phase,
        expect_secret=_SECRET,
        expect_service_account=_SA,
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
# check_service — the credential-exposure regression this config exists for
# --------------------------------------------------------------------------


def test_webhook_url_as_literal_is_caught():
    """A literal puts a live credential in the service doc and in build logs."""
    doc = _set_env(_doc(), "NOTIFY_WEBHOOK_URL", "https://discord.com/api/webhooks/1/x")
    failures = _check(doc)
    assert any("LITERAL" in f for f in failures)


def test_the_literal_webhook_url_value_is_never_echoed():
    """The failure message must not reproduce the credential it is reporting.

    Build logs are the exact place this checker's output lands, so a message
    that helpfully quotes the bad value would leak the URL it just objected to.
    """
    secret_url = "https://discord.com/api/webhooks/123456/super-secret-token"
    doc = _set_env(_doc(), "NOTIFY_WEBHOOK_URL", secret_url)
    joined = " ".join(_check(doc))
    assert "super-secret-token" not in joined
    assert secret_url not in joined


def test_webhook_url_missing_entirely_is_caught():
    assert _check(_drop_env(_doc(), "NOTIFY_WEBHOOK_URL")) != []


def test_webhook_url_pointing_at_the_wrong_secret_is_caught():
    doc = _doc()
    for entry in doc["spec"]["template"]["spec"]["containers"][0]["env"]:
        if entry["name"] == "NOTIFY_WEBHOOK_URL":
            entry["valueFrom"]["secretKeyRef"]["name"] = "some-other-secret"
    assert _check(doc) != []


# --------------------------------------------------------------------------
# check_service — auth + identity
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["ALLOWED_CALLERS", "GCP_PROJECT", "OWN_URL"])
def test_each_required_env_var_is_required(name):
    assert _check(_drop_env(_doc(), name)) != []


@pytest.mark.parametrize("name", ["ALLOWED_CALLERS", "GCP_PROJECT", "OWN_URL"])
@pytest.mark.parametrize("bad", [None, "", "   "])
def test_null_or_empty_env_value_is_not_a_pass(name, bad):
    """A check that cannot see its subject must FAIL, not abstain."""
    assert _check(_set_env(_doc(), name, bad)) != []


def test_a_foreign_caller_allowlist_is_caught():
    doc = _set_env(_doc(), "ALLOWED_CALLERS", "attacker@evil.example")
    assert _check(doc) != []


def test_own_url_placeholder_is_caught():
    assert _check(_set_env(_doc(), "OWN_URL", "https://placeholder.invalid")) != []


def test_own_url_pointing_at_another_service_is_caught():
    doc = _set_env(_doc(), "OWN_URL", "https://driftscribe-rollback-x-an.a.run.app")
    assert _check(doc) != []


def test_wrong_service_account_is_caught():
    """A fallback to the default compute SA would over-privilege the worker."""
    doc = _doc()
    doc["spec"]["template"]["spec"]["serviceAccountName"] = (
        f"{_PROJECT}-compute@developer.gserviceaccount.com"
    )
    assert _check(doc) != []


def test_missing_service_account_fails_rather_than_abstains():
    doc = _doc()
    del doc["spec"]["template"]["spec"]["serviceAccountName"]
    assert _check(doc) != []


# --------------------------------------------------------------------------
# check_service — traffic + serving proof
# --------------------------------------------------------------------------


def test_missing_status_block_fails_rather_than_abstains():
    doc = _doc()
    del doc["status"]
    assert _check(doc, phase="preflight") != []


def test_pinned_traffic_is_caught():
    """A pinned service would serve 0% while the build reported success."""
    doc = _doc()
    doc["spec"]["traffic"] = [{"revisionName": _REV, "percent": 100}]
    assert _check(doc) != []


@pytest.mark.parametrize("bad", ["true", 1, None])
def test_latest_revision_must_be_the_boolean_true(bad):
    doc = _doc()
    doc["spec"]["traffic"] = [{"latestRevision": bad, "percent": 100}]
    assert _check(doc) != []


def test_postcondition_requires_the_deployed_tag():
    assert _check(_doc(), tag="some-other-tag") != []


def test_postcondition_catches_a_new_revision_that_never_became_ready():
    doc = _doc()
    doc["status"]["latestCreatedRevisionName"] = "driftscribe-notifier-00006-new"
    assert _check(doc) != []


@pytest.mark.parametrize("bad", [None, "", "   "])
def test_postcondition_requires_latest_created_rather_than_skipping_it(bad):
    doc = _doc()
    doc["status"]["latestCreatedRevisionName"] = bad
    assert _check(doc) != []


def test_postcondition_rejects_a_zero_percent_entry():
    doc = _doc()
    doc["status"]["traffic"] = [
        {"latestRevision": True, "percent": 0, "revisionName": _REV}
    ]
    assert _check(doc) != []


def test_preflight_does_not_demand_a_healthy_latest_revision():
    """A broken latest revision is the state this pipeline must be able to fix."""
    doc = _doc()
    doc["status"]["latestReadyRevisionName"] = ""
    doc["status"]["latestCreatedRevisionName"] = ""
    assert _check(doc, phase="preflight") == []


def test_malformed_document_raises_rather_than_passing():
    with pytest.raises(_mod.AssertionFailure):
        _check({"spec": {}})


def test_malformed_env_entry_raises_rather_than_passing():
    doc = _doc()
    doc["spec"]["template"]["spec"]["containers"][0]["env"].append({"noname": 1})
    with pytest.raises(_mod.AssertionFailure):
        _check(doc)


# --------------------------------------------------------------------------
# Structural pins on the cloudbuild config
# --------------------------------------------------------------------------


def _config() -> dict:
    return yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))


def test_deploy_step_is_image_only():
    """--set-env-vars would REPLACE the env block and drop the secret binding.

    The worker then fails closed at boot (KeyError at import), so this is a
    total outage of the notification path, not a degradation.
    """
    doc = _config()
    deploy = [
        s
        for s in doc["steps"]
        if s.get("entrypoint") == "gcloud" and _SERVICE in (s.get("args") or [])
    ]
    assert len(deploy) == 1, "expected exactly one gcloud deploy step"
    flags = [a for a in deploy[0]["args"] if isinstance(a, str) and a.startswith("--")]
    assert any(a.startswith("--image=") for a in flags)
    assert not any(a.startswith("--set-env-vars") for a in flags)
    # Only --image and --region: anything else is scope creep past image-only.
    assert {a.split("=", 1)[0] for a in flags} == {"--image", "--region"}


def test_no_step_relaxes_failure_or_ordering():
    for step in _config()["steps"]:
        assert "allowFailure" not in step
        assert "waitFor" not in step
        # `script` bypasses the args-based pins above.
        assert "script" not in step


def test_build_uses_the_notifier_dockerfile_and_repo_root_context():
    doc = _config()
    build = [
        s
        for s in doc["steps"]
        if s.get("name") == _DOCKER and "build" in (s.get("args") or [])
    ]
    assert len(build) == 1
    args = build[0]["args"]
    assert "workers/notifier/Dockerfile" in args
    # Repo root, because the worker imports driftscribe_lib/.
    assert args[-1] == "."


def test_both_assertion_phases_run():
    """Preflight AND postcondition — one without the other is half a guard."""
    body = _CONFIG.read_text(encoding="utf-8")
    assert "assert_notifier_service.py" in body
    assert "preflight" in body
    assert "postcondition" in body


def test_runs_as_the_dedicated_deploy_sa_not_default_compute():
    doc = _config()
    assert "cloudbuild-deploy-sa@" in doc["serviceAccount"]
    # A user-specified serviceAccount REQUIRES an explicit logging option.
    assert doc["options"]["logging"] == "CLOUD_LOGGING_ONLY"


def test_expected_secret_substitution_is_the_real_secret():
    assert _config()["substitutions"]["_EXPECT_SECRET"] == _SECRET


def test_expected_service_account_substitution_is_least_privilege():
    assert _config()["substitutions"]["_EXPECT_SERVICE_ACCOUNT"] == _SA
