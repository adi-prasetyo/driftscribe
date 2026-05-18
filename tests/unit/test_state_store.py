from agent.state_store import InMemoryStateStore


def test_record_event_first_call_returns_true():
    s = InMemoryStateStore()
    assert s.record_event("ev-1", {"trigger": "manual"}) is True


def test_record_event_duplicate_returns_false():
    s = InMemoryStateStore()
    assert s.record_event("ev-1", {"trigger": "manual"}) is True
    assert s.record_event("ev-1", {"trigger": "manual"}) is False


def test_find_decision_for_event_before_decision_recorded_returns_none():
    s = InMemoryStateStore()
    s.record_event("ev-1", {})
    assert s.find_decision_for_event("ev-1") is None


def test_record_decision_cross_references_event():
    s = InMemoryStateStore()
    s.record_event("ev-1", {})
    s.record_decision("dec-1", "ev-1", {"action": "drift_issue"})
    assert s.find_decision_for_event("ev-1") == {"action": "drift_issue"}


def test_get_decision_returns_recorded_decision():
    s = InMemoryStateStore()
    s.record_event("ev-1", {})
    s.record_decision("dec-1", "ev-1", {"action": "drift_issue"})
    assert s.get_decision("dec-1") == {"action": "drift_issue"}


def test_get_decision_for_unknown_id_returns_none():
    s = InMemoryStateStore()
    assert s.get_decision("missing") is None


def test_find_decision_for_unknown_event_returns_none():
    s = InMemoryStateStore()
    assert s.find_decision_for_event("missing") is None


def test_release_event_allows_re_claim():
    s = InMemoryStateStore()
    assert s.record_event("ev-1", {}) is True
    s.release_event("ev-1")
    assert s.record_event("ev-1", {}) is True


def test_release_event_is_noop_for_unknown_key():
    s = InMemoryStateStore()
    s.release_event("never-claimed")  # must not raise
