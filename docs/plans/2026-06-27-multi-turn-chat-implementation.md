# Multi-Turn Persisted Chat — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or
> superpowers:subagent-driven-development) to implement this plan task-by-task.

**Goal:** Make DriftScribe chat multi-turn and persisted — conversations survive
reloads, each is locked to one crew, and a crew sees its own prior turns; plus
the HTTP surface to list/rehydrate them.

**Architecture:** Replay-into-fresh-session (design doc
`docs/plans/2026-06-27-multi-turn-chat-and-team-memory-design.md`). On each
`POST /chat` we resolve/create a `conversations/{id}` record in the existing
`StateStore`, load prior turns, seed them as ADK events into the fresh per-call
`InMemorySessionService`, run, then persist the new user+crew turns. Persistence
happens at the **endpoint layer** so all four crews (incl. provision fan-out) are
covered without double-writes; the JSON-others path keeps calling `run_chat`
(pinned by tests) with persistence wrapped around it.

**Tech Stack:** FastAPI, `google-adk==1.33.0` (`InMemorySessionService`,
`append_event`, `Event`/`Content`/`Part`), Firestore (`google.cloud.firestore`,
`@firestore.transactional`), pytest, Svelte 5 (P2).

**Phasing:** This plan is **P1 = the full backend** (StateStore + `/chat` wiring +
GET endpoints + tests). P2 (frontend thread/rail) and P3 (cross-crew
`read_conversations` tool + breadcrumb) are outlined at the end as separate PRs.

**Working dir:** `/home/adi/driftscribe/.worktrees/multi-turn-chat-p1` (branch
`feat/multi-turn-chat-p1`). Run tests with `uv run pytest`, lint with
`uv run ruff check .`.

---

## Design invariants (do not violate)

- **Crew-lock is server-enforced.** A `conversation_id` that resolves to a
  different `workload` than the request → `409`. Unknown id → `404` (never
  silently fork a new thread). Absent id → server creates one.
- **Seed event authorship.** User turns → `Event(author="user", role="user")`.
  Crew turns → `Event(author=<the built agent's .name>, role="model")`. Any other
  author makes ADK `contents.py` rewrite the event into a `"For context: …"`
  user message (verified in `google/adk/flows/llm_flows/contents.py:573,585-611`).
- **No double-persist.** Persistence lives ONLY at the endpoint layer. The
  provision fan-out's internal delegation to `run_chat_stream` is invisible to the
  endpoint wrapper, so it persists exactly once.
- **`run_chat` stays on the JSON-others path.** `test_provision_fanout_route.py`
  and `tests/integration/test_chat_endpoint.py` pin that drift JSON calls
  `agent.adk_agent.run_chat`. Do not reroute it; wrap persistence around it.
- **Fail-soft writes.** A Firestore write failure must log and NOT break the reply
  already produced. Crew-lock *reads* are NOT fail-soft (a failed lookup must not
  silently bypass the lock — let it surface).
- **Additive only.** New `conversations` collection; `conversation_id` optional on
  `ChatRequest`. Omitting it preserves today's one-shot behavior. No migration.
- **Preserve `session_id` passthrough.** The caller-supplied ADK `session_id` is
  still forwarded to `run_chat`/`run_chat_stream` unchanged (pinned by
  `test_chat_endpoint.py:81-97`). `conversation_id` is a *separate*, new concept;
  do NOT pass `session_id=None`. Memory comes from seeding, not from session reuse.
- **Atomic turn writes + honest id.** The user+crew turn pair is written in ONE
  Firestore transaction (no half-turns). `conversation_id` is returned to the
  client ONLY when persistence succeeded — never echo an id that resolves to
  nothing.
- **Path-safe ids.** `conversation_id` is validated `^[A-Za-z0-9_-]{1,128}$` on
  both POST and GET (Firestore doc ids must not contain `/` or path escapes).

> **Codex review (thread 019f0774) folded in 2026-06-27:** atomic `append_turns`
> (was two separate `append_turn` calls), `session_id` passthrough preserved,
> persist-returns-bool / id-only-on-success, pause-path enforces 404/409,
> path-safe id validation, and the pinned `assert_awaited_once_with` updates.
> Deferred (accepted): provision committed multi-slice path seeds prior turns
> (single-slice/observe/non-policy provision paths ARE seeded; only the N-slice
> authoring decomposition is not — turns still persist regardless).

---

## Data model

`conversations/{conversation_id}` (one document):

| field | type | notes |
|---|---|---|
| `conversation_id` | str | = doc id |
| `workload` | str | locked crew: `drift`/`upgrade`/`explore`/`provision` |
| `title` | str | first user prompt, sanitized + truncated ~60 chars |
| `created_at` | timestamp | |
| `updated_at` | timestamp | bumped each turn; rail sorts by this |
| `turn_count` | int | next seq to allocate |
| `last_trace_id` | str \| None | most recent turn's trace |

`conversations/{conversation_id}/turns/{seq:06d}` (subcollection):

| field | type | notes |
|---|---|---|
| `seq` | int | 0-based allocation order |
| `role` | str | `user` or `crew` |
| `text` | str | the message text |
| `workload` | str | the crew at the time (== conversation workload) |
| `trace_id` | str \| None | links the crew turn to `/trace/{trace_id}` |
| `created_at` | timestamp | |
| `iac_pr` | dict \| None | crew turns only; `{pr_number, pr_url}` |
| `tool_calls` | list \| None | crew turns only; summary |

Subcollection (not an embedded array) keeps turns append-only and dodges the 1 MB
document ceiling.

---

# P1 — Backend

## Task 1: `StateStore` — conversation methods on `InMemoryStateStore`

**Files:**
- Modify: `agent/state_store.py` (Protocol ~lines 33-36; `InMemoryStateStore`
  `__init__` ~line 58 and methods after `list_decisions_for_pr` ~line 160)
- Test: `tests/unit/test_state_store_conversations.py` (create)

**Step 1: Write the failing tests**

Create `tests/unit/test_state_store_conversations.py`:

```python
"""Conversation persistence on the StateStore (P1 multi-turn chat)."""
from datetime import datetime, timezone

import pytest

from agent.state_store import InMemoryStateStore


def _store():
    return InMemoryStateStore()


def test_create_then_get_conversation_round_trips():
    s = _store()
    s.create_conversation("c1", workload="drift", title="why is svc drifting")
    conv = s.get_conversation("c1")
    assert conv is not None
    assert conv["conversation_id"] == "c1"
    assert conv["workload"] == "drift"
    assert conv["title"] == "why is svc drifting"
    assert conv["turn_count"] == 0
    assert conv["turns"] == []
    assert isinstance(conv["created_at"], datetime)


def test_get_unknown_conversation_returns_none():
    assert _store().get_conversation("nope") is None


def test_append_turn_allocates_monotonic_seq_and_orders_turns():
    s = _store()
    s.create_conversation("c1", workload="drift", title="t")
    seq0 = s.append_turn("c1", role="user", text="hello", workload="drift",
                         trace_id="tr-1")
    seq1 = s.append_turn("c1", role="crew", text="hi there", workload="drift",
                         trace_id="tr-1", tool_calls=["read_live_env_tool"])
    assert seq0 == 0 and seq1 == 1
    conv = s.get_conversation("c1")
    assert conv["turn_count"] == 2
    assert [t["seq"] for t in conv["turns"]] == [0, 1]
    assert [t["role"] for t in conv["turns"]] == ["user", "crew"]
    assert conv["turns"][1]["tool_calls"] == ["read_live_env_tool"]
    assert conv["last_trace_id"] == "tr-1"


def test_append_turn_records_iac_pr_on_crew_turn_only():
    s = _store()
    s.create_conversation("c1", workload="provision", title="t")
    s.append_turn("c1", role="user", text="adopt bucket", workload="provision")
    s.append_turn("c1", role="crew", text="opened PR", workload="provision",
                  trace_id="tr", iac_pr={"pr_number": 5, "pr_url": "https://x/5"})
    turns = s.get_conversation("c1")["turns"]
    assert "iac_pr" not in turns[0]
    assert turns[1]["iac_pr"] == {"pr_number": 5, "pr_url": "https://x/5"}


def test_append_turn_unknown_conversation_raises():
    with pytest.raises(KeyError):
        _store().append_turn("ghost", role="user", text="x", workload="drift")


def test_append_turns_pair_is_atomic_and_creates_on_demand():
    s = _store()
    seqs = s.append_turns(
        "c1",
        [
            {"role": "user", "text": "q", "workload": "drift", "trace_id": "tr"},
            {"role": "crew", "text": "a", "workload": "drift", "trace_id": "tr",
             "tool_calls": ["x"]},
        ],
        create_with={"workload": "drift", "title": "q"},
    )
    assert seqs == [0, 1]
    conv = s.get_conversation("c1")
    assert conv["title"] == "q"
    assert conv["turn_count"] == 2
    assert conv["last_trace_id"] == "tr"
    assert [t["role"] for t in conv["turns"]] == ["user", "crew"]


def test_append_turns_without_create_with_on_missing_raises():
    with pytest.raises(KeyError):
        _store().append_turns("ghost", [{"role": "user", "text": "x",
                                         "workload": "drift"}])


def test_list_conversations_newest_first_and_limited():
    s = _store()
    for i in range(3):
        s.create_conversation(f"c{i}", workload="drift", title=f"t{i}")
        s.append_turn(f"c{i}", role="user", text="x", workload="drift")
    rows = s.list_conversations(limit=2)
    assert len(rows) == 2
    # most-recently-updated first; c2 was created/updated last
    assert rows[0]["conversation_id"] == "c2"
    # list rows are metadata only — no embedded turns
    assert "turns" not in rows[0]


def test_list_conversations_filters_by_workload():
    s = _store()
    s.create_conversation("d", workload="drift", title="t")
    s.create_conversation("p", workload="provision", title="t")
    rows = s.list_conversations(workload="provision")
    assert [r["conversation_id"] for r in rows] == ["p"]
```

**Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_state_store_conversations.py -q`
Expected: FAIL — `InMemoryStateStore` has no `create_conversation`.

**Step 3: Add the Protocol stubs**

In `agent/state_store.py`, in the `StateStore` Protocol after
`list_decisions_for_pr` (~line 36), add:

```python
    def create_conversation(
        self, conversation_id: str, *, workload: str, title: str
    ) -> dict[str, Any]: ...
    def append_turn(
        self,
        conversation_id: str,
        *,
        role: str,
        text: str,
        workload: str,
        trace_id: str | None = None,
        iac_pr: dict[str, Any] | None = None,
        tool_calls: list[Any] | None = None,
    ) -> int: ...
    def append_turns(
        self,
        conversation_id: str,
        turns: list[dict[str, Any]],
        *,
        create_with: dict[str, Any] | None = None,
    ) -> list[int]: ...
    def get_conversation(
        self, conversation_id: str
    ) -> dict[str, Any] | None: ...
    def list_conversations(
        self, *, limit: int = 50, workload: str | None = None
    ) -> list[dict[str, Any]]: ...
```

**Step 4: Implement on `InMemoryStateStore`**

Add to `__init__` (after `self._autonomy = None`, ~line 58):

```python
        # conversation_id -> conversation doc (metadata only)
        self._conversations: dict[str, dict[str, Any]] = {}
        # conversation_id -> ordered list of turn docs
        self._conversation_turns: dict[str, list[dict[str, Any]]] = {}
```

Add methods after `list_decisions_for_pr` (~line 160):

```python
    def create_conversation(
        self, conversation_id: str, *, workload: str, title: str
    ) -> dict[str, Any]:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        doc = {
            "conversation_id": conversation_id,
            "workload": workload,
            "title": title,
            "created_at": now,
            "updated_at": now,
            "turn_count": 0,
            "last_trace_id": None,
        }
        self._conversations[conversation_id] = doc
        self._conversation_turns.setdefault(conversation_id, [])
        return dict(doc)

    def append_turn(
        self,
        conversation_id: str,
        *,
        role: str,
        text: str,
        workload: str,
        trace_id: str | None = None,
        iac_pr: dict[str, Any] | None = None,
        tool_calls: list[Any] | None = None,
    ) -> int:
        # Thin single-turn wrapper over the atomic append_turns.
        return self.append_turns(
            conversation_id,
            [{
                "role": role, "text": text, "workload": workload,
                "trace_id": trace_id, "iac_pr": iac_pr, "tool_calls": tool_calls,
            }],
        )[0]

    def append_turns(
        self,
        conversation_id: str,
        turns: list[dict[str, Any]],
        *,
        create_with: dict[str, Any] | None = None,
    ) -> list[int]:
        from datetime import datetime, timezone

        conv = self._conversations.get(conversation_id)
        if conv is None:
            if create_with is None:
                raise KeyError(f"conversation {conversation_id!r} not found")
            self.create_conversation(conversation_id, **create_with)
            conv = self._conversations[conversation_id]
        start = int(conv["turn_count"])
        now = datetime.now(timezone.utc)
        last_trace = conv.get("last_trace_id")
        seqs: list[int] = []
        for i, t in enumerate(turns):
            seq = start + i
            turn = {
                "seq": seq,
                "role": t["role"],
                "text": t.get("text") or "",
                "workload": t["workload"],
                "trace_id": t.get("trace_id"),
                "created_at": now,
            }
            if t.get("iac_pr"):
                turn["iac_pr"] = t["iac_pr"]
            if t.get("tool_calls"):
                turn["tool_calls"] = t["tool_calls"]
            self._conversation_turns.setdefault(conversation_id, []).append(turn)
            if t.get("trace_id"):
                last_trace = t["trace_id"]
            seqs.append(seq)
        conv["turn_count"] = start + len(turns)
        conv["updated_at"] = now
        conv["last_trace_id"] = last_trace
        return seqs

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        conv = self._conversations.get(conversation_id)
        if conv is None:
            return None
        out = dict(conv)
        turns = sorted(
            self._conversation_turns.get(conversation_id, []),
            key=lambda t: t.get("seq", 0),
        )
        out["turns"] = [dict(t) for t in turns]
        return out

    def list_conversations(
        self, *, limit: int = 50, workload: str | None = None
    ) -> list[dict[str, Any]]:
        from datetime import datetime, timezone

        sentinel = datetime.min.replace(tzinfo=timezone.utc)
        rows = [
            dict(c)
            for c in self._conversations.values()
            if workload is None or c.get("workload") == workload
        ]
        rows.sort(key=lambda c: c.get("updated_at") or sentinel, reverse=True)
        return rows[:limit]
```

**Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_state_store_conversations.py -q`
Expected: PASS (9 tests).

**Step 6: Commit**

```bash
git add agent/state_store.py tests/unit/test_state_store_conversations.py
git commit -m "feat(state): conversation persistence on InMemoryStateStore"
```

---

## Task 2: `StateStore` — conversation methods on `FirestoreStateStore`

**Files:**
- Modify: `agent/state_store.py` (`FirestoreStateStore.__init__` ~line 233;
  methods after `list_decisions_for_pr` ~line 432)
- Test: `tests/unit/test_firestore_conversations.py` (create)

The Firestore impl mirrors the InMemory contract but allocates `seq` in a
`@firestore.transactional` block (read `turn_count` → write turn + bump doc
atomically), unlike `record_decision`'s `WriteBatch` (its ids are pre-known; ours
is not). Pattern copied from the existing `evict_cached_decision` transaction
(`state_store.py:319-342`).

**Step 1: Write the failing test (fake Firestore client)**

Create `tests/unit/test_firestore_conversations.py`. Use a minimal in-process
fake that records `set`/`update`/`get`/`stream` and supports the
`@firestore.transactional` protocol so the seq-allocation logic is exercised
without a live backend:

```python
"""FirestoreStateStore conversation methods over a fake client.

Verifies the transactional seq allocation + doc/subcollection shape without a
live Firestore. The fake implements just enough of the client surface the impl
touches (document/collection/get/set/update/stream/transaction).
"""
import pytest

from agent.state_store import FirestoreStateStore


class _Snap:
    def __init__(self, data, create_time=None):
        self._data = data
        self.exists = data is not None
        self.create_time = create_time

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _DocRef:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    def collection(self, name):
        return _CollRef(self._store, f"{self._path}/{name}")

    def get(self, transaction=None):
        data = self._store.docs.get(self._path)
        ct = self._store.create_times.get(self._path)
        return _Snap(data, ct)

    def set(self, data, merge=False):
        import itertools
        self._store._tick = next(self._store._counter)
        if merge and self._path in self._store.docs:
            self._store.docs[self._path].update(self._resolve(data))
        else:
            self._store.docs[self._path] = self._resolve(data)
        self._store.create_times.setdefault(self._path, self._store._tick)

    def update(self, data):
        self._store.docs[self._path].update(self._resolve(data))

    def _resolve(self, data):
        # turn SERVER_TIMESTAMP sentinels into a monotonically increasing int
        out = {}
        for k, v in data.items():
            if v is _SERVER_TS:
                self._store._tick = next(self._store._counter)
                out[k] = self._store._tick
            else:
                out[k] = v
        return out


class _CollRef:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    def document(self, doc_id):
        return _DocRef(self._store, f"{self._path}/{doc_id}")

    def where(self, field, op, value):
        return _Query(self._store, self._path, [(field, op, value)])

    def stream(self):
        return _Query(self._store, self._path, []).stream()


class _Query:
    def __init__(self, store, path, filters):
        self._store = store
        self._path = path
        self._filters = filters

    def where(self, field, op, value):
        return _Query(self._store, self._path, self._filters + [(field, op, value)])

    def stream(self):
        for path, data in self._store.docs.items():
            parent, _, _ = path.rpartition("/")
            if parent != self._path:
                continue
            if all(data.get(f) == v for f, op, v in self._filters):
                yield _Snap(data, self._store.create_times.get(path))


class _Txn:
    """Stand-in for google.cloud.firestore transaction object."""
    def __init__(self, store):
        self._store = store

    def get(self, ref, **kw):
        return ref.get()

    def set(self, ref, data):
        ref.set(data)

    def update(self, ref, data):
        ref.update(data)


class _FakeClient:
    def __init__(self):
        import itertools
        self.docs = {}
        self.create_times = {}
        self._counter = itertools.count(1)
        self._tick = 0

    def collection(self, name):
        return _CollRef(self, name)

    def transaction(self):
        return _Txn(self)

    def batch(self):
        raise AssertionError("conversations must not use a batch")


# sentinel matching firestore.SERVER_TIMESTAMP identity in the impl path
import agent.state_store as _ss_mod
_SERVER_TS = object()


@pytest.fixture
def store(monkeypatch):
    """FirestoreStateStore wired to the fake client + a stubbed firestore module.

    The impl does `from google.cloud import firestore` inside each method and
    uses `firestore.SERVER_TIMESTAMP` and `@firestore.transactional`. Patch a
    fake module so those resolve to our sentinel + a pass-through decorator.
    """
    import sys, types as _t

    fake_fs = _t.SimpleNamespace(
        SERVER_TIMESTAMP=_SERVER_TS,
        transactional=lambda fn: fn,  # call inline; fake txn needs no retry loop
    )
    fake_cloud = _t.ModuleType("google.cloud")
    fake_cloud.firestore = fake_fs
    monkeypatch.setitem(sys.modules, "google.cloud.firestore", fake_fs)
    # `from google.cloud import firestore` resolves the attribute on google.cloud
    import google.cloud as gc
    monkeypatch.setattr(gc, "firestore", fake_fs, raising=False)

    return FirestoreStateStore(project="p", client=_FakeClient())


def test_firestore_create_and_get(store):
    store.create_conversation("c1", workload="drift", title="t")
    conv = store.get_conversation("c1")
    assert conv["workload"] == "drift"
    assert conv["turn_count"] == 0
    assert conv["turns"] == []


def test_firestore_append_turn_transactional_seq(store):
    store.create_conversation("c1", workload="drift", title="t")
    assert store.append_turn("c1", role="user", text="a", workload="drift") == 0
    assert store.append_turn("c1", role="crew", text="b", workload="drift",
                             trace_id="tr") == 1
    conv = store.get_conversation("c1")
    assert [t["seq"] for t in conv["turns"]] == [0, 1]
    assert conv["turn_count"] == 2
    assert conv["last_trace_id"] == "tr"


def test_firestore_list_filters_and_limits(store):
    store.create_conversation("d", workload="drift", title="t")
    store.create_conversation("p", workload="provision", title="t")
    assert {c["conversation_id"] for c in store.list_conversations()} == {"d", "p"}
    assert [c["conversation_id"]
            for c in store.list_conversations(workload="provision")] == ["p"]


def test_firestore_append_turns_create_with_in_one_transaction(store):
    # The new-conversation persist path: create doc + both turns atomically.
    seqs = store.append_turns(
        "new",
        [
            {"role": "user", "text": "q", "workload": "drift", "trace_id": "tr"},
            {"role": "crew", "text": "a", "workload": "drift", "trace_id": "tr"},
        ],
        create_with={"workload": "drift", "title": "q"},
    )
    assert seqs == [0, 1]
    conv = store.get_conversation("new")
    assert conv["turn_count"] == 2
    assert conv["title"] == "q"
    assert [t["seq"] for t in conv["turns"]] == [0, 1]
    assert conv["last_trace_id"] == "tr"
```

> Note: if patching `from google.cloud import firestore` proves brittle in the
> fake-client harness, fall back to asserting the InMemory contract here and pin
> the Firestore impl by code review + the shared Protocol; the transaction logic
> is small and mirrors `evict_cached_decision`. Decide during implementation;
> don't spend more than ~15 min fighting the fake.

**Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_firestore_conversations.py -q`
Expected: FAIL — methods missing.

**Step 3: Implement on `FirestoreStateStore`**

In `__init__` after `self._config = client.collection("config")` (~line 233):

```python
        self._conversations = client.collection("conversations")
```

Add methods after `list_decisions_for_pr` (~line 432):

```python
    def create_conversation(
        self, conversation_id: str, *, workload: str, title: str
    ) -> dict[str, Any]:
        from google.cloud import firestore

        doc = {
            "conversation_id": conversation_id,
            "workload": workload,
            "title": title,
            "created_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP,
            "turn_count": 0,
            "last_trace_id": None,
        }
        self._conversations.document(conversation_id).set(doc)
        return doc

    def append_turn(
        self,
        conversation_id: str,
        *,
        role: str,
        text: str,
        workload: str,
        trace_id: str | None = None,
        iac_pr: dict[str, Any] | None = None,
        tool_calls: list[Any] | None = None,
    ) -> int:
        return self.append_turns(
            conversation_id,
            [{
                "role": role, "text": text, "workload": workload,
                "trace_id": trace_id, "iac_pr": iac_pr, "tool_calls": tool_calls,
            }],
        )[0]

    def append_turns(
        self,
        conversation_id: str,
        turns: list[dict[str, Any]],
        *,
        create_with: dict[str, Any] | None = None,
    ) -> list[int]:
        from google.cloud import firestore

        conv_ref = self._conversations.document(conversation_id)

        @firestore.transactional
        def _txn(transaction) -> list[int]:
            # READS FIRST (Firestore txn requires all reads before writes).
            snap = conv_ref.get(transaction=transaction)
            if not snap.exists:
                if create_with is None:
                    raise KeyError(f"conversation {conversation_id!r} not found")
                base = {
                    "conversation_id": conversation_id,
                    "workload": create_with["workload"],
                    "title": create_with["title"],
                    "created_at": firestore.SERVER_TIMESTAMP,
                    "last_trace_id": None,
                }
                start, last_trace, is_create = 0, None, True
            else:
                data = snap.to_dict() or {}
                start = int(data.get("turn_count", 0))
                last_trace = data.get("last_trace_id")
                base, is_create = {}, False
            # WRITES.
            seqs: list[int] = []
            for i, t in enumerate(turns):
                seq = start + i
                turn = {
                    "seq": seq,
                    "role": t["role"],
                    "text": t.get("text") or "",
                    "workload": t["workload"],
                    "trace_id": t.get("trace_id"),
                    "created_at": firestore.SERVER_TIMESTAMP,
                }
                if t.get("iac_pr"):
                    turn["iac_pr"] = t["iac_pr"]
                if t.get("tool_calls"):
                    turn["tool_calls"] = t["tool_calls"]
                transaction.set(
                    conv_ref.collection("turns").document(f"{seq:06d}"), turn
                )
                if t.get("trace_id"):
                    last_trace = t["trace_id"]
                seqs.append(seq)
            doc_fields = {
                "turn_count": start + len(turns),
                "updated_at": firestore.SERVER_TIMESTAMP,
                "last_trace_id": last_trace,
            }
            if is_create:
                transaction.set(conv_ref, {**base, **doc_fields})
            else:
                transaction.update(conv_ref, doc_fields)
            return seqs

        return _txn(self._db.transaction())

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        conv_ref = self._conversations.document(conversation_id)
        snap = conv_ref.get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        data.setdefault("created_at", snap.create_time)
        turns = [s.to_dict() or {} for s in conv_ref.collection("turns").stream()]
        turns.sort(key=lambda t: t.get("seq", 0))
        data["turns"] = turns
        return data

    def list_conversations(
        self, *, limit: int = 50, workload: str | None = None
    ) -> list[dict[str, Any]]:
        query = (
            self._conversations.where("workload", "==", workload)
            if workload is not None
            else self._conversations
        )
        snaps = list(query.stream())
        rows: list[dict[str, Any]] = []
        for s in snaps:
            d = s.to_dict() or {}
            d.setdefault("created_at", s.create_time)
            d.setdefault("updated_at", d.get("created_at"))
            rows.append(d)
        rows.sort(key=lambda d: d.get("updated_at") or 0, reverse=True)
        return rows[:limit]
```

**Step 4: Run + commit**

Run: `uv run pytest tests/unit/test_firestore_conversations.py tests/unit/test_state_store_conversations.py -q`
Expected: PASS.

```bash
git add agent/state_store.py tests/unit/test_firestore_conversations.py
git commit -m "feat(state): conversation persistence on FirestoreStateStore (transactional seq)"
```

---

## Task 3: Seed prior turns into the ADK session

**Files:**
- Modify: `agent/adk_agent.py` (`run_chat_stream` ~881-989, `run_chat`
  ~992-1036; add `_seed_event_from_turn` + `MAX_SEED_TURNS` near them)
- Test: `tests/unit/test_chat_seeding.py` (create)

**Step 1: Write the failing tests**

Create `tests/unit/test_chat_seeding.py`:

```python
"""Prior-turn seeding into the per-call ADK session (P1 multi-turn)."""
from types import SimpleNamespace

import pytest

import agent.adk_agent as adk_agent


def test_seed_event_user_turn_uses_user_author():
    ev = adk_agent._seed_event_from_turn(
        {"role": "user", "text": "hello"}, agent_name="driftscribe_chat_drift"
    )
    assert ev.author == "user"
    assert ev.content.role == "user"
    assert ev.content.parts[0].text == "hello"


def test_seed_event_crew_turn_uses_agent_name_and_model_role():
    # CRITICAL: crew turns MUST carry the agent's own name, else ADK rewrites
    # them into "For context: ... said" user messages.
    ev = adk_agent._seed_event_from_turn(
        {"role": "crew", "text": "all clear"}, agent_name="driftscribe_chat_drift"
    )
    assert ev.author == "driftscribe_chat_drift"
    assert ev.content.role == "model"
    assert ev.content.parts[0].text == "all clear"


@pytest.mark.asyncio
async def test_run_chat_stream_seeds_prior_turns_into_session(monkeypatch):
    """run_chat_stream appends prior turns (user, then crew-as-agent) before run."""
    appended = []

    class _RecordingSession:
        def __init__(self):
            self.events = []

    class _RecordingService:
        def __init__(self):
            self._session = _RecordingSession()

        async def create_session(self, **kw):
            return self._session

        async def append_event(self, session, event):
            appended.append((event.author, event.content.role, event.content.parts[0].text))
            return event

    async def _stub_run(*a, **k):
        # minimal final-response event so run_chat_stream produces a reply
        yield SimpleNamespace(
            content=SimpleNamespace(parts=[SimpleNamespace(text="ok", thought=False)]),
            partial=False,
            usage_metadata=None,
            is_final_response=lambda: True,
        )

    monkeypatch.setattr(adk_agent, "InMemorySessionService", _RecordingService)
    monkeypatch.setattr(adk_agent, "load_workload", lambda w: SimpleNamespace())
    monkeypatch.setattr(
        adk_agent, "build_chat_agent",
        lambda res, autonomy_mode: SimpleNamespace(name="driftscribe_chat_drift"),
    )

    class _Runner:
        def __init__(self, **kw):
            pass

        def run_async(self, **kw):
            return _stub_run()

    monkeypatch.setattr(adk_agent, "Runner", _Runner)

    prior = [
        {"role": "user", "text": "first q", "workload": "drift"},
        {"role": "crew", "text": "first a", "workload": "drift"},
    ]
    items = [
        it async for it in adk_agent.run_chat_stream(
            "second q", workload="drift", autonomy_mode="propose_apply",
            prior_turns=prior,
        )
    ]
    assert appended == [
        ("user", "user", "first q"),
        ("driftscribe_chat_drift", "model", "first a"),
    ]
    assert items[-1]["type"] == "result"
```

**Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_chat_seeding.py -q`
Expected: FAIL — `_seed_event_from_turn` missing; `run_chat_stream` has no
`prior_turns` param.

**Step 3: Implement**

In `agent/adk_agent.py`, add the `Event` import near the other ADK imports
(~line 94):

```python
from google.adk.events import Event
```

Add near the chat helpers (above `run_chat_stream`, ~line 880):

```python
# Cap how many prior turns we replay into a fresh session — bounds prompt cost.
# ~10 exchanges. Older turns are dropped with a single marker line.
MAX_SEED_TURNS = 20


def _seed_event_from_turn(turn: dict, *, agent_name: str) -> Event:
    """Build an ADK event replaying ONE stored turn into a fresh session.

    User turns are authored "user"; crew turns are authored with the *current
    agent's name* so ADK renders them as model turns (any other author makes
    ``contents.py`` rewrite them into "For context: ... said" user messages).
    """
    text = turn.get("text") or ""
    if turn.get("role") == "user":
        return Event(
            author="user",
            content=types.Content(role="user", parts=[types.Part(text=text)]),
        )
    return Event(
        author=agent_name,
        content=types.Content(role="model", parts=[types.Part(text=text)]),
    )
```

Add `prior_turns` to `run_chat_stream`'s signature (keyword-only, default
`None`):

```python
async def run_chat_stream(
    prompt: str,
    session_id: str | None = None,
    *,
    workload: str = "drift",
    autonomy_mode: str,
    prior_turns: list[dict] | None = None,
):
```

Capture the session and seed, replacing the discard at lines 913-917:

```python
    session = await session_service.create_session(
        app_name="driftscribe",
        user_id="driftscribe-runtime",
        session_id=sid,
    )
    turns_to_seed = list(prior_turns or [])
    if len(turns_to_seed) > MAX_SEED_TURNS:
        omitted = len(turns_to_seed) - MAX_SEED_TURNS
        turns_to_seed = turns_to_seed[-MAX_SEED_TURNS:]
        await session_service.append_event(
            session,
            Event(
                author="user",
                content=types.Content(
                    role="user",
                    parts=[types.Part(
                        text=f"[{omitted} earlier turn(s) omitted for brevity]"
                    )],
                ),
            ),
        )
    for _t in turns_to_seed:
        await session_service.append_event(
            session, _seed_event_from_turn(_t, agent_name=agent.name)
        )
```

Thread `prior_turns` through `run_chat` (signature + the inner call):

```python
async def run_chat(
    prompt: str,
    session_id: str | None = None,
    *,
    workload: str = "drift",
    autonomy_mode: str,
    prior_turns: list[dict] | None = None,
) -> dict:
    ...
    async for item in run_chat_stream(
        prompt, session_id=session_id, workload=workload,
        autonomy_mode=autonomy_mode, prior_turns=prior_turns,
    ):
```

**Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/test_chat_seeding.py -q`
Expected: PASS (3 tests).

**Step 5: Commit**

```bash
git add agent/adk_agent.py tests/unit/test_chat_seeding.py
git commit -m "feat(chat): seed prior turns into the per-call ADK session"
```

---

## Task 4: Thread `prior_turns` through the provision fan-out

**Files:**
- Modify: `agent/fanout.py` (`run_provision_fanout_stream` ~1097-1235)

**Step 1: Add the param + thread to delegated calls**

Add `prior_turns: list[dict] | None = None` (keyword-only) to
`run_provision_fanout_stream`, and pass it to each of the THREE delegated
`run_chat_stream` calls (observe ~1193, non-policy fail-open ~1222, single-slice
~1231):

```python
async def run_provision_fanout_stream(
    prompt: str,
    session_id: str | None = None,
    *,
    autonomy_mode: str,
    prior_turns: list[dict] | None = None,
) -> AsyncIterator[dict]:
    ...
        async for item in run_chat_stream(
            prompt, sid, workload="provision", autonomy_mode=autonomy_mode,
            prior_turns=prior_turns,
        ):
            yield item
        return
```

(Do this in all three delegation sites.) The committed multi-slice path seeds via
the decompose prompt — that is the **deferred sub-item** (P1 stretch / P3); turns
still PERSIST from day one via the endpoint layer regardless.

**Step 2: Verify existing fanout tests still pass**

Run: `uv run pytest tests/unit/test_fanout_orchestrator.py tests/unit/test_provision_fanout_route.py -q`
Expected: PASS (after Task 6 updates the stubs; if run before, the route test may
need the stub kwarg — see Task 6).

**Step 3: Commit** (fold into Task 5's commit if small.)

```bash
git add agent/fanout.py
git commit -m "feat(chat): pass prior_turns through provision fan-out delegation"
```

---

## Task 5: `/chat` endpoint wiring — resolve, seed, persist

**Files:**
- Modify: `agent/main.py` — `ChatRequest` (~5307-5340); `/chat` handler
  (~5578-5759); `_chat_stream` (~5443-5470); `_drain_chat_stream_result`
  (~5473-5499); `_chat_sse` (~5502-5575); `_paused_chat_response` (~5403); add
  helpers near `get_state` / the chat helpers.
- Test: `tests/unit/test_chat_conversations_endpoint.py` (create) — see Task 7.

**Step 1: Add `conversation_id` to `ChatRequest`** (~line 5338, after
`session_id`). Pattern-validate it (Firestore doc ids must not contain `/` or
path escapes; server-generated ids are UUIDs, so this only constrains client
echoes):

```python
    conversation_id: str | None = Field(
        default=None, max_length=128, pattern=r"^[A-Za-z0-9_-]{1,128}$"
    )
```

Update the docstring to note `conversation_id` is the durable thread id (vs the
inert ADK `session_id`, which is still accepted + forwarded unchanged).

**Step 2: Add helpers** near `get_state` (after line 389) — confirm `uuid` and a
module logger are imported at the top of `main.py`; add `import uuid` and/or a
`_log = logging.getLogger(__name__)` if absent (grep first):

```python
def _derive_conversation_title(prompt: str) -> str:
    """First-prompt title: sanitize control/bidi + truncate. No LLM call."""
    from agent.adk_tools import _team_log_sanitize

    return _team_log_sanitize(prompt, 60) or "(untitled)"


def _resolve_chat_conversation(
    state: "StateStore", conversation_id: str | None, workload: str
) -> dict:
    """Resolve the conversation for a /chat turn (crew-lock enforced).

    Absent id  -> new conversation (created lazily at persist time).
    Unknown id -> 404 (never silently fork on a typo / stale client).
    Crew-lock mismatch -> 409.
    Returns {conversation_id, workload, is_new, prior_turns}.
    """
    if conversation_id is None:
        return {
            "conversation_id": str(uuid.uuid4()),
            "workload": workload,
            "is_new": True,
            "prior_turns": [],
        }
    conv = state.get_conversation(conversation_id)
    if conv is None:
        raise HTTPException(
            status_code=404,
            detail="conversation not found",
            headers={"Cache-Control": "no-store"},
        )
    if conv.get("workload") != workload:
        raise HTTPException(
            status_code=409,
            detail=(
                f"conversation is locked to crew {conv.get('workload')!r}; "
                f"start a new chat to talk to {workload!r}"
            ),
            headers={"Cache-Control": "no-store"},
        )
    return {
        "conversation_id": conversation_id,
        "workload": workload,
        "is_new": False,
        "prior_turns": conv.get("turns", []),
    }


def _persist_chat_turn(
    state: "StateStore", *, conv: dict, prompt: str, trace_id: str | None,
    result: dict,
) -> bool:
    """Atomically append the user+crew turn pair. Fail-soft.

    Returns True iff the pair persisted — the caller attaches conversation_id to
    the response ONLY on True, so we never hand the client an id that resolves to
    nothing. The whole exchange (incl. lazy creation for a new conversation) is
    one transaction, so there are no half-turns.
    """
    try:
        turns = [
            {"role": "user", "text": prompt, "workload": conv["workload"],
             "trace_id": trace_id},
            {"role": "crew", "text": result.get("reply") or "",
             "workload": conv["workload"], "trace_id": trace_id,
             "iac_pr": result.get("iac_pr"),
             "tool_calls": result.get("tool_calls")},
        ]
        create_with = (
            {"workload": conv["workload"],
             "title": _derive_conversation_title(prompt)}
            if conv.get("is_new") else None
        )
        state.append_turns(conv["conversation_id"], turns, create_with=create_with)
        conv["is_new"] = False
        return True
    except Exception:  # noqa: BLE001 — reply already produced; never break it
        _log.warning("chat_turn_persist_failed", exc_info=True)
        return False


async def _persisting_chat_stream(
    workload: str, prompt: str, conv: dict, trace_id: str | None,
    session_id: str | None, *, autonomy_mode: str,
):
    """Wrap _chat_stream: seed prior turns in, persist the new turn out.

    Single persist site for the SSE path (all crews) and the JSON provision path
    (both already route through _chat_stream). The fan-out's internal delegation
    to run_chat_stream is invisible here, so we persist exactly once. The
    caller-supplied ADK session_id is forwarded unchanged (separate from
    conversation_id).
    """
    state = get_state()
    async for item in _chat_stream(
        workload, prompt, session_id, autonomy_mode=autonomy_mode,
        prior_turns=conv["prior_turns"],
    ):
        if item.get("type") == "result":
            if _persist_chat_turn(
                state, conv=conv, prompt=prompt, trace_id=trace_id, result=item
            ):
                item = {**item, "conversation_id": conv["conversation_id"]}
        yield item
```

**Step 3: Thread `prior_turns` through `_chat_stream`** (~5443):

```python
def _chat_stream(
    workload: str, prompt: str, session_id: str | None, *, autonomy_mode: str,
    prior_turns: list[dict] | None = None,
):
    if workload == "provision":
        from agent.fanout import run_provision_fanout_stream
        return run_provision_fanout_stream(
            prompt, session_id, autonomy_mode=autonomy_mode,
            prior_turns=prior_turns,
        )
    from agent.adk_agent import run_chat_stream
    return run_chat_stream(
        prompt, session_id=session_id, workload=workload,
        autonomy_mode=autonomy_mode, prior_turns=prior_turns,
    )
```

**Step 4: Pass `conversation_id` through the drains.**

`_drain_chat_stream_result` (~5473) — add passthrough:

```python
            if item.get("iac_pr"):
                out["iac_pr"] = item["iac_pr"]
            if item.get("conversation_id"):
                out["conversation_id"] = item["conversation_id"]
            return out
```

`_chat_sse` (~5502) — add `conv` (keep `session_id` for passthrough), use
`_persisting_chat_stream`, and add `conversation_id` to the `done` frame:

```python
async def _chat_sse(prompt: str, session_id: str | None, conv: dict,
                    workload: str, trace_id: str, *, autonomy_mode: str):
    ...
        async for item in _persisting_chat_stream(
            workload, prompt, conv, trace_id, session_id,
            autonomy_mode=autonomy_mode,
        ):
    ...
                    done_data = {
                        "reply": item["reply"],
                        "tool_calls": item["tool_calls"],
                        "session_id": item["session_id"],
                    }
                    if item.get("iac_pr"):
                        done_data["iac_pr"] = item["iac_pr"]
                    if item.get("conversation_id"):
                        done_data["conversation_id"] = item["conversation_id"]
                    yield _sse_frame(event="done", data=done_data)
```

**Step 5: `_paused_chat_response`** (~5403) — echo `conversation_id` so a paused
turn on an existing thread keeps continuity:

```python
def _paused_chat_response(
    pause: PauseState, *, wants_sse: bool, session_id: str | None,
    conversation_id: str | None = None,
) -> "dict | StreamingResponse":
    ...
    payload = {
        "reply": _paused_chat_reply(pause),
        "tool_calls": [],
        "session_id": session_id or "",
        "paused": True,
    }
    if conversation_id:
        payload["conversation_id"] = conversation_id
```

**Step 6: Wire the `/chat` handler.** After `_eager_resolve_upgrade_contract(resolution)`
(~5681), add the resolve + trace capture:

```python
    state = get_state()
    conv = _resolve_chat_conversation(state, req.conversation_id, req.workload)
    trace_id = current_trace_id_or_new()
```

Update the pause branch (~5621-5626). The pause gate runs BEFORE workload/conv
resolution, but the crew-lock invariant must still hold: if the client supplied a
`conversation_id`, enforce 404/409 even while paused (no new conversation is
created, no turn persists). Then echo the id back:

```python
    pause = _pause_state_fail_closed()
    if pause.paused:
        wants_sse = "text/event-stream" in request.headers.get("accept", "")
        if req.conversation_id is not None:
            # raises 404 (unknown) / 409 (wrong crew); does NOT create or persist
            _resolve_chat_conversation(
                get_state(), req.conversation_id, req.workload
            )
        return _paused_chat_response(
            pause, wants_sse=wants_sse, session_id=req.session_id,
            conversation_id=req.conversation_id,
        )
```

Update the SSE branch (~5691) to use the captured `trace_id` + pass `conv`:

```python
    if wants_sse:
        return StreamingResponse(
            _chat_sse(
                req.prompt, req.session_id, conv, req.workload, trace_id,
                autonomy_mode=autonomy.mode,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Trace-Id": trace_id,
            },
        )
```

Update the JSON provision branch (~5720) and JSON-others branch (~5736):

```python
            if req.workload == "provision":
                return await _drain_chat_stream_result(
                    _persisting_chat_stream(
                        "provision", req.prompt, conv, trace_id, req.session_id,
                        autonomy_mode=autonomy.mode,
                    )
                )
            result = await run_chat(
                req.prompt, session_id=req.session_id, workload=req.workload,
                autonomy_mode=autonomy.mode, prior_turns=conv["prior_turns"],
            )
            if _persist_chat_turn(
                state, conv=conv, prompt=req.prompt, trace_id=trace_id,
                result=result,
            ):
                result["conversation_id"] = conv["conversation_id"]
            return result
```

> Note the JSON-others branch STILL calls `run_chat` with the caller's
> `session_id` (pinned by `test_provision_fanout_route.py` /
> `test_chat_endpoint.py:81-97`); persistence wraps around it and attaches
> `conversation_id` only when the write succeeded.

**Step 7: Run the existing chat tests** (they must stay green; stub kwargs come
in Task 6):

Run: `uv run pytest tests/unit/test_chat_sse.py tests/unit/test_provision_fanout_route.py tests/integration/test_chat_endpoint.py -q`
Expected: After Task 6, PASS. (If run now, expect TypeErrors from stubs lacking
`prior_turns` — that is Task 6.)

**Step 8: Commit**

```bash
git add agent/main.py agent/fanout.py
git commit -m "feat(chat): multi-turn /chat — resolve conversation, seed, persist"
```

---

## Task 6: Update test stubs for the `prior_turns` kwarg

**Files (grep `def _run_chat\|def _stub_stream\|run_chat_stream\|run_provision_fanout_stream` in tests first):**
- `tests/unit/test_chat_sse.py` — `_stub_stream` (~56), `_run_chat` (~139),
  `_boom` (~190)
- `tests/unit/test_provision_fanout_route.py` — `_run_chat` (~201), any
  orchestrator stub
- `tests/integration/test_chat_endpoint.py` — the `run_chat` stub

**Step 1:** Add `prior_turns=None` to each stub signature, e.g.:

```python
async def _stub_stream(prompt, session_id=None, *, workload="drift",
                       autonomy_mode="propose_apply", prior_turns=None):
```

```python
async def _run_chat(prompt, session_id=None, *, workload="drift",
                    autonomy_mode="propose_apply", prior_turns=None):
```

These stubs ignore `prior_turns`; the kwarg only needs to be accepted.

**Step 1b: Update pinned call-signature assertions.** `tests/integration/test_chat_endpoint.py` pins the exact `run_chat` call args; the new `prior_turns` kwarg must be added (a brand-new conversation seeds `[]`). `session_id` stays:

```python
# test_chat_endpoint.py:75 (no session_id sent -> None; new conv -> prior_turns=[])
fake.assert_awaited_once_with(
    "what's the live state?", session_id=None, workload="drift",
    autonomy_mode="propose_apply", prior_turns=[],
)
# test_chat_endpoint.py:95 (session_id="s1" forwarded unchanged)
fake.assert_awaited_once_with(
    "hi", session_id="s1", workload="drift", autonomy_mode="propose_apply",
    prior_turns=[],
)
```

Grep `assert_awaited_once_with\|assert_called_once_with` across the chat tests for
any others (e.g. `test_provision_fanout_route.py`) and add `prior_turns=[]`
(and confirm `session_id` matches what the test posts).

**Step 2: Run the chat suite**

Run: `uv run pytest tests/unit/test_chat_sse.py tests/unit/test_provision_fanout_route.py tests/integration/test_chat_endpoint.py tests/unit/test_fanout_orchestrator.py -q`
Expected: PASS.

**Step 3: Commit**

```bash
git add tests/
git commit -m "test(chat): accept prior_turns kwarg in chat stubs"
```

---

## Task 7: GET `/conversations` and GET `/conversations/{id}`

**Files:**
- Modify: `agent/main.py` (append after the `/chat` handler, ~line 5759)
- Test: `tests/unit/test_chat_conversations_endpoint.py` (create)

**Step 1: Write the failing endpoint tests**

Create `tests/unit/test_chat_conversations_endpoint.py`. Use `TestClient`, the
`_adk_enabled`-style fixture (set `USE_ADK=true`, override `verify_token`), patch
`run_chat`/`load_workload`/`_eager_resolve_upgrade_contract`, and reset the
`get_state` singleton to a fresh `InMemoryStateStore` per test:

```python
"""Multi-turn /chat persistence + the conversations HTTP surface (P1)."""
import json

import pytest
from fastapi.testclient import TestClient

import agent.main as agent_main
from agent.auth import verify_token
from agent.state_store import InMemoryStateStore


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("USE_ADK", "true")
    agent_main.get_settings.cache_clear()
    # fresh state singleton per test
    monkeypatch.setattr(agent_main, "_state_singleton", InMemoryStateStore())
    monkeypatch.setattr(agent_main, "get_state",
                        lambda: agent_main._state_singleton)
    agent_main.app.dependency_overrides[verify_token] = lambda: None

    async def _run_chat(prompt, session_id=None, *, workload="drift",
                        autonomy_mode="propose_apply", prior_turns=None):
        # echo how many prior turns were seeded so tests can assert resume
        return {"reply": f"reply to {prompt} (seeded={len(prior_turns or [])})",
                "tool_calls": [], "session_id": "sid"}

    monkeypatch.setattr("agent.adk_agent.run_chat", _run_chat)
    monkeypatch.setattr(agent_main, "load_workload", lambda w: object())
    monkeypatch.setattr(agent_main, "_eager_resolve_upgrade_contract", lambda r: None)
    yield TestClient(agent_main.app)
    agent_main.app.dependency_overrides.pop(verify_token, None)
    agent_main.get_settings.cache_clear()


def _post(client, prompt, workload="drift", conversation_id=None):
    body = {"prompt": prompt, "workload": workload}
    if conversation_id is not None:
        body["conversation_id"] = conversation_id
    return client.post("/chat", json=body)  # JSON path (no SSE Accept header)


def test_new_chat_creates_conversation_and_returns_id(client):
    r = _post(client, "first question")
    assert r.status_code == 200
    cid = r.json()["conversation_id"]
    assert cid
    # listed in the rail
    lst = client.get("/conversations").json()["conversations"]
    assert any(c["conversation_id"] == cid for c in lst)
    assert lst[0]["title"] == "first question"
    assert lst[0]["workload"] == "drift"


def test_resume_seeds_prior_turns(client):
    cid = _post(client, "q1").json()["conversation_id"]
    r2 = _post(client, "q2", conversation_id=cid)
    # turn1 = user q1 + crew; so q2 should see 2 prior turns seeded
    assert "seeded=2" in r2.json()["reply"]
    conv = client.get(f"/conversations/{cid}").json()
    assert [t["role"] for t in conv["turns"]] == ["user", "crew", "user", "crew"]
    assert conv["turns"][0]["text"] == "q1"


def test_unknown_conversation_id_404(client):
    assert _post(client, "x", conversation_id="ghost").status_code == 404


def test_crew_lock_mismatch_409(client):
    cid = _post(client, "q1", workload="drift").json()["conversation_id"]
    r = _post(client, "q2", workload="explore", conversation_id=cid)
    assert r.status_code == 409


def test_get_unknown_conversation_404(client):
    assert client.get("/conversations/ghost").status_code == 404


def test_conversations_list_limit_bounds(client):
    assert client.get("/conversations?limit=0").status_code == 400
    assert client.get("/conversations?limit=500").status_code == 400


def test_persist_failure_omits_conversation_id(client, monkeypatch):
    # A write failure must not break the reply AND must not hand back an id
    # that resolves to nothing.
    def _boom(*a, **k):
        raise RuntimeError("firestore down")

    monkeypatch.setattr(agent_main._state_singleton, "append_turns", _boom)
    r = _post(client, "q")
    assert r.status_code == 200
    assert "conversation_id" not in r.json()


def _set_paused(monkeypatch, paused):
    from types import SimpleNamespace
    monkeypatch.setattr(
        agent_main, "_pause_state_fail_closed",
        lambda: SimpleNamespace(paused=paused),
    )


def test_paused_unknown_conversation_id_404(client, monkeypatch):
    _set_paused(monkeypatch, True)
    assert _post(client, "x", conversation_id="ghost").status_code == 404


def test_paused_crew_lock_mismatch_409(client, monkeypatch):
    cid = _post(client, "q", workload="drift").json()["conversation_id"]
    _set_paused(monkeypatch, True)
    assert _post(client, "q", workload="explore",
                 conversation_id=cid).status_code == 409
```

> The `_set_paused` helper stubs `_pause_state_fail_closed` to a paused state; the
> 404/409 fire in the resolve call BEFORE `_paused_chat_response` runs, so the
> paused reply path isn't exercised (intended).

> If `explore`/`provision` workloads fail to "load" through the patched
> `load_workload`, the 409 test still exercises the resolve path because crew-lock
> is checked right after `_eager_resolve_upgrade_contract` — but confirm
> `load_workload` is patched to a no-op for every workload value used.

**Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_chat_conversations_endpoint.py -q`
Expected: FAIL — endpoints 404 (not defined) / `conversation_id` not echoed.

**Step 3: Implement the endpoints** (append after the `/chat` handler, mirroring
`list_decisions_endpoint` at ~1909):

```python
@app.get("/conversations")
def list_conversations_endpoint(
    response: Response,
    limit: int = 50,
    workload: str | None = None,
    _: None = Depends(verify_token),
    state: StateStore = Depends(get_state),
) -> dict:
    """List recent conversations (metadata only), newest-updated first."""
    if limit < 1 or limit > 200:
        raise HTTPException(
            status_code=400, detail="limit must be 1..200",
            headers={"Cache-Control": "no-store"},
        )
    response.headers["Cache-Control"] = "no-store"
    rows = state.list_conversations(limit=limit, workload=workload)
    return {"conversations": rows}


@app.get("/conversations/{conversation_id}")
def get_conversation_endpoint(
    conversation_id: str,
    response: Response,
    _: None = Depends(verify_token),
    state: StateStore = Depends(get_state),
) -> dict:
    """Full ordered turns for rehydrating a conversation on reload."""
    response.headers["Cache-Control"] = "no-store"
    # Path-safe id guard (Firestore doc id; reject path escapes). Treat a
    # malformed id as not-found rather than letting it reach .document().
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", conversation_id):
        raise HTTPException(
            status_code=404, detail="conversation not found",
            headers={"Cache-Control": "no-store"},
        )
    conv = state.get_conversation(conversation_id)
    if conv is None:
        raise HTTPException(
            status_code=404, detail="conversation not found",
            headers={"Cache-Control": "no-store"},
        )
    return conv
```

> P1 returns turn text as stored. The cross-crew redaction policy (secret_guard +
> snippet caps) lands in P3 with the `read_conversations` tool; the
> single-operator `/conversations*` surface is the operator's own history. If
> serve-time scrubbing of turn text is wanted earlier, add it here in a fast
> follow — note it in the PR.

**Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/test_chat_conversations_endpoint.py -q`
Expected: PASS.

**Step 5: Commit**

```bash
git add agent/main.py tests/unit/test_chat_conversations_endpoint.py
git commit -m "feat(chat): GET /conversations + GET /conversations/{id}"
```

---

## Task 8: Full suite + lint + manual smoke

**Step 1:** `uv run pytest -q` — full suite green (baseline was ~2660).
**Step 2:** `uv run ruff check .` — clean (fix any new findings).
**Step 3 (optional manual smoke):** boot dry-run uvicorn, POST `/chat` twice with
the returned `conversation_id`, confirm `GET /conversations` lists it and
`GET /conversations/{id}` returns 4 turns. (See `live_probe_recipes` memory for
the local rig.)
**Step 4: Commit** any lint fixes.

```bash
git add -A && git commit -m "chore(chat): lint + suite green for P1 multi-turn"
```

---

# P2 — Frontend (separate PR, outline)

Mirrors the design doc §Frontend. Files (verified in survey):

- `frontend/src/lib/sse.ts` — add `conversation_id?: string` to `ChatDone`.
- `frontend/src/lib/types.ts` — add `Conversation` + `ConversationsResponse`.
- `frontend/src/App.svelte` —
  - `let conversationId = $state<string | null>(null)`.
  - `onDone` (~305): `if (d.conversation_id) conversationId = d.conversation_id`.
  - `submitChat` body (~241): include `conversation_id: conversationId` when set;
    on a crew change, start a new conversation (clear id) since threads are
    crew-locked.
  - `newChat()` (~460): `conversationId = null`.
  - `loadConversations()` (mirror `loadDecisions` ~193) calling
    `GET /conversations`, called at mount + after each submit.
  - Thread view: render the rehydrated ordered turns (user/crew bubbles, reuse
    `FinalResponse`; each crew turn links `/trace/{trace_id}`); on open, fetch
    `GET /conversations/{id}`.
- New `frontend/src/components/ConversationsRail.svelte` — mirror
  `DecisionsRail.svelte` (`<aside>` + grouped list + open button); group by
  Today/Yesterday/Older on `updated_at`; show `CrewGlyph` + title + relative time.
- `App.svelte` layout grid (~622): add a column or stack the new rail.
- Tests: vitest for the rail grouping; a Playwright resume-after-reload smoke.

Deferred (YAGNI v1): rename/delete UI, rail search box.

---

# P3 — Cross-crew team memory (separate PR, outline)

Mirrors the design doc §Cross-crew. Full wiring checklist (verified in survey —
this is MORE than one list because tools are gated per-workload YAML):

1. **Tool** `read_conversations_tool` in `agent/adk_tools.py` (mirror
   `read_team_log_tool` ~1262): reads `get_state()`, projects via a new
   `_project_conversation` that allowlists metadata AND, for turn text, runs
   `secret_guard.redact_text` + `_team_log_sanitize` (control/bidi strip + cap) +
   snippets-by-default (full turns only on explicit `conversation_id`, still
   capped). Fail-soft `try/except`. New `_CONVERSATIONS_CAVEAT` like
   `_TEAM_LOG_CAVEAT`.
2. **Registry** `agent/workloads/registry.py`: add `"read_conversations"` ->
   `read_conversations_tool` to `_TOOL_REGISTRY` (~373) + import (~72); tier
   `"report"` in `_TOOL_TIERS` (~452).
3. **adk_agent.py** mirror constants: add `"read_conversations"` to
   `DRIFT/UPGRADE/EXPLORE/PROVISION_WORKLOAD_TOOL_NAMES` (in YAML order, read
   tools before mutation tools) and `read_conversations_tool` to
   `COORDINATOR_TOOLS`.
4. **All four** `workloads/<crew>/workload.yaml` — append `- read_conversations`
   to `enabled_tool_names`.
5. **Prompts** — add a "Tools available" bullet + the injection guard (mirror
   `workloads/explore/system_prompt.md:81-88`) to the four CHAT prompts:
   `drift/chat_system_prompt.md`, `upgrade/chat_system_prompt.md`,
   `explore/system_prompt.md`, `provision/system_prompt.md`.
6. **Inventory test** `tests/unit/test_coordinator_tool_inventory.py`: add
   `"read_conversations_tool"` to `EXPECTED_TOOL_NAMES` (~72), update the four
   `*_WORKLOAD_TOOL_NAMES` expectations, add `query`-style safe param names if
   new. It is read-only → NOT in `MUTATION_TOOL_NAMES`.
7. **Tool tests** `tests/unit/test_read_conversations_tool.py` (mirror
   `test_read_team_log_tool.py`): allowlist projection, leak gate (no secrets /
   tokens / control chars), snippet cap, crew filter, fail-soft.
8. **Breadcrumb** — at `/chat` time, build a per-request instruction string from
   `list_conversations(limit=10)` EXCLUDING the current crew, prepend to the
   `Agent` instruction (do NOT mutate the cached `WorkloadResolution` — pass a
   per-request `instruction=`); fail-soft; sanitize titles. Needs a small change
   to `build_chat_agent` (accept an optional `extra_instruction`/prefix) so the
   breadcrumb composes without touching `chat_system_prompt`.

---

## Execution notes

- Order: Tasks 1→8 in sequence. Tasks 3+5+6 are coupled (signature change ripples
  to stubs) — land them close together and keep the chat suite green between.
- After P1 is green + Codex-reviewed, follow the deploy-autonomy memory: merge +
  redeploy, then `update-traffic` (coordinator traffic pinning) if the coordinator
  image changed. P1 touches the agent image, so a redeploy is needed for it to go
  live.
