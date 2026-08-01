"""Stable selector contract for the Playwright UI E2E (post-Svelte-refresh).

The transparency UI is now a Svelte+Vite SPA (frontend/src/**), served via a thin
shell at GET / (the site root). The Playwright spec (tests/e2e/ui/transparency.spec.ts)
keys off ``data-testid`` attributes + a few element ids, so this meta-test pins
that those selectors exist IN THE SVELTE SOURCE (the rendered DOM is verified at
runtime by the mock-Playwright smoke in frontend/tests/smoke and by the cloud
e2e). The approval pages remain server-rendered Jinja, so their testids are still
checked against the template file.

Adding/removing a testid here must be done in lockstep with the Playwright
selector list (tests/e2e/ui) — otherwise a UI rename quietly orphans the test.
"""
from pathlib import Path

_FRONTEND_SRC = Path("frontend/src")

REQUIRED_TESTIDS_TRANSPARENCY = {
    "chat-prompt",
    "chat-submit",
    # A chat reply settles into the conversation thread and carries its own
    # reasoning disclosure; there is no standalone "final-response" hero any
    # more (ds-jns Task 3.3 deleted it, along with the decisions rail and the
    # page-level replay it fed).
    "conversation-thread",
    "reasoning-disclosure",
    "turn-time",
    # The front door of a fresh chat, and the one link off it.
    "chat-empty-chip",
    "capability-link",
    "rail-new-chat",
    # Every route to a past decision ends here: a row on the desk's ledger,
    # opening that decision as a record.
    "approval-desk",
    "ledger-strip-row",
    "decision-record",
}


def _frontend_source() -> str:
    """Concatenated text of every Svelte/TS source file under frontend/src."""
    parts = []
    for path in sorted(_FRONTEND_SRC.rglob("*")):
        if path.suffix in {".svelte", ".ts"} and path.is_file():
            parts.append(path.read_text())
    return "\n".join(parts)


def test_transparency_source_has_required_testids():
    body = _frontend_source()
    missing = [
        tid
        for tid in REQUIRED_TESTIDS_TRANSPARENCY
        if f'data-testid="{tid}"' not in body
    ]
    assert not missing, f"missing data-testids in frontend/src: {missing}"


def test_reasoning_rows_are_interleaved_not_grouped():
    """Reasoning renders as ONE ordered list of rows, not three sibling panels.

    Until ds-jns Task 3.3 this pinned the opposite shape: a Timeline.svelte with
    three Group instances keyed coordinator/tools/mcp, each a
    <details id="group-{key}"> wrapping a <div data-group="{key}">. Both
    components are deleted. A turn's reasoning now hangs off the turn itself and
    reads in RUN order -- what the coordinator thought, then what it called --
    which is the point of the redesign and is why the bins had to go.

    Pinned here because it is a claim about a SHAPE that a passing render test
    cannot make: TraceDetail would render perfectly happily if someone re-binned
    its rows by kind."""
    detail = (_FRONTEND_SRC / "components/TraceDetail.svelte").read_text()
    for row in ("trace-row-thought", "trace-row-tool", "trace-row-mcp"):
        assert f'data-testid="{row}"' in detail, f"TraceDetail missing {row!r}"
    # One list, driven by the single interleaving function -- not three.
    assert "interleaveTimeline" in detail, "TraceDetail must render interleaveTimeline's rows"
    assert not list(_FRONTEND_SRC.glob("components/Group.svelte")), "Group.svelte is retired"
    assert not list(_FRONTEND_SRC.glob("components/Timeline.svelte")), "Timeline.svelte is retired"


def test_sessionstorage_key_documented():
    """Playwright seeds sessionStorage['driftscribe_token'] (underscore form)."""
    body = _frontend_source()
    assert "driftscribe_token" in body  # underscore, NOT dot


def test_workload_option_values_present():
    """The workload <select> values are the /chat API contract. Re-homed from the
    old served-HTML grep; the canonical list lives in frontend/src/lib/workloads.ts
    (also covered by vitest workloads.test.ts)."""
    workloads = (_FRONTEND_SRC / "lib/workloads.ts").read_text()
    for value in ("drift", "upgrade", "explore", "provision"):
        assert f"'{value}'" in workloads, f"workloads.ts missing value {value!r}"


def test_approval_template_has_testids():
    """Approval pages remain server-rendered Jinja form-POST — testids stay in
    the template file."""
    body = Path("agent/templates/approval.html").read_text()
    for tid in ("approve-button", "reject-button", "token-field"):
        assert (
            f'data-testid="{tid}"' in body
        ), f"approval.html missing data-testid={tid!r}"


def test_approval_pages_link_shared_css_no_inline_style():
    """P5b: both approval pages link the built design-system CSS (via the
    ds_css_href() Jinja global) and ship NO inline <style> — the IaC approval
    CSP is ``style-src 'self'`` (no 'unsafe-inline'), so an inline block would
    be blocked. This guard is what makes the P5b restyle non-optional."""
    for name in ("approval.html", "iac_approval.html"):
        body = Path(f"agent/templates/{name}").read_text()
        assert "ds_css_href()" in body, f"{name} must link the built CSS"
        assert "<style" not in body, f"{name} must not ship an inline <style>"
        assert 'class="ds-' in body, f"{name} must use ds-* classes"


def test_approval_reason_preserves_its_own_line_breaks():
    """ds-thm: the rollback reason is model-authored prose and may carry
    DriftScribe's own "[… N characters omitted …]" marker on its own blank
    line. In an ordinary text node those newlines collapse to a single space
    and the marker reads as part of the sentence it interrupts.

    Three things are pinned, because each was a way to get this wrong:

    1. the value is wrapped in the ``ds-prewrap`` element at all;
    2. the rule lives in the BUILT stylesheet. Both approval templates are held
       to "no inline style" by the test above; that convention exists because
       the IaC approval page is served under ``style-src 'self'`` with no
       ``'unsafe-inline'``. This page (the rollback approval) carries no CSP
       today, so an inline style here would break the convention rather than be
       blocked — worth stating precisely, since "the CSP forbids it" would be
       an overclaim on this template;
    3. ``{{ approval.reason }}`` sits flush against its tags. Under
       ``pre-wrap``, template indentation is no longer invisible — a newline
       and two spaces before the expression would render as real leading
       whitespace on the page.
    """
    tpl = Path("agent/templates/approval.html").read_text()
    assert '<span class="ds-prewrap">{{ approval.reason }}</span>' in tpl, (
        "the reason must be wrapped flush inside ds-prewrap — any whitespace "
        "between the tag and the expression becomes visible under pre-wrap"
    )

    css = Path("frontend/src/styles/base.css").read_text()
    block = css[css.index(".ds-field > .ds-prewrap"):]
    block = block[: block.index("}")]
    assert "white-space: pre-wrap" in block
    # .ds-field is a flex row; without these the pre-wrap child refuses to
    # shrink and overflows the card instead of wrapping inside it.
    assert "min-width: 0" in block
    assert "flex: 1 1 0" in block


def test_approval_pages_preserve_form_post_and_hidden_token():
    """P5b must not regress the form-POST security flow: real method=post forms
    + hidden CSRF token fields with the token-field testid."""
    rollback = Path("agent/templates/approval.html").read_text()
    assert 'method="post"' in rollback
    assert 'name="t"' in rollback and 'data-testid="token-field"' in rollback
    iac = Path("agent/templates/iac_approval.html").read_text()
    assert 'method="post"' in iac
    assert 'name="form_token"' in iac and 'data-testid="token-field"' in iac
