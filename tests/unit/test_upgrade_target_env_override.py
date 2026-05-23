"""Phase 20: UPGRADE_TARGET_REPO_OVERRIDE redirects the registry-resolved target_repo.

The agent-side ``resolve_upgrade_target`` consults this env var so the
E2E build (which deploys both upgrade workers with
``UPGRADE_TARGET_REPO=adi-prasetyo/driftscribe-e2e-target``) sees a
coordinator that proposes against the same redirected repo. Prod (env
unset) keeps the registry default.
"""
import os
from unittest.mock import patch

from agent.workloads.registry import resolve_upgrade_target


def test_resolve_uses_registry_default_without_override():
    if "UPGRADE_TARGET_REPO_OVERRIDE" in os.environ:
        del os.environ["UPGRADE_TARGET_REPO_OVERRIDE"]
    target = resolve_upgrade_target("phase17_demo")
    assert target.target_repo == "adi-prasetyo/driftscribe"


def test_resolve_uses_override_when_set():
    with patch.dict(os.environ, {"UPGRADE_TARGET_REPO_OVERRIDE": "acme/driftscribe-e2e-target"}):
        target = resolve_upgrade_target("phase17_demo")
        assert target.target_repo == "acme/driftscribe-e2e-target"
        # lockfile_path + advisory_source are untouched.
        assert target.lockfile_path == "demo/upgrade-target/package.json"
