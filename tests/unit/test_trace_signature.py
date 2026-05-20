"""Unit tests for ``agent.main._signature_of`` (Phase 19.A.6).

The signature is what drives observed-stability completion in the
``/trace/{trace_id}`` endpoint. Three Codex review rounds tightened
its shape:

* Codex v3 IMPORTANT: a weaker ``(count, last_(timestamp, insert_id))``
  signature missed same-count replacement / reorder cases. Fix: hash
  every event's ``(timestamp, insert_id, event)`` tuple.
* Codex v3.1 MINOR: use JSON-encoded tuples (not a separator-joined
  string) so delimiters inside a field can't produce digest
  collisions.

These tests pin the SHA-256-over-JSON shape directly so a future
refactor that "simplifies" to a weaker signature is caught here,
not in the field after a flaky-completion incident.
"""

from __future__ import annotations

import hashlib
import json

from agent.main import _signature_of


def _reference_signature(events: list[dict]) -> str:
    """Re-compute the signature the way the production code does.

    Pin shape: SHA-256 over JSON-encoded ``[timestamp, insert_id,
    event]`` tuples with compact separators (no whitespace).
    """
    h = hashlib.sha256()
    for e in events:
        h.update(
            json.dumps(
                [
                    e.get("timestamp", ""),
                    e.get("insert_id", ""),
                    e.get("event", ""),
                ],
                separators=(",", ":"),
            ).encode()
        )
    return h.hexdigest()


def test_signature_matches_reference_shape():
    """Pin the exact SHA-256-over-JSON shape. A future refactor that
    swaps to a weaker representation (e.g. a hex32 of the count) will
    fail here."""
    events = [
        {
            "timestamp": "2026-05-21T00:00:00Z",
            "insert_id": "ins-1",
            "event": "llm_thought",
        },
        {
            "timestamp": "2026-05-21T00:00:01Z",
            "insert_id": "ins-2",
            "event": "final_response",
        },
    ]
    assert _signature_of(events) == _reference_signature(events)


def test_signature_detects_same_count_reorder():
    """Codex v3 IMPORTANT regression guard: two events at the same
    timestamps but with insert_ids swapped MUST produce different
    signatures. A ``(count, last_(timestamp, insert_id))`` signature
    misses this because the count is unchanged and the SORTED tail
    looks identical."""
    ts = "2026-05-21T00:00:00Z"
    a = [
        {"timestamp": ts, "insert_id": "ins-a", "event": "llm_thought"},
        {"timestamp": ts, "insert_id": "ins-b", "event": "final_response"},
    ]
    b = [
        {"timestamp": ts, "insert_id": "ins-b", "event": "final_response"},
        {"timestamp": ts, "insert_id": "ins-a", "event": "llm_thought"},
    ]
    assert _signature_of(a) != _signature_of(b)


def test_signature_detects_same_count_replacement():
    """``max_results`` clipping the tail of the result page could keep
    the count unchanged across two polls while swapping one event for
    another. The signature must detect this — pin via a synthetic
    "page-clipped" case."""
    base = [
        {
            "timestamp": "2026-05-21T00:00:00Z",
            "insert_id": "ins-1",
            "event": "llm_thought",
        },
        {
            "timestamp": "2026-05-21T00:00:01Z",
            "insert_id": "ins-2",
            "event": "tool_call",
        },
    ]
    replaced = [
        base[0],
        {
            "timestamp": "2026-05-21T00:00:01Z",
            "insert_id": "ins-3",  # different insert_id
            "event": "tool_call",
        },
    ]
    assert _signature_of(base) != _signature_of(replaced)


def test_signature_stable_for_identical_inputs():
    events = [
        {
            "timestamp": "2026-05-21T00:00:00Z",
            "insert_id": "ins-1",
            "event": "llm_thought",
        }
    ]
    assert _signature_of(events) == _signature_of(events)
    # And re-building the dicts (different object identity, same
    # content) yields the same digest.
    events2 = [dict(events[0])]
    assert _signature_of(events) == _signature_of(events2)


def test_signature_uses_json_encoding_not_delimiter_join():
    """Codex v3.1 MINOR: pin that a payload containing the chosen
    separator character can't collide with a shifted-field payload.

    Construct two timelines that would alias under a naive
    ``"|".join(...)`` signature but differ under JSON encoding.
    """
    # Under a "|"-delimited signature, both timelines below would
    # encode to ``"a|b|c|d|e|f"`` (the timestamp containing "|"
    # consumes a delimiter), yielding identical digests.
    # JSON encoding preserves the field boundary so the digests
    # diverge.
    aliased_a = [
        {"timestamp": "a|b", "insert_id": "c", "event": "d|e|f"},
    ]
    aliased_b = [
        {"timestamp": "a", "insert_id": "b|c|d", "event": "e|f"},
    ]
    assert _signature_of(aliased_a) != _signature_of(aliased_b)


def test_signature_empty_events_is_stable():
    """No events ⇒ empty digest (the hex of SHA-256 over zero bytes)."""
    assert _signature_of([]) == hashlib.sha256().hexdigest()


def test_signature_missing_fields_default_to_empty_string():
    """Events without ``timestamp``/``insert_id``/``event`` keys still
    produce a digest (the dict ``.get(..., "")`` defaults flow through
    the JSON encoder as empty strings)."""
    events = [{}, {"event": "llm_thought"}]
    # Should not raise.
    digest = _signature_of(events)
    assert isinstance(digest, str)
    assert len(digest) == 64  # SHA-256 hex
