"""Coordinator-isolation invariant for the tofu-editor worker (Phase D1-3).

Spec-critical: importing ``workers.tofu_editor.main`` must NOT pull any
``agent.*`` module into ``sys.modules``. Workers bundle only
``driftscribe_lib/`` (+ ``tools/iac_static_gate.py``) and their own source;
they stay isolated from coordinator authority code. ``agent.workloads.registry``
(and the rest of the ``agent.*`` package) drags in coordinator-only deps via
``agent.adk_tools`` — see the long comment at
``agent/workloads/registry.py:429-440`` for the rationale.

Implementation note (copied from the upgrade-docs / upgrade-reader isolation
test): a plain ``sys.modules`` check inside the running pytest session would
be flaky because other test files will have already imported ``agent.*``
earlier in the same process. We spawn a clean Python subprocess that imports
ONLY the worker module and inspects its own ``sys.modules``; if a future change
adds ``from agent...`` anywhere in the worker's chain, this surfaces it loudly.
"""
import os
import subprocess
import sys
import textwrap


def test_worker_does_not_import_agent() -> None:
    script = textwrap.dedent(
        """
        import os
        import sys

        # Required env for boot-time module load — match the values seeded
        # by the other test modules so the worker imports cleanly here.
        os.environ["IAC_EDITOR_TARGET_REPO"] = "adi-prasetyo/driftscribe"
        os.environ["GITHUB_TOKEN"] = "test-token"
        os.environ["OWN_URL"] = "https://tofu-editor.example.com"
        os.environ["ALLOWED_CALLERS"] = "driftscribe-agent@test-proj.iam.gserviceaccount.com"

        import workers.tofu_editor.main  # noqa: F401

        leaked = sorted(
            m for m in sys.modules if m == "agent" or m.startswith("agent.")
        )
        if leaked:
            sys.stderr.write("LEAKED: " + ",".join(leaked) + "\\n")
            sys.exit(1)
        sys.exit(0)
        """
    ).strip()
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        ))),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "tofu-editor worker leaked coordinator imports — workers must "
        f"stay isolated from agent.* code.\nstderr: {result.stderr}\n"
        f"stdout: {result.stdout}"
    )
