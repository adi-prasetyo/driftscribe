# "Ask about this change" on the approval page (ClickOps item 12) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** From the strict-CSP IaC approval page, an operator can jump to the SPA with the chat prefilled to ask a read-only assistant about the pending plan ("what does `uniform_bucket_level_access` mean?") — backed by a new report-tier explore tool that reads the verified plan artifact directly from GCS.

**Architecture:** Three thin pieces. (1) A new coordinator-local tool `load_iac_plan` resolves the latest c2.v1 plan artifact for a PR **by listing GCS** (never via the GitHub C2 comment — that ride's the write-capable PAT, which is exactly why `search_recent_prs` is banned from explore; the coordinator SA holds only `roles/storage.objectViewer` on the artifacts bucket, live-confirmed), re-verifies it with the existing `iac_artifacts` machinery, and returns the bounded, sensitive-masked plain-language summary. (2) The approval page gains a plain `<a href="/?ask_pr={{ pr_number }}">` link (CSP-safe, mirrors the existing `/?preview_pr=` link). (3) `App.svelte` parses `?ask_pr=N` at boot and prefills the composer with an explore-workload prompt via the existing `ChatPrefill` bridge (adopt-button pattern), then strips the param.

**Tech Stack:** FastAPI coordinator (`agent/`), google-cloud-storage listing, Jinja2 template, Svelte 5 SPA, pytest + vitest.

**Roadmap text (item 12):** "A read-only `explore`-workload chat scoped to the plan artifact … Strict-CSP approval page → link out to the SPA with the PR context prefilled rather than embedding chat in the page." Size M; item 1 (plain-language summary) already shipped and is reused here verbatim (`summarize_plan` + `blast_radius_phrase` + `BLAST_CANNOT_TOUCH_NOTE`).

---

## Design decisions (the load-bearing ones)

1. **GCS-listing, not GitHub-comment, artifact resolution.** `find_latest_c2_comment` needs the coordinator's write-capable GitHub PAT. Explore's "strictly read-only" label is pinned as *tool-set disjoint from `MUTATION_TOOL_NAMES`*, and `search_recent_prs` sits in that set purely for credential containment (Codex 2026-05-25). The artifact triplet lives at `gs://{project}-tofu-artifacts/pr-{N}/{sha}/run-{id}-{attempt}/…` and the coordinator SA's only grant on that bucket is `objectViewer` (read+list — verified live 2026-06-11). So the tool lists `pr-{N}/` and picks the newest `metadata.json` by `(run_id, attempt, generation)`. **Consequence:** `load_iac_plan` does NOT join `MUTATION_TOOL_NAMES`; every explore pin holds unchanged; the report∩MUTATION pin stays exactly `{notify, search_recent_prs}`.
2. **Advisory divergence, documented:** the approval page binds to the **latest C2 comment** (`created_at`); the tool reads the **latest uploaded artifact** (`run_id`/`attempt` are GitHub-monotonic). They coincide whenever C2 completes normally (upload and comment are one job). The tool's output carries an explicit advisory caveat and the approval-page path; the system prompt tells the model the approval page is authoritative.
3. **Tier = `report`.** Asking questions must work in every autonomy mode — an Observe-mode operator is precisely the anxious adopter this feature serves. The tool reads bytes and returns text; no side effects.
4. **Summary honesty mirrors the page, with one deliberate divergence.** Like the approval page, the tool returns NO summary when the artifact is `unverifiable` or `integrity_ok == False` (never describe a possibly-tampered plan). UNLIKE the page (which also suppresses the summary card on denylist violations), the tool DOES return the summary alongside violations — "what does this blocked plan try to do?" is a legitimate question, the summary is mechanically derived from the integrity-checked bytes, and the violations are surfaced in the same response so the model cannot present the plan as approvable. The system prompt pins the framing.
5. **Sensitive values never reach the LLM.** `summarize_plan`'s `AttrChange.before/after` are display strings already masked (`(sensitive)`) and clamped (120 chars) — the tool serializes those, never raw plan values. Output is bounded by the lib caps (≤40 entries, ≤25 attr rows each).
6. **Link-out, not embed.** The IaC CSP is `default-src 'none'` with no `script-src` — a plain same-origin anchor is the only option, and it's the right one (one chat surface, one auth story). The link renders whenever a plan view exists (pending, blocked, AND terminal renders — questions are most valuable when something looks scary). Link copy says "read-only" (honesty: the chat cannot approve or apply).
7. **No `ChatRequest` change, no new endpoint, no new worker.** The PR number travels in the prefilled prompt text; the model calls `load_iac_plan(pr_number=N)`. `pr_number` is already in the inventory test's safe-params list.

## Invariant ledger (what moves, what must not)

| Pin | File | Change |
|---|---|---|
| `EXPECTED_TOOL_NAMES` | tests/unit/test_coordinator_tool_inventory.py | + `load_iac_plan_tool` |
| `EXPLORE_WORKLOAD_TOOL_NAMES` | agent/adk_agent.py (+ YAML order pin) | + `load_iac_plan` appended LAST |
| `TOOL_REGISTRY` / `TOOL_TIERS` set-equality | agent/workloads/registry.py | + `load_iac_plan` → callable / `"report"` |
| `TOOL_DESCRIPTIONS` set-equality | agent/capabilities.py | + description |
| explore ∩ MUTATION_TOOL_NAMES == ∅ | (unchanged) | holds — tool is NOT a mutation tool |
| report ∩ MUTATION == {notify, search_recent_prs} | (unchanged) | holds |
| apply tier == {upgrade_merge_pr} | (unchanged) | holds |
| dangerous name/param regexes | (unchanged) | `load_iac_plan` / `pr_number` both clean |
| `load_plan_view` behavior | agent/iac_artifacts.py | refactor-only — existing tests must stay green untouched |

**Rebake surface:** coordinator only (`agent/`, `workloads/`, `frontend/`, templates — all in `Dockerfile.agent`). No gate/denylist change → no tofu-apply/tofu-editor rebake; no `iac/` change → no infra-reader rebake.

---

### Task 1: GCS latest-plan resolver (`find_latest_plan_meta_in_gcs`)

**Files:**
- Modify: `agent/iac_artifacts.py` (new section after `find_latest_c2_comment`)
- Test: `tests/unit/test_iac_artifacts_gcs_latest.py` (new)

**Step 1: Write the failing tests**

```python
"""Tests for the GCS-listing latest-plan resolver (ClickOps item 12).

The resolver picks the newest metadata.json for a PR by (run_id, attempt,
generation) — GitHub run ids are globally monotonic, attempts monotonic
within a run; generation breaks the (impossible in practice) exact tie.
"""
from types import SimpleNamespace

import pytest

from agent.iac_artifacts import find_latest_plan_meta_in_gcs

SHA_A = "a" * 40
SHA_B = "b" * 40


class FakeListingClient:
    def __init__(self, blobs):
        self._blobs = blobs
        self.calls = []

    def list_blobs(self, bucket_name, prefix=None):
        self.calls.append((bucket_name, prefix))
        return [b for b in self._blobs if b.name.startswith(prefix or "")]


def _blob(name, generation=1):
    return SimpleNamespace(name=name, generation=generation)


def test_picks_highest_run_id():
    client = FakeListingClient([
        _blob(f"pr-7/{SHA_A}/run-100-1/metadata.json", 11),
        _blob(f"pr-7/{SHA_B}/run-200-1/metadata.json", 5),
    ])
    got = find_latest_plan_meta_in_gcs(7, bucket_name="bkt", client=client)
    assert got == (f"pr-7/{SHA_B}/run-200-1/metadata.json", 5)
    assert client.calls == [("bkt", "pr-7/")]


def test_attempt_breaks_run_tie():
    client = FakeListingClient([
        _blob(f"pr-7/{SHA_A}/run-100-1/metadata.json", 1),
        _blob(f"pr-7/{SHA_A}/run-100-3/metadata.json", 2),
    ])
    got = find_latest_plan_meta_in_gcs(7, bucket_name="bkt", client=client)
    assert got == (f"pr-7/{SHA_A}/run-100-3/metadata.json", 2)


def test_ignores_non_metadata_and_malformed_names():
    client = FakeListingClient([
        _blob(f"pr-7/{SHA_A}/run-100-1/plan.json", 1),
        _blob(f"pr-7/{SHA_A}/run-100-1/plan.tfplan", 1),
        _blob("pr-7/evil/run-1-1/metadata.json", 1),          # sha not hex40
        _blob(f"pr-7/{SHA_A}/run-0-1/metadata.json", 1),       # run id 0
        _blob(f"pr-77/{SHA_A}/run-999-1/metadata.json", 1),    # other PR (prefix-safe)
        _blob(f"pr-7/{SHA_A}/run-100-1/metadata.json", 4),
    ])
    got = find_latest_plan_meta_in_gcs(7, bucket_name="bkt", client=client)
    assert got == (f"pr-7/{SHA_A}/run-100-1/metadata.json", 4)


def test_none_when_no_artifacts():
    client = FakeListingClient([])
    assert find_latest_plan_meta_in_gcs(7, bucket_name="bkt", client=client) is None


@pytest.mark.parametrize("bad", [0, -1, "7", 1.5, None, True])
def test_rejects_non_positive_int_pr(bad):
    with pytest.raises(ValueError):
        find_latest_plan_meta_in_gcs(bad, bucket_name="bkt",
                                     client=FakeListingClient([]))
```

Note the `pr-77` case: the listing prefix is `pr-7/` (with trailing slash), so `pr-77/…` is excluded by prefix; the fake honors `startswith` to prove the trailing slash matters. `True` is rejected because `bool` is an `int` subclass — same `isinstance(v, bool)` discipline as `iac_plan_metadata`.

**Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_iac_artifacts_gcs_latest.py -q`
Expected: FAIL — `ImportError: cannot import name 'find_latest_plan_meta_in_gcs'`

**Step 3: Implement**

Append to `agent/iac_artifacts.py` (after `find_latest_c2_comment`, before the Plan-view section):

```python
# --------------------------------------------------------------------------- #
# GCS-listing latest-plan resolver (ClickOps item 12 — "ask about this change").
#
# The chat tool path: resolve the newest plan artifact for a PR WITHOUT the
# GitHub C2 comment. Rationale: find_latest_c2_comment rides the coordinator's
# write-capable GitHub PAT — the exact credential-containment reason
# search_recent_prs is banned from the read-only explore workload. The
# coordinator SA holds only roles/storage.objectViewer on the artifact bucket
# (read + list), so a listing-based resolver keeps explore strictly read-only.
#
# Divergence (advisory, documented): the approval page binds to the LATEST C2
# COMMENT; this resolver returns the LATEST UPLOADED artifact, ordered by
# (run_id, attempt, generation) — GitHub run ids are globally monotonic. The
# two coincide whenever a C2 run completes normally (upload + comment are one
# job). Q&A is advisory; the approval page + apply worker stay authoritative.
# --------------------------------------------------------------------------- #

_META_NAME_RE = re.compile(
    r"^pr-(?P<pr>[1-9][0-9]*)/[0-9a-f]{40}/run-(?P<run>[1-9][0-9]*)-(?P<attempt>[1-9][0-9]*)/metadata\.json$"
)


def find_latest_plan_meta_in_gcs(
    pr_number: int, *, bucket_name: str, client: Any = None
) -> tuple[str, int] | None:
    """Newest ``metadata.json`` object for ``pr_number`` → ``(object_name, generation)``.

    Lists ``pr-{N}/`` (trailing slash — ``pr-7/`` never matches ``pr-77/...``)
    and keeps only names matching the full artifact scheme with a metadata.json
    basename AND the exact PR number (defense in depth on top of the prefix).
    ``None`` when the PR has no plan artifact. Raises ``ValueError`` on a
    non-positive/non-int ``pr_number`` (the tool layer translates to an error
    dict); GCS listing errors propagate (the tool layer is fail-soft).
    """
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number <= 0:
        raise ValueError(f"pr_number must be a positive int (got {pr_number!r})")
    if client is None:
        from google.cloud import storage  # lazy: tests inject a double

        client = storage.Client()
    best: tuple[tuple[int, int, int], str, int] | None = None
    for blob in client.list_blobs(bucket_name, prefix=f"pr-{pr_number}/"):
        m = _META_NAME_RE.fullmatch(blob.name)
        if m is None or int(m.group("pr")) != pr_number:
            continue
        gen = int(getattr(blob, "generation", 0) or 0)
        key = (int(m.group("run")), int(m.group("attempt")), gen)
        if best is None or key > best[0]:
            best = (key, blob.name, gen)
    if best is None:
        return None
    return best[1], best[2]
```

**Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_iac_artifacts_gcs_latest.py -q`
Expected: PASS

**Step 5: Commit**

```bash
git add agent/iac_artifacts.py tests/unit/test_iac_artifacts_gcs_latest.py
git commit -m "feat(iac): GCS-listing latest-plan resolver (item 12, no GitHub PAT)"
```

---

### Task 2: `load_plan_view_from_gcs` (+ behavior-preserving refactor of `load_plan_view`)

**Files:**
- Modify: `agent/iac_artifacts.py` (extract shared tail; add new loader)
- Test: `tests/unit/test_iac_artifacts_gcs_latest.py` (extend)
- Guard: ALL existing `tests/unit/test_iac_artifacts*.py` / approval integration tests must pass UNMODIFIED.

**Step 1: Write the failing tests** (extend the Task-1 file)

```python
import hashlib
import json

from agent.iac_artifacts import IacPlanView, load_plan_view_from_gcs


def _c2v1_metadata(pr=7, *, plan_json_bytes):
    # Mirrors driftscribe_lib.iac_plan_metadata.build_metadata's field set.
    sha = SHA_A
    return {
        "schema_version": "c2.v1",
        "repo": "adi-prasetyo/driftscribe",
        "pr_number": pr,
        "head_sha": sha,
        "base_sha": SHA_B,
        "workflow_run_id": 100,
        "workflow_run_attempt": 1,
        "artifact_uri_plan": f"gs://driftscribe-hack-2026-tofu-artifacts/pr-{pr}/{sha}/run-100-1/plan.tfplan",
        "artifact_uri_json": f"gs://driftscribe-hack-2026-tofu-artifacts/pr-{pr}/{sha}/run-100-1/plan.json",
        "generation_plan": "1",
        "generation_json": "2",
        "plan_sha256": "c" * 64,
        "plan_json_sha256": hashlib.sha256(plan_json_bytes).hexdigest(),
        "opentofu_version": "1.12.0",
        "provider_lockfile_sha256": "d" * 64,
    }


class FakeGcsClient(FakeListingClient):
    """Listing + generation-pinned fetch, matching fetch_gcs_object's calls."""

    def __init__(self, blobs, objects):
        super().__init__(blobs)
        self._objects = objects  # {(name, int(generation)): bytes}

    def bucket(self, name):
        outer = self

        class _B:
            def blob(self, object_name, generation=None):
                class _Blob:
                    def download_as_bytes(self, raw_download=True, if_generation_match=None):
                        return outer._objects[(object_name, if_generation_match)]
                return _Blob()
        return _B()


def _fixture_client(pr=7, plan_json=None):
    plan_json_bytes = json.dumps(plan_json if plan_json is not None
                                 else {"resource_changes": []}).encode()
    md = _c2v1_metadata(pr, plan_json_bytes=plan_json_bytes)
    meta_name = f"pr-{pr}/{SHA_A}/run-100-1/metadata.json"
    return FakeGcsClient(
        blobs=[_blob(meta_name, 3)],
        objects={
            (meta_name, 3): json.dumps(md).encode(),
            (f"pr-{pr}/{SHA_A}/run-100-1/plan.json", 2): plan_json_bytes,
        },
    )


def test_load_from_gcs_happy_path_verifies_and_summarizes():
    bucket = "driftscribe-hack-2026-tofu-artifacts"
    view = load_plan_view_from_gcs(7, bucket_name=bucket, client=_fixture_client())
    assert isinstance(view, IacPlanView)
    assert view.unverifiable is False
    assert view.integrity_ok is True
    assert view.denylist_violations == []
    assert view.change_summary is not None and view.change_summary.entries == ()
    assert view.tofu_show_text == ""  # no C2 comment on this path — by design
    assert view.generation_metadata == "3"
    assert view.artifact_uri_metadata.endswith("/metadata.json")


def test_load_from_gcs_none_when_no_artifact():
    bucket = "driftscribe-hack-2026-tofu-artifacts"
    assert load_plan_view_from_gcs(7, bucket_name=bucket,
                                   client=FakeGcsClient([], {})) is None


def test_load_from_gcs_unverifiable_on_malformed_metadata():
    bucket = "driftscribe-hack-2026-tofu-artifacts"
    client = _fixture_client()
    meta_name = f"pr-7/{SHA_A}/run-100-1/metadata.json"
    client._objects[(meta_name, 3)] = b"not json"
    view = load_plan_view_from_gcs(7, bucket_name=bucket, client=client)
    assert view is not None and view.unverifiable is True


def test_load_from_gcs_unverifiable_on_pr_mismatch():
    # metadata claims a different PR than the listing prefix — refuse.
    bucket = "driftscribe-hack-2026-tofu-artifacts"
    plan_json_bytes = json.dumps({"resource_changes": []}).encode()
    md = _c2v1_metadata(9, plan_json_bytes=plan_json_bytes)  # claims PR 9
    meta_name = f"pr-7/{SHA_A}/run-100-1/metadata.json"      # listed under PR 7
    # build_metadata cross-checks URIs against pr_number, so craft URIs for 9
    # but store under 7 — the loader must catch the mismatch itself.
    client = FakeGcsClient(
        blobs=[_blob(meta_name, 3)],
        objects={(meta_name, 3): json.dumps(md).encode()},
    )
    view = load_plan_view_from_gcs(7, bucket_name=bucket, client=client)
    assert view is not None and view.unverifiable is True


def test_load_from_gcs_integrity_mismatch_flagged():
    bucket = "driftscribe-hack-2026-tofu-artifacts"
    client = _fixture_client()
    client._objects[(f"pr-7/{SHA_A}/run-100-1/plan.json", 2)] = b'{"tampered": 1}'
    view = load_plan_view_from_gcs(7, bucket_name=bucket, client=client)
    assert view is not None and view.integrity_ok is False
```

**Step 2: Run to verify failure** — `ImportError` on `load_plan_view_from_gcs`.

**Step 3: Implement.** Two parts, in `agent/iac_artifacts.py`:

**(a) Extract the shared tail.** Cut steps 3–5 of `load_plan_view` (the "Step 3: fetch plan.json" block through the denylist block) verbatim into:

```python
def _populate_plan_view_from_metadata(
    view: IacPlanView, md: dict[str, Any], *, bucket_name: str, client: Any
) -> None:
    """Steps 3–5 of the plan-view load, shared by the comment path
    (:func:`load_plan_view`) and the GCS-listing path
    (:func:`load_plan_view_from_gcs`): fetch plan.json pinned to the
    validated metadata's generation, recompute the digest, re-run the C1
    denylist. Mutates ``view`` in place; any failure sets ``unverifiable``."""
```

…and have `load_plan_view` call it after `view.metadata = md`. **The diff inside the moved block must be zero** (pure move); `load_plan_view`'s docstring and signature unchanged.

**(b) The new loader:**

```python
def load_plan_view_from_gcs(
    pr_number: int, *, bucket_name: str, client: Any = None
) -> IacPlanView | None:
    """Fetch + advisory-verify the NEWEST plan artifact for ``pr_number`` by
    GCS listing (no GitHub) — the explore chat tool's loader.

    ``None`` when the PR has no plan artifact at all. Otherwise the same
    fail-closed IacPlanView contract as :func:`load_plan_view`, with two
    structural differences: ``tofu_show_text`` is always ``""`` (it lives only
    in the C2 comment) and the metadata identity comes from the listing (the
    loader additionally refuses, as unverifiable, a metadata doc whose own
    ``pr_number`` disagrees with the requested one).
    """
    if client is None:
        from google.cloud import storage  # lazy: tests inject a double

        client = storage.Client()
    found = find_latest_plan_meta_in_gcs(
        pr_number, bucket_name=bucket_name, client=client
    )
    if found is None:
        return None
    meta_obj, generation = found
    view = IacPlanView(
        _artifact_uri_metadata=f"gs://{bucket_name}/{meta_obj}",
        _generation_metadata=str(generation),
    )
    try:
        meta_bytes = fetch_gcs_object(bucket_name, meta_obj, generation, client=client)
        md = json.loads(meta_bytes.decode("utf-8"))
    except (IacArtifactError, ValueError, UnicodeDecodeError):
        view.unverifiable = True
        return view
    if not _assert_c2v1_metadata(md) or md.get("pr_number") != pr_number:
        view.unverifiable = True
        return view
    view.metadata = md
    _populate_plan_view_from_metadata(view, md, bucket_name=bucket_name, client=client)
    return view
```

**Step 4: Run** the new file AND the full existing artifact/approval suites:
`.venv/bin/pytest tests/unit/test_iac_artifacts_gcs_latest.py tests/unit -k "iac_artifact or iac_approval" -q` then `.venv/bin/pytest tests/integration/test_iac_approval_get.py tests/integration/test_iac_approval_post.py -q`
Expected: ALL PASS with zero edits to existing tests (refactor-faithfulness proof).

**Step 5: Commit**

```bash
git add agent/iac_artifacts.py tests/unit/test_iac_artifacts_gcs_latest.py
git commit -m "feat(iac): load_plan_view_from_gcs — comment-free plan view for chat (item 12)"
```

---

### Task 3: the `load_iac_plan_tool` callable

**Files:**
- Modify: `agent/adk_tools.py` (new section after `load_contract_tool`)
- Test: `tests/unit/test_load_iac_plan_tool.py` (new)

**Step 1: Write the failing tests**

```python
"""load_iac_plan_tool — bounded, fail-soft, sensitive-masked plan Q&A surface.

Pins (item 12 design §4–5):
- NO summary when unverifiable or integrity fails (never describe a
  possibly-tampered plan).
- Summary IS returned alongside denylist violations (deliberate divergence
  from the approval page's card suppression — framing pinned in the prompt).
- AttrChange display strings pass through ALREADY masked — assert the
  literal "(sensitive)" marker, never a raw value.
- Fail-soft: every failure path returns an error dict; the tool never raises.
"""
```

Test cases (full code in-file, structure as in Task 1/2 with monkeypatched `agent.adk_tools.load_plan_view_from_gcs` + `get_settings.cache_clear()` and `GCP_PROJECT=testproj` env so `artifacts_bucket` derives `testproj-tofu-artifacts`):

1. `test_not_found` — loader returns `None` → `{"found": False, "error": …}` mentioning the plan-builder workflow; `bucket_name` kwarg received `"testproj-tofu-artifacts"`.
2. `test_invalid_pr_number` — `load_iac_plan_tool(0)` and `load_iac_plan_tool(-3)` → error dict, loader NOT called.
3. `test_unverifiable_returns_no_summary` — view with `unverifiable=True` → `found=True`, `unverifiable=True`, no `"summary"` key, `error` says the artifact could not be verified.
4. `test_integrity_mismatch_returns_no_summary` — `integrity_ok=False` → no `"summary"` key, error names the integrity mismatch.
5. `test_happy_path_summary_shape` — build a real `IacPlanView` whose `_plan_json` is a small two-resource plan (one `create` bucket with `uniform_bucket_level_access` attr, one `update` with a **sensitive** attr change); assert: counts dict, `entries[0]` has `verb/type_label/name/address/location/attr_changes`, the sensitive row renders exactly `"(sensitive)"` on both sides, `blast_radius` equals `blast_radius_phrase(view.change_summary)`, `cannot_touch == BLAST_CANNOT_TOUCH_NOTE`, `approval_page == "/iac-approvals/7"`, and a non-empty `caveat`.
6. `test_denylist_violations_with_summary` — view with violations + `integrity_ok=True` → BOTH `denylist_violations` (as `[{"rule","detail"}]`) AND `summary` present, plus `blocked=True`.
7. `test_summary_unavailable` — `_plan_json` shaped so `summarize_plan` returns `None` → `summary` is `None` + `summary_unavailable` prose.
8. `test_loader_exception_is_fail_soft` — loader raises `RuntimeError("boom")` → `{"found": False, "error": "..."}`, no exception.

**Step 2: Run to verify failure** — import error.

**Step 3: Implement** in `agent/adk_tools.py` (imports stay lazy where heavy):

```python
def load_iac_plan_tool(pr_number: int) -> dict[str, Any]:
    """Read the latest verified ``tofu plan`` artifact for an infra PR — read-only.

    Coordinator-local (like :func:`load_contract_tool`): resolves the newest
    c2.v1 artifact for ``pr_number`` by LISTING the artifacts bucket
    (``agent.iac_artifacts.load_plan_view_from_gcs``) — deliberately NOT via
    the GitHub C2 comment, which would ride the coordinator's write-capable
    PAT inside the strictly read-only explore workload. The coordinator SA
    holds only roles/storage.objectViewer on that bucket.

    Output contract (bounded; values pre-masked by driftscribe_lib —
    sensitive attribute values arrive as the literal "(sensitive)" marker):

    - not found            → ``{"found": False, "error": ...}``
    - unverifiable / integrity mismatch → ``found=True`` + ``error``, NO summary
      (never describe a possibly-tampered plan)
    - verified             → ``summary`` (plain-language entries + counts),
      ``blast_radius`` + ``cannot_touch`` (item-8 reuse), ``denylist_violations``
      (summary INCLUDED alongside violations — explaining a blocked plan is the
      point; ``blocked=True`` keeps the framing honest), ``approval_page`` path,
      and an advisory ``caveat``.

    Fail-soft: never raises — every failure path returns an error dict the
    model can relay (explore prompt rule: surface tool errors, don't invent).
    """
    from agent.config import artifacts_bucket
    from agent.iac_artifacts import load_plan_view_from_gcs
    from driftscribe_lib.iac_plan_summary import (
        BLAST_CANNOT_TOUCH_NOTE,
        blast_radius_phrase,
    )

    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number <= 0:
        return {
            "found": False,
            "error": f"pr_number must be a positive integer (got {pr_number!r})",
        }
    s = get_settings()
    try:
        view = load_plan_view_from_gcs(pr_number, bucket_name=artifacts_bucket(s))
    except Exception as e:  # noqa: BLE001 — advisory read; chat turn must survive
        return {"found": False, "error": f"plan artifact read failed: {e}"}
    if view is None:
        return {
            "found": False,
            "error": (
                f"no plan artifact found for PR #{pr_number} — the plan-builder "
                "workflow may not have run for it yet, or the PR number is wrong"
            ),
        }
    out: dict[str, Any] = {
        "found": True,
        "pr_number": pr_number,
        "head_sha": view.head_sha,
        "opentofu_version": str(view.metadata.get("opentofu_version", "")),
        "integrity_ok": view.integrity_ok,
        "unverifiable": view.unverifiable,
        "approval_page": f"/iac-approvals/{pr_number}",
        "caveat": (
            "Advisory: this is the latest plan artifact uploaded for this PR. "
            "Nothing can be applied from chat — an operator decides on the "
            "approval page, and the apply worker independently re-verifies "
            "integrity, policy, and plan fidelity before anything applies."
        ),
    }
    if view.unverifiable:
        out["error"] = (
            "the plan artifact could not be verified — its contents are "
            "unavailable; do not describe what this plan does"
        )
        return out
    if not view.integrity_ok:
        out["error"] = (
            "plan integrity check FAILED (digest mismatch) — do not describe "
            "or rely on this plan's contents"
        )
        return out
    out["denylist_violations"] = [
        {"rule": r, "detail": d} for r, d in view.denylist_violations
    ]
    out["blocked"] = bool(view.denylist_violations)
    summary = view.change_summary
    if summary is None:
        out["summary"] = None
        out["summary_unavailable"] = (
            "no faithful structured summary could be derived from this plan — "
            "point the operator at the approval page's raw plan output"
        )
        return out
    out["summary"] = {
        "counts": {
            "create": summary.n_create,
            "update": summary.n_update,
            "destroy": summary.n_destroy,
            "replace": summary.n_replace,
            "import": summary.n_import,
            "forget": summary.n_forget,
            "other": summary.n_change,
        },
        "destructive": summary.destructive,
        "adopt_only": summary.adopt_only,
        "entries": [
            {
                "verb": e.verb,
                "resource_type": e.type_label,
                "name": e.name,
                "address": e.address,
                "location": e.location,
                "imported": e.imported,
                "deposed": e.deposed,
                "action_reason": e.action_reason,
                "attr_changes": [
                    {
                        "path": a.path,
                        "before": a.before,
                        "after": a.after,
                        "sensitive": a.sensitive,
                    }
                    for a in e.attr_changes
                ],
                "attrs_truncated": e.attrs_truncated,
            }
            for e in summary.entries
        ],
        "n_hidden": summary.n_hidden,
    }
    out["blast_radius"] = blast_radius_phrase(summary)
    out["cannot_touch"] = BLAST_CANNOT_TOUCH_NOTE
    return out
```

**Step 4: Run** `.venv/bin/pytest tests/unit/test_load_iac_plan_tool.py -q` → PASS.

**Step 5: Commit**

```bash
git add agent/adk_tools.py tests/unit/test_load_iac_plan_tool.py
git commit -m "feat(agent): load_iac_plan tool — bounded plan Q&A for explore (item 12)"
```

---

### Task 4: registration — registry, tier, manifest, description, inventory pins

**Files:**
- Modify: `agent/workloads/registry.py` (`_TOOL_REGISTRY` + `_TOOL_TIERS`)
- Modify: `agent/adk_agent.py` (import; `COORDINATOR_TOOLS`; `EXPLORE_WORKLOAD_TOOL_NAMES`)
- Modify: `agent/capabilities.py` (`TOOL_DESCRIPTIONS`)
- Modify: `tests/unit/test_coordinator_tool_inventory.py` (`EXPECTED_TOOL_NAMES`)

**Step 1: Run the inventory + tiers + capabilities suites FIRST** to see the exact failures the registration must fix (red): add `load_iac_plan_tool` to `EXPECTED_TOOL_NAMES` and `"load_iac_plan"` to `EXPLORE_WORKLOAD_TOOL_NAMES` (test side comes from Task 5's YAML; this task wires the code constants), then watch set-equality tests fail until all four files agree.

**Step 2: Edits.**

`agent/workloads/registry.py` — after the `search_recent_prs` entry in `_TOOL_REGISTRY`:

```python
    # Read the latest verified tofu-plan artifact for an infra PR (ClickOps
    # item 12 — "ask about this change"). Coordinator-local, GCS-listing only:
    # deliberately NEVER the GitHub C2 comment, so it carries no write-capable
    # credential and stays eligible for the strictly read-only explore
    # workload (objectViewer on the artifacts bucket is the whole authority).
    "load_iac_plan":           load_iac_plan_tool,
```

…and in `_TOOL_TIERS` (after `search_recent_prs`):

```python
    "load_iac_plan":              "report",
```

`agent/adk_agent.py` — import `load_iac_plan_tool` from `agent.adk_tools`; append to `COORDINATOR_TOOLS` (with a comment mirroring the registry's); append to the explore tuple **last** (order pin):

```python
EXPLORE_WORKLOAD_TOOL_NAMES: tuple[str, ...] = (
    "drift_read_live_env",
    "upgrade_read_dependencies",
    "load_contract",
    "search_developer_docs",
    "retrieve_developer_doc",
    "read_project_inventory",
    # Item 12 — pending-infra-PR plan Q&A. Read-only by credential
    # (GCS objectViewer, no GitHub PAT) — see agent/adk_tools.py.
    "load_iac_plan",
)
```

`agent/capabilities.py` — `TOOL_DESCRIPTIONS` entry (after `search_recent_prs`):

```python
    "load_iac_plan": (
        "Reads the latest verified plan artifact for a pending infrastructure "
        "PR and summarizes it in plain language — read-only; cannot approve, "
        "reject, or apply anything."
    ),
```

`tests/unit/test_coordinator_tool_inventory.py` — add to `EXPECTED_TOOL_NAMES`:

```python
    # Item 12 — pending-infra-PR plan Q&A for the explore workload.
    # Coordinator-local GCS read (objectViewer only, no GitHub PAT) —
    # deliberately NOT in MUTATION_TOOL_NAMES: both the operation and the
    # credential are read-only, unlike search_recent_prs.
    "load_iac_plan_tool",
```

**Step 3: Run** `.venv/bin/pytest tests/unit/test_coordinator_tool_inventory.py tests/unit/test_tool_tiers.py tests/unit/test_capabilities.py -q`
Expected: inventory tests that pin the explore YAML still FAIL (YAML lags until Task 5) — everything else PASS. If the YAML pin is the only red, proceed; commit lands in Task 5 to keep the tree green per-commit. **Alternative (preferred): do Task 4 and Task 5 edits in one commit** — the lockstep pins make them one logical change.

---

### Task 5: explore workload manifest + system prompt

**Files:**
- Modify: `workloads/explore/workload.yaml`
- Modify: `workloads/explore/system_prompt.md`
- Test: existing `tests/unit/test_explore_workload_loads.py` + `tests/unit/test_coordinator_tool_inventory.py` (no new test files; the pins are the tests)

**Step 1: YAML.** Append to `enabled_tool_names` (LAST — order pin):

```yaml
enabled_tool_names:
  - drift_read_live_env
  - upgrade_read_dependencies
  - load_contract
  - search_developer_docs
  - retrieve_developer_doc
  - read_project_inventory
  # Item 12 — read the latest verified plan artifact for a pending infra PR
  # (GCS listing + objectViewer only; no GitHub credential — see the
  # search_recent_prs exclusion note above, which this tool does NOT trip).
  - load_iac_plan
```

Also update the header comment's "NO search_recent_prs" rationale block to mention the new tool's credential story (one sentence). `worker_names` unchanged (coordinator-local tool, no worker).

**Step 2: System prompt.** In `workloads/explore/system_prompt.md`, add to the tools list:

```
- load_iac_plan_tool(pr_number) — read the latest verified `tofu plan`
  artifact for a pending infrastructure PR and get a plain-language summary:
  what would be created/updated/destroyed, the attribute-level diffs
  (sensitive values masked), the blast radius, and the policy (denylist)
  verdict. Read-only: it reads a plan file from storage; it cannot approve,
  reject, apply, or change the PR.
```

And to the Rules:

```
- When the operator asks about a pending infrastructure change or arrives
  from an approval page mentioning a PR number, call load_iac_plan_tool
  first and explain the plan in plain language — lead with what changes
  (the counts and the entries), then the blast radius. Use
  search_developer_docs to explain unfamiliar resource settings (e.g.
  `uniform_bucket_level_access`) when the operator asks what something
  means.
- Relay the tool's verification verdicts honestly. If it reports the
  artifact unverifiable or an integrity mismatch, say the plan's contents
  cannot be trusted and DO NOT describe them. If it reports denylist
  violations, lead with "this plan is blocked by policy" and use the
  summary only to explain WHAT the blocked plan attempted — never present
  a blocked plan as approvable.
- You cannot approve or apply anything, and this conversation changes
  nothing. The decision happens on the approval page
  (/iac-approvals/<pr_number>), where the apply worker independently
  re-verifies the plan before anything runs. Frame this as how the system
  works — the operator stays in charge — not as a safety guarantee from you.
- The plan you read is the latest one uploaded for that PR. If the PR was
  just rebuilt, the approval page is authoritative — suggest reloading it
  if anything looks inconsistent.
```

**Step 3: Run** `.venv/bin/pytest tests/unit/test_explore_workload_loads.py tests/unit/test_coordinator_tool_inventory.py tests/unit/test_capabilities.py tests/unit/test_tool_tiers.py tests/unit/test_workload_registry.py -q`
Expected: ALL PASS (the read-only-flavored prompt test greps for read-only language — keep the word "read-only" in the added text, which it has).

**Step 4: Commit (Tasks 4+5 together)**

```bash
git add agent/workloads/registry.py agent/adk_agent.py agent/capabilities.py \
        workloads/explore/workload.yaml workloads/explore/system_prompt.md \
        tests/unit/test_coordinator_tool_inventory.py
git commit -m "feat(explore): register load_iac_plan (report tier) + prompt rules (item 12)"
```

---

### Task 6: "Ask about this change" link on the approval page

**Files:**
- Modify: `agent/templates/iac_approval.html`
- Test: `tests/integration/test_iac_approval_get.py` (extend)

**Step 1: Write the failing test** (in the existing GET test file, reusing its fixtures/style — find the test that asserts `preview-map-link` and mirror it):

```python
def test_ask_about_link_renders_with_view(...existing fixture signature...):
    # Renders whenever a plan view exists — pending, blocked, AND terminal
    # renders: questions matter most when something looks scary.
    resp = client.get(f"/iac-approvals/{PR}")
    assert resp.status_code == 200
    assert f'href="/?ask_pr={PR}"' in resp.text
    assert 'data-testid="ask-about-link"' in resp.text


def test_ask_about_link_absent_without_artifact(...):
    # view is None (no C2 artifact) → no link: there is nothing to ask about
    # and the chat tool would only report not-found.
    ...
    assert "ask_pr" not in resp.text
```

Also extend ONE blocked-render test (unverifiable or denylist) and one terminal-render test with the `ask-about-link` presence assertion — the link must NOT be gated on `can_approve`/`show_summary`.

**Step 2: Run to verify failure.**

**Step 3: Implement.** In `agent/templates/iac_approval.html`, immediately AFTER the integrity/denylist card's closing `</div>` (line ~68, before the summary block):

```html
      <p class="ds-subtle">
        <a href="/?ask_pr={{ pr_number }}" data-testid="ask-about-link">
          Questions about this change? Ask the read-only assistant →</a>
      </p>
```

(Same-origin anchor; allowed under `default-src 'none'` because navigation links are not a fetch directive. Lives inside the `view is not none` branch, outside every inner conditional.)

**Step 4: Run** `.venv/bin/pytest tests/integration/test_iac_approval_get.py tests/integration/test_iac_approval_post.py -q` → PASS.

**Step 5: Commit**

```bash
git add agent/templates/iac_approval.html tests/integration/test_iac_approval_get.py
git commit -m "feat(ui): ask-about-this-change link on the IaC approval page (item 12)"
```

---### Task 7: frontend lib — `askPrFromSearch` + `askAboutPrPrefill`

**Files:**
- Modify: `frontend/src/lib/workloads.ts`
- Test: `frontend/tests/unit/workloads.test.ts` (extend)

**Step 1: Write the failing tests** (vitest, in the existing file's style):

```ts
import { askAboutPrPrefill, askPrFromSearch } from '../../src/lib/workloads';

describe('askPrFromSearch', () => {
  it('parses a positive integer', () => {
    expect(askPrFromSearch('?ask_pr=18')).toBe(18);
    expect(askPrFromSearch('?preview_pr=3&ask_pr=00012')).toBe(12);
  });
  it('rejects junk, zero, negatives, floats, absence', () => {
    for (const s of ['', '?ask_pr=', '?ask_pr=abc', '?ask_pr=0', '?ask_pr=-3',
                     '?ask_pr=1.5', '?other=1']) {
      expect(askPrFromSearch(s)).toBeNull();
    }
  });
});

describe('askAboutPrPrefill', () => {
  it('names the PR and asks for a plain-language explanation', () => {
    const text = askAboutPrPrefill(18);
    expect(text).toContain('PR #18');
    expect(text.toLowerCase()).toContain('plain language');
  });
});
```

**Step 2: Run** `npm run test -- workloads` (from `frontend/`) → FAIL (no export).

**Step 3: Implement** in `frontend/src/lib/workloads.ts`:

```ts
/**
 * Parse `ask_pr` from a `location.search` string (the approval page's
 * "ask about this change" link). Same validation discipline as
 * infra_graph.ts::previewPrFromSearch: all-digits, positive, safe integer.
 */
export function askPrFromSearch(search: string): number | null {
  const raw = new URLSearchParams(search).get('ask_pr');
  if (raw === null || !/^\d+$/.test(raw)) return null;
  const n = Number(raw);
  return Number.isSafeInteger(n) && n > 0 ? n : null;
}

/**
 * Composer prefill text for an ask_pr arrival. PREFILLED, never auto-sent —
 * the operator reads and edits before anything happens (same contract as the
 * Adopt-button bridge). The PR number rides in the text; the explore agent
 * extracts it for load_iac_plan.
 */
export function askAboutPrPrefill(pr: number): string {
  return (
    `I'm reviewing infrastructure change PR #${pr} before deciding on it. ` +
    'Load its plan and explain what it would change in plain language.'
  );
}
```

**Step 4: Run** → PASS.

**Step 5: Commit**

```bash
git add frontend/src/lib/workloads.ts frontend/tests/unit/workloads.test.ts
git commit -m "feat(ui): ask_pr query parsing + explore prefill text (item 12)"
```

---

### Task 8: App.svelte boot wiring

**Files:**
- Modify: `frontend/src/App.svelte`

**Step 1: Implement.** Three small edits:

(a) Import: add `askPrFromSearch, askAboutPrPrefill` to the existing `./lib/workloads` import (where `ChatPrefill`/`WORKLOADS` come from).

(b) Below the `previewPr` boot parse (line ~57) and the `chatPrefill` declaration (line ~98), seed the prefill at boot — Svelte 5 `$state` initializers run once:

```ts
  // ?ask_pr=N (linked from the IaC approval page) → prefill the composer with
  // an explore-workload question about that PR. PREFILL ONLY (never auto-send)
  // — the same operator-stays-in-charge contract as the Adopt bridge above.
  const bootAskPr = askPrFromSearch(window.location.search);
```

…and change the `chatPrefill` initializer:

```ts
  let chatPrefill = $state<ChatPrefill | null>(
    bootAskPr !== null
      ? { text: askAboutPrPrefill(bootAskPr), workload: 'explore', epoch: 1 }
      : null
  );
```

(c) In the component's existing `onMount` (or alongside the boot code if mount work is done elsewhere — match the file's current pattern), strip the param and reveal the composer:

```ts
  if (bootAskPr !== null) {
    // Remove ONLY ask_pr (preserve other params + hash) so reload/share
    // doesn't re-prefill — mirrors exitPreview()'s surgical removal.
    const u = new URL(window.location.href);
    u.searchParams.delete('ask_pr');
    history.replaceState(null, '', u);
    document.getElementById('chat-form')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
```

`ChatForm`'s `$effect` applies a non-null prefill whose `epoch` differs from its `lastPrefillEpoch` start value, so a boot-seeded `{epoch: 1}` applies on mount; a later Adopt click bumps from `chatPrefill?.epoch ?? 0`, which now reads 1 → 2 — no collision.

**Step 2: Verify.** `npm run test` (full vitest — ChatForm prefill tests must stay green) and `npm run build` from `frontend/`. Manual reasoning check: `?ask_pr=18&preview_pr=18` → both features engage independently (preview panel + prefill); acceptable and useful.

**Step 3: Commit**

```bash
git add frontend/src/App.svelte
git commit -m "feat(ui): boot-seed explore chat prefill from ?ask_pr (item 12)"
```

---

### Task 9: full verification

**Step 1:** `.venv/bin/pytest -q` from repo root — expect ≥ 2731 + new, zero failures.
**Step 2:** `cd frontend && npm run test && npm run build` — expect ≥ 483 + new, zero failures.
**Step 3:** `.venv/bin/ruff check --no-cache .` — clean (watch unused imports in adk_tools/adk_agent — the item-11 DON'T-SHIP lesson).
**Step 4:** Re-read the full `git diff main` hunk-by-hunk before opening the PR.

---

## Mode × surface matrix (autonomy-dial interplay)

| Surface | Observe | Propose | Propose+Apply |
|---|---|---|---|
| `load_iac_plan` in explore chat | available (report tier) | available | available |
| Approval-page ask link | renders (page is mode-agnostic) | renders | renders |
| Approve button next to it | already 409-gated by item 11 | 409 | normal |

The feature deliberately works fully in Observe — explaining a pending change requires no authority.

## Live verification plan (post-deploy)

1. `GET /iac-approvals/<merged-or-open PR with artifact>` → ask-about link present with correct href.
2. Click-through (Playwright, `frontend/.scratch-e2e/`, recipe per memory): land on `/?ask_pr=N` → composer prefilled with the PR text, workload selector shows "Explore (read-only)", URL param stripped.
3. Send the prefilled prompt live → reply describes the plan in plain language; trace shows exactly one `load_iac_plan` call; no mutation tool calls.
4. Negative: ask about a PR with no artifact → honest not-found reply.
5. Capability card → explore lists the new tool with the read-only description.
