"""Byte-for-byte golden tests for the drift workload (Phase 17.A.2 /
17.B.3).

Pins:

1. ``workloads/drift/system_prompt.md`` equals the Phase 17.B.3 golden
   below. The literal in this test (NOT imported from production code)
   is the audit point: production code is allowed to evolve, the
   literal evolves only by an intentional human edit that re-justifies
   prompt content. Phase 17.B.3 added an MCP-citation paragraph (the
   ``search_developer_docs`` call instruction for ``docs_pr``); the
   golden was bumped at the same time.

2. ``workloads/drift/contract.yaml`` equals ``demo/ops-contract.yaml``
   byte-for-byte. The 17.A.2 move was a copy (not a symlink) for
   Windows/portability reasons and because several deploy/demo paths
   already consume ``demo/ops-contract.yaml`` directly (cloudbuild,
   docker build, the checker CLI, the demo shell script). This test
   stops the two copies from silently drifting if someone edits one
   and forgets the other.

3. ``WorkloadResolution`` for drift exposes the prompt and contract
   path with the expected shape (string prompt; absolute path to the
   contract). Pins the contract is reachable and parseable.

The prompt golden is intentionally a long string literal — copy-paste
auditability beats DRY here. If the prompt changes for a real reason
(e.g. 17.B.3 adds an MCP step), update the literal and the
``workloads/drift/system_prompt.md`` together; the diff in code review
is exactly the prompt edit.
"""
from __future__ import annotations

from pathlib import Path

import yaml


# The drift system prompt as it currently lives in
# ``workloads/drift/system_prompt.md``. Byte-equal pin — any change
# here must be intentional and reviewed. Phase 17.B.3 added the
# ``search_developer_docs`` citation paragraph (between the output
# schema block and the Rules block) so the LLM grounds docs PR
# wording in authoritative Cloud Run env-variable guidance.
_DRIFT_SYSTEM_PROMPT_GOLDEN = """\
You are DriftScribe, an AI DevOps agent that detects and triages drift between
a deployed Cloud Run service's live configuration and the team's declared
operational contract (ops-contract.yaml).

You cannot mutate any system directly. You can ONLY call worker tools. Each
worker has its own scoped IAM and payload-intent policy.

For each invocation, you must:
1. Call `load_contract_tool()` to read the baked-in contract.
2. Call `read_live_env_tool()` to read the live Cloud Run env + revision.
3. For variables that differ from the contract, call `search_recent_prs_tool`
   with the var names as keywords.
4. Emit a single JSON DecisionProposal — and ONLY that JSON, no prose around it.

Output schema (JSON, no other text):

{
  "action": "docs_pr" | "drift_issue" | "escalation" | "no_op" | "rollback",
  "env_diffs": [
    {
      "name": "STRING",
      "expected": "STRING_OR_NULL",
      "live": "STRING_OR_NULL",
      "contract_status": "absent" | "present_allow_manual" | "present_disallow_manual" | "match",
      "debug_config_value": "STRING_OR_NULL",
      "recent_pr_match": "STRING_OR_NULL"
    }
  ],
  "target_docs_file": "STRING_OR_NULL",
  "target_docs_section": "STRING_OR_NULL",
  "target_revision": "STRING_OR_NULL",
  "rationale": "STRING",
  "confidence": 0.0_to_1.0,
  "requires_human_review": true_or_false
}

When proposing a `docs_pr`, first call `search_developer_docs` to find
authoritative Cloud Run env-variable guidance for the var(s) being
documented. Cite the resulting document URL in the PR body's rationale
so the reviewer can audit which canonical guidance the proposed wording
references. If the search returns ``{"error": ...}`` or no relevant
matches, proceed with the docs PR but note the absence of an
authoritative citation in the rationale rather than inventing a URL.

Rules:
- If you cannot reach a tool, say so in `rationale`; do NOT invent values.
- If any tool returns an object containing the key `_error`, treat it as a
  failure result — the value is a diagnostic string. Do NOT interpret
  `_error` as a config field, an env var name, or contract data.
- Never propose `docs_pr` for a var whose contract entry says `allow_manual_change: false`.
- Never propose `docs_pr` for a var name containing SECRET, TOKEN, KEY, PASSWORD, CRED, PRIVATE.
- For an absent (not-in-contract) var, only propose `docs_pr` if a recent merged PR
  mentions the EXACT var name (word boundary, case-sensitive). Otherwise `escalation`.
- Propose `rollback` when a variable with contract_status == "present_disallow_manual"
  has drifted to an unsanctioned value AND a previous Cloud Run revision exists
  whose env was contract-compliant. Set `target_revision` to that previous
  revision's name (e.g., "payment-demo-00041-xyz"), set `requires_human_review: true`,
  and do NOT set `target_docs_file` / `target_docs_section`. Do NOT infer or
  fabricate a revision name — only propose rollback when a concrete previous
  revision name has come back from a tool call. If you cannot identify one,
  emit `drift_issue` instead (operators can roll back manually).
- (Phase 13 limitation: Reader Worker currently returns only the active revision,
  not a previous-revision list. Until a future phase extends it, the LLM may
  need to refuse rollback proposals where it cannot identify a previous
  revision — fall back to `drift_issue` in that case.)

The /recheck path only emits a DecisionProposal — do NOT call
propose_rollback_tool, patch_docs_tool, or notify_tool on this path. Those
tools are reserved for the /chat path where the operator may explicitly
request a rollback, docs PR, or notification. The orchestrator routes a
`rollback` decision through the Rollback Worker on your behalf; the LLM
only outputs the JSON decision and never mints approval tokens directly.
"""


_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_drift_system_prompt_file_matches_pre17_constant():
    """Byte-for-byte golden: ``workloads/drift/system_prompt.md`` equals
    :data:`_DRIFT_SYSTEM_PROMPT_GOLDEN` above.

    Test name is kept as ``..._pre17_constant`` for git-blame
    continuity even though the literal has been bumped through 17.B.3
    (the ``search_developer_docs`` citation paragraph). The function
    still pins what it always has: a byte-equal contract between the
    file on disk and the test's literal.

    Intentional edits must change BOTH the file and the literal in this
    test — the diff in PR review is exactly the prompt edit, with no
    way for a refactor to silently change LLM behavior.
    """
    file_text = (_REPO_ROOT / "workloads" / "drift" / "system_prompt.md").read_text(
        encoding="utf-8"
    )
    assert file_text == _DRIFT_SYSTEM_PROMPT_GOLDEN, (
        "Drift system prompt diverged from the test's golden literal. "
        "If this is intentional (e.g. a prompt evolution for a later "
        "phase), update the golden literal in this test alongside the "
        "file change."
    )


def test_drift_contract_yaml_matches_demo_copy():
    """Byte-for-byte: workloads/drift/contract.yaml equals
    demo/ops-contract.yaml.

    The 17.A.2 move was a copy (not a symlink) because several
    deploy/demo paths consume ``demo/ops-contract.yaml`` directly
    (cloudbuild, Dockerfile.agent, checker CLI, demo/scripts). This
    test pins both copies byte-equal so they cannot silently drift.
    Future work may consolidate to a single file once the consumers
    are migrated, at which point this test becomes obsolete.
    """
    workload_copy = (_REPO_ROOT / "workloads" / "drift" / "contract.yaml").read_bytes()
    demo_copy = (_REPO_ROOT / "demo" / "ops-contract.yaml").read_bytes()
    assert workload_copy == demo_copy, (
        "workloads/drift/contract.yaml has diverged from "
        "demo/ops-contract.yaml. Pick one as canonical and reconcile — "
        "the deploy infrastructure currently reads the demo/ copy while "
        "the workload registry reads workloads/drift/contract.yaml; a "
        "silent drift here means the LLM and the deploy see different "
        "contracts."
    )


def test_drift_recheck_uses_pre17_compatible_contract():
    """The workload-local contract parses to the same dict as the
    legacy ``demo/ops-contract.yaml`` copy.

    Why both this and ``test_drift_contract_yaml_matches_demo_copy``
    above: ``CONTRACT_PATH`` still points at the legacy
    ``demo/ops-contract.yaml`` (integration conftest sets that), so the
    coordinator's settings layer reads the demo copy. The workload
    registry reads the new ``workloads/drift/contract.yaml`` copy. Both
    must yield the same parsed dict — if they ever drift, the LLM and
    the classifier would see different ground truths.

    Reads via ``yaml.safe_load`` so this catches semantic drift (a
    rewritten-but-equivalent YAML wouldn't be byte-equal but should
    still parse to the same dict). The byte-equal guard lives in
    ``test_drift_contract_yaml_matches_demo_copy`` above; this test is
    the parse-equivalence companion. Pure file I/O — no FastAPI
    plumbing — so it lives in the unit suite, not integration.
    """
    demo_parsed = yaml.safe_load(
        (_REPO_ROOT / "demo" / "ops-contract.yaml").read_text(encoding="utf-8")
    )
    workload_parsed = yaml.safe_load(
        (_REPO_ROOT / "workloads" / "drift" / "contract.yaml").read_text(encoding="utf-8")
    )
    assert demo_parsed == workload_parsed, (
        "demo/ops-contract.yaml and workloads/drift/contract.yaml "
        "parsed to different dicts. Reconcile before the next deploy — "
        "the coordinator's settings layer reads the demo copy while the "
        "workload registry reads the workload-local copy."
    )


def test_load_workload_drift_exposes_prompt_byte_for_byte(drift_workload_env):
    """End-to-end: ``load_workload('drift').system_prompt`` returns the
    golden text byte-for-byte.

    Pins the resolver layer (workload YAML → file path → file contents),
    not just the file itself. A bug in :func:`_load_from_path` that
    silently swallowed a trailing newline (for example) would slip past
    the file-only assertion above but fail here.
    """
    from agent.workloads import load_workload

    resolution = load_workload("drift")
    assert resolution.system_prompt == _DRIFT_SYSTEM_PROMPT_GOLDEN


# The drift /chat system prompt as it currently lives in
# ``workloads/drift/chat_system_prompt.md``. Byte-equal pin — any change
# here must be intentional and reviewed. Phase 17.C.4 moved this content
# verbatim out of the ``SYSTEM_PROMPT_CHAT`` constant in
# ``agent/adk_agent.py``; the test pins the move was byte-faithful so
# the LLM's behavior on the /chat surface didn't change as a side
# effect of the refactor.
_DRIFT_CHAT_SYSTEM_PROMPT_GOLDEN = """\
You are DriftScribe's coordinator agent. Your job is to help an on-call
operator detect, triage, and respond to drift between a Cloud Run service's
live state and its declared operations contract.

CRITICAL constraint: You cannot mutate any system directly. You can ONLY
call worker tools. Each tool is delegated to a separate worker service with
its own scoped IAM and payload-intent policy. You are deliberately built
without direct GCP or GitHub mutation access.

Tools available to you:
- read_live_env_tool() — ask the Reader Agent for the live env + revision
- propose_rollback_tool(target_revision, reason) — ask Rollback Agent to
  create an approval. Rollbacks REQUIRE human approval; you do NOT execute
  them. Return the approval URL to the operator and explain that they must
  click it and press Approve.
- patch_docs_tool(file_path, new_content, title, body) — ask Docs Agent to
  open a docs PR. Path must be under demo/docs/*.md.
- notify_tool(channel, severity, body) — ask Notifier Agent to post a
  webhook. Channel: info|alert|approval. Severity: low|medium|high|critical.
- search_recent_prs_tool(keywords, days=7) — read-only PR history
- load_contract_tool() — read the baked-in ops contract
- search_developer_docs(query) — search Google's Developer Knowledge
  corpus (Cloud Run, GitHub Actions, etc.) for authoritative product
  documentation. Returns up to 5 doc refs with parent/content/id.
- retrieve_developer_doc(name) — fetch the full body of a single doc
  by name (use the `parent` field from a search result as `name`).

Rules:
- If asked to do something destructive (rollback, redeploy, delete), use
  propose_rollback_tool and explain that human approval is required.
  NEVER attempt to bypass the approval gate.
- When proposing a docs PR (via patch_docs_tool), first call
  search_developer_docs to find authoritative Cloud Run env-variable
  guidance for the var(s) being documented; cite the resulting document
  URL in the PR body so the reviewer can audit which canonical guidance
  the proposed wording references. If the search returns an `error` key
  or no relevant matches, proceed but note the absence of an
  authoritative citation rather than inventing a URL.
- If a tool returns an error, surface it to the operator clearly. Do NOT
  pretend the action succeeded.
- Be concise. The operator is on-call and wants the answer, not prose.
"""


def test_drift_chat_system_prompt_file_matches_pre17c4_constant():
    """Phase 17.C.4 byte-for-byte golden: the moved
    ``workloads/drift/chat_system_prompt.md`` equals the previously
    coordinator-wide ``SYSTEM_PROMPT_CHAT`` constant byte-for-byte.

    The move (from ``agent/adk_agent.py::SYSTEM_PROMPT_CHAT`` to a
    per-workload file via the ``chat_system_prompt_file`` field on
    :class:`~agent.workloads.WorkloadSpec`) must NOT change the
    LLM's /chat behavior. This test pins the byte-equal property so a
    future PR that "tidies" the file by, say, normalizing trailing
    whitespace can't silently shift agent behavior on the drift /chat
    surface.

    Intentional edits must change BOTH the file and the literal in this
    test — the diff in PR review is exactly the prompt edit.
    """
    file_text = (
        _REPO_ROOT / "workloads" / "drift" / "chat_system_prompt.md"
    ).read_text(encoding="utf-8")
    assert file_text == _DRIFT_CHAT_SYSTEM_PROMPT_GOLDEN, (
        "Drift chat system prompt diverged from the test's golden literal. "
        "If this is intentional (e.g. a prompt evolution), update the "
        "golden literal in this test alongside the file change."
    )


def test_load_workload_drift_exposes_chat_prompt_byte_for_byte(
    drift_workload_env,
):
    """Phase 17.C.4: ``load_workload('drift').chat_system_prompt``
    returns the golden chat text byte-for-byte.

    Pins the resolver layer (workload YAML's ``chat_system_prompt_file``
    → file path → file contents → :class:`WorkloadResolution.chat_system_prompt`),
    not just the file itself. Distinct from
    :func:`test_load_workload_drift_exposes_prompt_byte_for_byte` above
    which pins the ``/recheck`` prompt — the two surfaces have
    historically diverged and the 17.C.4 schema split makes that
    divergence explicit.
    """
    from agent.workloads import load_workload

    resolution = load_workload("drift")
    assert resolution.chat_system_prompt == _DRIFT_CHAT_SYSTEM_PROMPT_GOLDEN


def test_load_workload_drift_exposes_contract_path(drift_workload_env):
    """``WorkloadResolution.contract_path`` resolves to the workload-
    local copy and the file parses as the expected ops-contract shape.

    Pins:
    - the path resolution (the YAML's ``contract_file: contract.yaml``
      becomes an absolute path under ``workloads/drift/``),
    - the file is reachable and yaml-parseable,
    - the parsed contract has the expected top-level keys (smoke check
      against a stale or empty file).
    """
    from agent.workloads import load_workload

    resolution = load_workload("drift")
    assert resolution.contract_path is not None
    assert resolution.contract_path.is_absolute()
    assert resolution.contract_path.name == "contract.yaml"
    assert resolution.contract_path.parent.name == "drift"

    parsed = yaml.safe_load(resolution.contract_path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    # Sanity-pin a couple of fields that should always be present.
    assert parsed.get("service") == "payment-demo"
    assert "expected_env" in parsed
    assert "PAYMENT_MODE" in parsed["expected_env"]


def test_drift_workload_contract_yaml_parses_into_ops_contract(drift_workload_env):
    """The workload-local contract.yaml parses cleanly through the
    existing :func:`agent.contract.load_contract` shape checker.

    This is the schema bridge: 17.A.2 moves the contract file but
    doesn't introduce a new parser; the existing ``OpsContract`` model
    must accept the file unchanged. If a future refactor reshapes the
    contract format, this test catches it before the LLM ever sees
    the new shape.
    """
    from agent.contract import load_contract
    from agent.workloads import load_workload

    resolution = load_workload("drift")
    assert resolution.contract_path is not None
    contract = load_contract(resolution.contract_path)
    assert contract.service == "payment-demo"
    assert "PAYMENT_MODE" in contract.expected_env
