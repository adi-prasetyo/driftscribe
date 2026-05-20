from agent.secret_guard import redact_text


def test_redact_text_strips_url_userinfo():
    assert redact_text("connect to postgres://u:p@host/db now") == \
        "connect to postgres://<redacted>@host/db now"


def test_redact_text_passes_through_plain_text():
    assert redact_text("no secrets here") == "no secrets here"


def test_redact_text_handles_multiple():
    s = "a postgres://u:p@h1 then mysql://x:y@h2 done"
    out = redact_text(s)
    assert "u:p@" not in out and "x:y@" not in out
