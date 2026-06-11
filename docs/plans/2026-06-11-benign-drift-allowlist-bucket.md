# Benign-Drift Allowlist: Per-Type Scoping + `google_storage_bucket` + Null↔Empty Normalization

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stop the semantic freshness gate from refusing applies on provably content-free
refresh drift — `google_storage_bucket.updated` readback churn and provider null↔empty-collection
normalization on explicitly approved paths — while keeping every fail-closed property intact.

**Architecture:** Restructure the flat allowlist (`COMPUTED_ONLY_DRIFT_PATHS` /
`COMPUTED_ONLY_DRIFT_SUBTREES` + `_BENIGN_DRIFT_TYPES`) in
`workers/tofu_apply/tofu_runner.py` into a per-resource-type `DriftAllowlist` map, add a
`google_storage_bucket` entry (`updated` only), and teach the leaf-diff to classify
explicit-null↔empty-collection deltas as a distinct *normalization* category — benign
ONLY on per-type approved paths, audited per-path. A `_MISSING` sentinel keeps absent
keys and list-element add/remove distinct from explicit null (Codex must-fix 2+3).

**Tech Stack:** Python 3.12, pytest (injectable `run_tofu` seam — no live tofu/GCP in tests).

**Codex plan review:** thread 019eb556-8d65-7092-a564-a4d8075e27ce — GO-with-must-fix;
all three must-fixes folded below (normalization is path-scoped per type, not global;
absent-vs-null sentinel; list cardinality changes never normalize).

---

## 0. Why now (live evidence, 2026-06-11)

The adopt PR #95 apply was refused `drift_refused` with:

> `google_storage_bucket.checkout_assets: computed-drift allowlist is type-scoped;
> 'google_storage_bucket' not recognized`

The actual drift (verified via a local read-only `tofu plan -refresh-only`; full output
preserved in the session transcript):

1. `google_storage_bucket.checkout_assets` — **only** `updated` (RFC3339 metadata-write
   readback timestamp) changed.
2. `google_cloud_run_v2_service.storefront` / `.orders_worker` — **only** explicit-null →
   `{}`/`[]` readback artifacts, on exactly these (index-stripped) paths:
   `annotations`, `custom_audiences`, `labels`, `template.annotations`,
   `template.labels`, `template.containers.args`, `template.containers.command`,
   `template.containers.depends_on`.

Both are content-free, yet the gate fails closed on them, and recovering cost a manual
state reconcile — which then invalidated the saved plan (serial pinning) and forced
re-plan PR #96. Until this fix, ANY bucket metadata churn re-blocks every apply.
The gate's own design comment (Codex review 019e7a3f) anticipated exactly this
extension: "refresh drift on any OTHER resource type fails closed until the allowlist
is extended for it."

## 1. Safety analysis (what must NOT change)

- **Fail-closed posture.** An incomplete allowlist may only OVER-refuse; nothing in this
  change can introduce a false-clean for a material desired-state delta:
  - Per-type scoping is *strengthened*: paths benign for Cloud Run (e.g. `etag`,
    `update_time`) are no longer even shareable with other types by accident.
  - The null↔empty rule fires ONLY when one side is an **explicit** `null` and the other
    the EMPTY `{}`/`[]`, AND the (index-stripped) path is in the type's
    `normalization_paths` (or already computed-only). No attribute values exist on either
    side, so an approved delta cannot encode a desired-state change. `null↔""`, `null↔0`,
    `null↔false`, `null↔non-empty`, and any unapproved path remain genuine refusals.
  - **Absent ≠ null** (Codex must-fix 2): a `_MISSING` sentinel marks keys/elements
    present on only one side. Sentinel↔anything is a genuine change — so a key that
    vanishes, appears, or a list that grows/shrinks (`[]→[{}]`, Codex must-fix 3 — block
    cardinality IS content in provider schemas) is material unless its path is
    computed-only allowlisted. (In practice `tofu show -json` emits every schema
    attribute with explicit `null`, and the fidelity gate pins the provider via the
    signed lockfile SHA, so before/after key sets match — the sentinel is a cheap
    correctness backstop, not an over-refusal risk.)
  - The normalization rule is only *reachable* for types present in the allowlist map —
    unknown types still refuse at the type check, before any path analysis.
- **Identity/lifecycle fields stay material.** `time_created` (bucket) joins `uid` /
  `create_time` / `delete_time` (Cloud Run) as deliberately NOT allowlisted — a changed
  creation timestamp signals out-of-band recreate and MUST refuse.
- **Labels stay material for buckets** — `labels` / `effective_labels` /
  `terraform_labels` are in NEITHER bucket set (paths nor normalization): an out-of-band
  label add is operator-visible drift we want surfaced, and `iac/checkout_assets.tf`
  declares explicit labels so the empty-artifact shape shouldn't even occur. For Cloud
  Run, `labels`/`annotations` appear ONLY in `normalization_paths`: the empty artifact
  (null↔`{}`) is tolerated, but any REAL label value yields a genuine changed path
  (`labels.x` for `{}→{x:y}`, or `labels` itself for `null→{x:y}` — a type-mismatch
  leaf) — still material either way.
- **The `run_apply_sequence` symmetry guard stays.** Exit-2 with an empty
  `verdict.paths` still refuses ("refresh exit 2 but no classifiable drift").
  Approved normalization deltas are recorded as *paths* (`addr:path [null<->empty]`), so
  an all-normalization exit-2 is explained and proceeds — without weakening the guard
  for genuinely unexplained exit-2s (e.g. an `update` drift entry whose before==after
  contributes nothing, exactly as today).
- **Marker interplay unchanged.** The `before_sensitive` / `after_sensitive` /
  `after_unknown` checks stay BEFORE any diffing — sensitive/unknown markers combined
  with normalization-shaped deltas still fail closed.
- **Audit transparency.** Every benign path — allowlisted or normalization — lands in
  `ApplyOutcome.benign_drift_paths` → the success audit; normalization paths carry a
  `[null<->empty]` marker (in refusal messages too, so an operator extending the
  allowlist can tell the categories apart).
- **Provider pin caveat (Codex).** The allowlists are validated against
  `hashicorp/google` 6.50.0 (`iac/.terraform.lock.hcl`); revalidate when the lockfile
  changes (the fidelity gate makes a lockfile change explicit by construction).

## 2. Target design (complete new code)

In `workers/tofu_apply/tofu_runner.py`, replace lines 466–479 (the flat
`COMPUTED_ONLY_DRIFT_PATHS` / `COMPUTED_ONLY_DRIFT_SUBTREES` / `_BENIGN_DRIFT_TYPES`
constants) with:

```python
@dataclass(frozen=True)
class DriftAllowlist:
    """Per-resource-type benign-drift allowlist. ``paths``/``subtrees`` admit
    GENUINE value changes on computed/status fields; ``normalization_paths``
    admit ONLY the explicit-null↔empty-collection readback artifact at that
    exact (index-stripped) path — a real value appearing at the same path is a
    genuine change and is judged by ``paths``/``subtrees`` alone."""

    paths: frozenset[str]
    subtrees: tuple[str, ...] = ()
    normalization_paths: frozenset[str] = frozenset()


# Benign refresh-drift classification is SCOPED BY RESOURCE TYPE: a type absent
# from this map fails closed (refuses) until an allowlist is deliberately added
# for it (Codex review 019e7a3f). Each entry holds ONLY fields with no
# desired-state security meaning — server-computed readback (plus, for Cloud Run,
# the two ignore_changes'd gcloud-deploy metadata tags client / client_version).
# Identity/lifecycle-computed fields (uid, create_time, delete_time,
# time_created) are deliberately NOT allowlisted: a changed creation identity
# signals an out-of-band recreate/deletion and MUST refuse. Bucket labels
# (labels / effective_labels / terraform_labels) are in NEITHER set: an
# out-of-band label add is operator-visible drift, surfaced by design.
# normalization_paths entries are the EXACT artifact set observed live
# 2026-06-11 (adopt PR #95 recovery) on provider hashicorp/google 6.50.0 —
# revalidate when iac/.terraform.lock.hcl changes.
BENIGN_DRIFT_ALLOWLISTS: dict[str, DriftAllowlist] = {
    "google_cloud_run_v2_service": DriftAllowlist(
        paths=frozenset({
            "generation", "observed_generation", "etag", "update_time",
            "last_modifier", "client", "client_version",
            "latest_created_revision", "latest_ready_revision", "reconciling",
        }),
        subtrees=("conditions", "terminal_condition", "traffic_statuses"),
        normalization_paths=frozenset({
            "annotations", "custom_audiences", "labels",
            "template.annotations", "template.labels",
            "template.containers.args", "template.containers.command",
            "template.containers.depends_on",
        }),
    ),
    # `updated` is the bucket's RFC3339 metadata-write readback timestamp — it
    # churns on ANY out-of-band metadata touch. It carries no desired-state
    # content itself; if the touch changed a REAL attribute, that attribute
    # drifts too and still refuses. (Live evidence: adopt PR #95 drift_refused
    # on exactly this field, 2026-06-11.) No normalization_paths: no bucket
    # null↔empty artifact has been observed live — extend only on evidence.
    "google_storage_bucket": DriftAllowlist(paths=frozenset({"updated"})),
}
```

Rework `_is_computed_only_path` to take the type's allowlist:

```python
def _is_computed_only_path(path: str, allowlist: DriftAllowlist) -> bool:
    """True iff ``path`` is an exact computed leaf OR sits under a status subtree
    of the given TYPE-SPECIFIC allowlist (anchored at the path root — never a
    same-named field deeper in the tree)."""
    if path in allowlist.paths:
        return True
    return any(path == p or path.startswith(p + ".") for p in allowlist.subtrees)
```

Rework the leaf diff: rename `_changed_leaf_paths` → `_diff_leaf_paths`, returning a
`(changed, normalized)` pair so null↔empty artifacts are classified by VALUE — and a
`_MISSING` sentinel so absent keys / out-of-range list elements NEVER read as null:

```python
# Sentinel for a key/element present on only ONE side of the diff. Distinct from
# explicit null: `tofu show -json` emits every schema attribute (null when unset),
# so a key that genuinely appears/vanishes — or a list that grows/shrinks (block
# cardinality IS content in provider schemas) — is a real change, never a
# normalization artifact (Codex must-fix: [] -> [{}] must refuse).
_MISSING = object()


def _is_null_empty_normalization(before: object, after: object) -> bool:
    """True iff one side is an EXPLICIT ``null`` and the other the EMPTY
    collection (``{}``/``[]``) — the provider-readback normalization artifact.
    No attribute values exist on either side, so the delta cannot encode a
    desired-state change. ``null↔\"\"``, ``null↔0``, ``null↔false``,
    ``null↔non-empty`` and ``_MISSING↔anything`` are NOT normalization."""
    if before is None:
        return isinstance(after, (dict, list)) and len(after) == 0
    if after is None:
        return isinstance(before, (dict, list)) and len(before) == 0
    return False


def _diff_leaf_paths(
    before: object, after: object, prefix: str = ""
) -> tuple[set[str], set[str]]:
    """Recursively diff two JSON values → ``(changed, normalized)`` sets of
    NORMALIZED leaf paths. ``changed`` are genuine value deltas (including any
    side being ``_MISSING``); ``normalized`` are explicit-null↔empty-collection
    readback artifacts (benign ONLY if the type's allowlist approves the path).
    Lists compare element-wise by index (indices stripped in the normalized
    leaf path); length mismatch puts ``_MISSING`` on the short side."""
    if before is _MISSING or after is _MISSING:
        return {_normalize_attr_path(prefix)}, set()
    if before == after:
        return set(), set()
    if _is_null_empty_normalization(before, after):
        return set(), {_normalize_attr_path(prefix)}
    if isinstance(before, dict) and isinstance(after, dict):
        changed: set[str] = set()
        normalized: set[str] = set()
        for key in set(before) | set(after):
            sub = f"{prefix}.{key}" if prefix else str(key)
            c, n = _diff_leaf_paths(
                before.get(key, _MISSING), after.get(key, _MISSING), sub)
            changed |= c
            normalized |= n
        return changed, normalized
    if isinstance(before, list) and isinstance(after, list):
        changed, normalized = set(), set()
        for i in range(max(len(before), len(after))):
            b = before[i] if i < len(before) else _MISSING
            a = after[i] if i < len(after) else _MISSING
            c, n = _diff_leaf_paths(b, a, f"{prefix}[{i}]")
            changed |= c
            normalized |= n
        return changed, normalized
    # scalar (or type-mismatch) leaf: a genuine value change.
    return {_normalize_attr_path(prefix)}, set()
```

In `classify_refresh_drift`, the per-entry tail becomes:

```python
        allowlist = BENIGN_DRIFT_ALLOWLISTS.get(entry.get("type") or "")
        if allowlist is None:
            # The computed-field allowlist is type-scoped; an unrecognized type's
            # drift cannot be proven benign by it → fail closed.
            return RefreshDriftVerdict(
                False, (), f"{addr}: computed-drift allowlist is type-scoped; "
                f"{entry.get('type')!r} not recognized")
        ...sensitive/unknown marker checks unchanged...
        changed, normalized = _diff_leaf_paths(before, after)
        for p in sorted(normalized):
            benign_norm = (p in allowlist.normalization_paths
                           or _is_computed_only_path(p, allowlist))
            (computed if benign_norm else material).append(
                f"{addr}:{p} [null<->empty]")
        for p in sorted(changed):
            (computed if _is_computed_only_path(p, allowlist) else material).append(
                f"{addr}:{p}")
```

(The type lookup REPLACES the current `entry.get("type") not in _BENIGN_DRIFT_TYPES`
check at the same position — before the marker checks, exactly as today. The
`[null<->empty]` marker appears in BOTH the success audit and refusal messages, so an
operator can recognize an unapproved-but-normalization-shaped path when deciding
whether to extend the allowlist.)

Also update the block comment above the constants (lines 435–462: "Semantic freshness
gate" SAFETY paragraph) to describe per-type scoping + the path-scoped normalization
rule + the sentinel, and the `run_apply_sequence` docstring sentence about benign
churn — no logic change there.

**Unchanged on purpose:** the symmetry guard in `run_apply_sequence`; the
sensitive/unknown marker checks (still BEFORE diffing); `RefreshDriftVerdict`;
`_refresh_drift_verdict`; `ApplyOutcome`; worker `main.py` (it only serializes
`benign_drift_paths`); the agent/coordinator (no references — verified by grep).

## 3. Behavior matrix (old → new)

| Drift | Old verdict | New verdict |
|---|---|---|
| Cloud Run computed churn (generation/etag/…) | benign | benign (unchanged) |
| Cloud Run material (image/env/service_account) | refuse | refuse (unchanged) |
| Bucket `updated` only | **refuse** (type-scoped) | **benign**, audited |
| Bucket `labels`/`force_destroy`/`versioning` value change | refuse | refuse |
| Bucket `labels`/`effective_labels` null↔`{}` | refuse | refuse (no bucket normalization paths) |
| Bucket `time_created` changed | refuse | refuse (recreate signal) |
| Bucket drift on a Cloud-Run-only path (`etag`) | refuse | refuse (per-type scoping) |
| Cloud Run null↔`{}`/`[]` on an APPROVED path (`labels`, `template.containers.args`, …) | **refuse** | **benign**, audited `[null<->empty]` |
| Cloud Run null↔`{}` on an UNAPPROVED path (`template.vpc_access`, …) | refuse | refuse, named `[null<->empty]` in the refusal |
| Real value at an approved normalization path (`labels.x` added) | refuse | refuse (genuine leaf change) |
| `null→{"a":1}` / `null→""` | refuse | refuse (not normalization) |
| List growth/shrink `[]↔[{}]` (block cardinality) | refuse | refuse (`_MISSING` is a genuine change) |
| Key present on one side only (absent↔null/empty) | (collapsed via `.get`) | refuse unless computed-only (sentinel) |
| Unknown type (`google_pubsub_topic`, …) | refuse | refuse (type check unchanged) |
| `update` entry with before==after, sole entry | refuse (symmetry guard) | refuse (unchanged) |
| sensitive/unknown markers set (with or without normalization shapes) | refuse | refuse (checked before diff) |

## 4. Tasks

### Task 1: Per-type allowlist refactor (no behavior change)

**Files:**
- Modify: `workers/tofu_apply/tofu_runner.py` (constants block + `_is_computed_only_path` + the type check in `classify_refresh_drift`)
- Test: `workers/tofu_apply/tests/test_tofu_apply.py`

**Step 1:** Update `test_is_computed_only_path_anchoring` to pass
`BENIGN_DRIFT_ALLOWLISTS["google_cloud_run_v2_service"]` as the second arg (same
assertions). Update `test_classify_drift_unknown_type_fails_closed` to use
`google_pubsub_topic` as the unrecognized type (bucket becomes recognized in Task 2) —
keep asserting `"type-scoped" in v.reason`.

**Step 2:** Run `.venv/bin/pytest workers/tofu_apply/tests/ -q` — expect the two
updated tests to FAIL (no `BENIGN_DRIFT_ALLOWLISTS` yet).

**Step 3:** Implement `DriftAllowlist` + `BENIGN_DRIFT_ALLOWLISTS` (Cloud Run entry
ONLY, and WITHOUT `normalization_paths` content yet — paths/subtrees as today), new
`_is_computed_only_path(path, allowlist)`, allowlist lookup in
`classify_refresh_drift`. Delete `COMPUTED_ONLY_DRIFT_PATHS` /
`COMPUTED_ONLY_DRIFT_SUBTREES` / `_BENIGN_DRIFT_TYPES`.

**Step 4:** `.venv/bin/pytest workers/tofu_apply/tests/ -q` — all green.

**Step 5:** Commit: `refactor(tofu-apply): per-type benign-drift allowlist structure`

### Task 2: `google_storage_bucket` entry

**Step 1:** Write failing tests:

```python
_BKT = "google_storage_bucket.checkout_assets"


def _bucket_drift_show(before, after):  # noqa: ANN001, ANN201
    return {"resource_drift": [{
        "address": _BKT, "type": "google_storage_bucket",
        "change": {"actions": ["update"], "before": before, "after": after},
    }]}


def test_classify_drift_bucket_updated_benign() -> None:
    """The live PR #95 refusal signature: ONLY `updated` churned → benign now."""
    v = tofu_runner.classify_refresh_drift(_bucket_drift_show(
        {"updated": "2026-06-10T00:00:00Z", "force_destroy": False},
        {"updated": "2026-06-11T05:00:00Z", "force_destroy": False}))
    assert v.benign is True
    assert v.paths == (f"{_BKT}:updated",)


def test_classify_drift_bucket_material_refuses() -> None:
    for delta in ({"force_destroy": True}, {"labels": {"x": "y"}},
                  {"effective_labels": {"x": "y"}}, {"versioning": [{"enabled": False}]}):
        before = {"updated": "t1", "force_destroy": False, "labels": {},
                  "effective_labels": {}, "versioning": [{"enabled": True}]}
        v = tofu_runner.classify_refresh_drift(_bucket_drift_show(before, {**before, "updated": "t2", **delta}))
        assert v.benign is False, delta


def test_classify_drift_bucket_label_normalization_still_refuses() -> None:
    """Buckets have NO approved normalization paths: labels/effective_labels
    null<->{} refuses (Codex must-fix 1 — label policy is material)."""
    for field in ("labels", "effective_labels", "terraform_labels"):
        v = tofu_runner.classify_refresh_drift(_bucket_drift_show(
            {field: None}, {field: {}}))
        assert v.benign is False, field
        assert any("[null<->empty]" in p for p in v.paths), field


def test_classify_drift_bucket_time_created_material() -> None:
    """A changed creation timestamp signals out-of-band recreate → refuse."""
    v = tofu_runner.classify_refresh_drift(_bucket_drift_show(
        {"time_created": "t1"}, {"time_created": "t2"}))
    assert v.benign is False


def test_classify_drift_no_cross_type_path_leakage() -> None:
    """Cloud-Run-allowlisted paths (etag/update_time) are NOT benign for buckets,
    and the bucket's `updated` is NOT benign for Cloud Run — per-type scoping."""
    v = tofu_runner.classify_refresh_drift(_bucket_drift_show({"etag": "a"}, {"etag": "b"}))
    assert v.benign is False
    v = tofu_runner.classify_refresh_drift(_drift_show({"updated": "t1"}, {"updated": "t2"}))
    assert v.benign is False
```

**Step 2:** Run — expect FAIL.

**Step 3:** Add the `google_storage_bucket` entry to `BENIGN_DRIFT_ALLOWLISTS`.

**Step 4:** Run — green.

**Step 5:** Commit: `feat(tofu-apply): benign-drift allowlist covers google_storage_bucket updated readback`

### Task 3: Path-scoped null↔empty-collection normalization + `_MISSING` sentinel

**Step 1:** Write failing tests:

```python
def test_is_null_empty_normalization() -> None:
    f = tofu_runner._is_null_empty_normalization
    assert f(None, {}) and f({}, None) and f(None, []) and f([], None)
    assert not f(None, "") and not f(None, 0) and not f(None, False)
    assert not f(None, {"a": 1}) and not f(None, [1]) and not f({}, []) and not f(1, 2)
    # _MISSING never normalizes (absent != explicit null)
    assert not f(tofu_runner._MISSING, {}) and not f({}, tofu_runner._MISSING)


def test_diff_leaf_paths_separates_normalization() -> None:
    changed, normalized = tofu_runner._diff_leaf_paths(
        {"a": None, "b": {"c": None}, "d": 1}, {"a": {}, "b": {"c": []}, "d": 2})
    assert changed == {"d"}
    assert normalized == {"a", "b.c"}


def test_diff_leaf_paths_missing_key_is_changed_not_normalized() -> None:
    """Absent key vs empty collection / null is a GENUINE change (sentinel) —
    never a normalization artifact (Codex must-fix 2)."""
    changed, normalized = tofu_runner._diff_leaf_paths({}, {"a": {}})
    assert changed == {"a"} and normalized == set()
    changed, normalized = tofu_runner._diff_leaf_paths({"a": None}, {})
    assert changed == {"a"} and normalized == set()


def test_diff_leaf_paths_list_growth_is_changed_not_normalized() -> None:
    """[] -> [{}] is block-cardinality content, NEVER normalization
    (Codex must-fix 3)."""
    changed, normalized = tofu_runner._diff_leaf_paths({"v": []}, {"v": [{}]})
    assert changed == {"v"} and normalized == set()
    changed, normalized = tofu_runner._diff_leaf_paths({"v": [{}]}, {"v": []})
    assert changed == {"v"} and normalized == set()


def test_classify_drift_null_empty_normalization_approved_paths_benign() -> None:
    """The live storefront/orders_worker signature (2026-06-11): null->{}/[] on
    the approved artifact paths → benign, audited with the [null<->empty] marker
    (non-empty paths so the run_apply_sequence symmetry guard is satisfied)."""
    v = tofu_runner.classify_refresh_drift(_drift_show(
        {"annotations": None, "custom_audiences": None, "labels": None,
         "template": {"annotations": None, "labels": None,
                      "containers": [{"args": None, "command": None, "depends_on": None}]}},
        {"annotations": {}, "custom_audiences": [], "labels": {},
         "template": {"annotations": {}, "labels": {},
                      "containers": [{"args": [], "command": [], "depends_on": []}]}}))
    assert v.benign is True
    assert all("[null<->empty]" in p for p in v.paths) and len(v.paths) == 8


def test_classify_drift_normalization_on_unapproved_path_refuses() -> None:
    """null->{} on a path NOT in normalization_paths refuses, and the refusal
    names it with the [null<->empty] marker (Codex must-fix 1)."""
    v = tofu_runner.classify_refresh_drift(_drift_show(
        {"template": {"vpc_access": None}}, {"template": {"vpc_access": []}}))
    assert v.benign is False
    assert any("template.vpc_access [null<->empty]" in p for p in v.paths)


def test_classify_drift_real_value_at_approved_normalization_path_refuses() -> None:
    """A REAL label appearing is a genuine change (at `labels` — null vs
    non-empty dict is a type-mismatch leaf) — approval of the empty artifact at
    `labels` does not cover it."""
    v = tofu_runner.classify_refresh_drift(_drift_show(
        {"labels": None}, {"labels": {"x": "y"}}))
    assert v.benign is False


def test_classify_drift_normalization_plus_material_still_refuses() -> None:
    v = tofu_runner.classify_refresh_drift(_drift_show(
        {"labels": None, "template": {"service_account": "runtime@"}},
        {"labels": {}, "template": {"service_account": "evil@"}}))
    assert v.benign is False
    assert any("service_account" in p for p in v.paths)
    assert not any("labels" in p for p in v.paths)


def test_classify_drift_normalization_under_status_subtree_benign() -> None:
    """null<->empty UNDER an allowlisted status subtree (conditions/...) is
    computed churn regardless of normalization_paths."""
    v = tofu_runner.classify_refresh_drift(_drift_show(
        {"conditions": [{"reasons": None}]}, {"conditions": [{"reasons": []}]}))
    assert v.benign is True


def test_classify_drift_live_pr95_combined_signature_benign() -> None:
    """The full 2026-06-11 PR #95 drift: bucket `updated` + a Cloud Run service
    with approved null<->empty noise → benign end-to-end."""
    j = {"resource_drift": [
        {"address": _BKT, "type": "google_storage_bucket",
         "change": {"actions": ["update"],
                    "before": {"updated": "t1"}, "after": {"updated": "t2"}}},
        {"address": "google_cloud_run_v2_service.storefront",
         "type": "google_cloud_run_v2_service",
         "change": {"actions": ["update"],
                    "before": {"labels": None}, "after": {"labels": {}}}},
    ]}
    v = tofu_runner.classify_refresh_drift(j)
    assert v.benign is True and len(v.paths) == 2
```

Also update the two existing `_changed_leaf_paths` tests to the
`_diff_leaf_paths` name/signature (assert on the `changed` element, `normalized == set()`).
`test_classify_drift_sensitive_marker_fails_closed` /
`test_classify_drift_after_unknown_marker_fails_closed` already prove marker
interplay; extend one of them to use a normalization-shaped before/after (null vs
`{}`) to lock in "markers beat normalization".

**Step 2:** Run — expect FAIL (`_diff_leaf_paths` / `_is_null_empty_normalization` / `_MISSING` missing).

**Step 3:** Implement `_MISSING` + `_is_null_empty_normalization` + `_diff_leaf_paths`
(replacing `_changed_leaf_paths`) + the classify tail + Cloud Run
`normalization_paths` from §2.

**Step 4:** Run — green.

**Step 5:** Commit: `feat(tofu-apply): path-scoped null<->empty refresh normalization (live PR #95 artifact set)`

### Task 4: Gate-level test + comment/docstring sync + full suite

**Step 1:** Add a `run_apply_sequence`-level test (alongside
`test_gate_benign_drift_proceeds_and_records_paths`): exit-2 refresh whose show-json is
ALL approved normalization → apply proceeds, `benign_drift_paths` non-empty with
markers. (Codex test-gap list.)

**Step 2:** Update the semantic-gate block comment (lines 435–462) and the
`run_apply_sequence` docstring for per-type scoping + path-scoped normalization +
sentinel. No logic.

**Step 3:** `.venv/bin/ruff check workers/tofu_apply` — clean.

**Step 4:** Full suite `.venv/bin/pytest -q` — all green (baseline 2140+).

**Step 5:** Commit: `docs(tofu-apply): semantic-gate comments reflect per-type allowlists`

## 5. Ship sequence

1. PR off `fix/benign-drift-allowlist-bucket` → CI green. NOTE: `.github/workflows/iac.yml`
   runs its static checks (tofu fmt/validate + static-gate) on EVERY PR by design — they
   must pass too; only the plan-builder job stays skipped (no `iac/` diff → no dispatch).
2. Codex completed-work review on the same thread as the plan review
   (019eb556-8d65-7092-a564-a4d8075e27ce).
3. Merge (squash) → **rebake tofu-apply** at the merged short SHA:
   `gcloud builds submit --config=infra/cloudbuild.tofu-apply.yaml --substitutions=_TAG=<sha> --project=driftscribe-hack-2026`
   (~3m15s; the build deploys the worker — no traffic pinning on this service).
4. Verify the serving revision reports the new SHA; no live drift exists right now, so
   the proof is the unit matrix + the next organic apply.
