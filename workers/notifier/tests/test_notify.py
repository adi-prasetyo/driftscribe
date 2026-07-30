"""Tests for the Notifier Agent worker (Phase 11.6).

The Notifier is the simplest of the four workers: ``POST /notify`` takes
``{channel, severity, body}``, builds a normalized payload, and posts it to
the *single* webhook URL configured at boot via Secret Manager. The caller
cannot supply or override the URL — Layer 2 enforcement is that ``url`` is
not a field on the schema, and ``extra="forbid"`` rejects any attempt to
sneak it in (a textbook confused-deputy attempt).

Coverage:

- Happy path: well-formed body → 200, outbound POST goes to the
  env-configured URL with the expected normalized payload.
- Confused-deputy: caller supplies a ``url`` field → 422, no outbound POST.
- Invalid channel / severity / oversized body / empty body → 422.
- Webhook 5xx → 502; webhook connection error → 502.
- Missing bearer / caller not in allowlist → 401 / 403 (delegated to
  ``verify_caller``).
- ``/healthz`` is unauthenticated.
- Real ``_verify_caller_dep`` is wired with OWN_URL + ALLOWED_CALLERS read
  from env at boot (mirror of reader/docs/rollback Layer 3 integration
  check — same Codex review #4 rationale).
"""
import os

import pytest
from fastapi import HTTPException, status
from fastapi.testclient import TestClient

# Env MUST be set before importing workers.notifier.main — the module reads
# OWN_URL / ALLOWED_CALLERS / GCP_PROJECT / NOTIFY_WEBHOOK_URL at import
# time and raises if any is missing. This mirrors the production fail-fast
# behavior.
os.environ.setdefault("GCP_PROJECT", "test-proj")
os.environ.setdefault("OWN_URL", "https://notifier.example.com")
os.environ.setdefault(
    "ALLOWED_CALLERS",
    "coordinator@test-proj.iam.gserviceaccount.com",
)
os.environ.setdefault("NOTIFY_WEBHOOK_URL", "https://webhook.example.com/test")

from workers.notifier import main as notifier_main  # noqa: E402
from workers.notifier.main import _verify_caller_dep, app  # noqa: E402


# --------------------------------------------------------------------------- #
# httpx stubs
# --------------------------------------------------------------------------- #


class _FakeResp:
    """Minimal httpx.Response stand-in for the happy path."""

    status_code = 200
    text = "ok"


class _FakeClient:
    """Captures the outbound POST so tests can assert on URL + payload.

    Stored as a class attribute (``_FakeClient.last``) because the worker
    constructs its own ``httpx.Client()`` instance inside ``/notify`` —
    we can't reach in and read the instance state from the test. A class
    attribute is the simplest channel; pytest's monkeypatch resets between
    tests so this doesn't leak across test cases (each test gets a fresh
    monkeypatch and our fixture re-patches).
    """

    last: dict = {}

    def __init__(self, *args, **kwargs):
        # Reset on each instantiation so a test that creates a client but
        # never POSTs doesn't observe a stale capture from a prior test.
        _FakeClient.last = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, json):
        _FakeClient.last = {"url": url, "json": json}
        return _FakeResp()


@pytest.fixture
def client(monkeypatch):
    """Build a TestClient with httpx stubbed and auth bypassed.

    Patches ``httpx.Client`` as it's bound inside ``workers.notifier.main``
    — not the source in the ``httpx`` package — for the same reason the
    reader patches ``workers.reader.main.read_live_state``: the worker
    imported ``httpx`` and now has its own binding (``httpx.Client``
    resolves through the module's ``httpx`` name).
    """
    _FakeClient.last = {}
    monkeypatch.setattr(notifier_main.httpx, "Client", _FakeClient)
    app.dependency_overrides[_verify_caller_dep] = (
        lambda: "coordinator@test-proj.iam.gserviceaccount.com"
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Happy path + Layer 2 (payload-intent policy) tests
# --------------------------------------------------------------------------- #


def test_notify_happy_path(client):
    """Well-formed body posts to the env-configured URL and returns 200."""
    r = client.post(
        "/notify",
        json={"channel": "info", "severity": "low", "body": "hello"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "sent"
    assert body["channel"] == "info"
    assert body["severity"] == "low"
    assert body["downstream_status"] == 200
    # The outbound URL is the env-configured one — NOT the caller's input.
    assert _FakeClient.last["url"] == "https://webhook.example.com/test"
    # The outbound payload is the worker's normalized dict, not the
    # caller's raw body verbatim.
    payload = _FakeClient.last["json"]
    assert payload["service"] == "DriftScribe"
    assert payload["channel"] == "info"
    assert payload["severity"] == "low"
    assert "hello" in payload["text"]
    assert "hello" in payload["content"]


# --------------------------------------------------------------------------- #
# Destination compatibility: Discord ``content`` + the 2000-char cap
# --------------------------------------------------------------------------- #


def test_payload_carries_content_for_discord(client):
    """Discord renders ``content`` and ignores ``text``.

    A payload carrying only ``text`` is refused by Discord with
    ``400 {"message": "Cannot send an empty message", "code": 50006}``
    (verified live against a real webhook, 2026-07-30). Both keys ship so one
    payload serves Discord, Slack and a generic viewer.
    """
    r = client.post(
        "/notify",
        json={
            "channel": "approval",
            "severity": "high",
            "body": "rollback proposed",
        },
    )
    assert r.status_code == 200, r.text
    payload = _FakeClient.last["json"]
    assert "rollback proposed" in payload["content"]
    assert "rollback proposed" in payload["text"]


def test_short_content_is_identical_to_text(client):
    """Below the cap, no truncation machinery should engage at all."""
    r = client.post(
        "/notify",
        json={"channel": "info", "severity": "low", "body": "short"},
    )
    assert r.status_code == 200, r.text
    payload = _FakeClient.last["json"]
    assert payload["content"] == payload["text"]


def test_oversize_content_is_capped_but_text_is_not(client):
    """Discord hard-rejects >2000 chars; it does not truncate for us.

    ``text`` stays uncapped on purpose — the 2000 limit is Discord's, and
    imposing it on Slack or a generic receiver would lose detail for no reason.
    """
    body = "x" * 9000
    r = client.post(
        "/notify",
        json={"channel": "approval", "severity": "high", "body": body},
    )
    assert r.status_code == 200, r.text
    payload = _FakeClient.last["json"]
    assert len(payload["content"]) <= 2000
    assert len(payload["text"]) > 2000
    assert body in payload["text"]


def test_truncation_keeps_the_tail_where_the_approval_link_lives(client):
    """The middle is dropped, never the tail.

    This is the regression guard for the specific silent failure this feature
    invites: a naive ``content[:2000]`` truncates away the approval URL, which
    sits at the END of the rendered body — and only does so for long bodies,
    i.e. exactly the messy drifts where the operator needs the link most.
    """
    tail = "APPROVE-HERE-https://example.test/approvals/abc?t=zzz"
    body = ("R" * 9000) + tail
    r = client.post(
        "/notify",
        json={"channel": "approval", "severity": "high", "body": body},
    )
    assert r.status_code == 200, r.text
    content = _FakeClient.last["json"]["content"]
    assert len(content) <= 2000
    assert content.endswith(tail)
    assert content.startswith("[DriftScribe/approval/high] RRR")
    # Prove the naive implementation really would have dropped it, so this
    # test fails if someone "simplifies" the helper back to a head slice.
    assert tail not in f"[DriftScribe/approval/high] {body}"[:2000]


def _unfenced_len() -> int:
    """Output length when no fence repair is needed (the common path)."""
    return (
        notifier_main._TRUNCATION_HEAD_BUDGET
        + len(notifier_main._TRUNCATION_MARKER)
        + notifier_main._TRUNCATION_TAIL_BUDGET
    )


def test_content_at_exactly_the_limit_is_untouched():
    """Boundary: 2000 passes through verbatim, 2001 is truncated."""
    at_limit = "y" * 2000
    assert notifier_main._discord_safe_content(at_limit) == at_limit
    over = notifier_main._discord_safe_content("y" * 2001)
    assert len(over) <= notifier_main._DISCORD_CONTENT_LIMIT
    # Plain text has no fence to close, so the reserved allowance goes unused.
    assert len(over) == _unfenced_len()


def test_truncation_budgets_are_internally_consistent():
    """A non-positive head budget would corrupt output instead of raising.

    ``text[:negative]`` slices from the END in Python, so a mis-set budget
    emits a mangled notification silently. The module fails at import if this
    breaks; the invariant is stated here too so the arithmetic is pinned.
    """
    assert notifier_main._TRUNCATION_HEAD_BUDGET > 0
    assert (
        notifier_main._TRUNCATION_HEAD_BUDGET
        + len(notifier_main._TRUNCATION_MARKER)
        + notifier_main._TRUNCATION_TAIL_BUDGET
        + len(notifier_main._FENCE_CLOSE)
        == notifier_main._DISCORD_CONTENT_LIMIT
    )


def test_the_marker_does_not_promise_a_decision_record():
    """``/notify`` is generic — callers exist that create no decision record.

    Naming one in the marker sends an operator hunting for something that
    never existed.
    """
    assert "decision record" not in notifier_main._TRUNCATION_MARKER


# --------------------------------------------------------------------------- #
# Markdown safety: an open code fence would swallow the approval link
# --------------------------------------------------------------------------- #


def _fences_before(content: str, needle: str) -> int:
    """Count fence delimiters preceding ``needle``.

    An EVEN count means ``needle`` renders outside a code block, which is the
    difference between a clickable link and an inert one. Counts RUNS of 3+
    backticks rather than "```" substrings, because a six-backtick run is one
    delimiter and substring counting would score it as a harmless two.
    """
    return notifier_main._fence_delimiters(content[: content.index(needle)])


def test_an_open_code_fence_in_the_head_is_closed():
    """Regression guard for the Codex finding on the first cut of this change.

    If the model's rationale opens a ``` block whose closing fence lands in the
    DELETED middle, the retained tail renders inside a code block: the approval
    URL is present and visible, but not clickable. Preserving the URL string is
    not the same as preserving a usable link.
    """
    link = "https://example.test/approvals/abc?t=zzz"
    body = "opening a block:\n```\n" + ("code line\n" * 900) + "\n```\ntail " + link
    content = notifier_main._discord_safe_content(
        f"[DriftScribe/approval/high] {body}"
    )
    assert len(content) <= 2000
    assert link in content
    assert _fences_before(content, link) % 2 == 0, (
        "the approval URL is inside an unterminated code block"
    )


def test_an_orphan_closing_fence_in_the_tail_is_removed():
    """The second artifact truncation creates, and the one I first dismissed.

    When the block's OPENER falls in the deleted middle, the surviving closer
    would instead OPEN a block in the tail and swallow everything after it.
    ``/notify`` is generic — notify_tool passes arbitrary model-authored
    text — so the tail is not always the rollback template's fence-free footer.
    """
    link = "https://example.test/approvals/abc?t=zzz"
    body = "intro\n```\n" + ("code line\n" * 900) + "```\nsee " + link
    content = notifier_main._discord_safe_content(
        f"[DriftScribe/approval/high] {body}"
    )
    assert len(content) <= 2000
    assert link in content
    assert _fences_before(content, link) % 2 == 0


def test_a_balanced_head_is_not_given_a_spurious_fence():
    """Only an ODD count triggers repair; balanced Markdown is left alone."""
    link = "https://example.test/approvals/abc?t=zzz"
    body = "```\nfenced\n```\n" + ("R" * 9000) + " tail " + link
    content = notifier_main._discord_safe_content(
        f"[DriftScribe/approval/high] {body}"
    )
    assert len(content) <= 2000
    # No repair on either side, so the reserved fence allowance is unused.
    assert len(content) == _unfenced_len()
    assert _fences_before(content, link) % 2 == 0


def test_fence_closing_never_pushes_output_over_the_limit():
    """The allowance is reserved unconditionally, so both branches fit."""
    for body in ("```\n" + "x" * 9000, "x" * 9000):
        content = notifier_main._discord_safe_content(
            f"[DriftScribe/approval/high] {body}"
        )
        assert len(content) <= 2000


def test_a_six_backtick_run_is_one_delimiter_not_two():
    """``count("```")`` reports 2 for a 6-backtick run and repairs nothing.

    It is a single delimiter that OPENS a block, so the tail would be
    swallowed. Counting runs of 3+ backticks gets this right — the concrete
    case from the second Codex pass.
    """
    assert notifier_main._fence_delimiters("``````") == 1
    assert notifier_main._fence_delimiters("```") == 1
    assert notifier_main._fence_delimiters("``` ```") == 2
    assert notifier_main._fence_delimiters("`` ``") == 0, "inline spans are not fences"

    link = "https://example.test/approvals/abc?t=zzz"
    body = "open\n``````\n" + ("R" * 9000) + "\nsee " + link
    content = notifier_main._discord_safe_content(
        f"[DriftScribe/approval/high] {body}"
    )
    assert len(content) <= 2000
    assert link in content
    assert notifier_main._fence_delimiters(
        content[: content.index(link)]
    ) % 2 == 0


# --------------------------------------------------------------------------- #
# Mention suppression
# --------------------------------------------------------------------------- #


def test_mentions_are_suppressed(client):
    """The body carries LLM-authored text and live env values.

    Under this project's compromised-model threat model, a rationale
    containing ``@everyone`` must not page a server on the strength of text
    the model chose. Probed live 2026-07-30: without this key a mention
    RESOLVES; with ``parse: []`` it does not.
    """
    r = client.post(
        "/notify",
        json={
            "channel": "approval",
            "severity": "high",
            "body": "@everyone rollback proposed <@1472813135273529485>",
        },
    )
    assert r.status_code == 200, r.text
    assert _FakeClient.last["json"]["allowed_mentions"] == {"parse": []}


def test_mention_suppression_is_sent_on_every_notification(client):
    """Not only for the approval channel — any body can contain a mention."""
    for channel, severity in (("info", "low"), ("alert", "medium")):
        r = client.post(
            "/notify",
            json={"channel": channel, "severity": severity, "body": "hi"},
        )
        assert r.status_code == 200, r.text
        assert _FakeClient.last["json"]["allowed_mentions"] == {"parse": []}


# --------------------------------------------------------------------------- #
# A reflecting destination must not put the approval token in our logs
# --------------------------------------------------------------------------- #


def test_reflected_approval_token_is_scrubbed_from_the_error_snippet():
    """webhook.site / httpbin.org/post reflect the request body verbatim.

    Discord does not, but the destination is configuration — the guarantee
    must not rest on which vendor is set today.
    """
    reflected = (
        '{"json": {"content": "click '
        "https://driftscribe.example/approvals/abc"
        '?t=driftscribe-fixture-approval-token-not-real"}}'
    )
    scrubbed = notifier_main._scrub_approval_tokens(reflected)
    assert "driftscribe-fixture-approval-token-not-real" not in scrubbed
    assert "[redacted]" in scrubbed


def test_token_scrubbing_survives_truncation_order():
    """Scrub happens BEFORE the 200-char cut.

    Cutting first could leave a token fragment too short for the pattern to
    match, which would leak the prefix of a live credential.
    """
    prefix = "E" * 150
    raw = f"{prefix}https://x.test/approvals/a?t=" + "A" * 60
    snippet = notifier_main._scrub_approval_tokens(raw[:1000])[:200]
    assert "AAAAAAAAAAAAAAAAAAAA" not in snippet


def test_scrubbing_leaves_ordinary_error_text_readable():
    """The snippet's diagnostic value is why it exists — don't gut it."""
    body = '{"message": "Cannot send an empty message", "code": 50006}'
    assert notifier_main._scrub_approval_tokens(body) == body


@pytest.mark.parametrize(
    "reflection",
    [
        # Verbatim, the case the context rule already covered.
        "https://x.test/approvals/a?t={tok}",
        # Percent-encoded by a proxy — evades a `?t=` context match, but the
        # token is still a usable credential once decoded.
        "https://x.test/approvals/a%3Ft%3D{tok}",
        # Unicode-escaped inside a JSON echo.
        '{{"u": "https://x.test/approvals/a\\u003ft\\u003d{tok}"}}',
        # Token as a path segment rather than a query parameter.
        "https://x.test/approvals/{tok}",
        # Bare, with no URL around it at all.
        "echo: {tok}",
    ],
)
def test_the_token_is_scrubbed_regardless_of_how_it_is_reflected(reflection):
    """A context rule alone is evadable; the shape rule is the net.

    All five of these were verified by Codex against the context-only version;
    the last four got through. The minter emits ``[A-Za-z0-9_-]{43,64}``, so
    matching the shape survives any re-encoding that preserves the token's own
    characters.
    """
    tok = "driftscribe-fixture-approval-token-not-real"
    assert len(tok) == 43, "fixture must match the real minted token length"
    scrubbed = notifier_main._scrub_approval_tokens(reflection.format(tok=tok))
    assert tok not in scrubbed
    assert "[redacted]" in scrubbed


def test_the_shape_rule_does_not_eat_short_identifiers():
    """Below the minted length, ordinary ids stay readable in the log."""
    body = '{"request_id": "abc123-def456", "trace": "0123456789abcdef"}'
    assert notifier_main._scrub_approval_tokens(body) == body


def test_caller_url_field_rejected(client):
    """Confused-deputy attempt: caller tries to supply a ``url`` field.

    ``extra="forbid"`` on the request schema makes pydantic raise
    ``ValidationError`` before the handler runs, which FastAPI surfaces
    as 422. The outbound httpx call must NOT have happened — the
    captured outbound dict stays empty.
    """
    r = client.post(
        "/notify",
        json={
            "channel": "info",
            "severity": "low",
            "body": "hello",
            "url": "https://attacker.example.com/exfil",
        },
    )
    assert r.status_code == 422
    assert _FakeClient.last == {}, (
        "outbound POST must not happen when schema rejects the request"
    )


def test_caller_extra_arbitrary_field_rejected(client):
    """Schema closure check: any unexpected field is refused, not just url."""
    r = client.post(
        "/notify",
        json={
            "channel": "info",
            "severity": "low",
            "body": "hello",
            "priority": "max",  # not in schema
        },
    )
    assert r.status_code == 422
    assert _FakeClient.last == {}


def test_invalid_channel_rejected(client):
    """Channel constrained to info|alert|approval via ``Literal``."""
    r = client.post(
        "/notify",
        json={"channel": "private-attack-channel", "severity": "low", "body": "x"},
    )
    assert r.status_code == 422
    assert _FakeClient.last == {}


def test_valid_channels_all_accepted(client):
    """Sanity-check that the three allowed channels do work."""
    for ch in ("info", "alert", "approval"):
        r = client.post(
            "/notify",
            json={"channel": ch, "severity": "low", "body": "x"},
        )
        assert r.status_code == 200, f"channel={ch} should be accepted"


def test_invalid_severity_rejected(client):
    """Severity constrained to low|medium|high|critical via ``Literal``."""
    r = client.post(
        "/notify",
        json={"channel": "info", "severity": "lol", "body": "x"},
    )
    assert r.status_code == 422
    assert _FakeClient.last == {}


def test_valid_severities_all_accepted(client):
    for sev in ("low", "medium", "high", "critical"):
        r = client.post(
            "/notify",
            json={"channel": "info", "severity": sev, "body": "x"},
        )
        assert r.status_code == 200, f"severity={sev} should be accepted"


def test_oversize_body_rejected(client):
    """Body length cap is 10000; anything larger is refused at the schema."""
    r = client.post(
        "/notify",
        json={"channel": "info", "severity": "low", "body": "x" * 20000},
    )
    assert r.status_code == 422
    assert _FakeClient.last == {}


def test_at_limit_body_accepted(client):
    """Exactly 10000 chars is at the cap and must be accepted."""
    r = client.post(
        "/notify",
        json={"channel": "info", "severity": "low", "body": "x" * 10000},
    )
    assert r.status_code == 200


def test_empty_body_rejected(client):
    """``min_length=1`` keeps the payload meaningful."""
    r = client.post(
        "/notify",
        json={"channel": "info", "severity": "low", "body": ""},
    )
    assert r.status_code == 422


def test_missing_channel_rejected(client):
    """All three fields are required."""
    r = client.post(
        "/notify",
        json={"severity": "low", "body": "x"},
    )
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Downstream-webhook failure-mode tests
# --------------------------------------------------------------------------- #


def test_webhook_5xx_returns_502(client, monkeypatch):
    """Non-2xx from the downstream webhook → 502 with the status surfaced."""

    class _FailResp:
        status_code = 500
        text = "downstream broken"

    class _FailClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json):
            return _FailResp()

    monkeypatch.setattr(notifier_main.httpx, "Client", _FailClient)
    r = client.post(
        "/notify",
        json={"channel": "info", "severity": "low", "body": "x"},
    )
    assert r.status_code == 502
    assert "500" in r.json()["detail"]


def test_webhook_4xx_returns_502(client, monkeypatch):
    """4xx from the downstream is also "this notification didn't land" → 502."""

    class _FailResp:
        status_code = 404
        text = "no such hook"

    class _FailClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json):
            return _FailResp()

    monkeypatch.setattr(notifier_main.httpx, "Client", _FailClient)
    r = client.post(
        "/notify",
        json={"channel": "info", "severity": "low", "body": "x"},
    )
    assert r.status_code == 502


def test_webhook_connect_error_returns_502(client, monkeypatch):
    """``httpx.RequestError`` (incl. ConnectError / TimeoutException) → 502."""
    import httpx

    class _TimeoutClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json):
            raise httpx.ConnectError("nope")

    monkeypatch.setattr(notifier_main.httpx, "Client", _TimeoutClient)
    r = client.post(
        "/notify",
        json={"channel": "info", "severity": "low", "body": "x"},
    )
    assert r.status_code == 502
    assert "unavailable" in r.json()["detail"].lower()


# --------------------------------------------------------------------------- #
# Layer 3 (inter-service auth) tests
# --------------------------------------------------------------------------- #


def test_missing_bearer_returns_401(client):
    def deny_401():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
        )

    app.dependency_overrides[_verify_caller_dep] = deny_401
    r = client.post(
        "/notify",
        json={"channel": "info", "severity": "low", "body": "x"},
    )
    assert r.status_code == 401


def test_caller_not_in_allowlist_returns_403(client):
    def deny_caller():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="caller service account not allowed",
        )

    app.dependency_overrides[_verify_caller_dep] = deny_caller
    r = client.post(
        "/notify",
        json={"channel": "info", "severity": "low", "body": "x"},
    )
    assert r.status_code == 403


def test_healthz_does_not_require_auth(client):
    """``/healthz`` has no Depends, so even a denying override doesn't fire."""

    def boom():
        raise HTTPException(status_code=401, detail="should not be called")

    app.dependency_overrides[_verify_caller_dep] = boom
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_real_verify_caller_dep_wired_with_env(monkeypatch):
    """Layer 3 integration check (mirror of reader/docs/rollback).

    Without ``dependency_overrides`` the real ``_verify_caller_dep`` must
    call ``verify_caller`` with OWN_URL + ALLOWED_CALLERS read from env
    at boot. We monkeypatch the module-level constants rather than relying
    on the import-time env read because, in a unified pytest run, another
    worker's test module may have populated ``OWN_URL`` before this module
    was imported (Python caches the import; ``os.environ.setdefault`` at
    the top of this file would then be a no-op).
    """
    seen = {}

    def fake_verify(request, *, own_url, allowed_callers):
        seen["own_url"] = own_url
        seen["allowed_callers"] = set(allowed_callers)
        return "coordinator@test-proj.iam.gserviceaccount.com"

    monkeypatch.setattr(notifier_main, "verify_caller", fake_verify)
    monkeypatch.setattr(notifier_main, "OWN_URL", "https://notifier.example.com")
    monkeypatch.setattr(
        notifier_main,
        "ALLOWED_CALLERS",
        frozenset({"coordinator@test-proj.iam.gserviceaccount.com"}),
    )
    # Stub httpx too so the request doesn't try to hit the network.
    monkeypatch.setattr(notifier_main.httpx, "Client", _FakeClient)

    # No dependency_overrides — exercise the real _verify_caller_dep.
    notifier_main.app.dependency_overrides.clear()
    c = TestClient(app)
    r = c.post(
        "/notify",
        json={"channel": "info", "severity": "low", "body": "hello"},
        headers={"Authorization": "Bearer faketoken"},
    )
    assert r.status_code == 200, r.text
    assert seen["own_url"] == "https://notifier.example.com"
    assert seen["allowed_callers"] == {
        "coordinator@test-proj.iam.gserviceaccount.com",
    }
