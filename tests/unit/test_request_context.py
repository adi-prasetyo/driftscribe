"""Tests for agent.request_context — autonomy-mode contextvar with Token-based reset."""


def test_get_current_autonomy_mode_default_is_observe():
    from agent.request_context import get_current_autonomy_mode
    assert get_current_autonomy_mode() == "observe"


def test_autonomy_mode_scope_sets_and_resets():
    from agent.request_context import autonomy_mode_scope, get_current_autonomy_mode
    assert get_current_autonomy_mode() == "observe"
    with autonomy_mode_scope("propose_apply"):
        assert get_current_autonomy_mode() == "propose_apply"
    assert get_current_autonomy_mode() == "observe"


def test_autonomy_mode_scope_resets_even_on_exception():
    from agent.request_context import autonomy_mode_scope, get_current_autonomy_mode
    try:
        with autonomy_mode_scope("propose_apply"):
            raise ValueError("boom")
    except ValueError:
        pass
    assert get_current_autonomy_mode() == "observe"


def test_autonomy_mode_scope_nested():
    from agent.request_context import autonomy_mode_scope, get_current_autonomy_mode
    with autonomy_mode_scope("propose"):
        assert get_current_autonomy_mode() == "propose"
        with autonomy_mode_scope("propose_apply"):
            assert get_current_autonomy_mode() == "propose_apply"
        assert get_current_autonomy_mode() == "propose"
    assert get_current_autonomy_mode() == "observe"


def test_stale_propose_apply_does_not_leak_to_next_run():
    """Contextvar resets between runs — stale propose_apply from a prior scope
    does NOT trigger dispatch in a later scope. This is the critical correctness
    test from the plan (Codex Medium finding)."""
    from agent.request_context import autonomy_mode_scope, get_current_autonomy_mode
    with autonomy_mode_scope("propose_apply"):
        assert get_current_autonomy_mode() == "propose_apply"
    # After exit, default is restored
    assert get_current_autonomy_mode() == "observe"
    # Second run in same thread/task — should NOT see propose_apply
    with autonomy_mode_scope("observe"):
        assert get_current_autonomy_mode() == "observe"


def test_is_demo_anonymous_default_is_false():
    from agent.request_context import is_demo_anonymous
    assert is_demo_anonymous() is False


def test_demo_anonymous_scope_sets_and_resets():
    from agent.request_context import demo_anonymous_scope, is_demo_anonymous
    assert is_demo_anonymous() is False
    with demo_anonymous_scope(True):
        assert is_demo_anonymous() is True
    assert is_demo_anonymous() is False


def test_demo_anonymous_scope_resets_even_on_exception():
    from agent.request_context import demo_anonymous_scope, is_demo_anonymous
    try:
        with demo_anonymous_scope(True):
            raise ValueError("boom")
    except ValueError:
        pass
    assert is_demo_anonymous() is False
