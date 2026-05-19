"""Tests for ``UPGRADE_TARGET_REGISTRY`` — the authority record for the
upgrade workload's target repo / lockfile / advisory source (Phase 17.C.1).

Authority property pinned here is the same as 17.A.1's TOOL/WORKER/ACTION
registries: the real ``target_repo``, ``lockfile_path``, and
``advisory_source`` for the upgrade workload live in code, NOT in YAML.
The workload's ``contract.yaml`` references a **symbolic** target name
(today only ``"phase17_demo"``); flipping a YAML value can choose from
the allowlist but cannot redirect the agent at a different repository.

Codex 2026-05-20 blocker — if these fields lived in YAML, an operator
(or attacker editing the manifest) could point the upgrade workers at
any GitHub repo, and the worker-side env-pinned defense would not be
enough on its own because the coordinator would already have called
GitHub Advisories for the wrong repo.

Pin tests:

1. ``UPGRADE_TARGET_REGISTRY["phase17_demo"]`` resolves to a
   ``UpgradeTarget`` with the expected fields populated.
2. Unknown target names raise ``UnknownUpgradeTargetError`` (a subclass
   of ``KeyError`` so callers using dict-shaped lookups catch it with
   the same idiom — matches the existing
   ``UnknownToolError`` / ``UnknownWorkerError`` / ``UnknownActionError``
   convention).
3. The registry is exposed as a ``MappingProxyType`` view so a caller
   that grabs a reference cannot widen the authority by in-place
   mutation (``REGISTRY["x"] = ...`` raises ``TypeError``).
4. ``UpgradeTarget`` is frozen so its fields cannot be reassigned after
   construction (defense in depth alongside the proxy).
5. **CRITICAL pin (Codex 2026-05-20):**
   ``UPGRADE_TARGET_REGISTRY["phase17_demo"].target_repo`` MUST equal
   ``Settings.github_repo`` (configured via the ``GITHUB_REPO`` env
   var). Phase 17 demos the upgrade workload against the same repo as
   the drift workload because ``search_recent_prs`` reads
   ``Settings.github_repo`` — if a future operator changes that env
   without updating the registry, the coordinator would search PRs in
   the wrong repo and miss duplicate-upgrade-PR detection. Pin the
   invariant here so the test suite catches the drift.

The pin is **Phase 17 specific**. Future targets may legitimately
diverge from ``Settings.github_repo`` (e.g. a customer-owned repo
demo); if 17.C grows more targets, this test should be revisited.
"""
from __future__ import annotations

from types import MappingProxyType

import pytest

from agent.config import Settings
from agent.workloads.registry import (
    UPGRADE_TARGET_REGISTRY,
    UnknownUpgradeTargetError,
    UpgradeTarget,
    resolve_upgrade_target,
)


def test_phase17_demo_target_resolves():
    """The single Phase 17 demo target must be present and populated.

    Pins the expected fields so a refactor that drops or renames a field
    surfaces here rather than at a worker-call site."""
    target = UPGRADE_TARGET_REGISTRY["phase17_demo"]
    assert isinstance(target, UpgradeTarget)
    assert target.target_repo, "target_repo must be a non-empty repo string"
    assert "/" in target.target_repo, (
        "target_repo must be a GitHub <owner>/<name> slug, "
        f"got {target.target_repo!r}"
    )
    assert target.lockfile_path == "demo/upgrade-target/package.json"
    assert target.advisory_source == "github"


def test_resolve_upgrade_target_phase17_demo():
    """The resolver helper returns the same record as the registry lookup."""
    target = resolve_upgrade_target("phase17_demo")
    assert target is UPGRADE_TARGET_REGISTRY["phase17_demo"]


def test_unknown_target_name_raises_unknown_upgrade_target_error():
    """Unknown names must raise ``UnknownUpgradeTargetError`` so the
    workload loader can fail boot with a clear message rather than a
    raw KeyError at first agent call."""
    with pytest.raises(UnknownUpgradeTargetError, match="attacker_target"):
        resolve_upgrade_target("attacker_target")


def test_unknown_upgrade_target_error_subclasses_key_error():
    """Mirrors the existing ``UnknownToolError`` / ``UnknownWorkerError``
    / ``UnknownActionError`` convention — they all subclass ``KeyError``
    so callers using dict-shaped catches still pick them up."""
    assert issubclass(UnknownUpgradeTargetError, KeyError)


def test_upgrade_target_registry_is_immutable_mapping_proxy():
    """Same security pin as the TOOL/WORKER/ACTION registries: the
    public registry is a ``MappingProxyType`` so a caller cannot widen
    the authority by in-place mutation. ``Final`` only blocks rebinding,
    not in-place mutation."""
    assert isinstance(UPGRADE_TARGET_REGISTRY, MappingProxyType)
    with pytest.raises(TypeError):
        UPGRADE_TARGET_REGISTRY["attacker_target"] = UpgradeTarget(  # type: ignore[index]
            target_repo="attacker/repo",
            lockfile_path="demo/upgrade-target/package.json",
            advisory_source="github",
        )


def test_upgrade_target_dataclass_is_frozen():
    """Defense-in-depth alongside the proxy: ``UpgradeTarget`` itself
    is frozen so a caller that holds a reference cannot mutate its
    fields. The dataclass + proxy combo matches the pattern used for
    ``ActionSpec`` / ``WorkerSpec`` elsewhere in the registry."""
    target = UPGRADE_TARGET_REGISTRY["phase17_demo"]
    with pytest.raises((AttributeError, TypeError)):
        target.target_repo = "attacker/repo"  # type: ignore[misc]


# Hardcoded canonical repo for the Phase 17 demo target. Pinning this
# as a literal (rather than reading it back from the registry) is the
# point of the test below — if the registry's value drifts away from
# this constant, the assertion fails. Codex 2026-05-20 review caught
# the original "read it from the registry, set env to that, assert
# equality" formulation as tautological. The correct shape is: pin
# both the registry and the env to the same independently-stated
# string.
EXPECTED_PHASE17_DEMO_REPO = "adi-prasetyo/driftscribe"


def test_phase17_demo_target_repo_pins_to_settings_github_repo(monkeypatch):
    """**CRITICAL Phase 17 invariant pin (Codex 2026-05-20).**

    Phase 17 demos the upgrade workload against the same repository as
    the drift workload. ``agent.adk_tools.search_recent_prs_tool``
    reads ``Settings.github_repo`` (via ``get_settings()``) to look up
    duplicate-upgrade PRs; the upgrade workers and the coordinator's
    upgrade tooling target ``UPGRADE_TARGET_REGISTRY["phase17_demo"].
    target_repo``. If those two diverge — e.g. an operator updates
    ``GITHUB_REPO`` env without updating the registry constant — the
    agent would search for past upgrade PRs in the wrong repo and
    happily reopen a near-duplicate, or fail silently.

    The test compares **two sides independently** against
    :data:`EXPECTED_PHASE17_DEMO_REPO` so a registry change in the
    wrong direction fails the assertion. (An earlier formulation read
    the expected value back from the registry, then set env to that,
    which was tautological — flagged by Codex.) Setting ``GITHUB_REPO``
    via monkeypatch keeps the test deterministic across CI / dev /
    laptop without coupling to the user's local ``.env``.

    **Future targets may diverge** from ``Settings.github_repo``
    (e.g. a customer-owned repo demo with a different ``GITHUB_REPO``);
    revisit this test when 17.C grows more entries. Today
    ``phase17_demo`` is the only entry and the invariant must hold.
    """
    # First pin: the registry constant matches the expected repo string.
    assert (
        UPGRADE_TARGET_REGISTRY["phase17_demo"].target_repo
        == EXPECTED_PHASE17_DEMO_REPO
    ), (
        "UPGRADE_TARGET_REGISTRY['phase17_demo'].target_repo drifted from "
        f"{EXPECTED_PHASE17_DEMO_REPO!r} — Phase 17 demos the upgrade "
        "workload against the drift repo, and search_recent_prs reads the "
        "matching Settings.github_repo. If the registry legitimately moves "
        "(e.g. customer-owned demo repo), update this test deliberately."
    )

    # Second pin: when configured together, Settings.github_repo and
    # the registry agree. Setting env via monkeypatch keeps the test
    # deterministic regardless of dev .env state.
    monkeypatch.setenv("GITHUB_REPO", EXPECTED_PHASE17_DEMO_REPO)
    settings = Settings()
    assert (
        settings.github_repo
        == UPGRADE_TARGET_REGISTRY["phase17_demo"].target_repo
    ), (
        "Phase 17 invariant violated: UPGRADE_TARGET_REGISTRY['phase17_demo']"
        ".target_repo must equal Settings.github_repo when configured "
        "together — search_recent_prs_tool reads the latter to detect "
        "duplicate upgrade PRs, the upgrade workers target the former, "
        "and they must agree for Phase 17."
    )
