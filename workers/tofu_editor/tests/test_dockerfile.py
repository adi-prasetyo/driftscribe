"""Structure assertions for the tofu-editor worker Dockerfile (Phase D1-5).

The tofu-editor is the sole new WRITE surface Phase D introduces, and it runs
the SAME AGENT-mode static gate CI runs — so the image MUST bundle the `tools`
package (as a package, not a loose module) alongside driftscribe_lib/ and the
worker source, and MUST NOT contain coordinator (`agent/`) code (worker
isolation). These are cheap structure locks on those invariants.
"""
from __future__ import annotations

from pathlib import Path

_DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"


def _text() -> str:
    return _DOCKERFILE.read_text(encoding="utf-8")


def test_base_image_is_the_pinned_slim() -> None:
    # Same base-image pin the sibling workers use.
    assert "FROM python:3.12-slim" in _text()


def test_copies_driftscribe_lib() -> None:
    # The worker imports driftscribe_lib.{github,auth,logging,iac_editor_policy,iac_hcl}.
    assert "COPY driftscribe_lib/ ./driftscribe_lib/" in _text()


def test_bundles_tools_as_a_package() -> None:
    t = _text()
    # `from tools.iac_static_gate import ...` must resolve, so the package marker
    # AND the gate module are both copied (a loose iac_static_gate.py would NOT
    # be importable as `tools.iac_static_gate`).
    assert "COPY tools/__init__.py ./tools/__init__.py" in t
    assert "COPY tools/iac_static_gate.py ./tools/iac_static_gate.py" in t


def test_copies_worker_source() -> None:
    t = _text()
    assert "COPY workers/__init__.py ./workers/__init__.py" in t
    assert "COPY workers/tofu_editor/__init__.py ./workers/tofu_editor/__init__.py" in t
    assert "COPY workers/tofu_editor/main.py ./workers/tofu_editor/main.py" in t


def test_does_not_copy_coordinator_code() -> None:
    # Worker isolation: the image must not contain any agent/ (coordinator
    # authority) code. Mirrors the worker's test_no_agent_import.py invariant at
    # the image layer. Check the COPY directives specifically (a bare "agent/"
    # substring would false-positive on prose like "workers stay isolated from
    # agent.* code"); no COPY may reference the agent/ tree as src or dest.
    for line in _text().splitlines():
        stripped = line.lstrip()
        if stripped.upper().startswith("COPY"):
            assert "agent/" not in line, line


def test_cmd_targets_the_worker_app() -> None:
    t = _text()
    assert "uvicorn workers.tofu_editor.main:app" in t
    # Shell form so $PORT expands at runtime (Cloud Run injects PORT).
    assert "--host 0.0.0.0" in t
    assert "${PORT:-8080}" in t
