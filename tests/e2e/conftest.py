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
