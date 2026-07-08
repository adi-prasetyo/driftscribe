# Eventarc Pub/Sub Adopt-Suppression

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stop offering "Adopt into IaC" for the Pub/Sub topics and subscriptions Eventarc auto-creates as trigger transport (`eventarc-*`), the same way Google-service-managed buckets are already suppressed. Removes 4 of the 5 Adopt buttons anonymous visitors can currently click during the open demo window, leaving only the intended demo target (`adopt-probe-svc`).

**Architecture:** Extend the existing service-managed identity rule to Pub/Sub across the SAME three surfaces the bucket rule covers, in one place each: denylist plan rule + shared name helper (`iac_plan_denylist.py`), tool-boundary refusal (`adopt_recipe._reject_control_plane`), and the node `control_plane` flag (`CONTROL_PLANE_NODE_MATCHERS`, consumed by `infra_graph` + `infra_inventory`). The three-surface parity is already pinned by tests; this change extends the pinned set.

**Why these four resources are service-managed:** `eventarc-asia-northeast1-driftscribe-cloudrun-changes-823` / `...-v2-update-516` (topics) and their `...-sub-019` / `...-sub-865` subscriptions are created and lifecycle-owned by Eventarc for DriftScribe's two audit-log triggers (see iam-matrix `eventarc-trigger-sa` row). Adopting them into operator IaC is meaningless (deleting/recreating a trigger orphans or replaces them), exactly the service-managed-bucket rationale.

---

## Design decisions

- **Identity rule: prefix `eventarc-` on the SHORT name, for asset types `pubsub.googleapis.com/Topic` and `pubsub.googleapis.com/Subscription` only.** Google names all Eventarc Pub/Sub transport `eventarc-<location>-<trigger>-<suffix>`. A user-created topic that happens to start with `eventarc-` would be a false positive — fail-safe (adopt refusal + plan denial, never a bad apply), same accepted tradeoff as the bucket prefixes.
- **Same `control_plane` CTA-suppression flag**, not a new flag — matching how service-managed buckets ride the control-plane flag today ("Both carry the same control_plane CTA-suppression flag").
- **Denylist rule scope mirrors `service-managed-bucket`:** violation on ANY non-no-op change to or import of a matching identity. New violation id: `service-managed-pubsub`. No OBJECT-style sub-case (nothing nested to smuggle).
- **Coverage math shifts intentionally:** the 4 nodes move from actionable drift into `not_in_iac_control_plane`, so the header denominator (managed + actionableDrift) shrinks by 4 and the actionable-drift badge drops. This is the point, not a regression — update any pinned counts deliberately.

## Scope

- `driftscribe_lib/iac_plan_denylist.py` — constants, helper, matcher entries, plan check, module-docstring rule list (~line 46)
- `driftscribe_lib/adopt_recipe.py` — `_reject_control_plane` pubsub branch + docstring ("Pub/Sub has no identity rule" sentences here and in the denylist matcher comment become "Pub/Sub's only identity rule is the Eventarc transport prefix")
- `driftscribe_lib/infra_graph.py` — `ADOPTION_CONTROL_PLANE_NOTE` copy gains the Eventarc transport clause (flows to crews + SPA)
- `workloads/provision/system_prompt.md` — control-plane bullet gains the Eventarc clause (provision is NOT byte-golden; still run the prompt anchor tests)
- Tests: `tests/unit/test_iac_plan_denylist_lib.py`, `test_infra_graph.py` (incl. the parity classes at ~877-1067), `test_infra_inventory.py`, `test_adopt_recipe.py` (or wherever `_reject_control_plane` is pinned), any pinned drift-count fixtures
- Check-only: `agent/adk_tools.py` docstrings (generic "control-plane infrastructure" wording likely fine), `agent/capabilities.py` + homepage denylist-panel copy (add a line ONLY if the panel enumerates rules individually), frontend copy for control-plane rows (server note may flow through)

NOT in scope: closing the already-open PRs #168/#215 (probe resources, not eventarc); any IAM change; demo-reset auto-close of adoption PRs (declined for now — a pending card demonstrates the gate).

## Tasks

### 1. `iac_plan_denylist.py`

- `SERVICE_MANAGED_PUBSUB_PREFIXES: tuple[str, ...] = ("eventarc-",)` next to the bucket constants.
- `is_service_managed_pubsub_name(name: object) -> bool` — None/non-str safe, prefix match, same PUBLIC/shared-across-surfaces docstring stance as the bucket helper.
- `CONTROL_PLANE_NODE_MATCHERS` gains `"pubsub.googleapis.com/Topic"` and `"pubsub.googleapis.com/Subscription"` → `is_service_managed_pubsub_name`. Rewrite the "Pub/Sub has no identity rule, so its nodes are never flagged" comment.
- `_check_service_managed_pubsub(rc, rtype, actions, before, after, violations)` mirroring `_check_service_managed_bucket`: `rtype` gate on `{"google_pubsub_topic", "google_pubsub_subscription"}`, match `before/after["name"]`, emit `Violation("service-managed-pubsub", ...)`. Register it wherever the bucket check is dispatched from.
- Module docstring rule list (~line 46): add the `service-managed-pubsub` entry.
- `__all__` deliberately unchanged: `is_service_managed_bucket_name` is public-but-not-exported today and the exact-list test pins it; the new helper follows the same convention. The tools shim re-exports the new constant + helper, pinned by the shim identity test (Codex review).

Tests: violation on update/import/delete of an `eventarc-*` topic and sub; NO violation for `order-events`/`orders-sub`/`adopt-probe-*`; a mutated pubsub row with NO name on either side emits `plan-json-malformed-change` (bias-to-deny — this check owns the case, there is no earlier bucket-style guard for Pub/Sub; Codex review); existing bucket cases untouched.

### 2. `adopt_recipe.py`

New `_reject_control_plane` branch (after the bucket branches, before the run-service one):

```python
if resource_type in ("google_pubsub_topic", "google_pubsub_subscription") and (
    is_service_managed_pubsub_name(name)
):
    raise AdoptRecipeError(
        f"{name!r} cannot be adopted: it is a Pub/Sub resource that Eventarc "
        "creates automatically to deliver trigger events, not a resource you "
        "provisioned — the always-on denylist refuses any plan that would "
        f"change or import it. {FINAL_REFUSAL_MARKER}"
    )
```

Docstring: drop the "Pub/Sub has no identity rule" sentence, state the Eventarc prefix rule. (LESSON PR #202: this error string reaches the model and the operator — keep it operator-plain, no code identifiers.)

Tests: eventarc topic + sub names rejected with the FINAL marker; `adopt-probe-topic`/`adopt-probe-sub` still render fine.

### 3. Copy surfaces

- `ADOPTION_CONTROL_PLANE_NOTE` (`infra_graph.py:136`): extend the parenthetical list with "and the Pub/Sub topics and subscriptions Eventarc creates to deliver trigger events".
- `workloads/provision/system_prompt.md` control-plane bullet (the "neither can buckets that a Google service auto-creates" sentence): same clause. Run `test_prompt_tool_names.py` + `test_crew_redirect_block.py`; update anchors deliberately if pinned.
- Sweep `agent/adk_tools.py` docstrings for bucket-specific "service auto-creates" phrasing that should now say buckets or Pub/Sub transport (gotcha 2).

### 4. Verify + parity

- `uv run ruff check . && uv run pytest tests/unit workers/infra_reader/tests -q`
- `cd frontend && npm run test:unit -- --run && npm run check && npm run build` (only if frontend copy changed; the `control_plane` flag itself needs no frontend change)
- The parity tests (`test_infra_graph.py` ~877-1067) MUST now cover the pubsub matcher: the matcher-set assertion at line 998 pins the exact key set — extend it, and add eventarc fixtures to the import-admission parity cases.

### 5. Deploy + live verify

`driftscribe_lib` is shared → deploy coordinator AND infra-reader AND tofu-apply (Codex review: the apply worker bakes driftscribe_lib and re-runs the denylist immediately before apply — PR #195 lesson, extended). Then confirm on the public graph (after L2 cache expiry or forced refresh): the four `eventarc-*` rows carry `control_plane: true`, the Adopt zone shows only `adopt-probe-svc`, and the coverage denominator dropped by 4.
