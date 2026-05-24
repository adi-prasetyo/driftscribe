"""Regression: Dockerfile.agent must package runtime data dirs the agent reads.

Phase 17 (`bf54a79`) added the top-level `workloads/` tree. Dockerfile.agent
wasn't updated to copy it, so the built image silently shipped without
`/app/workloads/`. `agent/workloads/registry.py:_repo_root()` resolves to
`/app` inside the container and expects `/app/workloads/<name>/workload.yaml`
— `/chat` returned 500 with `UnknownWorkloadError` on the first parameterized
E2E build. The bug stayed latent under `DRY_RUN=true` because `/recheck`
returns `no_op` without invoking the workload registry, so prod didn't
notice. This test pins the Dockerfile packaging contract so the same class
of "agent code requires a runtime data dir; Dockerfile forgot to COPY it"
escape can't recur.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "Dockerfile.agent"


def test_dockerfile_agent_copies_workloads():
    content = DOCKERFILE.read_text()
    assert "COPY workloads/" in content, (
        "Dockerfile.agent must `COPY workloads/ ./workloads/` so "
        "agent/workloads/registry.py can resolve workload manifests at "
        "/app/workloads/<name>/workload.yaml at runtime."
    )


def test_dockerfile_agent_copies_required_python_packages():
    content = DOCKERFILE.read_text()
    for required in ("COPY agent/", "COPY checker/", "COPY driftscribe_lib/"):
        assert required in content, (
            f"Dockerfile.agent must contain `{required}` — the editable "
            "install needs the source tree present."
        )


def test_dockerfile_agent_copies_demo_contract():
    content = DOCKERFILE.read_text()
    assert "COPY demo/" in content and "/contract/demo/" in content, (
        "Dockerfile.agent must copy demo/ to /contract/demo/ so "
        "CONTRACT_PATH and DOCS_ROOT resolve runtime artifacts."
    )


def test_workload_manifests_exist_in_source():
    """Cross-check: every workload the registry can serve must have a
    manifest committed in the repo, otherwise `COPY workloads/` would
    package an incomplete tree."""
    workloads_root = REPO / "workloads"
    assert workloads_root.is_dir(), "workloads/ missing from repo root"
    for name in ("drift", "upgrade"):
        manifest = workloads_root / name / "workload.yaml"
        assert manifest.is_file(), (
            f"workloads/{name}/workload.yaml is missing — Dockerfile "
            "would package an empty workload dir."
        )
