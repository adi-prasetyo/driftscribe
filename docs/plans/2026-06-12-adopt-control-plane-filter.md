# Adopt-Guide Control-Plane Filter Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stop every adoption surface (panel adopt list, Start-here pick, onboarding-tour step 4, chat) from suggesting or accepting DriftScribe's OWN control-plane resources — whose adoption the denylist is guaranteed to refuse — by flagging those nodes server-side with the denylist's own identity constants and refusing them deterministically at the adoption-tool boundary.

**Architecture:** `build_graph` adds an additive, only-when-true `control_plane: true` flag to nodes whose (asset type, name) matches a denylist control-plane identity rule; the three client adopt surfaces suppress the CTA for flagged nodes with an honest note; `render_adoption` (the single choke point — the fanout path entry-delegates adoption to the same `propose_adoption_tool` path) rejects control-plane identities with an honest reason; the two workload prompts gain a pinned canonical sentence. No denylist change, no inventory change — coordinator rebake only.

**Tech Stack:** Python (driftscribe_lib + pytest), TypeScript/Svelte 5 (vitest + @testing-library/svelte), static prompt .md files with lib-pinned copy.

---

## 1. The papercut (found live, item-14 tour verify)

On prod, the guided-adoption rank-1 suggestion — surfaced by the panel's **Start here** chip AND mirrored by the tour's step-4 prefill — is `driftscribe-hack-2026-tofu-artifacts`: **DriftScribe's own plan-artifact bucket**. Codex (thread 019eb76d) agreed this is a separate backlog item for adoption ranking/filtering.

This is worse than an odd ranking. The denylist **hard-refuses importing control-plane identities**: in `evaluate` (driftscribe_lib/iac_plan_denylist.py:796) the identity rules run `if _is_mutation(actions) or importing is not None` — a pure zero-change import of a `-tofu-artifacts` bucket emits `control-plane-bucket`, and an import of the `driftscribe-agent` service emits `control-plane-service`. So the Adopt button on those nodes is a **guaranteed dead-end CTA**: operator clicks → agent authors the PR → C2 evaluation blocks it. For the ClickOps audience (building trust in the agent), the flagship "start here" suggestion leading to a guaranteed refusal is exactly the wrong first experience.

## 2. Design decisions

1. **Filter, not deprioritize.** The denylist already answers the question: these adoptions can never succeed, so the UI must not offer them. Deprioritizing a guaranteed refusal would still leave a dead-end button.
2. **The node stays on the map.** It IS unmanaged drift; hiding it would misreport the estate. Only the adopt *affordance* is suppressed, replaced by an honest note.
3. **Parity-by-construction.** The flag is computed from the denylist's own public constants (`CONTROL_PLANE_BUCKET_SUFFIXES`, `CONTROL_PLANE_SERVICE_NAMES`) with the same matching semantics as the rules (`str.endswith(suffixes)` == `_is_protected_bucket_name`; name-in-set == `_check_control_plane_service`). A parity test drives `build_graph` and `evaluate` with the same identity and asserts flag ⟺ blocked. Only Bucket and Run Service get matchers — **Pub/Sub has no control-plane identity rule**, so its nodes are never flagged (their import is admitted; flagging them would be the dishonest direction).
4. **Failure direction is safe.** A stale coordinator response without the field → the button shows → C2 still blocks the plan (annoying, never dangerous). A name the constants miss → same. A false positive is impossible without the denylist also refusing that identity (same constants, same semantics).
5. **Safety framing is CORRECT here.** Item 10's rule (confidence-framing, never safety) governs the adoption-ORDER copy. This note states what the always-on gate actually does — like the capability card's rule descriptions, it is a gate fact, and tests pin it as such. **Precision (Codex 019eb932 MF1):** the denylist refuses plans that would CHANGE OR IMPORT a control-plane identity — a plain no-op row on one passes (`noop_control_plane_service_pass` fixture). All new copy says "change or import", never "any plan that touches".
6. **Deterministic tool-boundary refusal (Codex 019eb932 MF2).** Prompts alone don't guarantee chat refuses: `provision_propose_adoption` would happily render the recipe and the refusal would only land at C2, after a PR exists. `render_adoption` in `driftscribe_lib/adopt_recipe.py` is the single choke point (adoption is routed to the single-agent `provision_propose_adoption` path — fanout entry-delegates rather than calling the editor directly, the item-11 lesson — and that path calls `render_adoption`) and already maps `AdoptRecipeError` → an honest `{"status": "rejected", "reason": …}`. A control-plane identity check there, using the same constants, makes the refusal immediate and reliable. The rejection reason must be explicit that this is NOT parameter feedback (the tool docstring tells the model rejections are retryable parameter problems — that sentence gets amended too).
7. **No denylist / inventory / worker changes.** `infra_graph.py` and `adopt_recipe.py` already live in `driftscribe_lib` and (with their consumers) ship in the coordinator image; `adopt_recipe`'s consumers are `agent/adk_tools.py` + `agent/fanout.py` only. **Coordinator rebake only** — no tofu-editor, tofu-apply, or infra-reader rebake.

## 3. Out of scope

- Mermaid styling of control-plane nodes on the map (they keep the plain drift color).
- Filtering Google-managed buckets (`*_cloudbuild`, `run-sources-*`, …) — adopting those is legitimate.
- Backend re-ranking of `ADOPTION_GUIDE` (the per-TYPE ranks stay as item 10 shipped them).
- SA / secret / KMS node flags — SAs are not an adoptable type and secrets are sensitive counts-only; no adopt CTA exists to suppress.
- Any change to what the denylist admits.

## 4. Grounding facts (verified at main `8e5114b`)

- `driftscribe_lib/iac_plan_denylist.py:219` — `CONTROL_PLANE_BUCKET_SUFFIXES: tuple = ("-tofu-state", "-tofu-artifacts")`; `:169` — `CONTROL_PLANE_SERVICE_NAMES: frozenset` (13 names); `:796` — imports run the identity rules; `:108` — `Violation(rule, detail)` dataclass; `:121` — `DenylistInput(plan: dict)`; entry point `evaluate(di)`.
- `driftscribe_lib/infra_graph.py:28` already does `from driftscribe_lib.iac_plan_denylist import ADOPTABLE_RESOURCE_TYPES`; node dicts are built at `:319-339`; only-when-true key precedent: `truncated_in_group` (`:364`).
- Node labels are SHORT names: `infra_inventory.py:94` — `display = norm.rsplit("/", 1)[-1]` — so bucket labels are bucket names and Run labels are service names; matching the constants directly is sound.
- Frontend node affordance today is group-level only: `frontend/src/lib/infra_graph.ts:335` `adoptRows` (helper, test-covered), `frontend/src/components/InfraDiagram.svelte:129-171` `adoptGroups` + `startHereAssetType`, `frontend/src/lib/tour.ts:139` `adoptStepState`.
- Prompt pins: `tests/unit/test_adoption_order_prompts.py` whitespace-normalizes `workloads/{explore,provision}/system_prompt.md` and asserts the lib constants verbatim — the new note follows the same pattern.
- Real provider-generated fixtures exist under `tests/fixtures/iac_plan_denylist/`: `import_control_plane_state_bucket.json` (pure no-op import of bucket `driftscribe-hack-2026-tofu-state` — blocked), `real_import_bucket_pure_noop.json` (bucket `driftscribe-hack-2026-c6e-probe` — admitted), `real_import_run_pure_noop.json` (service `adopt-probe-svc` — admitted). The parity test runs against these REAL plans as well as the synthetic ones (Codex 019eb932).
- `agent/adk_tools.py:865` — `except AdoptRecipeError as exc: return {"status": "rejected", "reason": str(exc)}`; `adopt_recipe.py` currently imports only stdlib, and its `render_adoption` validates names via `_validate_name` before the type-specific branches — the guard slots in right after.
- Live estate check: prod unmanaged buckets include `driftscribe-hack-2026-tofu-artifacts` and `-tofu-state` (now suppressed) AND non-control-plane buckets (e.g. `driftscribe-hack-2026_cloudbuild`), so a real rank-1 suggestion remains after the filter; unmanaged Run services are all `driftscribe-*` control-plane, so the Run group keeps rows but loses its dead-end buttons.

---

### Task 1: backend `control_plane` node flag

**Files:**
- Modify: `driftscribe_lib/infra_graph.py`
- Test: `tests/unit/test_infra_graph.py`

**Step 1: Write the failing tests** — append to `tests/unit/test_infra_graph.py`:

```python
# --------------------------------------------------------------------------- #
# Control-plane adopt suppression (2026-06-12 ranking-filter follow-up to the
# item-14 tour): nodes whose identity the denylist's control-plane rules would
# refuse to import carry `control_plane: True` so adopt surfaces suppress the
# guaranteed-dead-end CTA. PARITY: same public constants, same semantics as
# the denylist; the parity test below drives both libraries with the same
# identity and asserts flag ⟺ import-blocked.
# --------------------------------------------------------------------------- #


def _one_node_inventory(atype: str, name: str) -> dict:
    return _inventory(
        total_resources=1,
        declared_in_iac=0,
        not_in_iac=1,
        by_type={
            atype: {
                "count": 1,
                "declared_in_iac": 0,
                "not_in_iac": 1,
                "sensitive": False,
                "sample": [
                    {"name": name, "location": "asia-northeast1", "iac": False,
                     "match_confidence": None},
                ],
            },
        },
    )


def _single_import_plan(rtype: str, attrs: dict) -> dict:
    """A minimal plan.json: ONE pure (no-op) import of the given identity."""
    return {
        "format_version": "1.2",
        "resource_changes": [
            {
                "address": f"{rtype}.adopt",
                "type": rtype,
                "name": "adopt",
                "change": {
                    "actions": ["no-op"],
                    "before": attrs,
                    "after": attrs,
                    "importing": {"id": "whatever"},
                },
            },
        ],
    }


class TestControlPlaneNodeFlag:
    def test_protected_bucket_node_is_flagged(self):
        g = build_graph(_one_node_inventory(BUCKET_TYPE, "acme-prod-tofu-artifacts"))
        assert g["groups"][0]["nodes"][0]["control_plane"] is True

    def test_state_bucket_suffix_also_flagged(self):
        g = build_graph(_one_node_inventory(BUCKET_TYPE, "acme-prod-tofu-state"))
        assert g["groups"][0]["nodes"][0]["control_plane"] is True

    def test_ordinary_bucket_node_carries_no_key(self):
        # Only-when-true (truncated_in_group style): non-control-plane graphs
        # stay byte-identical to the pre-flag era.
        g = build_graph(_one_node_inventory(BUCKET_TYPE, "acme-assets"))
        assert "control_plane" not in g["groups"][0]["nodes"][0]

    def test_control_plane_service_node_is_flagged(self):
        g = build_graph(_one_node_inventory(RUN_TYPE, "driftscribe-agent"))
        assert g["groups"][0]["nodes"][0]["control_plane"] is True

    def test_workload_service_node_carries_no_key(self):
        g = build_graph(_one_node_inventory(RUN_TYPE, "storefront"))
        assert "control_plane" not in g["groups"][0]["nodes"][0]

    def test_type_scoped_a_topic_named_like_a_service_is_not_flagged(self):
        # Name collisions across types must not flag: there is no control-plane
        # Pub/Sub identity rule, so a topic named "driftscribe-agent" is
        # adoptable and its import is admitted.
        g = build_graph(_one_node_inventory(TOPIC_TYPE, "driftscribe-agent"))
        assert "control_plane" not in g["groups"][0]["nodes"][0]

    def test_matchers_cover_only_adoptable_types(self):
        # The flag exists to suppress adopt CTAs; a matcher on a non-adoptable
        # type would be dead code. Exactly Bucket + Run Service have
        # control-plane identity rules among the adoptable four.
        assert set(_CONTROL_PLANE_NODE_MATCHERS) == {
            "storage.googleapis.com/Bucket",
            "run.googleapis.com/Service",
        }
        assert set(_CONTROL_PLANE_NODE_MATCHERS) <= ADOPTABLE_ASSET_TYPES

    @pytest.mark.parametrize(
        ("atype", "rtype", "attrs", "name", "expect_blocked"),
        [
            ("storage.googleapis.com/Bucket", "google_storage_bucket",
             {"name": "acme-prod-tofu-artifacts"}, "acme-prod-tofu-artifacts", True),
            ("storage.googleapis.com/Bucket", "google_storage_bucket",
             {"name": "acme-prod-tofu-state"}, "acme-prod-tofu-state", True),
            ("storage.googleapis.com/Bucket", "google_storage_bucket",
             {"name": "acme-assets"}, "acme-assets", False),
            ("run.googleapis.com/Service", "google_cloud_run_v2_service",
             {"name": "driftscribe-agent"}, "driftscribe-agent", True),
            ("run.googleapis.com/Service", "google_cloud_run_v2_service",
             {"name": "storefront"}, "storefront", False),
        ],
    )
    def test_flag_parity_with_denylist_import_admission(
        self, atype, rtype, attrs, name, expect_blocked
    ):
        # THE invariant this feature rests on: the node is flagged exactly when
        # a pure single import of that identity is denylist-blocked by a
        # control-plane rule. Drives both libraries end-to-end via their
        # public surfaces.
        g = build_graph(_one_node_inventory(atype, name))
        flagged = g["groups"][0]["nodes"][0].get("control_plane") is True

        violations = evaluate(DenylistInput(plan=_single_import_plan(rtype, attrs)))
        blocked = any(v.rule.startswith("control-plane-") for v in violations)

        assert flagged is expect_blocked
        assert blocked is expect_blocked
        # A pure import of a NON-control-plane adoptable identity must be
        # fully admitted — no other rule may fire either.
        if not expect_blocked:
            assert violations == []

    @pytest.mark.parametrize(
        ("fixture", "atype", "expect_blocked"),
        [
            # REAL provider-generated plans (Codex 019eb932): better pins of
            # provider attribute shape than the synthetic ones above.
            ("import_control_plane_state_bucket.json",
             "storage.googleapis.com/Bucket", True),
            ("real_import_bucket_pure_noop.json",
             "storage.googleapis.com/Bucket", False),
            ("real_import_run_pure_noop.json",
             "run.googleapis.com/Service", False),
        ],
    )
    def test_flag_parity_on_real_import_fixtures(self, fixture, atype, expect_blocked):
        plan = json.loads(
            (FIXTURES_DENYLIST / fixture).read_text(encoding="utf-8")
        )
        # Identity name as the provider emitted it — the graph node label for
        # the same live resource (infra_inventory uses the short name).
        rc = plan["resource_changes"][0]
        name = rc["change"]["after"]["name"]

        g = build_graph(_one_node_inventory(atype, name))
        flagged = g["groups"][0]["nodes"][0].get("control_plane") is True
        blocked = any(
            v.rule.startswith("control-plane-")
            for v in evaluate(DenylistInput(plan=plan))
        )
        assert flagged is expect_blocked
        assert blocked is expect_blocked

    def test_managed_control_plane_node_still_flagged(self):
        # The flag describes IDENTITY, not adoptability — a (hypothetically)
        # already-managed control-plane node keeps it; clients only consult it
        # on unmanaged rows anyway.
        inv = _one_node_inventory(BUCKET_TYPE, "acme-prod-tofu-state")
        inv["by_type"][BUCKET_TYPE]["sample"][0]["iac"] = True
        inv["by_type"][BUCKET_TYPE]["declared_in_iac"] = 1
        inv["by_type"][BUCKET_TYPE]["not_in_iac"] = 0
        g = build_graph(inv)
        assert g["groups"][0]["nodes"][0]["control_plane"] is True
```

Add to the test file's imports (top of file):

```python
import json
from pathlib import Path

from driftscribe_lib.iac_plan_denylist import DenylistInput, evaluate
from driftscribe_lib.infra_graph import _CONTROL_PLANE_NODE_MATCHERS

FIXTURES_DENYLIST = (
    Path(__file__).resolve().parents[1] / "fixtures" / "iac_plan_denylist"
)
```

(`TOPIC_TYPE` already exists alongside `RUN_TYPE`/`BUCKET_TYPE`/`SECRET_TYPE`; if the constant is named differently, use the file's existing Pub/Sub topic constant. `ADOPTABLE_ASSET_TYPES` and `build_graph` are already imported. If a fixtures-path helper already exists in the test tree, reuse it instead of `FIXTURES_DENYLIST`.)

**Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/unit/test_infra_graph.py -q` (from repo root)
Expected: FAIL — `ImportError: cannot import name '_CONTROL_PLANE_NODE_MATCHERS'`.

**Step 3: Implement** — in `driftscribe_lib/infra_graph.py`:

(a) extend the existing denylist import (line 28):

```python
from driftscribe_lib.iac_plan_denylist import (
    ADOPTABLE_RESOURCE_TYPES,
    CONTROL_PLANE_BUCKET_SUFFIXES,
    CONTROL_PLANE_SERVICE_NAMES,
)
```

(b) add `from collections.abc import Callable` to the imports.

(c) insert after `adoption_order_sentence()` (line 122) and before `SENSITIVE_PLAN_RTYPES`:

```python
# Control-plane adopt suppression (2026-06-12, ranking-filter follow-up found
# live during the item-14 tour verify: the rank-1 "start here" suggestion was
# DriftScribe's OWN -tofu-artifacts bucket). The denylist refuses any plan
# that would CHANGE OR IMPORT a control-plane identity (evaluate runs the
# identity rules `if _is_mutation(actions) or importing is not None`; a plain
# no-op row on one passes) — so an Adopt button on such a node is a
# guaranteed dead end at C2 evaluation. Nodes
# matching a control-plane identity carry `control_plane: True` so every adopt
# surface (panel list, Start-here pick, tour step 4) suppresses the CTA with
# an honest note. The node itself stays on the map: it IS unmanaged drift, and
# hiding it would misreport the estate.
#
# PARITY-BY-CONSTRUCTION: the matchers are the denylist's own public identity
# constants for the two adoptable types that HAVE control-plane rules — bucket
# name suffix (same semantics as _is_protected_bucket_name) and Cloud Run
# service name (CONTROL_PLANE_SERVICE_NAMES). Pub/Sub has no control-plane
# identity rule, so its nodes are never flagged. test_infra_graph pins the
# parity by driving build_graph and evaluate with the same identity. Failure
# direction is safe: an unflagged protected name only shows a button whose
# plan C2 then blocks; a false positive cannot happen without the denylist
# also refusing that same identity.
_CONTROL_PLANE_NODE_MATCHERS: dict[str, Callable[[str], bool]] = {
    "storage.googleapis.com/Bucket": lambda name: name.endswith(
        CONTROL_PLANE_BUCKET_SUFFIXES
    ),
    "run.googleapis.com/Service": lambda name: name in CONTROL_PLANE_SERVICE_NAMES,
}


def _is_control_plane_node(atype: str, label: str) -> bool:
    """True iff a node of ``atype`` named ``label`` is DriftScribe control plane."""
    matcher = _CONTROL_PLANE_NODE_MATCHERS.get(atype)
    return bool(matcher is not None and label and matcher(label))
```

(d) in the `build_graph` node loop, replace lines 330-339:

```python
                    # dict.get's default only fires on a MISSING key; a present
                    # name=None would otherwise stringify to the literal "None".
                    name = sample.get("name")
                    label = str(name) if name is not None else ""
                    node = {
                        "id": f"g{gi}n{ni}",
                        "label": label,
                        "asset_type": atype,
                        "managed": bool(sample.get("iac")),
                        "location": location if isinstance(location, str) else None,
                    }
                    if _is_control_plane_node(atype, label):
                        # Only-when-true (truncated_in_group style) so every
                        # non-control-plane graph stays byte-identical.
                        node["control_plane"] = True
                    nodes.append(node)
```

(e) update `build_graph`'s docstring DTO sketch (Codex nit): the nodes line becomes
`nodes: [ {id, label, asset_type, managed, location, control_plane?} ]`.

**Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/unit/test_infra_graph.py -q`
Expected: PASS (all, including the pre-existing ones — the flag is additive).

**Step 5: Commit**

```bash
git add driftscribe_lib/infra_graph.py tests/unit/test_infra_graph.py
git commit -m "feat(graph): flag control-plane nodes whose adoption the denylist refuses"
```

---

### Task 2: canonical prompt note + workload prompt edits

**Files:**
- Modify: `driftscribe_lib/infra_graph.py` (one constant)
- Modify: `workloads/provision/system_prompt.md`, `workloads/explore/system_prompt.md`
- Test: `tests/unit/test_adoption_order_prompts.py`

**Step 1: Write the failing test** — append to `tests/unit/test_adoption_order_prompts.py` (and extend its lib import):

```python
from driftscribe_lib.infra_graph import (
    ADOPTION_CONTROL_PLANE_NOTE,
    ADOPTION_ORDER_HONESTY,
    adoption_order_sentence,
)


@pytest.mark.parametrize("workload", ["explore", "provision"])
def test_prompt_carries_the_control_plane_note(workload):
    text = _normalized(WORKLOADS / workload / "system_prompt.md")
    assert " ".join(ADOPTION_CONTROL_PLANE_NOTE.split()) in text
```

**Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/unit/test_adoption_order_prompts.py -q`
Expected: FAIL — ImportError on `ADOPTION_CONTROL_PLANE_NOTE`.

**Step 3: Implement.**

(a) in `driftscribe_lib/infra_graph.py`, directly after `ADOPTION_ORDER_HONESTY`:

```python
# Canonical control-plane adoption refusal (same prompt-pin pattern as
# ADOPTION_ORDER_HONESTY — the static .md prompts duplicate it by hand and the
# pin test keeps the duplication safe). Unlike the order hints, this IS safety
# framing — accurately: it states what the always-on gate does, mirroring the
# capability card's rule descriptions. Precision (Codex 019eb932 MF1): the
# gate refuses CHANGES and IMPORTS — a plain no-op on a control-plane
# identity passes — so the copy says "change or import", never "touches".
ADOPTION_CONTROL_PLANE_NOTE = (
    "DriftScribe's own control-plane resources — its Cloud Run services and "
    "the -tofu-state / -tofu-artifacts buckets — cannot be adopted: the "
    "always-on denylist refuses any plan that would change or import them."
)
```

(b) `workloads/provision/system_prompt.md` — add a bullet to the "Adopting existing resources" section (after the `read_project_inventory` bullet around line 60), hard-wrapped to the file's existing width:

```markdown
- DriftScribe's own control-plane resources — its Cloud Run services and the
  -tofu-state / -tofu-artifacts buckets — cannot be adopted: the always-on
  denylist refuses any plan that would change or import them. If the operator
  asks to adopt one, say so plainly and do not call
  provision_propose_adoption for it (it would be rejected with this reason).
```

(c) `workloads/explore/system_prompt.md` — add a bullet to the where-to-start passage (after the adoption-order suggestion around line 88):

```markdown
- DriftScribe's own control-plane resources — its Cloud Run services and the
  -tofu-state / -tofu-artifacts buckets — cannot be adopted: the always-on
  denylist refuses any plan that would change or import them. Never suggest
  one as a first adoption.
```

**Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/unit/test_adoption_order_prompts.py -q`
Expected: PASS.

**Step 5: Commit**

```bash
git add driftscribe_lib/infra_graph.py workloads/provision/system_prompt.md workloads/explore/system_prompt.md tests/unit/test_adoption_order_prompts.py
git commit -m "feat(prompts): pin control-plane adoption refusal into explore + provision"
```

---

### Task 3: deterministic tool-boundary refusal (Codex 019eb932 MF2)

Prompts instruct, but only the tool can guarantee: `render_adoption` is the single choke point: `propose_adoption_tool` (agent/adk_tools.py) calls it, and the provision fanout path entry-delegates adoption to that same single-agent path. And `AdoptRecipeError` already becomes `{"status": "rejected", "reason": str(exc)}` (adk_tools.py:865-866). Reject control-plane identities there, with a reason that is explicitly NOT parameter feedback.

**Files:**
- Modify: `driftscribe_lib/adopt_recipe.py`
- Modify: `agent/adk_tools.py` (one docstring sentence)
- Test: `tests/unit/test_adopt_recipe.py` (the file that already tests `render_adoption` — locate by `grep -rln "render_adoption" tests/`)

**Step 1: Write the failing tests** — append to the existing `render_adoption` test module (match its calling conventions for required params):

```python
class TestControlPlaneRefusal:
    def test_control_plane_bucket_is_rejected_with_explicit_reason(self):
        with pytest.raises(AdoptRecipeError) as ei:
            render_adoption(
                "google_storage_bucket",
                "driftscribe-hack-2026-tofu-artifacts",
                "driftscribe-hack-2026",
                location="asia-northeast1",
            )
        msg = str(ei.value)
        assert "cannot be adopted" in msg
        assert "denylist" in msg
        # Explicitly NOT parameter feedback — the model must not retry.
        assert "not a parameter problem" in msg

    def test_state_bucket_suffix_also_rejected(self):
        with pytest.raises(AdoptRecipeError):
            render_adoption(
                "google_storage_bucket",
                "acme-prod-tofu-state",
                "driftscribe-hack-2026",
                location="asia-northeast1",
            )

    def test_control_plane_service_is_rejected(self):
        with pytest.raises(AdoptRecipeError) as ei:
            render_adoption(
                "google_cloud_run_v2_service",
                "driftscribe-agent",
                "driftscribe-hack-2026",
                location="asia-northeast1",
                image="gcr.io/x/y:z",
            )
        msg = str(ei.value)
        assert "cannot be adopted" in msg
        assert "not a parameter problem" in msg

    def test_topic_named_like_a_service_still_renders(self):
        # Type-scoped, exactly like the denylist: no control-plane Pub/Sub
        # identity rule exists, so this import is admitted — and the recipe
        # must keep rendering it.
        r = render_adoption(
            "google_pubsub_topic", "driftscribe-agent", "driftscribe-hack-2026"
        )
        assert "import" in r.content

    def test_ordinary_bucket_still_renders(self):
        r = render_adoption(
            "google_storage_bucket",
            "acme-assets",
            "driftscribe-hack-2026",
            location="asia-northeast1",
        )
        assert "acme-assets" in r.content
```

(Adapt the `AdoptRendering` attribute names — `r.content` vs `r.files` — to whatever the existing tests in that module assert on.)

**Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/unit/test_adopt_recipe.py -q`
Expected: FAIL — the control-plane cases render instead of raising.

**Step 3: Implement.**

(a) in `driftscribe_lib/adopt_recipe.py`, add the import:

```python
from driftscribe_lib.iac_plan_denylist import (
    CONTROL_PLANE_BUCKET_SUFFIXES,
    CONTROL_PLANE_SERVICE_NAMES,
)
```

(b) add the guard helper (near the other `_validate_*` helpers):

```python
def _reject_control_plane(resource_type: str, name: str) -> None:
    """Refuse control-plane identities at the tool boundary (Codex 019eb932).

    Same identity semantics as the denylist rules that would block the C2
    plan anyway (and as infra_graph's node flag): rejecting HERE means chat
    gets an immediate, honest refusal instead of authoring a PR that is
    guaranteed to be blocked at plan evaluation. Type-scoped exactly like
    the rules — Pub/Sub has no control-plane identity rule, so topics and
    subscriptions are never rejected by name.
    """
    if resource_type == "google_storage_bucket" and name.endswith(
        CONTROL_PLANE_BUCKET_SUFFIXES
    ):
        raise AdoptRecipeError(
            f"{name!r} cannot be adopted: bucket names ending in -tofu-state "
            "or -tofu-artifacts are IaC control-plane infrastructure, and "
            "the always-on denylist refuses any plan that would change or "
            "import them. This is not a parameter problem — do not retry."
        )
    if (
        resource_type == "google_cloud_run_v2_service"
        and name in CONTROL_PLANE_SERVICE_NAMES
    ):
        raise AdoptRecipeError(
            f"{name!r} cannot be adopted: it is one of DriftScribe's own "
            "control-plane services, and the always-on denylist refuses any "
            "plan that would change or import it. This is not a parameter "
            "problem — do not retry."
        )
```

(c) call it in `render_adoption`, immediately after `_validate_name(name, _HUMAN[resource_type] + " name")`:

```python
    _validate_name(name, _HUMAN[resource_type] + " name")
    _reject_control_plane(resource_type, name)
```

(d) in `agent/adk_tools.py`, amend the `propose_adoption_tool` docstring sentence

> On a ``{"status": "rejected"}`` result, read the ``reason`` and retry with corrected parameters — a rejection is parameter feedback, not a product limitation, unless the reason says the TYPE is not adoptable.

to:

> On a ``{"status": "rejected"}`` result, read the ``reason`` and retry with corrected parameters — a rejection is parameter feedback, not a product limitation, unless the reason says the TYPE is not adoptable or the resource is control-plane infrastructure (those are final — relay the reason, do not retry).

**Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/unit/test_adopt_recipe.py tests/unit/test_adk_tools.py -q` (include whichever module covers `propose_adoption_tool`)
Expected: PASS.

**Step 5: Commit**

```bash
git add driftscribe_lib/adopt_recipe.py agent/adk_tools.py tests/unit/test_adopt_recipe.py
git commit -m "feat(adopt): refuse control-plane identities at the tool boundary"
```

---

### Task 4: frontend lib — node type + `adoptRows`

**Files:**
- Modify: `frontend/src/lib/infra_graph.ts`
- Test: `frontend/tests/unit/infra_graph.test.ts`

**Step 1: Write the failing tests** — append to the adopt-rows describe block in `frontend/tests/unit/infra_graph.test.ts` (reuse the file's existing graph/group fixture helpers; the shape below shows intent):

```ts
it('control-plane node in an adoptable group is a non-adoptable row with the flag', () => {
  const g = graphWith({
    asset_type: 'storage.googleapis.com/Bucket',
    label: 'Storage bucket',
    adoptable: true,
    nodes: [
      { id: 'g0n0', label: 'acme-tofu-artifacts', asset_type: 'storage.googleapis.com/Bucket', managed: false, location: null, control_plane: true },
      { id: 'g0n1', label: 'acme-assets', asset_type: 'storage.googleapis.com/Bucket', managed: false, location: null },
    ],
  });
  const rows = adoptRows(g);
  expect(rows[0]).toMatchObject({ adoptable: false, controlPlane: true, prefill: '' });
  expect(rows[1]).toMatchObject({ adoptable: true, controlPlane: false });
  expect(rows[1].prefill).toContain('`acme-assets`');
});

it('missing control_plane field (stale coordinator) keeps the row adoptable — fail-safe, C2 still blocks', () => {
  const g = graphWith({
    adoptable: true,
    nodes: [{ id: 'g0n0', label: 'x', asset_type: 'storage.googleapis.com/Bucket', managed: false, location: null }],
  });
  expect(adoptRows(g)[0].adoptable).toBe(true);
  expect(adoptRows(g)[0].controlPlane).toBe(false);
});
```

**Step 2: Run to verify failure**

Run: `cd frontend && npm run test:unit -- infra_graph`
Expected: FAIL — `controlPlane` missing from rows / TS error on `control_plane`.

**Step 3: Implement** — in `frontend/src/lib/infra_graph.ts`:

(a) extend `InfraNode`:

```ts
  location: string | null;
  /**
   * Server-marked: DriftScribe's own control-plane infrastructure (its Cloud
   * Run services / the -tofu-state and -tofu-artifacts buckets). The
   * always-on denylist refuses any plan that would change or import it, so
   * adopt surfaces suppress the CTA. Optional + fail-safe: a stale
   * coordinator response without the field shows the button and C2 still
   * blocks the plan.
   */
  control_plane?: boolean;
```

(b) extend `AdoptRow`:

```ts
export interface AdoptRow {
  nodeId: string;
  groupLabel: string;
  nodeLabel: string;
  adoptable: boolean;
  /** IaC control-plane infrastructure — denylist-refused, so never adoptable. */
  controlPlane: boolean;
  /** Chat prefill — composed ONLY for adoptable rows, else ''. */
  prefill: string;
}
```

(c) rework `adoptRows`:

```ts
export function adoptRows(graph: InfraGraph): AdoptRow[] {
  const rows: AdoptRow[] = [];
  for (const g of graph.groups) {
    if (g.sensitive) continue;
    const groupAdoptable = g.adoptable === true;
    for (const n of g.nodes) {
      if (n.managed) continue;
      const controlPlane = n.control_plane === true;
      const adoptable = groupAdoptable && !controlPlane;
      rows.push({
        nodeId: n.id,
        groupLabel: g.label,
        nodeLabel: n.label,
        adoptable,
        controlPlane,
        prefill: adoptable ? adoptPrefill(g.label, n.label, n.location) : '',
      });
    }
  }
  return rows;
}
```

**Step 4: Run to verify pass**

Run: `cd frontend && npm run test:unit -- infra_graph`
Expected: PASS.

**Step 5: Commit**

```bash
git add frontend/src/lib/infra_graph.ts frontend/tests/unit/infra_graph.test.ts
git commit -m "feat(ui-lib): control-plane rows are never adoptable in adoptRows"
```

---

### Task 5: InfraDiagram — suppress button, honest note, Start-here fix

**Files:**
- Modify: `frontend/src/components/InfraDiagram.svelte`
- Test: `frontend/tests/unit/InfraDiagram.test.ts`

**Step 1: Write the failing tests** — append to `frontend/tests/unit/InfraDiagram.test.ts`, reusing `liveGraph()`'s style (build a graph where the rank-1 bucket group's first node is control-plane):

```ts
function adoptGraphWithControlPlane(): InfraGraph {
  return {
    generated_at: null,
    project: 'demo',
    caveat: 'test caveat',
    degraded: false,
    degraded_reason: null,
    totals: { resources: 3, managed: 0, drift: 3 },
    groups: [
      {
        asset_type: 'storage.googleapis.com/Bucket',
        label: 'Storage bucket',
        count: 2, managed: 0, drift: 2, sensitive: false,
        adoptable: true, adopt_rank: 1, adopt_hint: 'a simple leaf resource',
        nodes: [
          { id: 'g0n0', label: 'demo-tofu-artifacts', asset_type: 'storage.googleapis.com/Bucket', managed: false, location: null, control_plane: true },
          { id: 'g0n1', label: 'demo-assets', asset_type: 'storage.googleapis.com/Bucket', managed: false, location: null },
        ],
      },
      {
        asset_type: 'pubsub.googleapis.com/Topic',
        label: 'Pub/Sub topic',
        count: 1, managed: 0, drift: 1, sensitive: false,
        adoptable: true, adopt_rank: 2, adopt_hint: 'small and quick to review',
        nodes: [{ id: 'g1n0', label: 'orders', asset_type: 'pubsub.googleapis.com/Topic', managed: false, location: null }],
      },
    ],
    edges: [],
  };
}

it('control-plane row shows the denylist note instead of an Adopt button', async () => {
  // render with adoptGraphWithControlPlane(), open the panel as existing tests do
  const note = screen.getByTestId('adopt-control-plane');
  // Codex nit: never claim "DriftScribe's own" for ANY suffix-matched bucket —
  // an unrelated `acme-tofu-state` would be refused too. The note names the
  // protection, not ownership.
  expect(note.textContent).toContain('control-plane');
  expect(note.textContent).toContain('denylist');
  // exactly the two non-control-plane rows still get buttons
  expect(screen.getAllByTestId('adopt-btn')).toHaveLength(2);
  // it is NOT the generic "not yet adoptable" note
  expect(screen.queryByTestId('adopt-unavailable')).toBeNull();
});

it('Start here stays on the rank-1 group while it still has an adoptable row', async () => {
  // same graph: bucket group has demo-assets adoptable → chip on buckets
  const chip = screen.getByTestId('adopt-start-here');
  expect(chip.parentElement?.textContent).toContain('Storage bucket');
});

it('a ranked group whose every row is control-plane cannot claim Start here', async () => {
  // graph variant: bucket group has ONLY the control-plane node → chip moves
  // to the rank-2 topic group
  const chip = screen.getByTestId('adopt-start-here');
  expect(chip.parentElement?.textContent).toContain('Pub/Sub topic');
});
```

(Adapt render/open mechanics to the file's existing patterns — `liveGraph` tests already render the component and expand the adopt list.)

**Step 2: Run to verify failure**

Run: `cd frontend && npm run test:unit -- InfraDiagram`
Expected: FAIL — no `adopt-control-plane` testid; chip logic unchanged.

**Step 3: Implement** — in `frontend/src/components/InfraDiagram.svelte`:

(a) extend the local row type (line 116):

```ts
  type AdoptListRow = {
    nodeId: string;
    label: string;
    adoptable: boolean;
    controlPlane: boolean;
    prefill: string;
  };
```

(b) row construction inside `adoptGroups` (lines 136-144):

```ts
      for (const n of g.nodes) {
        if (n.managed) continue;
        const controlPlane = n.control_plane === true;
        const rowAdoptable = adoptable && !controlPlane;
        rows.push({
          nodeId: n.id,
          label: n.label,
          adoptable: rowAdoptable,
          controlPlane,
          prefill: rowAdoptable ? adoptPrefill(g.label, n.label, n.location) : '',
        });
      }
```

(c) Start-here derivation (replace lines 167-171):

```ts
  // First group that is ranked AND still has a clickable Adopt row — a ranked
  // group whose every shown row is control-plane (denylist-refused) must not
  // claim "Start here": the chip would sit on a group with no button. Ranked
  // groups sort first, so the scan walks the guide order.
  const startHereAssetType = $derived(
    adoptGroups.find((g) => g.rank != null && g.rows.some((r) => r.adoptable))
      ?.assetType ?? null,
  );
```

(d) template (the `{:else}` branch at lines 558-562) becomes:

```svelte
                {:else if row.controlPlane}
                  <span class="ds-subtle infra-adopt__muted" data-testid="adopt-control-plane"
                    >IaC control-plane infrastructure — the always-on denylist blocks changes
                    to it, adoption included</span
                  >
                {:else}
                  <span class="ds-subtle infra-adopt__muted" data-testid="adopt-unavailable"
                    >not yet adoptable</span
                  >
                {/if}
```

**Step 4: Run to verify pass**

Run: `cd frontend && npm run test:unit -- InfraDiagram`
Expected: PASS (existing tests untouched — `liveGraph()` has no control-plane nodes).

**Step 5: Commit**

```bash
git add frontend/src/components/InfraDiagram.svelte frontend/tests/unit/InfraDiagram.test.ts
git commit -m "feat(ui): suppress Adopt on control-plane rows; Start-here skips fully-blocked groups"
```

---

### Task 6: tour step 4 — skip control-plane nodes + honest fallback

**Files:**
- Modify: `frontend/src/lib/tour.ts`
- Test: `frontend/tests/unit/tour.test.ts`

**Step 1: Write the failing tests** — append to the `adoptStepState` describe block (reuse `makeNode`/`makeGroup`/`makeGraph`):

```ts
it('skips control-plane nodes — the live papercut: rank-1 must not be our own bucket', () => {
  const g = makeGraph({
    groups: [
      makeGroup({
        adoptable: true,
        adopt_rank: 1,
        nodes: [
          makeNode({ label: 'demo-tofu-artifacts', managed: false, control_plane: true }),
          makeNode({ id: 'n2', label: 'demo-assets', managed: false }),
        ],
      }),
    ],
  });
  const s = adoptStepState(g);
  expect(s.kind).toBe('target');
  if (s.kind === 'target') {
    expect(s.prefill).toContain('`demo-assets`');
    expect(s.prefill).not.toContain('tofu-artifacts');
  }
});

it('falls through to the NEXT group when a whole group is control-plane', () => {
  const g = makeGraph({
    groups: [
      makeGroup({
        adoptable: true,
        adopt_rank: 1,
        nodes: [makeNode({ label: 'demo-tofu-state', managed: false, control_plane: true })],
      }),
      makeGroup({
        asset_type: 'pubsub.googleapis.com/Topic',
        label: 'Pub/Sub topic',
        adoptable: true,
        adopt_rank: 2,
        nodes: [makeNode({ id: 'n2', label: 'orders', managed: false })],
      }),
    ],
  });
  const s = adoptStepState(g);
  expect(s.kind).toBe('target');
  if (s.kind === 'target') expect(s.prefill).toContain('`orders`');
});

it('all-control-plane estate gets the honest denylist line, not the unnamed line', () => {
  const g = makeGraph({
    totals: { resources: 1, managed: 0, drift: 1 },
    groups: [
      makeGroup({
        adoptable: true,
        adopt_rank: 1,
        nodes: [makeNode({ label: 'demo-tofu-artifacts', managed: false, control_plane: true })],
      }),
    ],
  });
  const s = adoptStepState(g);
  expect(s.kind).toBe('none');
  if (s.kind === 'none') {
    expect(s.line).toContain('control-plane');
    expect(s.line).toContain('denylist');
    // honesty: not misdescribed as a naming problem or a type problem
    expect(s.line).not.toContain('named adopt target');
    expect(s.line).not.toContain('not adoptable types');
  }
});

it('control-plane plus unnamed still reports the unnamed line (non-CP nodes exist)', () => {
  const g = makeGraph({
    totals: { resources: 2, managed: 0, drift: 2 },
    groups: [
      makeGroup({
        adoptable: true,
        adopt_rank: 1,
        nodes: [
          makeNode({ label: 'demo-tofu-state', managed: false, control_plane: true }),
          makeNode({ id: 'n2', label: '   ', managed: false }),
        ],
      }),
    ],
  });
  const s = adoptStepState(g);
  expect(s.kind).toBe('none');
  if (s.kind === 'none') expect(s.line).toContain('named adopt target');
});
```

**Step 2: Run to verify failure**

Run: `cd frontend && npm run test:unit -- tour`
Expected: FAIL — first test picks `demo-tofu-artifacts`; all-CP case returns the unnamed line.

**Step 3: Implement** — in `frontend/src/lib/tour.ts`, inside `adoptStepState`:

(a) the candidate `find` (lines 157-163) becomes:

```ts
  for (const { g, rank } of candidates) {
    // T7 (Codex MF2): never suggest a node the graph didn't name — an empty
    // normalized label would yield an empty-backtick prefill and blank copy.
    // Control-plane nodes are skipped too: the denylist refuses their
    // adoption outright (ranking-filter follow-up — the live rank-1 pick was
    // DriftScribe's own -tofu-artifacts bucket).
    const node = g.nodes.find(
      (n) =>
        !n.managed &&
        n.control_plane !== true &&
        normalizeForPrompt(n.label, 254) !== '',
    );
```

(b) the fallback tail (after the `drift === 0` early return, replacing the `adoptableUnnamed` block at lines 186-206):

```ts
  // Distinguish WHY there is no suggestion (Codex 019eb76d round-2 + the
  // ranking-filter follow-up): control-plane-only ≠ unnamed ≠ no adoptable
  // type — each gets its own honest line.
  const unmanagedShown = candidates.flatMap(({ g }) =>
    g.nodes.filter((n) => !n.managed),
  );
  const nonControlPlane = unmanagedShown.filter((n) => n.control_plane !== true);
  if (unmanagedShown.length > 0 && nonControlPlane.length === 0) {
    return {
      kind: 'none',
      line:
        'The unmanaged resources the agent could otherwise adopt are IaC ' +
        'control-plane infrastructure — DriftScribe services or IaC ' +
        'state/artifact buckets — which the always-on denylist blocks the ' +
        'agent from changing, adoption included. The Infrastructure panel ' +
        'shows everything that is there.',
    };
  }
  return nonControlPlane.length > 0
    ? {
        kind: 'none',
        line:
          'There are unmanaged resources the agent could adopt, but none ' +
          'has a named adopt target the tour can prefill. The ' +
          'Infrastructure panel shows what the live graph can show.',
      }
    : {
        kind: 'none',
        line:
          'Your remaining unmanaged resources are not adoptable types yet. ' +
          'The Infrastructure panel shows what is there, and you can ask ' +
          'about any of them in chat.',
      };
```

**Step 4: Run to verify pass**

Run: `cd frontend && npm run test:unit -- tour`
Expected: PASS (existing tour tests untouched — `makeNode` has no `control_plane`).

**Step 5: Commit**

```bash
git add frontend/src/lib/tour.ts frontend/tests/unit/tour.test.ts
git commit -m "feat(tour): step 4 never suggests control-plane resources; honest all-blocked fallback"
```

---

### Task 7: full verification

**Step 1:** `.venv/bin/pytest -q` (repo root) — expect ≥ 2824 + new, 0 failures.
**Step 2:** `.venv/bin/ruff check --no-cache .` — clean.
**Step 3:** `cd frontend && npm run test:unit` — expect ≥ 526 + new, 0 failures.
**Step 4:** `cd frontend && npm run check && npm run build` — clean.
**Step 5:** commit anything outstanding; no commit expected here.

---

## Ship checklist (operator-side, after review)

1. PR → CI green → Codex completed-work review (same thread) → SHIP → squash-merge.
2. **Coordinator rebake only** (`infra/cloudbuild.coordinator-update.yaml`, `_TAG=<short-sha>`), find revision by image digest, `update-traffic --to-revisions=<rev>=100`. No tofu-editor / tofu-apply / infra-reader rebake (denylist and inventory untouched).
3. Live verify: `/infra/graph` shows `control_plane: true` on the `-tofu-artifacts` / `-tofu-state` bucket nodes and `driftscribe-*` service nodes; panel adopt list shows the denylist note on those rows and the Start-here chip + tour step 4 pick the first NON-control-plane bucket; chat ("what should I adopt first?") never suggests a control-plane resource.
