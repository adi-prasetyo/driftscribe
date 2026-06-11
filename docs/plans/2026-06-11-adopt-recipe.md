# Adopt Recipe (adopt design Phase 3) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Give the coordinator agent a deterministic, probe-proven adopt tool — `propose_adoption` — that renders the resource block + co-located import block for one live unmanaged resource and opens the PR through the unchanged tofu-editor, with honest failure copy when live config can't be mirrored.

**Architecture:** A pure renderer in `driftscribe_lib/adopt_recipe.py` produces byte-deterministic HCL (resource + import block) per adoptable type from identifying facts only (name, location, topic, image). A new coordinator mutation tool `propose_adoption_tool` validates params, renders, and reuses the exact `open_infra_pr_tool` plumbing (authority derivation, worker call, compact result, pending-approval notification). No gate, denylist, or worker changes — admission shipped in Phase 2; this phase only *produces* PRs that pass it.

**Tech Stack:** Python 3.12 / pytest; hcl2 (parse-back validation); the Phase-2 static gate + denylist as consistency oracles in tests; no frontend changes except capability-card tool listing fallout.

**Parent design:** `docs/plans/2026-06-11-adopt-import-design.md` (§5 recipe, §7 phase 3, §9 fidelity probes). Phases 1+2 shipped (PR #91 → `1e35164`, PR #92 → `88d533a`, live on all three services).

---

## §0 Grounded facts (verified 2026-06-11, this session)

### 0.1 What the agent can and cannot read

- `read_project_inventory_tool` → infra-reader `/describe` (`workers/infra_reader/main.py:114-144`). CAI read mask is **exactly** `["name", "asset_type", "location"]` (`main.py:50-54`) — deliberately minimal, no `resource.data`. Per-type samples capped at 10.
- The reader worker (`drift_read_live_env`) is **pinned to payment-demo** at boot; env vars + revision only.
- **No tool can read**: a bucket's storage class/UBLA/PAP, a subscription's topic, a run service's image/spec. ⇒ The recipe renders *minimal* blocks; `topic` (subscription) and `image` (run service) are **operator-supplied tool params** — the agent must ask, never guess.

### 0.2 Adoption-fidelity probe results (design §9 — the evidence)

Probed live against `driftscribe-hack-2026`, scratch dirs `/tmp/adopt-probe/{bucket,topic,sub,run}/`, provider `hashicorp/google 6.50.0` (== `iac/.terraform.lock.hcl`), tofu 1.12.0, local backend, provider block mirroring `iac/providers.tf` (project+region vars, **NO** `add_terraform_attribution_label` override). Probe resources hand-made via gcloud (ClickOps simulation): bucket `driftscribe-hack-2026-adopt-probe` (UBLA on + PAP enforced = console defaults), topic `adopt-probe-topic`, sub `adopt-probe-sub`, run svc `adopt-probe-svc` (image `gcr.io/cloudrun/hello`, default SA, no-unauth).

| Probe | Config rendered | Plan result |
|---|---|---|
| bucket, console defaults | `name` + `project` + `location` only | **pure no-op import** |
| bucket + live-only label (`made-by=console-user`) | same | **pure no-op** (labels non-authoritative in provider 5+; omitted `labels` attr ⇒ no diff, no attribution injection) |
| bucket + versioning enabled live | same | **pure no-op** (optional+computed adopts live value) |
| bucket + `NEARLINE` storage class live | same | `importing`+**update** on `storage_class` (optional-with-default `STANDARD`) → Phase-2 `import-with-changes-forbidden-v1` refusal — the honest "can't cleanly adopt yet" case |
| topic | `project` + `name` | **pure no-op** |
| subscription | `project` + `name` + `topic` as **full-path literal string** | **pure no-op** (default ack deadline etc. adopt live) |
| run service, image known | + `template{containers{image}}` + `lifecycle { ignore_changes = [client, client_version, scaling] }` | **pure no-op**. Without `scaling` in ignore_changes: `update` with exactly one diff — top-level `scaling` `[{...}] → []` (server-populated on gcloud deploys). Same trio the managed `iac/checkout_storefront.tf` already uses. |

**Attribution-label landmine resolved:** the Phase-2 c6e probe needed `add_terraform_attribution_label = false` because its config *declared* `labels` (declaring `labels` makes the provider compute `terraform_labels` = config + `goog-terraform-provisioned`). The minimal recipe **omits `labels` entirely** ⇒ no injection, no provider-block change needed. Live-only labels stay live (proven above).

- Lowercase `location = "asia-northeast1"` vs live `ASIA-NORTHEAST1` bucket location: no diff (case-insensitive).
- Omitted `service_account` on run v2: adopts live (default compute SA, proven).
- Plan artifacts: `/tmp/adopt-probe/{bucket,topic,sub,run}/plan.json` (pure no-op ×4) + `/tmp/adopt-probe/bucket/plan_nearline.json` (the deviant). Regeneration: recreate scratch dir per §0.2 header, `tofu init && tofu plan -out=p.plan && tofu show -json p.plan`.

### 0.3 The authoring/PR plumbing the tool reuses (verbatim surfaces)

- `agent/adk_tools.py:692-748` `open_infra_pr_tool(files, title, body)`: `authority = derive_iac_pr_authority(title)` → `worker_client.call_open_infra_pr(target_repo=..., branch=..., title=..., body=..., files=...)` → compact result `{status, pr_number, pr_url, branch, next_steps: "PR opened. " + iac_pr_next_steps(pr_number)}` → `if iac_pr_pointer(compact_result) is not None: notify_iac_pr_pending(pr_number, pr_url, title)`.
- Worker validation order (`workers/tofu_editor/main.py:238-349`): target_repo → base=main → branch `infra/` → file writes (iac/-only, .tf/.md, no foundation, ≤32 files, ≤200KB/file, ≤1MB) → title ≤200 / body ≤20KB → **AGENT-mode static gate in-process** (422, no GitHub side effect) → `tofu fmt` → re-validate sizes → GitHub PR + `driftscribe-infra` label.
- Phase-2 gate import rules the rendered output must satisfy: import+target resource both in changed files; type ∈ `ADOPTABLE_RESOURCE_TYPES`; `id` plain literal matching `ADOPT_IMPORT_ID_SHAPES[rtype]` (no `${`, no `identity` attr); `to` plain `type.name`, target without count/for_each; no for_each on import; ≤1 import block.
- `ADOPT_IMPORT_ID_SHAPES` (in `tools/iac_static_gate.py`): bucket `^[^/\s]+$`; topic `^projects/[^/\s]+/topics/[^/\s]+$`; sub `^projects/[^/\s]+/subscriptions/[^/\s]+$`; run `^projects/[^/\s]+/locations/[^/\s]+/services/[^/\s]+$`. **The import `id` must be a LITERAL — no `${var.project_id}` interpolation** (gate `import-id-not-literal`). So the renderer needs the project as a literal string for ids; resource bodies keep house-style `var.project_id`.
- `iac_hcl.extract_declared_identities` consumes import `id` verbatim (confidence high); derived resource identity resolves `var.project_id` via the variables.tf default `"driftscribe-hack-2026"` — id literal and derived identity coincide iff the runtime project == that default (true in this deployment; a mismatch fails safe at C2 plan: the import 404s or shows changes).

### 0.4 Tool registration pattern (must-mirror)

- `agent/fanout.py:258-284` — `MUTATION_TOOL_NAMES` (symbolic) + `MUTATION_CALLABLE_NAMES` (callable `__name__`); `resolve_provision_read_tools()` drops a tool if EITHER matches. **Both sets must gain the new tool** or slice sub-agents could receive it.
- `agent/adk_agent.py:293-300` — `PROVISION_WORKLOAD_TOOL_NAMES` order-pins `workloads/provision/workload.yaml` exactly; `provision_open_infra_pr` LAST today. Inventory pinned in `tests/unit/test_coordinator_tool_inventory.py` (mutation-set membership + per-workload lists).
- `agent/capabilities.py:326` — capability card derives `write_capable` from `MUTATION_TOOL_NAMES`; the tools list on the card will grow by one ⇒ tool-description map + any count/set pins (backend tests + possibly frontend vitest) must be updated in the same PR.
- Routing: `/chat` with `workload=provision` → `run_provision_fanout_stream` → `decompose()`; 1-slice/non-policy-failure falls back to single-agent `run_chat_stream` (which carries the full provision tool set incl. the new tool). Slices author freehand HCL with NO PR tool — an adopt request that got decomposed would author un-probed HCL ⇒ the decompose instruction must pin "never decompose adoption requests" (Task 5).
- Provision system prompt: `workloads/provision/system_prompt.md` (69 lines today).

### 0.5 What this phase does NOT touch

`tools/iac_static_gate.py`, `driftscribe_lib/iac_plan_denylist.py`, `workers/*` — untouched ⇒ **coordinator rebake only** (no tofu-apply / tofu-editor rebake; the Phase-2 three-rebake rule applies only to gate/denylist changes). `iac/` files — untouched by this PR (the live e2e *after* deploy adopts the probe bucket through the product itself).

---

## §1 Design decisions

1. **Deterministic renderer, not freehand LLM HCL.** The no-op window is delicate: one helpful hallucinated `labels` block (house style has them everywhere!) triggers attribution injection → `importing`+update → refused. The renderer emits exactly the probe-proven bytes; tests pin them and run them through the real static gate.
2. **The tool opens the PR itself** (no files handoff for the LLM to "improve"). It shares the `open_infra_pr_tool` tail via a new private helper `_open_iac_pr_and_notify(files, title, body)` so the two paths cannot drift.
3. **Params per type** (everything else rejected): bucket → `name`, `location`; topic → `name`; subscription → `name`, `topic` (short topic name; full `projects/<P>/topics/<N>` accepted and normalized iff `<P>` == runtime project, else rejected); run service → `name`, `location`, `image`. `project` is **never** a param — pinned server-side from settings (no cross-project adoption).
4. **HCL-injection hardening**: every param validated by strict regex — ALL params ban quotes, `${`, whitespace/newlines, backslash; `/` is additionally banned in `name` and `location` ONLY (`image` legitimately contains `/` `:` `@`; `topic` accepts the one normalized `projects/<P>/topics/<N>` form). After rendering, the import id must `fullmatch` the same per-type shape regexes the gate enforces (drift-pinned in tests), and the rendered file must hcl2-parse and pass `evaluate(GateMode.AGENT)` with zero violations.
5. **Run services and subscriptions require operator input** (`image` / `topic`) — the prompt instructs the agent to ask, never guess. This is the design's honest scope: those facts are not in our CAI read mask.
6. **Honest failure copy** lives in two places: tool rejections (`status: "rejected"` + operator-plain reason, no worker call) and prompt guidance for the post-PR world (if the C2 plan shows changes, the resource deviates from defaults in ways DriftScribe can't read — the operator states the differing settings, the agent regenerates; "this resource can't be cleanly adopted yet" otherwise).
7. **D3 one-adoption-per-PR by construction** — the tool renders exactly one resource + one import block per call.
8. **No `labels`, no provider changes, run trio `ignore_changes = [client, client_version, scaling]`** — straight from the probes (§0.2). The `scaling` ignore matches the managed-service precedent (`checkout_storefront.tf`).
9. **File path** `iac/adopt_<short>_<slug>.tf` (`short` ∈ bucket/topic/subscription/service; `slug` = name with non-alnum → `_`); **address** `<rtype>.adopt_<slug>` — the `adopt_` prefix avoids collisions with existing semantic addresses. Title: `Adopt <human type> <name> into IaC management (zero-change import)`. Deterministic body: what/why, the import id, zero-change framing, create-class C6 re-bake warning, design-doc reference.
10. **Freehand-import guard (Codex round-1 must-fix #1).** Phase 2's gate *admits* a well-formed import block, so `provision_open_infra_pr` and the fan-out's merged-slices editor call could carry LLM-authored import HCL, bypassing the renderer. A coordinator-side pure helper `find_import_violations(files) -> list[str]` (hcl2-parse each `.tf`; any `import` block ⇒ violation; **parse failure ⇒ violation, fail-closed**) runs at BOTH generic authoring sites — `open_infra_pr_tool` and the fan-out merged-files site — returning an LLM-feedback error ("use provision_propose_adoption for adoptions") with NO worker call. Only `propose_adoption_tool` passes `allow_import=True` to the shared tail. This guard lives coordinator-side ONLY — `driftscribe_lib/iac_editor_policy.py` and the tofu-editor worker are NOT touched (the worker must keep accepting the adopt tool's own import PRs, and touching shared editor policy would force a worker rebake).
11. **Main-tree preflight (Codex round-1 must-fix #2).** `ds_github` UPDATES an existing path on the branch (`driftscribe_lib/github.py:315`+), so a slug collision (e.g. `a.b` vs `a-b` → same `a_b`) or a re-adoption would silently rewrite an existing iac file — caught only later at C2 (mixed-plan refusal; fail-safe but bad). Before opening the PR, `propose_adoption_tool` fetches `iac/*.tf` from the target repo's `main` via the coordinator's existing read-capable GitHub client (the one behind `search_recent_prs_tool`, `adk_tools.py:224` — implementer grounds the exact client) and runs a pure lib check `preflight_conflicts(rendering, iac_files, runtime_project) -> str | None`: reject if (a) the rendered path already exists, (b) the rendered address is already declared, (c) the import id is already a declared identity for that asset type (`extract_declared_identities` over the fetched tree), or (d) `variables.tf`'s `project_id` default ≠ `settings.gcp_project` (the §0.3 literal-id/var-body consistency assumption, now pinned at runtime — Codex important #2). **Fetch failure ⇒ fail-closed reject** ("couldn't verify the current IaC tree — try again").

## §2 Behavior matrix (tests pin all of these)

| Call | Outcome |
|---|---|
| bucket, valid name+location | PR opened; rendered file == golden; notify fired |
| topic, valid name | PR opened; golden |
| subscription, name+short topic | PR opened; golden (topic rendered as full-path literal) |
| subscription, full-path topic same project | normalized, golden identical to short-name call |
| subscription, full-path topic OTHER project | rejected, no worker call |
| subscription, missing topic | rejected: "I need the topic this subscription belongs to" |
| run, name+location+image | PR opened; golden incl. lifecycle trio |
| run, missing image | rejected: needs the live container image |
| `google_service_account` / any non-adoptable type | rejected, names the capability-card allowlist |
| any param with `"` `${` newline `\` or whitespace | rejected before render |
| `/` in name or location | rejected (path break-out guard). `/` IS allowed in `image` (`gcr.io/cloudrun/hello`) and in the normalized full-path `topic` input — only quotes/`${`/whitespace/backslash are banned there |
| worker 422/403 | error surfaced as feedback (same as open_infra_pr_tool), no notify |
| freehand `import` block via `open_infra_pr_tool` | rejected coordinator-side ("use provision_propose_adoption"), zero worker calls |
| freehand `import` block in fan-out merged slices | rejected at the merged-files site before the editor call |
| unparseable `.tf` content at either generic site | rejected fail-closed (cannot prove import-free) |
| re-adoption / slug collision / identity already declared | rejected by main-tree preflight with the specific conflict named |
| GitHub tree fetch fails during preflight | rejected fail-closed, retry guidance |
| `variables.tf` `project_id` default ≠ runtime `gcp_project` | rejected (deployment/iac mismatch) |
| rendered output (all 4 types) | hcl2-parses; `evaluate(GateMode.AGENT, [path], {path: content})` == `[]`; import id fullmatches `ADOPT_IMPORT_ID_SHAPES[rtype]`; `extract_declared_identities` yields the import id verbatim (high) AND the derived resource identity equal to it (with variables stub) |
| real probe plan JSONs (4 no-op + 1 deviant) | denylist `evaluate()`: no-op ×4 → `[]`; nearline deviant → exactly `["import-with-changes-forbidden-v1"]` |

---

## Task 1: `driftscribe_lib/adopt_recipe.py` — the renderer

**Files:** Create `driftscribe_lib/adopt_recipe.py`, `tests/unit/test_adopt_recipe.py`.

**Step 1 — failing tests first** (`tests/unit/test_adopt_recipe.py`): golden exact-bytes per type (write the goldens from the code below — they are the probe-proven shapes), the validation matrix rows from §2, the gate-clean test, the identity-consistency test, the id-shape drift pin (`set(ADOPT_IMPORT_ID_SHAPES) == set(SUPPORTED kinds)` and per-type fullmatch).

**Step 2 — implementation:**

```python
"""Deterministic adopt-PR renderer (adopt design Phase 3, docs/plans/2026-06-11-adopt-import-design.md §5).

Renders the resource block + co-located ``import`` block for ONE live
resource, in exactly the shapes the 2026-06-11 fidelity probes proved reach a
pure no-op import plan (docs/plans/2026-06-11-adopt-recipe.md §0.2). The
output is byte-deterministic: no LLM authors adopt HCL. Minimality is
load-bearing — declaring ``labels`` would trigger the provider's
attribution-label injection and break the zero-change promise.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["AdoptRecipeError", "AdoptRendering", "ADOPT_KINDS", "render_adoption"]

ADOPT_KINDS: dict[str, str] = {
    "google_storage_bucket": "bucket",
    "google_pubsub_topic": "topic",
    "google_pubsub_subscription": "subscription",
    "google_cloud_run_v2_service": "service",
}
_HUMAN = {
    "google_storage_bucket": "Cloud Storage bucket",
    "google_pubsub_topic": "Pub/Sub topic",
    "google_pubsub_subscription": "Pub/Sub subscription",
    "google_cloud_run_v2_service": "Cloud Run service",
}

# Param shapes — HCL-injection hardening (§1.4): all params ban quotes,
# interpolation, whitespace, backslash; name/location are additionally
# slash-free (path break-out guard); image allows / : @ (artifact refs).
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~+%-]{0,253}$")
_LOCATION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{1,30}$")
_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{2,511}$")
_PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")


class AdoptRecipeError(ValueError):
    """Operator-plain rejection; the tool surfaces ``str(exc)`` verbatim."""


@dataclass(frozen=True)
class AdoptRendering:
    path: str
    content: str
    address: str
    import_id: str
    title: str
    body: str


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "_", name.lower())


def render_adoption(
    resource_type: str,
    name: str,
    project: str,
    *,
    location: str | None = None,
    topic: str | None = None,
    image: str | None = None,
) -> AdoptRendering:
    if resource_type not in ADOPT_KINDS:
        allowed = ", ".join(sorted(ADOPT_KINDS))
        raise AdoptRecipeError(
            f"{resource_type!r} is not adoptable yet. Adoptable types: {allowed}."
        )
    if not _PROJECT_RE.fullmatch(project):
        raise AdoptRecipeError("internal: invalid runtime project id")
    if not _NAME_RE.fullmatch(name):
        raise AdoptRecipeError(
            f"{name!r} is not a valid {_HUMAN[resource_type]} name."
        )
    ...
```

The four render bodies emit EXACTLY these shapes (canonical `tofu fmt` spacing; goldens in the tests — header comment included):

```hcl
# Adopted into IaC management by DriftScribe (zero-change import).
# The import block is retained as a permanent audit record
# (adopt design 2026-06-11 §3).
resource "google_storage_bucket" "adopt_<slug>" {
  name     = "<name>"
  project  = var.project_id
  location = "<location>"
}

import {
  to = google_storage_bucket.adopt_<slug>
  id = "<name>"
}
```

```hcl
resource "google_pubsub_topic" "adopt_<slug>" {
  project = var.project_id
  name    = "<name>"
}

import {
  to = google_pubsub_topic.adopt_<slug>
  id = "projects/<project>/topics/<name>"
}
```

```hcl
resource "google_pubsub_subscription" "adopt_<slug>" {
  project = var.project_id
  name    = "<name>"
  topic   = "projects/<project>/topics/<topic>"
}

import {
  to = google_pubsub_subscription.adopt_<slug>
  id = "projects/<project>/subscriptions/<name>"
}
```

```hcl
resource "google_cloud_run_v2_service" "adopt_<slug>" {
  name     = "<name>"
  location = "<location>"
  project  = var.project_id

  template {
    containers {
      image = "<image>"
    }
  }

  lifecycle {
    ignore_changes = [client, client_version, scaling]
  }
}

import {
  to = google_cloud_run_v2_service.adopt_<slug>
  id = "projects/<project>/locations/<location>/services/<name>"
}
```

Required/forbidden params per type enforced (bucket: location required, topic/image forbidden; topic: name only; subscription: topic required — full-path input `projects/<P>/topics/<N>` normalized iff `<P> == project` else `AdoptRecipeError` (cross-project); run: location+image required). Title/body composed here (deterministic; body states the zero-change promise, the import id, the C6 create-class re-bake requirement, and that the import block stays as an audit record). Final post-render asserts (defense in depth, raise `AdoptRecipeError` on failure): import id fullmatches the type's shape in `tools.iac_static_gate.ADOPT_IMPORT_ID_SHAPES`… **NO — lib must not import from tools/ (layering).** Instead duplicate the four regexes as `_ID_SHAPES` here and add a drift-pin test asserting `_ID_SHAPES[t].pattern == ADOPT_IMPORT_ID_SHAPES[t].pattern` for all four (test imports both; the lib stays tools-free).

**Step 3 — key tests (complete list):**
- `test_golden_bucket/topic/subscription/run` — exact `content`, `path`, `address`, `import_id`, title.
- `test_subscription_full_path_topic_normalized` / `test_subscription_cross_project_topic_rejected` / `test_missing_required_param_rejected` (parametrized) / `test_forbidden_param_rejected` / `test_non_adoptable_type_rejected_names_allowlist` (incl. `google_service_account`).
- `test_injection_chars_rejected` — parametrized: `"`, `${`, `\n`, `\\`, ` ` in name/location/topic/image; `/` additionally rejected in name and location ONLY (image legitimately contains `/`, e.g. `gcr.io/cloudrun/hello`; topic accepts the one normalized full-path form).
- `test_rendered_output_parses_and_passes_static_gate` — for all 4: `hcl2` parses; `evaluate(GateMode.AGENT, changed_paths=[r.path], hcl_files={r.path: r.content})` returns `[]` (imports from `tools.iac_static_gate` — tests may cross layers).
- `test_rendered_identity_consistency` — `extract_declared_identities({r.path: r.content, "variables.tf": VARIABLES_STUB})` contains the import-id identity at confidence `high` AND a `derived_resource` entry with the SAME identity string (VARIABLES_STUB pins `project_id` default to the `project` arg used).
- `test_id_shapes_match_static_gate` — pattern-equality drift pin (×4) + key-set equality with `ADOPTABLE_RESOURCE_TYPES`.

**Step 4:** `uv run pytest tests/unit/test_adopt_recipe.py -q` green; `uv run ruff check .`; commit `feat: adopt-recipe renderer — probe-proven zero-change HCL (lib)`.

## Task 2: real-fixture extension — all four types proven at the denylist

**Files:** Create `tests/fixtures/iac_plan_denylist/real_import_topic_pure_noop.json`, `real_import_sub_pure_noop.json`, `real_import_run_pure_noop.json`, `real_import_bucket_storage_class_update.json` (copied from `/tmp/adopt-probe/{topic,sub,run}/plan.json` + `/tmp/adopt-probe/bucket/plan_nearline.json`; add a top-level `"_test_intent"` key each, following the existing fixtures' style — do NOT reflow/re-indent the provider JSON, insert the key surgically). Modify `tests/unit/test_iac_plan_denylist.py`.

Tests: parametrize the existing real-fixture pure-no-op test over all four types → `evaluate()` == `[]`; the storage-class deviant → exactly `["import-with-changes-forbidden-v1"]`. If `/tmp` artifacts are gone, regenerate per §0.2 (live read-only plans; bucket deviant = `gcloud storage buckets update --default-storage-class=NEARLINE`, restore STANDARD after).

Commit: `test: provider-real no-op import fixtures for all four adoptable types`.

## Task 3: `propose_adoption_tool` + shared PR tail

**Files:** Modify `agent/adk_tools.py`, `agent/worker_client.py` (only if needed — expected NOT), `tests/unit/test_open_infra_pr_tool.py` (or wherever open_infra_pr tests live), create `tests/unit/test_propose_adoption_tool.py`.

1. Extract the tail of `open_infra_pr_tool` (lines 717-748: authority → call → compact result → notify) into `_open_iac_pr_and_notify(files: list[dict], title: str, body: str) -> dict`; `open_infra_pr_tool` becomes a thin wrapper. **Existing open_infra_pr tests must pass unchanged.**
2. New tool:

```python
def propose_adoption_tool(
    resource_type: str,
    name: str,
    location: str = "",
    topic: str = "",
    image: str = "",
) -> dict:
    """Adopt ONE existing live resource into IaC management (zero-change import).

    Renders the probe-proven minimal resource block + co-located import block
    deterministically (driftscribe_lib.adopt_recipe — the LLM never authors
    adopt HCL) and opens the PR through the same tofu-editor path as
    provision_open_infra_pr. One resource per PR (design D3). The import id
    and HCL shape are pre-validated against the same rules the static gate
    enforces; the C2 plan must still show a pure no-op import or the
    denylist refuses it (D1 — enforced, never assumed).
    """
    s = get_settings()
    try:
        r = render_adoption(
            resource_type,
            name,
            s.gcp_project,
            location=location or None,
            topic=topic or None,
            image=image or None,
        )
    except AdoptRecipeError as exc:
        return {"status": "rejected", "reason": str(exc)}
    result = _open_iac_pr_and_notify(
        [{"path": r.path, "content": r.content}], r.title, r.body
    )
    if result.get("pr_number"):
        result["next_steps"] = (
            result.get("next_steps", "")
            + " NOTE: an adoption is create-class — after approval and merge,"
            " the apply worker must be RE-BAKED (C6) before the import can"
            " apply. Applying it changes NOTHING in the cloud; it only"
            " records the resource in IaC state."
        )
    return result
```

(Verified: `agent/config.py:21` — `Settings.gcp_project: str = ""`; `get_settings()` is already imported in `adk_tools.py`. An empty `gcp_project` fails the renderer's `_PROJECT_RE` → clean `rejected` status, fail-safe.)

3. **Freehand-import guard (§1.10):** pure helper in `driftscribe_lib/adopt_recipe.py` — `find_import_violations(files: list[dict]) -> list[str]` (per `.tf` file: hcl2-parse via `iac_hcl.parse_hcl`; parse failure → `"<path>: does not parse as HCL"`; any `import` block via `iter_blocks` → `"<path>: contains an import block"`; `.md` files skipped). Wire it: (a) `open_infra_pr_tool` calls it FIRST and returns `{"status": "rejected", "reason": "... adoptions must go through provision_propose_adoption ..."}` on any violation, zero worker calls; (b) the fan-out merged-files site (`agent/fanout.py`, where `AuthorResult.files` is finalized before the editor call) treats violations as a POLICY failure (fail-closed, same path as `validate_file_writes` failures — no editor call, surfaced error). `_open_iac_pr_and_notify` itself stays guard-free; `propose_adoption_tool` is the only caller that may submit an import block. Do NOT touch `driftscribe_lib/iac_editor_policy.py` or any `workers/` file — the worker must keep accepting the adopt tool's import PRs and the coordinator-only-rebake story depends on it.
4. **Main-tree preflight (§1.11):** pure lib check `preflight_conflicts(rendering: AdoptRendering, iac_files: dict[str, str], runtime_project: str) -> str | None` in `adopt_recipe.py` (path / address / `(asset_type, identity)` / `variables.tf` `project_id`-default checks; the rtype→CAI-asset-type map duplicated locally with a drift-pin test against `iac_hcl._SUPPORTED_RESOURCE_ASSET_TYPES`). **Parse errors in the fetched tree fail closed** (Codex round-2 nit): `extract_declared_identities` returns `parse_errors` — if non-empty, reject (collision checks would be incomplete otherwise). The tool fetches `iac/*.tf`@`main` from `derive_iac_pr_authority`'s target repo using the coordinator's existing read-capable GitHub client (ground it from `search_recent_prs_tool`); ANY fetch exception → `rejected` fail-closed. Preflight runs after render, before `_open_iac_pr_and_notify`.
5. Tests: happy path (mock `worker_client.call_open_infra_pr` + notifier + tree fetch) → worker payload contains exactly the rendered file/title/body, notify fired once; `rejected` paths (bad type, missing image/topic) → **zero** worker calls and **zero** tree fetches, reason text operator-plain; guard rows from §2 (freehand import via open_infra_pr_tool; fanout merged-files; unparseable file fail-closed); preflight rows (path/address/identity collision against a fixture tree containing a prior adoption; project-default mismatch; fetch-exception fail-closed); worker error propagates like open_infra_pr (no notify); next_steps carries the create-class re-bake note; D3: payload always has exactly 1 file.

Commit: `feat: propose_adoption coordinator tool — shared iac-PR tail, freehand-import guard, main-tree preflight`.

## Task 4: registration — mutation sets, workload, capability card

**Files:** Modify `agent/fanout.py` (both frozensets), `agent/adk_agent.py` (`PROVISION_WORKLOAD_TOOL_NAMES`), **`agent/workloads/registry.py` `_TOOL_REGISTRY`** (`"provision_propose_adoption": propose_adoption_tool` — without this `load_workload("provision")` fails on the YAML reference; Codex round-1 must-fix #3), `workloads/provision/workload.yaml` (tools list — append `provision_propose_adoption` LAST, mirroring the tuple), `agent/capabilities.py` (tool-description map entry; operator-plain: "Adopt an existing resource into IaC management via a zero-change import PR — renders the config deterministically; cannot modify live infrastructure"), **frontend: `frontend/src/lib/labels.ts`** (keyed on the CALLABLE name — add `propose_adoption_tool: 'Adopt resource (import PR)'` next to `open_infra_pr_tool`) and **`frontend/tests/unit/CapabilityCard.test.ts`** (the provision fixture's `tools:` array gains the new write-capable tool so the fixture mirrors the live DTO), plus every pinned test: `tests/unit/test_coordinator_tool_inventory.py` (mutation membership, workload lists, read-tool-strip disjointness), backend capabilities pins.

Tests to add: `resolve_provision_read_tools()` result contains NEITHER `provision_propose_adoption` NOR a callable named `propose_adoption_tool` (mirrors the existing double-filter test for open_infra_pr).

Commit: `feat: register provision_propose_adoption (mutation-set + workload + capability card)`.

## Task 5: prompts — provision adopt guidance + decompose no-split rule

**Files:** Modify `workloads/provision/system_prompt.md`, `agent/fanout.py` (decompose instruction), any byte-pin tests on those prompts.

Provision prompt — insert after the `provision_open_infra_pr` section:

```
Adopting existing resources (zero-change import):
- When the operator asks to ADOPT / bring an existing live resource under
  IaC management, use provision_propose_adoption — NEVER author adopt HCL
  yourself and NEVER use provision_open_infra_pr for adoptions. The tool
  renders the exact config proven to import with zero changes.
- Adoptable types are exactly: Cloud Storage bucket, Pub/Sub topic, Pub/Sub
  subscription, Cloud Run service. Anything else: explain it is not yet
  adoptable.
- Check read_project_inventory first: adopt only resources labeled NOT
  declared-in-IaC. Required facts you must have (ask the operator if you
  cannot read them): bucket → location; subscription → its topic; Cloud Run
  service → location AND the exact container image it runs. Do NOT guess a
  topic or image — ask.
- An adoption changes NOTHING in the cloud: the plan must show a pure
  no-op import or the pipeline refuses it. Tell the operator this plainly.
- If the C2 plan later shows changes, the resource's live settings deviate
  from defaults in ways DriftScribe cannot read (for example a non-default
  storage class). Say "this resource can't be cleanly adopted yet", ask the
  operator for the differing settings shown on the approval page, and only
  then regenerate. One resource per adoption PR.
```

Decompose instruction (in `agent/fanout.py`, the decompose sub-agent prompt): add one rule — "An ADOPTION request (bringing an existing live resource under IaC management / importing) is NEVER decomposed: always return exactly ONE slice for it" (single slice ⇒ orchestrator falls back to the single agent, which holds the adopt tool). Pin the rule's presence with a text-content test on the instruction constant (LLM behavior itself is untestable in unit tests — the SAFETY backstop is the Task-3 merged-files import guard, which is fully tested; this instruction is UX, the guard is the invariant).

Commit: `feat: adopt guidance in provision prompt + decompose no-split rule`.

## Task 6: full gates + ship

1. `uv run pytest -q` (expect ~2500+), `uv run ruff check .`, `cd frontend && npm run test:unit -- --run && npm run check && npm run build` (frontend changes expected zero-or-pin-only).
2. Branch `feat/adopt-recipe`, PR with the §0.2 probe table in the body; CI green on final head; Opus whole-branch review; Codex completed-work (same Phase-3 thread); squash-merge.
3. **Deploy: coordinator ONLY** — `gcloud builds submit --config=infra/cloudbuild.coordinator-update.yaml --substitutions=_TAG=<short-sha>` then poll new revision ready then **mandatory** `gcloud run services update-traffic driftscribe-agent --to-revisions=<new>=100 --region=asia-northeast1`. No worker rebakes (assert in review: no `tools/`, `driftscribe_lib/iac_plan_denylist.py`, or `workers/` diffs except tests).
4. Live verify: `/capabilities` lists `provision_propose_adoption` (write_capable true).
5. **Live e2e (Phase-3 exit, ahead of the design's Phase-4 button e2e):** `/chat` workload=provision: "Adopt the Cloud Storage bucket driftscribe-hack-2026-adopt-probe into IaC management" → PR opens with the exact golden bytes → C2 dispatch → plan shows pure no-op import, denylist `[]`, approval page zero-change framing → approve → merge → re-bake tofu-apply (C6, at the new main SHA) → apply → verify: state contains the address, CAI describe before/after identical, `/describe` flips the bucket to declared (managed 7→8). The probe topic/sub/run service stay live unmanaged — they are Phase-4's demo material.
6. Memory update (`clickops_audience_initiative.md` + index line).

---

## Probe/cleanup notes

- Live probe resources kept deliberately: bucket (gets adopted in the e2e), topic + sub + run svc (Phase-4 Adopt-button demo nodes; scale-to-zero/no-traffic ≈ $0).
- `/tmp/adopt-probe/` scratch dirs hold the plan JSONs for Task 2 — copy before any reboot.

## Plan-review record (Codex)

Thread `019eb4c2-2281-73e1-8152-e65bba84e599`. Round 1: **NO-GO**, 4 must-fix + 3 important — all verified against code and folded:

1. **Renderer bypass** — Phase 2's gate admits a well-formed freehand import via `provision_open_infra_pr` or the fan-out editor call → §1.10 coordinator-side freehand-import guard at both generic sites (fail-closed on parse errors), `allow_import` reserved to the adopt tool; worker/editor-policy untouched.
2. **Path/address collision unsafe** — `ds_github` updates existing paths (`github.py:315`+); same-slug names (`a.b`/`a-b`) or re-adoption would rewrite a prior adopt file → §1.11 main-tree preflight (path/address/identity/project-default), fail-closed on fetch error.
3. **`_TOOL_REGISTRY` missing** (`agent/workloads/registry.py:343`) — without it `load_workload("provision")` fails on the YAML reference → folded into Task 4.
4. **Image-slash contradiction** — §2 banned `/` in all params while the happy-path image is `gcr.io/cloudrun/hello` → matrix split: `/` banned in name/location only; image/topic ban quotes/`${`/whitespace/backslash.

Importants folded: frontend fallout is definite (`labels.ts` callable-name label + `CapabilityCard.test.ts` provision fixture) → Task 4; `gcp_project` vs `variables.tf` `project_id` default pinned at runtime as preflight check (d); decompose no-split rule backed by an instruction-text pin + the tested merged-files guard as the real invariant.

Round 2: **GO**. One important folded (stale §1.4 prose + skeleton comment still said slash-free-for-all-params — aligned with the corrected matrix); one nit folded (preflight fails closed when fetched main-tree files don't parse — `parse_errors` non-empty ⇒ reject). Coordinator-only rebake confirmed valid by Codex.
