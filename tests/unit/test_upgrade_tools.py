"""Unit tests for the upgrade-workload ADK tool wrappers (Phase 17.C.4).

The two LLM-facing tools live in :mod:`agent.adk_tools`:

- :func:`upgrade_read_dependencies_tool` — no arguments; derives
  ``target_repo`` / ``lockfile_path`` from
  :data:`agent.workloads.registry.UPGRADE_TARGET_REGISTRY`.
- :func:`upgrade_propose_pr_tool` — accepts only the decision content
  (``package_name``, ``target_version``, ``advisory_url``, ``body``);
  derives ``target_repo`` / ``lockfile_path`` / ``branch`` / ``base``
  / ``title`` server-side.

The tests below pin the Codex 2026-05-20 follow-up invariant: the
LLM-facing surface MUST NOT carry authority fields as parameters, even
though the worker re-validates them defensively. Letting the LLM pick
the branch name would invite ``branch="main"`` / ``branch="../.."``
foot-guns; pre-binding the values in the coordinator wrapper is what
keeps the worker's re-check genuinely defense-in-depth.
"""
from __future__ import annotations

import inspect
from unittest.mock import patch


# --------------------------------------------------------------------------- #
# Signature pins — authority fields must NOT appear as LLM-facing parameters
# --------------------------------------------------------------------------- #


def test_upgrade_read_dependencies_tool_takes_no_args():
    """The reader-side tool's LLM-facing signature has zero parameters.

    Authority fields (``target_repo``, ``lockfile_path``) are derived
    server-side from the upgrade contract — the LLM never sees a way
    to redirect this call. Worker-side defense in depth (the
    :func:`workers.upgrade_reader.main.read` handler re-validates
    against ``UPGRADE_TARGET_REPO``) must NOT be the primary authority
    boundary.
    """
    from agent.adk_tools import upgrade_read_dependencies_tool

    sig = inspect.signature(upgrade_read_dependencies_tool)
    assert list(sig.parameters) == [], (
        "upgrade_read_dependencies_tool must take NO arguments — "
        "authority fields are derived from UPGRADE_TARGET_REGISTRY"
    )


def test_upgrade_propose_pr_tool_signature_excludes_authority_fields():
    """The proposer-side tool's LLM-facing signature contains ONLY
    decision content, never routing fields.

    Codex 2026-05-20 follow-up (Phase 17.C.4 step 3): the worker accepts
    ``target_repo`` / ``lockfile_path`` / ``branch`` / ``base`` /
    ``title`` so it can re-validate them, but the LLM-facing tool must
    derive all five server-side. A regression that "helpfully" added
    one of these to the wrapper signature would let the LLM pick the
    branch name (foot-gun) or even the target repo (capability widening).
    """
    from agent.adk_tools import upgrade_propose_pr_tool

    sig = inspect.signature(upgrade_propose_pr_tool)
    params = set(sig.parameters)
    forbidden = {"target_repo", "lockfile_path", "branch", "base", "title"}
    leak = forbidden & params
    assert not leak, (
        f"upgrade_propose_pr_tool exposes authority field(s) {sorted(leak)} "
        f"to the LLM. These must be derived server-side from "
        f"UPGRADE_TARGET_REGISTRY + a deterministic naming rule."
    )
    # And positive: the decision-content params are present.
    assert {"package_name", "target_version", "advisory_url", "body"} <= params


# --------------------------------------------------------------------------- #
# Payload pins — authority fields ARE sent to the worker (defense in depth)
# --------------------------------------------------------------------------- #


def test_upgrade_read_dependencies_tool_calls_worker_with_authority_fields(
    upgrade_workload_env,
):
    """The tool calls ``upgrade_reader`` with ``target_repo`` and
    ``lockfile_path`` matching :data:`UPGRADE_TARGET_REGISTRY["phase17_demo"]`.

    The worker re-validates both fields against its env-pinned
    ``UPGRADE_TARGET_REPO`` allowlist; the cross-check here is that
    the coordinator sends the values the worker expects, sourced from
    the same registry the worker's CI guard pins against.
    """
    from agent.adk_tools import upgrade_read_dependencies_tool
    from agent.workloads import UPGRADE_TARGET_REGISTRY

    expected = UPGRADE_TARGET_REGISTRY["phase17_demo"]

    with patch("agent.adk_tools.worker_client.call") as m:
        m.return_value = {"target_repo": expected.target_repo, "dependencies": []}
        upgrade_read_dependencies_tool()

    m.assert_called_once()
    args, _ = m.call_args
    worker_name = args[0]
    payload = args[1]
    assert worker_name == "upgrade_reader"
    assert payload == {
        "target_repo": expected.target_repo,
        "lockfile_path": expected.lockfile_path,
    }


def test_upgrade_propose_pr_tool_derives_authority_server_side(
    upgrade_workload_env,
):
    """The tool derives ``target_repo`` / ``lockfile_path`` / ``branch``
    / ``base`` / ``title`` server-side and sends them to the worker.

    Pins:
    - ``target_repo`` / ``lockfile_path`` come from
      ``UPGRADE_TARGET_REGISTRY["phase17_demo"]``;
    - ``branch`` is ``upgrade/{package_name}-{ver_dashed}`` (matching
      the worker's :data:`ALLOWED_BRANCH_PREFIX`);
    - ``base`` is hardcoded ``"main"``;
    - ``title`` starts with the worker's :data:`ALLOWED_TITLE_PREFIX`
      (``upgrade``);
    - the four LLM-decision args (``package_name`` /
      ``target_version`` / ``advisory_url`` / ``body``) pass through
      unchanged.
    """
    from agent.adk_tools import upgrade_propose_pr_tool
    from agent.workloads import UPGRADE_TARGET_REGISTRY

    expected = UPGRADE_TARGET_REGISTRY["phase17_demo"]

    with patch("agent.adk_tools.worker_client.call") as m:
        m.return_value = {"url": "https://example.invalid/pull/1"}
        upgrade_propose_pr_tool(
            package_name="lodash",
            target_version="4.17.21",
            advisory_url="https://github.com/advisories/GHSA-jf85-cpcp-j695",
            body="Bumps lodash to 4.17.21 to address GHSA-jf85-cpcp-j695.",
        )

    m.assert_called_once()
    args, _ = m.call_args
    worker_name = args[0]
    payload = args[1]
    assert worker_name == "upgrade_docs"
    # Authority fields — server-side derivation.
    assert payload["target_repo"] == expected.target_repo
    assert payload["lockfile_path"] == expected.lockfile_path
    assert payload["branch"] == "upgrade/lodash-4-17-21"
    assert payload["base"] == "main"
    assert payload["title"].startswith("upgrade")
    assert "lodash" in payload["title"]
    assert "4.17.21" in payload["title"]
    # LLM-decision fields — pass-through.
    assert payload["package_name"] == "lodash"
    assert payload["target_version"] == "4.17.21"
    assert (
        payload["advisory_url"]
        == "https://github.com/advisories/GHSA-jf85-cpcp-j695"
    )
    assert (
        payload["body"]
        == "Bumps lodash to 4.17.21 to address GHSA-jf85-cpcp-j695."
    )


def test_upgrade_propose_pr_tool_branch_slug_handles_version_dots(
    upgrade_workload_env,
):
    """``target_version`` semver dots become dashes in the branch slug.

    ``4.17.21`` → ``upgrade/lodash-4-17-21``. Pins the deterministic
    branch-naming rule so a future "tidy" that swapped the dash for an
    underscore (or kept the dot) can't silently change the branch
    naming convention — the worker's :data:`_BRANCH_TAIL` regex would
    still accept dots, so this test is what catches the change.
    """
    from agent.adk_tools import upgrade_propose_pr_tool

    with patch("agent.adk_tools.worker_client.call") as m:
        m.return_value = {"url": "https://example.invalid/pull/2"}
        upgrade_propose_pr_tool(
            package_name="some-pkg",
            target_version="1.2.3",
            advisory_url="https://github.com/advisories/GHSA-aaaa-bbbb-cccc",
            body="bump",
        )

    args, _ = m.call_args
    payload = args[1]
    assert payload["branch"] == "upgrade/some-pkg-1-2-3", (
        f"branch slug must replace each version dot with a dash; "
        f"got {payload['branch']!r}"
    )


# --------------------------------------------------------------------------- #
# upgrade_close_pr_tool — authority-clean + best-effort
# --------------------------------------------------------------------------- #


def test_upgrade_close_pr_tool_signature_excludes_authority_fields():
    """The close tool's LLM-facing signature carries ONLY pr_number +
    reason. target_repo is derived server-side, same authority-clean
    invariant as upgrade_propose_pr_tool."""
    from agent.adk_tools import upgrade_close_pr_tool

    params = set(inspect.signature(upgrade_close_pr_tool).parameters)
    assert params == {"pr_number", "reason"}, (
        f"upgrade_close_pr_tool signature drifted: {sorted(params)}. "
        f"target_repo (and any other routing field) must stay server-side."
    )


def test_upgrade_close_pr_tool_passes_resolved_target_repo(upgrade_workload_env):
    """The tool resolves target_repo from UPGRADE_TARGET_REGISTRY and
    forwards (target_repo, pr_number, reason) to call_close_pr — the LLM
    never supplies the repo."""
    from agent.adk_tools import upgrade_close_pr_tool
    from agent.workloads import UPGRADE_TARGET_REGISTRY

    expected = UPGRADE_TARGET_REGISTRY["phase17_demo"]

    with patch("agent.adk_tools.worker_client.call_close_pr") as m:
        m.return_value = {"closed": True, "number": 7}
        out = upgrade_close_pr_tool(pr_number=7, reason="superseded")

    m.assert_called_once_with(expected.target_repo, 7, "superseded")
    assert out == {"closed": True, "number": 7}


def test_upgrade_close_pr_tool_is_best_effort_on_worker_error(upgrade_workload_env):
    """A WorkerClientError (e.g. the worker's 403 label-gate bounce) is
    returned as a soft dict, NOT raised — so /chat reports the refusal
    reason instead of mapping it to a 502. The worker's response body is
    surfaced so the operator sees *why* the close was refused."""
    from agent.adk_tools import upgrade_close_pr_tool
    from agent.worker_client import WorkerClientError

    with patch("agent.adk_tools.worker_client.call_close_pr") as m:
        m.side_effect = WorkerClientError(
            403, "PR #7 is not a DriftScribe PR (missing 'driftscribe' label)",
            "upgrade_docs",
        )
        out = upgrade_close_pr_tool(pr_number=7, reason="superseded")

    assert out["closed"] is False
    assert out["worker"] == "upgrade_docs"
    assert out["status_code"] == 403
    assert "driftscribe" in out["error"]
