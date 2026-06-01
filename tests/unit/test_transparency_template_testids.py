"""Stable selector contract for the Playwright UI E2E (post-Svelte-refresh).

The transparency UI is now a Svelte+Vite SPA (frontend/src/**), served via a thin
shell at GET /ui/transparency. The Playwright spec (tests/e2e/ui/transparency.spec.ts)
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
    "final-response",
    "past-decisions-pane",
    "past-decision-item",
    "open-trace-button",
    "historical-banner",
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


def test_three_reasoning_groups_present():
    """The three reasoning groups must exist as Group instances keyed
    coordinator/tools/mcp (Timeline.svelte), each rendered as a real
    <details id="group-{key}"> with a child <div data-group="{key}"> (Group.svelte).
    The Playwright spec sets `.open = true` on #group-tools and asserts
    [data-group="tools"] becomes visible — runtime-verified by the smoke."""
    timeline = (_FRONTEND_SRC / "components/Timeline.svelte").read_text()
    for key in ("coordinator", "tools", "mcp"):
        assert f'key="{key}"' in timeline, f"Timeline missing Group key={key!r}"
    group = (_FRONTEND_SRC / "components/Group.svelte").read_text()
    assert "data-group={key}" in group, "Group.svelte must render data-group={key}"
    assert "group-${key}" in group, "Group.svelte must render id=group-{key}"


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
