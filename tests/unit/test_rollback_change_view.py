"""Unit tests for ``agent.main._rollback_change_view`` (ds-uwc).

The function turns an approval's worker-written ``env_snapshot`` into the
"what this rollback will change" view the approval page renders.

Two properties carry the whole feature, and most of this file exists to pin
them:

1. **Unknown is not empty.** Every way the snapshot can fail to answer the
   question must render as *we could not work this out*, never as an empty
   change set — an empty table reads as a promise that nothing will change,
   and that is the one wrong answer this feature must never give.
2. **No observed env VALUE is ever in the output.** The approval page is
   reachable by anyone holding the link. The snapshot carries names and
   booleans only; the values on the page are the CONTRACT's literals, which
   are public. :func:`test_no_observed_env_value_can_reach_the_view` asserts
   that as a property of the output rather than trusting the shape upstream.

The function is also contractually total: ``approval_get`` promises an
always-200 page, so a malformed doc must degrade, never raise.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent.config import get_settings
from agent.contract import contract_hash, load_contract
from agent.main import _rollback_change_view

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

#: Mirrors the real demo contract's SHAPE: one var the operator may not touch
#: (``allow_manual_change: false``) and one they may. Both halves are needed —
#: the whole point of the view is that those two kinds of var produce different
#: operator-facing answers.
_CONTRACT_YAML = """
service: payment-demo
environment: production
cloud_run_service: payment-demo
region: asia-northeast1
github_repo: adi-prasetyo/driftscribe
expected_env:
  PAYMENT_MODE:
    value: "mock"
    docs:
      file: demo/docs/runbook.md
      section: Runtime Configuration
    allow_manual_change: false
  FEATURE_NEW_CHECKOUT:
    value: "false"
    docs:
      file: demo/docs/runbook.md
      section: Feature Flags
    allow_manual_change: true
    operator_note: "Operator-toggleable."
"""


class _FakeApproval:
    """Only the attribute the view reads. Deliberately not the real
    :class:`~driftscribe_lib.approvals.Approval`: the view uses ``getattr``
    with a default, so it must cope with a doc that has no such field at all,
    which is every approval minted before ds-uwc."""

    def __init__(self, env_snapshot: Any = None, *, omit: bool = False) -> None:
        if not omit:
            self.env_snapshot = env_snapshot


@pytest.fixture
def contract_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the coordinator's settings at a throwaway contract."""
    p = tmp_path / "ops-contract.yaml"
    p.write_text(_CONTRACT_YAML, encoding="utf-8")
    monkeypatch.setenv("CONTRACT_PATH", str(p))
    get_settings.cache_clear()
    yield p
    get_settings.cache_clear()


@pytest.fixture
def good_hash(contract_path: Path) -> str:
    return contract_hash(load_contract(contract_path))


def _snapshot(
    *,
    hash_: str,
    payment_changed: bool = True,
    payment_matches: bool = True,
    feature_changed: bool = False,
    feature_matches: bool = True,
    changed_names: Any = None,
    source_revision: str = "payment-demo-00016-w9k",
) -> dict[str, Any]:
    return {
        "source_revision": source_revision,
        "target_revision": "payment-demo-00015-sgt",
        "observed_at": "2026-07-29T12:23:56+00:00",
        "contract_hash": hash_,
        "changed_names": (
            ["PAYMENT_MODE"] if changed_names is None else changed_names
        ),
        "contract_vars": {
            "PAYMENT_MODE": {
                "changed": payment_changed,
                "target_matches_contract": payment_matches,
            },
            "FEATURE_NEW_CHECKOUT": {
                "changed": feature_changed,
                "target_matches_contract": feature_matches,
            },
        },
    }


# --------------------------------------------------------------------------- #
# Unknown ≠ empty
# --------------------------------------------------------------------------- #


def test_an_approval_with_no_snapshot_field_at_all_is_unknown(contract_path):
    """Every approval minted before ds-uwc has no such field. ``getattr``
    default, not an AttributeError, and not an empty change set."""
    view = _rollback_change_view(_FakeApproval(omit=True))
    assert view == {"state": "unknown", "reason": "absent"}


def test_a_null_snapshot_is_unknown(contract_path):
    assert _rollback_change_view(_FakeApproval(None)) == {
        "state": "unknown",
        "reason": "absent",
    }


@pytest.mark.parametrize(
    "junk",
    [
        "a string",
        ["a", "list"],
        42,
        True,
        # A doc round-tripped through something that stringified the map.
        '{"contract_vars": {}}',
    ],
    ids=["str", "list", "int", "bool", "json-string"],
)
def test_a_non_dict_snapshot_is_unknown_not_a_crash(contract_path, junk):
    """The page is contractually always-200; a malformed doc degrades."""
    assert _rollback_change_view(_FakeApproval(junk)) == {
        "state": "unknown",
        "reason": "absent",
    }


@pytest.mark.parametrize(
    "junk", [None, "PAYMENT_MODE", ["PAYMENT_MODE"], 0], ids=["none", "str", "list", "int"]
)
def test_non_dict_contract_vars_is_unknown(contract_path, good_hash, junk):
    snap = _snapshot(hash_=good_hash)
    snap["contract_vars"] = junk
    assert _rollback_change_view(_FakeApproval(snap)) == {
        "state": "unknown",
        "reason": "absent",
    }


def test_a_contract_that_cannot_be_loaded_says_so_specifically(
    tmp_path, monkeypatch, good_hash
):
    """"We could not load the contract" is not "we could not read the target
    revision" — telling the operator the wrong one sends them to the wrong
    place, so the reason is distinct."""
    monkeypatch.setenv("CONTRACT_PATH", str(tmp_path / "does-not-exist.yaml"))
    get_settings.cache_clear()
    try:
        view = _rollback_change_view(_FakeApproval(_snapshot(hash_=good_hash)))
    finally:
        get_settings.cache_clear()
    assert view == {"state": "unknown", "reason": "contract_unavailable"}


def test_a_malformed_contract_is_unavailable_not_a_crash(tmp_path, monkeypatch, good_hash):
    bad = tmp_path / "bad.yaml"
    bad.write_text("expected_env: [this is not the shape]", encoding="utf-8")
    monkeypatch.setenv("CONTRACT_PATH", str(bad))
    get_settings.cache_clear()
    try:
        view = _rollback_change_view(_FakeApproval(_snapshot(hash_=good_hash)))
    finally:
        get_settings.cache_clear()
    assert view == {"state": "unknown", "reason": "contract_unavailable"}


# --------------------------------------------------------------------------- #
# The mid-rollout state: a snapshot recorded with no contract information
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("missing", ["", None, "absent-key"], ids=["empty", "null", "missing"])
def test_a_snapshot_with_no_contract_hash_reads_as_not_recorded_not_as_changed(
    contract_path, good_hash, missing
):
    """The worker MUST deploy before the coordinator (``ProposeRequest`` is
    ``extra="forbid"``), so in the rollout window the new worker records
    snapshots for an old coordinator that sends no contract at all:
    ``contract_vars={}``, empty hash.

    Those must read as *not recorded*. Reporting "the ops contract has changed
    since this proposal was recorded" would be a statement about an edit that
    never happened, and would send the operator looking for it.
    """
    snap = _snapshot(hash_=good_hash)
    snap["contract_vars"] = {}
    if missing == "absent-key":
        snap.pop("contract_hash")
    else:
        snap["contract_hash"] = missing
    assert _rollback_change_view(_FakeApproval(snap)) == {
        "state": "unknown",
        "reason": "absent",
    }


# --------------------------------------------------------------------------- #
# Coverage is checked, not assumed
# --------------------------------------------------------------------------- #


def test_a_var_the_snapshot_never_inspected_degrades_the_whole_view(contract_path, good_hash):
    """A partial scan must not report a clean bill. If the contract declares a
    var the snapshot has no answer for, the view is unknown — not "ok, and that
    var is fine"."""
    snap = _snapshot(hash_=good_hash)
    del snap["contract_vars"]["FEATURE_NEW_CHECKOUT"]
    assert _rollback_change_view(_FakeApproval(snap)) == {
        "state": "unknown",
        "reason": "contract_changed",
    }


def test_a_snapshot_var_the_contract_no_longer_declares_also_degrades(contract_path, good_hash):
    """The other direction: a var was dropped from the contract after the
    snapshot was taken. The comparison no longer lines up either way."""
    snap = _snapshot(hash_=good_hash)
    snap["contract_vars"]["RETIRED_VAR"] = {
        "changed": False,
        "target_matches_contract": True,
    }
    assert _rollback_change_view(_FakeApproval(snap)) == {
        "state": "unknown",
        "reason": "contract_changed",
    }


def test_a_contract_whose_VALUES_moved_degrades_even_when_the_names_match(
    contract_path, good_hash
):
    """The key-set check cannot see this: same var names, different expected
    value. Only the hash catches it, and without it the page would render
    ``target_matches_contract`` booleans computed against a contract that no
    longer exists."""
    contract_path.write_text(
        _CONTRACT_YAML.replace('value: "mock"', 'value: "sandbox"'), encoding="utf-8"
    )
    assert _rollback_change_view(_FakeApproval(_snapshot(hash_=good_hash))) == {
        "state": "unknown",
        "reason": "contract_changed",
    }


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #


def test_a_matching_snapshot_renders_a_row_per_declared_var(contract_path, good_hash):
    view = _rollback_change_view(_FakeApproval(_snapshot(hash_=good_hash)))
    assert view["state"] == "ok"
    # Sorted, so the page order does not depend on dict insertion order.
    assert [r["name"] for r in view["rows"]] == [
        "FEATURE_NEW_CHECKOUT",
        "PAYMENT_MODE",
    ]
    assert view["source_revision"] == "payment-demo-00016-w9k"
    assert view["observed_at"] == "2026-07-29T12:23:56+00:00"


def test_the_contract_value_shown_is_the_contracts_own_literal(contract_path, good_hash):
    """The reference value on the page comes from the contract (public), never
    from anything observed."""
    view = _rollback_change_view(_FakeApproval(_snapshot(hash_=good_hash)))
    by_name = {r["name"]: r for r in view["rows"]}
    assert by_name["PAYMENT_MODE"]["contract_value"] == "mock"
    assert by_name["FEATURE_NEW_CHECKOUT"]["contract_value"] == "false"


def test_a_target_that_misses_a_disallow_manual_var_sets_violates(contract_path, good_hash):
    """The whole point of the acknowledgment: rolling onto this target removes
    one drift and introduces another."""
    view = _rollback_change_view(
        _FakeApproval(_snapshot(hash_=good_hash, payment_matches=False))
    )
    assert view["state"] == "ok"
    assert view["violates"] is True


def test_an_allow_manual_var_missing_the_contract_value_does_NOT_violate(
    contract_path, good_hash
):
    """``allow_manual_change=true`` means the operator is entitled to move it,
    so a target that does not hold the contract literal for that var is not a
    violation and must not demand an acknowledgment. Same discrimination the
    ds-b3m rollback gate makes, on the other side of the flow."""
    view = _rollback_change_view(
        _FakeApproval(_snapshot(hash_=good_hash, feature_matches=False))
    )
    assert view["state"] == "ok"
    assert view["violates"] is False
    by_name = {r["name"]: r for r in view["rows"]}
    # It is still SHOWN — the operator sees the mismatch, it just does not gate.
    assert by_name["FEATURE_NEW_CHECKOUT"]["target_matches_contract"] is False


def test_reverts_operator_change_flags_only_a_CHANGED_operator_managed_var(
    contract_path, good_hash
):
    """The blast-radius answer ds-b3m could not give. Both halves are load
    bearing: an unchanged operator var is not being reverted, and a changed
    var the operator may not touch is not "their" change."""
    view = _rollback_change_view(
        _FakeApproval(
            _snapshot(hash_=good_hash, feature_changed=True, payment_changed=True)
        )
    )
    by_name = {r["name"]: r for r in view["rows"]}
    assert by_name["FEATURE_NEW_CHECKOUT"]["reverts_operator_change"] is True
    assert by_name["PAYMENT_MODE"]["reverts_operator_change"] is False

    unchanged = _rollback_change_view(
        _FakeApproval(_snapshot(hash_=good_hash, feature_changed=False))
    )
    by_name = {r["name"]: r for r in unchanged["rows"]}
    assert by_name["FEATURE_NEW_CHECKOUT"]["reverts_operator_change"] is False


# --------------------------------------------------------------------------- #
# Booleans must be booleans
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("truthy", ["yes", 1, "true", [1], {"a": 1}], ids=str)
def test_a_truthy_non_boolean_changed_does_not_read_as_changed(
    contract_path, good_hash, truthy
):
    """``is True``, not truthiness. A hand-edited or older doc must not be able
    to assert a boolean the worker never computed."""
    snap = _snapshot(hash_=good_hash)
    snap["contract_vars"]["PAYMENT_MODE"]["changed"] = truthy
    view = _rollback_change_view(_FakeApproval(snap))
    by_name = {r["name"]: r for r in view["rows"]}
    assert by_name["PAYMENT_MODE"]["changed"] is False


@pytest.mark.parametrize("truthy", ["yes", 1, "true"], ids=str)
def test_a_truthy_non_boolean_match_does_not_satisfy_the_contract(
    contract_path, good_hash, truthy
):
    """This one fails CLOSED and that is the right direction: a value that is
    not literally ``True`` does not prove the target satisfies the contract, so
    it counts as a violation and asks for the acknowledgment."""
    snap = _snapshot(hash_=good_hash)
    snap["contract_vars"]["PAYMENT_MODE"]["target_matches_contract"] = truthy
    view = _rollback_change_view(_FakeApproval(snap))
    assert view["violates"] is True


def test_an_entry_that_is_not_a_mapping_at_all_degrades_to_violates(
    contract_path, good_hash
):
    """``snapshot_vars.get(name) or {}`` — a junk entry yields no positive
    proof, so the disallow-manual var reads as unproven and gates."""
    snap = _snapshot(hash_=good_hash)
    snap["contract_vars"]["PAYMENT_MODE"] = None
    view = _rollback_change_view(_FakeApproval(snap))
    assert view["state"] == "ok"
    assert view["violates"] is True


# --------------------------------------------------------------------------- #
# Vars outside the contract
# --------------------------------------------------------------------------- #


def test_changed_vars_outside_the_contract_are_listed_separately(contract_path, good_hash):
    """A rollback reverts the target's ENTIRE env, so vars the contract says
    nothing about still move. Surfacing them by name is the point."""
    view = _rollback_change_view(
        _FakeApproval(
            _snapshot(
                hash_=good_hash,
                changed_names=["PAYMENT_MODE", "SENTRY_DSN", "LOG_LEVEL"],
            )
        )
    )
    # Contract-governed names are rendered in the table above, not here.
    assert view["other_changed"] == ["LOG_LEVEL", "SENTRY_DSN"]


@pytest.mark.parametrize(
    "changed_names",
    [None, "PAYMENT_MODE", 7, {"PAYMENT_MODE": True}],
    ids=["none", "str", "int", "dict"],
)
def test_a_non_list_changed_names_yields_no_other_changed(
    contract_path, good_hash, changed_names
):
    view = _rollback_change_view(
        _FakeApproval(_snapshot(hash_=good_hash, changed_names=changed_names))
    )
    assert view["state"] == "ok"
    assert view["other_changed"] == []


def test_non_string_entries_in_changed_names_are_dropped(contract_path, good_hash):
    view = _rollback_change_view(
        _FakeApproval(
            _snapshot(hash_=good_hash, changed_names=["SENTRY_DSN", 7, None, {"x": 1}])
        )
    )
    assert view["other_changed"] == ["SENTRY_DSN"]


# --------------------------------------------------------------------------- #
# The security property
# --------------------------------------------------------------------------- #


def test_no_observed_env_value_can_reach_the_view(contract_path, good_hash):
    """The property, asserted on the OUTPUT rather than trusted from the shape.

    A worker that started recording observed values — or a hand-written doc
    carrying them — must not be able to get them onto a page anyone holding the
    link can read. The view is built from the contract's own key set, so
    anything the snapshot adds is dropped; this pins that.
    """
    snap = _snapshot(hash_=good_hash)
    # Every place a value could plausibly be smuggled in.
    snap["contract_vars"]["PAYMENT_MODE"]["source_value"] = "sk_live_LEAKED"
    snap["contract_vars"]["PAYMENT_MODE"]["target_value"] = "sk_live_LEAKED"
    snap["source_env"] = {"PAYMENT_MODE": "sk_live_LEAKED"}
    snap["target_env"] = {"PAYMENT_MODE": "sk_live_LEAKED"}
    snap["changed_names"] = ["PAYMENT_MODE", "sk_live_LEAKED"]

    view = _rollback_change_view(_FakeApproval(snap))
    assert view["state"] == "ok"
    # `changed_names` is operator-facing by design, so a value smuggled in as a
    # NAME would surface. That is the one channel with a legitimate reason to
    # echo snapshot strings, and it is why the worker builds it from env keys.
    rendered = repr({k: v for k, v in view.items() if k != "other_changed"})
    assert "sk_live_LEAKED" not in rendered
    # The row dicts carry exactly the keys the template reads — nothing extra
    # rides along from the snapshot.
    assert {frozenset(r) for r in view["rows"]} == {
        frozenset(
            {
                "name",
                "changed",
                "target_matches_contract",
                "contract_value",
                "allow_manual_change",
                "reverts_operator_change",
            }
        )
    }


def test_the_view_never_raises_on_an_arbitrarily_hostile_snapshot(contract_path, good_hash):
    """``approval_get`` promises always-200. Whatever is in the doc, this
    returns a dict with a ``state``."""
    hostile = _snapshot(hash_=good_hash)
    hostile["contract_vars"] = {
        "PAYMENT_MODE": {"changed": object(), "target_matches_contract": object()},
        "FEATURE_NEW_CHECKOUT": [],
    }
    hostile["changed_names"] = [object()]
    hostile["source_revision"] = object()
    view = _rollback_change_view(_FakeApproval(hostile))
    assert view["state"] in {"ok", "unknown"}
