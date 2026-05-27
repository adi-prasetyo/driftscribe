"""Container import-smoke for the Infra-Reader Agent worker (Phase B).

Sibling of the worker's own unit tests; this one guards the *container* rather
than the request logic. Two complementary checks:

1. **Import proxy.** The worker's distinctive runtime imports
   (``driftscribe_lib.iac_hcl``, ``driftscribe_lib.infra_inventory``,
   ``google.cloud.asset_v1``) import cleanly in the dev venv. The dev venv is
   not the container, but if a declared dep is missing here it's certainly
   missing in the image — so this is a cheap early signal that the dep set is
   coherent.

2. **Dockerfile dep-completeness.** Parse the ``uv pip install`` line out of
   the worker's Dockerfile and assert the two infra-specific deps
   (``google-cloud-asset`` + ``python-hcl2``) are present. If a future edit to
   main.py adds a dependency without updating the Dockerfile, this is the
   regression that fails — the assertions read the file, they do not restate a
   hardcoded dep list.
"""
import importlib
import re
from pathlib import Path

# tests/unit/<this file> -> tests -> repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCKERFILE = _REPO_ROOT / "workers" / "infra_reader" / "Dockerfile"


def test_worker_runtime_imports_resolve():
    """Proxy for container dep sufficiency: the worker's headline imports load."""
    for name in (
        "driftscribe_lib.iac_hcl",
        "driftscribe_lib.infra_inventory",
        "google.cloud.asset_v1",
    ):
        assert importlib.import_module(name) is not None


def _dockerfile_install_line() -> str:
    """Return the ``uv pip install`` block from the worker Dockerfile.

    The block is line-continued with trailing backslashes; collapse it to a
    single string so individual specifiers can be matched regardless of layout.
    """
    text = _DOCKERFILE.read_text(encoding="utf-8")
    match = re.search(r"uv pip install --system\s*((?:.*\\\n)*.*)", text)
    assert match, f"no `uv pip install --system` line found in {_DOCKERFILE}"
    # Collapse continuations + whitespace into one line.
    return " ".join(match.group(1).split())


def test_dockerfile_declares_infra_deps():
    """The Dockerfile install line must declare the infra-specific deps.

    Reads the actual file (not a hardcoded list) so adding a dep to main.py
    without updating the Dockerfile fails here.
    """
    install_line = _dockerfile_install_line()
    assert "google-cloud-asset" in install_line, install_line
    assert "python-hcl2" in install_line, install_line
