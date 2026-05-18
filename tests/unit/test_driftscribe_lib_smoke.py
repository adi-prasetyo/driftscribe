"""Smoke test: driftscribe_lib submodules import cleanly and shims re-export.

The full behavior tests live next to the historical agent module names
(test_cloud_run_client.py, test_github_actions.py); this file just guards
the new package structure.
"""


def test_imports_cloud_run():
    from driftscribe_lib import cloud_run
    assert callable(cloud_run.read_live_env)


def test_imports_github():
    from driftscribe_lib import github
    for name in ("get_repo", "open_drift_issue", "open_escalation_issue", "open_docs_pr"):
        assert callable(getattr(github, name))


def test_imports_auth():
    from driftscribe_lib import auth
    assert callable(auth.mint_id_token)
    assert callable(auth.verify_caller)


def test_imports_logging():
    from driftscribe_lib import logging as ds_logging
    logger = ds_logging.setup("smoke-test")
    assert logger.name == "smoke-test"


def test_agent_shims_reexport():
    """Existing import paths keep working — no callers need to change."""
    from agent.cloud_run_client import read_live_env  # noqa: F401
    from agent.github_actions import (  # noqa: F401
        get_repo,
        open_docs_pr,
        open_drift_issue,
        open_escalation_issue,
    )
