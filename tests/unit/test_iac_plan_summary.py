"""Unit tests for driftscribe_lib.iac_plan_summary (ClickOps roadmap W1-1).

The summary is ADVISORY DISPLAY ONLY, but its failure modes are safety-shaped:
- never a partial summary (any unparseable entry => None, not a shorter list);
- sensitive values must never appear in any output string;
- counts are computed over ALL entries, truncation trims only the display list.
"""
from __future__ import annotations

from driftscribe_lib.iac_plan_summary import (
    MAX_ATTRS_PER_ENTRY,  # noqa: F401 — public-API contract, exercised in later tasks
    MAX_ENTRIES,  # noqa: F401 — public-API contract, exercised in later tasks
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
