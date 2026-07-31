"""Persistent state for DriftScribe.

Two implementations:
- InMemoryStateStore: tests + DRY_RUN mode. Resets per-process.
- FirestoreStateStore: production. Uses Cloud Firestore native mode.

Both implement the StateStore Protocol so callers can substitute freely.

Idempotency model:
- ``record_event`` claims an event key. If the key already exists, returns
  False (claim refused). Used to gate side-effect work.
- ``record_decision`` writes the decision JSON keyed by decision_id, and
  cross-references it back to event_key so ``find_decision_for_event`` can
  return it on subsequent identical recheck calls.
- ``get_decision(decision_id)`` returns the recorded response or None.
"""

from typing import Any, Protocol

# Sentinel for "the caller stated no expectation", which None cannot express
# here: None is itself a meaningful expectation ("no proposal was open").
_UNSET: Any = object()


class StateStore(Protocol):
    def record_event(self, event_key: str, payload: dict[str, Any]) -> bool: ...
    def release_event(self, event_key: str) -> None: ...
    def find_decision_for_event(self, event_key: str) -> dict[str, Any] | None: ...
    def record_decision(
        self, decision_id: str, event_key: str, decision: dict[str, Any]
    ) -> None: ...
    def set_decision_notify_outcome(
        self, decision_id: str, outcome: dict[str, Any]
    ) -> None: ...
    def get_decision(self, decision_id: str) -> dict[str, Any] | None: ...
    def evict_cached_decision(self, event_key: str, decision_id: str) -> bool: ...
    def find_decision_by_trace_id(
        self, trace_id: str
    ) -> dict[str, Any] | None: ...
    def list_decisions(self, *, limit: int = 50) -> list[dict[str, Any]]: ...
    def list_decisions_for_pr(
        self, pr_number: int, *, limit: int = 50
    ) -> list[dict[str, Any]]: ...
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
        pending_handoff: dict[str, Any] | None = None,
        clear_pending_handoff: bool = False,
        expect_workload: str | None = None,
        expect_pending_digest: Any = _UNSET,
    ) -> list[int]: ...
    def get_conversation(
        self, conversation_id: str
    ) -> dict[str, Any] | None: ...
    def begin_chat_run(
        self, conversation_id: str, *, run_id: str, now: Any,
        ttl_seconds: int = ...,
    ) -> bool: ...
    def finish_chat_run(self, conversation_id: str, *, run_id: str) -> None: ...
    def redeem_handoff(
        self, conversation_id: str, *, nonce: str, accept: bool, now: Any,
        trace_id: str | None = None, run_id: str | None = None,
        ttl_seconds: int = ...,
    ) -> dict[str, Any]: ...
    def list_conversations(
        self, *, limit: int = 50, workload: str | None = None
    ) -> list[dict[str, Any]]: ...
    def get_pause(self) -> dict[str, Any] | None: ...
    def set_pause(
        self, *, paused: bool, reason: str | None, actor: str
    ) -> dict[str, Any]: ...
    def get_autonomy(self) -> dict[str, Any] | None: ...
    def set_autonomy(
        self, *, mode: str, reason: str | None, actor: str
    ) -> dict[str, Any]: ...


# --- Crew handoff: shared decision logic (both stores) ----------------------
#
# The two stores differ only in HOW they read/write atomically; WHAT counts as
# a valid redemption must not differ at all. Keeping the ladder here means a
# rule can never be tightened in one implementation and forgotten in the other
# — and the in-memory store, which every test runs against, exercises the exact
# code Firestore runs in production.

# How long a /chat run may hold a conversation's lease before another caller
# may take it. This bounds nothing — a real run finishes and releases
# explicitly — it only decides how long an ABANDONED lease wedges a thread.
#
# Anchored, not picked: Cloud Run stops the coordinator's requests at 300s
# (``--timeout=300`` in infra/cloudbuild.yaml, sized so /chat can hold its slot
# for a whole agent run), so a small margin over the request timeout is the
# tightest defensible value. Longer would strand an operator whose browser
# disconnected mid-turn; shorter could steal a live run's lease.
#
# Stated honestly, because an earlier version of this comment claimed more than
# it can: the request timeout ends the REQUEST, which is not the same as
# proving the container stopped working on it. There is no application-level
# cancellation or lease renewal here, so a run that somehow outlives its own
# request can still append after another caller takes the lease. That costs
# transcript attribution — the same thing a failed lease acquisition costs
# below — and never authority, because the crew a run may use was fixed at
# request entry and no lease decides it.
CHAT_RUN_LEASE_TTL_S = 330


def conversation_crews(conv: Any) -> list[str]:
    """Every crew that has taken part in this conversation, in joining order.

    ``workload`` and ``crews`` answer different questions and must not be
    conflated. ``workload`` is who is bound RIGHT NOW — the crew-lock authority
    the 409 is built on — and redemption rewrites it. ``crews`` is the
    participant history, which nothing rewrites; it is what "team memory" has
    to filter and label on, because a thread's TITLE is the first prompt the
    ORIGINATING crew was asked.

    Conversations written before this field existed carry no ``crews``. For
    those the single bound workload is the entire participant history, so the
    fallback is exact rather than a guess — which is why no backfill is needed.
    """
    if not isinstance(conv, dict):
        return []
    raw = conv.get("crews")
    if isinstance(raw, list):
        out = [c for c in raw if isinstance(c, str) and c]
        if out:
            return out
    bound = conv.get("workload")
    return [bound] if isinstance(bound, str) and bound else []


def conversation_has_crew(conv: Any, crew: str) -> bool:
    """Whether ``crew`` took part in this conversation at any point."""
    return crew in conversation_crews(conv)


def conversation_user_turns(conv: Any) -> int:
    """How many prompts the OPERATOR actually typed in this conversation.

    The rail's "N messages" means this, and used to derive it as
    ``ceil(turn_count / 2)`` from an invariant that no longer holds: every
    exchange wrote one user turn and one crew turn, so the total was even. A
    handoff breaks it from both ends — an accepted transition appends a
    ``crew_change`` row, and the joining turn writes a reply with no prompt in
    front of it, because the operator confirmed a suggestion rather than typing
    one. So the count is carried rather than recomputed.

    Conversations predating the counter are seeded from ``turn_count`` on their
    next append. For them the old invariant genuinely did hold — turns land two
    at a time in a single atomic append — so half the total is their exact prior
    count, not an estimate.
    """
    if not isinstance(conv, dict):
        return 0
    stored = conv.get("user_turn_count")
    if isinstance(stored, int) and not isinstance(stored, bool) and stored >= 0:
        return stored
    total = conv.get("turn_count")
    if isinstance(total, int) and not isinstance(total, bool) and total > 0:
        return (total + 1) // 2
    return 0


def _count_user_turns(turns: list[dict[str, Any]]) -> int:
    return sum(1 for t in turns if t.get("role") == "user")


def _with_crew(conv: Any, crew: str) -> list[str]:
    """The participant list with ``crew`` appended if it is not already there.

    A thread that bounces explore → drift → explore lists two participants, not
    three: this records who took part, not a move log.
    """
    crews = conversation_crews(conv)
    return crews if crew in crews else [*crews, crew]


def _evaluate_handoff_redemption(
    conv: dict[str, Any] | None, *, nonce: str, now: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return ``(pending, None)`` if this redemption may proceed, else
    ``(None, reason)``.

    Refusal reasons are stable tokens the HTTP layer maps to status codes.
    Every one of them fails CLOSED: an unreadable or ambiguous state refuses
    the transition rather than granting it, because a refused handoff costs
    the operator one re-ask and a wrongly granted one moves a conversation to
    a crew with tools the current crew does not have.
    """
    from agent.handoff import is_handoff_expired, verify_handoff_nonce

    if conv is None:
        return None, "not_found"
    pending = conv.get("pending_handoff")
    if not isinstance(pending, dict) or not pending.get("nonce_digest"):
        return None, "no_pending"
    if not verify_handoff_nonce(nonce, pending.get("nonce_digest")):
        # Deliberately checked BEFORE expiry/staleness so a wrong guess never
        # reveals anything about the real proposal — and never burns it.
        return None, "invalid_nonce"
    if is_handoff_expired(pending, now=now):
        return None, "expired"
    if pending.get("from") != conv.get("workload"):
        # The thread moved on by some other path; this proposal describes a
        # route that no longer starts where it claims to.
        return None, "stale"
    lease = conv.get("chat_run_lease")
    if isinstance(lease, dict) and not _lease_is_free(lease, now=now):
        # A turn is in flight. It will persist using the workload it CAPTURED
        # at request entry, so flipping now would attribute that turn to the
        # wrong crew in the durable transcript.
        return None, "busy"
    return pending, None


def _may_touch_pending_handoff(
    conv: Any, expect_workload: str | None, expect_pending_digest: Any = _UNSET,
) -> bool:
    """Whether this writer is still current enough to mutate an open proposal.

    A turn captures its crew at request entry and can persist much later: the
    lease fails open on a store error, and nothing cancels a run that outlives
    its TTL. If a redemption moved the conversation in the meantime, this
    writer is stale — and BOTH of its effects on ``pending_handoff`` are then
    destructive. Clearing would delete a proposal the crew that has since
    JOINED just made, silently retiring a chip the operator can still see.
    Writing would replace that proposal with one whose ``from`` no longer
    matches the current crew, so it could only ever be refused.

    Only this is fenced. A late turn ROW still appends: that is a wrong audit
    line, which is the trade ``_acquire_chat_run``'s fail-open documents and
    accepts. Deleting live control state is not the same kind of wrong, and
    reading the difference as one cost was the mistake this guard corrects.

    TWO keys, because neither is sufficient alone.

    ``expect_pending_digest`` is a compare-and-swap on the proposal itself: may
    I still edit the one I saw when I started? Digests come from
    ``secrets.token_urlsafe(32)`` and never recur, so this survives ownership
    CYCLING — Explore → Provision → Explore leaves the symbolic name equal to
    what a stale Explore run captured, and the workload check alone would wave
    it through to overwrite a proposal minted in between. That is not
    hypothetical; it was reproduced.

    ``expect_workload`` catches what the digest cannot: a stale run that saw NO
    proposal, and still sees none, but whose own new proposal carries a
    ``from`` that is no longer the bound crew. The digests match (both absent)
    while the offer is already meaningless — it could only ever be refused.

    Either expectation omitted means the caller has none, and keeps the old
    behavior.
    """
    if expect_workload is not None:
        current_wl = conv.get("workload")
        # An absent workload means nothing to contradict — treat as current
        # rather than dropping a legitimate write on a partially-shaped doc.
        if current_wl is not None and current_wl != expect_workload:
            return False
    if expect_pending_digest is not _UNSET:
        current = (conv.get("pending_handoff") or {}).get("nonce_digest")
        if current != expect_pending_digest:
            return False
    return True


def _lease_is_free(lease: object, *, now: Any) -> bool:
    """True when no live run holds the conversation. Unreadable lease == free
    (fail-open) — the lease protects transcript attribution, not authority, and
    a malformed one must not wedge a thread permanently."""
    if not isinstance(lease, dict) or not lease.get("run_id"):
        return True
    expires_at = lease.get("expires_at")
    if not hasattr(expires_at, "tzinfo"):
        return True
    if expires_at.tzinfo is None:
        from datetime import timezone as _tz

        expires_at = expires_at.replace(tzinfo=_tz.utc)
    return now >= expires_at


def _new_lease(run_id: str, now: Any, ttl_seconds: int) -> dict[str, Any]:
    from datetime import timedelta

    return {"run_id": run_id, "expires_at": now + timedelta(seconds=ttl_seconds)}


def _handoff_transition_turn(
    pending: dict[str, Any], *, accept: bool, trace_id: str | None,
) -> dict[str, Any]:
    """The server-authored row recording that a transition happened (or was
    declined). Not model output: it replays to the joining crew as trusted
    event text, never as a model-authored turn."""
    return {
        "role": "crew_change" if accept else "handoff_declined",
        "text": pending.get("reason") or "",
        # An accepted transition belongs to the crew that JOINED; a declined
        # one belongs to the crew that stayed.
        "workload": pending["to"] if accept else pending["from"],
        "trace_id": trace_id,
        "handoff": {"from": pending["from"], "to": pending["to"]},
    }


class InMemoryStateStore:
    """Process-local state. Used in tests and DRY_RUN mode.

    The conversation mutators below hold ``_lock``. Firestore gets its
    all-or-nothing behavior from ``@firestore.transactional``; this store has
    to say so explicitly, and it matters: ``_persist_chat_turn`` runs inside
    ``asyncio.to_thread``, so several OS threads really do reach these methods
    at once. Without the lock, two callers can both validate a nonce before
    either burns it and both redeem it. Reentrant because ``redeem_handoff``
    calls ``append_turns``.
    """

    def __init__(self) -> None:
        import threading

        self._lock = threading.RLock()
        self._events: dict[str, dict[str, Any]] = {}  # event_key -> {payload, decision_id}
        self._decisions: dict[str, dict[str, Any]] = {}  # decision_id -> full decision
        # Pause flag singleton. None = never written (system is running by default —
        # the pause doc not existing means the operator has never toggled it).
        self._pause: dict[str, Any] | None = None
        # Autonomy dial singleton. None = never written; agent.autonomy maps
        # absent → the permissive DEFAULT_MODE (system's pre-dial behavior).
        self._autonomy: dict[str, Any] | None = None
        # Multi-turn chat (P1). conversation_id -> conversation doc (metadata);
        # turns kept in a parallel dict so list_conversations stays metadata-only.
        self._conversations: dict[str, dict[str, Any]] = {}
        self._conversation_turns: dict[str, list[dict[str, Any]]] = {}

    def record_event(self, event_key: str, payload: dict[str, Any]) -> bool:
        if event_key in self._events:
            return False
        self._events[event_key] = {"payload": payload, "decision_id": None}
        return True

    def release_event(self, event_key: str) -> None:
        """Drop a claim. Used by ``_do_recheck`` when side effects fail so
        retries can proceed. No-op if the event isn't claimed."""
        self._events.pop(event_key, None)

    def find_decision_for_event(self, event_key: str) -> dict[str, Any] | None:
        record = self._events.get(event_key)
        if not record or not record["decision_id"]:
            return None
        return self._decisions.get(record["decision_id"])

    def record_decision(
        self, decision_id: str, event_key: str, decision: dict[str, Any]
    ) -> None:
        # Phase 19.A.7: every new decision carries a ``created_at`` so
        # ``list_decisions`` has a sortable field on every row. Use a
        # real ``datetime`` here (no ``SERVER_TIMESTAMP`` equivalent for
        # in-memory state). ``setdefault`` lets tests that need a
        # deterministic value pass it in explicitly without being
        # clobbered. Defensive copy so the caller's dict isn't mutated.
        from datetime import datetime, timezone

        record = dict(decision)
        record.setdefault("created_at", datetime.now(timezone.utc))
        # Phase C5e: store ``event_key`` on the decision itself so the Firestore
        # store's query-fallback recovery (find_decision_for_event) has a field to
        # query, and so callers / tests see the same shape across both stores.
        record["event_key"] = event_key
        self._decisions[decision_id] = record
        if event_key in self._events:
            self._events[event_key]["decision_id"] = decision_id

    def set_decision_notify_outcome(
        self, decision_id: str, outcome: dict[str, Any]
    ) -> None:
        # Raises KeyError on a missing decision, mirroring the Firestore
        # store's NotFound: this only ever patches a row this process just
        # wrote, so "not there" is a real fault worth surfacing, not a
        # condition to upsert away. Deliberately narrow — it patches ONE
        # key. A generic patch method would invite arbitrary post-hoc
        # rewrites of a decision doc that is meant to be an immutable record.
        record = self._decisions.get(decision_id)
        if record is None:
            raise KeyError(decision_id)
        record["notify"] = dict(outcome)

    def get_decision(self, decision_id: str) -> dict[str, Any] | None:
        return self._decisions.get(decision_id)

    def evict_cached_decision(self, event_key: str, decision_id: str) -> bool:
        """Compare-and-delete the event doc. True == the caller may re-propose.

        In-memory parity with the Firestore store — see its docstring for why
        an ABSENT event doc is a success rather than a failure (ds-q38).
        """
        record = self._events.get(event_key)
        if record is None:
            return True
        if record.get("decision_id") != decision_id:
            return False
        self._events.pop(event_key, None)
        return True

    def find_decision_by_trace_id(self, trace_id: str) -> dict[str, Any] | None:
        """Linear scan over decisions for the matching ``trace_id``.

        Phase 19.A.6: the ``/trace/{trace_id}`` endpoint enriches the
        reasoning timeline with the persisted decision document so the
        UI can show the final action alongside the events. Linear scan
        is fine for InMemoryStateStore — used only in tests / DRY_RUN —
        where the decision dict is at most a few entries deep.

        Newest-first (2026-07-10, Codex review): one request can record
        MULTIPLE decisions for the same trace_id (the create-class merge
        path records a waiting_for_rebake pending → merged pair) — pick the
        newest by ``created_at`` so the caller sees the current lifecycle
        stage, not an arbitrary one. Newest-first parity with the Firestore
        store — see its docstring for why. ``record_decision`` always
        setdefaults ``created_at`` here, but tolerate a missing/None value
        via the same UTC sentinel :meth:`list_decisions` uses, since callers
        can pass explicit decision dicts (as the tests above do).
        """
        from datetime import datetime, timezone

        sentinel = datetime.min.replace(tzinfo=timezone.utc)
        matching = [
            d for d in self._decisions.values() if d.get("trace_id") == trace_id
        ]
        if not matching:
            return None
        return max(matching, key=lambda d: d.get("created_at") or sentinel)

    def list_decisions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Return up to ``limit`` decisions, newest first.

        Phase 19.A.7: powers the operator-facing ``/decisions``
        listing. Sort key tolerates a missing ``created_at`` via a
        UTC ``datetime.min`` sentinel — a missing field would
        otherwise raise ``TypeError`` on the ``None`` vs ``datetime``
        compare, and a malformed write shouldn't crash the UI.
        """
        from datetime import datetime, timezone

        sentinel = datetime.min.replace(tzinfo=timezone.utc)
        by_time = sorted(
            self._decisions.values(),
            key=lambda d: d.get("created_at") or sentinel,
            reverse=True,
        )
        return by_time[:limit]

    def list_decisions_for_pr(
        self, pr_number: int, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return up to ``limit`` decisions for ``pr_number``, newest first.

        Backs ``read_team_log_tool(pr_number=N)``. Filtering happens BEFORE
        the limit (unlike ``list_decisions(limit)`` + a caller-side filter,
        which trims the global newest ``limit`` first and so misses an older
        PR's rows). Same missing-``created_at`` sentinel tolerance as
        :meth:`list_decisions`.
        """
        from datetime import datetime, timezone

        sentinel = datetime.min.replace(tzinfo=timezone.utc)
        matching = [
            d for d in self._decisions.values() if d.get("pr_number") == pr_number
        ]
        matching.sort(key=lambda d: d.get("created_at") or sentinel, reverse=True)
        return matching[:limit]

    # --- Multi-turn chat conversations (P1) ---------------------------------

    def create_conversation(
        self, conversation_id: str, *, workload: str, title: str
    ) -> dict[str, Any]:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        doc = {
            "conversation_id": conversation_id,
            "workload": workload,
            "crews": [workload],
            "title": title,
            "created_at": now,
            "updated_at": now,
            "turn_count": 0,
            "user_turn_count": 0,
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
        pending_handoff: dict[str, Any] | None = None,
        clear_pending_handoff: bool = False,
        expect_workload: str | None = None,
        expect_pending_digest: Any = _UNSET,
    ) -> list[int]:
        with self._lock:
            return self._append_turns_locked(
                conversation_id, turns, create_with=create_with,
                pending_handoff=pending_handoff,
                clear_pending_handoff=clear_pending_handoff,
                expect_workload=expect_workload,
                expect_pending_digest=expect_pending_digest,
            )

    def _append_turns_locked(
        self,
        conversation_id: str,
        turns: list[dict[str, Any]],
        *,
        create_with: dict[str, Any] | None = None,
        pending_handoff: dict[str, Any] | None = None,
        clear_pending_handoff: bool = False,
        expect_workload: str | None = None,
        expect_pending_digest: Any = _UNSET,
    ) -> list[int]:
        """Append turns; optionally record a crew-handoff proposal with them.

        ``pending_handoff`` writes in the SAME operation as the turns, which is
        what makes a proposal possible on turn 1: a new conversation has no
        document until its first turns persist, so a tool that wrote the
        proposal itself would have nothing to write to.

        ``clear_pending_handoff`` retires an outstanding proposal in that same
        operation. The caller sets it when the appended turns include one the
        OPERATOR typed: being asked "shall I bring in Provision?" and replying
        with something else is an answer, so the proposal is spent.

        This used to be the opposite — ``None`` left the proposal alone, on the
        reasoning that someone who types should not silently lose the chip. The
        SPA nonetheless retires the chip on a typed turn, because leaving it
        under a NEWER reply attaches the suggestion to a question it was never
        about. That left the view and the store disagreeing, and the gap was
        reachable: a second tab holding the same nonce could still confirm a
        suggestion the operator had already answered by typing, moving the
        conversation on the older of two contradictory instructions. Losing the
        chip is not silent when the operator's own next message is what caused
        it, so the store now agrees with the view.
        """
        from datetime import datetime, timezone

        conv = self._conversations.get(conversation_id)
        if conv is None:
            if create_with is None:
                raise KeyError(f"conversation {conversation_id!r} not found")
            self.create_conversation(conversation_id, **create_with)
            conv = self._conversations[conversation_id]
        start = int(conv["turn_count"])
        prior_user_turns = conversation_user_turns(conv)
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
            if t.get("handoff"):
                turn["handoff"] = t["handoff"]
            self._conversation_turns.setdefault(conversation_id, []).append(turn)
            if t.get("trace_id"):
                last_trace = t["trace_id"]
            seqs.append(seq)
        conv["turn_count"] = start + len(turns)
        # Read the prior count BEFORE turn_count moves — the legacy seed inside
        # conversation_user_turns derives from it.
        conv["user_turn_count"] = prior_user_turns + _count_user_turns(turns)
        conv["updated_at"] = now
        conv["last_trace_id"] = last_trace
        if not _may_touch_pending_handoff(
            conv, expect_workload, expect_pending_digest
        ):
            pending_handoff, clear_pending_handoff = None, False
        if pending_handoff is not None:
            # Overwrites any prior proposal, which IS the supersede-and-burn:
            # only one nonce digest can be stored, so the old one stops
            # verifying. Load-bearing — two proposals from the same crew both
            # satisfy ``pending.from == workload``, so nothing else catches it.
            conv["pending_handoff"] = dict(pending_handoff)
        elif clear_pending_handoff:
            conv.pop("pending_handoff", None)
        return seqs

    # --- Crew handoff -------------------------------------------------------

    def begin_chat_run(
        self, conversation_id: str, *, run_id: str, now: Any,
        ttl_seconds: int = CHAT_RUN_LEASE_TTL_S,
    ) -> bool:
        with self._lock:
            conv = self._conversations.get(conversation_id)
            if conv is None:
                # Nothing to lease and nothing to race: a conversation with no
                # document has no proposal either.
                return True
            if not _lease_is_free(conv.get("chat_run_lease"), now=now):
                return False
            conv["chat_run_lease"] = _new_lease(run_id, now, ttl_seconds)
            return True

    def finish_chat_run(self, conversation_id: str, *, run_id: str) -> None:
        with self._lock:
            conv = self._conversations.get(conversation_id)
            if conv is None:
                return
            lease = conv.get("chat_run_lease")
            # Only the holder may release: a late finish from a run that
            # already timed out must not free the lease a newer run now holds.
            if isinstance(lease, dict) and lease.get("run_id") == run_id:
                conv.pop("chat_run_lease", None)

    def redeem_handoff(
        self, conversation_id: str, *, nonce: str, accept: bool, now: Any,
        trace_id: str | None = None, run_id: str | None = None,
        ttl_seconds: int = CHAT_RUN_LEASE_TTL_S,
    ) -> dict[str, Any]:
        with self._lock:
            conv = self._conversations.get(conversation_id)
            view = (
                self.get_conversation(conversation_id)
                if conv is not None else None
            )
            pending, refusal = _evaluate_handoff_redemption(
                view, nonce=nonce, now=now,
            )
            if refusal is not None:
                return {"ok": False, "error": refusal}
            assert pending is not None and conv is not None
            # Burn first: even if the append below were to fail, the credential
            # is spent. A stuck transition the operator can re-request beats a
            # live nonce whose conversation already moved.
            conv.pop("pending_handoff", None)
            if accept:
                conv["crews"] = _with_crew(conv, pending["to"])
                conv["workload"] = pending["to"]
                if run_id:
                    conv["chat_run_lease"] = _new_lease(run_id, now, ttl_seconds)
            self._append_turns_locked(
                conversation_id,
                [_handoff_transition_turn(
                    pending, accept=accept, trace_id=trace_id,
                )],
            )
            return {
                "ok": True, "pending": dict(pending), "accepted": accept,
                "run_id": run_id if accept else None,
            }

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
            if workload is None or conversation_has_crew(c, workload)
        ]
        rows.sort(key=lambda c: c.get("updated_at") or sentinel, reverse=True)
        return rows[:limit]

    def get_pause(self) -> dict[str, Any] | None:
        """Return a defensive copy of the pause document, or None if never set.

        Returns a copy so callers cannot alias or mutate the stored state.
        Absent doc = not paused: the system predates this feature; the default
        is always-running.
        """
        if self._pause is None:
            return None
        return dict(self._pause)

    def set_pause(
        self, *, paused: bool, reason: str | None, actor: str
    ) -> dict[str, Any]:
        """Overwrite the pause document and return a defensive copy.

        Stores a fresh ``updated_at`` timestamp (UTC ``datetime`` — the
        in-memory equivalent of Firestore's SERVER_TIMESTAMP). Defensive copy
        on both the stored dict and the returned dict so neither the caller
        nor a subsequent get_pause caller can alias internal state.
        """
        from datetime import datetime, timezone

        self._pause = {
            "paused": paused,
            "reason": reason,
            "actor": actor,
            "updated_at": datetime.now(timezone.utc),
        }
        return dict(self._pause)

    def get_autonomy(self) -> dict[str, Any] | None:
        """Return a defensive copy of the autonomy document, or None if never set.

        Mirrors get_pause: absent doc = dial never touched; the caller
        (agent.autonomy.read_autonomy_state) maps None to the default mode.
        """
        if self._autonomy is None:
            return None
        return dict(self._autonomy)

    def set_autonomy(
        self, *, mode: str, reason: str | None, actor: str
    ) -> dict[str, Any]:
        """Overwrite the autonomy document and return a defensive copy."""
        from datetime import datetime, timezone

        self._autonomy = {
            "mode": mode,
            "reason": reason,
            "actor": actor,
            "updated_at": datetime.now(timezone.utc),
        }
        return dict(self._autonomy)


class FirestoreStateStore:
    """Cloud Firestore-backed state. Collections: ``events``, ``decisions``, ``config``."""

    def __init__(self, project: str, client: Any = None) -> None:
        # Lazy import so tests that don't use this don't need GCP creds installed
        if client is None:
            from google.cloud import firestore

            client = firestore.Client(project=project)
        self._db = client
        self._events = client.collection("events")
        self._decisions = client.collection("decisions")
        # ``config`` collection for singleton operator-configuration documents.
        # Currently only the ``pause`` document (id="pause") lives here.
        # Separate from ``events``/``decisions`` so IAM and query scopes stay clean.
        self._config = client.collection("config")
        # Multi-turn chat (P1): one doc per conversation; turns live in a
        # ``turns`` subcollection under each conversation doc.
        self._conversations = client.collection("conversations")

    def record_event(self, event_key: str, payload: dict[str, Any]) -> bool:
        # Create-if-absent: succeed only when the doc didn't already exist.
        # We narrow to AlreadyExists so genuine infra failures (permissions,
        # network) propagate as exceptions rather than being misread as "claim
        # refused" — Codex review #4 of Phase 4.
        from google.api_core.exceptions import AlreadyExists

        doc = self._events.document(event_key)
        try:
            doc.create({"payload": payload, "decision_id": None})
            return True
        except AlreadyExists:
            return False

    def release_event(self, event_key: str) -> None:
        """Drop a claim so retries can proceed after a side-effect failure.
        No-op if the document doesn't exist."""
        from google.api_core.exceptions import NotFound

        try:
            self._events.document(event_key).delete()
        except NotFound:
            pass

    def find_decision_for_event(self, event_key: str) -> dict[str, Any] | None:
        snap = self._events.document(event_key).get()
        decision_id = None
        if snap.exists:
            data = snap.to_dict() or {}
            decision_id = data.get("decision_id")
        if decision_id:
            return self.get_decision(decision_id)
        # Phase C5e recovery fallback (belt-and-suspenders): if the event doc is
        # missing or carries no decision_id — e.g. the pointer write was lost — fall
        # back to a query on the ``event_key`` field that ``record_decision`` now
        # stores INSIDE the decision doc. C5e uses the decision doc as the
        # apply-then-merge reconcile pointer, so a lost pointer must still be
        # recoverable rather than silently re-minting + re-applying.
        #
        # NEWEST-FIRST, and here that is a safety property rather than a display
        # nicety (ds-q38 / ds-bej). Two decisions can legitimately share one
        # ``event_key`` with NO pointer to arbitrate between them:
        #
        #   1. a stale row D0 is CAS-evicted, deleting the pointer;
        #   2. the repair claims the event and mints approval A, and
        #      ``record_decision``'s batch COMMITS D1 + its pointer;
        #   3. the client still sees an ambiguous transport error, so
        #      ``_do_rollback`` releases the claim — deleting the pointer again;
        #   4. D0 and the valid, unexpired D1 now both carry this ``event_key``.
        #
        # An unordered ``.limit(1)`` could hand back D0 there. D0 is stale, so
        # the caller evicts (a no-op — the pointer is already gone), re-claims
        # successfully, and mints approval B: TWO live approvals for one drift.
        # ``record_event`` cannot prevent that, because it only ever sees the
        # pointer and this state has none. Returning the NEWEST decision returns
        # D1, which is servable, so the caller serves it instead of re-minting.
        #
        # Client-side sort on the server-managed ``create_time``, mirroring
        # find_decision_by_trace_id and list_decisions: a server-side
        # ``order_by("created_at")`` would EXCLUDE pre-19.A.7 rows missing the
        # field, turning a recovery query into a silent miss.
        #
        # And NO ``.limit()`` on the unordered stream — the trap list_decisions
        # already documents (invariant 2 in its docstring). Firestore's implicit
        # ordering is by document ID, and decision ids are random UUIDs, so
        # ``.limit(N)`` takes an ARBITRARY N rather than the newest N. Capping
        # here would have re-opened the very double-mint this sort exists to
        # close, just past N rows: the newest decision falls outside the page,
        # recovery hands back an older stale row, and the caller evicts,
        # re-claims and mints a second approval. The equality filter is what
        # bounds this read (rows sharing one event_key are a handful even in the
        # pathological repeated-replacement case), not a page size.
        snaps = list(self._decisions.where("event_key", "==", event_key).stream())
        if not snaps:
            return None
        newest = max(snaps, key=lambda s: s.create_time)
        return newest.to_dict()

    def record_decision(
        self, decision_id: str, event_key: str, decision: dict[str, Any]
    ) -> None:
        # Phase 19.A.7: every new decision carries a ``created_at``
        # field set to ``firestore.SERVER_TIMESTAMP`` so the listing
        # has a server-authoritative sortable column on every row,
        # immune to client clock skew. Defensive copy so the caller's
        # dict isn't mutated. NOTE: ``list_decisions`` does NOT rely
        # on this field for ordering (it sorts client-side on
        # ``snapshot.create_time`` so pre-Phase-19 docs without
        # ``created_at`` still appear) — but the UI surfaces it as
        # the displayed timestamp, so it's worth recording explicitly.
        #
        # Phase C5e: the decision doc is the apply-then-merge reconcile pointer, so
        # the decision write + the event→decision pointer write MUST commit together
        # — previously two separate writes, where a crash between them orphaned the
        # pointer (a later /apply could then re-mint + re-apply over a possibly-
        # changed world). A WriteBatch makes both atomic. We ALSO store ``event_key``
        # inside the decision doc so ``find_decision_for_event`` can recover via a
        # query if the pointer write is ever lost. The event pointer uses
        # ``set(..., merge=True)`` rather than ``update`` so a (corner-case) missing
        # event doc upserts instead of raising NotFound — without clobbering the
        # existing claim payload.
        from google.cloud import firestore

        record = dict(decision)
        record["created_at"] = firestore.SERVER_TIMESTAMP
        record["event_key"] = event_key
        batch = self._db.batch()
        batch.set(self._decisions.document(decision_id), record)
        batch.set(
            self._events.document(event_key),
            {"decision_id": decision_id},
            merge=True,
        )
        batch.commit()

    def set_decision_notify_outcome(
        self, decision_id: str, outcome: dict[str, Any]
    ) -> None:
        # ``update`` (not ``set(merge=True)``): this only ever patches a doc
        # this process just wrote, so a missing doc is a genuine fault and
        # should raise NotFound rather than upsert a decision-shaped fragment
        # carrying nothing but a ``notify`` key — a row like that would join
        # the /decisions listing with no action, no approval and no timestamp.
        #
        # Deliberately narrow (one key, not a generic patch): the decision doc
        # is an audit record, and the only field that legitimately settles
        # AFTER the row is written is the advisory delivery outcome.
        self._decisions.document(decision_id).update({"notify": outcome})

    def get_decision(self, decision_id: str) -> dict[str, Any] | None:
        snap = self._decisions.document(decision_id).get()
        return snap.to_dict() if snap.exists else None

    def evict_cached_decision(self, event_key: str, decision_id: str) -> bool:
        """Compare-and-delete the event doc transactionally.

        Returns True when the caller may go on to re-propose — which is NOT
        the same as "I deleted something". Three cases:

        * pointer names ``decision_id`` -> delete it, True (the ordinary CAS
          win). Closes Phase 13 Codex W2 carry-over: two concurrent /recheck
          retries observing the same expired cached rollback would both call
          ``release_event`` under the pre-Phase-13 code, letting one re-claim
          and the other delete that fresh claim. The compare keeps the loser
          from clobbering the winner.
        * pointer names something ELSE -> False. Someone already installed a
          replacement; deleting it would clobber a fresh claim.
        * **pointer ABSENT -> True (ds-q38).** There is nothing to clobber and
          no claim held, so the caller is free to proceed.

        That third case used to return False, and it is worth being explicit
        about why that was a bug rather than a nicety, because it only bites
        in combination with ds-bej:

        1. A run wins the CAS, so the event doc is gone — but the decision doc
           survives, carrying its ``event_key`` field.
        2. Anything then fails before a replacement is recorded (the Rollback
           Worker's /propose, rendering, a GitHub side effect, the
           ``record_decision`` write itself).
        3. The next audit's :meth:`find_decision_for_event` misses on the
           pointer and RESURRECTS the stale row through the ``event_key``
           recovery query.
        4. It is correctly judged stale, so the caller tries to evict it — and
           under the old contract that CAS could never succeed again, because
           the pointer it compares against no longer exists.
        5. Every subsequent audit for that key 409s. Forever.

        The wedge lands on exactly the path that REPAIRS a poisoned key, so a
        single transient worker failure during a repair would have made the
        damage permanent. Treating "already absent" as success makes the whole
        sequence retryable instead: a failed repair leaves the key in a state
        the next delivery can pick up.

        Safety of the relaxation rests on TWO things, and both are load-bearing:

        * **The claim.** Minting is gated by ``record_event``
          (create-if-absent) downstream on BOTH branches — ``_do_rollback``
          claims before calling /propose, and the non-rollback path claims
          before any side effect. Two concurrent runs that both pass here
          still serialize one step later, and only one can mint.
        * **Deterministic recovery.** The claim only ever sees the POINTER, so
          it cannot arbitrate between two completed decisions that share an
          ``event_key`` with no pointer between them — a state reachable when a
          repair's ``record_decision`` batch commits but its client sees a
          transport error and releases the claim. If recovery handed back the
          OLDER (stale) row there, this branch would return True, the re-claim
          would succeed, and one drift would mint a SECOND approval.
          :meth:`find_decision_for_event` therefore returns the newest
          decision, which is servable, so the caller serves rather than
          re-mints. Weakening either half re-opens the double-mint.
        """
        from google.cloud import firestore

        doc_ref = self._events.document(event_key)

        @firestore.transactional
        def _txn(transaction, expected_decision_id):
            snap = doc_ref.get(transaction=transaction)
            if not snap.exists:
                return True
            data = snap.to_dict() or {}
            if data.get("decision_id") != expected_decision_id:
                return False
            transaction.delete(doc_ref)
            return True

        transaction = self._db.transaction()
        return _txn(transaction, decision_id)

    def find_decision_by_trace_id(self, trace_id: str) -> dict[str, Any] | None:
        """Index lookup on ``trace_id`` over the decisions collection.

        Phase 19.A.6: the ``/trace/{trace_id}`` endpoint enriches the
        reasoning timeline with the persisted decision so the UI can
        show the final action alongside the events. ``.limit(10)``
        bounds the read; the field is set on every decision since
        19.A.4 (``record_decision`` persists the request's trace_id).

        Returns ``None`` if no decision matches (e.g. /trace was called
        before /recheck finished, or the trace_id was for a /chat call
        that doesn't write a decision document at all).

        Newest-first (2026-07-10, Codex review): one request can record
        MULTIPLE decisions for the same trace_id (the create-class merge
        path records a waiting_for_rebake pending → merged pair), and the
        old unordered ``.limit(1)`` picked arbitrarily. Fetch a small page
        and take the newest by server-managed ``snapshot.create_time``
        (client-side — a server ``order_by("created_at")`` would EXCLUDE
        pre-19.A.7 docs missing the field, see list_decisions). Backfill
        ``created_at`` from ``create_time``, mirroring list_decisions, so the
        /trace fetch hint works for every decision.
        """
        snaps = list(
            self._decisions.where("trace_id", "==", trace_id).limit(10).stream()
        )
        if not snaps:
            return None
        newest = max(snaps, key=lambda s: s.create_time)
        d = newest.to_dict() or {}
        d.setdefault("created_at", newest.create_time)
        return d

    def list_decisions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Return up to ``limit`` decisions, newest first.

        Phase 19.A.7 — Codex review IMPORTANT (two distinct invariants):

        1. **Do NOT use server-side ``order_by("created_at")``.**
           Firestore's ``order_by(field)`` EXCLUDES documents where
           the field is missing — it does not sort them last. A
           pre-Phase-19 decision (written before this task added the
           ``created_at`` schema column) would silently disappear
           from the listing. Sorting CLIENT-SIDE on
           ``DocumentSnapshot.create_time`` — which is always present
           and server-managed — gives us a stable union of old and
           new docs without backfilling.

        2. **Do NOT call ``.limit(N)`` on the unordered stream.**
           Firestore's default ordering without ``order_by`` is by
           document ID, so ``.limit(N)`` picks an arbitrary subset
           that may exclude the newest decisions entirely. We have to
           fetch ALL snapshots, sort, then trim.

        Documented assumption: hackathon decision volume is in the
        hundreds, not millions. If this scales past that, swap to a
        server-side ordered query — but that needs a one-time
        backfill of ``created_at`` on every old doc first to
        preserve invariant (1).

        Polish: ``snapshot.create_time`` isn't in ``to_dict()``, but
        pre-Phase-19 docs don't have an explicit ``created_at``
        either. Backfill from ``create_time`` so the UI can show a
        timestamp uniformly across every row.
        """
        snaps = list(self._decisions.stream())
        snaps.sort(
            key=lambda s: s.create_time,
            reverse=True,
        )
        out: list[dict[str, Any]] = []
        for s in snaps[:limit]:
            d = s.to_dict() or {}
            d.setdefault("created_at", s.create_time)
            out.append(d)
        return out

    def list_decisions_for_pr(
        self, pr_number: int, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return up to ``limit`` decisions for ``pr_number``, newest first.

        Backs ``read_team_log_tool(pr_number=N)``. A single equality filter
        ``where("pr_number", "==", n)`` uses Firestore's automatic
        single-field index — no composite index needed. We deliberately do
        NOT add ``order_by`` (it would EXCLUDE any matched doc missing the
        sort field — the same trap :meth:`list_decisions` documents) and do
        NOT ``.limit(N)`` the stream (limit-before-sort picks an arbitrary
        subset by doc id). Filter server-side, then sort CLIENT-SIDE on
        ``snapshot.create_time`` and trim — so the per-PR view is exact
        regardless of how many newer unrelated decisions exist. Per-PR row
        counts are tiny (a PR's apply lifecycle is a handful of docs), so
        fetching all matches before trimming is cheap.
        """
        snaps = list(self._decisions.where("pr_number", "==", pr_number).stream())
        snaps.sort(key=lambda s: s.create_time, reverse=True)
        out: list[dict[str, Any]] = []
        for s in snaps[:limit]:
            d = s.to_dict() or {}
            d.setdefault("created_at", s.create_time)
            out.append(d)
        return out

    # --- Multi-turn chat conversations (P1) ---------------------------------

    def create_conversation(
        self, conversation_id: str, *, workload: str, title: str
    ) -> dict[str, Any]:
        from google.cloud import firestore

        doc = {
            "conversation_id": conversation_id,
            "workload": workload,
            "crews": [workload],
            "title": title,
            "created_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP,
            "turn_count": 0,
            "user_turn_count": 0,
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
        pending_handoff: dict[str, Any] | None = None,
        clear_pending_handoff: bool = False,
        expect_workload: str | None = None,
        expect_pending_digest: Any = _UNSET,
    ) -> list[int]:
        """Append ``turns`` atomically, allocating contiguous ``seq`` values.

        One transaction: read the conversation doc's ``turn_count`` (the seq
        cursor), then write every turn doc + the bumped parent doc. A plain
        batch — unlike ``record_decision`` whose ids are pre-known — would let
        two concurrent posts pick the same ``seq``. When ``create_with`` is set
        and the conversation does not exist, the doc is created INSIDE the same
        transaction so a new conversation + its first turns persist all-or-
        nothing (no empty-doc / half-turn windows). Mirrors the read-before-
        write shape of :meth:`evict_cached_decision`.

        ``pending_handoff`` rides the SAME transaction — that is the whole
        reason it is a parameter here rather than its own method. A proposal
        made on turn 1 has no document to attach to until these turns create
        one, so proposal and turns must commit together or not at all. ``None``
        leaves any existing proposal untouched; see the in-memory twin.
        """
        from google.cloud import firestore

        conv_ref = self._conversations.document(conversation_id)

        @firestore.transactional
        def _txn(transaction) -> list[int]:
            # READS FIRST (Firestore requires all reads before any writes).
            snap = conv_ref.get(transaction=transaction)
            if not snap.exists:
                if create_with is None:
                    raise KeyError(f"conversation {conversation_id!r} not found")
                base = {
                    "conversation_id": conversation_id,
                    "workload": create_with["workload"],
                    "crews": [create_with["workload"]],
                    "title": create_with["title"],
                    "created_at": firestore.SERVER_TIMESTAMP,
                    "last_trace_id": None,
                }
                start, last_trace, is_create = 0, None, True
                prior_user_turns = 0
                # Created here, with this caller's own workload — there is no
                # earlier writer to be stale against.
                may_touch_handoff = True
            else:
                data = snap.to_dict() or {}
                start = int(data.get("turn_count", 0))
                prior_user_turns = conversation_user_turns(data)
                last_trace = data.get("last_trace_id")
                base, is_create = {}, False
                # Decided from the doc read INSIDE the transaction, so a
                # redemption that lands between the read and the commit loses
                # to Firestore's contention retry rather than slipping past
                # this check.
                may_touch_handoff = _may_touch_pending_handoff(
                    data, expect_workload, expect_pending_digest
                )
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
                if t.get("handoff"):
                    turn["handoff"] = t["handoff"]
                transaction.set(
                    conv_ref.collection("turns").document(f"{seq:06d}"), turn
                )
                if t.get("trace_id"):
                    last_trace = t["trace_id"]
                seqs.append(seq)
            doc_fields = {
                "turn_count": start + len(turns),
                "user_turn_count": prior_user_turns + _count_user_turns(turns),
                "updated_at": firestore.SERVER_TIMESTAMP,
                "last_trace_id": last_trace,
            }
            if pending_handoff is not None and may_touch_handoff:
                doc_fields["pending_handoff"] = dict(pending_handoff)
            elif clear_pending_handoff and not is_create and may_touch_handoff:
                # DELETE_FIELD is invalid inside a ``set`` of a brand-new doc,
                # and a conversation being created cannot have a proposal to
                # retire anyway.
                doc_fields["pending_handoff"] = firestore.DELETE_FIELD
            if is_create:
                transaction.set(conv_ref, {**base, **doc_fields})
            else:
                transaction.update(conv_ref, doc_fields)
            return seqs

        return _txn(self._db.transaction())

    # --- Crew handoff -------------------------------------------------------

    def begin_chat_run(
        self, conversation_id: str, *, run_id: str, now: Any,
        ttl_seconds: int = CHAT_RUN_LEASE_TTL_S,
    ) -> bool:
        """Claim the conversation for one in-flight chat run.

        Transactional read-then-write for the same reason ``append_turns`` is:
        two concurrent callers must not both see a free lease. Returns False
        when a live run already holds it. An absent document returns True —
        turn 1 has nothing to lease and no proposal to race.
        """
        from datetime import timedelta

        from google.cloud import firestore

        conv_ref = self._conversations.document(conversation_id)

        @firestore.transactional
        def _txn(transaction) -> bool:
            snap = conv_ref.get(transaction=transaction)
            if not snap.exists:
                return True
            if not _lease_is_free((snap.to_dict() or {}).get("chat_run_lease"),
                                  now=now):
                return False
            transaction.update(conv_ref, {
                "chat_run_lease": {
                    "run_id": run_id,
                    "expires_at": now + timedelta(seconds=ttl_seconds),
                },
            })
            return True

        return _txn(self._db.transaction())

    def finish_chat_run(self, conversation_id: str, *, run_id: str) -> None:
        """Release the lease, but only if this run still holds it — a late
        finish from a timed-out run must not free a newer run's claim."""
        from google.cloud import firestore

        conv_ref = self._conversations.document(conversation_id)

        @firestore.transactional
        def _txn(transaction) -> None:
            snap = conv_ref.get(transaction=transaction)
            if not snap.exists:
                return
            lease = (snap.to_dict() or {}).get("chat_run_lease")
            if isinstance(lease, dict) and lease.get("run_id") == run_id:
                transaction.update(
                    conv_ref, {"chat_run_lease": firestore.DELETE_FIELD}
                )

        _txn(self._db.transaction())

    def redeem_handoff(
        self, conversation_id: str, *, nonce: str, accept: bool, now: Any,
        trace_id: str | None = None, run_id: str | None = None,
        ttl_seconds: int = CHAT_RUN_LEASE_TTL_S,
    ) -> dict[str, Any]:
        """Verify, burn, flip, reserve, and record — all inside one transaction.

        Single-use is only real if the check and the burn cannot interleave,
        so the whole ladder runs against the transaction's snapshot: two
        simultaneous redemptions of the same nonce cannot both win. The
        validity rules themselves live in :func:`_evaluate_handoff_redemption`
        so they cannot drift from the in-memory store every test runs against.

        ``run_id`` reserves the conversation for the joining turn in this SAME
        transaction. Acquiring it afterwards left a gap in which an ordinary
        turn could take the lease first — and the confirmation would then 409
        having ALREADY burned its nonce and flipped the crew. Reserving here
        means the credential and the run it pays for commit together.
        """
        from google.cloud import firestore

        conv_ref = self._conversations.document(conversation_id)

        @firestore.transactional
        def _txn(transaction) -> dict[str, Any]:
            # READS FIRST (Firestore requires all reads before any writes).
            snap = conv_ref.get(transaction=transaction)
            conv = (snap.to_dict() or {}) if snap.exists else None
            pending, refusal = _evaluate_handoff_redemption(
                conv, nonce=nonce, now=now,
            )
            if refusal is not None:
                return {"ok": False, "error": refusal}
            assert pending is not None and conv is not None
            # WRITES.
            seq = int(conv.get("turn_count", 0))
            turn = _handoff_transition_turn(
                pending, accept=accept, trace_id=trace_id,
            )
            transaction.set(
                conv_ref.collection("turns").document(f"{seq:06d}"),
                {**turn, "seq": seq, "created_at": firestore.SERVER_TIMESTAMP},
            )
            doc_fields: dict[str, Any] = {
                "pending_handoff": firestore.DELETE_FIELD,
                "turn_count": seq + 1,
                # Unchanged in value — a transition row is not something the
                # operator typed — but pinned explicitly, because leaving it
                # absent while turn_count moves would let the legacy seed in
                # conversation_user_turns derive from the larger total later.
                "user_turn_count": conversation_user_turns(conv),
                "updated_at": firestore.SERVER_TIMESTAMP,
            }
            if accept:
                doc_fields["workload"] = pending["to"]
                # Computed from the transaction's own snapshot rather than an
                # ArrayUnion so the participant list commits with the flip it
                # describes, under the same read that authorised it.
                doc_fields["crews"] = _with_crew(conv, pending["to"])
                if run_id:
                    doc_fields["chat_run_lease"] = _new_lease(
                        run_id, now, ttl_seconds,
                    )
            transaction.update(conv_ref, doc_fields)
            return {
                "ok": True, "pending": dict(pending), "accepted": accept,
                "run_id": run_id if accept else None,
            }

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
        # Filtered in Python, not by a `where`. A crew now matches a thread it
        # merely took part in, and the two candidate server-side forms both
        # fail: `workload ==` misses threads handed away, and `crews
        # array_contains` misses every conversation written before that field
        # existed. A deduplicated union of both queries WOULD cut filtered reads
        # at scale; it is not worth the second round trip here, because the
        # unfiltered path already streams the whole collection and sorts in
        # Python and the breadcrumb walks it on every chat turn — so that scan
        # dominates, and this one is the existing cost profile rather than a new
        # one. Revisit the union if the breadcrumb ever stops full-scanning.
        # Using the same predicate
        # as the in-memory store is worth more: it is the store every test runs
        # against, so a rule cannot hold there and quietly differ in prod.
        rows: list[dict[str, Any]] = []
        for s in self._conversations.stream():
            d = s.to_dict() or {}
            d.setdefault("created_at", s.create_time)
            d.setdefault("updated_at", d.get("created_at"))
            if workload is not None and not conversation_has_crew(d, workload):
                continue
            rows.append(d)
        rows.sort(key=lambda d: d.get("updated_at") or 0, reverse=True)
        return rows[:limit]

    def get_pause(self) -> dict[str, Any] | None:
        """Point-read the ``config/pause`` document; returns ``to_dict()`` or None.

        Returns None when the document has never been written (the feature was
        added after the system was deployed; absent = not paused by design).
        ``to_dict()`` already returns a plain dict copy so no extra defensive copy
        is needed here — Firestore's client always constructs a fresh object.
        """
        snap = self._config.document("pause").get()
        return snap.to_dict() if snap.exists else None

    def set_pause(
        self, *, paused: bool, reason: str | None, actor: str
    ) -> dict[str, Any]:
        """Full-overwrite the ``config/pause`` document and return the as-written dict.

        Uses ``firestore.SERVER_TIMESTAMP`` for ``updated_at`` so the caller
        receives the real server-authoritative time (not client-clock time that
        drifts across Cloud Run instances). One extra point-read after the write
        is intentional: toggles are rare operator actions, and returning a
        client-side guess at the server timestamp would silently lie about what
        Firestore actually stored. The read-after-write is the cheapest way to
        give the caller — and the audit log — the truthful value.
        """
        from google.cloud import firestore

        doc_ref = self._config.document("pause")
        doc_ref.set(
            {
                "paused": paused,
                "reason": reason,
                "actor": actor,
                "updated_at": firestore.SERVER_TIMESTAMP,
            }
        )
        # Read back the written document so the returned dict carries the real
        # server timestamp rather than the sentinel value.
        snap = doc_ref.get()
        return snap.to_dict()

    def get_autonomy(self) -> dict[str, Any] | None:
        """Point-read the ``config/autonomy`` document; ``to_dict()`` or None.

        Mirrors get_pause: None when the document has never been written
        (the dial was never touched; agent.autonomy maps that to the
        permissive default mode). ``to_dict()`` already returns a fresh
        plain dict so no extra defensive copy is needed.
        """
        snap = self._config.document("autonomy").get()
        return snap.to_dict() if snap.exists else None

    def set_autonomy(
        self, *, mode: str, reason: str | None, actor: str
    ) -> dict[str, Any]:
        """Full-overwrite the ``config/autonomy`` document; return as-written.

        Mirrors set_pause: ``firestore.SERVER_TIMESTAMP`` for ``updated_at``
        plus a read-after-write so the caller and audit log see the real
        server-authoritative time rather than the sentinel.
        """
        from google.cloud import firestore

        doc_ref = self._config.document("autonomy")
        doc_ref.set(
            {
                "mode": mode,
                "reason": reason,
                "actor": actor,
                "updated_at": firestore.SERVER_TIMESTAMP,
            }
        )
        snap = doc_ref.get()
        return snap.to_dict()
