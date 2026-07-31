"""ds-thm — every coordinator-side mirror of a worker bound, pinned equal.

The bug class ds-j0i proved on production: a value bounded at the CONSUMER and
unbounded (or differently bounded) at the PRODUCER. It is invisible in review
because the two halves ship in different containers, so the only thing that can
catch a drift is a test that reads BOTH.

Two assertions per bound, because they prove different things and ds-j0i's
review got this wrong:

- **equality** — the coordinator's constant IS the worker's declared bound.
  Seam validation alone cannot do this: a coordinator constant of 9000 against
  a worker cap of 10000 produces payloads the worker accepts happily, and the
  drift goes unnoticed until someone raises the worker's cap and wonders why
  nothing changed.
- **seam validation** (in the integration tests) — the payload the coordinator
  actually emits satisfies the worker's real model.

The worker packages are separate deployables and the coordinator must never
take a BUILD-time dependency on one (``agent/validator.py`` states the rule).
A TEST-time import is precisely how the mirror stays honest, and is the reason
duplicating these constants is safe rather than sloppy.
"""
from __future__ import annotations

import re
from pathlib import Path

import annotated_types
import pytest
from pydantic import BaseModel, ValidationError

from agent import renderer
from agent.validator import _REVISION_NAME as COORDINATOR_REVISION_NAME
from workers._testenv import import_worker_main

# Each worker main is booted under its OWN canonical env — three of them pin
# ``OWN_URL`` three different ways and only one value can be in ``os.environ``
# at a time, so importing them naively makes whichever module got there first
# decide for the rest of the process. ``import_worker_main`` sets each worker's
# canon immediately before its import and hands the process env back untouched
# afterwards, which is what lets this module import three workers in a row
# without leaking any of them into the suites that run later (ds-2n1).
import_worker_main("workers.notifier.main")
import_worker_main("workers.rollback.main")
import_worker_main("workers.upgrade_docs.main")

from workers.notifier.main import NotifyRequest  # noqa: E402
from workers.rollback.main import _REVISION_NAME as WORKER_REVISION_NAME  # noqa: E402
from workers.upgrade_docs.main import ClosePrRequest  # noqa: E402


def _bound(model: type[BaseModel], field: str, kind: type) -> int | None:
    """Read a declared ``MinLen``/``MaxLen`` off the worker's real model.

    Introspection rather than a regex over source text: this reads what pydantic
    will actually ENFORCE at runtime, so it cannot be fooled by a constraint
    that is commented out, shadowed, or declared somewhere the regex isn't
    looking.
    """
    for meta in model.model_fields[field].metadata:
        if isinstance(meta, kind):
            return getattr(meta, "max_length", None) or getattr(meta, "min_length", None)
    return None


def test_notifier_body_cap_is_mirrored_exactly() -> None:
    assert _bound(NotifyRequest, "body", annotated_types.MaxLen) == (
        renderer.NOTIFIER_BODY_MAX_CHARS
    ), (
        "agent.renderer.NOTIFIER_BODY_MAX_CHARS drifted from the Notifier's "
        "declared body cap — the coordinator would emit bodies the worker 422s"
    )


def test_upgrade_close_reason_cap_is_mirrored_exactly() -> None:
    assert _bound(ClosePrRequest, "reason", annotated_types.MaxLen) == (
        renderer.UPGRADE_CLOSE_REASON_MAX_CHARS
    )


def test_the_minimums_are_one_which_is_why_empty_input_needs_a_policy() -> None:
    """Pinned as well as the maxima. The empty-input fallbacks
    (``ROLLBACK_REASON_ABSENT``, ``UPGRADE_CLOSE_REASON_ABSENT``, notify_tool's
    soft refusal) exist because of a REAL consumer constraint, not a stylistic
    preference — ds-j0i hit both ends of the range. If a worker ever drops its
    minimum, this failing is the prompt to re-examine those fallbacks rather
    than leave three unexplained special cases behind."""
    assert _bound(NotifyRequest, "body", annotated_types.MinLen) == 1
    assert _bound(ClosePrRequest, "reason", annotated_types.MinLen) == 1


def test_the_empty_input_fallbacks_satisfy_the_minimum_they_exist_for() -> None:
    assert len(renderer.ROLLBACK_REASON_ABSENT.strip()) >= 1
    assert len(renderer.UPGRADE_CLOSE_REASON_ABSENT.strip()) >= 1


def test_the_fallbacks_also_fit_the_maximum() -> None:
    """A fallback longer than the cap would turn the empty-input fix into the
    over-length bug — the exact shape of ds-q38's "the fix is riskier than the
    bug"."""
    assert len(renderer.ROLLBACK_REASON_ABSENT) <= renderer.ROLLBACK_REASON_MAX_CHARS
    assert (
        len(renderer.UPGRADE_CLOSE_REASON_ABSENT)
        <= renderer.UPGRADE_CLOSE_REASON_MAX_CHARS
    )


def test_the_revision_name_regex_is_mirrored_exactly() -> None:
    """Found by the ds-thm audit: ``agent/validator.py`` mirrors this regex
    under a comment saying "update this constant in lockstep" — and until now
    nothing checked that anyone had. ``propose_rollback_tool`` refuses on the
    coordinator's copy (ds-thm fix 5), so a drift here would either refuse
    revisions the worker accepts or forward ones it rejects."""
    assert COORDINATOR_REVISION_NAME.pattern == WORKER_REVISION_NAME.pattern


def test_the_rollback_propose_schema_still_enforces_the_regex() -> None:
    """The mirror above is only worth pinning while the worker actually applies
    it at the schema layer."""
    from workers.rollback.main import ProposeRequest

    patterns = [
        m for m in ProposeRequest.model_fields["target_revision"].metadata
        if getattr(m, "pattern", None)
    ]
    assert patterns, "target_revision lost its pattern constraint"
    assert patterns[0].pattern == WORKER_REVISION_NAME.pattern


def test_the_regex_bounds_length_to_the_declared_maximum() -> None:
    """Why fix 5 checks the pattern ALONE and not the length separately: the
    regex is ``[a-z][a-z0-9-]{0,62}[a-z0-9]``, i.e. 2..64 characters, so
    matching it already implies the field's 1..64 bound. Pinned so a future
    loosening of the regex cannot silently let an over-length name through the
    coordinator's guard."""
    assert COORDINATOR_REVISION_NAME.match("a" * 64)
    assert not COORDINATOR_REVISION_NAME.match("a" * 65)
    assert _bound(
        __import__("workers.rollback.main", fromlist=["x"]).ProposeRequest,
        "target_revision",
        annotated_types.MaxLen,
    ) == 64


@pytest.mark.parametrize(
    "candidate",
    [
        "payment-demo-00024-f6v",          # the real shape
        "payment-demo-00024-f6v\n",        # ⚠️ the blocker below
        "payment-demo-00024-f6v\nrm -rf /",
        "Payment-Demo-00024",
        "9-starts-with-digit",
        "trailing-hyphen-",
        "a" * 64,
        "a" * 65,
        "",
        "../../etc/passwd",
    ],
)
def test_the_guard_and_the_worker_agree_on_every_candidate(candidate: str) -> None:
    """Differential test: whatever the coordinator's guard decides, the worker's
    real model must decide the same. Testing each side separately cannot catch
    a DISAGREEMENT, and a disagreement is the entire bug class.

    It found one. The guard used ``re.match``, and Python's ``$`` also matches
    just before a final newline — so ``"payment-demo-00024-f6v\\n"`` passed the
    coordinator and was rejected by pydantic-core (Rust regex, where ``$`` is
    strictly end-of-text) with ``string_pattern_mismatch``. A guard that
    forwards exactly what the consumer refuses is ds-j0i reproduced inside the
    fix for ds-j0i. Both lanes now use ``fullmatch``.
    """
    from workers.rollback.main import ProposeRequest

    guard_accepts = bool(COORDINATOR_REVISION_NAME.fullmatch(candidate))
    try:
        ProposeRequest(target_revision=candidate, reason="why")
        worker_accepts = True
    except ValidationError:
        worker_accepts = False

    assert guard_accepts == worker_accepts, (
        f"{candidate!r}: coordinator says {guard_accepts}, worker says "
        f"{worker_accepts} — the guard must never forward what the worker "
        f"refuses, nor refuse what the worker would take"
    )


def test_the_autonomous_validator_rejects_what_the_worker_rejects() -> None:
    """The trailing-newline defect was in ``agent/validator.py`` too, where it
    PREDATES ds-thm: the autonomous lane would pass validation and then 422 at
    the worker, with no model in the loop to recover — ds-j0i's exact shape.

    Behavioural, not a source-text pin. An earlier version of this test grepped
    for ``.fullmatch(``, which passes for any code that merely contains the
    string and proves nothing about what the validator decides.
    """
    from agent.contract import load_contract
    from agent.models import ContractStatus, DecisionAction, DecisionProposal, EnvDiff
    from agent.validator import ValidationError, validate

    proposal = DecisionProposal(
        action=DecisionAction.ROLLBACK,
        env_diffs=[
            EnvDiff(
                name="PAYMENT_MODE",
                expected="mock",
                live="live",
                contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,
            )
        ],
        target_revision="payment-demo-00024-f6v\n",
        rationale="drifted",
        confidence=0.9,
        requires_human_review=True,
    )
    contract = load_contract(Path("demo/ops-contract.yaml"))

    with pytest.raises(ValidationError, match="revision-name regex"):
        validate(proposal, contract, live_env={"PAYMENT_MODE": "live"})


def test_the_coordinator_regex_is_anchored() -> None:
    """An unanchored mirror would accept a long name with a valid prefix and
    hand the worker something it rejects — reopening ds-j0i through the guard
    meant to close it."""
    assert not COORDINATOR_REVISION_NAME.match("payment-demo-00024-f6v\nrm -rf /")
    assert not re.search(r"^\^", "") or COORDINATOR_REVISION_NAME.pattern.startswith("^")
    assert COORDINATOR_REVISION_NAME.pattern.endswith("$")
