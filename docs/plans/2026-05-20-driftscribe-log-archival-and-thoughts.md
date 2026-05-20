# DriftScribe Log Retention + Thought-Summary Capture Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Extend Cloud Logging `_Default` bucket retention from 30 days to 365 days, and surface Gemini 2.5 Flash's thought summaries (plus per-event tool calls and per-call thinking-token usage) through DriftScribe's existing structured JSON log pipeline so the same retention extension preserves them automatically.

**Architecture:** Phase 18.A is a single `gcloud logging buckets update` call wired into the existing bootstrap script — no sink, no BigQuery, no GCS bucket, no IAM grants beyond what's already there. Phase 18.B wires ADK's `BuiltInPlanner(ThinkingConfig(include_thoughts=True))` into both agent builders and extends the existing event loop in `agent/adk_agent.py` to emit one structured log line per thought summary, per tool call, and per LLM usage payload. Output of 18.B rides 18.A's retention extension for free because everything is already a `log.info(...)` JSON line going through `driftscribe_lib.logging.setup`.

**Tech Stack:** `gcloud logging buckets update`, `google-adk` `BuiltInPlanner`, `google.genai.types.ThinkingConfig`, ADK's `Event.partial` / `Event.usage_metadata.thoughts_token_count`, existing `driftscribe_lib.logging` + `agent.workload_context.current_workload`.

---

## Background facts that shaped this plan

1. **A log sink mirrors at ingestion — there is no "after 30 days" trigger.** Earlier framing called for moving logs to cheap storage *after* the 30-day window. That misunderstands how Cloud Logging works: a sink writes to its destination from the moment a log is ingested, in parallel with the `_Default` bucket. So the actual question is just "how long should we hold the durable copy, and where." The simplest correct answer is to extend `_Default`'s own retention — `$0.01/GiB-month` for storage beyond day 30, no new infra, queryable via Logs Explorer exactly as it is today.
2. **Gemini 2.5 Flash exposes "thought summaries", not raw chain-of-thought.** The model returns a self-summary of its reasoning when `ThinkingConfig(include_thoughts=True)` is set; the raw reasoning trace is not accessible. Every external reference in this plan and the implementation uses "thought summary" or "thought" — not "chain of thought".
3. **The coordinator likely already pays for thinking tokens.** `gemini-2.5-flash` uses dynamic thinking by default. The current `Agent(...)` constructions in `agent/adk_agent.py:287-292,310-315` set no `thinking_config`, which means the SDK default applies — thoughts are generated and discarded. Phase 18.B should *not* be advertised as zero-cost; it should *measure* cost by logging `thoughts_token_count` from `event.usage_metadata` so we have real numbers post-deploy.
4. **ADK streams thoughts as partial events, then re-emits merged.** From `/home/adi/driftscribe/.venv/lib/python3.14/site-packages/google/adk/events/event.py:96`, the `partial` field on `Event` is what `is_final_response()` uses to distinguish in-flight chunks from the merged completion. Phase 18.B must filter on `event.partial` so a single thought summary becomes a single log line, not five.
5. **Phase 18.B has an atomic-commit constraint.** Turning on `include_thoughts=True` without simultaneously teaching the event loop to *skip thought parts when collecting the final-text JSON* will break `agent.adk_agent.run_agent`'s response parse — thought text would get concatenated into the JSON blob, the parse would fail, and `/recheck` would 502. The planner enable and the parse-loop fix MUST land in one commit. This is the single most important sequencing constraint in the plan.

---

## Phase 18.A: Extend `_Default` log bucket retention to 365 days

### Task 18.A.1: Add the retention-update call to the bootstrap script

**Files:**
- Modify: `infra/scripts/setup_secrets.sh` (append a new numbered section after the existing last block)
- Test: `tests/integration/test_log_retention_setup.py` (new)

**Step 1: Find the script's current last numbered section**

Run: `grep -nE '^# [0-9]+\.' infra/scripts/setup_secrets.sh | tail`
Expected: Output shows the highest existing section number. The new block is appended as `# (N+1). Log retention`.

**Step 2: Write the failing test**

Create `tests/integration/test_log_retention_setup.py`:

```python
"""Pin the `_Default` log-bucket retention extension in setup_secrets.sh.

Single invariant: the bootstrap script invokes `gcloud logging buckets
update _Default` with `--retention-days=365`. We do NOT call gcloud
from the test — we parse the script body. The intent is a regression
guard: if a future edit drops or shortens this call, every DriftScribe
log line older than 30 days disappears silently.

365 days was chosen for hackathon scope: it's $0.01/GiB-mo beyond day
30, sits inside Cloud Logging's max-without-CMEK cap, and matches the
default table-expiration intent we'd want from any future BQ archive.
"""
from __future__ import annotations

import re
from pathlib import Path

SETUP_SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "setup_secrets.sh"


def _read_script() -> str:
    return SETUP_SCRIPT.read_text()


def test_setup_script_extends_default_bucket_retention_to_365_days():
    body = _read_script()
    # The full command, possibly broken across continuation lines.
    pattern = re.compile(
        r"gcloud\s+logging\s+buckets\s+update\s+_Default[\s\S]*?--retention-days=365",
        re.MULTILINE,
    )
    assert pattern.search(body), (
        "expected `gcloud logging buckets update _Default ... --retention-days=365` "
        "in setup_secrets.sh"
    )


def test_setup_script_pins_default_bucket_location_to_global():
    """`_Default` lives in `--location=global` — pinning the location
    prevents `gcloud` from prompting interactively on first run."""
    body = _read_script()
    # Find the buckets-update block and confirm --location=global appears in it.
    match = re.search(
        r"(gcloud\s+logging\s+buckets\s+update\s+_Default[\s\S]*?)(?=\n# |\Z)",
        body,
    )
    assert match is not None
    assert "--location=global" in match.group(1)
```

**Step 3: Run the test to verify it fails**

Run: `pytest tests/integration/test_log_retention_setup.py -v`
Expected: 2 failures (no matching command in the script yet).

**Step 4: Append the new section to `setup_secrets.sh`**

Find the highest existing section number with `grep -nE '^# [0-9]+\.' infra/scripts/setup_secrets.sh | tail`, then append (replace `N` with `last+1`):

```bash
# --------------------------------------------------------------------------
# N. Log retention — extend `_Default` bucket to 365 days (Phase 18.A)
# --------------------------------------------------------------------------
# Default Cloud Logging `_Default` bucket retention is 30 days. After that,
# every DriftScribe log line (including the thought-summary, tool-call,
# and LLM-usage records emitted by Phase 18.B) ages out and is unrecoverable.
#
# Extending retention is the cheapest, simplest durable-copy option for the
# hackathon's volume profile (<1 GiB/month): no sink, no BigQuery dataset,
# no GCS bucket, no IAM grants. Storage beyond the first 30 days is billed
# at $0.01/GiB-month. The Logs Explorer query surface stays identical.
#
# Idempotent server-side — re-running this on a project that already has
# 365-day retention is a no-op. `--location=global` is explicit so gcloud
# does not prompt for it on a fresh shell.
gcloud logging buckets update _Default \
  --project="$PROJECT" \
  --location=global \
  --retention-days=365 >/dev/null
echo "  log retention: _Default bucket extended to 365 days"
```

**Step 5: Run the test to verify it passes**

Run: `pytest tests/integration/test_log_retention_setup.py -v`
Expected: 2 PASS.

**Step 6: Run the rest of the suite**

Run: `pytest -q`
Expected: All previously-passing tests still pass; 2 new ones added.

**Step 7: Commit**

```bash
git add infra/scripts/setup_secrets.sh tests/integration/test_log_retention_setup.py
git commit -m "feat(infra): extend _Default log bucket retention to 365 days (18.A.1)"
```

---

### Task 18.A.2: Document the retention extension

**Files:**
- Modify: `docs/runbooks/deploy.md` (one new step or one updated step under setup)
- Modify: `README.md` (one bullet under §Cost & latency)
- Modify: `README.ja.md` (matching one bullet)

**Step 1: Read the existing setup section of the runbook**

Run: `grep -n '##\|### ' docs/runbooks/deploy.md | head -30`
Expected: Section list. Identify the section that documents `setup_secrets.sh`.

**Step 2: Insert the retention-verification step**

Immediately after the `setup_secrets.sh` invocation step, add:

```markdown
**Verify the `_Default` log-bucket retention extension landed**

`setup_secrets.sh` extends Cloud Logging's `_Default` bucket retention
from 30 days to 365 days. This holds every DriftScribe log line —
including the thought-summary, tool-call, and LLM-usage events from
Phase 18.B — for a full year. Storage beyond the first 30 days is
billed at $0.01/GiB-month; hackathon volume is well under 1 GiB/month.

Verify:

```bash
gcloud logging buckets describe _Default \
  --project=$PROJECT \
  --location=global \
  --format='value(retentionDays)'
```

Expected: `365`. If the value is still `30`, re-run `setup_secrets.sh`.

Querying example for thought-summary + tool-call replay (after Phase 18.B
is also deployed):

```text
resource.type="cloud_run_revision"
resource.labels.service_name="driftscribe-agent"
jsonPayload.event=("llm_thought" OR "tool_call" OR "llm_usage")
jsonPayload.trace_id="<the trace id you want to replay>"
```

Paste into Logs Explorer; sort ascending by timestamp.
```

**Step 3: Update both READMEs**

In `README.md` under `## Cost & latency`, add:

```markdown
- Log retention: Cloud Logging's `_Default` bucket is extended to 365
  days by `infra/scripts/setup_secrets.sh`. All DriftScribe logs
  (including the agent's thought summaries and tool-call events) are
  preserved and queryable via Logs Explorer for a year. Storage beyond
  day 30 is billed at $0.01/GiB-month; hackathon volume is well under
  the threshold where this matters. See [`docs/runbooks/deploy.md`](docs/runbooks/deploy.md)
  for the verification step and a sample query.
```

In `README.ja.md` under `## コストとレイテンシ`, add:

```markdown
- ログ保持期間: Cloud Logging の `_Default` バケットは
  `infra/scripts/setup_secrets.sh` によって 365 日まで延長されます。
  すべての DriftScribe ログ (エージェントの思考要約とツール呼び出し
  イベントを含む) は 1 年間保持され、Logs Explorer から照会可能です。
  30 日を超えたストレージは $0.01/GiB-月で課金されますが、ハッカソン
  規模ではほぼ無視できます。確認手順とサンプルクエリは
  [`docs/runbooks/deploy.md`](docs/runbooks/deploy.md) を参照してください。
```

**Step 4: Commit**

```bash
git add docs/runbooks/deploy.md README.md README.ja.md
git commit -m "docs: document 365-day log retention in runbook + README (18.A.2)"
```

---

## Phase 18.B: Thought-summary + tool-call + LLM-usage structured logging

> **CRITICAL SEQUENCING:** Task 18.B.1 enables `include_thoughts=True` AND fixes the final-text collection loop in the **same commit**. If you split them, `/recheck`'s JSON parse will swallow thought text and 502. Do not skip ahead.

### Task 18.B.1: Wire `BuiltInPlanner` AND fix final-text collection (atomic)

**Files:**
- Modify: `agent/adk_agent.py` — both `build_agent` (~lines 287-292) and `build_chat_agent` (~lines 310-315) to add the planner; both `run_agent` and `run_chat` event loops to skip thought parts when collecting final-response text
- Test: `tests/unit/test_adk_agent_thinking.py` (new)

**Step 1: Read the current run_chat event loop**

Run: `sed -n '400,435p' agent/adk_agent.py`
Expected: The `async for event in runner.run_async(...)` block. The `is_final_response()` branch around lines 420-423 is what needs to skip thought parts.

**Step 2: Write the failing test**

Create `tests/unit/test_adk_agent_thinking.py`:

```python
"""Pin the BuiltInPlanner wiring and the final-text-skips-thoughts fix.

These two invariants are tested together because they MUST land in the
same commit (Phase 18.B.1). If the planner is enabled without the
final-text filter, run_agent's JSON parse will swallow thought text
and produce a runtime error mid-/recheck. The tests assert both halves
of the invariant.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from google.adk.planners.built_in_planner import BuiltInPlanner
from google.genai.types import ThinkingConfig

from agent import adk_agent
from agent.workloads import load_workload


# --- half 1: planner is wired -----------------------------------------------


def test_build_agent_has_builtin_planner_with_thoughts_enabled():
    resolution = load_workload("drift")
    agent = adk_agent.build_agent(resolution)
    assert isinstance(agent.planner, BuiltInPlanner)
    assert isinstance(agent.planner.thinking_config, ThinkingConfig)
    assert agent.planner.thinking_config.include_thoughts is True


def test_build_chat_agent_has_builtin_planner_with_thoughts_enabled():
    resolution = load_workload("drift")
    agent = adk_agent.build_chat_agent(resolution)
    assert isinstance(agent.planner, BuiltInPlanner)
    assert agent.planner.thinking_config.include_thoughts is True


def test_upgrade_workload_agents_also_have_thoughts_enabled():
    resolution = load_workload("upgrade")
    for builder in (adk_agent.build_agent, adk_agent.build_chat_agent):
        agent = builder(resolution)
        assert agent.planner.thinking_config.include_thoughts is True


# --- half 2: final-text collection skips thought parts ----------------------


class _P:
    def __init__(self, *, text=None, function_call=None, thought=False):
        self.text = text
        self.function_call = function_call
        self.thought = thought


class _Ev:
    def __init__(self, parts, *, partial=False, final=False):
        self.content = SimpleNamespace(parts=parts)
        self.partial = partial
        self._final = final
        self.usage_metadata = None

    def is_final_response(self):
        return self._final


async def _stub_run(*args, **kwargs):
    # One non-partial thought summary, then the merged final JSON.
    yield _Ev(
        [_P(text="reasoning about contract", thought=True)],
        partial=False,
    )
    yield _Ev(
        [
            _P(text="ignored-thought-text", thought=True),
            _P(text='{"action":"no_op","rationale":"matches"}', thought=False),
        ],
        partial=False,
        final=True,
    )


@pytest.mark.asyncio
async def test_run_chat_final_text_excludes_thought_parts():
    """Thought text MUST NOT contaminate run_chat's reply field."""
    with patch.object(adk_agent, "Runner") as runner_cls:
        runner_cls.return_value.run_async = _stub_run
        result = await adk_agent.run_chat("hi", workload="drift")
    assert "ignored-thought-text" not in result["reply"]
    assert "no_op" in result["reply"]


@pytest.mark.asyncio
async def test_run_agent_parses_final_response_when_thought_part_present():
    """The primary parse-breaking landmine is `run_agent` / `/recheck`,
    not `run_chat`. If `include_thoughts=True` ships without the
    final-text thought-skip in `run_agent`, the thought summary gets
    concatenated into the JSON blob fed to `_parse_response` and the
    parse raises, breaking the entire `/recheck` flow. This test pins
    that path: stub a final event with both a `thought=True` part and
    a valid decision-JSON part, and assert `run_agent` returns a parsed
    `DecisionProposal` instead of raising.
    """
    with patch.object(adk_agent, "Runner") as runner_cls:
        runner_cls.return_value.run_async = _stub_run
        proposal = await adk_agent.run_agent("hi", workload="drift")
    # Don't pin the exact class — just confirm parsing didn't blow up
    # and the rationale survived. The strong invariant is "no exception".
    assert proposal is not None
    assert getattr(proposal, "action", None) is not None
```

**Step 3: Run the test to verify it fails**

Run: `pytest tests/unit/test_adk_agent_thinking.py -v`
Expected: All 4 tests fail (no planner, no thought-skip).

**Step 4: Implement both halves in one commit**

In `agent/adk_agent.py`, add imports near the top:

```python
from google.adk.planners.built_in_planner import BuiltInPlanner
from google.genai.types import ThinkingConfig
```

Update `build_agent`'s `Agent(...)` call:

```python
return Agent(
    name=f"driftscribe_{workload.spec.name}",
    model="gemini-2.5-flash",
    instruction=workload.system_prompt,
    tools=list(workload.tools.values()),
    # 18.B.1: surface Gemini 2.5 Flash's thought summaries. The model
    # already spends thinking tokens at default-dynamic budget; this
    # only changes whether the summaries are *returned*.
    planner=BuiltInPlanner(
        thinking_config=ThinkingConfig(include_thoughts=True),
    ),
)
```

Apply the identical `planner=` change to `build_chat_agent`'s `Agent(...)` call.

Then fix the final-response branch in **both** `run_chat` and `run_agent` event loops. In `run_chat`, replace the final-text collection at ~lines 420-423:

```python
        if event.is_final_response() and event.content and event.content.parts:
            for part in event.content.parts:
                # 18.B.1: skip thought parts. With include_thoughts=True the
                # final event interleaves a thought-summary part alongside
                # the response JSON; collecting both corrupts the parse.
                if getattr(part, "thought", False):
                    continue
                if getattr(part, "text", None):
                    reply_chunks.append(part.text)
```

Apply the identical thought-skip to `run_agent`'s final-text collection (whichever path concatenates parts into the JSON blob fed to `_parse_response`).

**Step 5: Run the test to verify it passes**

Run: `pytest tests/unit/test_adk_agent_thinking.py -v`
Expected: 4 PASS.

**Step 6: Run the broader agent suite**

Run: `pytest tests/unit/test_adk_agent*.py tests/unit/test_workloads*.py -q`
Expected: All previously-passing tests still pass.

**Step 7: Commit (single atomic commit, both halves)**

```bash
git add agent/adk_agent.py tests/unit/test_adk_agent_thinking.py
git commit -m "feat(agent): enable Gemini thought summaries + skip thought parts in final text (18.B.1)"
```

---

### Task 18.B.2: Emit `llm_thought` + `tool_call` structured log lines (with partial-event dedup)

**Files:**
- Modify: `agent/adk_agent.py` — both `run_chat` and `run_agent` event loops
- Test: `tests/unit/test_adk_agent_event_logging.py` (new)

**Step 1: Confirm the partial-event field exists on ADK Event**

Run: `grep -n 'partial' /home/adi/driftscribe/.venv/lib/python3.14/site-packages/google/adk/events/event.py | head`
Expected: At least one hit on `self.partial` (it's the field `is_final_response()` already inspects). Without this field the dedup strategy below is wrong.

**Step 2: Write the failing test**

Create `tests/unit/test_adk_agent_event_logging.py`:

```python
"""Pin the structured log shape for thought / tool-call events, including
the partial-event dedup. ADK streams thoughts as a sequence of partial
events and then re-emits them merged in a non-partial event — naive
per-event logging would multiply each thought summary.

Field schema (consumed by Logs Explorer queries documented in the
deploy runbook):

  event=llm_thought   trace_id=<hex32>  workload=<name>  thought_text=<text>
  event=tool_call     trace_id=<hex32>  workload=<name>  tool_name=<name>
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import adk_agent


class _P:
    def __init__(self, *, text=None, function_call=None, thought=False):
        self.text = text
        self.function_call = function_call
        self.thought = thought


class _Ev:
    def __init__(self, parts, *, partial=False, final=False):
        self.content = SimpleNamespace(parts=parts)
        self.partial = partial
        self._final = final
        self.usage_metadata = None

    def is_final_response(self):
        return self._final


async def _stub_run(*args, **kwargs):
    # Two partial thought chunks — should NOT be logged.
    yield _Ev(
        [_P(text="checking ", thought=True)],
        partial=True,
    )
    yield _Ev(
        [_P(text="...contract", thought=True)],
        partial=True,
    )
    # Merged non-partial thought — SHOULD be logged (once).
    yield _Ev(
        [_P(text="checking ...contract", thought=True)],
        partial=False,
    )
    # Tool call — SHOULD be logged (function_calls never come as partials,
    # but we apply the same guard for uniformity).
    yield _Ev(
        [_P(function_call=SimpleNamespace(name="read_drift"))],
        partial=False,
    )
    # Final response.
    yield _Ev(
        [_P(text='{"action":"no_op"}')],
        partial=False,
        final=True,
    )


@pytest.mark.asyncio
async def test_run_chat_dedups_partial_thoughts(caplog):
    caplog.set_level(logging.INFO, logger="driftscribe.agent.adk_agent")
    with patch.object(adk_agent, "Runner") as runner_cls:
        runner_cls.return_value.run_async = _stub_run
        result = await adk_agent.run_chat("hi", workload="drift")

    thoughts = [
        r for r in caplog.records
        if getattr(r, "event", None) == "llm_thought"
    ]
    assert len(thoughts) == 1, f"expected 1 thought log, got {len(thoughts)}"
    assert getattr(thoughts[0], "thought_text", "") == "checking ...contract"
    assert getattr(thoughts[0], "workload", None) == "drift"

    tool_calls = [
        r for r in caplog.records
        if getattr(r, "event", None) == "tool_call"
    ]
    assert len(tool_calls) == 1
    assert getattr(tool_calls[0], "tool_name", None) == "read_drift"

    # /chat response body still includes tool_calls for back-compat with
    # the operator UI (this is a public contract from Phase 11.7).
    assert result["tool_calls"] == ["read_drift"]
```

**Step 3: Run the test to verify it fails**

Run: `pytest tests/unit/test_adk_agent_event_logging.py -v`
Expected: FAIL — no `llm_thought` or `tool_call` log records exist yet.

**Step 4: Implement the structured logging**

In `agent/adk_agent.py`, add near the existing imports (reuse if already present elsewhere — do NOT duplicate):

```python
import logging
from driftscribe_lib.logging import current_trace_id_or_new
from agent.workload_context import current_workload

_log = logging.getLogger("driftscribe.agent.adk_agent")
```

Replace the parts-iteration in `run_chat`'s event loop (currently lines ~414-418) with:

```python
        if event.content and event.content.parts and getattr(event, "partial", None) is not True:
            for part in event.content.parts:
                # 18.B.2: dedup — only the non-partial (merged) emission
                # carries the complete thought summary or function call.
                if getattr(part, "thought", False) and getattr(part, "text", None):
                    _log.info(
                        "llm_thought",
                        extra={
                            "event": "llm_thought",
                            "trace_id": current_trace_id_or_new(),
                            "workload": current_workload(),
                            "thought_text": part.text,
                        },
                    )
                    continue
                fc = getattr(part, "function_call", None)
                if fc and getattr(fc, "name", None):
                    tool_calls.append(fc.name)
                    _log.info(
                        "tool_call",
                        extra={
                            "event": "tool_call",
                            "trace_id": current_trace_id_or_new(),
                            "workload": current_workload(),
                            "tool_name": fc.name,
                        },
                    )
```

Apply the **identical** block to `run_agent`'s event loop, minus the `tool_calls` list bookkeeping (that list is `run_chat`-only).

**Step 5: Run the focused test**

Run: `pytest tests/unit/test_adk_agent_event_logging.py -v`
Expected: PASS.

**Step 6: Run the full unit suite**

Run: `pytest tests/unit -q`
Expected: All previously-passing tests still pass; new tests added.

**Step 7: Commit**

```bash
git add agent/adk_agent.py tests/unit/test_adk_agent_event_logging.py
git commit -m "feat(agent): log llm_thought + tool_call events with partial-event dedup (18.B.2)"
```

---

### Task 18.B.3: Emit `llm_usage` log lines with `thoughts_token_count`

**Files:**
- Modify: `agent/adk_agent.py` — same event loops in `run_chat` and `run_agent`
- Test: `tests/unit/test_adk_agent_usage_logging.py` (new)

**Step 1: Confirm the usage-metadata field exists**

Run: `grep -n 'thoughts_token_count\|usage_metadata' /home/adi/driftscribe/.venv/lib/python3.14/site-packages/google/adk/events/event.py /home/adi/driftscribe/.venv/lib/python3.14/site-packages/google/genai/types.py | head`
Expected: At least one hit on `usage_metadata` on the ADK Event class and one hit on `thoughts_token_count` in `genai.types`. Without `thoughts_token_count` the cost-measurement story collapses; bail out and reopen this task.

**Step 2: Write the failing test**

Create `tests/unit/test_adk_agent_usage_logging.py`:

```python
"""Pin the llm_usage log line shape. One record per LLM event that
carries usage metadata. Required fields: prompt_token_count,
candidates_token_count, thoughts_token_count, total_token_count.
thoughts_token_count is the whole point — it's the only way to prove
post-deploy that include_thoughts=True did or did not move the cost
needle relative to the pre-Phase-18 baseline.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import adk_agent


class _P:
    def __init__(self, *, text=None, thought=False):
        self.text = text
        self.thought = thought
        self.function_call = None


class _Ev:
    def __init__(self, parts, *, usage=None, partial=False, final=False):
        self.content = SimpleNamespace(parts=parts)
        self.partial = partial
        self._final = final
        self.usage_metadata = usage

    def is_final_response(self):
        return self._final


def _usage(prompt=120, candidates=80, thoughts=64, total=264):
    return SimpleNamespace(
        prompt_token_count=prompt,
        candidates_token_count=candidates,
        thoughts_token_count=thoughts,
        total_token_count=total,
    )


async def _stub_run(*args, **kwargs):
    yield _Ev([_P(text="reasoning", thought=True)], partial=False)
    yield _Ev(
        [_P(text='{"action":"no_op"}')],
        usage=_usage(),
        partial=False,
        final=True,
    )


@pytest.mark.asyncio
async def test_run_chat_emits_llm_usage_log(caplog):
    caplog.set_level(logging.INFO, logger="driftscribe.agent.adk_agent")
    with patch.object(adk_agent, "Runner") as runner_cls:
        runner_cls.return_value.run_async = _stub_run
        await adk_agent.run_chat("hi", workload="drift")

    usage = [
        r for r in caplog.records
        if getattr(r, "event", None) == "llm_usage"
    ]
    assert len(usage) >= 1
    r = usage[-1]
    assert getattr(r, "prompt_token_count") == 120
    assert getattr(r, "candidates_token_count") == 80
    assert getattr(r, "thoughts_token_count") == 64
    assert getattr(r, "total_token_count") == 264
    assert getattr(r, "workload") == "drift"
```

**Step 3: Run the test to verify it fails**

Run: `pytest tests/unit/test_adk_agent_usage_logging.py -v`
Expected: FAIL — no `llm_usage` log records exist.

**Step 4: Implement usage logging in both event loops**

At the **bottom** of each event-loop iteration in `run_chat` and `run_agent` (right after the parts-iteration block from 18.B.2, still inside the `async for event in ...:` loop), add:

```python
        usage = getattr(event, "usage_metadata", None)
        if usage is not None:
            # 18.B.3: emit one log line per LLM call's usage payload so
            # post-deploy dashboards can graph thoughts_token_count vs
            # the pre-Phase-18 baseline. Each Gemini call typically
            # surfaces usage_metadata on its final (non-partial) event.
            _log.info(
                "llm_usage",
                extra={
                    "event": "llm_usage",
                    "trace_id": current_trace_id_or_new(),
                    "workload": current_workload(),
                    "prompt_token_count": getattr(usage, "prompt_token_count", None),
                    "candidates_token_count": getattr(usage, "candidates_token_count", None),
                    "thoughts_token_count": getattr(usage, "thoughts_token_count", None),
                    "total_token_count": getattr(usage, "total_token_count", None),
                },
            )
```

**Step 5: Run the focused test**

Run: `pytest tests/unit/test_adk_agent_usage_logging.py -v`
Expected: PASS.

**Step 6: Run the full unit suite**

Run: `pytest tests/unit -q`
Expected: Green.

**Step 7: Commit**

```bash
git add agent/adk_agent.py tests/unit/test_adk_agent_usage_logging.py
git commit -m "feat(agent): log llm_usage with thoughts_token_count for cost tracking (18.B.3)"
```

---

### Task 18.B.4: Integration smoke — trace_id flows from middleware to thought log

**Files:**
- Test: `tests/integration/test_chat_emits_thought_logs.py` (new)

**Step 1: Copy the canonical /chat test pattern from `test_chat_endpoint.py`**

The integration suite already has a working `/chat`-against-stubbed-Runner pattern. Do NOT invent a parallel fixture — Codex flagged this specifically and it is the single most common way new tests in this repo break.

The pattern, copied verbatim from `tests/integration/test_chat_endpoint.py:47-77`:

1. The autouse fixture in `tests/integration/conftest.py:14-67` already bypasses `verify_token` via `app.dependency_overrides`. No `X-DriftScribe-Token` header is needed. No `auth_headers` fixture exists — do not invent one.
2. The autouse fixture sets `USE_ADK=false` for every test. To exercise the `/chat → run_chat` path you MUST opt in:

```python
monkeypatch.setenv("USE_ADK", "true")
get_settings.cache_clear()  # without this, the cached Settings still says false
```

3. To exercise the **real** `run_chat` event loop (which is what emits the new log lines), patch at `agent.adk_agent.Runner` — NOT at `agent.adk_agent.run_chat` (that's what the existing `test_chat_endpoint.py` does, and it bypasses the very loop we need to exercise).

Run: `grep -n 'USE_ADK\|cache_clear\|Runner' tests/integration/test_chat_endpoint.py | head -10`
Expected: Confirms the `USE_ADK` + `cache_clear` pattern. Note that existing tests patch `run_chat` itself; OUR test patches `Runner` instead — that's intentional.

**Step 2: Write the integration test**

Create `tests/integration/test_chat_emits_thought_logs.py`:

```python
"""Smoke-test the seam between the FastAPI middleware (which binds
X-Trace-Id to the ContextVar) and the agent event loop (which reads
the same ContextVar through current_trace_id_or_new). If this test
passes, every log line written during a /chat invocation will carry
the request's trace_id — which is what makes the 365-day Logs
Explorer replay work.

Fixture/auth pattern is copied from tests/integration/test_chat_endpoint.py:
- The autouse fixture in tests/integration/conftest.py bypasses
  verify_token via app.dependency_overrides; no header is needed.
- The autouse fixture sets USE_ADK=false. We opt in via monkeypatch
  + get_settings.cache_clear() (cached Settings would otherwise still
  say false and the endpoint would 503 before reaching the Runner).
- We patch agent.adk_agent.Runner (not run_chat) so the REAL event
  loop runs and emits the new log lines.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent import adk_agent
from agent.config import get_settings
from agent.main import app


class _P:
    def __init__(self, *, text=None, function_call=None, thought=False):
        self.text = text
        self.function_call = function_call
        self.thought = thought


class _Ev:
    def __init__(self, parts, *, partial=False, final=False):
        self.content = SimpleNamespace(parts=parts)
        self.partial = partial
        self._final = final
        self.usage_metadata = None

    def is_final_response(self):
        return self._final


async def _stub_run(*args, **kwargs):
    yield _Ev(
        [_P(text="thinking about contract", thought=True)],
        partial=False,
    )
    yield _Ev(
        [_P(function_call=SimpleNamespace(name="read_drift"))],
        partial=False,
    )
    yield _Ev(
        [_P(text='{"action":"no_op"}')],
        partial=False,
        final=True,
    )


def test_chat_thought_log_carries_request_trace_id(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="driftscribe.agent.adk_agent")
    # Opt in to the ADK path — autouse fixture pins USE_ADK=false otherwise.
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()

    fixed_trace = "0" * 32

    with patch.object(adk_agent, "Runner") as runner_cls:
        runner_cls.return_value.run_async = _stub_run
        client = TestClient(app)
        resp = client.post(
            "/chat",
            headers={"X-Trace-Id": fixed_trace},
            json={"prompt": "what is the current drift?", "workload": "drift"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("X-Trace-Id") == fixed_trace

    thoughts = [
        r for r in caplog.records
        if getattr(r, "event", None) == "llm_thought"
    ]
    assert thoughts, "expected at least one llm_thought log line"
    assert getattr(thoughts[0], "trace_id") == fixed_trace
    assert getattr(thoughts[0], "workload") == "drift"
```

**Step 3: Run the test**

Run: `pytest tests/integration/test_chat_emits_thought_logs.py -v`
Expected: PASS. If it FAILS with 503, the `USE_ADK=true` + `get_settings.cache_clear()` pair is missing — re-read Step 1.

**Step 4: Commit**

```bash
git add tests/integration/test_chat_emits_thought_logs.py
git commit -m "test(integration): pin trace_id flow into llm_thought log lines (18.B.4)"
```

---

### Task 18.B.5: Update multi-agent design doc

**Files:**
- Modify: `docs/architecture/multi-agent-design.md`

**Step 1: Find the existing logging section in the doc**

Run: `grep -n '##\|### ' docs/architecture/multi-agent-design.md | head -40`
Expected: A section that mentions logging or observability. The new subsection slots in after it (or after the §6 MCP grounding section if logging isn't separately called out).

**Step 2: Add the reasoning-observability subsection**

```markdown
### Reasoning observability (Phase 18.B)

Every `/chat` and `/recheck` invocation emits four structured JSON
log-line shapes, all keyed by `trace_id` (bound by the request
middleware) and `workload` (bound by the request handler before the
agent runs):

| event           | additional fields                                        |
| --------------- | -------------------------------------------------------- |
| `llm_thought`   | `thought_text` — Gemini's own summary of its reasoning   |
| `tool_call`     | `tool_name`                                              |
| `llm_usage`     | `prompt_token_count`, `candidates_token_count`, `thoughts_token_count`, `total_token_count` |
| `mcp_call`      | (pre-existing, Phase 17.B.4) `mcp_tool`, `query_or_names`, `doc_count`, `latency_ms`, `error?` |

Thought summaries come from Gemini 2.5 Flash's built-in thinking,
surfaced via ADK's `BuiltInPlanner(ThinkingConfig(include_thoughts=True))`.
The model already spent thinking tokens at the SDK-default dynamic
budget before Phase 18 — `include_thoughts=True` only changes whether
the summaries are returned. `thoughts_token_count` on each `llm_usage`
line is what lets the operator confirm cost behaviour empirically
rather than from documentation.

Streaming dedup: ADK emits partial events as a thought summary is
generated, then re-emits the merged summary as a non-partial event.
`agent/adk_agent.py` filters on `event.partial` so a single thought
summary maps to a single log line.

Retention: all of the above ride Cloud Logging's `_Default` bucket,
extended to 365 days by `infra/scripts/setup_secrets.sh` (Phase 18.A).
Logs Explorer queries can replay a full agent trace by filtering on
`jsonPayload.trace_id`.
```

**Step 3: Commit**

```bash
git add docs/architecture/multi-agent-design.md
git commit -m "docs(arch): document reasoning observability (18.B.5)"
```

---

### Task 18.B.6: Final lint + full pytest + Codex follow-up review

**Step 1: Lint**

Run: `ruff check agent/ tests/ infra/`
Expected: Clean. (Codex review of the draft flagged unused imports in earlier test sketches — confirm none crept into the implemented files.)

**Step 2: Full pytest**

Run: `pytest -q`
Expected: 728 (pre-Phase-18 baseline) + new tests, all green.

**Step 3: Local sanity check — emit and inspect the three new log shapes**

Run:
```bash
python3 - <<'PY'
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import patch

from driftscribe_lib.logging import setup as setup_logging
setup_logging("driftscribe-agent")  # write JSON to stderr
logging.getLogger("driftscribe.agent.adk_agent").setLevel(logging.INFO)

from agent import adk_agent

class P:
    def __init__(self, **kw):
        self.text = kw.get("text")
        self.thought = kw.get("thought", False)
        self.function_call = kw.get("function_call")

class Ev:
    def __init__(self, parts, *, usage=None, partial=False, final=False):
        self.content = SimpleNamespace(parts=parts)
        self.partial = partial
        self._fin = final
        self.usage_metadata = usage
    def is_final_response(self):
        return self._fin

async def fake_run(*a, **kw):
    yield Ev([P(text="examining the ops contract", thought=True)], partial=False)
    yield Ev([P(function_call=SimpleNamespace(name="read_drift"))], partial=False)
    yield Ev(
        [P(text='{"action":"no_op"}')],
        usage=SimpleNamespace(
            prompt_token_count=110, candidates_token_count=42,
            thoughts_token_count=58, total_token_count=210,
        ),
        partial=False, final=True,
    )

with patch.object(adk_agent, "Runner") as rc:
    rc.return_value.run_async = fake_run
    out = asyncio.run(adk_agent.run_chat("hi", workload="drift"))
print(">>> reply:", out["reply"])
PY
```

Expected: Three JSON log lines on stderr — one each for `llm_thought`, `tool_call`, `llm_usage`. Each carries `trace_id` (freshly minted because no middleware ran), `workload=drift`. The reply printed at the end is the JSON action blob, with no thought text mixed in.

**Step 4: Codex follow-up review** (per the user's global CLAUDE.md instruction — this is standard process for every DriftScribe phase, not optional)

Reopen the Codex MCP thread used during plan review and ask Codex to read every commit in the Phase 18 series against this plan. Use `mcp__codex__codex-reply` (no `model` parameter — let Codex pick its current recommended model). Address all findings before merging.

**Step 5: Commit any final fixes from the Codex review**

```bash
git add -A
git commit -m "chore: phase 18 wrap (18.B.6)"
```

---

## Out of scope (explicit non-goals)

- **BigQuery / GCS log sinks.** User confirmed `_Default` retention extension is sufficient. SQL replay is not a Phase 18 requirement; Logs Explorer covers the query surface at hackathon volume. Revisit only if monthly log volume blows past 10 GiB or someone needs to JOIN log records with external data.
- **Backfilling pre-Phase-18 logs.** Whatever has already aged out is gone. There is no recovery path.
- **Capping `thinking_budget`.** Let it ride at the SDK default. `llm_usage` records will tell us empirically whether to cap; premature capping risks truncating real reasoning for borderline cases.
- **Raw chain-of-thought.** Gemini exposes only thought summaries. No "more verbose" knob exists.
- **Real-time SSE streaming of thoughts to the operator.** Out of scope; would reshape `/chat` from `JSONResponse` to `StreamingResponse`. Phase 19 territory if ever needed.
- **PII scanning of thought summaries.** Input space is Cloud Run env vars + lockfile contents + ops contracts — no end-user PII enters the agent. No DLP needed.

---

## Sanity-check checklist before merge

- [ ] `setup_secrets.sh` is still idempotent: re-running on a project that already has 365-day retention is a no-op and does not error.
- [ ] `gcloud logging buckets describe _Default --location=global` shows `retentionDays: 365` on the deployed project.
- [ ] Both `build_agent` and `build_chat_agent` pass `planner=BuiltInPlanner(ThinkingConfig(include_thoughts=True))`.
- [ ] **Atomicity check:** the planner-enable and the final-text-skip-thoughts fix share a single git commit. If `git log -p agent/adk_agent.py` shows them in separate commits, redo 18.B.1.
- [ ] Local sanity-check from 18.B.6 Step 3 prints exactly three new log lines: one `llm_thought`, one `tool_call`, one `llm_usage`.
- [ ] No new IAM grants. No new secrets. No new Cloud Run env vars. No new service accounts.
- [ ] `pytest -q` is fully green.
- [ ] `ruff check` is clean.
- [ ] Codex follow-up review (18.B.6 Step 4) has been completed and all findings either fixed or explicitly deferred with a comment.
