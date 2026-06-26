"""Unit tests for renderer.scrub_pr_body — the serve-time scrub applied to a
PR body before it is cached/served in the open-trace "what this change did"
disclosure.

The body is agent-authored markdown (low secret risk), but belt-and-braces:
strip credentialed-URL userinfo (redact_text) and any rollback approval ``?t=``
token (redact_approval_tokens_deep). Conventions mirror the other scrubbers:
None / non-str / empty pass through; never raises.
"""
from __future__ import annotations

from agent.renderer import scrub_pr_body


def test_none_passes_through():
    assert scrub_pr_body(None) is None


def test_non_str_passes_through():
    assert scrub_pr_body(123) == 123


def test_empty_passes_through():
    assert scrub_pr_body("") == ""


def test_clean_body_unchanged_value():
    body = "## Repoints payment-demo\n\nWhy: completes the C5f isolation.\n"
    assert scrub_pr_body(body) == body


def test_strips_credentialed_url_userinfo():
    body = "See https://user:secretpass@example.com/repo for details."
    out = scrub_pr_body(body)
    assert "secretpass" not in out
    assert "<redacted>@example.com" in out


def test_strips_rollback_approval_token():
    body = "Roll back at https://x.run.app/approvals/abc123?t=SUPERSECRETTOKEN now."
    out = scrub_pr_body(body)
    assert "SUPERSECRETTOKEN" not in out
    assert "/approvals/abc123?t=<redacted>" in out
