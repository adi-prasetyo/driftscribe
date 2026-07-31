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
_CAP = NotifyRequest.model_fields["body"].metadata[-1].max_length


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


def test_even_a_naive_head_slice_would_now_keep_a_link():
    """ds-thm inverted this test, and the inversion is the point.

    It used to assert the opposite — that the URL was absent from the first
    2000 characters — as the justification for the notifier's middle-out cut,
    on the premise that the link lived ONLY at the end. Putting an approval
    link above the model's rationale (so forward-parsed Markdown cannot swallow
    it) deliberately breaks that premise, and leaves the body robust to a
    head-slicing consumer as a side effect.

    The notifier's middle-out cut is still worth keeping: it is generic across
    bodies, and retaining the tail preserves the expiry note and the traffic
    warning. It is simply no longer the ONLY thing standing between the
    operator and an unusable notification.
    """
    body = render_rollback_body(
        _proposal("R" * _MAX_REALISTIC_RATIONALE), _APPROVAL_URL
    )
    text = f"[DriftScribe/approval/high] {body}"
    assert len(text) > _DISCORD_CONTENT_LIMIT, "fixture must exceed the cap"
    assert _APPROVAL_URL in text[:_DISCORD_CONTENT_LIMIT], (
        "the leading approval link should survive even a naive head slice"
    )


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
    _assert_link_survives_the_whole_path("Observed:\n```yaml\n" + "k: v\n" * _OVER_THE_SCHEMA_CAP)


def test_a_tilde_fenced_rationale_also_keeps_the_link_clickable():
    """CommonMark has TWO fence characters, and a backtick-only rule silently
    leaves the other half live. Codex reproduced this through the real
    renderer: a ``~~~yaml`` block whose closer fell in the omitted middle left
    the approval URL inside the fence with no link rendered at all — a
    notification that arrives, looks fine, and cannot be acted on.
    """
    _assert_link_survives_the_whole_path("Observed:\n~~~yaml\n" + "k: v\n" * _OVER_THE_SCHEMA_CAP)


# The rationales that defeated one or more of the six string-surgery attempts.
# They are kept as a set because the structural fix has to hold for ALL of them
# without knowing anything about Markdown — that is the whole claim.
_HOSTILE = {
    "plain": "R" * 40_000,
    "unclosed_fence": "Observed:\n```yaml\n" + "k: v\n" * 8000,
    "long_fence": "Observed:\n" + "`" * 12 + "\n```\n" + "k: v\n" * 8000,
    "tilde_fence": "Observed:\n~~~yaml\n" + "k: v\n" * 8000,
    "inline_spans": ("prose with ` a stray span " * 2000),
    "coalescing": "~~`~" * 5000,
    "html_comment": "```html\n<!--\n" + "k: v\n" * 8000,
    "cdata": "<![CDATA[\n" + "k: v\n" * 8000,
    "script": "<script>\n" + "k: v\n" * 8000,
    "quotes_the_marker": "[… 42 characters omitted by DriftScribe …]\n" * 900,
}


@pytest.mark.parametrize("name", sorted(_HOSTILE))
def test_the_approval_footer_survives_every_hostile_rationale(name: str):
    """ds-thm's STRUCTURAL claim, isolated from the sanitizing one.

    The body is bounded at RENDER — the variable sections are fitted to what
    the fixed template leaves over — so the template is never part of any text
    that gets cut. The approval URL, the expiry note and the traffic warning
    are therefore present verbatim no matter what the model wrote, and no
    Markdown reasoning is involved in making that true.

    Deliberately kept separate from the rendering assertions below, so that
    what each mechanism guarantees stays visible after nine attempts at
    conflating them.
    """
    body = render_rollback_body(
        _proposal(_HOSTILE[name]), _APPROVAL_URL, max_chars=_CAP
    )
    assert len(body) <= _CAP
    NotifyRequest(channel="approval", severity="high", body=body)
    assert f"<{_APPROVAL_URL}>" in body
    assert "This approval link expires in 15 minutes." in body
    assert "swing **100% of traffic**" in body


@pytest.mark.parametrize("name", sorted(_HOSTILE))
def test_the_approval_link_still_RENDERS_for_every_hostile_rationale(name: str):
    """At least one clickable link, whatever the model wrote.

    Now guaranteed by the LEADING link rather than by sanitization: Markdown
    parses forwards, so the link above the rationale renders before anything
    below it can open a fence. Verified by Codex against an identity
    neutralizer — all ten still render.

    An earlier version of this docstring claimed disabling ``_neutralize_fences``
    made seven of these fail. That was true when the only link was in the
    footer, and became false the moment the leading link landed. Left corrected
    rather than deleted: a test that describes a guarantee it no longer
    provides is how the six broken repairs stayed green.
    """
    from markdown_it import MarkdownIt

    body = render_rollback_body(
        _proposal(_HOSTILE[name]), _APPROVAL_URL, max_chars=_CAP
    )
    assert f'href="{_APPROVAL_URL}"' in MarkdownIt().render(body), (
        "the link is present but does not render — something above it is still "
        "swallowing it"
    )


@pytest.mark.parametrize("name", sorted(_HOSTILE))
def test_the_FOOTER_link_renders_too_which_is_what_neutralization_buys(name: str):
    """What ``_neutralize_fences`` is actually for, now that the leading link
    carries the safety guarantee.

    The footer is where the expiry note and the traffic warning live, and an
    operator who scrolls expects the call to action there. Reaching it means
    surviving the whole rationale, so this is the assertion that genuinely
    depends on the rationale being sanitized — disable the neutralizer and
    these fail while the leading-link test above stays green.
    """
    from markdown_it import MarkdownIt

    body = render_rollback_body(
        _proposal(_HOSTILE[name]), _APPROVAL_URL, max_chars=_CAP
    )
    html = MarkdownIt().render(body)
    assert html.count(f'href="{_APPROVAL_URL}"') == 2, (
        "the footer link did not render — the rationale is still swallowing "
        "everything below it"
    )


@pytest.mark.parametrize("name", sorted(_HOSTILE))
def test_a_SHORT_hostile_rationale_also_keeps_the_link_clickable(name: str):
    """The defect that predates ds-thm, and the reason the link moved above the
    rationale.

    Neutralization only runs when the rationale is TRUNCATED, so a model that
    emitted an unclosed ``` in a 926-character rationale broke the approval
    link in every notification — bounded or not, before this change or after.
    Budgeting cannot help: the body was never over the cap.

    Markdown parses forwards, so a link rendered ABOVE the model's text cannot
    be captured by anything the model writes below it. That is structural, and
    it is why this passes on the UNBOUNDED path too.
    """
    from markdown_it import MarkdownIt

    short = _HOSTILE[name][:900]
    for kwargs in ({}, {"max_chars": _CAP}):
        body = render_rollback_body(_proposal(short), _APPROVAL_URL, **kwargs)
        assert f'href="{_APPROVAL_URL}"' in MarkdownIt().render(body), (
            f"{name} ({'bounded' if kwargs else 'unbounded'}): no clickable link"
        )


@pytest.mark.parametrize("cap", [1, 50, 261, 500, 5000])
def test_an_impossible_cap_still_produces_a_conforming_body(cap: int):
    """No implementation can preserve an arbitrary URL AND fit a cap shorter
    than that URL — but it must never emit an over-cap body either, because
    that is the 422 this whole bead exists to prevent. Degrade, never exceed.
    """
    body = render_rollback_body(_proposal("x" * 5000), _APPROVAL_URL, max_chars=cap)
    assert len(body) <= cap


def test_an_absurd_approval_url_degrades_instead_of_overflowing():
    """Only reachable through a misconfigured ``COORDINATOR_URL``. It must not
    raise either: this runs after the approval is minted and before the
    decision row is written, so an exception would strand it (ds-hdt)."""
    body = render_rollback_body(
        _proposal("x" * 200), "https://x/" + "u" * 20_000, max_chars=_CAP
    )
    assert len(body) <= _CAP


@pytest.mark.parametrize("rows", [1, 2, 5, 20])
def test_the_evidence_table_is_never_cut_mid_row(rows: int):
    """Rows are dropped whole. Half a row is malformed Markdown: the pipes stop
    matching the header and the table degrades to prose, which on an approval
    page reads as though the evidence were something other than a table."""
    p = _proposal("x" * 4000)
    p.env_diffs = [
        EnvDiff(
            name=f"VAR_{i}_{'n' * 200}",
            expected="mock",
            live="live",
            contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,
        )
        for i in range(rows)
    ]
    body = render_rollback_body(p, _APPROVAL_URL, max_chars=_CAP)
    assert len(body) <= _CAP
    table_rows = [ln for ln in body.split("\n") if ln.startswith("| `VAR_")]
    for line in table_rows:
        assert line.count("|") == 7, f"row was cut mid-way: {line[-40:]!r}"


@pytest.mark.parametrize("rationale_len", [12, 581, 4500, 5000, 8000, 8700])
def test_any_body_that_fits_is_byte_identical_to_the_unbounded_render(
    rationale_len: int,
):
    """The bounded path must not fire on anything the worker would accept.

    The lengths matter. An earlier version budgeted the rationale to half the
    available space WITHOUT first asking whether the whole body fits, so a
    5000-char rationale — assembling to ~6000 against a 10 000 cap — came out
    abridged at ~5577. The old test used a short rationale only and claimed to
    cover this (Codex). 8700 is just under the point where truncation becomes
    genuinely necessary (template overhead is ~1250); 581 is what ds-j0i actually observed on prod.
    """
    rationale = "R" * rationale_len
    bounded = render_rollback_body(_proposal(rationale), _APPROVAL_URL, max_chars=_CAP)
    unbounded = render_rollback_body(_proposal(rationale), _APPROVAL_URL)
    assert len(unbounded) <= _CAP, "premise: this fixture must already fit"
    assert bounded == unbounded, "a body that fits was abridged anyway"
    assert "omitted" not in bounded


@pytest.mark.parametrize("cap", [0, -1, -10_000])
def test_a_nonsense_cap_is_refused_rather_than_sliced(cap: int):
    """``minimal[:-1]`` would quietly emit 226 characters under Python's
    negative-index semantics — the same shape of bug this module was bitten by
    in ``clamp_middle_out``. Callers pass a module constant, so this is a
    programming error and must surface as one."""
    with pytest.raises(ValueError):
        render_rollback_body(_proposal("x"), _APPROVAL_URL, max_chars=cap)


def _assert_link_survives_the_whole_path(rationale: str) -> None:
    """Coordinator clamp -> worker schema -> Discord cut, then ask a real
    parser whether the operator has something to click.

    The assertion used to be ``before.count("```") % 2 == 0``. That is the same
    backtick-parity reasoning that was wrong four times in the implementation,
    and it cannot see a tilde fence or a delimiter-length mismatch at all — an
    oracle that shares the blind spot of the code it checks is not an oracle.
    """
    from markdown_it import MarkdownIt

    body = normalize_notifier_body(
        render_rollback_body(_proposal(rationale), _APPROVAL_URL)
    )
    cap = NotifyRequest.model_fields["body"].metadata[-1].max_length
    assert len(body) <= cap, "the coordinator's clamp would 422"
    NotifyRequest(channel="approval", severity="high", body=body)

    content = _discord_safe_content(f"[DriftScribe/approval/high] {body}")
    assert len(content) <= _DISCORD_CONTENT_LIMIT
    assert _APPROVAL_URL in content, "the operator got a notification with nothing to click"

    # ``_APPROVAL_URL`` here is the bare URL (the renderer adds the ``<...>``
    # autolink brackets itself), unlike the unit-test fixture which is already
    # wrapped — hence no slicing.
    html = MarkdownIt().enable("linkify").render(content)
    assert f'href="{_APPROVAL_URL}"' in html, (
        "the approval URL survived the cut but does not render as a link — "
        "present, visible, and unclickable is not delivery"
    )
