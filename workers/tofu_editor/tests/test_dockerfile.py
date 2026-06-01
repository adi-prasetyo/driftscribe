"""Structure assertions for the tofu-editor worker Dockerfile (Phase D1-5).

The tofu-editor is the sole new WRITE surface Phase D introduces, and it runs
the SAME AGENT-mode static gate CI runs — so the image MUST bundle the `tools`
package (as a package, not a loose module) alongside driftscribe_lib/ and the
worker source, and MUST NOT contain coordinator (`agent/`) code (worker
isolation). These are cheap structure locks on those invariants.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

_WORKER_DIR = Path(__file__).resolve().parents[1]
_DOCKERFILE = _WORKER_DIR / "Dockerfile"
_PYPROJECT = _WORKER_DIR / "pyproject.toml"


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


# --- runtime-dependency locks (Phase D bugfix) -----------------------------
#
# WHY python-hcl2 matters here: this worker runs the SAME AGENT-mode static gate
# CI runs, importing it in-process at boot:
#
#     main.py  ->  tools.iac_static_gate  ->  driftscribe_lib.iac_hcl  ->  import hcl2
#
# `import hcl2` is provided by the PyPI package **python-hcl2** (`hcl2` is only
# the import name). The worker's image installs its deps directly via
# `uv pip install --system <list>` — it does NOT `pip install .` against the
# repo root — so anything the boot path imports MUST be in that list. Unit tests
# pass without it only because the repo-root env happens to have python-hcl2;
# the DEPLOYED image would ModuleNotFound at boot. These locks guard against a
# future reader "cleaning up" the (seemingly unused) python-hcl2 dep.
#
# The complete set of third-party top-level imports the boot path needs:
#   - fastapi / uvicorn         : the ASGI app itself
#   - google-auth + requests    : driftscribe_lib.auth.verify_caller (ID tokens)
#   - PyGithub                  : driftscribe_lib.github.open_iac_pr
#   - python-hcl2               : tools.iac_static_gate -> driftscribe_lib.iac_hcl
_REQUIRED_RUNTIME_DEPS = (
    "fastapi",
    "uvicorn",
    "google-auth",
    "requests",
    "PyGithub",
    "python-hcl2",
)


def _install_list() -> str:
    """The text of the `uv pip install --system ...` instruction (the line +
    its backslash-continued lines), lowercased for case-insensitive matching."""
    lines = _text().splitlines()
    for i, line in enumerate(lines):
        if "uv pip install" in line:
            block = [line]
            j = i
            while lines[j].rstrip().endswith("\\"):
                j += 1
                block.append(lines[j])
            return "\n".join(block).lower()
    raise AssertionError("no `uv pip install` instruction found in the Dockerfile")


def test_dockerfile_install_list_includes_python_hcl2() -> None:
    # The specific regression this bugfix closes: the boot path imports hcl2 via
    # tools.iac_static_gate -> driftscribe_lib.iac_hcl, so the image MUST install
    # python-hcl2 or the deployed container ModuleNotFound's at boot.
    assert "python-hcl2" in _install_list()


def test_dockerfile_install_list_includes_all_runtime_deps() -> None:
    # Every third-party top-level package the boot path imports must appear in the
    # image's install list (the image does not `pip install .`). Checking the
    # whole set — not just python-hcl2 — so dropping ANY runtime dep is caught.
    install = _install_list()
    for dep in _REQUIRED_RUNTIME_DEPS:
        assert dep.lower() in install, f"{dep} missing from the Dockerfile install list"


def test_dockerfile_documents_why_python_hcl2_is_required() -> None:
    # Keep a human-readable reason in the Dockerfile so a future reader doesn't
    # "clean up" python-hcl2 as an unused dep. Require the import-chain breadcrumb.
    t = _text().lower()
    assert "iac_static_gate" in t
    assert "iac_hcl" in t


def test_pyproject_dependencies_include_python_hcl2() -> None:
    # pyproject.toml is the canonical dep doc that mirrors the Dockerfile floors;
    # parse it for real (tomllib) and assert python-hcl2 is declared there too.
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]
    names = [d.lower() for d in deps]
    assert any(d.startswith("python-hcl2") for d in names), deps
    # And the rest of the runtime set stays declared here as well.
    for dep in _REQUIRED_RUNTIME_DEPS:
        assert any(d.startswith(dep.lower()) for d in names), f"{dep} missing from pyproject deps"
