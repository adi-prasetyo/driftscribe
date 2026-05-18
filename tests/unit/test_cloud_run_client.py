from unittest.mock import MagicMock
from agent.cloud_run_client import read_live_env


def _env_var(name, value):
    m = MagicMock()
    m.name = name
    m.value = value
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
    secret = MagicMock()
    secret.name = "DB_PASSWORD"
    secret.value = ""
    plain = _env_var("PAYMENT_MODE", "live")
    container = MagicMock()
    container.env = [secret, plain]
    svc = MagicMock()
    svc.template.containers = [container]
    client.get_service.return_value = svc

    env = read_live_env("s", "r", "p", client=client)
    assert "DB_PASSWORD" not in env
    assert env["PAYMENT_MODE"] == "live"
