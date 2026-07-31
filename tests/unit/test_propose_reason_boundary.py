"""ds-j0i — the coordinator/worker boundary on the /propose payload.

Proven on prod 2026-07-31. The ds-q38 coherence fix worked exactly as designed
— the poisoned ``no_op`` was CAS-evicted — and then the repair died one step
later:

    01:03:23  recheck_evicted_stale_decision
    01:03:42  POST /propose -> 422 Unprocessable Entity

Root cause, reproduced against the worker's real ``ProposeRequest`` rather than
inferred: the coordinator sent ``reason`` UNBOUNDED while the worker declared
``max_length=500`` with ``extra="forbid"``. The model's rationale was 581
characters.

The bug class is what these tests defend, not just the one field: **a value
bounded at the consumer and unbounded at the producer.** Three instances were
found on this one payload — over-long ``reason``, EMPTY ``reason`` (the worker
requires ``min_length=1``, ``DecisionProposal.rationale`` has no minimum), and
``contract_env`` cardinality (worker caps 64 keys, ``OpsContract.expected_env``
has no limit). Each one takes autonomous self-heal down the same way.

So the bounds themselves are pinned as EQUAL here. A future change that raises
one side without the other re-opens the outage, and a unit test is the only
place that skew is visible — the two live in different deployables.
"""
from __future__ import annotations

import agent.contract as contract_mod
from agent.renderer import (
    ROLLBACK_REASON_ABSENT,
    ROLLBACK_REASON_MAX_CHARS,
    normalize_rollback_reason,
)

# The live rationale from the 2026-07-31 incident, verbatim. 581 chars.
_INCIDENT_RATIONALE = (
    "Variable PAYMENT_MODE has drifted from its contracted expected value of "
    "'mock' to 'live' in the active revision payment-demo-00023-6qc. "
    "PAYMENT_MODE has allow_manual_change: false, making this a drift violation "
    "of contract_status present_disallow_manual. We propose rolling back to "
    "payment-demo-00022-p2c, which is the most recent candidate revision in the "
    "previous_revisions list. Because we cannot read a previous revision's env "
    "configuration, the configuration of the chosen candidate "
    "payment-demo-00022-p2c is unverified and should be checked by the operator "
    "prior to approval."
)


def _worker_propose_bounds() -> dict[str, int]:
    """Read the bounds off the WORKER's own module, not a copy of them.

    Importing ``workers/rollback/main.py`` needs its boot env, and the point
    here is the schema rather than the app, so the field constraints are parsed
    out of the source. That keeps this honest: if someone edits the worker's
    numbers, this test sees the edit.
    """
    import re
    from pathlib import Path

    src = Path("workers/rollback/main.py").read_text(encoding="utf-8")
    body = src[src.index("class ProposeRequest"):src.index("class ExecuteRequest")]
    reason_line = next(
        ln for ln in body.splitlines() if ln.strip().startswith("reason:")
    )
    env_line = next(
        ln for ln in body.splitlines() if ln.strip().startswith("contract_env:")
    )
    return {
        "reason_max": int(re.search(r"max_length=(\d+)", reason_line).group(1)),
        "reason_min": int(re.search(r"min_length=(\d+)", reason_line).group(1)),
        "contract_env_max": int(re.search(r"max_length=(\d+)", env_line).group(1)),
    }


def test_the_coordinator_clamp_equals_the_worker_cap() -> None:
    """The skew IS the bug. Raising one side alone re-opens the outage: a
    higher coordinator clamp 422s against the deployed worker, a higher worker
    cap silently does nothing because the producer never uses it."""
    bounds = _worker_propose_bounds()
    assert ROLLBACK_REASON_MAX_CHARS == bounds["reason_max"], (
        f"coordinator clamps at {ROLLBACK_REASON_MAX_CHARS}, worker caps at "
        f"{bounds['reason_max']} — these must move together, worker first"
    )
    assert contract_mod._WORKER_CONTRACT_ENV_MAX_KEYS == bounds["contract_env_max"]


def test_the_incident_rationale_now_fits() -> None:
    """The exact string that produced the 422, as the regression it is."""
    assert len(_INCIDENT_RATIONALE) == 581, "premise: this is the live rationale"
    out = normalize_rollback_reason(_INCIDENT_RATIONALE)
    assert out == _INCIDENT_RATIONALE, "it fits under the raised cap, untouched"
    assert len(out) <= ROLLBACK_REASON_MAX_CHARS


def test_it_would_have_been_rejected_by_the_old_cap() -> None:
    """Pins that the incident was real rather than theoretical — the fixture
    must actually exceed the bound that was deployed when self-heal broke."""
    assert len(_INCIDENT_RATIONALE) > 500


def test_an_over_long_rationale_is_clamped_to_the_contract() -> None:
    out = normalize_rollback_reason("x" * 9000)
    assert len(out) <= ROLLBACK_REASON_MAX_CHARS


def test_truncation_keeps_the_tail_where_the_operator_caveat_lives() -> None:
    """The safety property, not a formatting preference.

    The model puts its caveat LAST — the incident rationale ended "...should be
    checked by the operator prior to approval." A head-only truncation drops
    precisely the sentence an operator needs before approving a production
    rollback, which is why roughly half the budget is reserved for the tail.
    """
    caveat = " The chosen candidate is unverified and must be checked first."
    out = normalize_rollback_reason("PADDING. " * 900 + caveat)

    assert out.endswith(caveat), "the trailing operator caveat must survive"
    assert out.startswith("PADDING."), "and the opening must survive too"
    assert len(out) <= ROLLBACK_REASON_MAX_CHARS


def test_the_omission_is_stated_in_driftscribes_own_voice() -> None:
    """It must not read as though the model said less than it did. The marker
    names DriftScribe as the actor and counts what was dropped, so an operator
    can tell an abridged rationale from a short one."""
    original = "y" * 9000
    out = normalize_rollback_reason(original)
    assert "omitted by DriftScribe" in out

    head, rest = out.split("\n\n[… ", 1)
    count, tail = rest.split(" characters omitted by DriftScribe …]\n\n", 1)

    # The count must describe reality. A marker that under- or over-states the
    # loss is worse than none: an operator would calibrate trust against a
    # number that is wrong.
    assert len(head) + int(count) + len(tail) == len(original)


def test_an_empty_rationale_gets_a_deterministic_fallback() -> None:
    """The worker requires ``min_length=1``; ``DecisionProposal.rationale`` has
    no minimum. Without this, an empty model rationale is the SAME outage by a
    different route — 422, no approval, self-heal dead."""
    for empty in ("", "   ", "\n\t "):
        out = normalize_rollback_reason(empty)
        assert out == ROLLBACK_REASON_ABSENT
        assert len(out) >= 1
        assert len(out) <= ROLLBACK_REASON_MAX_CHARS


def test_every_output_satisfies_both_worker_bounds() -> None:
    """The invariant the worker actually enforces, over the input classes that
    reach it."""
    bounds = _worker_propose_bounds()
    for probe in ("", "  ", "short", _INCIDENT_RATIONALE, "z" * 50_000):
        out = normalize_rollback_reason(probe)
        assert bounds["reason_min"] <= len(out) <= bounds["reason_max"], probe[:20]


def test_an_oversized_contract_preview_degrades_to_nothing(tmp_path, monkeypatch) -> None:
    """Worker caps ``contract_env`` at 64 keys; the contract has no limit. A
    65-variable contract would 422 every proposal.

    Degrade to ``{}``, never a 64-key slice: the preview is optional and the
    approval page renders an honest "no snapshot" when it is absent, whereas a
    truncated subset renders as a COMPLETE contract that quietly omits
    variables. A preview that lies is worse than no preview on the page an
    operator approves a production rollback from.
    """
    class _Rule:
        def __init__(self, v): self.value = v

    class _Contract:
        expected_env = {f"VAR_{i}": _Rule("v") for i in range(65)}

    monkeypatch.setattr(contract_mod, "load_contract", lambda _p: _Contract())
    assert contract_mod.contract_preview_payload("ignored.yaml") == {}


def test_a_contract_at_the_limit_is_still_previewed(tmp_path, monkeypatch) -> None:
    """Exactly 64 is fine — the degrade must not fire one variable early and
    silently cost every operator their change preview."""
    class _Rule:
        def __init__(self, v): self.value = v

    class _Contract:
        expected_env = {f"VAR_{i}": _Rule("v") for i in range(64)}

    monkeypatch.setattr(contract_mod, "load_contract", lambda _p: _Contract())
    monkeypatch.setattr(contract_mod, "contract_hash", lambda _c: "h" * 16)
    out = contract_mod.contract_preview_payload("ignored.yaml")
    assert len(out["contract_env"]) == 64
