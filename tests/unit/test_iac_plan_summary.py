"""Unit tests for driftscribe_lib.iac_plan_summary (ClickOps roadmap W1-1).

The summary is ADVISORY DISPLAY ONLY, but its failure modes are safety-shaped:
- never a partial summary (any unparseable entry => None, not a shorter list);
- sensitive values must never appear in any output string;
- counts are computed over ALL entries, truncation trims only the display list.
"""
from __future__ import annotations

from driftscribe_lib.iac_plan_summary import (
    BLAST_CANNOT_TOUCH_NOTE,
    MAX_ATTRS_PER_ENTRY,
    MAX_ENTRIES,
    _pluralize,
    blast_radius_phrase,
    summarize_plan,
)


def _rc(actions, *, rtype="google_storage_bucket", name="b", address=None,
        before=None, after=None, b_sens=False, a_sens=False, unknown=False,
        mode="managed", **extra):
    rc = {
        "address": address or f"{rtype}.{name}",
        "mode": mode,
        "type": rtype,
        "name": name,
        "change": {
            "actions": actions,
            "before": before,
            "after": after,
            "before_sensitive": b_sens,
            "after_sensitive": a_sens,
            "after_unknown": unknown,
        },
    }
    rc.update(extra)
    return rc


def _plan(*rcs):
    return {"format_version": "1.2", "resource_changes": list(rcs)}


def test_create_entry():
    s = summarize_plan(_plan(_rc(["create"], after={"name": "b", "location": "ASIA-NORTHEAST1"})))
    assert s is not None
    assert s.n_create == 1 and not s.destructive
    e = s.entries[0]
    assert e.verb == "create"
    assert e.type_label == "Cloud Storage bucket"
    assert e.name == "b"
    assert e.address == "google_storage_bucket.b"
    assert e.location == "ASIA-NORTHEAST1"
    assert e.attr_changes == ()


def test_update_destroy_replace_classification():
    s = summarize_plan(_plan(
        _rc(["update"], name="u", before={"x": 1}, after={"x": 2}),
        _rc(["delete"], name="d"),
        _rc(["delete", "create"], name="r1"),
        _rc(["create", "delete"], name="r2"),
    ))
    assert (s.n_update, s.n_destroy, s.n_replace) == (1, 1, 2)
    assert s.destructive
    assert [e.verb for e in s.entries] == ["update", "destroy", "replace", "replace"]


def test_noop_read_and_data_reads_are_skipped():
    s = summarize_plan(_plan(
        _rc(["no-op"], name="n"),
        _rc(["read"], name="rd"),
        _rc(["read"], name="dm", mode="data"),
    ))
    assert s is not None and s.entries == ()
    assert (s.n_create, s.n_update, s.n_destroy, s.n_replace) == (0, 0, 0, 0)


def test_data_row_with_mutation_actions_voids_summary():
    # A data row is only skippable as a well-formed READ — one claiming
    # mutation actions is outside audited semantics and must not be hidden.
    good = _rc(["create"], name="ok")
    assert summarize_plan(_plan(good, _rc(["create"], name="dm", mode="data"))) is None
    assert summarize_plan(_plan(good, _rc(["forget"], name="df", mode="data"))) is None


def test_forget_is_its_own_verb_never_green():
    # ["forget"] = OpenTofu "removed" block: the resource LEAVES state, the
    # live resource is untouched. Real state mutation — own verb, never green.
    s = summarize_plan(_plan(_rc(["forget"], name="f")))
    assert s.n_forget == 1 and s.entries[0].verb == "forget"
    assert not s.destructive and not s.all_accounted_safe


def test_unknown_action_combo_is_visible_not_green():
    # Exact-tuple matching: an unaudited combo must NOT classify as a benign
    # create — it shows as amber "change" and suppresses the green line.
    s = summarize_plan(_plan(_rc(["create", "read"], name="weird")))
    assert s.n_change == 1 and s.n_create == 0
    assert s.entries[0].verb == "change"
    assert not s.destructive and not s.all_accounted_safe


def test_malformed_data_row_or_unknown_mode_voids_summary():
    # Never-partial holds for EVERY row: only a WELL-FORMED data read is
    # skipped; a malformed data row or an unknown/missing mode => None.
    good = _rc(["create"], name="ok")
    bad_data = _rc("not-a-list", name="d", mode="data")
    assert summarize_plan(_plan(good, bad_data)) is None
    unknown_mode = _rc(["create"], name="m", mode="mystery")
    assert summarize_plan(_plan(good, unknown_mode)) is None
    no_mode = _rc(["create"], name="nm")
    del no_mode["mode"]
    assert summarize_plan(_plan(good, no_mode)) is None


def test_deposed_row_is_labeled():
    rc = _rc(["delete"], name="b")
    rc["deposed"] = "byebye01"
    s = summarize_plan(_plan(rc))
    e = s.entries[0]
    assert e.verb == "destroy" and e.deposed == "byebye01"


def test_truthy_non_dict_importing_voids_summary():
    rc = _rc(["no-op"], name="b")
    rc["change"]["importing"] = "yes"
    assert summarize_plan(_plan(rc)) is None


def test_unknown_type_label_falls_back_to_readable():
    s = summarize_plan(_plan(_rc(["create"], rtype="google_dataproc_cluster", name="c")))
    assert s.entries[0].type_label == "dataproc cluster"


def test_missing_resource_changes_key_is_empty_plan():
    s = summarize_plan({"format_version": "1.2"})
    assert s is not None and s.entries == ()


def test_malformed_entry_voids_whole_summary():
    # NEVER a partial summary: one bad entry => None, even with good siblings.
    good = _rc(["create"], name="ok")
    for bad in (
        "not-a-dict",
        {"address": "a.b", "mode": "managed", "type": "t", "name": "n"},  # no change
        _rc("not-a-list", name="x"),
        {**_rc(["create"], name="y"), "address": ""},
    ):
        assert summarize_plan(_plan(good, bad)) is None


def test_non_dict_plan_is_none():
    assert summarize_plan(None) is None
    assert summarize_plan([]) is None
    assert summarize_plan({"resource_changes": "nope"}) is None


def test_pathologically_deep_structures_fall_back_to_none():
    # Unbounded nesting (values or masks) must never crash or partially
    # render — RecursionError is caught by summarize_plan => None.
    # Depth note: since Python 3.12 the C json encoder recurses far beyond
    # sys.getrecursionlimit() (real C-stack checks), so the value path needs
    # to be MUCH deeper than the pure-Python limit: 200_000 raises reliably
    # (2x margin over the ~100_000 threshold observed on CPython 3.14).
    deep_val: dict = {"k": 1}
    deep_mask: dict = {"k": True}
    for _ in range(200_000):
        deep_val = {"k": deep_val}
        deep_mask = {"k": deep_mask}
    assert summarize_plan(_plan(_rc(["update"], name="b",
                                    before=deep_val, after={"k": 2}))) is None
    # The mask must MIRROR the value structure (as tofu emits it) for the
    # walk to descend into it — key "k" matches, so _mask_any really recurses.
    assert summarize_plan(_plan(_rc(["update"], name="b",
                                    before={"k": 1}, after={"k": 2},
                                    b_sens=deep_mask))) is None


# ---------------------------------------------------------------------------
# Task 2: Attribute diff — nested paths, lists, clamping
# ---------------------------------------------------------------------------

def _one(s):
    assert s is not None and len(s.entries) == 1
    return s.entries[0]


def test_update_scalar_diff_with_dotted_path():
    e = _one(summarize_plan(_plan(_rc(
        ["update"], rtype="google_cloud_run_v2_service", name="svc",
        before={"template": {"max_instance_request_concurrency": 80}},
        after={"template": {"max_instance_request_concurrency": 200}},
    ))))
    assert e.type_label == "Cloud Run service"
    (a,) = e.attr_changes
    assert a.path == "template.max_instance_request_concurrency"
    assert (a.before, a.after) == ("80", "200")


def test_update_list_index_path_and_added_key():
    e = _one(summarize_plan(_plan(_rc(
        ["update"], name="svc",
        before={"env": [{"name": "FOO", "value": "1"}]},
        after={"env": [{"name": "FOO", "value": "2"}], "labels": {"team": "ops"}},
    ))))
    paths = {a.path: (a.before, a.after) for a in e.attr_changes}
    assert paths["env[0].value"] == ('"1"', '"2"')
    # Key absent on before side: diffs as null → value at the parent path.
    assert paths["labels"] == ("null", '{"team": "ops"}')


def test_unequal_list_lengths_summarized_as_counts():
    e = _one(summarize_plan(_plan(_rc(
        ["update"], name="svc",
        before={"env": [1]}, after={"env": [1, 2, 3]},
    ))))
    (a,) = e.attr_changes
    assert a.path == "env"
    assert (a.before, a.after) == ("(1 item(s))", "(3 item(s))")


def test_long_value_clamped():
    e = _one(summarize_plan(_plan(_rc(
        ["update"], name="b", before={"v": "x" * 500}, after={"v": "y"},
    ))))
    (a,) = e.attr_changes
    assert len(a.before) <= 120 and a.before.endswith("…")


def test_unknown_after_renders_known_after_apply():
    e = _one(summarize_plan(_plan(_rc(
        ["update"], name="b",
        before={"etag": "abc"}, after={"etag": None},
        unknown={"etag": True},
    ))))
    (a,) = e.attr_changes
    assert a.unknown and a.after == "(known after apply)" and a.before == '"abc"'


def test_attr_budget_truncates_with_flag():
    before = {f"k{i:03d}": i for i in range(40)}
    after = {f"k{i:03d}": i + 1 for i in range(40)}
    e = _one(summarize_plan(_plan(_rc(["update"], name="b", before=before, after=after))))
    assert e.attrs_truncated
    assert len(e.attr_changes) == MAX_ATTRS_PER_ENTRY


# ---------------------------------------------------------------------------
# Task 3: Sensitivity masking (the critical one)
# ---------------------------------------------------------------------------

SECRET = "hunter2-super-secret"


def _assert_secret_nowhere(s):
    for e in s.entries:
        for a in e.attr_changes:
            assert SECRET not in a.before and SECRET not in a.after
            assert SECRET not in a.path
        assert SECRET not in e.location


def test_sensitive_leaf_masked_both_sides():
    e = _one(summarize_plan(_plan(_rc(
        ["update"], name="b",
        before={"password": SECRET, "x": 1},
        after={"password": "rotated-" + SECRET, "x": 1},
        b_sens={"password": True}, a_sens={"password": True},
    ))))
    (a,) = e.attr_changes
    assert a.sensitive and a.path == "password"
    assert (a.before, a.after) == ("(sensitive)", "(sensitive)")


def test_sensitive_subtree_never_descended():
    s = summarize_plan(_plan(_rc(
        ["update"], name="b",
        before={"conn": {"user": "u", "pass": SECRET}},
        after={"conn": {"user": "u2", "pass": SECRET}},
        b_sens={"conn": True}, a_sens={"conn": True},
    )))
    e = _one(s)
    (a,) = e.attr_changes
    assert a.path == "conn" and a.sensitive
    _assert_secret_nowhere(s)


def test_unknown_with_nested_sensitive_before_does_not_leak():
    # after_unknown=True at the node, before contains a sensitive leaf:
    # the before display must be masked wholesale, not json-dumped.
    s = summarize_plan(_plan(_rc(
        ["update"], name="b",
        before={"cfg": {"token": SECRET}}, after={"cfg": None},
        b_sens={"cfg": {"token": True}}, unknown={"cfg": True},
    )))
    e = _one(s)
    (a,) = e.attr_changes
    assert a.path == "cfg" and a.unknown
    assert a.before == "(sensitive)" and a.after == "(known after apply)"
    _assert_secret_nowhere(s)


def test_sensitive_unchanged_emits_nothing():
    s = summarize_plan(_plan(_rc(
        ["update"], name="b",
        before={"password": SECRET, "x": 1}, after={"password": SECRET, "x": 2},
        b_sens={"password": True}, a_sens={"password": True},
    )))
    e = _one(s)
    assert [a.path for a in e.attr_changes] == ["x"]
    _assert_secret_nowhere(s)


def test_sensitive_location_not_surfaced_on_create():
    e = _one(summarize_plan(_plan(_rc(
        ["create"], name="b", after={"location": SECRET},
        a_sens={"location": True},
    ))))
    assert e.location == ""


def test_max_depth_wholesale_respects_sensitivity():
    deep_b = {"l1": {"l2": {"l3": {"l4": {"l5": {"l6": {"l7": {"l8": {"l9": SECRET}}}}}}}}}
    deep_a = {"l1": {"l2": {"l3": {"l4": {"l5": {"l6": {"l7": {"l8": {"l9": "other"}}}}}}}}}
    sens = {"l1": {"l2": {"l3": {"l4": {"l5": {"l6": {"l7": {"l8": {"l9": True}}}}}}}}}
    s = summarize_plan(_plan(_rc(
        ["update"], name="b", before=deep_b, after=deep_a,
        b_sens=sens, a_sens=sens,
    )))
    _one(s)
    _assert_secret_nowhere(s)


# ---------------------------------------------------------------------------
# Task 4: Truncation counts + import recognition
# ---------------------------------------------------------------------------

def test_entry_cap_truncates_display_but_not_counts():
    rcs = [_rc(["create"], name=f"c{i}") for i in range(45)] + [_rc(["delete"], name="d")]
    s = summarize_plan(_plan(*rcs))
    assert len(s.entries) == MAX_ENTRIES
    assert s.n_hidden == 46 - MAX_ENTRIES
    # The destroy is beyond the display cap but MUST still be counted/warned.
    assert s.n_destroy == 1 and s.destructive


def test_import_only_change_is_import_verb_not_skipped():
    rc = _rc(["no-op"], name="adopted",
             before={"name": "adopted"}, after={"name": "adopted"})
    rc["change"]["importing"] = {"id": "projects/p/buckets/adopted"}
    e = _one(summarize_plan(_plan(rc)))
    assert e.verb == "import" and e.imported
    assert e.attr_changes == ()
    assert summarize_plan(_plan(rc)).n_import == 1


def test_import_resource_name_is_real_gcp_name_not_tf_label():
    """#168 shape: the Terraform local label carries the doubled ``adopt_``
    prefix, but the resource's real GCP name (from ``after.name``) does not.
    ``resource_name`` must be the real name so a crew never echoes the TF label
    as the resource's name (the adopt-probe-topic vs adopt_adopt_probe_topic slip)."""
    rc = _rc(["no-op"], name="adopt_adopt_probe_topic",
             before={"name": "adopt-probe-topic"}, after={"name": "adopt-probe-topic"})
    rc["change"]["importing"] = {"id": "projects/p/topics/adopt-probe-topic"}
    e = _one(summarize_plan(_plan(rc)))
    assert e.verb == "import" and e.imported
    assert e.name == "adopt_adopt_probe_topic"      # Terraform local label
    assert e.resource_name == "adopt-probe-topic"   # real GCP name


def test_import_plus_update_keeps_update_verb_with_imported_flag():
    rc = _rc(["update"], name="adopted", before={"x": 1}, after={"x": 2})
    rc["change"]["importing"] = {"id": "x"}
    s = summarize_plan(_plan(rc))
    e = _one(s)
    assert e.verb == "update" and e.imported
    assert s.n_update == 1 and s.n_import == 0


def test_action_reason_prettified():
    rc = _rc(["delete", "create"], name="r")
    rc["action_reason"] = "replace_because_cannot_update"
    e = _one(summarize_plan(_plan(rc)))
    assert e.action_reason == "replace because cannot update"


def test_unequal_list_with_positional_sensitive_mask_uses_count_display():
    # When list lengths differ, counts are emitted regardless of per-position masks.
    # The sensitive flag is False because no value is surfaced — that is correct.
    e = _one(summarize_plan(_plan(_rc(
        ["update"], name="b",
        before={"env": [SECRET]}, after={"env": [SECRET, "other"]},
        b_sens={"env": [True]}, a_sens={"env": [True, False]},
    ))))
    (a,) = e.attr_changes
    assert a.path == "env"
    assert SECRET not in a.before and SECRET not in a.after
    assert (a.before, a.after) == ("(1 item(s))", "(2 item(s))")
    # sensitive flag is False: acceptable here because the count display
    # never surfaces any value regardless of position masks.
    assert not a.sensitive


# ---------------------------------------------------------------------------
# Task (adopt-button-ui Phase 4): PlanSummary.adopt_only — drives the approval
# page's calm adoption framing (banner + reframed blast line). True IFF the
# plan does NOTHING except import (adopt). Counts are FULL-PLAN (not display-
# capped), so a mutation hidden beyond MAX_ENTRIES still falsifies the claim.
# ---------------------------------------------------------------------------

def _import_rc(name="adopted"):
    """A pure-import resource_change (["no-op"] + an importing block)."""
    rc = _rc(["no-op"], name=name,
             before={"name": name}, after={"name": name})
    rc["change"]["importing"] = {"id": f"projects/p/buckets/{name}"}
    return rc


class TestAdoptOnly:
    def test_single_import_only_is_adopt_only(self):
        s = summarize_plan(_plan(_import_rc()))
        assert s.n_import == 1 and s.adopt_only is True

    def test_two_imports_only_is_adopt_only(self):
        # adopt_only is a COUNTS claim (not a batch-policy claim) — two imports
        # and nothing else still reads as "only puts resources under management".
        s = summarize_plan(_plan(_import_rc("a"), _import_rc("b")))
        assert s.n_import == 2 and s.adopt_only is True

    def test_import_plus_create_is_not_adopt_only(self):
        s = summarize_plan(_plan(_import_rc(), _rc(["create"], name="c")))
        assert s.n_import == 1 and s.n_create == 1 and s.adopt_only is False

    def test_import_plus_update_is_not_adopt_only(self):
        s = summarize_plan(_plan(
            _import_rc(), _rc(["update"], name="u", before={"x": 1}, after={"x": 2}),
        ))
        assert s.adopt_only is False

    def test_import_plus_destroy_is_not_adopt_only(self):
        s = summarize_plan(_plan(_import_rc(), _rc(["delete"], name="d")))
        assert s.n_destroy == 1 and s.adopt_only is False

    def test_import_plus_replace_is_not_adopt_only(self):
        s = summarize_plan(_plan(_import_rc(), _rc(["delete", "create"], name="r")))
        assert s.n_replace == 1 and s.adopt_only is False

    def test_import_plus_forget_is_not_adopt_only(self):
        s = summarize_plan(_plan(_import_rc(), _rc(["forget"], name="f")))
        assert s.n_forget == 1 and s.adopt_only is False

    def test_importing_plus_update_row_is_not_adopt_only(self):
        # An ["update"] row that ALSO carries an importing block is counted as
        # update (imported=True), NOT import — the plan DOES modify the resource,
        # so adopt_only must be False even though n_import would tempt it.
        rc = _rc(["update"], name="adopted", before={"x": 1}, after={"x": 2})
        rc["change"]["importing"] = {"id": "x"}
        s = summarize_plan(_plan(rc))
        assert s.entries[0].verb == "update" and s.entries[0].imported
        assert s.n_update == 1 and s.n_import == 0
        assert s.adopt_only is False

    def test_deposed_destroy_alongside_import_is_not_adopt_only(self):
        rc = _rc(["delete"], name="d")
        rc["deposed"] = "byebye01"
        s = summarize_plan(_plan(_import_rc(), rc))
        assert s.n_destroy == 1 and s.entries[1].deposed == "byebye01"
        assert s.adopt_only is False

    def test_pure_create_is_not_adopt_only(self):
        s = summarize_plan(_plan(_rc(["create"], name="c")))
        assert s.n_import == 0 and s.adopt_only is False

    def test_empty_plan_is_not_adopt_only(self):
        s = summarize_plan(_plan())
        assert s.entries == () and s.adopt_only is False

    def test_imports_with_hidden_import_beyond_max_entries_still_adopt_only(self):
        # 42 imports: display capped at MAX_ENTRIES, n_hidden>0, but every hidden
        # row is ALSO an import → counts are full-plan import-only → still True.
        rcs = [_import_rc(f"a{i}") for i in range(MAX_ENTRIES + 2)]
        s = summarize_plan(_plan(*rcs))
        assert s.n_import == MAX_ENTRIES + 2 and s.n_hidden == 2
        assert s.adopt_only is True

    def test_mutation_hidden_beyond_max_entries_falsifies_adopt_only(self):
        # MAX_ENTRIES imports + one destroy pushed past the display cap: the
        # destroy never renders but is still COUNTED — adopt_only is full-plan,
        # so it must be False (a capped summary cannot whitewash a hidden
        # destroy). Codex test-gap pin.
        rcs = [_import_rc(f"a{i}") for i in range(MAX_ENTRIES)]
        rcs.append(_rc(["delete"], name="hidden_destroy"))
        s = summarize_plan(_plan(*rcs))
        assert s.n_hidden == 1 and s.n_destroy == 1
        assert s.adopt_only is False


# ---------------------------------------------------------------------------
# Task (ghost-nodes): resource_name extraction (Decision 4)
# ---------------------------------------------------------------------------

class TestResourceName:
    def test_create_uses_after_name(self):
        s = summarize_plan(_plan(_rc(
            ["create"], rtype="google_pubsub_topic", name="t",
            after={"name": "order-events"},
        )))
        assert s is not None
        assert s.entries[0].resource_name == "order-events"

    def test_update_prefers_before_name(self):
        s = summarize_plan(_plan(_rc(
            ["update"], rtype="google_pubsub_topic", name="t",
            before={"name": "live-name"}, after={"name": "new-name"},
        )))
        assert s.entries[0].resource_name == "live-name"

    def test_update_falls_back_to_after_when_before_has_no_name(self):
        s = summarize_plan(_plan(_rc(
            ["update"], rtype="google_pubsub_topic", name="t",
            before={}, after={"name": "n2"},
        )))
        assert s.entries[0].resource_name == "n2"

    def test_destroy_uses_before_only_never_after(self):
        # after.name must never be used for a destroy row
        s = summarize_plan(_plan(_rc(
            ["delete"], rtype="google_pubsub_topic", name="t",
            before={}, after={"name": "ghost"},
        )))
        assert s.entries[0].resource_name == ""

    def test_sensitive_name_is_never_extracted(self):
        s = summarize_plan(_plan(_rc(
            ["create"], rtype="google_pubsub_topic", name="t",
            after={"name": "secret-ish"},
            a_sens={"name": True},
        )))
        assert s.entries[0].resource_name == ""

    def test_non_string_or_empty_name_yields_empty(self):
        for bad_after in ({"name": 7}, {"name": ""}, {"name": None}, {},
                          "not-a-dict", None):
            s = summarize_plan(_plan(_rc(
                ["create"], rtype="google_pubsub_topic", name="t",
                after=bad_after,
            )))
            assert s is not None, f"summarize_plan returned None for after={bad_after!r}"
            assert s.entries[0].resource_name == "", f"expected '' for after={bad_after!r}"

    def test_unknown_after_create_yields_empty(self):
        # name "known after apply": after carries no name value
        s = summarize_plan(_plan(_rc(
            ["create"], rtype="google_pubsub_topic", name="t",
            after={},
        )))
        assert s is not None
        assert s.entries[0].resource_name == ""

    def test_per_rtype_identity_fixtures(self):
        # Realistic after.name values for the 5 identity-resolver types
        cases = [
            ("google_storage_bucket", "my-assets-bucket"),
            ("google_pubsub_topic", "order-events"),
            ("google_pubsub_subscription", "order-events-sub"),
            ("google_cloud_run_v2_service", "storefront"),
            ("google_service_account",
             "projects/my-proj/serviceAccounts/worker-sa@my-proj.iam.gserviceaccount.com"),
        ]
        for rtype, expected_name in cases:
            s = summarize_plan(_plan(_rc(
                ["create"], rtype=rtype, name="x",
                after={"name": expected_name},
            )))
            assert s is not None
            assert s.entries[0].resource_name == expected_name, (
                f"rtype={rtype}: expected {expected_name!r}, "
                f"got {s.entries[0].resource_name!r}"
            )

    def test_rtype_with_no_name_in_after_yields_empty(self):
        # A type whose after dict carries no "name" key -> ""
        s = summarize_plan(_plan(_rc(
            ["create"], rtype="google_compute_network", name="vpc",
            after={"auto_create_subnetworks": True},
        )))
        assert s is not None
        assert s.entries[0].resource_name == ""


def test_iac_plan_view_change_summary_property():
    from agent.iac_artifacts import IacPlanView

    v = IacPlanView()
    v._plan_json = _plan(_rc(["create"], name="b"))
    s = v.change_summary
    assert s is not None and s.n_create == 1
    assert v.change_summary is s  # cached

    v2 = IacPlanView()  # _plan_json stays None (unparsed / unverifiable)
    assert v2.change_summary is None
    assert "change_summary" in v2.__dict__  # the None result is cached too


# ---------------------------------------------------------------------------
# Blast-radius line (ClickOps Wave 2 item 8)
# ---------------------------------------------------------------------------


class TestTypeCounts:
    """PlanSummary.type_counts — pre-truncation aggregation sorted (-count, label)."""

    def test_multi_type_plan_sorted_by_count_desc_then_label(self):
        # 3 buckets + 1 pubsub topic → sorted by (-count, label):
        # [("Cloud Storage bucket", 3), ("Pub/Sub topic", 1)]
        rcs = (
            [_rc(["create"], rtype="google_storage_bucket", name=f"b{i}") for i in range(3)]
            + [_rc(["create"], rtype="google_pubsub_topic", name="t")]
        )
        s = summarize_plan(_plan(*rcs))
        assert s is not None
        assert s.type_counts == (
            ("Cloud Storage bucket", 3),
            ("Pub/Sub topic", 1),
        )

    def test_type_counts_survive_max_entries_truncation(self):
        # 42 creates of one type — entries capped at MAX_ENTRIES=40, but type_counts
        # must reflect the true total of 42 (computed pre-truncation).
        rcs = [_rc(["create"], rtype="google_storage_bucket", name=f"b{i}")
               for i in range(42)]
        s = summarize_plan(_plan(*rcs))
        assert s is not None
        assert len(s.entries) == MAX_ENTRIES  # display is capped
        assert s.type_counts == (("Cloud Storage bucket", 42),)  # count is total

    def test_empty_plan_yields_empty_type_counts(self):
        s = summarize_plan({"format_version": "1.2"})
        assert s is not None
        assert s.type_counts == ()

    def test_same_count_sorted_by_label_alphabetically(self):
        # Two types, equal count (1 each) → sorted by label ascending.
        rcs = [
            _rc(["create"], rtype="google_storage_bucket", name="b"),
            _rc(["create"], rtype="google_pubsub_topic", name="t"),
        ]
        s = summarize_plan(_plan(*rcs))
        assert s is not None
        labels = [label for label, _ in s.type_counts]
        assert labels == sorted(labels)


class TestPluralize:
    """_pluralize sibilant helper — exact spec-pinned cases."""

    def test_regular_noun_gets_s(self):
        assert _pluralize("Cloud Storage bucket") == "Cloud Storage buckets"
        assert _pluralize("Pub/Sub topic") == "Pub/Sub topics"

    def test_sibilant_ending_s_gets_es(self):
        # label ending in "address" (from google_compute_address fallback):
        # bare +'s' would emit "addresss" — must get "addresses".
        assert _pluralize("compute address") == "compute addresses"

    def test_sibilant_x_gets_es(self):
        assert _pluralize("compute index") == "compute indexes"

    def test_sibilant_z_gets_es(self):
        assert _pluralize("topaz") == "topazes"

    def test_sibilant_ch_gets_es(self):
        assert _pluralize("compute match") == "compute matches"

    def test_sibilant_sh_gets_es(self):
        assert _pluralize("compute mesh") == "compute meshes"

    def test_consonant_y_gets_ies(self):
        # google_artifact_registry_repository → "Artifact Registry repository"
        # → plural "Artifact Registry repositories" (not "repositorys").
        assert _pluralize("Artifact Registry repository") == "Artifact Registry repositories"

    def test_vowel_y_stays_regular(self):
        # A label ending in vowel+y (contrived; just ensures we don't over-fire)
        assert _pluralize("monkey") == "monkeys"


class TestBlastRadiusPhrase:
    """blast_radius_phrase — singular/plural/join/empty."""

    def test_empty_plan_returns_empty_string(self):
        s = summarize_plan({"format_version": "1.2"})
        assert s is not None
        assert blast_radius_phrase(s) == ""

    def test_singular_one_type(self):
        s = summarize_plan(_plan(_rc(["create"], rtype="google_pubsub_topic", name="t")))
        assert s is not None
        assert blast_radius_phrase(s) == "1 Pub/Sub topic"

    def test_plural_one_type(self):
        rcs = [_rc(["create"], rtype="google_storage_bucket", name=f"b{i}")
               for i in range(2)]
        s = summarize_plan(_plan(*rcs))
        assert s is not None
        assert blast_radius_phrase(s) == "2 Cloud Storage buckets"

    def test_multi_type_join(self):
        # 1 pubsub topic + 2 buckets → "2 Cloud Storage buckets, 1 Pub/Sub topic"
        # (sorted by -count → buckets first)
        rcs = (
            [_rc(["create"], rtype="google_storage_bucket", name=f"b{i}")
             for i in range(2)]
            + [_rc(["create"], rtype="google_pubsub_topic", name="t")]
        )
        s = summarize_plan(_plan(*rcs))
        assert s is not None
        assert blast_radius_phrase(s) == "2 Cloud Storage buckets, 1 Pub/Sub topic"

    def test_uses_plural_label_not_bare_s_for_sibilant(self):
        # google_compute_address (hypothetical) → fallback label "compute address"
        # → plural "compute addresses" (not "compute addresss").
        rcs = [
            _rc(["create"], rtype="google_compute_address", name=f"a{i}")
            for i in range(2)
        ]
        s = summarize_plan(_plan(*rcs))
        assert s is not None
        phrase = blast_radius_phrase(s)
        assert "addresses" in phrase
        assert "addresss" not in phrase

    def test_consonant_y_plural_via_artifact_registry(self):
        # google_artifact_registry_repository → "Artifact Registry repository"
        # 2 of them → "2 Artifact Registry repositories" (not "repositorys").
        rcs = [
            _rc(["create"], rtype="google_artifact_registry_repository", name=f"r{i}")
            for i in range(2)
        ]
        s = summarize_plan(_plan(*rcs))
        assert s is not None
        phrase = blast_radius_phrase(s)
        assert "repositories" in phrase
        assert "repositorys" not in phrase


class TestBlastCannotTouchNote:
    """BLAST_CANNOT_TOUCH_NOTE — honesty contract and drift pin."""

    def test_note_is_non_empty_and_mentions_denylist(self):
        assert BLAST_CANNOT_TOUCH_NOTE
        assert "denylist" in BLAST_CANNOT_TOUCH_NOTE

    def test_note_never_mentions_networks_or_databases(self):
        # The actual denylist does NOT protect "networks" or "databases" as
        # classes. These roadmap terms must never creep into the shipped copy.
        note_lower = BLAST_CANNOT_TOUCH_NOTE.lower()
        assert "networks" not in note_lower
        assert "databases" not in note_lower

    def test_rule_descriptions_key_set_drift_pin(self):
        # FORCING FUNCTION: this assertion pins the EXACT set of RULE_DESCRIPTIONS
        # keys. Any denylist rule addition/removal fails here and forces a re-review
        # of BLAST_CANNOT_TOUCH_NOTE (same honesty contract as the capability-card
        # pin, but scoped to this consumer).
        from driftscribe_lib.iac_plan_denylist import RULE_DESCRIPTIONS

        assert set(RULE_DESCRIPTIONS) == {
            "plan-json-unparseable",
            "plan-json-missing-resource-changes",
            "plan-json-malformed-change",
            "control-plane-service",
            "control-plane-sa",
            "control-plane-bucket",
            "service-managed-bucket",
            "service-managed-pubsub",
            "control-plane-secret",
            "control-plane-kms",
            "wif-config-change",
            "iam-change-forbidden-v1",
            "import-with-changes-forbidden-v1",
            "import-type-not-adoptable-v1",
            "import-mixed-plan-forbidden-v1",
            "import-batch-forbidden-v1",
            "delete-action-forbidden-v1",
            "forget-action-forbidden-v1",
            "replace-action-forbidden-v1",
            "unknown-action-forbidden-v1",
        }


def test_public_aliases_for_cost_lib():
    """Wave-4 item 13: iac_cost reuses the audited verb classification and
    sensitivity-mask walkers — public aliases, never a re-derivation."""
    from driftscribe_lib import iac_plan_summary as m

    assert m.classify_verb is m._verb
    assert m.mask_any is m._mask_any
    assert m.sub_mask is m._sub_mask
    for name in ("classify_verb", "mask_any", "sub_mask"):
        assert name in m.__all__
    # behavior smoke (the alias really is the audited function)
    assert m.classify_verb(("no-op",), True) == "import"
    assert m.classify_verb(("create",), False) == "create"
    assert m.classify_verb(("no-op",), False) is None
