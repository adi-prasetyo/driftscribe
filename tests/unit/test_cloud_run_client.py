from unittest.mock import MagicMock

from agent.cloud_run_client import read_live_env
from driftscribe_lib.cloud_run import read_live_state


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


def test_read_live_state_reads_env_from_latest_ready_revision():
    """Phase 11.3: env must come from the *revision* named by
    latest_ready_revision, NOT from svc.template — otherwise a failed/rolling
    deploy can pair template-env with a stale revision name and mislead the
    drift detector."""
    svc_client = MagicMock()
    rev_client = MagicMock()
    rev_path = (
        "projects/p/locations/r/services/payment-demo/revisions/payment-demo-00007-abc"
    )

    # Service template has the NEXT-deploy env (different from the served one
    # to make the bug visible if the function reads the wrong place).
    template_container = MagicMock()
    template_container.env = [_env_var("PAYMENT_MODE", "live-NEW")]
    svc = MagicMock()
    svc.template.containers = [template_container]
    svc.latest_ready_revision = rev_path
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
    assert state["revision"] == "payment-demo-00007-abc"
    rev_client.get_revision.assert_called_once_with(name=rev_path)


def test_read_live_state_falls_back_to_template_when_no_ready_revision():
    """If a service never had a ready revision (just-created or every deploy
    failed), latest_ready_revision is empty. Fall back to template env and
    return an empty revision string."""
    svc_client = MagicMock()
    template_container = MagicMock()
    template_container.env = [_env_var("PAYMENT_MODE", "mock")]
    svc = MagicMock()
    svc.template.containers = [template_container]
    svc.latest_ready_revision = ""
    svc_client.get_service.return_value = svc

    state = read_live_state("s", "r", "p", services_client=svc_client)
    assert state["env"] == {"PAYMENT_MODE": "mock"}
    assert state["revision"] == ""


def test_read_live_state_skips_value_source_secrets_in_revision():
    svc_client = MagicMock()
    rev_client = MagicMock()
    rev_path = "projects/p/locations/r/services/s/revisions/s-00001-xyz"

    svc = MagicMock()
    svc.template.containers = []
    svc.latest_ready_revision = rev_path
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
