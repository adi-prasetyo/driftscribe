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

import os
import re

# Worker modules read config at import and fail closed if it is missing, so the
# values have to be in place BEFORE the imports below — a fixture is too late.
#
# The process env is then RESTORED once the imports are done. Without that, this
# module's values leak into every suite that runs after it in the same pytest
# process: a worker whose own test file uses ``setdefault`` would silently
# inherit ours instead of its own, and the resulting failure appears in a file
# nobody touched. (Observed here: it broke the rollback worker's
# ``test_propose_happy_path`` only in a full-suite run.) The imported modules
# have already captured what they need, so restoring costs nothing.
_PRIOR_ENV: dict[str, str | None] = {}

# Every value below is the SAME one that worker's own test file uses. That
# matters more than it looks: `setdefault` makes the winner whichever module
# imports first in the pytest process, so a different-but-valid value here
# silently overrides the value another suite asserts on. It broke
# workers/rollback/tests/test_rollback.py::test_propose_happy_path (which pins
# coord.example.com) in the full-suite run only. Matching the canon makes the
# import order stop mattering.
_SHARED = {
    "GCP_PROJECT": "test-proj",
    "ALLOWED_CALLERS": "coordinator@test-proj.iam.gserviceaccount.com",
    "NOTIFY_WEBHOOK_URL": "https://webhook.example.com/test",
    "COORDINATOR_URL": "https://coord.example.com",
    # Self-describing placeholder, matching the rollback worker's own tests. A
    # random-looking value here would trip GitGuardian's generic high-entropy
    # rule on every commit in the PR (the ds-y5i fixture lesson) while adding
    # nothing: this module never signs anything, it only imports the schema.
    "APPROVAL_HMAC_KEY": "test-hmac-key",
    "TARGET_REPO": "adi-prasetyo/driftscribe",
    "UPGRADE_TARGET_REPO": "adi-prasetyo/driftscribe",
    "GITHUB_TOKEN": "test-token",
}


def _set(**kw: str) -> None:
    for k, v in kw.items():
        _PRIOR_ENV.setdefault(k, os.environ.get(k))
        os.environ.setdefault(k, v)


_set(**_SHARED)

from pathlib import Path  # noqa: E402

import annotated_types  # noqa: E402
import pytest  # noqa: E402
from pydantic import BaseModel, ValidationError  # noqa: E402

from agent import renderer  # noqa: E402
from agent.validator import _REVISION_NAME as COORDINATOR_REVISION_NAME  # noqa: E402

# ``OWN_URL`` is the one value three workers pin three different ways, and only
# one can win per process — so it is set to each worker's own canon immediately
# before that worker is imported, and forced (not setdefault) between imports.
# It feeds `verify_caller`'s audience check, which these tests never exercise;
# doing it properly anyway keeps this module from being the reason a future
# audience assertion fails somewhere else.
_PRIOR_ENV["OWN_URL"] = os.environ.get("OWN_URL")
os.environ["OWN_URL"] = "https://notifier.example.com"
from workers.notifier.main import NotifyRequest  # noqa: E402

os.environ["OWN_URL"] = "https://rollback.example.com"
from workers.rollback.main import (  # noqa: E402
    _REVISION_NAME as WORKER_REVISION_NAME,
)

os.environ["OWN_URL"] = "https://upgrade-docs.example.com"
from workers.upgrade_docs.main import ClosePrRequest  # noqa: E402

# Imports done — hand the process env back exactly as it was found.
for _k, _old in _PRIOR_ENV.items():
    if _old is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _old


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


def test_the_autonomous_validator_uses_the_same_call_as_the_chat_guard() -> None:
    """The trailing-newline defect above was present in ``agent/validator.py``
    too, where it predates ds-thm — the autonomous lane would have passed
    validation and then 422'd at the worker with no model in the loop to
    recover. Pinned by source so a future edit cannot quietly revert one lane
    to ``.match(`` while the other keeps ``.fullmatch(``."""
    src = Path("agent/validator.py").read_text(encoding="utf-8")
    assert "_REVISION_NAME.fullmatch(" in src
    assert "_REVISION_NAME.match(" not in src


def test_the_coordinator_regex_is_anchored() -> None:
    """An unanchored mirror would accept a long name with a valid prefix and
    hand the worker something it rejects — reopening ds-j0i through the guard
    meant to close it."""
    assert not COORDINATOR_REVISION_NAME.match("payment-demo-00024-f6v\nrm -rf /")
    assert not re.search(r"^\^", "") or COORDINATOR_REVISION_NAME.pattern.startswith("^")
    assert COORDINATOR_REVISION_NAME.pattern.endswith("$")
