"""The approval URL must survive Discord's 2000-char ``content`` cap.

Delivering a clickable rollback approval link is the entire reason this
notification exists. The notifier truncates middle-out and keeps a fixed tail
(``_TRUNCATION_TAIL_BUDGET``) on the theory that the link lives near the end of
the body — but that theory is a fact about the *renderer*, which lives in
``agent/`` and evolves independently of the worker. A budget justified by a
comment is a heuristic; a budget checked against the real renderer is an
invariant.

So this test wires the two halves together: it renders a REAL rollback body
with the REAL renderer at punishing rationale lengths, runs it through the REAL
notifier truncation, and asserts the tokened URL is still present. If the
template's tail ever outgrows the budget, this fails loudly instead of the link
silently vanishing from production notifications.

Bead: ds-v00. Companion to the notifier's own unit tests, which cover the
truncation primitive in isolation.
"""
import os

# The notifier reads its config at import time and raises if anything is
# missing (deliberate fail-closed boot). Mirror the worker's own test setup.
os.environ.setdefault("GCP_PROJECT", "test-proj")
os.environ.setdefault("OWN_URL", "https://notifier.example.com")
os.environ.setdefault(
    "ALLOWED_CALLERS", "coordinator@test-proj.iam.gserviceaccount.com"
)
os.environ.setdefault("NOTIFY_WEBHOOK_URL", "https://webhook.example.com/test")

import pytest  # noqa: E402

from agent.models import (  # noqa: E402
    ContractStatus,
    DecisionAction,
    DecisionProposal,
    EnvDiff,
)
from agent.renderer import (  # noqa: E402
    normalize_notifier_body,
    render_rollback_body,
)
from workers.notifier.main import (  # noqa: E402
    _DISCORD_CONTENT_LIMIT,
    NotifyRequest,
    _discord_safe_content,
)

# The worst case the coordinator can actually mint: an absolute coordinator
# origin, a uuid4 approval id, and a MAXIMUM-length approval token. The shape
# guard accepts ``[A-Za-z0-9_-]{43,64}``, so 64 is the longest real URL.
# Low-entropy on purpose: a random-looking 64-char token trips GitGuardian as a
# "Generic High Entropy Secret", and randomness was never what made the fixture
# realistic — the alphabet and the length are.
_APPROVAL_URL = (
    "https://driftscribe-agent-u272wv52kq-an.a.run.app"
    "/approvals/92e0fdb7-1111-2222-3333-444455556666"
    "?t=" + "T" * 64
)

# ds-thm: this used to read "the notifier's schema caps body at 10000 chars, so
# a rationale longer than this could not reach the truncation code at all — it
# would 422 first", and 8000 was chosen to stay under that. It documented the
# boundary and then arranged not to cross it: nothing enforced 8000, which was
# an assumption about how verbose the model feels rather than a property of the
# system. The coordinator now clamps (``normalize_notifier_body``), so the
# fixture is free to exceed the schema cap — and must, or this suite still only
# proves the case it chose for itself.
_MAX_REALISTIC_RATIONALE = 8000
_OVER_THE_SCHEMA_CAP = 30_000


def _proposal(rationale: str) -> DecisionProposal:
    return DecisionProposal(
        action=DecisionAction.ROLLBACK,
        env_diffs=[
            EnvDiff(
                name="PAYMENT_MODE",
                expected="mock",
                live="live",
                contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,
            )
        ],
        target_revision="payment-demo-00020-5qn",
        rationale=rationale,
        confidence=0.95,
    )


def _notified_content(rationale: str) -> str:
    """Render a rollback body exactly as the coordinator does, then cap it."""
    body = render_rollback_body(_proposal(rationale), _APPROVAL_URL)
    # Mirrors the envelope the notifier wraps around every body.
    return _discord_safe_content(f"[DriftScribe/approval/high] {body}")


@pytest.mark.parametrize(
    "rationale_len", [0, 200, 1000, 4000, _MAX_REALISTIC_RATIONALE]
)
def test_the_approval_url_survives_the_discord_cap(rationale_len):
    """At every plausible body length, the operator still gets a live link."""
    content = _notified_content("R" * rationale_len)
    assert len(content) <= _DISCORD_CONTENT_LIMIT
    assert _APPROVAL_URL in content, (
        "the approval URL was truncated away — the notification would reach "
        "the operator with nothing to click"
    )
    # Present is not the same as usable: inside an unterminated code block the
    # URL renders as inert text. An even fence count means it is outside one.
    before = content[: content.index(_APPROVAL_URL)]
    assert before.count("```") % 2 == 0


def test_the_url_stays_clickable_when_the_rationale_opens_a_code_fence():
    """Models emit code blocks. An unclosed one must not swallow the link.

    The plain-``R`` cases above cannot catch this, because they contain no
    Markdown at all — the gap Codex found in the first cut of this change.
    """
    rationale = "Observed config:\n```yaml\n" + ("key: value\n" * 700)
    content = _notified_content(rationale)
    assert len(content) <= _DISCORD_CONTENT_LIMIT
    assert _APPROVAL_URL in content
    before = content[: content.index(_APPROVAL_URL)]
    assert before.count("```") % 2 == 0, (
        "the approval URL rendered inside an unterminated code block"
    )


def test_the_retained_tail_of_a_real_body_contains_no_code_fence():
    """Pins the assumption the tail-closing fix relies on.

    The notifier closes a fence left open by the HEAD of the cut. A fence
    living inside the retained TAIL would need separate handling — an orphan
    closer there would open a spurious block. That case cannot arise today
    because the rollback template's last 800 chars are the link, the expiry
    note and a blockquote, with no fenced content. This test is what keeps
    that true: if the template ever grows fenced content into its tail, fix
    the truncator rather than deleting this test.
    """
    from workers.notifier.main import _TRUNCATION_TAIL_BUDGET

    body = render_rollback_body(
        _proposal("R" * _MAX_REALISTIC_RATIONALE), _APPROVAL_URL
    )
    assert "```" not in body[-_TRUNCATION_TAIL_BUDGET:]


def test_a_naive_head_slice_would_have_lost_the_url():
    """Justifies the middle-out design by proving the obvious one fails.

    Without this, a future reader sees only that both implementations pass the
    short-body cases and may 'simplify' the helper into the bug.
    """
    body = render_rollback_body(
        _proposal("R" * _MAX_REALISTIC_RATIONALE), _APPROVAL_URL
    )
    text = f"[DriftScribe/approval/high] {body}"
    assert len(text) > _DISCORD_CONTENT_LIMIT, "fixture must exceed the cap"
    assert _APPROVAL_URL not in text[:_DISCORD_CONTENT_LIMIT]


def test_the_rendered_body_still_fits_the_notifier_schema():
    """A body over the schema cap 422s before truncation is ever reached.

    Pins the assumption behind ``_MAX_REALISTIC_RATIONALE`` so the parametrized
    cases above stay reachable scenarios rather than hypothetical ones. The cap
    is read from the worker's own model rather than hand-copied — ds-thm, where
    a literal ``10000`` here would have kept passing after a schema change.
    """
    cap = NotifyRequest.model_fields["body"].metadata[-1].max_length
    body = render_rollback_body(
        _proposal("R" * _MAX_REALISTIC_RATIONALE), _APPROVAL_URL
    )
    assert len(body) <= cap


def test_a_rationale_past_the_schema_cap_still_delivers_a_clickable_link():
    """ds-thm — the case the 8000 ceiling above was chosen to avoid.

    The model's rationale is unbounded, so a body over the schema cap is
    reachable in ordinary operation. Two things have to hold at once: the
    coordinator's clamp keeps the request valid, AND the notifier's own Discord
    cut still lands the URL outside a code block. Either alone is insufficient
    — this is the full path, end to end, with both cuts applied in order.
    """
    body = normalize_notifier_body(
        render_rollback_body(
            _proposal("Observed:\n```yaml\n" + "k: v\n" * _OVER_THE_SCHEMA_CAP),
            _APPROVAL_URL,
        )
    )
    cap = NotifyRequest.model_fields["body"].metadata[-1].max_length
    assert len(body) <= cap, "the coordinator's clamp would 422"
    NotifyRequest(channel="approval", severity="high", body=body)

    content = _discord_safe_content(f"[DriftScribe/approval/high] {body}")
    assert len(content) <= _DISCORD_CONTENT_LIMIT
    assert _APPROVAL_URL in content, "the operator got a notification with nothing to click"
    before = content[: content.index(_APPROVAL_URL)]
    assert before.count("```") % 2 == 0, "the link rendered inside a code block"
