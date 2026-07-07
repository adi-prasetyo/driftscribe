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


def _highest_percent_entry(entries):
    """Pick the entry in ``entries`` with the highest ``percent``, skipping
    not-serving entries (``percent <= 0``). Ties broken by lower index for
    determinism. Returns ``None`` if no entry is serving.

    Works on either ``TrafficTarget`` (``svc.traffic[*]``) or
    ``TrafficTargetStatus`` (``svc.traffic_statuses[*]``) — both expose
    ``percent`` / ``revision`` / ``type_``.
    """
    serving = [
        (idx, entry)
        for idx, entry in enumerate(entries)
        if entry.percent > 0
    ]
    if not serving:
        return None
    serving.sort(key=lambda pair: (-pair[1].percent, pair[0]))
    return serving[0][1]


def read_live_state(
    service: str,
    region: str,
    project: str,
    services_client=None,
    revisions_client=None,
) -> dict:
    """Read the env + revision-name of the *traffic-serving* revision for a
    Cloud Run service, **fetched from the same revision resource** so env
    and revision can never drift apart.

    Returns::

        {
            "env": {name: value, ...},   # plain-value env entries
            "revision": "<short-name>",  # e.g. "payment-demo-00007-abc"
        }

    Why follow traffic and not ``svc.latest_ready_revision``: after a
    successful rollback (or any ``--no-traffic`` deploy), the
    traffic-serving revision is OLDER than the latest ready — the newer
    revision is ready, just not receiving traffic. A reconciler that reads
    env from latest_ready would propose rollback on every tick, forever.
    Following traffic describes what the world is *currently* seeing,
    which is the question drift detection is actually asking.

    Why ``svc.traffic_statuses`` is the primary source, with ``svc.traffic``
    as fallback: ``traffic`` is the *desired* configuration; while
    ``svc.reconciling`` is true the actually-serving state lives in the
    output-only ``traffic_statuses`` field (per Cloud Run v2 proto docs).
    Once reconciliation completes, the two match — but during a mid-deploy
    Eventarc trigger they can diverge. ``traffic_statuses`` is what other
    callers in this repo use to identify the active revision
    (see ``workers/rollback/main.py::_list_revisions``). Falling back to
    ``svc.traffic`` covers the freshly-created-service case where the
    server has not yet populated ``traffic_statuses``.

    Why not read env from ``svc.template``: ``svc.template`` describes the
    *next* revision that would be created if the service were re-deployed
    right now. During a failed/rolling deploy or a ``--no-traffic`` push,
    the template's env can differ from the env of the actually-serving
    revision. Pairing template-env with a revision name would produce
    misleading "live state" — exactly the kind of bug a drift detector
    exists to catch.

    Algorithm:

    1. Pick the highest-percent entry from ``svc.traffic_statuses``.
       ``TrafficTargetStatus.revision`` is the *observed* serving revision —
       trust it whenever it's populated, even if ``type_`` says LATEST
       (the type field describes how the entry was configured; ``.revision``
       describes where traffic is actually flowing).
    2. If ``traffic_statuses`` has no serving entry (brand-new service —
       transient state per the v2 proto docs), apply the same picker to
       ``svc.traffic``. ``TrafficTarget`` is the desired config, so
       ``type_ == TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST`` (or empty
       ``.revision``) means "resolve through ``latest_ready_revision``".
    3. If neither source yields a revision name, resolve through
       ``svc.latest_ready_revision`` — Cloud Run's documented default for
       empty traffic is "100% to latest ready".
    4. If even that is unset (just-created service, all deploys failed),
       fall back to ``svc.template`` env with ``revision=""`` — the caller
       can branch on the empty revision to decide whether to skip
       reconciliation.

    Sibling of :func:`read_live_env`, which is preserved for callers that
    only need env from the template (Phase 8 agent + the historical shim
    in ``agent.cloud_run_client``).
    """
    services_client = services_client or run_v2.ServicesClient()
    name = f"projects/{project}/locations/{region}/services/{service}"
    svc = services_client.get_service(name=name)

    # Step 1: prefer observed serving state. For TrafficTargetStatus a
    # populated ``.revision`` is the resolved serving revision — trust it
    # even if ``type_`` says LATEST, because re-resolving through
    # ``latest_ready_revision`` during reconciliation could undo the whole
    # "follow observed state" property when status.revision and
    # latest_ready_revision temporarily disagree.
    short_name = ""
    chosen_status = _highest_percent_entry(svc.traffic_statuses)
    if chosen_status is not None and chosen_status.revision:
        short_name = chosen_status.revision

    # Step 2: fall back to desired config. For TrafficTarget the ``type_``
    # enum is authoritative — LATEST means "send to latest_ready_revision"
    # regardless of any ``.revision`` value that may be present.
    if not short_name:
        chosen_target = _highest_percent_entry(svc.traffic)
        if chosen_target is not None:
            is_latest = (
                chosen_target.type_
                == run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST
            )
            if not is_latest and chosen_target.revision:
                short_name = chosen_target.revision

    # Steps 3 + 4: nothing in traffic_statuses or traffic pinned a revision.
    # Cloud Run's documented default for empty/LATEST traffic is "100% to
    # latest_ready_revision", so resolve through that — only fall back to
    # template env if even that is unset.
    if not short_name:
        latest_path = svc.latest_ready_revision or ""
        if not latest_path:
            env = _extract_env_from_containers(svc.template.containers)
            return {"env": env, "revision": ""}
        short_name = latest_path.rsplit("/", 1)[-1]

    rev_path = (
        f"projects/{project}/locations/{region}/services/{service}"
        f"/revisions/{short_name}"
    )
    revisions_client = revisions_client or run_v2.RevisionsClient()
    rev = revisions_client.get_revision(name=rev_path)
    env = _extract_env_from_containers(rev.containers)
    return {"env": env, "revision": short_name}


def _is_ready(revision) -> bool:
    """True if ``revision`` (a ``run_v2.Revision``) carries a ``Ready``
    condition in the succeeded state.

    Cloud Run's per-revision ``conditions`` list is Knative-style: each
    entry has a string ``type_`` ("Ready", "Active", ...) rather than a
    single top-level status field. A revision that failed to start (bad
    image, crashed on boot) or is still mid-rollout will lack a succeeded
    ``Ready`` condition — but it still EXISTS as a revision resource, so
    the rollback worker's existence-only validation would accept it (see
    :func:`list_previous_ready_revisions`) and the rollback would shift
    100% of traffic onto a revision that fails the same way the original
    bad deploy did.
    """
    return any(
        c.type_ == "Ready" and c.state == run_v2.Condition.State.CONDITION_SUCCEEDED
        for c in revision.conditions
    )


def list_previous_ready_revisions(
    service: str,
    region: str,
    project: str,
    active_revision: str,
    revisions_client=None,
    cap: int = 5,
) -> list[str]:
    """Return up to ``cap`` READY revision names for ``service``, newest
    first, excluding ``active_revision``.

    Powers the drift chat "roll back to..." flow: an operator asking Anchor
    to roll back rarely knows a Cloud Run revision name by heart, and
    :func:`read_live_state` only ever returns the ONE currently-serving
    revision. This gives the LLM a short list of concrete, already-valid
    candidate names instead of forcing it to guess or refuse.

    Filtered to READY (see :func:`_is_ready`) because this is the ONLY
    readiness gate on the rollback path: the rollback worker validates that
    ``target_revision`` exists and is not the active one
    (``workers/rollback/main.py::_list_revisions`` returns EVERY revision,
    healthy or not, and ``/propose`` checks membership + not-active only) —
    it never checks revision health. A failed/pending revision surfaced
    here would therefore be approved and applied as-is, so it must never
    be surfaced.

    Sorted by ``create_time`` descending rather than trusting
    ``list_revisions``' return order (undocumented) or the revision-name
    suffix (a random 3-letter tag, not a sequence number).
    """
    revisions_client = revisions_client or run_v2.RevisionsClient()
    parent = f"projects/{project}/locations/{region}/services/{service}"
    candidates = []
    for rev in revisions_client.list_revisions(parent=parent):
        short_name = rev.name.rsplit("/", 1)[-1]
        if short_name == active_revision or not _is_ready(rev):
            continue
        candidates.append((rev.create_time, short_name))
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return [name for _, name in candidates[:cap]]
