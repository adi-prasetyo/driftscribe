from google.cloud import run_v2


def _extract_env_from_containers(containers) -> dict[str, str]:
    """Pull plain-value env entries out of a list of Container protos, skipping
    Secret-Manager-backed entries (those have ``value_source`` set).

    Empty string is a legitimate Cloud Run env value, so we cannot use
    truthiness on ``.value`` as the discriminator. Shared between
    Service.template.containers and Revision.containers.
    """
    env: dict[str, str] = {}
    for container in containers:
        for ev in container.env:
            if getattr(ev, "value_source", None):
                continue
            env[ev.name] = ev.value or ""
    return env


def read_live_env(service: str, region: str, project: str, client=None) -> dict[str, str]:
    """Read the env block from a Cloud Run service's *template*.

    Note: this reads ``svc.template.containers`` (next-deploy spec). It can
    diverge from the actively-served revision during failed/rolling deploys.
    :func:`read_live_state` is the safer choice for new code — it pulls env
    from the same revision resource whose name it returns. This function is
    retained for backward compat with the Phase 8 agent (which is happy with
    template env because the demo never has split-traffic).
    """
    client = client or run_v2.ServicesClient()
    name = f"projects/{project}/locations/{region}/services/{service}"
    svc = client.get_service(name=name)
    return _extract_env_from_containers(svc.template.containers)


def read_live_state(
    service: str,
    region: str,
    project: str,
    services_client=None,
    revisions_client=None,
) -> dict:
    """Read the env + revision-name of the *latest-ready* revision for a Cloud
    Run service, **fetched from the same revision resource** so env and
    revision can never drift apart.

    Returns::

        {
            "env": {name: value, ...},   # plain-value env entries
            "revision": "<short-name>",  # latest_ready_revision, e.g. "payment-demo-00007-abc"
        }

    Why not read env from ``svc.template``: ``svc.template`` describes the
    *next* revision that would be created if the service were re-deployed
    right now. During a failed/rolling deploy or a ``--no-traffic`` push, the
    template's env can differ from the env of the actually-serving revision.
    Pairing template-env with a revision name would produce misleading "live
    state" — exactly the kind of bug a drift detector exists to catch. So we
    instead resolve ``latest_ready_revision`` and pull env directly from
    that revision's container spec.

    **Important semantic caveat — "latest ready" vs "actually serving"**:
    ``latest_ready_revision`` is the most recent revision that passed
    readiness, **not** necessarily the revision currently receiving traffic.
    After a manual ``--no-traffic`` deploy or a rollback (Phase 11.5), the
    traffic-serving revision may be older than ``latest_ready_revision``.
    For drift detection where we care about *what the world is currently
    seeing*, the correct source is ``svc.traffic[*]`` filtered to where
    ``percent > 0``. We use ``latest_ready_revision`` here because (a) the
    Phase 11 demo never splits traffic, and (b) renaming/refactoring this to
    "traffic-weighted" is a follow-up tracked for Phase 11.5 — the Rollback
    Agent needs the same proto traversal and will be the right place to
    introduce a shared helper. Until then, this function's contract is
    "latest ready", not "actively serving".

    If the service has no ready revision (just-created, all deploys failed),
    we fall back to ``svc.template`` and return an empty revision string —
    the caller can decide whether that's a useful response.

    Sibling of :func:`read_live_env`, which is preserved for callers that only
    need env from the template (Phase 8 agent + the historical shim in
    ``agent.cloud_run_client``). Used by the Reader Agent worker (Phase 11.3)
    and will be reused by the Rollback Agent (Phase 11.5).
    """
    services_client = services_client or run_v2.ServicesClient()
    name = f"projects/{project}/locations/{region}/services/{service}"
    svc = services_client.get_service(name=name)
    # latest_ready_revision is the fully-qualified resource path:
    # "projects/<p>/locations/<r>/services/<s>/revisions/<s>-00007-abc"
    rev_path = svc.latest_ready_revision or ""
    if rev_path:
        revisions_client = revisions_client or run_v2.RevisionsClient()
        rev = revisions_client.get_revision(name=rev_path)
        env = _extract_env_from_containers(rev.containers)
    else:
        # No ready revision yet — best-effort fallback to the template so
        # callers at least get *something*. revision="" signals "no ready
        # revision" so they can branch on it.
        env = _extract_env_from_containers(svc.template.containers)
    return {
        "env": env,
        "revision": rev_path.rsplit("/", 1)[-1],
    }
