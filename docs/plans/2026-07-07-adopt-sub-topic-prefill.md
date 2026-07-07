# Subscription Topic Enrichment + Demo "Real, but restorable" README Section

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make one-click "Adopt into IaC" work for Pub/Sub subscriptions by carrying the subscription→topic edge through the whole data plane (reader → inventory sample → graph node → chat prefill), so the Provision crew never has to stall and ask the operator for a topic it should be able to read. Plus a small README addition documenting the demo's "real, but restorable" design.

**Branch:** `feat/adopt-sub-topic-prefill` (this worktree, off main `9987636`). Codex plan-review thread: `019f3d12-9be4-7b71-8e8e-fd7124d8b7dc` — after implementation, follow up on that same thread via `mcp__codex__codex-reply` so Codex can review the completed work against this plan.

---

## Background — the verified stall (2026-07-07)

Live trace `0ae9512f2bd54911b8937c728888db4c`: a visitor clicked Adopt on `adopt-probe-sub`. The Provision crew correctly identified `propose_adoption_tool`, which for `google_pubsub_subscription` REQUIRES `topic` (`agent/adk_tools.py:967`). It read `read_project_inventory_tool` (no topic there), searched team conversations 4x, refused to guess despite `adopt-probe-topic` sitting in the same inventory (prompt hard-rule: "Do NOT guess a topic or image — ask", `workloads/provision/system_prompt.md:~70`), and asked the operator. Correct behavior; unanswerable question for a visitor.

Root cause is a data gap, NOT a prompt or demo-gating problem: the infra-reader's CAI `SearchAllResources` uses `read_mask ["name","asset_type","location"]` (`workers/infra_reader/main.py:54`), so the sub→topic edge never enters DriftScribe's data plane — not the inventory sample (`driftscribe_lib/infra_inventory.py:110`), not the `/infra/graph` node (`driftscribe_lib/infra_graph.py:390`), not the Adopt prefill (`frontend/src/lib/infra_graph.ts adoptPrefill`).

**Verified live (no new IAM needed):** `gcloud asset search-all-resources --scope=projects/driftscribe-hack-2026 --asset-types=pubsub.googleapis.com/Subscription --read-mask=name,versionedResources` returns `versionedResources[0].resource.topic = "projects/driftscribe-hack-2026/topics/adopt-probe-topic"`. The reader SA's existing permission (`cloudasset.assets.searchAllResources`, whether via the custom role or `roles/cloudasset.viewer`) covers this call.

(The same gap exists for Cloud Run `image` — out of scope here; note it in the PR body as known follow-up.)

---

## Design decisions (settled — do not re-litigate)

- **Mechanism:** a SECOND scoped CAI call, not a widened main read_mask (would over-fetch rich metadata for every asset type, including sensitive-adjacent fields) and not the Pub/Sub API (new IAM). Codex concurred.
- **Only `topic` is retained** from the versioned resource. The rest (push endpoints, SA emails, labels) is discarded immediately, never stored, never logged, never returned.
- **Soft-fail contract:** any failure of the enrichment call (API error, unexpected shapes) degrades to an empty map → samples simply lack `topic` → today's behavior (crew asks). The primary inventory must NEVER degrade because of the enrichment. Individually malformed rows are skipped while the rest proceed; the whole-map `{}` fallback is for API failure only.
- **Join key:** the raw CAI `name` from both `SearchAllResources` calls (`//pubsub.googleapis.com/projects/.../subscriptions/...`), NOT the normalized display name.
- **Topic display form:** shorten `projects/{project}/topics/{name}` → `{name}` iff the project matches the reader's `GCP_PROJECT`; any other shape passes through unchanged (a cross-project full path is still valid input to `propose_adoption_tool`, which normalizes/rejects at its own boundary — do not silently shorten foreign projects).
- **Optional-field style:** `topic` appears in sample/node dicts ONLY when present (matches `control_plane` / `truncated_in_group` only-when-true style; keeps all non-subscription output byte-identical; stale caches/coordinators are safe because every consumer treats it as optional).

## Scope

- `driftscribe_lib/infra_inventory.py`, `driftscribe_lib/infra_graph.py`
- `workers/infra_reader/main.py`
- `frontend/src/lib/infra_graph.ts`, `frontend/src/lib/tour.ts`
- `agent/adk_tools.py` (docstring only), `workloads/provision/system_prompt.md`
- `docs/architecture/iam-matrix.md`
- `README.md`, `README.ja.md`
- Tests: `tests/unit/test_infra_inventory.py`, `tests/unit/test_infra_graph.py`, a new infra-reader worker test, `frontend/tests/unit/infra_graph.test.ts` (+ tour test if one pins the prefill)

NOT in scope: Cloud Run image enrichment; any coordinator route change (`read_project_inventory_tool` passes the worker response through untouched — zero coordinator code change); any IAM change.

---

## Tasks

### 1. `driftscribe_lib/infra_inventory.py` — carry the field

- `CaiResource` gains `topic: str | None = None` (frozen dataclass; default keeps every existing construction valid).
- New pure helper `shorten_topic(topic: str, project: str) -> str` per the design decision above.
- `build_inventory`: when `r.topic` is a non-empty `str`, sample entry gains `"topic": r.topic`. Absent otherwise.

Tests (`test_infra_inventory.py`): sub sample carries topic; `topic=None` → NO `topic` key; non-sub entries unchanged; `shorten_topic` (same-project → short, cross-project → passthrough, garbage → passthrough).

### 2. `workers/infra_reader/main.py` — the scoped enrichment call

- `_SUB_ASSET_TYPE = "pubsub.googleapis.com/Subscription"`.
- `_subscription_topics(client) -> dict[str, str]`: `SearchAllResourcesRequest(scope=..., asset_types=[_SUB_ASSET_TYPE], read_mask={"paths": ["name", "versioned_resources"]})`. For each hit, take the FIRST versioned resource whose `resource["topic"]` is a non-empty string (skip malformed rows, keep going); value = `shorten_topic(raw, GCP_PROJECT)`; key = the hit's raw `name`. The `resource` field is a protobuf Struct — verify the exact accessor against the pinned google-cloud-asset version and keep the boundary conversion thin: extract via a small pure function unit-testable with plain-dict doubles.
- `describe()`: after `_search_all`, only if any resource has `asset_type == _SUB_ASSET_TYPE`, call `_subscription_topics` inside its own try/except (log a warning, never raise, never touch the primary result on failure), then join: `dataclasses.replace(r, topic=topics.get(r.name))` for subscription resources.
- Update the module docstring's read-mask paragraph: primary inventory stays minimal-masked; subscriptions get one additional scoped `versioned_resources` read from which ONLY `resource.topic` is retained.

Tests: pure extractor (good row, malformed rows skipped, empty); a fake client capturing requests to PIN `asset_types=["pubsub.googleapis.com/Subscription"]` and `read_mask paths == ["name", "versioned_resources"]`; enrichment failure → describe still returns the full inventory without `topic` keys.

### 3. `driftscribe_lib/infra_graph.py` — node passthrough

At the node build (~line 390): `t = sample.get("topic")`; add `node["topic"] = t` only when `isinstance(t, str) and t` (type-strict, same defensive stance as `location`).

Tests (`test_infra_graph.py`): passthrough when present; absent key when sample lacks it or carries a non-string.

### 4. `frontend/src/lib/infra_graph.ts` + `tour.ts` — prefill

- `InfraNode` gains `topic?: string | null` (optional = stale-coordinator-safe, same pattern as `control_plane`).
- `adoptPrefill(groupLabel, nodeLabel, location, topic: string | null = null)`: when `typeof topic === 'string' && topic` (runtime guard — server data is untrusted), append `` ` Its topic is \`${normalizeForPrompt(topic, 254)}\`.` ``
- ALL THREE call sites pass the node's topic: `adoptRows` (~line 407), `resourceRows` (~line 593), and `tour.ts:212` (the tour's "good first adoption" prefill — missing this recreates the stall from the tour path).
- **Location suppression for Pub/Sub (recommended polish, Codex nit):** `render_adoption` FORBIDS `location` for topics/subscriptions (`driftscribe_lib/adopt_recipe.py` `_enforce_forbidden`), yet the prefill currently says "in global", inviting the crew to pass `location` and eat a rejected/retry loop. Add a small helper (e.g. `prefillLocation(assetType, location)` returning `null` for `pubsub.googleapis.com/Topic|Subscription`) and use it at the same three call sites. This changes existing prefill strings for pubsub rows — update the pinned test expectations deliberately.

Tests (`frontend/tests/unit/infra_graph.test.ts`): prefill with topic; without topic → unchanged legacy string; topic normalized (control chars/length); pubsub rows carry no "in {location}"; non-pubsub rows keep location. Check whether a tour test pins the prefill string and update it.

### 5. Crew-facing text (LESSON, PR #202: ADK sends tool docstrings to the model)

- `propose_adoption_tool` docstring, the `google_pubsub_subscription` bullet: "ask the operator if unknown" → read it from `read_project_inventory_tool`'s subscription sample (`topic` field); ask the operator only if it is missing there.
- `workloads/provision/system_prompt.md` required-facts line: note the inventory sample now carries the subscription's topic, and adjust the closing rule to "Do NOT guess a topic or image — read the fact from the inventory, or ask." (anti-guess rule intact; reading ≠ guessing). Run the prompt anchor tests (`test_crew_redirect_block.py` and friends) — if any pins the old byte sequence, update it deliberately.

### 6. `docs/architecture/iam-matrix.md` — keep the contract honest

Row `infra-reader-sa` (~line 18) currently says "minimal read_mask (name/asset_type/location)". Update to: primary inventory minimal-masked as before, PLUS one scoped `versioned_resources` search for `pubsub.googleapis.com/Subscription` only, from which only `resource.topic` is retained (nothing else stored, logged, or returned). Same permission (`cloudasset.assets.searchAllResources`); no grant change.

### 7. README "Real, but restorable" (separate commit in the same PR)

Add under `## Demo` (README.md:122, right after the first paragraph), then a JA-parity translation under `## デモ` (README.ja.md:111). EN rules: NO em dashes, plain de-AI voice. Draft (adjust to fit surrounding register):

> ### Real, but restorable
>
> A mutation stays open to anonymous visitors when its blast radius is bounded and mechanically restorable, and is gated when it is not.
>
> - **Open, self-healing:** asking Patch to fix the vulnerable dependency merges a real PR (one line of `demo/upgrade-target/package.json`); asking Anchor to roll back really moves `payment-demo` traffic to an earlier revision. A scheduled workflow ([`demo-reset.yml`](.github/workflows/demo-reset.yml)) restores both baselines: the service every two hours, the upgrade fixture every morning.
> - **Gated:** merging an infrastructure PR always requires the operator's identity, and free-form infrastructure authoring is operator-only during the public window. A merged infra PR cannot be unmerged, so it never happens anonymously.
>
> What you see is neither a mockup nor an honor system: real changes land, and the parts that cannot be safely reset are the parts you cannot reach.

(The "operator-only during the public window" claim is TRUE on main as of #212/M4 — verify the wording against the shipped demo-anonymous gating before committing.)

### 8. Verify

- `pytest tests/unit` (full unit suite), plus the touched integration tests if any reference inventory shapes.
- `cd frontend && npm test`.
- Manual sanity: `python -c` a `build_inventory` round-trip with a topic-carrying `CaiResource`.

---

## Post-merge deploy (PR #195 lesson: driftscribe_lib is shared — deploy BOTH)

1. Infra-reader worker redeploy (sample gains `topic`).
2. Coordinator redeploy (graph builder + SPA bundle; remember the traffic-pinning gotcha — use the `driftscribe-deploy` skill runbook).
3. `/infra/graph` has an L1(60s)/L2(900s) cache: the topic shows in the UI after cache expiry or a forced refresh. Not a bug.
4. Live verify: re-run the Adopt-a-subscription flow (`adopt-probe-sub`) end-to-end; expect the crew to call `propose_adoption_tool` with `topic=adopt-probe-topic` WITHOUT asking. NOTE: an open adoption PR for the sub will be dupe-guarded if one already exists — check `GET /infra/pending-approvals` first. Do NOT approve/merge the resulting PR; leaving it open is fine (same as #168).
5. `mcp__codex__codex-reply` on thread `019f3d12-9be4-7b71-8e8e-fd7124d8b7dc` for the post-implementation review.
