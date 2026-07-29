"""ds-qua — the coordinator records WHICH reasoning run authored an infra PR.

The approval desk's listing-provenance card has no decision document, so without this
record it asks the operator to approve an infrastructure change with no visible
supporting evidence at all. These tests pin the write side of the fix at the
single-agent tool gate (``agent.adk_tools._open_iac_pr_and_notify``); the fan-out gate
is pinned in ``test_fanout_orchestrator.py``.

The property under test throughout: **a missing link is fine, a wrong link is not.**
Every ambiguous condition must record nothing.
"""
from __future__ import annotations

import pytest

from agent.iac_pr_trace_store import (
    InMemoryIacPrTraceStore,
    _reset_iac_pr_trace_store_for_tests,
    _set_iac_pr_trace_store_for_tests,
)
from driftscribe_lib.logging import reset_trace_id, set_trace_id

_TRACE = "c" * 32


@pytest.fixture
def store():
    s = InMemoryIacPrTraceStore()
    _set_iac_pr_trace_store_for_tests(s)
    yield s
    _reset_iac_pr_trace_store_for_tests()


@pytest.fixture
def bound_trace():
    tok = set_trace_id(_TRACE)
    yield _TRACE
    reset_trace_id(tok)


def _patch_worker(monkeypatch, response):
    """Stub the editor worker with an exact response body."""
    from agent import adk_tools

    def _fake(*, target_repo, branch, title, body, files, dispatch_plan_builder=False):
        return {"branch": branch, **response}

    monkeypatch.setattr(adk_tools.worker_client, "call_open_infra_pr", _fake)


def _open(**kwargs):
    from agent import adk_tools

    return adk_tools.open_infra_pr_tool(
        files=[{"path": "iac/net.tf", "content": "# vpc\n"}],
        title="Add VPC network",
        body="Provision the shared VPC.",
        **kwargs,
    )


def _target_repo():
    from agent.adk_tools import derive_iac_pr_authority

    return derive_iac_pr_authority("Add VPC network").target_repo


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #
def test_a_newly_opened_pr_records_the_authoring_trace(monkeypatch, store, bound_trace):
    _patch_worker(
        monkeypatch,
        {"status": "opened", "pr_number": 7, "pr_url": "https://gh/pr/7", "reused": False},
    )
    _open()
    assert store.get(_target_repo(), 7) == bound_trace


def test_the_record_is_scoped_to_the_repo_the_pr_opened_against(
    monkeypatch, store, bound_trace
):
    """PR numbers are repository-local. The write must key on the EDITOR TARGET repo,
    which diverges from settings.github_repo under the override — otherwise target
    repo B's trace would later render as evidence on listing repo A's PR #7."""
    monkeypatch.setenv("IAC_EDITOR_TARGET_REPO_OVERRIDE", "owner/editor-target")
    _patch_worker(
        monkeypatch,
        {"status": "opened", "pr_number": 7, "pr_url": "https://gh/pr/7", "reused": False},
    )
    _open()
    assert store.get("owner/editor-target", 7) == bound_trace
    assert store.get("adi-prasetyo/driftscribe", 7) is None


def test_the_recorded_trace_is_the_bound_one_not_a_freshly_minted_id(
    monkeypatch, store, bound_trace
):
    """⚠️ ``current_trace_id_or_new()`` would MINT here. A minted id names no logged
    reasoning, so the desk's link would open an empty timeline."""
    _patch_worker(
        monkeypatch,
        {"status": "opened", "pr_number": 7, "pr_url": "https://gh/pr/7", "reused": False},
    )
    _open()
    recorded = store.get(_target_repo(), 7)
    assert recorded == _TRACE
    assert recorded is not None


# --------------------------------------------------------------------------- #
# Everything ambiguous records NOTHING
# --------------------------------------------------------------------------- #
def test_a_reused_pr_records_nothing(monkeypatch, store, bound_trace):
    """open_iac_pr is idempotent and returns an EXISTING PR for a known branch.
    Attributing this run's reasoning to a PR it merely rediscovered would be a false
    evidence claim."""
    _patch_worker(
        monkeypatch,
        {"status": "opened", "pr_number": 7, "pr_url": "https://gh/pr/7", "reused": True},
    )
    _open()
    assert store.get(_target_repo(), 7) is None


@pytest.mark.parametrize(
    "reused",
    [
        pytest.param(..., id="field-absent-old-worker"),
        pytest.param(None, id="null"),
        pytest.param("false", id="string-false"),
        pytest.param(0, id="int-zero"),
        pytest.param("true", id="string-true"),
        pytest.param(1, id="int-one"),
    ],
)
def test_an_ambiguous_reused_field_records_nothing(monkeypatch, store, bound_trace, reused):
    """Strict ``is False``, not truthiness. The old-worker case (field absent) is the
    important one: it cannot tell us whether the PR is new, and absence is not consent.
    An earlier draft used ``is not True`` and would have recorded here — turning a
    reused PR under an old worker into a link to reasoning that did not author it."""
    response = {"status": "opened", "pr_number": 7, "pr_url": "https://gh/pr/7"}
    if reused is not ...:
        response["reused"] = reused
    _patch_worker(monkeypatch, response)
    _open()
    assert store.get(_target_repo(), 7) is None


@pytest.mark.parametrize(
    "response",
    [
        pytest.param({"status": "opened", "pr_url": "https://gh/pr/7", "reused": False}, id="no-pr-number"),
        pytest.param({"status": "opened", "pr_number": 7, "reused": False}, id="no-pr-url"),
        pytest.param({"status": "opened", "pr_number": True, "pr_url": "https://gh/pr/7", "reused": False}, id="bool-pr-number"),
        pytest.param({"status": "opened", "pr_number": 0, "pr_url": "https://gh/pr/7", "reused": False}, id="zero-pr-number"),
        pytest.param({"status": "opened", "pr_number": 7, "pr_url": "", "reused": False}, id="empty-pr-url"),
    ],
)
def test_an_unconfirmed_pr_records_nothing(monkeypatch, store, bound_trace, response):
    """Same ``iac_pr_pointer`` gate as the notification: no confirmed PR, no record."""
    _patch_worker(monkeypatch, response)
    _open()
    assert store._traces == {}


def test_no_bound_trace_records_nothing(monkeypatch, store):
    """No ``bound_trace`` fixture here — the ContextVar is empty."""
    monkeypatch.setattr("driftscribe_lib.logging.get_trace_id", lambda: "")
    _patch_worker(
        monkeypatch,
        {"status": "opened", "pr_number": 7, "pr_url": "https://gh/pr/7", "reused": False},
    )
    _open()
    assert store._traces == {}


# --------------------------------------------------------------------------- #
# Evidence must never break PR authoring
# --------------------------------------------------------------------------- #
def test_a_store_failure_does_not_break_pr_opening(monkeypatch, bound_trace):
    class _Exploding:
        def get(self, repo, pr_number):
            raise RuntimeError("firestore is down")

        def set_if_absent(self, repo, pr_number, trace_id):
            raise RuntimeError("firestore is down")

    _set_iac_pr_trace_store_for_tests(_Exploding())
    try:
        _patch_worker(
            monkeypatch,
            {"status": "opened", "pr_number": 7, "pr_url": "https://gh/pr/7", "reused": False},
        )
        result = _open()
        # The PR still opened and the model still gets its pointer + next steps.
        assert result["pr_number"] == 7
        assert result["pr_url"] == "https://gh/pr/7"
        assert "/iac-approvals/7" in result["next_steps"]
    finally:
        _reset_iac_pr_trace_store_for_tests()


def test_first_writer_wins_across_two_authoring_calls(monkeypatch, store, bound_trace):
    """Two runs cannot both claim authorship of one PR; the first is the author.

    DEFENSIVE: the current worker cannot return `reused: False` twice for one PR
    number (a second call derives a fresh branch, and a reused PR reports True), so
    this fixture is not reachable today. It pins the store's first-writer-wins
    primitive against a concurrent writer or a future call path.
    """
    _patch_worker(
        monkeypatch,
        {"status": "opened", "pr_number": 7, "pr_url": "https://gh/pr/7", "reused": False},
    )
    _open()
    tok = set_trace_id("d" * 32)
    try:
        _open()
    finally:
        reset_trace_id(tok)
    assert store.get(_target_repo(), 7) == _TRACE


# --------------------------------------------------------------------------- #
# The adoption path shares the same tail
# --------------------------------------------------------------------------- #
def test_the_adoption_path_records_through_the_same_gate(monkeypatch, store, bound_trace):
    """``propose_adoption_tool`` reaches ``_open_iac_pr_and_notify`` directly, so it
    must inherit the record without a call site of its own.

    Drives the PUBLIC tool, not the shared tail: asserting on the tail would stay
    green if the adoption path ever stopped routing through it, which is precisely
    the regression this test exists to catch.
    """
    from agent import adk_tools

    _patch_worker(
        monkeypatch,
        {"status": "opened", "pr_number": 9, "pr_url": "https://gh/pr/9", "reused": False},
    )
    project = "driftscribe-hack-2026"
    monkeypatch.setattr(
        adk_tools, "find_open_adopt_pr_for_resource", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "agent.config.Settings.gcp_project",
        property(lambda self: project),
        raising=False,
    )
    # The main-tree preflight (§1.11) fetches iac/*.tf@main from GitHub and
    # fail-CLOSES on error. Stub it: without this the test only passes when some
    # earlier test happens to have left a stub behind — which is exactly how it
    # first passed in isolation and failed in the full suite.
    monkeypatch.setattr(
        adk_tools,
        "_fetch_main_iac_tree",
        lambda repo: {
            "iac/variables.tf": (
                f'variable "project_id" {{\n  default = "{project}"\n}}\n'
            )
        },
    )
    out = adk_tools.propose_adoption_tool(
        resource_type="google_pubsub_topic",
        name="adopt-probe-topic",
    )
    assert out.get("status") == "opened", out
    assert out["pr_number"] == 9
    # Whatever title the adoption renderer chose, the record must sit under the
    # editor target repo — the record is keyed by repo, not by title.
    assert store.get(_target_repo(), 9) == bound_trace
