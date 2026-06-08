# Backend Rationale Scrub Implementation Plan (Phase 3, PR 2 of 2)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Scrub the persisted `rationale` of any secret-like value (drawn from the decision's own `diffs[]`/`env_diffs`) at **every serve/return boundary** that hands a decision (or rationale) to a caller — closing the raw-rationale leak across the Svelte SPA, the legacy `/ui/transparency-legacy` template, the raw `/runs` + `/recheck` + `/eventarc` API responses, and the rollback approval page — for both already-persisted and new decisions, with NO Firestore backfill.

**Architecture:** A serve-time helper `scrub_decision_rationale(decision)` in `agent/renderer.py` reuses the existing tested `_scrub_secret_values_from_rationale(rationale, diffs)` by coercing stored `diffs[]` dicts back into `EnvDiff` objects. It is applied at the HTTP boundary (the same place `get_trace` already applies `redact_event`), NOT in `StateStore`. The decision doc is otherwise returned verbatim (still unredacted by design); only the free-text `rationale` is scrubbed, and the helper never mutates the source dict (copy-on-change, identity-when-unchanged). The rollback worker `reason` payload — a raw rationale string sent to the worker and rendered on the approval page — is scrubbed at the source via a public `scrub_rationale_text(rationale, env_diffs)` wrapper.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2 (`EnvDiff`/`ContractStatus`, `agent/models.py`), pytest. No new dependencies. No frontend changes.

---

## Background / grounding (read before starting)

The leak (Codex thread `019ea615`, expanded in the completed-plan review): the coordinator persists `rationale: proposal.rationale` **raw** and exposes it on multiple paths. Only `rendered_body` is scrubbed at persist (via the `render_*_body` renderers). The standalone `rationale` (and the rollback `reason`) leak through:

| Path | Site | Consumer | Auth |
|---|---|---|---|
| `GET /trace/{id}` → `decision.rationale` | `agent/main.py:1844` read; returned at 1848 (cache hit) + 1900 (fresh) | Svelte SPA hero (`App.svelte:~307`) + legacy template (`transparency_legacy.html:2130`) | token |
| `GET /decisions` → each `rationale` | `agent/main.py:1707` | SPA rail + legacy | token |
| `GET /runs/{decision_id}` → raw decision | `agent/main.py:1665` returns `get_state().get_decision(...)` | raw API / e2e | **NONE** |
| `POST /recheck` → decision `response` | `_do_recheck` builds `response` w/ raw `rationale` at `1388`, returns at `1398`; `/recheck` returns it at `1431` | API caller | token |
| `POST /eventarc` → decision `response` | same `_do_recheck`; `/eventarc` returns at `1658` | Eventarc (machine) | OIDC |
| rollback `response.rationale` | `_do_rollback` builds at `985`; flows out via `_do_recheck`→`/recheck`+`/eventarc` (called at `1345`) | API caller | token |
| rollback approval-page `reason` | `_do_rollback` sends `reason: proposal.rationale` to worker at `902`; `workers/rollback/main.py:303` — *"the approval page renders this"* | operator approval page | approval token |

Both UIs fetch the same `/trace` + `/decisions` JSON client-side, so scrubbing those covers them. The notification body (`render_rollback_body`, line 935) is **already scrubbed** — do not touch it. `rendered_body` is **already scrubbed** — do not touch it.

**The reused function** (`agent/renderer.py:68`) reads only `d.name`, `d.expected`, `d.live`, `d.debug_config_value`, `d.recent_pr_match` (never `contract_status`), replaces each secret-like value (`should_redact(name, v)` true) with `(redacted)`, dedups, and only acts when `len(v) >= 4`:
```python
def _scrub_secret_values_from_rationale(rationale: str, diffs: list[EnvDiff]) -> str:
    scrubbed = rationale
    seen: set[str] = set()
    def _scrub(v: str | None) -> None:
        nonlocal scrubbed
        if v and v not in seen and len(v) >= 4:
            scrubbed = scrubbed.replace(v, "(redacted)")
            seen.add(v)
    for d in diffs:
        for v in (d.expected, d.live, d.debug_config_value):
            if should_redact(d.name, v):
                _scrub(v)
        if should_redact(d.name, d.recent_pr_match):
            _scrub(d.recent_pr_match)
    return scrubbed
```

**Stored diff shape** (`EnvDiff.model_dump(mode="json")`): `{name, expected, live, contract_status, debug_config_value, recent_pr_match}`. `contract_status` is a REQUIRED `EnvDiff` field with no default — coercion must supply one (any value; scrub ignores it).

**Design decisions (locked):**
- **Serve-time at the HTTP boundary** (not StateStore): keeps the data layer pure; mirrors `redact_event`-at-handler precedent; covers historical docs with no backfill; uniform for new + old docs (persisted data stays raw, every read scrubs). For the POST/recheck and eventarc paths, wrapping the **handler return** covers every internal return (fresh `response`, cached `existing`, and the rollback `response` routed through `_do_recheck`) in ONE site each.
- **Rollback `reason` scrubbed at the source** (line 902) — we don't control the worker's serve path, so the only fix is to send a scrubbed string.
- **Out of scope:** persist-time scrub of `985`/`1388` (serve-time covers all reads; YAGNI); retiring the legacy route (no longer a leak once scrubbed); ANY frontend change (rationale must NOT be scrubbed client-side — single source of truth = backend); the rollback notification body + `rendered_body` (already scrubbed).

---

## Task 1: `_coerce_env_diffs` — stored diff dicts → `EnvDiff[]`

**Files:**
- Modify: `agent/renderer.py` (add helper; extend `from agent.models import ...` to include `ContractStatus`)
- Test: `tests/unit/test_renderer_scrub_decision.py` (new)

**Step 1: Write the failing test**

```python
# tests/unit/test_renderer_scrub_decision.py
import pytest
from agent.models import ContractStatus, EnvDiff
from agent.renderer import _coerce_env_diffs


def test_coerce_well_formed_diffs():
    raw = [{"name": "API_TOKEN", "expected": "old", "live": "new",
            "contract_status": "present_disallow_manual",
            "debug_config_value": None, "recent_pr_match": None}]
    out = _coerce_env_diffs(raw)
    assert len(out) == 1 and isinstance(out[0], EnvDiff)
    assert (out[0].name, out[0].expected, out[0].live) == ("API_TOKEN", "old", "new")


def test_coerce_tolerates_missing_or_invalid_contract_status():
    raw = [{"name": "API_TOKEN", "expected": "old", "live": "new"},       # no status
           {"name": "X", "live": "y", "contract_status": "not-a-status"}] # bad status
    out = _coerce_env_diffs(raw)
    assert len(out) == 2
    assert all(isinstance(d.contract_status, ContractStatus) for d in out)


def test_coerce_skips_non_dict_entries_and_non_list_input():
    assert _coerce_env_diffs("nope") == []
    assert _coerce_env_diffs(None) == []
    out = _coerce_env_diffs([{"name": "A", "live": "1"}, "garbage", 42, None])
    assert [d.name for d in out] == ["A"]


def test_coerce_defaults_nameless_diff_to_empty_name():
    # A diff with no string name still scrubs a credentialed-URL value:
    # should_redact("", url) is True via value_looks_credentialed.
    out = _coerce_env_diffs([{"live": "https://u:p@h/x"}])
    assert len(out) == 1 and out[0].name == "" and out[0].live == "https://u:p@h/x"


def test_coerce_coerces_non_string_value_fields_to_none():
    out = _coerce_env_diffs([{"name": "A", "expected": 123, "live": ["x"]}])
    assert out[0].expected is None and out[0].live is None
```

**Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_renderer_scrub_decision.py -x -q`
Expected: FAIL — `ImportError: cannot import name '_coerce_env_diffs'`

**Step 3: Implement**

In `agent/renderer.py`, extend the import: `from agent.models import ContractStatus, DecisionProposal, EnvDiff`. Add near `_scrub_secret_values_from_rationale`:
```python
def _coerce_env_diffs(raw: object) -> list[EnvDiff]:
    """Rebuild ``EnvDiff`` objects from a persisted decision's ``diffs[]``
    (plain dicts from ``model_dump``) so they can feed
    :func:`_scrub_secret_values_from_rationale` at serve time.

    Defensive — the doc is whatever Firestore holds (possibly malformed or
    legacy). Non-dict entries are skipped. Missing/invalid ``contract_status``
    defaults to ``ABSENT`` (scrub never reads it). A non-string ``name`` becomes
    ``""`` so a credentialed-URL value is still caught by value. Non-string
    value fields collapse to ``None``.
    """
    if not isinstance(raw, list):
        return []
    out: list[EnvDiff] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            status = ContractStatus(item.get("contract_status"))
        except (ValueError, TypeError):
            status = ContractStatus.ABSENT

        def _s(key: str) -> str | None:
            v = item.get(key)
            return v if isinstance(v, str) else None

        name = item.get("name")
        out.append(EnvDiff(
            name=name if isinstance(name, str) else "",
            expected=_s("expected"),
            live=_s("live"),
            contract_status=status,
            debug_config_value=_s("debug_config_value"),
            recent_pr_match=_s("recent_pr_match"),
        ))
    return out
```

**Step 4: Run to verify pass** — `uv run pytest tests/unit/test_renderer_scrub_decision.py -x -q` → PASS (5).

**Step 5: Commit**
```bash
git add agent/renderer.py tests/unit/test_renderer_scrub_decision.py
git commit -m "feat(renderer): _coerce_env_diffs — stored diff dicts → EnvDiff for serve-time scrub"
```

---

## Task 2: `scrub_decision_rationale` + `scrub_rationale_text`

**Files:**
- Modify: `agent/renderer.py`
- Test: `tests/unit/test_renderer_scrub_decision.py` (extend)

**Step 1: Write the failing tests**

```python
# append to tests/unit/test_renderer_scrub_decision.py
from agent.models import DecisionAction, DecisionProposal
from agent.renderer import scrub_decision_rationale, scrub_rationale_text


def _doc(rationale, diffs):
    return {"action": "drift_issue", "trace_id": "a" * 32, "decision_id": "d1",
            "rationale": rationale, "rendered_body": "BODY", "diffs": diffs}


def test_scrub_redacts_secret_by_name_value_in_rationale():
    doc = _doc("API_TOKEN changed from sk-OLD-123456 to sk-NEW-789012.",
               [{"name": "API_TOKEN", "expected": "sk-OLD-123456", "live": "sk-NEW-789012",
                 "contract_status": "present_disallow_manual"}])
    out = scrub_decision_rationale(doc)
    assert "sk-OLD-123456" not in out["rationale"]
    assert "sk-NEW-789012" not in out["rationale"]
    assert "API_TOKEN" in out["rationale"]            # var name survives
    assert out["rendered_body"] == "BODY"             # rendered_body untouched
    assert out["diffs"] == doc["diffs"]               # diffs left raw


def test_scrub_redacts_credentialed_url_value_with_nonsecret_name():
    doc = _doc("ENDPOINT now points at https://admin:hunter2@svc.internal/api.",
               [{"name": "ENDPOINT", "expected": None,
                 "live": "https://admin:hunter2@svc.internal/api", "contract_status": "absent"}])
    out = scrub_decision_rationale(doc)
    assert "hunter2" not in out["rationale"]
    assert "https://admin:hunter2@svc.internal/api" not in out["rationale"]


def test_scrub_redacts_recent_pr_match_and_debug_config_value():
    # The reused scrubber also covers recent_pr_match (for a secret-named var)
    # and debug_config_value — pin that the serve-time path keeps that coverage.
    doc = _doc("see PR https://github.com/x/x/pull/9?leak=zzzz9999 ; cfg was qqqq8888",
               [{"name": "OAUTH_KEY", "live": "zzzz9999",
                 "recent_pr_match": "https://github.com/x/x/pull/9?leak=zzzz9999",
                 "debug_config_value": "qqqq8888", "contract_status": "absent"}])
    out = scrub_decision_rationale(doc)
    assert "zzzz9999" not in out["rationale"]
    assert "qqqq8888" not in out["rationale"]


def test_scrub_leaves_benign_rationale_unchanged_by_identity():
    doc = _doc("Three variables drifted; secrets are redacted in the table.",
               [{"name": "LOG_LEVEL", "expected": "info", "live": "debug",
                 "contract_status": "present_allow_manual"}])
    assert scrub_decision_rationale(doc) is doc      # no needless copy


def test_scrub_does_not_mutate_input_doc():
    secret = "sk-OLD-123456"
    doc = _doc(f"value was {secret}",
               [{"name": "API_TOKEN", "live": secret, "contract_status": "present_disallow_manual"}])
    out = scrub_decision_rationale(doc)
    assert doc["rationale"] == f"value was {secret}"  # original untouched
    assert out is not doc and secret not in out["rationale"]


@pytest.mark.parametrize("doc", [
    None,
    {"action": "no_op"},                  # no rationale key
    {"rationale": None, "diffs": []},     # null rationale
    {"rationale": "", "diffs": []},       # empty rationale
    {"rationale": 123, "diffs": []},      # non-str rationale
    {"rationale": "hi", "diffs": None},   # no diffs
])
def test_scrub_handles_missing_or_malformed_inputs(doc):
    assert scrub_decision_rationale(doc) is doc       # never raises; identity


def test_scrub_rationale_text_scrubs_against_typed_env_diffs():
    p = DecisionProposal(
        action=DecisionAction.ROLLBACK,
        env_diffs=[EnvDiff(name="API_TOKEN", expected=None, live="sk-LEAK-4242",
                           contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL)],
        target_revision="rev-abc",
        rationale="rolling back; API_TOKEN was sk-LEAK-4242.",
        requires_human_review=True,
    )
    out = scrub_rationale_text(p.rationale, p.env_diffs)
    assert "sk-LEAK-4242" not in out and "API_TOKEN" in out


def test_scrub_rationale_text_leaves_benign_unchanged():
    p = DecisionProposal(
        action=DecisionAction.ROLLBACK,
        env_diffs=[EnvDiff(name="PAYMENT_MODE", expected="mock", live="live",
                           contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL)],
        target_revision="rev-abc", rationale="PAYMENT_MODE drifted mock→live.",
        requires_human_review=True,
    )
    assert scrub_rationale_text(p.rationale, p.env_diffs) == p.rationale
```
(`DecisionProposal` may require extra fields — match `agent/models.py`; `confidence`/`requires_human_review` as in `tests/integration/test_rollback_e2e.py:_rollback_proposal`.)

**Step 2: Run to verify it fails** — `ImportError: cannot import name 'scrub_decision_rationale'`.

**Step 3: Implement** — in `agent/renderer.py`:
```python
def scrub_decision_rationale(decision: object) -> object:
    """Serve-time defense: return the decision doc with its free-text
    ``rationale`` scrubbed of any secret-like value present in its own
    ``diffs[]``. Closes the raw-rationale leak on every decision serve/return
    boundary (GET /trace, /decisions, /runs; POST /recheck, /eventarc),
    including already-persisted docs — no Firestore backfill.

    The doc is otherwise returned verbatim (decision is unredacted by design;
    ``rendered_body`` is already scrubbed at persist, ``diffs[]`` are left raw).
    Never mutates the input: returns it unchanged by identity when there is
    nothing to scrub, else a shallow copy with the new ``rationale``. Accepts
    ``object`` and returns non-dict inputs as-is; never raises.
    """
    if not isinstance(decision, dict):
        return decision
    rationale = decision.get("rationale")
    if not isinstance(rationale, str) or not rationale:
        return decision
    scrubbed = _scrub_secret_values_from_rationale(
        rationale, _coerce_env_diffs(decision.get("diffs"))
    )
    if scrubbed == rationale:
        return decision
    return {**decision, "rationale": scrubbed}


def scrub_rationale_text(rationale: str, env_diffs: list[EnvDiff]) -> str:
    """Public wrapper over the rationale scrubber for callers holding typed
    ``EnvDiff`` objects (the rollback worker ``reason`` boundary, where the
    approval page renders the string). Decision-doc callers use
    :func:`scrub_decision_rationale` instead."""
    return _scrub_secret_values_from_rationale(rationale, env_diffs)
```

**Step 4: Run to verify pass** — full module green.

**Step 5: Commit**
```bash
git add agent/renderer.py tests/unit/test_renderer_scrub_decision.py
git commit -m "feat(renderer): scrub_decision_rationale + scrub_rationale_text (serve-time, copy-on-change)"
```

---

## Task 3: Wire `GET /trace/{id}` + `GET /runs/{id}`

**Files:**
- Modify: `agent/main.py` (renderer import ~44; `get_trace` decision read ~1844; `get_run` ~1665)
- Test: `tests/integration/test_trace_endpoint.py` (+1); `tests/integration/test_run_endpoint.py` (new, or extend an existing `/runs` test if one exists)

**Step 1: Write the failing tests**

```python
# tests/integration/test_trace_endpoint.py
def test_trace_endpoint_scrubs_secret_in_rationale():
    state = get_state()
    state.record_event("ev-scrub", {})
    secret = "sk-LEAK-9999"
    state.record_decision("dec-scrub", "ev-scrub", {
        "action": "drift_issue", "trace_id": _TRACE_A, "event_key": "ev-scrub",
        "rationale": f"API_TOKEN rotated to {secret} per the contract.",
        "diffs": [{"name": "API_TOKEN", "expected": None, "live": secret,
                   "contract_status": "present_disallow_manual"}],
    })
    _install_fetcher(_stub_with([]))
    resp = TestClient(app).get(f"/trace/{_TRACE_A}")
    assert resp.status_code == 200
    assert secret not in resp.text
    body = resp.json()
    assert secret not in body["decision"]["rationale"]
    assert "API_TOKEN" in body["decision"]["rationale"]
```
```python
# tests/integration/test_run_endpoint.py  (mirror get_state()/record_decision/TestClient)
from agent.main import app, get_state
from fastapi.testclient import TestClient

def test_runs_endpoint_scrubs_secret_in_rationale():
    state = get_state()
    secret = "sk-RUN-5555"
    state.record_decision("dec-run", "ev-run", {
        "decision_id": "dec-run", "action": "drift_issue", "trace_id": "d" * 32,
        "rationale": f"TOKEN set to {secret}.",
        "diffs": [{"name": "TOKEN", "live": secret, "contract_status": "present_disallow_manual"}],
    })
    resp = TestClient(app).get("/runs/dec-run")
    assert resp.status_code == 200
    assert secret not in resp.text
    assert secret not in resp.json()["rationale"] and "TOKEN" in resp.json()["rationale"]

def test_runs_endpoint_404_unchanged():
    assert TestClient(app).get("/runs/does-not-exist").status_code == 404
```
(Check `tests/integration/test_token_guard.py` / `test_recheck_dry_run.py` for the existing `/runs` usage + how `decision_id` is set, and reuse their `get_state` reset fixture if present.)

**Step 2: Run to verify it fails** — raw secret present in `resp.text` for both.

**Step 3: Implement** — `agent/main.py`:
```python
from agent.renderer import (
    render_docs_pr_body,
    render_drift_issue_body,
    render_escalation_issue_body,
    render_rollback_body,
    scrub_decision_rationale,
    scrub_rationale_text,
)
```
`get_trace` (~1844): `decision = scrub_decision_rationale(state.find_decision_by_trace_id(trace_id))`
`get_run` (~1665):
```python
    d = get_state().get_decision(decision_id)
    if not d:
        raise HTTPException(status_code=404, detail="decision not found")
    return scrub_decision_rationale(d)
```

**Step 4: Run to verify pass** — both files green (incl. the existing benign enrichment test).

**Step 5: Commit**
```bash
git add agent/main.py tests/integration/test_trace_endpoint.py tests/integration/test_run_endpoint.py
git commit -m "fix(trace,runs): scrub persisted rationale at GET /trace and GET /runs"
```

---

## Task 4: Wire `GET /decisions`

**Files:** Modify `agent/main.py` (`list_decisions_endpoint` ~1707); Test `tests/integration/test_decisions_endpoint.py` (+1).

**Step 1: Failing test**
```python
def test_list_decisions_scrubs_secret_in_rationale():
    state = get_state()
    secret = "sk-RAIL-7777"
    state.record_decision("dec-rail", "ev-rail", {
        "decision_id": "dec-rail", "action": "drift_issue", "trace_id": "c" * 32,
        "rationale": f"DB_PASSWORD changed to {secret}.",
        "diffs": [{"name": "DB_PASSWORD", "live": secret, "contract_status": "present_disallow_manual"}],
    })
    resp = TestClient(app).get("/decisions?limit=50")
    assert resp.status_code == 200 and secret not in resp.text
    row = next(d for d in resp.json()["decisions"] if d.get("decision_id") == "dec-rail")
    assert secret not in row["rationale"] and "DB_PASSWORD" in row["rationale"]
```
(Match this file's existing `record_decision` signature + fixtures — see `test_list_decisions_returns_newest_first`.)

**Step 2: Fails** — raw `sk-RAIL-7777` in `resp.text`.

**Step 3: Implement** — `list_decisions_endpoint`:
```python
    response.headers["Cache-Control"] = "no-store"
    return {"decisions": [
        scrub_decision_rationale(d) for d in state.list_decisions(limit=limit)
    ]}
```

**Step 4: Pass** — file green. **Step 5: Commit**
```bash
git add agent/main.py tests/integration/test_decisions_endpoint.py
git commit -m "fix(decisions): scrub persisted rationale at GET /decisions"
```

---

## Task 5: Wire `POST /recheck` + `POST /eventarc` returns

**Files:** Modify `agent/main.py` (`recheck` return ~1431; `eventarc` `_do_recheck` return ~1658); Test: extend `tests/integration/test_drift_recheck_deterministic.py` (or `test_recheck_dry_run.py`) +1.

Wrapping the two HANDLER returns covers every internal `_do_recheck` return (fresh `response`, cached `existing`) AND the rollback `response` (which `_do_recheck` returns via `_do_rollback` at line 1345) — one site each.

**Step 1: Failing test** — drive `/recheck` so the deterministic classifier emits a drift decision whose rationale quotes a secret-named diff value, then assert the response body's `rationale` is scrubbed. Mirror the harness in `test_drift_recheck_deterministic.py` (reader-worker env mock, contract with a secret-named disallow-manual var, `DRY_RUN`/no real side effects). Skeleton:
```python
def test_recheck_response_scrubs_secret_in_rationale(monkeypatch):
    # ... set up reader env + contract so the proposal's rationale quotes a
    #     secret-named var's value (e.g. API_TOKEN), same pattern as the file's
    #     existing deterministic tests ...
    resp = client.post("/recheck")
    assert resp.status_code == 200
    assert SECRET_VALUE not in resp.text
    assert SECRET_VALUE not in resp.json().get("rationale", "")
```
If shaping a secret-quoting rationale through the deterministic classifier is awkward, instead assert at the unit boundary that `recheck`/`eventarc` pass their `_do_recheck` result through `scrub_decision_rationale` (e.g. monkeypatch `_do_recheck` to return a secret-quoting decision dict and assert the response is scrubbed) — this directly pins the wrap without depending on classifier internals.

**Step 2: Fails** — raw secret in `/recheck` response.

**Step 3: Implement** — `agent/main.py`:
```python
# recheck (~1431)
    return scrub_decision_rationale(
        await _do_recheck("manual_recheck", force=force, workload=workload)
    )
# eventarc in-scope dispatch (~1658)
    return scrub_decision_rationale(await _do_recheck("eventarc", workload="drift"))
```
(Leave the `/eventarc` early-return `{"ignored": ...}` / `{service, region}` branches alone — `scrub_decision_rationale` passes non-rationale dicts through unchanged anyway, but only the `_do_recheck` return needs wrapping.)

**Step 4: Pass** — `uv run pytest tests/integration/test_drift_recheck_deterministic.py tests/integration/test_recheck_dry_run.py tests/integration/test_eventarc.py -q` green.

**Step 5: Commit**
```bash
git add agent/main.py tests/integration/test_drift_recheck_deterministic.py
git commit -m "fix(recheck,eventarc): scrub rationale in the decision response body"
```

---

## Task 6: Scrub the rollback worker `reason` at the source

**Files:** Modify `agent/main.py` (`_do_rollback`, line 902); Test: extend `tests/integration/test_rollback_e2e.py` (+1).

The rollback worker stores `reason` and `workers/rollback/main.py:303` renders it on the approval page. The notification body is already scrubbed; the `reason` is not. Scrub at the source — we don't control the worker's serve path.

**Step 1: Failing test** — add to `test_rollback_e2e.py`, mirroring `test_rollback_recheck_routes_through_worker_and_renders_approval_url` but with a SECRET-quoting proposal:
```python
def _rollback_proposal_with_secret() -> DecisionProposal:
    secret = "sk-ROLL-1234"
    return DecisionProposal(
        action=DecisionAction.ROLLBACK,
        env_diffs=[EnvDiff(name="API_TOKEN", expected="old", live=secret,
                           contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL)],
        target_revision=_TARGET_REVISION,
        rationale=f"API_TOKEN drifted to {secret}; rolling back to {_TARGET_REVISION}.",
        confidence=0.9, requires_human_review=True,
    )

def test_rollback_reason_payload_is_scrubbed(monkeypatch):
    # ... same monkeypatch/setenv/patch harness as the existing rollback test,
    #     but mock_run_agent returns _rollback_proposal_with_secret() ...
    rollback_payload = [c for c in m_call.call_args_list if c.args[0] == "rollback"][0].args[1]
    assert "sk-ROLL-1234" not in rollback_payload["reason"]
    assert "API_TOKEN" in rollback_payload["reason"]
```
The existing `test_rollback_recheck_routes_through_worker_and_renders_approval_url` (asserting `reason == _rollback_proposal().rationale` at line ~235) stays GREEN — that proposal uses the non-secret `PAYMENT_MODE`, so scrubbing is a no-op. It is the "benign → unchanged" regression guard.

**Step 2: Fails** — raw `sk-ROLL-1234` in the rollback `reason`.

**Step 3: Implement** — `agent/main.py:902`:
```python
                "reason": scrub_rationale_text(proposal.rationale, proposal.env_diffs),
```

**Step 4: Pass** — `uv run pytest tests/integration/test_rollback_e2e.py -q` green (new + existing).

**Step 5: Commit**
```bash
git add agent/main.py tests/integration/test_rollback_e2e.py
git commit -m "fix(rollback): scrub the worker reason payload (approval page no longer leaks)"
```

---

## Task 6.5: Chat rollback tool — don't forward model-authored `reason` (added during completed-work review)

**Files:** Modify `agent/adk_tools.py` (`propose_rollback_tool`, line ~90); Test `tests/unit/test_adk_tools.py`.

The Codex completed-work review found a **fourth** rollback-`reason` boundary: the `/chat` tool `propose_rollback_tool(target_revision, reason)` calls the rollback worker **directly** (bypassing `_do_rollback` and its source scrub). The worker renders `reason` on the operator approval page, and the chat LLM sees live env **unredacted** (`read_live_env_tool` → reader worker returns raw `env`, verified at `workers/reader/main.py:91`), so the model can quote **any** secret form (bare token or credentialed URL) into `reason`. This tool has no `EnvDiff` context for a value-scoped scrub, so a partial text-scrub would be incomplete.

**Fix:** do NOT forward the model-authored `reason`. Send a safe `reason` derived only from the non-secret `target_revision`; the model's full rationale stays in the chat conversation/trace. `_ = reason` keeps the tool's LLM-facing signature intact while documenting the intentional non-forward.

**Tests:** updated `test_propose_rollback_tool_*` to assert the payload carries a revision-derived safe reason; new `test_propose_rollback_tool_does_not_forward_secret_reason` proves a bare token AND a credentialed URL in the model `reason` never reach the worker payload.

(`call_execute`/`call_deny` carry no `reason`; grep confirmed `main.py:900` + `adk_tools.py:90` are the only two reason-bearing rollback-propose sites.)

---

## Task 7: Full-suite verification gate + self-audit

**Step 1:** `uv run pytest -q` → all green (no regressions; ~14 new tests).

**Step 2:** `uv run ruff check agent/renderer.py agent/main.py tests/unit/test_renderer_scrub_decision.py tests/integration/` → clean. (Run mypy/pyright on `agent/renderer.py` + `agent/main.py` if CI does.)

**Step 3: Self-audit grep**
```bash
git diff main --stat            # ONLY agent/renderer.py, agent/main.py, + test files. NO frontend.
grep -n '"rationale": proposal.rationale' agent/main.py   # persist sites 985 + 1388 unchanged by design
grep -rn "scrub_decision_rationale\|scrub_rationale_text" agent/main.py  # 5 decision boundaries + 1 reason
```
Expected: all five decision-serve boundaries wrapped (`/trace`, `/decisions`, `/runs`, `/recheck`, `/eventarc`), the rollback `reason` scrubbed, persist sites and frontend untouched.

---

## Definition of done (PR 2)

- Every decision serve/return boundary (`GET /trace`, `GET /decisions`, `GET /runs/{id}`, `POST /recheck`, `POST /eventarc`) returns `rationale` with secret-like values (by name OR credentialed URL, from the doc's own `diffs[]`) replaced by `(redacted)`; var names + benign prose survive.
- The rollback worker `reason` (rendered on the approval page) is scrubbed at the source.
- Covers SPA, legacy route, raw API, approval page, and already-persisted docs — no Firestore backfill.
- `scrub_decision_rationale` never mutates the source doc and never raises; `rendered_body`/`diffs[]`/persist sites/frontend untouched.
- Full pytest suite green.

## Deploy (SEPARATE — pause for operator go-ahead)

Coordinator change → redeploy:
1. Merge PR 2 to `main`.
2. Trigger the coordinator-update Cloud Build.
3. **Traffic-pinning gotcha** ([[coordinator_deploy_traffic_pinning]]): `driftscribe-agent` spec.traffic pins a specific revision → the build creates the new revision at **0%**. After the build: `gcloud run services update-traffic driftscribe-agent --to-revisions=<new-rev>=100 --region=...`.
4. Smoke: open a historical drift decision in the SPA + `/ui/transparency-legacy`; confirm the hero rationale shows `(redacted)` where a secret would have been, the env-diff card still renders, and a rollback approval page's `reason` is scrubbed.

Do NOT deploy without explicit operator go-ahead (operator gcloud / owner creds).
