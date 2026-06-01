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


def test_dockerfile_agent_packages_tools_static_gate_for_fanout():
    """Phase D5 regression — same bug class as the workloads/ escape above.

    `agent/fanout.py` (the parallel fan-out engine) imports
    `driftscribe_lib.iac_editor_policy`, which does
    `from tools.iac_static_gate import ...`. `tools` is NOT a setuptools
    package (`pyproject [tool.setuptools] packages` is agent/checker/
    driftscribe_lib only), so the editable install does NOT expose it — the
    image must COPY `tools/` as a package AND put /app on PYTHONPATH so the
    import resolves at runtime. Pre-D5 the coordinator never imported
    iac_editor_policy, so this was latent; the first live provision fan-out
    `/chat` call 500'd with `No module named 'tools'`. Pin it so it can't recur.
    """
    content = DOCKERFILE.read_text()
    assert "COPY tools/iac_static_gate.py" in content, (
        "Dockerfile.agent must copy tools/iac_static_gate.py — "
        "driftscribe_lib.iac_editor_policy (pulled in by agent/fanout.py) "
        "imports `from tools.iac_static_gate import ...`."
    )
    assert "COPY tools/__init__.py" in content, (
        "Dockerfile.agent must copy tools/__init__.py so `tools` is an "
        "importable package, not a loose module."
    )
    assert "PYTHONPATH=/app" in content, (
        "Dockerfile.agent must set PYTHONPATH=/app so the non-installed "
        "`tools` package resolves (the editable install only exposes the "
        "declared setuptools packages)."
    )


def test_iac_editor_policy_still_depends_on_tools_static_gate():
    """Cross-check that keeps the Dockerfile guard above honest: if the
    iac_editor_policy -> tools.iac_static_gate import is ever removed (e.g.
    the constants get relocated), this fails first so the COPY/PYTHONPATH
    assertions can be revisited rather than silently guarding a dead dep."""
    policy_src = (REPO / "driftscribe_lib" / "iac_editor_policy.py").read_text()
    assert "from tools.iac_static_gate import" in policy_src, (
        "driftscribe_lib/iac_editor_policy.py no longer imports "
        "tools.iac_static_gate — revisit the Dockerfile.agent tools/ COPY "
        "guard in this module (it may no longer be needed)."
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
