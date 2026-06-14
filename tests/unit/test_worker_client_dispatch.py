"""Tests for the dispatch_plan_builder kwarg on call_open_infra_pr (Task 4)."""
from unittest.mock import patch


def _make_fake_call(captured):
    def fake_call(worker, payload, endpoint=None):
        captured.append({"worker": worker, "payload": payload, "endpoint": endpoint})
        return {"status": "opened", "pr_number": 10, "pr_url": "https://gh/10", "branch": "infra/x"}
    return fake_call


def test_call_open_infra_pr_dispatch_flag_true():
    """dispatch_plan_builder=True is included in the POST body."""
    from agent import worker_client
    captured = []
    with patch.object(worker_client, "call", side_effect=_make_fake_call(captured)):
        worker_client.call_open_infra_pr(
            "owner/repo", "infra/b", "title", "body", [], dispatch_plan_builder=True
        )
    assert captured[0]["payload"]["dispatch_plan_builder"] is True


def test_call_open_infra_pr_dispatch_flag_false_default():
    """dispatch_plan_builder defaults to False and is included in the POST body."""
    from agent import worker_client
    captured = []
    with patch.object(worker_client, "call", side_effect=_make_fake_call(captured)):
        worker_client.call_open_infra_pr(
            "owner/repo", "infra/b", "title", "body", []
        )
    assert captured[0]["payload"]["dispatch_plan_builder"] is False
