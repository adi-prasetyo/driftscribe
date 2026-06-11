# Autonomy Dial Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A global operator autonomy dial — **Observe** (report only, no mutations), **Propose** (PRs/issues/approvals, no applies), **Propose + Apply** (current behavior) — enforced at the tool-registry layer and at every coordinator-initiated mutation site, controlled from the SPA like the pause button. ClickOps roadmap Wave 3 item 11 (`docs/plans/2026-06-10-clickops-audience-roadmap.md` §11).

**Architecture:** A new `agent/autonomy.py` mirrors `agent/pause.py` (fail-closed read, frozen state dataclass, no FastAPI deps) over a new Firestore `config/autonomy` document. Every tool in `TOOL_REGISTRY` gets a **tier** (`report` / `propose` / `apply`) in `agent/workloads/registry.py`, drift-pinned to the registry and cross-checked against `MUTATION_TOOL_NAMES`. Enforcement happens at four seams: (1) Layer 0 — `build_agent` / `build_chat_agent` filter the tool set by tier before the LLM ever sees it; (2) the drift pipeline's coordinator-executed actions (`_perform_action`, `_do_rollback`) are suppressed-but-recorded in Observe; (3) the two human apply gates (`POST /iac-approvals/{pr}` approve, `POST /approvals/{id}` approve) refuse 409 unless the dial is at Propose + Apply (reject paths stay ungated, same safety direction as pause); (4) the SPA gets an `AutonomyControl` next to `PauseControl`, a "not executed — Observe" treatment on suppressed rail rows, and a live-mode note on the capability card.

**Tech Stack:** Python/FastAPI/pydantic, Firestore (`config` collection), Google ADK, Svelte 5 + Vite, pytest + vitest/@testing-library/svelte.

---

## Decisions locked by the user (2026-06-11)

1. **Global dial v1** — one mode for the whole system; per-workload dials deferred.
2. **SPA control mirroring the pause button** — operator-token-gated POST, Firestore-persisted with `reason`/`actor`/`updated_at` audit, fail-closed read surfaced in the UI.
3. **The dial governs event-driven mutations too** — Eventarc-triggered drift work still *runs* in Observe (observing is the point; pause is the full stop) but its mutations are suppressed and recorded as "would have" decisions.

## Roadmap-pinned semantics

- Default (absent `config/autonomy` doc) = `propose_apply` — current behavior.
- Fail-closed direction on **read error or malformed doc** = `observe` — the MOST restrictive mode. Note the deliberate asymmetry with the absent-doc default; both directions get explicit tests.
- Enforcement at Layer 0 (tool registry) and coordinator mutation sites — never prompt-level. (A prompt note is *added* for UX when tools are stripped, but it is informational; the tools are already gone.)
- Pause outranks the dial everywhere. Order in every gated handler: existing dry-run / pause checks first, dial check immediately after.

## Mode × surface matrix (the contract all tests pin)

| Surface | observe | propose | propose_apply |
|---|---|---|---|
| Chat/recheck `report`-tier tools (reads, docs search, `notify`, `load_contract`, `search_recent_prs`) | ✓ | ✓ | ✓ |
| Chat/recheck `propose`-tier tools (`drift_patch_docs`, `drift_propose_rollback`, `upgrade_propose_pr`, `upgrade_close_pr`, `provision_open_infra_pr`, `provision_propose_adoption`) | stripped | ✓ | ✓ |
| Chat `apply`-tier tools (`upgrade_merge_pr`) | stripped | stripped | ✓ |
| Drift pipeline classify + validate + claim + **record decision** | ✓ | ✓ | ✓ |
| Drift pipeline action execution (`docs_pr`/`drift_issue`/`escalation` GitHub writes) | suppressed + recorded | ✓ | ✓ |
| Rollback proposal (`_do_rollback` worker `/propose` + notifier) | suppressed + recorded | ✓ | ✓ |
| `POST /iac-approvals/{pr}` approve (apply pipeline) | 409 | 409 | ✓ |
| `POST /approvals/{id}` approve (rollback execute) | 409 | 409 | ✓ |
| All reject paths; `GET` display pages; `/eventarc` event intake; `no_op` | ✓ | ✓ | ✓ |

Key nuances (each gets a dedicated test):

- `notify` and `search_recent_prs` are in `MUTATION_TOOL_NAMES` for **credential containment**, not because they mutate — they are `report`-tier. `notify` is Observe's reporting channel; stripping it would make Observe mute, defeating the mode.
- `_do_rollback` deliberately ignores `dry_run` for its worker calls (`dry_run_effective: False` pattern). The dial must NOT inherit that: Observe suppresses the rollback worker `/propose` and the notifier call entirely. Explicit divergence, explicit test.
- `/eventarc` is NOT dropped in Observe (contrast with pause's 200-ignored drop) — the event flows into `_do_recheck`, which records a suppressed decision. Test pins this.
- Dial refusals use **409 Conflict** (the request conflicts with the operator-configured mode), NOT 423 — 423 is pause's status and clients map it to pause messaging; conflating them would mislabel the operator's own dial setting as an emergency stop.
- Both refusal/note copy strings live in `agent/autonomy.py` constants — factual, never alarming; they say what is disabled and where to change it.

## Codex plan review

(Recorded after review — thread ID, must-fixes, and folds go here before implementation.)

---

### Task 1: `agent/autonomy.py` — modes, fail-closed read, tool filter

**Files:**
- Create: `agent/autonomy.py`
- Test: `tests/unit/test_autonomy.py`

**Step 1: Write the failing tests**

```python
"""Unit tests for agent/autonomy.py — the autonomy dial state + tool filter.

Mirrors tests/unit/test_pause.py's structure: plain InMemoryStateStore,
throw-on-get stub for fail-closed cases, no monkeypatching.
"""
import pytest

from agent.autonomy import (
    AUTONOMY_MODES,
    DEFAULT_MODE,
    FAIL_CLOSED_REASON,
    AutonomyState,
    autonomy_apply_blocked_detail,
    autonomy_instruction_note,
    filter_tools_for_mode,
    mode_allows,
    read_autonomy_state,
)
from agent.state_store import InMemoryStateStore


class _BoomStore:
    def get_autonomy(self):
        raise RuntimeError("firestore unavailable")


class _DocStore:
    def __init__(self, doc):
        self._doc = doc

    def get_autonomy(self):
        return self._doc


class TestReadAutonomyState:
    def test_absent_doc_defaults_to_propose_apply(self):
        st = read_autonomy_state(InMemoryStateStore())
        assert st == AutonomyState(mode="propose_apply")
        assert DEFAULT_MODE == "propose_apply"

    def test_storage_error_fails_closed_to_observe(self):
        st = read_autonomy_state(_BoomStore())
        assert st.mode == "observe"
        assert st.read_error is True
        assert st.reason == FAIL_CLOSED_REASON

    @pytest.mark.parametrize(
        "doc",
        [
            {},                          # clobbered empty doc
            {"mode": "yolo"},            # unknown mode string
            {"mode": 3},                 # wrong type
            {"mode": None},
            "not-a-dict",
            ["propose"],
        ],
    )
    def test_malformed_doc_fails_closed_to_observe(self, doc):
        st = read_autonomy_state(_DocStore(doc))
        assert st.mode == "observe"
        assert st.read_error is True

    @pytest.mark.parametrize("mode", list(AUTONOMY_MODES))
    def test_valid_doc_round_trips(self, mode):
        store = InMemoryStateStore()
        store.set_autonomy(mode=mode, reason="testing", actor="op@example.com")
        st = read_autonomy_state(store)
        assert st.mode == mode
        assert st.reason == "testing"
        assert st.actor == "op@example.com"
        assert st.read_error is False
        assert st.updated_at is not None

    def test_state_is_frozen(self):
        st = AutonomyState(mode="observe")
        with pytest.raises(Exception):
            st.mode = "propose_apply"  # type: ignore[misc]


class TestModeAllows:
    def test_observe_allows_only_report(self):
        assert mode_allows("observe", "report") is True
        assert mode_allows("observe", "propose") is False
        assert mode_allows("observe", "apply") is False

    def test_propose_allows_report_and_propose(self):
        assert mode_allows("propose", "report") is True
        assert mode_allows("propose", "propose") is True
        assert mode_allows("propose", "apply") is False

    def test_propose_apply_allows_everything(self):
        for tier in ("report", "propose", "apply"):
            assert mode_allows("propose_apply", tier) is True

    def test_unknown_tier_or_mode_fails_closed(self):
        assert mode_allows("propose_apply", "banana") is False
        assert mode_allows("banana", "report") is False


class TestFilterToolsForMode:
    TOOLS = {"a_read": (lambda: 1), "b_propose": (lambda: 2), "c_apply": (lambda: 3)}
    TIERS = {"a_read": "report", "b_propose": "propose", "c_apply": "apply"}

    def test_observe_keeps_only_report(self):
        out = filter_tools_for_mode(self.TOOLS, self.TIERS, "observe")
        assert set(out) == {"a_read"}

    def test_propose_strips_apply_only(self):
        out = filter_tools_for_mode(self.TOOLS, self.TIERS, "propose")
        assert set(out) == {"a_read", "b_propose"}

    def test_propose_apply_keeps_everything_in_order(self):
        out = filter_tools_for_mode(self.TOOLS, self.TIERS, "propose_apply")
        assert list(out) == list(self.TOOLS)  # insertion order preserved

    def test_tool_missing_from_tiers_is_treated_as_apply(self):
        # Fail-closed: a new tool cannot leak into a restricted mode by
        # missing its tier assignment.
        out = filter_tools_for_mode({"mystery": (lambda: 0)}, {}, "propose")
        assert out == {}
        out2 = filter_tools_for_mode({"mystery": (lambda: 0)}, {}, "propose_apply")
        assert set(out2) == {"mystery"}


class TestCopy:
    def test_instruction_note_names_the_mode_and_the_control(self):
        note = autonomy_instruction_note("observe")
        assert "Observe" in note
        assert "autonomy" in note.lower()

    def test_apply_blocked_detail_says_how_to_enable(self):
        detail = autonomy_apply_blocked_detail("propose")
        assert "Propose" in detail
        assert "Propose + Apply" in detail

    @pytest.mark.parametrize("text_fn", [autonomy_instruction_note, autonomy_apply_blocked_detail])
    @pytest.mark.parametrize("banned", ["risk", "danger", "unsafe", "rampage"])
    def test_copy_is_factual_not_alarming(self, text_fn, banned):
        assert banned not in text_fn("observe").lower()
```

**Step 2: Run tests to verify they fail**

Run from repo root: `.venv/bin/pytest tests/unit/test_autonomy.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.autonomy'` (and `InMemoryStateStore` has no `set_autonomy` — Task 2 wires the store; write Task 2's store methods together with this task if preferred, the tests force it).

**Step 3: Write `agent/autonomy.py`**

```python
"""Operator autonomy dial — global Observe / Propose / Propose+Apply (ClickOps item 11).

Fail-closed contract: read_autonomy_state NEVER raises. Any storage error or
malformed document returns mode="observe"/read_error=True — the MOST
restrictive mode — so mutation surfaces degrade to report-only while the
dial is unreadable. An ABSENT document means the dial was never touched:
the default is "propose_apply", the system's behavior before the dial
existed (roadmap item 11: "default = current behavior").

The two defaults point in different directions on purpose:
- absent doc  → propose_apply (operator never chose; keep current behavior)
- broken read → observe       (we cannot KNOW what the operator chose; the
  only honest stance is the most restrictive mode)

Mirrors agent/pause.py: no FastAPI dependency, unit-testable with a plain
InMemoryStateStore or a throw-on-get stub. The pause flag outranks the dial
everywhere — pause is the full stop; the dial chooses what runs when not
paused.
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

log = logging.getLogger("driftscribe.agent.autonomy")

# Ordered least → most permissive. Index = permissiveness rank.
AUTONOMY_MODES: tuple[str, ...] = ("observe", "propose", "propose_apply")

DEFAULT_MODE = "propose_apply"

# Tool tiers. "report" tools are available in every mode — observation and
# reporting are the dial's floor, not a privilege. "propose" tools may open
# PRs / issues / approval requests. "apply" tools change live state or merge
# to a branch that deploys. The per-tool assignment (TOOL_TIERS) lives in
# agent/workloads/registry.py next to TOOL_REGISTRY — the authority layer —
# and is drift-pinned to it.
TIER_NAMES: tuple[str, ...] = ("report", "propose", "apply")

_TIER_MIN_MODE: Mapping[str, str] = MappingProxyType({
    "report": "observe",
    "propose": "propose",
    "apply": "propose_apply",
})

MODE_LABELS: Mapping[str, str] = MappingProxyType({
    "observe": "Observe",
    "propose": "Propose",
    "propose_apply": "Propose + Apply",
})

# Returned as the ``reason`` field when the Firestore read itself fails —
# same surfacing convention as agent.pause.FAIL_CLOSED_REASON.
FAIL_CLOSED_REASON = "autonomy state could not be read — failing closed to Observe"


@dataclass(frozen=True)
class AutonomyState:
    """Immutable snapshot of the autonomy dial at a point in time.

    ``read_error=True`` means the storage read failed or the document was
    malformed; ``mode`` is then always ``"observe"`` (fail-closed).
    """

    mode: str
    reason: str | None = None
    actor: str | None = None
    updated_at: Any = None  # datetime or DatetimeWithNanoseconds from Firestore
    read_error: bool = False


def read_autonomy_state(state: Any) -> AutonomyState:
    """Read the autonomy dial from the given StateStore; NEVER raises.

    Calling convention matches agent.pause.read_pause_state: pass
    ``get_state()``. Storage exceptions and malformed documents (not a
    dict, ``mode`` missing or not one of AUTONOMY_MODES) both fail closed
    to Observe with ``read_error=True``. An absent document (None) returns
    the permissive DEFAULT_MODE — the operator never touched the dial.
    """
    try:
        doc = state.get_autonomy()
    except Exception:  # noqa: BLE001 — fail-closed by contract, never raise
        log.warning("autonomy_state_read_failed", exc_info=True)
        return AutonomyState(mode="observe", reason=FAIL_CLOSED_REASON, read_error=True)
    if doc is None:
        return AutonomyState(mode=DEFAULT_MODE)
    mode_val = doc.get("mode") if isinstance(doc, dict) else None
    if mode_val not in AUTONOMY_MODES:
        log.warning("autonomy_state_malformed", extra={"doc_type": type(doc).__name__})
        return AutonomyState(mode="observe", reason=FAIL_CLOSED_REASON, read_error=True)
    return AutonomyState(
        mode=mode_val,
        reason=doc.get("reason"),
        actor=doc.get("actor"),
        updated_at=doc.get("updated_at"),
    )


def mode_allows(mode: str, tier: str) -> bool:
    """True iff ``mode`` permits tools/actions of ``tier``.

    Unknown tier or unknown mode returns False — fail-closed, so a new
    tool cannot leak into a restricted mode by missing or typo-ing its
    tier assignment.
    """
    min_mode = _TIER_MIN_MODE.get(tier)
    if min_mode is None or mode not in AUTONOMY_MODES:
        return False
    return AUTONOMY_MODES.index(mode) >= AUTONOMY_MODES.index(min_mode)


def filter_tools_for_mode(
    tools: Mapping[str, Callable],
    tiers: Mapping[str, str],
    mode: str,
) -> dict[str, Callable]:
    """Return the subset of ``tools`` permitted under ``mode``, preserving
    order. A tool absent from ``tiers`` is treated as "apply" — the most
    restricted tier — so the filter fails closed on registry drift."""
    return {
        name: fn
        for name, fn in tools.items()
        if mode_allows(mode, tiers.get(name, "apply"))
    }


def autonomy_instruction_note(mode: str) -> str:
    """LLM instruction suffix used when the dial strips tools — informational
    UX only; enforcement already happened at the registry filter."""
    label = MODE_LABELS.get(mode, mode)
    return (
        f"NOTE: the operator has set the autonomy dial to {label}, so some "
        "tools that open pull requests or issues, create approvals, or apply "
        "changes are disabled in this conversation. If the operator asks for "
        "something you no longer have a tool for, say that plainly and point "
        "them at the Autonomy control in the operator UI — do not improvise "
        "another way to make the change."
    )


def autonomy_apply_blocked_detail(mode: str) -> str:
    """HTTP 409 detail for apply-gate refusals. Factual, names the fix."""
    label = MODE_LABELS.get(mode, mode)
    return (
        f"autonomy is set to {label} — applying changes is disabled. "
        "Raise the dial to Propose + Apply in the operator UI to enable this "
        "approval. The proposal itself remains valid and waiting."
    )
```

**Step 4: Run the tests** — `.venv/bin/pytest tests/unit/test_autonomy.py -q` — the `read_autonomy_state` round-trip tests still fail until Task 2's store methods exist. Implement Task 2 Step 3 now if you want green here; otherwise proceed (TDD across the pair).

**Step 5: Commit** once Tasks 1+2 are green together: `git commit -m "feat(autonomy): dial state module — fail-closed read, mode/tier semantics, tool filter"`

---

### Task 2: StateStore `get_autonomy` / `set_autonomy`

**Files:**
- Modify: `agent/state_store.py` (Protocol + `InMemoryStateStore` + `FirestoreStateStore`)
- Test: extend `tests/unit/test_autonomy.py` (round-trip already written in Task 1)

**Step 1: Protocol** — next to `get_pause`/`set_pause` in the `StateStore` Protocol add:

```python
    def get_autonomy(self) -> dict[str, Any] | None: ...
    def set_autonomy(
        self, *, mode: str, reason: str | None, actor: str
    ) -> dict[str, Any]: ...
```

**Step 2: `InMemoryStateStore`** — mirror the pause implementation exactly (defensive copies, `updated_at=datetime.now(timezone.utc)`); `self._autonomy: dict[str, Any] | None = None` in `__init__`:

```python
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
        self._autonomy = {
            "mode": mode,
            "reason": reason,
            "actor": actor,
            "updated_at": datetime.now(timezone.utc),
        }
        return dict(self._autonomy)
```

**Step 3: `FirestoreStateStore`** — mirror `get_pause`/`set_pause` byte-for-byte in idiom (`config/autonomy` document, `firestore.SERVER_TIMESTAMP`, read-after-write):

```python
    def get_autonomy(self) -> dict[str, Any] | None:
        """Point-read the ``config/autonomy`` document; ``to_dict()`` or None."""
        snap = self._config.document("autonomy").get()
        return snap.to_dict() if snap.exists else None

    def set_autonomy(
        self, *, mode: str, reason: str | None, actor: str
    ) -> dict[str, Any]:
        """Full-overwrite the ``config/autonomy`` document; return as-written."""
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
```

**Step 4: Run** `.venv/bin/pytest tests/unit/test_autonomy.py -q` → all green. **Step 5: Commit** (combined with Task 1).

---

### Task 3: `TOOL_TIERS` in the registry + drift-pin tests

**Files:**
- Modify: `agent/workloads/registry.py` (after `TOOL_REGISTRY`)
- Test: `tests/unit/test_tool_tiers.py`

**Step 1: Failing tests**

```python
"""Drift-pins for TOOL_TIERS — every tool has a tier; tiers cohere with
MUTATION_TOOL_NAMES (the existing trust-boundary classifier in fanout.py)."""
from agent.autonomy import TIER_NAMES
from agent.fanout import MUTATION_TOOL_NAMES
from agent.workloads.registry import TOOL_REGISTRY, TOOL_TIERS


def test_tool_tiers_cover_exactly_the_tool_registry():
    assert set(TOOL_TIERS) == set(TOOL_REGISTRY)


def test_tool_tiers_values_are_valid():
    assert set(TOOL_TIERS.values()) <= set(TIER_NAMES)


def test_every_propose_or_apply_tool_is_a_known_mutation_tool():
    # The dial's tier ladder must be at least as strict as the existing
    # mutation classifier: anything we let past Observe-stripping must
    # already be flagged write-capable there.
    elevated = {n for n, t in TOOL_TIERS.items() if t != "report"}
    assert elevated <= MUTATION_TOOL_NAMES


def test_report_tier_mutation_names_are_exactly_the_credential_containment_pair():
    # notify + search_recent_prs are in MUTATION_TOOL_NAMES because they
    # ride write-capable credentials, NOT because they mutate. They stay
    # available in Observe (notify IS the reporting channel). This pin makes
    # adding a third such exception an explicit, reviewed decision.
    report_but_mutation = {
        n for n, t in TOOL_TIERS.items() if t == "report"
    } & MUTATION_TOOL_NAMES
    assert report_but_mutation == {"notify", "search_recent_prs"}


def test_apply_tier_is_exactly_merge():
    assert {n for n, t in TOOL_TIERS.items() if t == "apply"} == {"upgrade_merge_pr"}
```

**Step 2: Run** `.venv/bin/pytest tests/unit/test_tool_tiers.py -q` → FAIL (no `TOOL_TIERS`).

**Step 3: Implement** — in `agent/workloads/registry.py`, directly after the `TOOL_REGISTRY = MappingProxyType(...)` line:

```python
# --------------------------------------------------------------------------- #
# TOOL_TIERS — autonomy-dial tier per tool (ClickOps item 11)
# --------------------------------------------------------------------------- #
#
# "report" tools are available in every dial mode; "propose" tools may open
# PRs / issues / approval requests (stripped in Observe); "apply" tools change
# live state or merge a deploying branch (Propose + Apply only). Consumed by
# agent.autonomy.filter_tools_for_mode at agent-build time (Layer 0).
#
# Drift-pins in tests/unit/test_tool_tiers.py:
# - set(TOOL_TIERS) == set(TOOL_REGISTRY): a new tool cannot ship untiered.
# - every non-"report" tool ∈ fanout.MUTATION_TOOL_NAMES (tiers at least as
#   strict as the existing mutation classifier).
# - "report"-tier ∩ MUTATION_TOOL_NAMES == {notify, search_recent_prs} —
#   the two credential-containment entries that do not themselves mutate.
#   notify stays available in Observe: it is the reporting channel, and
#   Observe is "report only", not "silent".
_TOOL_TIERS: Final[dict[str, str]] = {
    "drift_read_live_env":        "report",
    "read_project_inventory":     "report",
    "drift_patch_docs":           "propose",
    "drift_propose_rollback":     "propose",
    "notify":                     "report",
    "load_contract":              "report",
    "search_recent_prs":          "report",
    "upgrade_read_dependencies":  "report",
    "upgrade_propose_pr":         "propose",
    "upgrade_close_pr":           "propose",
    "upgrade_merge_pr":           "apply",
    "search_developer_docs":      "report",
    "retrieve_developer_doc":     "report",
    "provision_open_infra_pr":    "propose",
    "provision_propose_adoption": "propose",
    # Reserved (callable None — can never resolve): tier is moot but must
    # exist so the set-equality drift-pin holds.
    "get_session_state":          "report",
    "set_session_state":          "report",
}

TOOL_TIERS: Final[Mapping[str, str]] = MappingProxyType(_TOOL_TIERS)
```

(`registry.py` must not import `agent.autonomy` — the tier *names* are plain strings here; `autonomy.py` owns the semantics. Import direction stays acyclic: autonomy ← nothing; registry ← adk_tools; capabilities/adk_agent → both.)

**Step 4: Run** → green. **Step 5: Commit** `feat(autonomy): TOOL_TIERS registry classification + drift-pins`

---

### Task 4: routes — `GET /autonomy`, `POST /autonomy`, fail-closed helper, actor-derivation extraction

**Files:**
- Modify: `agent/main.py`
- Test: `tests/integration/test_autonomy_endpoints.py` (model on `tests/integration/test_pause_endpoints.py`)

**Step 1: Failing tests** — mirror the pause endpoint suite. Cover, at minimum:

```python
# tests/integration/test_autonomy_endpoints.py — model each test on its
# test_pause_endpoints.py counterpart (TestClient(app), conftest auth bypass).

def test_get_autonomy_defaults_to_propose_apply(client): ...
    # GET /autonomy → 200 {"mode": "propose_apply", "reason": None, "actor": None,
    #                      "updated_at": None, "read_error": False}
    # + Cache-Control: no-store

def test_post_then_get_round_trip(client): ...
    # POST {"mode": "observe", "reason": "new adopter"} → 200 mode=observe,
    # actor="operator-token", updated_at not None; GET agrees.

def test_post_rejects_unknown_mode(client): ...
    # POST {"mode": "yolo"} → 422 (Literal-constrained request model)

def test_post_rejects_extra_fields(client): ...
    # POST {"mode": "observe", "paused": true} → 422 (extra="forbid")

def test_post_storage_failure_is_502(client, monkeypatch): ...
    # patch.object(state, "set_autonomy", side_effect=RuntimeError) → 502,
    # detail says the toggle did NOT take effect.

def test_get_fail_closed_on_store_error(client, monkeypatch): ...
    # monkeypatch agent.main.get_state to raise → 200 {"mode": "observe",
    # "read_error": True} — the fail-closed state IS the effective state.

def test_routes_require_token(...): ...
    # mirror however test_pause_endpoints pins verify_token on POST/GET.
```

**Step 2: Run** → FAIL (404).

**Step 3: Implement in `agent/main.py`** — place everything adjacent to the pause block (§"Operator pause / kill switch", around line 1928) under a new comment banner `# Operator autonomy dial — ClickOps item 11`:

1. **Imports:** `from agent.autonomy import (AUTONOMY_MODES, AutonomyState, FAIL_CLOSED_REASON as AUTONOMY_FAIL_CLOSED_REASON, autonomy_apply_blocked_detail, read_autonomy_state)` — alias the reason constant; `pause.FAIL_CLOSED_REASON` is already imported unqualified.

2. **Request model**, next to `PauseToggleRequest`:

```python
class AutonomyToggleRequest(BaseModel):
    """Request body for POST /autonomy. ``mode`` is Literal-constrained so an
    unknown mode is a 422 at the edge, never an ambiguous write."""

    mode: Literal["observe", "propose", "propose_apply"]
    reason: str | None = Field(default=None, max_length=500)
    model_config = ConfigDict(extra="forbid")
```

3. **Fail-closed helper**, next to `_pause_state_fail_closed` (same two-layer fail-closed shape — `get_state()` resolution AND the read):

```python
def _autonomy_state_fail_closed() -> AutonomyState:
    """Resolve the StateStore AND read the dial, fail-closed end-to-end.

    Mirrors _pause_state_fail_closed: read_autonomy_state never raises on
    get_autonomy() errors, but get_state() itself can raise (Firestore
    client construction). Fail-closed direction here is mode="observe" —
    the MOST restrictive — not the absent-doc default "propose_apply".
    """
    try:
        state = get_state()
    except Exception:  # noqa: BLE001
        log.warning("autonomy_state_store_unavailable", exc_info=True)
        return AutonomyState(
            mode="observe", reason=AUTONOMY_FAIL_CLOSED_REASON, read_error=True
        )
    return read_autonomy_state(state)
```

4. **Actor-derivation extraction (mechanical refactor):** move the existing CF-Access actor block from `post_pause_route` (the `actor = "operator-token"` … `canonical_operator_email(claims)` lines, currently ~2028–2044) **verbatim** into a module-level helper:

```python
def _operator_actor_from_jwt(cf_access_jwt: str | None) -> str:
    """Best-effort operator attribution for config toggles (pause, autonomy).

    Extracted verbatim from post_pause_route — do not change behavior:
    default "operator-token"; CF-Access JWT upgrade when team domain + aud
    are configured; silent fallback on CfAccessJwtError.
    """
    ...  # the moved block, returning actor
```

`post_pause_route` then calls it. (Pin with the existing pause actor tests — they must stay green untouched.)

5. **Serializer + routes**, mirroring `_serialize_pause_state` / `get_pause_route` / `post_pause_route` exactly (same `Cache-Control: no-store`, same 502-on-write-failure wording, same read-after-write response built from the as-written doc):

```python
def _serialize_autonomy_state(a: AutonomyState) -> dict[str, Any]:
    # copy _serialize_pause_state's updated_at shaping (isoformat handling)
    return {
        "mode": a.mode,
        "reason": a.reason,
        "actor": a.actor,
        "updated_at": <same shaping as pause>,
        "read_error": a.read_error,
    }


@app.get("/autonomy")
def get_autonomy_route(response: Response, _: None = Depends(verify_token)) -> dict:
    """Current autonomy dial state. Fail-closed read serialized at 200 —
    observe/read_error=True IS the system's effective state."""
    response.headers["Cache-Control"] = "no-store"
    return _serialize_autonomy_state(_autonomy_state_fail_closed())


@app.post("/autonomy")
def post_autonomy_route(
    req: AutonomyToggleRequest,
    response: Response,
    _: None = Depends(verify_token),
    cf_access_jwt: str | None = Header(default=None, alias="Cf-Access-Jwt-Assertion"),
) -> dict:
    """Set the autonomy dial. Mirrors POST /pause: token-gated, audited
    (reason/actor/updated_at), 502 when the write fails (the toggle did
    NOT take effect), response built from the as-written document."""
    ...
```

(Match the pause handlers' exact ordering, logging, and error wording; reuse `_operator_actor_from_jwt`.)

**Step 4: Run** the new suite + `tests/integration/test_pause_endpoints.py` (refactor guard) → green. **Step 5: Commit** `feat(autonomy): GET/POST /autonomy routes — fail-closed, audited, pause-mirrored`

---

### Task 5: Layer 0 — tool filtering + instruction note in `adk_agent.py`, mode threading from `main.py`

**Files:**
- Modify: `agent/adk_agent.py` (`build_agent`, `build_chat_agent`, `run_agent`, `run_chat_stream`, `run_chat`)
- Modify: `agent/main.py` (`_run_adk_agent`, the `/chat` handler's `run_chat`/`run_chat_stream` call sites)
- Test: `tests/unit/test_adk_agent_autonomy.py` + extend existing chat/recheck integration tests' patch sites as needed

**Design rule: `autonomy_mode` is a REQUIRED keyword argument** on `build_agent`, `build_chat_agent`, `run_agent`, `run_chat_stream`, `run_chat`. No permissive default — a call site that forgets the dial must fail loudly at code time, not silently run at full autonomy. Update every existing test call site to pass `autonomy_mode="propose_apply"`.

**Step 1: Failing tests**

```python
# tests/unit/test_adk_agent_autonomy.py
# Build agents for the provision + upgrade workloads under each mode and
# assert on the .tools list handed to ADK (match how existing build_agent
# tests construct a WorkloadResolution — reuse their fixtures/monkeypatched
# env for worker URLs).

def test_chat_agent_observe_strips_all_mutation_tools(...):
    # provision workload, mode=observe → no open_infra_pr_tool /
    # propose_adoption_tool callables in agent.tools; read tools remain.

def test_chat_agent_propose_keeps_propose_strips_apply(...):
    # upgrade workload, mode=propose → upgrade_propose_pr present,
    # upgrade_merge_pr absent; mode=propose_apply → both present.

def test_recheck_agent_observe_strips_mutation_tools(...):
    # drift workload via build_agent, mode=observe → drift_patch_docs +
    # drift_propose_rollback absent; reads + notify remain.

def test_instruction_note_present_iff_restricted(...):
    # mode=observe/propose → instruction endswith autonomy_instruction_note(mode);
    # mode=propose_apply → instruction == workload prompt unchanged.

def test_mode_param_is_required(...):
    # calling build_agent(resolution) without autonomy_mode → TypeError.
```

**Step 2: Run** → FAIL.

**Step 3: Implement.** In `agent/adk_agent.py`:

```python
from agent.autonomy import autonomy_instruction_note, filter_tools_for_mode
from agent.workloads.registry import TOOL_TIERS
```

`build_agent` becomes:

```python
def build_agent(workload: WorkloadResolution, *, autonomy_mode: str) -> Agent:
    # ... existing docstring + add: the dial filter runs AFTER the chat-only
    # strip; tiers from registry.TOOL_TIERS; unknown tier fails closed (see
    # agent.autonomy.filter_tools_for_mode).
    recheck_tools = {
        name: fn
        for name, fn in workload.tools.items()
        if name not in CHAT_ONLY_TOOL_NAMES
    }
    allowed = filter_tools_for_mode(recheck_tools, TOOL_TIERS, autonomy_mode)
    instruction = workload.system_prompt
    if autonomy_mode != "propose_apply":
        instruction = f"{instruction}\n\n{autonomy_instruction_note(autonomy_mode)}"
    return Agent(
        name=f"driftscribe_{workload.spec.name}",
        model="gemini-2.5-flash",
        instruction=instruction,
        tools=list(allowed.values()),
        planner=BuiltInPlanner(thinking_config=ThinkingConfig(include_thoughts=True)),
    )
```

`build_chat_agent` identically (over the full `workload.tools`, `chat_system_prompt`). `run_agent` / `run_chat_stream` / `run_chat` each gain `*, autonomy_mode: str` and pass it to their builder.

In `agent/main.py`:
- `_run_adk_agent(user_msg, *, workload="drift", autonomy_mode: str)` — required keyword, forwarded to `run_agent`. (Integration tests patch `agent.main._run_adk_agent` wholesale; update their fake signatures to accept `**kwargs` or the new kwarg.)
- `_do_recheck`: read the dial ONCE near the top (after the existing settings/contract setup, before the LLM call): `autonomy = _autonomy_state_fail_closed()`; pass `autonomy_mode=autonomy.mode` to `_run_adk_agent`; thread `autonomy` into the execution branch (Task 6) and `_do_rollback`.
- `/chat` handler: after the existing pause gate, `autonomy = _autonomy_state_fail_closed()`; pass `autonomy_mode=autonomy.mode` to `run_chat` / `run_chat_stream`. Chat is never refused by the dial — tools are filtered instead.

**Step 4: Run** the new suite + full `tests/unit` → green (fix every call site the required kwarg breaks). **Step 5: Commit** `feat(autonomy): Layer-0 tool filtering — dial-filtered agent builds, mode threaded from handlers`

---

### Task 6: Observe-mode suppression in the drift pipeline (`_do_recheck` + `_do_rollback`)

**Files:**
- Modify: `agent/main.py`
- Test: `tests/integration/test_autonomy_gates.py` (model on `tests/integration/test_pause_gates.py`)

**Step 1: Failing tests**

```python
# tests/integration/test_autonomy_gates.py — part 1 (pipeline suppression).
# Helper mirrors test_pause_gates._pause:
def _set_mode(client, mode, reason="test"):
    r = client.post("/autonomy", json={"mode": mode, "reason": reason})
    assert r.status_code == 200

def test_recheck_observe_records_suppressed_decision_without_github_call(...):
    # Arrange the classifier/ADK path exactly as the existing recheck
    # integration tests do (patch _run_adk_agent / classifier to return a
    # docs_pr or drift_issue proposal). Set mode=observe. POST /recheck.
    # Assert: 200; response["suppressed_by_autonomy"] is True;
    # response["autonomy_mode"] == "observe";
    # response["github"] == {"suppressed_by_autonomy": "observe", "url": None,
    #                        "action": "<action>"};
    # patched open_docs_pr / open_drift_issue / open_escalation_issue NOT called;
    # decision persisted (GET /decisions contains it with both new fields);
    # event claimed (a second identical recheck returns the cached decision).

def test_recheck_observe_no_op_unchanged(...):
    # no_op proposal in observe → github == {"dry_run": ..., "url": None,
    # "action": "no_op"}; no suppression markers... 
    # DECISION: no_op also carries autonomy_mode (every new decision does)
    # but suppressed_by_autonomy is absent/False (nothing was suppressed).

def test_recheck_propose_executes_actions_normally(...):
    # mode=propose → identical to today's behavior for docs_pr (mocked
    # GitHub call IS made; no suppression markers beyond autonomy_mode).

def test_eventarc_observe_still_processes(...):
    # mode=observe + valid eventarc event (patch verify_oauth2_token as the
    # pause suite does) → NOT dropped: _do_recheck runs and the response is
    # the suppressed-decision shape (patch the proposal path). Contrast
    # pin: pause drops with {"ignored": "paused"}; the dial must not.

def test_rollback_observe_suppresses_worker_calls(...):
    # ADK path returns a ROLLBACK proposal; mode=observe.
    # Assert: worker_client.call NEVER called (no "rollback", no "notifier");
    # decision recorded with action="rollback", suppressed_by_autonomy=True,
    # autonomy_mode="observe", NO "approval" key, rendered_body mentions
    # Observe; requires_human_review True; event claimed.

def test_rollback_propose_proposes_normally(...):
    # mode=propose → _do_rollback behaves exactly as today (worker /propose
    # + notifier called, approval key present, autonomy_mode="propose").

def test_recheck_fail_closed_read_suppresses(...):
    # monkeypatch get_state→raise ONLY for the autonomy read? Not separable —
    # instead patch agent.main._autonomy_state_fail_closed to return the
    # fail-closed AutonomyState(mode="observe", read_error=True) and assert
    # the docs_pr action is suppressed. (Direct unit-style pin of the
    # fail-closed wiring at the pipeline seam.)
```

**Step 2: Run** → FAIL.

**Step 3: Implement.**

In `_do_recheck`, the ROLLBACK branch passes the mode: `return _do_rollback(s, proposal, event_key, trigger, autonomy_mode=autonomy.mode)`.

The action-execution site (currently `github_result = _perform_action(s, contract, proposal, rendered)`) becomes:

```python
    if autonomy.mode == "observe" and proposal.action != DecisionAction.NO_OP:
        # Observe: the pipeline observes, decides, and RECORDS — but does not
        # touch GitHub. The decision row is the operator-visible "would have"
        # artifact; the rail renders it distinctly (suppressed_by_autonomy).
        github_result = {
            "suppressed_by_autonomy": "observe",
            "url": None,
            "action": proposal.action.value,
        }
    else:
        github_result = _perform_action(s, contract, proposal, rendered)
```

The response dict gains, on every path (including no_op):

```python
        "autonomy_mode": autonomy.mode,
```

and additionally, only when suppressed:

```python
        "suppressed_by_autonomy": True,
```

In `_do_rollback(s, proposal, event_key, trigger, *, autonomy_mode: str)`: insert the Observe branch immediately after the `use_adk` guard and BEFORE the worker `/propose` call. It claims the event (same idempotency contract — Eventarc retries must not re-run the LLM), records, and returns:

```python
    state = get_state()
    claimed = state.record_event(event_key, {"trigger": trigger})
    if not claimed:
        existing = state.find_decision_for_event(event_key)
        if existing:
            return existing
        raise HTTPException(status_code=409, detail="event in-progress, retry")

    if autonomy_mode == "observe":
        # Observe suppression — deliberate DIVERGENCE from the dry_run
        # behavior documented above (dry_run still calls /propose so demos
        # get an approval URL). The dial is an operator trust boundary, not
        # a demo convenience: in Observe NOTHING leaves the coordinator —
        # no approval doc is minted, no notification is sent. The decision
        # row below is the only artifact.
        rendered = (
            f"DriftScribe proposed a rollback to revision "
            f"{proposal.target_revision} but did not create the approval — "
            f"the autonomy dial is set to Observe. Raise the dial to Propose "
            f"to let rollback proposals mint operator approvals.\n\n"
            f"Rationale: {scrub_rationale_text(proposal.rationale, proposal.env_diffs)}"
        )
        decision_id = str(uuid.uuid4())
        response = {
            "decision_id": decision_id,
            "event_key": event_key,
            "trace_id": current_trace_id_or_new(),
            "action": "rollback",
            "decision_path": "adk",
            "rendered_body": rendered,
            "rationale": proposal.rationale,
            "diffs": [d.model_dump(mode="json") for d in proposal.env_diffs],
            "target_revision": proposal.target_revision,
            "requires_human_review": True,
            "dry_run": s.dry_run,
            # NO "dry_run_effective": that field exists to disambiguate the
            # propose-despite-dry-run behavior, which did not happen here.
            # NO "approval": nothing was minted — readers must not see a
            # null-shaped approval and branch on it.
            "autonomy_mode": "observe",
            "suppressed_by_autonomy": True,
            "trigger": trigger,
        }
        state.record_decision(decision_id, event_key, response)
        return response
```

(The existing claim block above is the function's current first action — restructure so the claim happens once, shared by both branches; the non-observe path continues into `/propose` exactly as today and its response dict gains `"autonomy_mode": autonomy_mode`.)

**Verification step (in-task):** confirm `GET /decisions` serves the two new fields. `_do_recheck` persists the whole response dict and the decisions list endpoint returns stored docs (plus serve-time scrub) — assert in the integration test that the suppressed decision fetched via `GET /decisions?limit=...` carries `suppressed_by_autonomy` and `autonomy_mode`. If a serve-time field allowlist intervenes, extend it.

**Step 4: Run** new tests + the full recheck/rollback integration suites → green. **Step 5: Commit** `feat(autonomy): observe-mode suppression — drift pipeline records would-have decisions, rollback proposals stay coordinator-local`

---

### Task 7: Apply gates — IaC approvals + rollback approvals (POST refusal, GET display)

**Files:**
- Modify: `agent/main.py` (4 sites), `agent/templates/iac_approval.html`, `agent/templates/approval.html`
- Test: extend `tests/integration/test_autonomy_gates.py` (part 2)

**Step 1: Failing tests**

```python
# test_autonomy_gates.py — part 2 (apply gates). Model each on its
# test_pause_gates.py counterpart (same _FakeApprovalStore / CSRF / token
# arrangements), with mode set via _set_mode.

def test_iac_approval_post_refused_in_observe_and_propose(...):
    # POST /iac-approvals/{pr} approve → 409; detail ==
    # autonomy_apply_blocked_detail(mode). Both modes. Ordering pin: with
    # BOTH paused and mode=observe, the 423 pause response wins (dial gate
    # sits after the pause gate).

def test_iac_approval_reject_allowed_in_observe(...):
    # The reject path stays ungated (mirror the pause suite's reject test).

def test_rollback_approval_approve_refused_reject_allowed(...):
    # POST /approvals/{id} decision=approve → 409 in observe/propose;
    # decision=reject → passes through to worker deny (mock) in observe.

def test_apply_gates_open_in_propose_apply(...):
    # mode=propose_apply → existing happy-path behavior (reuse/extend an
    # existing approval-path test arrangement; the gate must be invisible).

def test_iac_approval_get_shows_dial_note(...):
    # GET /iac-approvals/{pr} with mode=propose → 200; Approve suppressed;
    # page contains the dial note copy with severity "pending" (calm), not
    # "error"; response carries the existing probe-safe shape.

def test_rollback_approval_get_shows_dial_note(...):
    # GET /approvals/{id} with mode=observe → Approve disabled + dial note;
    # Reject still rendered active.

def test_apply_gate_fail_closed_read(...):
    # patch _autonomy_state_fail_closed → AutonomyState(mode="observe",
    # read_error=True): approve 409 AND the GET note mentions the
    # fail-closed read (read_error surfaced, mirrors pause display).
```

**Step 2: Run** → FAIL.

**Step 3: Implement.**

- `POST /iac-approvals/{pr_number}`: immediately after the existing pause gate (`if _pause_state_fail_closed().paused: raise 423`):

```python
    # Autonomy dial gate: the apply pipeline is Propose+Apply territory.
    # AFTER the pause gate (pause outranks the dial; both fail closed) and
    # before plan re-resolution. 409, not 423 — this is the operator's own
    # configured mode, not the kill switch; clients must not render it as
    # "paused". The REJECT path above stays ungated (audit no-op).
    _autonomy = _autonomy_state_fail_closed()
    if _autonomy.mode != "propose_apply":
        raise HTTPException(
            status_code=409, detail=autonomy_apply_blocked_detail(_autonomy.mode)
        )
```

- `POST /approvals/{approval_id}`: in the approve branch, immediately after the existing `if pause.paused: raise 423`:

```python
        _autonomy = _autonomy_state_fail_closed()
        if _autonomy.mode != "propose_apply":
            raise HTTPException(
                status_code=409,
                detail=autonomy_apply_blocked_detail(_autonomy.mode),
            )
```

(Reject branch untouched — denying remains the safety direction, same rationale as pause.)

- `GET /iac-approvals/{pr_number}`: read `_autonomy = _autonomy_state_fail_closed()` next to the existing `_pause` read; add a gate-ladder rung AFTER the pause rung with `reason_severity = "pending"` (calm — the artifact is fine; the dial is a choice) and a reason string built from `autonomy_apply_blocked_detail(_autonomy.mode)`, plus `read_error` mention when set. Thread `autonomy_mode` / `autonomy_blocked` into the template context; in `iac_approval.html`, render the note in the same slot/style as the pause note.

- `GET /approvals/{approval_id}`: thread `autonomy_blocked` (bool) + the detail string into the template context next to `paused`; in `approval.html`, disable Approve and show the calm note when set (Reject stays active), mirroring the paused treatment.

**Step 4: Run** part-2 tests + the full `tests/integration/test_pause_gates.py` and iac-approval suites → green. **Step 5: Commit** `feat(autonomy): apply gates — approve refused 409 below Propose+Apply, displays explain the dial`

---

### Task 8: Frontend — `lib/autonomy.ts` + `AutonomyControl.svelte` + App wiring

**Files:**
- Create: `frontend/src/lib/autonomy.ts`, `frontend/src/components/AutonomyControl.svelte`
- Modify: `frontend/src/App.svelte` (import + render next to `<PauseControl {call} />`, line ~388)
- Test: `frontend/tests/unit/AutonomyControl.test.ts`

**Model every behavior on `PauseControl.svelte` / `PauseControl.test.ts`** — same `call` prop contract, same `seq` stale-response guard, same single-flight busy handling, same structural-validation of GET bodies, same inline error `data-testid` pattern. Read both files first and keep idioms identical.

**`frontend/src/lib/autonomy.ts`:**

```typescript
// Autonomy dial — wire types + parsing for GET/POST /autonomy.
// Mirrors the backend contract (agent/autonomy.py): absent-doc default and
// fail-closed semantics live SERVER-side; the client renders what it is told
// and treats anything structurally unexpected as 'unknown'.

export const AUTONOMY_MODES = ['observe', 'propose', 'propose_apply'] as const;
export type AutonomyMode = (typeof AUTONOMY_MODES)[number];

export const MODE_LABELS: Record<AutonomyMode, string> = {
  observe: 'Observe',
  propose: 'Propose',
  propose_apply: 'Propose + Apply',
};

export const MODE_BLURBS: Record<AutonomyMode, string> = {
  observe: 'Watch and report only — no pull requests, no issues, no applies.',
  propose: 'Open pull requests and issues for your review — applies stay off.',
  propose_apply: 'Propose changes and apply them after your approval (current default).',
};

export interface AutonomyDoc {
  mode: AutonomyMode;
  reason: string | null;
  actor: string | null;
  updated_at: string | null;
  read_error: boolean;
}

export function parseAutonomyDoc(body: unknown): AutonomyDoc | null {
  if (typeof body !== 'object' || body === null) return null;
  const b = body as Record<string, unknown>;
  if (!AUTONOMY_MODES.includes(b.mode as AutonomyMode)) return null;
  return {
    mode: b.mode as AutonomyMode,
    reason: typeof b.reason === 'string' ? b.reason : null,
    actor: typeof b.actor === 'string' ? b.actor : null,
    updated_at: typeof b.updated_at === 'string' ? b.updated_at : null,
    read_error: b.read_error === true,
  };
}
```

**`AutonomyControl.svelte`** — structure:

- Props: `{ call }` (same type as PauseControl's).
- On mount: GET `/autonomy` → `parseAutonomyDoc`; null/non-OK → `unknown` state with retry button (PauseControl idiom).
- Render: a labeled three-segment control (`data-testid="autonomy-mode-observe"` etc.), current mode highlighted (`aria-pressed`), one-line blurb for the current mode, audit meta line (actor + relative time + reason) when present.
- `read_error: true` → warn line `data-testid="autonomy-read-error"`: "autonomy state could not be read — failing closed to Observe" (mirror `pause-meta__warn`).
- Clicking a different segment arms a confirm row (`data-testid="autonomy-confirm"` / `autonomy-cancel`) with an optional reason input (`autonomy-reason`, maxlength 500) and the target mode's blurb; confirm POSTs `{mode, reason?}`; response body re-parsed and applied (no optimistic update); POST failure → inline `autonomy-error` + state unchanged.
- Busy/single-flight + `seq` guard exactly as PauseControl.
- Visual: reuse the ds-* token vocabulary used by PauseControl (no new palette); the Observe segment is NOT styled as a warning — modes are choices, not alarms.

**App.svelte:** render `<AutonomyControl {call} />` immediately after `<PauseControl {call} />`.

**Tests (`AutonomyControl.test.ts`)** — mirror PauseControl's 12-test structure: initial render per mode; fetch failure → unknown + retry; malformed 200 body → unknown; segment click arms confirm with correct target; confirm POSTs `{mode, reason}` body and applies response; cancel disarms; POST failure shows `autonomy-error`; busy disables segments; double-confirm single-flight; stale-GET seq guard; `read_error` warn line; blurb text per mode.

**Step order:** failing tests → run (`cd frontend && npx vitest run tests/unit/AutonomyControl.test.ts`) → implement → green → commit `feat(ui): autonomy dial control — three-mode segmented control with audit + fail-closed display`.

---

### Task 9: Frontend — suppressed-decision treatment in the rail

**Files:**
- Modify: `frontend/src/lib/types.ts` (Decision fields), `frontend/src/components/DecisionsRail.svelte`, possibly `frontend/src/lib/format.ts`
- Test: extend `frontend/tests/unit/DecisionsRail.test.ts` (or the rail's existing suite)

**Step 1: types** — add to the decision shape in `types.ts`:

```typescript
  autonomy_mode?: string;
  suppressed_by_autonomy?: boolean;
```

**Step 2: failing test** — a drift decision fixture with `action: 'docs_pr', suppressed_by_autonomy: true, autonomy_mode: 'observe'` renders a status token `data-testid="autonomy-suppressed"` with text `not executed — Observe mode`; a decision without the field renders no such token (stale-coordinator fail-quiet).

**Step 3: implement** — in `DecisionsRail.svelte`, where a drift row's action/status renders, add:

```svelte
{#if d.suppressed_by_autonomy === true}
  <span class="rail-status rail-status--muted" data-testid="autonomy-suppressed"
    >not executed — {d.autonomy_mode === 'observe' ? 'Observe' : d.autonomy_mode} mode</span>
{/if}
```

(match the rail's existing status-token classes — reuse the muted/pending token style used by `iacStatusLabel` statuses, not an alarm style; "would have" rows are the Observe mode working as designed.)

**Step 4: green → commit** `feat(ui): rail marks observe-suppressed decisions as recorded-not-executed`.

---

### Task 10: Frontend — capability card live-mode note

**Files:**
- Modify: `frontend/src/components/CapabilityCard.svelte`
- Test: extend `frontend/tests/unit/CapabilityCard.test.ts`

On first open (where it lazy-fetches `/capabilities`), additionally fire a best-effort `call('/autonomy')`; parse with `parseAutonomyDoc`. When it resolves to a mode ≠ `propose_apply`, render one line above the workloads section, `data-testid="capability-autonomy-note"`:

- observe: `The autonomy dial is currently set to Observe — the write-capable tools listed below are disabled until you raise the dial.`
- propose: `The autonomy dial is currently set to Propose — proposals (pull requests and issues) are enabled, applies are disabled until you raise the dial.`

Fetch failure or malformed body → render nothing (the card stays the static cage description; `AutonomyControl` is the authoritative live surface). Tests: note shown for observe/propose fixture responses, absent for propose_apply, absent on fetch failure.

Commit: `feat(ui): capability card notes the live autonomy mode when restricted`.

---

### Task 11: Full suites, docs touch, PR

1. Repo root: `.venv/bin/pytest -q` → expect previous-baseline 2645 + new tests, 0 failures.
2. `cd frontend && npx vitest run` → 462 + new, 0 failures. `npm run build` clean.
3. Update `docs/plans/2026-06-10-clickops-audience-roadmap.md` item 11 status line (mark shipped, PR #).
4. Branch `feat/autonomy-dial`, PR titled `feat: autonomy dial — Observe / Propose / Propose + Apply, enforced at the tool registry (#item-11)`. PR body: mode×surface matrix table + the three locked decisions + the dry_run-divergence note.

**Ships via:** coordinator rebake ONLY (`agent/` + `frontend/` + templates; no `driftscribe_lib`, no gate/denylist change → tofu-apply / tofu-editor / infra-reader untouched). Mandatory pinned-traffic cutover; pick the new revision by image digest, never `latestReadyRevisionName`.

---

## Live verification plan (post-deploy)

1. `GET /autonomy` (authed) → `{"mode": "propose_apply", ...}` default on prod (doc absent).
2. SPA: AutonomyControl renders Propose + Apply; flip to Observe with a reason → audit line shows actor + reason; Firestore `config/autonomy` doc exists.
3. While in Observe: open `/iac-approvals/<resolved PR>` → approve suppressed with calm dial note; live explore/provision chat asked to adopt → replies it cannot (tool stripped) and points at the Autonomy control.
4. Flip back to **Propose + Apply** (prod's working mode) and re-verify an approval page shows Approve again.
5. Capability card open in Observe shows the live-mode note (check before flipping back).
