"""E2E test harness — skip by default; require explicit env vars.

Key contracts (verified against agent/ and driftscribe_lib/):
- Auth header is X-DriftScribe-Token (NOT X-Operator-Token).
- Baseline reads use read_live_state (serving revision), NOT read_live_env (template).
- Cloud Run mutations use update_mask + LRO .result(timeout=) wait.
"""
import os

import httpx
import pytest
from google.cloud import run_v2
from google.protobuf.field_mask_pb2 import FieldMask

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


@pytest.fixture(scope="session", autouse=True)
def _github_target_pre_run_sweep(e2e_github_repo):
    """Pre-session: close any leftover upgrade PRs in the e2e target repo.

    The upgrade-docs worker creates branches matching ^upgrade/ — that
    prefix is the sweep filter. Production never targets the e2e repo
    (parameterized via _UPGRADE_TARGET_REPO), so this filter is safe.

    Skipped silently when DRIFTSCRIBE_E2E_GITHUB_TOKEN is unset — drift-only
    or HITL-only operator runs shouldn't fail because they didn't supply a
    GitHub token they don't need. Upgrade tests that actually use the GitHub
    client will raise a clear error at their own invocation time.
    """
    if not os.environ.get("DRIFTSCRIBE_E2E_GITHUB_TOKEN"):
        yield
        return
    try:
        from tests.e2e._github_helpers import sweep_upgrade_prs
    except ImportError:
        yield
        return
    sweep_upgrade_prs(e2e_github_repo)
    yield
