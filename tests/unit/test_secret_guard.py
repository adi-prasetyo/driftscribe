import pytest

from agent.secret_guard import redact_event, redact_text


# --------------------------------------------------------------------------- #
# M3: redact bare secret VALUES by distinctive shape (not just credentialed
# URLs / secret-NAMED keys). A model thought-summary or reply quoting a raw
# AIza.../ghp_.../github_pat_.../JWT token would otherwise reach durable logs /
# rendered surfaces unredacted. Only high-signal, low-false-positive prefixes.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("secret", [
    "<GOOGLE_API_KEY_EXAMPLE>",   # Google API key (AIza + 35)
    "<GITHUB_CLASSIC_PAT_EXAMPLE>",     # GitHub classic PAT
    "<GITHUB_FINE_GRAINED_PAT_EXAMPLE>",
    "<JWT_EXAMPLE>",   # JWT
])
def test_redact_text_masks_shaped_tokens(secret):
    out = redact_text(f"here is {secret} end")
    assert secret not in out
    assert "here is" in out and "end" in out


def test_redact_text_leaves_ordinary_words():
    s = "the quick brown fox jumps over 12345"
    assert redact_text(s) == s


def test_redact_event_masks_shaped_token_nested():
    payload = {"reply": "key is <GITHUB_CLASSIC_PAT_EXAMPLE> here"}
    out = redact_event(payload)
    assert "<GITHUB_CLASSIC_PAT_EXAMPLE>" not in out["reply"]


def test_redact_text_strips_url_userinfo():
    assert redact_text("connect to postgres://u:p@host/db now") == \
        "connect to postgres://<redacted>@host/db now"


def test_redact_text_passes_through_plain_text():
    assert redact_text("no secrets here") == "no secrets here"


def test_redact_text_handles_multiple():
    s = "a postgres://u:p@h1 then mysql://x:y@h2 done"
    out = redact_text(s)
    assert "u:p@" not in out and "x:y@" not in out


def test_redact_event_recurses_into_nested_dicts():
    payload = {"outer": {"inner": "postgres://u:p@h/d"}}
    out = redact_event(payload)
    assert out == {"outer": {"inner": "postgres://<redacted>@h/d"}}


def test_redact_event_allowlists_metadata():
    payload = {"trace_id": "abc"}
    assert redact_event(payload) == {"trace_id": "abc"}


def test_redact_event_lists():
    payload = {"errors": ["postgres://u:p@h"]}
    out = redact_event(payload)
    assert out == {"errors": ["postgres://<redacted>@h"]}


def test_redact_event_key_aware_takes_precedence():
    payload = {"DATABASE_URL": "anything"}
    assert redact_event(payload) == {"DATABASE_URL": "<redacted>"}


def test_redact_event_passes_through_numbers():
    payload = {"prompt_token_count": 42, "latency_ms": 17.5, "doc_count": 0}
    assert redact_event(payload) == {
        "prompt_token_count": 42,
        "latency_ms": 17.5,
        "doc_count": 0,
    }


def test_redact_event_secret_named_container_redacts_whole_value():
    assert redact_event({"PASSWORD": {"raw": "abc"}}) == {"PASSWORD": "<redacted>"}
    assert redact_event({"AUTH": {"header": "Bearer abc"}}) == {"AUTH": "<redacted>"}
    assert redact_event({"API_KEY": ["k1", "k2"]}) == {"API_KEY": "<redacted>"}


def test_redact_event_allowlist_string_still_userinfo_stripped():
    # Defense-in-depth: even allowlisted metadata keys get credentialed
    # URLs stripped — the allowlist promises "no secrets," but a future
    # caller could violate that and we don't want a silent leak.
    assert redact_event({"tool_name": "postgres://u:p@h/d"}) == {
        "tool_name": "postgres://<redacted>@h/d"
    }


def test_redact_event_depth_limit_returns_sentinel():
    # Build a 70-deep nested dict. Plain dict recursion would otherwise
    # be fine, but this exercises the depth-guard sentinel so a 200+
    # pathological MCP response never throws RecursionError inside the
    # logging framework.
    deep: dict = {"leaf": "bottom"}
    for _ in range(70):
        deep = {"n": deep}
    out = redact_event(deep)
    # Walk down ``out`` and confirm the sentinel string appears before
    # we hit the leaf — i.e. the guard fired.
    cur: object = out
    hit_sentinel = False
    for _ in range(200):
        if cur == "<redacted:depth>":
            hit_sentinel = True
            break
        if isinstance(cur, dict) and "n" in cur:
            cur = cur["n"]
            continue
        break
    assert hit_sentinel, f"depth guard did not fire; final cur={cur!r}"
