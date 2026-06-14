from unittest.mock import MagicMock
import pytest
from driftscribe_lib.github import dispatch_workflow

def test_dispatch_workflow_calls_create_dispatch_with_args():
    repo = MagicMock()
    wf = MagicMock()
    repo.get_workflow.return_value = wf
    dispatch_workflow(repo, "iac.yml", "main", {"pr_number": "123"})
    repo.get_workflow.assert_called_once_with("iac.yml")
    wf.create_dispatch.assert_called_once_with("main", {"pr_number": "123"})

def test_dispatch_workflow_propagates_errors():
    repo = MagicMock()
    repo.get_workflow.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        dispatch_workflow(repo, "iac.yml", "main", {"pr_number": "1"})
