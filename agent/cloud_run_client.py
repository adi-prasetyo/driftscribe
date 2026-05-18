from google.cloud import run_v2


def read_live_env(service: str, region: str, project: str, client=None) -> dict[str, str]:
    """Read the env block from the latest revision of a Cloud Run service."""
    client = client or run_v2.ServicesClient()
    name = f"projects/{project}/locations/{region}/services/{service}"
    svc = client.get_service(name=name)
    env: dict[str, str] = {}
    for container in svc.template.containers:
        for ev in container.env:
            # Skip Secret-Manager-backed entries (value_source is set).
            # Empty string is a legitimate Cloud Run env value, so we cannot use
            # truthiness on .value as the discriminator.
            if getattr(ev, "value_source", None):
                continue
            env[ev.name] = ev.value or ""
    return env
