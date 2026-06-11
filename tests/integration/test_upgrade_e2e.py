"""End-to-end integration tests for the upgrade workload (Phase 17.C.5).

Phases 17.C.1–17.C.4 wired the upgrade workload top-to-bottom at the
code level: ``UPGRADE_TARGET_REGISTRY`` carries the authority fields,
:mod:`agent.adk_tools` exposes ``upgrade_read_dependencies_tool`` and
``upgrade_propose_pr_tool`` with authority-clean signatures,
:mod:`agent.worker_client` knows about ``upgrade_reader`` /
``upgrade_docs``, the upgrade workload's ``workload.yaml`` resolves
cleanly, and ``POST /chat workload=upgrade`` eagerly resolves the
upgrade contract at request entry (503 on failure).

This task pins THREE end-to-end properties the per-task tests above
can't catch on their own:

1. **Authority-clean tool surface (load-bearing).** When the LLM picks
   ``upgrade_read_dependencies``, the request the worker actually
   receives carries the registry's ``target_repo`` and ``lockfile_path``
   — NOT any free-form value the LLM could smuggle in. Same for
   ``upgrade_propose_pr``: ``target_repo`` / ``lockfile_path`` /
   ``branch`` / ``base`` / ``title`` are derived server-side from
   ``UPGRADE_TARGET_REGISTRY`` and the upgrade tool wrapper itself.
   The LLM only picks decision content (``package_name``,
   ``target_version``, ``advisory_url``, ``body``). If a future
   "convenience" refactor accidentally accepts ``target_repo`` from
   the LLM, this test fails — the LLM would otherwise be able to
   redirect the upgrade PR at a different repository, which is the
   exact failure mode the authority-clean surface was designed to
   prevent.

2. **Workload isolation.** A turn run under ``workload=upgrade`` MUST
   NOT invoke any drift worker (``reader`` / ``docs`` / ``rollback``).
   The upgrade workload's manifest lists only ``upgrade_reader`` /
   ``upgrade_docs`` / ``notifier``; if a future tool wiring leak
   re-introduced a drift worker call, the test below catches it before
   a real deploy can.

3. **/chat routing smoke pin.** ``POST /chat workload=upgrade``
   reaches the coordinator's ``run_chat`` with ``workload="upgrade"``
   on the happy path (run_chat is mocked here — the contract-resolve
   tests in ``test_main_upgrade_routing.py`` pin the 503 surface).

Mock-seam choice
----------------

The plan suggested two viable seams. We picked the simpler one because
it pins the load-bearing property without depending on LLM behavior:

- **(Picked)** Call the tool wrappers AT THE TOOL LEVEL — not through
  the LLM — and mock :mod:`agent.worker_client.call`. This captures
  the exact payload the wrapper would send to the worker, so we can
  assert every authority field byte-for-byte. The LLM-prompt-adherence
  half (does Gemini actually invoke these tools in this order?) lives
  in the system-prompt goldens pinned in :mod:`tests.unit.test_upgrade_workload_loads`.
  The two halves compose: "the prompt asks for this sequence" + "if the
  LLM follows the prompt, the wiring delivers it correctly."

- **(Skipped)** End-to-end through ``POST /chat workload=upgrade``
  with a stubbed ``run_chat`` that calls the workload's tools in
  sequence (the pattern used by :mod:`tests.integration.test_drift_uses_mcp`).
  Adds plumbing without strengthening the assertion: the tool-level
  test already verifies the payload shape, and the LLM is stubbed
  anyway, so going through ``/chat`` mostly exercises FastAPI routing
  that's already covered by ``test_main_upgrade_routing.py``. We DO
  add a small smoke test below to pin the ``run_chat(workload="upgrade")``
  call signature so a routing-layer regression that drops the workload
  kwarg fails CI loudly.

Why not assert "no MCP call happened" the way drift's negative test does:
the upgrade workload's prompt DOES expect MCP citations on upgrade-PR
proposals. Pinning "no MCP" would be wrong here; pinning the
authority-clean payload shape catches the same class of regression
(silent capability widening) more directly.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agent.config import get_settings
from agent.main import app


# --------------------------------------------------------------------------- #
# Shared fixtures
# --------------------------------------------------------------------------- #


# These match ``UPGRADE_TARGET_REGISTRY["phase17_demo"]``. We pin them
# here as constants so the test failures point at the registry-vs-payload
# mismatch directly rather than at a generic "dict not equal" diff. If
# the registry ever changes, update both these constants AND the
# corresponding pin in :mod:`tests.unit.test_upgrade_target_registry`.
_EXPECTED_TARGET_REPO = "adi-prasetyo/driftscribe"
_EXPECTED_LOCKFILE_PATH = "demo/upgrade-target/package.json"


# Drift worker names the upgrade workload MUST NEVER call. Pinning the
# full set (not just "any drift worker") so a future addition of a
# fourth drift worker is caught by code review (must add to this set
# AND prove it doesn't leak into upgrade).
_DRIFT_WORKER_NAMES = frozenset({"reader", "docs", "rollback"})

# Upgrade workload's allowed worker name set (matches the
# ``worker_names`` list in ``workloads/upgrade/workload.yaml``).
# ``notifier`` is shared between drift and upgrade — it's NOT a drift
# worker by name, just by usage pattern.
_UPGRADE_ALLOWED_WORKERS = frozenset(
    {"upgrade_reader", "upgrade_docs", "notifier"}
)


@pytest.fixture
def upgrade_env_with_adk(monkeypatch, upgrade_workload_env):
    """Compose the shared :func:`upgrade_workload_env` fixture (from
    ``tests/conftest.py``) with the ``USE_ADK=true`` flip the /chat
    routing smoke test needs.

    The upgrade workload's ``load_workload("upgrade")`` reads
    ``UPGRADE_READER_URL`` / ``UPGRADE_DOCS_URL`` / ``NOTIFIER_URL``;
    the autouse drift fixture in ``tests/integration/conftest.py``
    doesn't set the upgrade URLs. ``upgrade_workload_env`` (root
    conftest) sets them AND clears the upgrade-tool ``lru_cache`` —
    using it directly here means new tests don't have to re-derive
    that plumbing.
    """
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    yield


# --------------------------------------------------------------------------- #
# (1) Authority-clean payload — upgrade_read_dependencies_tool
# --------------------------------------------------------------------------- #


def test_upgrade_read_dependencies_tool_sends_registry_authority_fields(
    upgrade_workload_env,
):
    """The LLM-facing ``upgrade_read_dependencies_tool`` takes no
    arguments. The payload it sends to ``upgrade_reader`` must carry
    EXACTLY the ``target_repo`` and ``lockfile_path`` from
    ``UPGRADE_TARGET_REGISTRY["phase17_demo"]`` — no LLM-supplied
    overrides, no defaults from elsewhere.

    Pins the authority-clean property: there is no LLM seam that can
    redirect this call at a different repository or lockfile.

    Mock seam is :mod:`agent.worker_client.call` (the bottom of every
    worker-delegating tool's outbound path). We patch where the tool
    imports it (``agent.adk_tools.worker_client.call``) rather than at
    the module definition, matching the pattern in
    :mod:`test_drift_uses_mcp` and :mod:`test_rollback_e2e`.
    """
    from agent.adk_tools import upgrade_read_dependencies_tool

    # Canonical upgrade-reader /read response shape (matches
    # ``workers.upgrade_reader.main.ReadResponse``). Minimal — the test
    # asserts the REQUEST shape, not the LLM's downstream reasoning over
    # the response.
    canned_read_response = {
        "target_repo": _EXPECTED_TARGET_REPO,
        "lockfile_path": _EXPECTED_LOCKFILE_PATH,
        "dependencies": [
            {
                "package_name": "lodash",
                "current_version": "4.17.20",
                "advisories": [
                    {
                        "ghsa_id": "GHSA-35jh-r3h4-6jhm",
                        "severity": "high",
                        "url": (
                            "https://github.com/advisories/"
                            "GHSA-35jh-r3h4-6jhm"
                        ),
                        "summary": "Prototype pollution in lodash",
                    }
                ],
            }
        ],
    }

    with patch(
        "agent.adk_tools.worker_client.call",
        return_value=canned_read_response,
    ) as m_call:
        result = upgrade_read_dependencies_tool()

    # Single worker call, to the upgrade reader, with EXACTLY the
    # registry's authority fields. Using positional-arg destructuring
    # matches how :mod:`agent.adk_tools.upgrade_read_dependencies_tool`
    # invokes the client — defensive on kwargs in case a future refactor
    # switches to keyword form.
    m_call.assert_called_once()
    args, kwargs = m_call.call_args
    worker = args[0] if args else kwargs.get("worker")
    payload = args[1] if len(args) > 1 else kwargs.get("payload", {})

    assert worker == "upgrade_reader", (
        f"upgrade_read_dependencies_tool must call worker 'upgrade_reader'; "
        f"got {worker!r}. A different name here suggests the tool wiring "
        f"is pointing at a drift worker (capability leak)."
    )
    assert payload == {
        "target_repo": _EXPECTED_TARGET_REPO,
        "lockfile_path": _EXPECTED_LOCKFILE_PATH,
    }, (
        f"upgrade_reader payload must match UPGRADE_TARGET_REGISTRY"
        f"['phase17_demo'] exactly; got {payload!r}. Any divergence "
        f"means either the registry changed (update _EXPECTED_* "
        f"constants) or the tool wrapper accepted an authority field "
        f"from somewhere it shouldn't have (capability leak)."
    )

    # Response is passed through unchanged — the wrapper does no
    # post-processing on the dependencies list. Pinning equality here
    # so a future "convenience" rewrite (e.g. filtering out low-severity
    # advisories) doesn't silently change what the LLM sees.
    assert result == canned_read_response


# --------------------------------------------------------------------------- #
# (2) Authority-clean payload — upgrade_propose_pr_tool
# --------------------------------------------------------------------------- #


def test_upgrade_propose_pr_tool_derives_authority_fields_server_side(
    upgrade_workload_env,
):
    """The LLM-facing ``upgrade_propose_pr_tool`` accepts only decision
    content (``package_name``, ``target_version``, ``advisory_url``,
    ``body``). The worker payload it produces must carry server-side-
    derived values for EVERY authority field:

    - ``target_repo`` / ``lockfile_path`` — from
      ``UPGRADE_TARGET_REGISTRY["phase17_demo"]``.
    - ``branch`` — derived as ``upgrade/{package_name}-{ver_dashed}``
      so all PRs from this worker are observability-scoped to the
      upgrade workload and match
      ``workers.upgrade_docs.main.ALLOWED_BRANCH_PREFIX``.
    - ``base`` — hardcoded to ``"main"`` (the worker's ``_check_base``
      re-asserts this).
    - ``title`` — formatted as ``upgrade({package_name}): {target_version}``
      so the worker's ``ALLOWED_TITLE_PREFIX`` ("upgrade") accepts it.

    LLM-decided fields (``package_name``, ``target_version``,
    ``advisory_url``, ``body``) MUST flow through unchanged — the
    wrapper is not allowed to substitute its own defaults for those.

    Pins the authority-clean property end-to-end for the propose path:
    if a future refactor lets the LLM pick the branch name (foot-gun:
    ``branch="main"``, ``branch="../../etc/passwd"``-style escapes,
    etc.) this test fails.
    """
    from agent.adk_tools import upgrade_propose_pr_tool

    canned_patch_response = {
        "pr_url": "https://github.com/adi-prasetyo/driftscribe/pull/123",
        "branch": "upgrade/lodash-4-17-21",
        "dry_run": True,
    }

    with patch(
        "agent.adk_tools.worker_client.call",
        return_value=canned_patch_response,
    ) as m_call:
        result = upgrade_propose_pr_tool(
            package_name="lodash",
            target_version="4.17.21",
            advisory_url=(
                "https://github.com/advisories/GHSA-35jh-r3h4-6jhm"
            ),
            body=(
                "Bumps lodash from 4.17.20 to 4.17.21 to address "
                "GHSA-35jh-r3h4-6jhm (prototype pollution)."
            ),
        )

    m_call.assert_called_once()
    args, kwargs = m_call.call_args
    worker = args[0] if args else kwargs.get("worker")
    payload = args[1] if len(args) > 1 else kwargs.get("payload", {})

    assert worker == "upgrade_docs", (
        f"upgrade_propose_pr_tool must call worker 'upgrade_docs'; got "
        f"{worker!r}. A different name here would mean the tool is "
        f"pointing at the drift docs worker (capability leak — drift "
        f"docs has a DIFFERENT path allowlist)."
    )

    # Full payload pin — every field is load-bearing.
    expected_payload = {
        # Authority fields, server-side from the registry.
        "target_repo": _EXPECTED_TARGET_REPO,
        "lockfile_path": _EXPECTED_LOCKFILE_PATH,
        # LLM-supplied decision content, flowed through unchanged.
        "package_name": "lodash",
        "target_version": "4.17.21",
        "advisory_url": (
            "https://github.com/advisories/GHSA-35jh-r3h4-6jhm"
        ),
        "body": (
            "Bumps lodash from 4.17.20 to 4.17.21 to address "
            "GHSA-35jh-r3h4-6jhm (prototype pollution)."
        ),
        # Routing fields, derived server-side from the LLM-supplied
        # decision content. The worker's Layer 2 (ALLOWED_BRANCH_PREFIX
        # / ALLOWED_TITLE_PREFIX / _check_base) re-validates all three;
        # we pin them here so the coordinator-side derivation stays
        # the primary authority surface.
        "branch": "upgrade/lodash-4-17-21",
        "base": "main",
        "title": "upgrade(lodash): 4.17.21",
    }

    assert payload == expected_payload, (
        f"upgrade_docs payload mismatch.\n"
        f"  Expected: {expected_payload!r}\n"
        f"  Actual:   {payload!r}\n"
        f"If branch / base / title diverged, either the worker's "
        f"allowlist regex changed (extend the wrapper) or the wrapper "
        f"accepted an authority field from the LLM (capability leak)."
    )

    assert result == canned_patch_response


# --------------------------------------------------------------------------- #
# (3) Workload isolation — no drift worker is ever touched
# --------------------------------------------------------------------------- #


def test_upgrade_tool_sequence_never_invokes_a_drift_worker(
    upgrade_workload_env,
):
    """Run BOTH upgrade tool wrappers in sequence (read → propose) and
    assert every recorded worker call goes to an upgrade-allowed worker.
    No drift worker name (``reader``, ``docs``, ``rollback``) may appear.

    This is the workload-isolation invariant: an upgrade turn must not
    leak into the drift worker set, period. If a future refactor wires
    one of the upgrade tools through ``call("reader", ...)`` by accident
    (autocomplete typo, paste from a drift test, etc.), this test
    catches it before deploy.

    Composes both upgrade tools into the same recording mock so the
    assertion sees the FULL call sequence — pinning per-tool only
    (tests 1 and 2 above) would miss a case where the wrapper makes a
    second, unintended call to a drift worker after the main one.
    """
    from agent.adk_tools import (
        upgrade_propose_pr_tool,
        upgrade_read_dependencies_tool,
    )

    recorded_workers: list[str] = []

    def recording_call(worker: str, payload: dict, *args, **kwargs) -> dict:
        recorded_workers.append(worker)
        # Branch by worker name so each tool gets a shape-correct
        # response. Real workers' response schemas live in
        # ``workers.upgrade_reader.main.ReadResponse`` /
        # ``workers.upgrade_docs.main.PatchResponse``; we only need the
        # minimum here.
        if worker == "upgrade_reader":
            return {
                "target_repo": _EXPECTED_TARGET_REPO,
                "lockfile_path": _EXPECTED_LOCKFILE_PATH,
                "dependencies": [],
            }
        if worker == "upgrade_docs":
            return {
                "pr_url": "https://github.com/x/y/pull/1",
                "branch": "upgrade/lodash-4-17-21",
                "dry_run": True,
            }
        # If the test ever sees a call to a worker name it didn't
        # expect, fail loudly with the name so debugging is one step.
        raise AssertionError(
            f"unexpected worker call {worker!r} during upgrade tool "
            f"sequence — workload isolation violated"
        )

    with patch(
        "agent.adk_tools.worker_client.call",
        side_effect=recording_call,
    ):
        upgrade_read_dependencies_tool()
        upgrade_propose_pr_tool(
            package_name="lodash",
            target_version="4.17.21",
            advisory_url=(
                "https://github.com/advisories/GHSA-35jh-r3h4-6jhm"
            ),
            body="Bumps lodash to address an advisory.",
        )

    # Positive shape: the recorded sequence is exactly the two upgrade
    # workers. If a future "convenience" wrapper auto-notifies on every
    # propose, the assertion needs ``notifier`` added — but that's
    # a deliberate behavior change, not a silent regression.
    assert recorded_workers == ["upgrade_reader", "upgrade_docs"], (
        f"Expected upgrade tool sequence to call exactly "
        f"['upgrade_reader', 'upgrade_docs']; got {recorded_workers!r}."
    )

    # Negative shape: NO drift worker name appears. Set-intersection so
    # the failure message includes the offending name (or names).
    leaked = set(recorded_workers) & _DRIFT_WORKER_NAMES
    assert not leaked, (
        f"Workload isolation violated — upgrade turn invoked drift "
        f"worker(s): {sorted(leaked)!r}. Allowed for upgrade: "
        f"{sorted(_UPGRADE_ALLOWED_WORKERS)!r}."
    )

    # Defense in depth: every recorded worker is in the allowlist.
    out_of_allowlist = set(recorded_workers) - _UPGRADE_ALLOWED_WORKERS
    assert not out_of_allowlist, (
        f"Upgrade turn invoked worker(s) outside the upgrade allowlist: "
        f"{sorted(out_of_allowlist)!r}."
    )


# --------------------------------------------------------------------------- #
# (4) /chat routing smoke pin — workload="upgrade" reaches run_chat
# --------------------------------------------------------------------------- #


def test_chat_upgrade_workload_routes_to_run_chat_with_workload_kwarg(
    upgrade_env_with_adk,
):
    """``POST /chat workload=upgrade`` reaches the coordinator's
    ``run_chat`` with the ``workload="upgrade"`` kwarg. Pins the
    routing-layer property end-to-end: the contract-resolve doesn't
    503 on the bundled valid contract, and the workload kwarg isn't
    silently dropped on the way to the agent.

    The companion 503 paths (bad upgrade contract, missing worker URLs,
    /recheck not implemented) are covered by
    :mod:`test_main_upgrade_routing` and :mod:`test_workload_routing`;
    this test is just the happy-path smoke pin so a future routing
    refactor that breaks the 200 surface fails CI loudly.
    """
    fake_run_chat = AsyncMock(
        return_value={
            "reply": "upgrade workload — no advisories to action",
            "tool_calls": ["upgrade_read_dependencies"],
            "session_id": "upgrade-sid",
        }
    )

    with patch("agent.adk_agent.run_chat", fake_run_chat):
        client = TestClient(app)
        r = client.post(
            "/chat",
            json={"prompt": "triage advisories", "workload": "upgrade"},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reply"].startswith("upgrade workload")
    assert body["session_id"] == "upgrade-sid"

    # Pin the full call signature — a routing refactor that drops the
    # workload kwarg would otherwise silently fall back to drift's
    # default and the contract-resolve eager check would still pass.
    fake_run_chat.assert_awaited_once_with(
        "triage advisories", session_id=None, workload="upgrade",
        autonomy_mode="propose_apply"
    )
