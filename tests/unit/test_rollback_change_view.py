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


#: The revision every snapshot in this file claims to describe. The view binds
#: the snapshot to the approval's own target, so the two must agree unless a
#: test is deliberately breaking that.
_TARGET = "payment-demo-00015-sgt"


class _FakeApproval:
    """Only the attributes the view reads. Deliberately not the real
    :class:`~driftscribe_lib.approvals.Approval`: the view uses ``getattr``
    with a default, so it must cope with a doc that has no such field at all,
    which is every approval minted before ds-uwc."""

    def __init__(
        self,
        env_snapshot: Any = None,
        *,
        omit: bool = False,
        target_revision: str = _TARGET,
    ) -> None:
        self.target_revision = target_revision
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


#: Sentinel so a test can pass ``changed_names=None`` and mean it, rather than
#: getting the derived default.
_DERIVE = object()


def _snapshot(
    *,
    hash_: str,
    payment_changed: bool = True,
    payment_matches: bool = True,
    feature_changed: bool = False,
    feature_matches: bool = True,
    changed_names: Any = _DERIVE,
    source_revision: str = "payment-demo-00016-w9k",
) -> dict[str, Any]:
    if changed_names is _DERIVE:
        # DERIVED, not hard-coded. The view now requires the whole-env half and
        # the per-var half to agree, so a fixture that pins one while varying
        # the other would be testing an inconsistency the worker cannot produce.
        changed_names = [
            n
            for n, c in (
                ("PAYMENT_MODE", payment_changed),
                ("FEATURE_NEW_CHECKOUT", feature_changed),
            )
            if c
        ]
    return {
        "source_revision": source_revision,
        "target_revision": _TARGET,
        "observed_at": "2026-07-29T12:23:56+00:00",
        "contract_hash": hash_,
        "changed_names": changed_names,
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


@pytest.mark.parametrize(
    "field", ["changed", "target_matches_contract"], ids=["changed", "matches"]
)
@pytest.mark.parametrize("junk", ["yes", 1, "true", [1], {"a": 1}, None], ids=str)
def test_a_non_boolean_result_degrades_the_view_rather_than_reading_as_False(
    contract_path, good_hash, field, junk
):
    """Real booleans, not truthiness and not absence.

    Coercing to ``is True`` would silently turn a junk value into ``False``,
    which reads as "unchanged" and — for an allow_manual var — "fine". That is a
    clean bill produced from evidence the view never had. The honest answer is
    that this snapshot cannot be interpreted.
    """
    snap = _snapshot(hash_=good_hash)
    snap["contract_vars"]["PAYMENT_MODE"][field] = junk
    assert _rollback_change_view(_FakeApproval(snap)) == {
        "state": "unknown",
        "reason": "absent",
    }


@pytest.mark.parametrize(
    "field", ["changed", "target_matches_contract"], ids=["changed", "matches"]
)
def test_an_entry_missing_one_of_the_two_results_is_not_a_clean_bill(
    contract_path, good_hash, field
):
    """The partial-scan case, per VAR rather than per name.

    Matching key sets prove the snapshot answered the right questions; an entry
    that carries only one of the two booleans has not answered this one. Before
    the per-entry check, the missing result read as ``False`` and the page
    reported "unchanged, satisfies the contract" for a var nothing was known
    about.
    """
    snap = _snapshot(hash_=good_hash)
    del snap["contract_vars"]["FEATURE_NEW_CHECKOUT"][field]
    assert _rollback_change_view(_FakeApproval(snap)) == {
        "state": "unknown",
        "reason": "absent",
    }


@pytest.mark.parametrize(
    "entry", [["junk"], "junk", 7, {"a"}], ids=["list", "str", "int", "set"]
)
def test_a_TRUTHY_non_mapping_entry_degrades_instead_of_raising(
    contract_path, good_hash, entry
):
    """The totality guard, with a truthy value on purpose.

    A falsy non-mapping was already handled by ``... or {}`` — which is why
    testing with ``None`` proved nothing. A truthy one reaches ``.get`` and
    raises ``AttributeError``, breaking the always-200 promise the approval GET
    makes to deny a presence oracle.
    """
    snap = _snapshot(hash_=good_hash)
    snap["contract_vars"]["PAYMENT_MODE"] = entry
    assert _rollback_change_view(_FakeApproval(snap)) == {
        "state": "unknown",
        "reason": "absent",
    }


# --------------------------------------------------------------------------- #
# The snapshot must describe THIS approval's target
# --------------------------------------------------------------------------- #


def test_a_snapshot_describing_a_DIFFERENT_revision_is_not_used(
    contract_path, good_hash
):
    """Right questions, wrong subject.

    Every other check here establishes that the snapshot answered the right
    QUESTIONS — the contract's vars, against the contract's current hash. None
    of them establish that it answered them about the revision this approval
    will actually roll onto, and the acknowledgment gate is derived entirely
    from these booleans. A snapshot bound to another revision would have the
    gate judging a target nobody is about to deploy.
    """
    view = _rollback_change_view(
        _FakeApproval(
            _snapshot(hash_=good_hash, payment_matches=False),
            target_revision="payment-demo-00009-zzz",
        )
    )
    assert view == {"state": "unknown", "reason": "absent"}


def test_an_approval_with_no_target_revision_attribute_is_not_used(
    contract_path, good_hash
):
    """``getattr`` default — an approval shape without the field cannot be
    proven to match, and unproven is not permission."""

    class _NoTarget:
        env_snapshot = _snapshot(hash_=good_hash)

    assert _rollback_change_view(_NoTarget()) == {"state": "unknown", "reason": "absent"}


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
def test_a_non_list_changed_names_degrades_the_view(
    contract_path, good_hash, changed_names
):
    """``changed_names`` is the whole-env half of the observation. Quietly
    treating a malformed one as "nothing else changed" is the empty-table
    failure by another route: the page would promise that no ungoverned var
    moves, from a field it could not read."""
    assert _rollback_change_view(
        _FakeApproval(_snapshot(hash_=good_hash, changed_names=changed_names))
    ) == {"state": "unknown", "reason": "absent"}


def test_non_string_entries_in_changed_names_degrade_the_view(contract_path, good_hash):
    """Dropping the junk and rendering the rest would report a change set that
    is knowably incomplete as if it were the whole answer."""
    assert _rollback_change_view(
        _FakeApproval(
            _snapshot(
                hash_=good_hash, changed_names=["PAYMENT_MODE", "SENTRY_DSN", 7, None]
            )
        )
    ) == {"state": "unknown", "reason": "absent"}


# --------------------------------------------------------------------------- #
# The two halves of the snapshot must agree
# --------------------------------------------------------------------------- #


def test_a_var_changed_in_one_half_and_not_the_other_degrades_the_view(
    contract_path, good_hash
):
    """The contradiction case, and the worst one available: ``changed_names``
    said PAYMENT_MODE moved while its own per-var result said it did not, and
    the page rendered a clean bill with the variable appearing in neither the
    changed rows nor the ungoverned list.

    The worker derives both halves from one pair of source/target maps, so they
    always agree in practice — which is exactly why the check is cheap. This
    function exists to defend against a malformed or skewed document, and "the
    writer is careful" is not a check.
    """
    snap = _snapshot(hash_=good_hash, payment_changed=False)
    snap["changed_names"] = ["PAYMENT_MODE"]
    assert _rollback_change_view(_FakeApproval(snap)) == {
        "state": "unknown",
        "reason": "absent",
    }


def test_a_var_changed_per_var_but_absent_from_changed_names_degrades(
    contract_path, good_hash
):
    """The other direction."""
    snap = _snapshot(hash_=good_hash, payment_changed=True)
    snap["changed_names"] = []
    assert _rollback_change_view(_FakeApproval(snap)) == {
        "state": "unknown",
        "reason": "absent",
    }


def test_consistent_halves_still_render(contract_path, good_hash):
    """The guard must not reject the shape the worker actually writes."""
    snap = _snapshot(
        hash_=good_hash,
        payment_changed=True,
        feature_changed=True,
        changed_names=["PAYMENT_MODE", "FEATURE_NEW_CHECKOUT", "SENTRY_DSN"],
    )
    view = _rollback_change_view(_FakeApproval(snap))
    assert view["state"] == "ok"
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
        # TRUTHY non-mapping — a falsy one is absorbed by ``or {}`` and proves
        # nothing about totality.
        "FEATURE_NEW_CHECKOUT": ["junk"],
    }
    hostile["changed_names"] = [object()]
    hostile["source_revision"] = object()
    view = _rollback_change_view(_FakeApproval(hostile))
    assert view["state"] in {"ok", "unknown"}


def test_a_hostile_changed_names_degrades_rather_than_raising(contract_path, good_hash):
    """Non-string members reach ``isinstance`` checks, never string ops."""
    hostile = _snapshot(hash_=good_hash)
    hostile["changed_names"] = [object(), None, 7]
    assert _rollback_change_view(_FakeApproval(hostile)) == {
        "state": "unknown",
        "reason": "absent",
    }


def test_junk_in_the_purely_DISPLAY_fields_does_not_degrade_the_view(
    contract_path, good_hash
):
    """``source_revision`` and ``observed_at`` are rendered as provenance, not
    reasoned over. Junk there is ugly but does not make the change set wrong, so
    it must not throw away an otherwise sound answer — and must not raise."""
    hostile = _snapshot(hash_=good_hash)
    sentinel_source, sentinel_at = object(), object()
    hostile["source_revision"] = sentinel_source
    hostile["observed_at"] = sentinel_at
    view = _rollback_change_view(_FakeApproval(hostile))
    assert view["state"] == "ok"
    assert view["other_changed"] == []
    # Passed through, not silently blanked — the test would otherwise pass just
    # as happily against an implementation that dropped both fields.
    assert view["source_revision"] is sentinel_source
    assert view["observed_at"] is sentinel_at
