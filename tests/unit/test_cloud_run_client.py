from unittest.mock import MagicMock

from google.cloud import run_v2

from agent.cloud_run_client import read_live_env
from driftscribe_lib.cloud_run import read_live_state

_TYPE_REVISION = run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION
_TYPE_LATEST = run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST


def _env_var(name, value):
    m = MagicMock()
    m.name = name
    m.value = value
    m.value_source = None
    return m


def _secret_env_var(name):
    """Simulates a Cloud Run env entry backed by Secret Manager (value_source set)."""
    m = MagicMock()
    m.name = name
    m.value = ""
    m.value_source = MagicMock()  # truthy
    return m


def _traffic_entry(revision: str, percent: int, tag: str = "", type_=_TYPE_REVISION):
    """Build a fake Cloud Run v2 traffic-entry mock.

    Used for both ``TrafficTarget`` (``svc.traffic``) and
    ``TrafficTargetStatus`` (``svc.traffic_statuses``) — both protos
    expose ``type_`` / ``revision`` / ``percent`` / ``tag``.

    ``revision`` is the SHORT name (``"payment-demo-00007-abc"``), matching
    how the real proto exposes the field (see
    ``google.cloud.run_v2.types.TrafficTarget``). Source-specific semantics
    differ for the LATEST sentinel:

    - On ``TrafficTarget`` (``svc.traffic``):
      ``type_=TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST`` (or empty ``revision``)
      means "send to ``latest_ready_revision``".
    - On ``TrafficTargetStatus`` (``svc.traffic_statuses``): a populated
      ``.revision`` is always the observed resolved target (trust it).
      An empty ``.revision`` is "no observed serving state yet" — the
      caller falls through to ``svc.traffic`` or ``latest_ready_revision``.
    """
    m = MagicMock()
    m.revision = revision
    m.percent = percent
    m.tag = tag
    m.type_ = type_
    return m


def _set_serving(svc, *, statuses=None, traffic=None):
    """Attach observed/desired traffic to a ``svc`` mock.

    ``read_live_state`` prefers ``svc.traffic_statuses`` (observed) and
    falls back to ``svc.traffic`` (desired); most tests want the same
    list on both fields so the test passes regardless of which path the
    implementation takes. Tests that care about the
    statuses-vs-traffic distinction pass them explicitly.
    """
    if statuses is None and traffic is not None:
        statuses = traffic
    elif traffic is None and statuses is not None:
        traffic = statuses
    svc.traffic_statuses = statuses or []
    svc.traffic = traffic or []


def test_read_live_env_extracts_env_block():
    client = MagicMock()
    container = MagicMock()
    container.env = [_env_var("PAYMENT_MODE", "live"), _env_var("FEATURE_X", "true")]
    svc = MagicMock()
    svc.template.containers = [container]
    client.get_service.return_value = svc

    env = read_live_env("payment-demo", "asia-northeast1", "p", client=client)
    assert env == {"PAYMENT_MODE": "live", "FEATURE_X": "true"}


def test_read_live_env_skips_value_source_secrets():
    client = MagicMock()
    secret = _secret_env_var("DB_PASSWORD")
    plain = _env_var("PAYMENT_MODE", "live")
    container = MagicMock()
    container.env = [secret, plain]
    svc = MagicMock()
    svc.template.containers = [container]
    client.get_service.return_value = svc

    env = read_live_env("s", "r", "p", client=client)
    assert "DB_PASSWORD" not in env
    assert env["PAYMENT_MODE"] == "live"


def test_read_live_env_keeps_legitimately_empty_string_value():
    # Empty string is a valid Cloud Run env value (e.g. EMPTY_FLAG=""), not a secret
    client = MagicMock()
    container = MagicMock()
    container.env = [_env_var("EMPTY_FLAG", ""), _env_var("PAYMENT_MODE", "mock")]
    svc = MagicMock()
    svc.template.containers = [container]
    client.get_service.return_value = svc

    env = read_live_env("s", "r", "p", client=client)
    assert env == {"EMPTY_FLAG": "", "PAYMENT_MODE": "mock"}


def test_read_live_state_reads_env_from_traffic_serving_revision():
    """Phase 14 / W3: env must come from the *traffic-serving* revision,
    NOT from ``latest_ready_revision``. After a successful rollback, the
    actual traffic-serving revision is OLDER than the latest ready (the
    newer revision is ready but not receiving traffic). Reading env from
    latest_ready_revision would loop the reconciler forever.

    To pin the contract: set ``svc.latest_ready_revision`` to a DIFFERENT
    (newer, not-serving) revision than what the traffic block says is
    receiving traffic, and assert the function picks the traffic one.
    """
    svc_client = MagicMock()
    rev_client = MagicMock()
    served_short = "payment-demo-00007-abc"
    served_path = (
        f"projects/p/locations/r/services/payment-demo/revisions/{served_short}"
    )
    # Newer revision is ready but receives 0% — should be ignored.
    not_serving_path = (
        "projects/p/locations/r/services/payment-demo/revisions/"
        "payment-demo-00009-xyz"
    )

    # Service template has the NEXT-deploy env (different from the served one
    # to make the bug visible if the function reads the wrong place).
    template_container = MagicMock()
    template_container.env = [_env_var("PAYMENT_MODE", "live-NEW")]
    svc = MagicMock()
    svc.template.containers = [template_container]
    svc.latest_ready_revision = not_serving_path
    _set_serving(svc, statuses=[_traffic_entry(served_short, 100)])
    svc_client.get_service.return_value = svc

    # Revision (the actually-serving one) has the OLD env.
    rev_container = MagicMock()
    rev_container.env = [_env_var("PAYMENT_MODE", "mock"), _env_var("FEATURE_X", "0")]
    rev = MagicMock()
    rev.containers = [rev_container]
    rev_client.get_revision.return_value = rev

    state = read_live_state(
        "payment-demo", "r", "p",
        services_client=svc_client,
        revisions_client=rev_client,
    )
    assert state["env"] == {"PAYMENT_MODE": "mock", "FEATURE_X": "0"}
    assert state["revision"] == served_short
    # The function must build the FULL revision path from the short name.
    rev_client.get_revision.assert_called_once_with(name=served_path)


def test_read_live_state_prefers_traffic_statuses_over_traffic():
    """``svc.traffic`` is the *configured* state; ``svc.traffic_statuses``
    is the *observed* / serving state. During reconciliation (and
    immediately after Eventarc fires) the two can disagree: the desired
    100% target may already be the new revision while the world is still
    seeing the old one. Drift detection cares about what the world is
    seeing — so ``traffic_statuses`` wins."""
    svc_client = MagicMock()
    rev_client = MagicMock()
    desired_short = "payment-demo-00009-bbb"  # in traffic, NOT yet serving
    observed_short = "payment-demo-00007-aaa"  # what the world actually sees
    observed_path = (
        f"projects/p/locations/r/services/payment-demo/revisions/{observed_short}"
    )

    svc = MagicMock()
    svc.template.containers = []
    svc.latest_ready_revision = (
        "projects/p/locations/r/services/payment-demo/revisions/payment-demo-00099-zzz"
    )
    svc.traffic = [_traffic_entry(desired_short, 100)]
    svc.traffic_statuses = [_traffic_entry(observed_short, 100)]
    svc_client.get_service.return_value = svc

    rev_container = MagicMock()
    rev_container.env = [_env_var("PAYMENT_MODE", "mock")]
    rev = MagicMock()
    rev.containers = [rev_container]
    rev_client.get_revision.return_value = rev

    state = read_live_state(
        "payment-demo", "r", "p",
        services_client=svc_client,
        revisions_client=rev_client,
    )
    assert state["revision"] == observed_short
    rev_client.get_revision.assert_called_once_with(name=observed_path)


def test_read_live_state_falls_back_to_traffic_when_statuses_empty():
    """A brand-new service may not have populated ``traffic_statuses`` yet
    (transient state per the Cloud Run v2 proto docs). Falling back to
    ``svc.traffic`` keeps the function useful for first-deploy services."""
    svc_client = MagicMock()
    rev_client = MagicMock()
    served_short = "payment-demo-00001-abc"
    served_path = (
        f"projects/p/locations/r/services/payment-demo/revisions/{served_short}"
    )

    svc = MagicMock()
    svc.template.containers = []
    svc.latest_ready_revision = served_path
    svc.traffic = [_traffic_entry(served_short, 100)]
    svc.traffic_statuses = []  # not yet populated
    svc_client.get_service.return_value = svc

    rev_container = MagicMock()
    rev_container.env = [_env_var("PAYMENT_MODE", "mock")]
    rev = MagicMock()
    rev.containers = [rev_container]
    rev_client.get_revision.return_value = rev

    state = read_live_state(
        "payment-demo", "r", "p",
        services_client=svc_client,
        revisions_client=rev_client,
    )
    assert state["revision"] == served_short
    rev_client.get_revision.assert_called_once_with(name=served_path)


def test_read_live_state_picks_highest_percent_when_traffic_split():
    """During a split-traffic canary (50/50 or 70/30), the "live" view of
    the world should follow the majority — pick the revision with the
    highest ``percent``."""
    svc_client = MagicMock()
    rev_client = MagicMock()
    # Order is intentionally smaller-percent first to verify the function
    # is sorting by percent (not just taking the [0] entry).
    svc = MagicMock()
    svc.template.containers = []
    svc.latest_ready_revision = (
        "projects/p/locations/r/services/payment-demo/revisions/payment-demo-00099-zzz"
    )
    _set_serving(
        svc,
        statuses=[
            _traffic_entry("payment-demo-00007-aaa", 30),
            _traffic_entry("payment-demo-00009-bbb", 70),
        ],
    )
    svc_client.get_service.return_value = svc

    rev_container = MagicMock()
    rev_container.env = [_env_var("PAYMENT_MODE", "live")]
    rev = MagicMock()
    rev.containers = [rev_container]
    rev_client.get_revision.return_value = rev

    state = read_live_state(
        "payment-demo", "r", "p",
        services_client=svc_client,
        revisions_client=rev_client,
    )
    assert state["revision"] == "payment-demo-00009-bbb"
    rev_client.get_revision.assert_called_once_with(
        name=(
            "projects/p/locations/r/services/payment-demo/revisions/"
            "payment-demo-00009-bbb"
        ),
    )


def test_read_live_state_skips_zero_percent_traffic_entries():
    """Cloud Run permits traffic entries with ``percent=0`` for tag-only
    targets (a tag pointing at a revision that gets no automatic traffic,
    only direct-URL access). Those entries must be skipped so we don't
    pick a not-serving tagged revision as the "live" one."""
    svc_client = MagicMock()
    rev_client = MagicMock()
    svc = MagicMock()
    svc.template.containers = []
    svc.latest_ready_revision = (
        "projects/p/locations/r/services/payment-demo/revisions/payment-demo-00099-zzz"
    )
    # Tag-only entry at 0% should NOT be picked even though it appears first.
    _set_serving(
        svc,
        statuses=[
            _traffic_entry("payment-demo-00099-zzz", 0, tag="canary"),
            _traffic_entry("payment-demo-00007-aaa", 100),
        ],
    )
    svc_client.get_service.return_value = svc

    rev_container = MagicMock()
    rev_container.env = [_env_var("PAYMENT_MODE", "live")]
    rev = MagicMock()
    rev.containers = [rev_container]
    rev_client.get_revision.return_value = rev

    state = read_live_state(
        "payment-demo", "r", "p",
        services_client=svc_client,
        revisions_client=rev_client,
    )
    assert state["revision"] == "payment-demo-00007-aaa"


def test_read_live_state_empty_revision_means_latest_ready():
    """A traffic entry with ``revision == ""`` is Cloud Run's LATEST
    sentinel — "send to whatever latest_ready_revision currently is".
    Resolve that against ``svc.latest_ready_revision`` and return its
    short name (not the empty string)."""
    svc_client = MagicMock()
    rev_client = MagicMock()
    latest_ready_short = "payment-demo-00012-qqq"
    latest_ready_path = (
        f"projects/p/locations/r/services/payment-demo/revisions/{latest_ready_short}"
    )

    svc = MagicMock()
    svc.template.containers = []
    svc.latest_ready_revision = latest_ready_path
    _set_serving(
        svc, statuses=[_traffic_entry("", 100, type_=_TYPE_LATEST)]
    )
    svc_client.get_service.return_value = svc

    rev_container = MagicMock()
    rev_container.env = [_env_var("PAYMENT_MODE", "live")]
    rev = MagicMock()
    rev.containers = [rev_container]
    rev_client.get_revision.return_value = rev

    state = read_live_state(
        "payment-demo", "r", "p",
        services_client=svc_client,
        revisions_client=rev_client,
    )
    assert state["revision"] == latest_ready_short
    rev_client.get_revision.assert_called_once_with(name=latest_ready_path)


def test_read_live_state_status_revision_wins_over_latest_type():
    """``TrafficTargetStatus.revision`` is the *observed* resolved revision
    receiving traffic. If a status entry carries ``type_=LATEST`` AND a
    populated ``.revision``, trust the populated revision — re-resolving
    through ``latest_ready_revision`` would undo the "follow observed
    state" property during reconciliation, since status.revision and
    latest_ready_revision can temporarily disagree."""
    svc_client = MagicMock()
    rev_client = MagicMock()
    observed_short = "payment-demo-00007-aaa"
    observed_path = (
        f"projects/p/locations/r/services/payment-demo/revisions/{observed_short}"
    )
    # ``latest_ready_revision`` is a DIFFERENT, newer revision — the test
    # fails if the function re-resolves through it.
    not_serving_path = (
        "projects/p/locations/r/services/payment-demo/revisions/"
        "payment-demo-00012-qqq"
    )

    svc = MagicMock()
    svc.template.containers = []
    svc.latest_ready_revision = not_serving_path
    svc.traffic_statuses = [
        _traffic_entry(observed_short, 100, type_=_TYPE_LATEST)
    ]
    svc.traffic = []
    svc_client.get_service.return_value = svc

    rev_container = MagicMock()
    rev_container.env = [_env_var("PAYMENT_MODE", "live")]
    rev = MagicMock()
    rev.containers = [rev_container]
    rev_client.get_revision.return_value = rev

    state = read_live_state(
        "payment-demo", "r", "p",
        services_client=svc_client,
        revisions_client=rev_client,
    )
    assert state["revision"] == observed_short
    rev_client.get_revision.assert_called_once_with(name=observed_path)


def test_read_live_state_traffic_target_latest_type_resolves_via_latest_ready():
    """``TrafficTarget`` (``svc.traffic``) is the desired *config*, not the
    observed state. ``type_=LATEST`` is authoritative there: even if the
    entry happens to have a stale ``.revision`` populated, treat it as the
    LATEST sentinel and resolve through ``svc.latest_ready_revision``.

    This path is only exercised when ``traffic_statuses`` has no serving
    entry (brand-new service, transient reconciling state)."""
    svc_client = MagicMock()
    rev_client = MagicMock()
    latest_ready_short = "payment-demo-00012-qqq"
    latest_ready_path = (
        f"projects/p/locations/r/services/payment-demo/revisions/{latest_ready_short}"
    )
    stale_short = "payment-demo-00007-aaa"

    svc = MagicMock()
    svc.template.containers = []
    svc.latest_ready_revision = latest_ready_path
    # ``traffic_statuses`` empty so we fall through to ``traffic``.
    svc.traffic_statuses = []
    svc.traffic = [_traffic_entry(stale_short, 100, type_=_TYPE_LATEST)]
    svc_client.get_service.return_value = svc

    rev_container = MagicMock()
    rev_container.env = [_env_var("PAYMENT_MODE", "live")]
    rev = MagicMock()
    rev.containers = [rev_container]
    rev_client.get_revision.return_value = rev

    state = read_live_state(
        "payment-demo", "r", "p",
        services_client=svc_client,
        revisions_client=rev_client,
    )
    assert state["revision"] == latest_ready_short
    rev_client.get_revision.assert_called_once_with(name=latest_ready_path)


def test_read_live_state_no_traffic_resolves_via_latest_ready():
    """If both ``traffic_statuses`` and ``traffic`` are empty but
    ``latest_ready_revision`` is set, Cloud Run's documented default for
    empty traffic is "100% to latest ready". Resolve through that — don't
    pessimistically fall back to template env."""
    svc_client = MagicMock()
    rev_client = MagicMock()
    latest_ready_short = "payment-demo-00001-abc"
    latest_ready_path = (
        f"projects/p/locations/r/services/payment-demo/revisions/{latest_ready_short}"
    )

    svc = MagicMock()
    svc.template.containers = []
    svc.latest_ready_revision = latest_ready_path
    _set_serving(svc, statuses=[], traffic=[])
    svc_client.get_service.return_value = svc

    rev_container = MagicMock()
    rev_container.env = [_env_var("PAYMENT_MODE", "mock")]
    rev = MagicMock()
    rev.containers = [rev_container]
    rev_client.get_revision.return_value = rev

    state = read_live_state(
        "payment-demo", "r", "p",
        services_client=svc_client,
        revisions_client=rev_client,
    )
    assert state["revision"] == latest_ready_short
    rev_client.get_revision.assert_called_once_with(name=latest_ready_path)


def test_read_live_state_no_traffic_no_latest_ready_falls_back_to_template():
    """Defensive: when *neither* ``traffic_statuses`` / ``traffic`` nor
    ``latest_ready_revision`` is set (just-created service, all deploys
    failed), best-effort fallback to template env + empty revision string."""
    svc_client = MagicMock()
    template_container = MagicMock()
    template_container.env = [_env_var("PAYMENT_MODE", "mock")]
    svc = MagicMock()
    svc.template.containers = [template_container]
    svc.latest_ready_revision = ""
    _set_serving(svc, statuses=[], traffic=[])
    svc_client.get_service.return_value = svc

    state = read_live_state("s", "r", "p", services_client=svc_client)
    assert state["env"] == {"PAYMENT_MODE": "mock"}
    assert state["revision"] == ""


def test_read_live_state_falls_back_to_template_when_no_traffic_serves():
    """If every traffic entry has ``percent == 0`` (no entry is actively
    serving) AND there's no ``latest_ready_revision`` to resolve through,
    fall back to template env. The ``traffic`` block here is non-empty —
    only the "all-zero + no latest ready" case triggers the fallback."""
    svc_client = MagicMock()
    template_container = MagicMock()
    template_container.env = [_env_var("PAYMENT_MODE", "mock")]
    svc = MagicMock()
    svc.template.containers = [template_container]
    svc.latest_ready_revision = ""
    _set_serving(
        svc,
        statuses=[_traffic_entry("payment-demo-00001-abc", 0, tag="canary")],
    )
    svc_client.get_service.return_value = svc

    state = read_live_state("s", "r", "p", services_client=svc_client)
    assert state["env"] == {"PAYMENT_MODE": "mock"}
    assert state["revision"] == ""


def test_read_live_state_skips_value_source_secrets_in_revision():
    svc_client = MagicMock()
    rev_client = MagicMock()
    served_short = "s-00001-xyz"
    served_path = f"projects/p/locations/r/services/s/revisions/{served_short}"

    svc = MagicMock()
    svc.template.containers = []
    svc.latest_ready_revision = ""
    _set_serving(svc, statuses=[_traffic_entry(served_short, 100)])
    svc_client.get_service.return_value = svc

    rev_container = MagicMock()
    rev_container.env = [
        _secret_env_var("DB_PASSWORD"),
        _env_var("PAYMENT_MODE", "mock"),
    ]
    rev = MagicMock()
    rev.containers = [rev_container]
    rev_client.get_revision.return_value = rev

    state = read_live_state(
        "s", "r", "p",
        services_client=svc_client, revisions_client=rev_client,
    )
    assert "DB_PASSWORD" not in state["env"]
    assert state["env"]["PAYMENT_MODE"] == "mock"
    rev_client.get_revision.assert_called_once_with(name=served_path)
