"""Unit tests for the rollback worker's value-free change snapshot (ds-uwc).

``_revision_env_states`` reads one revision's env into tagged states, and
``_env_change_snapshot`` compares the ACTIVE revision to the proposed target so
the approval page can say what the rollback would actually do.

Three properties are load-bearing:

- **Secret references are compared, never displayed.** The shared
  ``_extract_env_from_containers`` SKIPS ``value_source`` entries, which is
  right for "what is the live value" and wrong here: two revisions pointing the
  same var at DIFFERENT secrets would both read as absent and compare equal,
  reporting "no change" for a change.
- **None, never a partial answer.** If either revision cannot be read the
  snapshot is ``None``, because an empty change set is indistinguishable from
  "nothing will change".
- **No observed env VALUE is returned.** The snapshot carries names, booleans
  and the two revision names. The approval page is reachable by anyone holding
  the link, and "declared in the contract" does not make a var's CURRENT value
  public.
"""
from __future__ import annotations


import pytest
from workers._testenv import import_worker_main

# Canonical boot env, applied before the import below. The values live in
# workers/_testenv.py, not here: worker mains capture config at import and
# Python caches modules, so the FIRST importer in the pytest process decides
# them for everyone (ds-2n1).
import_worker_main("workers.rollback.main")

import workers.rollback.main as m  # noqa: E402


# --------------------------------------------------------------------------- #
# Fake Cloud Run protos — attribute shapes only, matching what the worker reads
# --------------------------------------------------------------------------- #


class _SecretKeyRef:
    def __init__(self, secret: str, version: str) -> None:
        self.secret = secret
        self.version = version


class _ValueSource:
    def __init__(self, secret: str, version: str) -> None:
        self.secret_key_ref = _SecretKeyRef(secret, version)


class _EnvVar:
    def __init__(
        self, name: str, value: str | None = None, secret: tuple[str, str] | None = None
    ) -> None:
        self.name = name
        self.value = value
        self.value_source = _ValueSource(*secret) if secret else None


class _Container:
    def __init__(self, env: list[_EnvVar]) -> None:
        self.env = env


class _Revision:
    def __init__(self, containers: list[_Container]) -> None:
        self.containers = containers


class _FakeRevisionsClient:
    """``get_revision(name=...)`` keyed by the trailing revision name."""

    def __init__(self, revisions: dict[str, _Revision]) -> None:
        self._revisions = revisions
        self.calls: list[str] = []

    def get_revision(self, name: str) -> _Revision:
        short = name.rsplit("/", 1)[-1]
        self.calls.append(short)
        if short not in self._revisions:
            raise RuntimeError(f"revision not found: {short}")
        return self._revisions[short]


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch):
    """Returns a callable that installs a fake revisions client."""

    def _wire(revisions: dict[str, _Revision]) -> _FakeRevisionsClient:
        client = _FakeRevisionsClient(revisions)
        monkeypatch.setattr(m, "_get_revisions_client", lambda: client)
        monkeypatch.setattr(
            m,
            "_service_name",
            lambda: "projects/p/locations/asia-northeast1/services/payment-demo",
        )
        return client

    return _wire


def _plain(**kv: str) -> _Revision:
    return _Revision([_Container([_EnvVar(n, value=v) for n, v in kv.items()])])


# --------------------------------------------------------------------------- #
# _revision_env_states
# --------------------------------------------------------------------------- #


def test_a_plain_value_is_tagged_plain():
    states = m._revision_env_states(_plain(PAYMENT_MODE="mock").containers)
    assert states == {"PAYMENT_MODE": ("plain", "mock")}


def test_a_secret_backed_var_is_tagged_with_its_REFERENCE_not_its_value():
    """The reference is what makes two secret-backed revisions comparable. The
    resolved value is never fetched, so it cannot be stored or displayed."""
    rev = _Revision([_Container([_EnvVar("API_KEY", secret=("payment-api-key", "3"))])])
    assert m._revision_env_states(rev.containers) == {
        "API_KEY": ("secret", "payment-api-key/3")
    }


def test_two_revisions_pointing_one_var_at_DIFFERENT_secrets_compare_as_changed(wire):
    """The bug the tagging exists to prevent. ``_extract_env_from_containers``
    skips ``value_source``, so under that extractor both revisions would read as
    absent, compare equal, and the page would report "no change" for a change of
    credential."""
    wire(
        {
            "src": _Revision(
                [_Container([_EnvVar("API_KEY", secret=("payment-api-key", "3"))])]
            ),
            "tgt": _Revision(
                [_Container([_EnvVar("API_KEY", secret=("payment-api-key", "1"))])]
            ),
        }
    )
    snap = m._env_change_snapshot(
        source_revision="src", target_revision="tgt", contract_env=None, contract_hash=None
    )
    assert snap is not None
    assert snap["changed_names"] == ["API_KEY"]


def test_a_var_that_moved_between_plain_and_secret_is_changed(wire):
    """A deploy that moved a var from a literal into Secret Manager (or back)
    is a real change to what the process sees, and must not read as unchanged."""
    wire(
        {
            "src": _plain(PAYMENT_MODE="mock"),
            "tgt": _Revision(
                [_Container([_EnvVar("PAYMENT_MODE", secret=("payment-mode", "latest"))])]
            ),
        }
    )
    snap = m._env_change_snapshot(
        source_revision="src", target_revision="tgt", contract_env=None, contract_hash=None
    )
    assert snap["changed_names"] == ["PAYMENT_MODE"]


def test_an_empty_string_value_is_present_not_absent():
    """``ev.value or ""`` — an explicitly empty var is set, and differs from a
    var that is not set at all."""
    rev = _Revision([_Container([_EnvVar("FLAG", value="")])])
    assert m._revision_env_states(rev.containers) == {"FLAG": ("plain", "")}


def test_later_containers_win_matching_the_shared_extractor():
    """Last-one-wins across containers, mirroring
    ``_extract_env_from_containers``. Pinned so the two cannot silently diverge
    on a multi-container service."""
    rev = _Revision(
        [
            _Container([_EnvVar("A", value="first")]),
            _Container([_EnvVar("A", value="second")]),
        ]
    )
    assert m._revision_env_states(rev.containers) == {"A": ("plain", "second")}


# --------------------------------------------------------------------------- #
# _env_change_snapshot — None, never partial
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("source", "target"),
    [("", "tgt"), ("src", ""), ("", "")],
    ids=["no-source", "no-target", "neither"],
)
def test_a_missing_revision_name_yields_None(wire, source, target):
    wire({"src": _plain(A="1"), "tgt": _plain(A="1")})
    assert (
        m._env_change_snapshot(
            source_revision=source,
            target_revision=target,
            contract_env=None,
            contract_hash=None,
        )
        is None
    )


@pytest.mark.parametrize("missing", ["src", "tgt"], ids=["source-unreadable", "target-unreadable"])
def test_an_unreadable_revision_yields_None_not_an_empty_change_set(wire, missing):
    """The single most important behavior here. An empty ``changed_names``
    renders as "nothing will change" — a promise we cannot make when we could
    not read one of the two revisions."""
    revisions = {"src": _plain(A="1"), "tgt": _plain(A="2")}
    del revisions[missing]
    wire(revisions)
    assert (
        m._env_change_snapshot(
            source_revision="src",
            target_revision="tgt",
            contract_env=None,
            contract_hash=None,
        )
        is None
    )


def test_a_preview_failure_never_propagates_out_of_the_snapshot(monkeypatch):
    """A preview must not be able to break the proposal it describes."""

    def boom():
        raise RuntimeError("cloud run unavailable")

    monkeypatch.setattr(m, "_get_revisions_client", boom)
    monkeypatch.setattr(m, "_service_name", lambda: "svc")
    assert (
        m._env_change_snapshot(
            source_revision="src",
            target_revision="tgt",
            contract_env=None,
            contract_hash=None,
        )
        is None
    )


# --------------------------------------------------------------------------- #
# The change set and the contract booleans
# --------------------------------------------------------------------------- #


def test_identical_revisions_report_no_changed_names(wire):
    wire({"src": _plain(A="1", B="2"), "tgt": _plain(A="1", B="2")})
    snap = m._env_change_snapshot(
        source_revision="src", target_revision="tgt", contract_env=None, contract_hash=None
    )
    assert snap["changed_names"] == []


def test_a_var_present_on_only_one_side_is_changed(wire):
    """Set-union, not intersection: a var the rollback would ADD or REMOVE is a
    change to the running config."""
    wire({"src": _plain(A="1", ONLY_ON_SOURCE="x"), "tgt": _plain(A="1", ONLY_ON_TARGET="y")})
    snap = m._env_change_snapshot(
        source_revision="src", target_revision="tgt", contract_env=None, contract_hash=None
    )
    assert snap["changed_names"] == ["ONLY_ON_SOURCE", "ONLY_ON_TARGET"]


def test_contract_compliance_is_scanned_for_EVERY_declared_var(wire):
    """Not just the change set. If source and target hold the SAME violating
    value the var never appears in ``changed_names`` at all — and that is
    exactly the case where the operator most needs to be told the target does
    not satisfy the contract."""
    wire(
        {
            "src": _plain(PAYMENT_MODE="live", FEATURE="false"),
            "tgt": _plain(PAYMENT_MODE="live", FEATURE="true"),
        }
    )
    snap = m._env_change_snapshot(
        source_revision="src",
        target_revision="tgt",
        contract_env={"PAYMENT_MODE": "mock", "FEATURE": "false"},
        contract_hash="h1",
    )
    assert snap["changed_names"] == ["FEATURE"]
    assert snap["contract_vars"]["PAYMENT_MODE"] == {
        "changed": False,
        "target_matches_contract": False,
    }
    assert snap["contract_vars"]["FEATURE"]["target_matches_contract"] is False


def test_a_secret_backed_target_never_claims_to_match_the_contract(wire):
    """``target.get(name) == ("plain", expected)`` — a secret-backed var cannot
    be observed to hold the contract literal, so it reports False. Correct:
    neither a secret reference nor an absent var IS observably the declared
    value, and the page must not assert compliance it cannot see."""
    wire(
        {
            "src": _plain(PAYMENT_MODE="live"),
            "tgt": _Revision(
                [_Container([_EnvVar("PAYMENT_MODE", secret=("payment-mode", "latest"))])]
            ),
        }
    )
    snap = m._env_change_snapshot(
        source_revision="src",
        target_revision="tgt",
        contract_env={"PAYMENT_MODE": "mock"},
        contract_hash="h1",
    )
    assert snap["contract_vars"]["PAYMENT_MODE"]["target_matches_contract"] is False


def test_a_var_absent_from_the_target_does_not_match_the_contract(wire):
    wire({"src": _plain(PAYMENT_MODE="live"), "tgt": _plain(OTHER="x")})
    snap = m._env_change_snapshot(
        source_revision="src",
        target_revision="tgt",
        contract_env={"PAYMENT_MODE": "mock"},
        contract_hash="h1",
    )
    assert snap["contract_vars"]["PAYMENT_MODE"]["target_matches_contract"] is False


def test_no_contract_yields_empty_contract_vars_and_an_empty_hash(wire):
    """The old-coordinator case. The worker still records the change set; the
    coordinator renders the whole view as unknown because it cannot judge
    contract compliance from it."""
    wire({"src": _plain(A="1"), "tgt": _plain(A="2")})
    snap = m._env_change_snapshot(
        source_revision="src", target_revision="tgt", contract_env=None, contract_hash=None
    )
    assert snap["contract_vars"] == {}
    assert snap["contract_hash"] == ""
    assert snap["changed_names"] == ["A"]


# --------------------------------------------------------------------------- #
# The security property
# --------------------------------------------------------------------------- #


def test_the_snapshot_contains_no_observed_env_value(wire):
    """Asserted over the whole serialized structure, not per field — a future
    field that started carrying a value would fail here rather than reaching a
    page anyone holding the link can read."""
    wire(
        {
            "src": _plain(PAYMENT_MODE="sk_live_SOURCE", OTHER="plain-source"),
            "tgt": _plain(PAYMENT_MODE="sk_live_TARGET", OTHER="plain-target"),
        }
    )
    snap = m._env_change_snapshot(
        source_revision="src",
        target_revision="tgt",
        contract_env={"PAYMENT_MODE": "mock"},
        contract_hash="h1",
    )
    blob = repr(snap)
    assert "sk_live_SOURCE" not in blob
    assert "sk_live_TARGET" not in blob
    assert "plain-source" not in blob
    assert "plain-target" not in blob
    # Names ARE present — that is the operator-facing payload.
    assert "PAYMENT_MODE" in blob


def test_a_secret_REFERENCE_is_not_echoed_into_the_snapshot(wire):
    """The reference is used for the comparison and then dropped. It names a
    Secret Manager resource, which is infrastructure detail the approval page
    has no reason to publish."""
    wire(
        {
            "src": _Revision(
                [_Container([_EnvVar("API_KEY", secret=("prod-signing-key", "7"))])]
            ),
            "tgt": _Revision(
                [_Container([_EnvVar("API_KEY", secret=("prod-signing-key", "6"))])]
            ),
        }
    )
    snap = m._env_change_snapshot(
        source_revision="src", target_revision="tgt", contract_env=None, contract_hash=None
    )
    blob = repr(snap)
    assert "prod-signing-key" not in blob
    assert snap["changed_names"] == ["API_KEY"]


def test_the_snapshot_records_which_two_revisions_were_compared(wire):
    """Without both names the page cannot say what the comparison was against,
    and a stale snapshot would be indistinguishable from a fresh one."""
    wire({"src": _plain(A="1"), "tgt": _plain(A="2")})
    snap = m._env_change_snapshot(
        source_revision="src", target_revision="tgt", contract_env=None, contract_hash=None
    )
    assert snap["source_revision"] == "src"
    assert snap["target_revision"] == "tgt"
    assert snap["observed_at"]
