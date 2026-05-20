from agent.secret_guard import redact_event, redact_text


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
