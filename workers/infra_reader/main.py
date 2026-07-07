"""Infra-Reader Agent — read-only whole-project inventory worker (Phase B).

Worker sibling of :mod:`workers.reader`. Where the Reader returns the live
shape of one *hardcoded* Cloud Run service, the Infra-Reader enumerates the
project's CAI-searchable resources and labels each one declared-in-IaC vs not,
by parsing the baked-in ``iac/`` dir. No tofu state, no KMS — zero sensitive
credential. The SA holds only ``cloudasset.viewer`` + ``serviceUsageConsumer``.

Safety layers (mirroring :mod:`workers.reader`):

- **Layer 1 (IAM scoping):** ``infra-reader-sa`` can only *read* the
  asset index; it cannot mutate anything.
- **Layer 2 (payload-intent policy):** the request body is a closed schema
  (:class:`DescribeRequest` with ``extra="forbid"``). Scope/project come from
  env at boot; the caller cannot influence them.
- **Layer 3 (inter-service auth):** :func:`driftscribe_lib.auth.verify_caller`
  validates the inbound Google ID token's audience + caller email.

Degradation contract: a CAI permission/availability failure soft-fails to an
HTTP 200 with ``{"error": "cloud_asset_unavailable", ...}`` (NOT 5xx), because
``worker_client.call`` treats non-2xx as a transport failure — a 200 lets chat
narrate the partial degradation. Auth failures stay real 401/403.

Read-mask policy: the PRIMARY inventory search stays minimal-masked
(name/asset_type/location) — anything more would over-fetch rich metadata for
every asset type, including sensitive-adjacent fields. Two adoptable types get
ONE additional scoped ``versioned_resources`` search each, from which ONLY a
single field is retained:

- Pub/Sub subscriptions → ``resource.topic`` (the subscription→topic edge).
- Cloud Run services → the template container image
  (``spec.template.spec.containers[0].image``), so a service can be adopted
  without stalling to ask for its image.

Every other field these versioned resources carry — push endpoints, container
**env vars (possible secrets)**, service-account emails, creator/lastModifier
operator emails, labels — is read by the API but never stored, logged (not even
in per-row skip warnings, which record only the asset type + exception, never
row content), or returned. The run image is additionally SUPPRESSED for
DriftScribe's own control-plane services at emission (build_inventory / graph
node), so it never enters the anonymous-visible ``/infra/graph`` JSON or the L2
cache. Both searches soft-fail INDEPENDENTLY to their no-enrichment behavior
without touching the primary inventory or each other.
"""
import dataclasses
import os
from collections.abc import Callable
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from google.api_core import exceptions as gax
from google.cloud import asset_v1
from pydantic import BaseModel, ConfigDict

from driftscribe_lib.auth import verify_caller
from driftscribe_lib.iac_hcl import DeclaredIdentity, extract_declared_identities
from driftscribe_lib.infra_inventory import CaiResource, build_inventory, shorten_topic
from driftscribe_lib.logging import install_trace_middleware, setup as setup_logging

log = setup_logging("infra-reader-agent")

# Boot-time env resolution. GCP_PROJECT / OWN_URL / ALLOWED_CALLERS MUST be set
# — KeyError here fails the Cloud Run revision at startup, surfacing the
# misconfig immediately. IAC_DIR / IAC_SNAPSHOT_SHA have demo-safe defaults.
GCP_PROJECT = os.environ["GCP_PROJECT"]
OWN_URL = os.environ["OWN_URL"].rstrip("/")
ALLOWED_CALLERS = frozenset(
    e.strip() for e in os.environ["ALLOWED_CALLERS"].split(",") if e.strip()
)
IAC_DIR = Path(os.environ.get("IAC_DIR", "/app/iac"))
IAC_SNAPSHOT_SHA = os.environ.get("IAC_SNAPSHOT_SHA", "unknown")

# read_mask is minimal by design: only the three fields infra_inventory needs.
# google-cloud-asset 4.3.0 coerces ``read_mask={"paths": [...]}`` into a
# FieldMask whose .paths preserves this order; anything else would over-fetch
# and risk surfacing sensitive resource attributes.
_READ_MASK_PATHS = ["name", "asset_type", "location"]

# Scoped per-type enrichment (see module docstring). For each adoptable type
# that needs one field the minimal mask omits, a SECOND scoped search restricted
# to that type, masked to ``versioned_resources``; ONLY the single named field is
# retained. Subscriptions → ``resource.topic``; run Services → the template
# container image.
_SUB_ASSET_TYPE = "pubsub.googleapis.com/Subscription"
_RUN_ASSET_TYPE = "run.googleapis.com/Service"
_VERSIONED_READ_MASK_PATHS = ["name", "versioned_resources"]


def _dig(obj, *path):
    """Walk ``path`` through a nested mapping/sequence; None on any miss.

    ``str`` steps index a mapping via ``.get``; ``int`` steps index a sequence.
    Returns None on any missing key, out-of-range index, or type mismatch. Works
    on plain dicts/lists (tests) and the proto-plus ``MapComposite`` /
    ``RepeatedComposite`` the live CAI client returns for a
    ``google.protobuf.Struct`` — so extractors navigate one shape either way.
    """
    cur = obj
    for step in path:
        try:
            if isinstance(step, int):
                cur = cur[step]
            else:
                get = getattr(cur, "get", None)
                if not callable(get):
                    return None
                cur = get(step)
        except (KeyError, IndexError, TypeError):
            return None
        if cur is None:
            return None
    return cur


def _first_topic(resources) -> str | None:
    """First non-empty string ``topic`` among the given resource mappings, else None.

    Each item is a CAI ``VersionedResource.resource`` Struct, which proto-plus
    exposes as a dict-like ``MapComposite`` (``.get`` works) — or a plain dict in
    tests. Pure: no network, unit-testable with plain-dict doubles. Malformed or
    topic-less rows are skipped individually so one bad row can't drop the rest.
    """
    for resource in resources:
        get = getattr(resource, "get", None)
        if not callable(get):
            continue
        topic = get("topic")
        if isinstance(topic, str) and topic:
            return topic
    return None


def extract_run_image(versioned) -> str | None:
    """First non-empty template container image among run Service versioned
    resources, else None.

    Tries the v1 Knative shape ``spec.template.spec.containers[0].image`` then a
    v2-shape fallback ``template.containers[0].image``; FIRST container only (the
    adopt recipe renders single-container HCL, so a multi-container service can't
    cleanly import anyway). Pure: no network, unit-testable with plain dicts.

    SECURITY: returns ONLY the image string. The run versioned resource also
    carries container env vars (possible secrets), the service-account email,
    and creator/lastModifier operator emails — none are returned, and callers
    MUST never store or log the raw payload.
    """
    for resource in versioned:
        image = _dig(resource, "spec", "template", "spec", "containers", 0, "image")
        if not (isinstance(image, str) and image):
            image = _dig(resource, "template", "containers", 0, "image")
        if isinstance(image, str) and image:
            return image
    return None


def _versioned_field_map(
    client: asset_v1.AssetServiceClient,
    asset_type: str,
    extract: Callable[[list], str | None],
) -> dict[str, str]:
    """Map each hit's raw CAI ``name`` → the single field ``extract`` retains,
    via ONE scoped ``versioned_resources`` search restricted to ``asset_type``.

    Shared plumbing for both enrichments (subscription→topic, run→image): the
    scoped search, the join by raw CAI ``name`` (the SAME string ``_search_all``
    returns — NOT the normalized display name), and the PER-ROW guard. A single
    malformed result (versioned_resources None/absent, a wrapper whose
    ``.resource`` raises, an unreadable name) skips ONLY that row — never drops
    the whole map. The whole-map {} fallback in ``describe()`` is reserved for an
    API-level failure (the search/pagination itself raising), which propagates
    out of this loop as intended.

    ``extract`` receives the row's versioned-resource mappings and returns the
    retained value or None. ONLY that return value is kept — every other field
    the payload carries (push endpoints, env vars, SA/operator emails, labels) is
    discarded here and never leaves this function, never stored or logged.
    """
    request = asset_v1.SearchAllResourcesRequest(
        scope=f"projects/{GCP_PROJECT}",
        asset_types=[asset_type],
        read_mask={"paths": _VERSIONED_READ_MASK_PATHS},
    )
    out: dict[str, str] = {}
    for r in client.search_all_resources(request=request):
        try:
            value = extract([vr.resource for vr in (r.versioned_resources or ())])
            if value:
                out[r.name] = value
        except Exception as e:  # noqa: BLE001 — one bad row must not drop the rest
            # Log the asset type + exception only (both payload-free) — NEVER the
            # row content (env vars / SA emails live in the run payload).
            log.warning("skipping malformed %s enrichment row: %s", asset_type, e)
            continue
    return out


def _subscription_topics(client: asset_v1.AssetServiceClient) -> dict[str, str]:
    """Map each subscription's raw CAI ``name`` → its topic (shortened in-project).

    Thin wrapper over :func:`_versioned_field_map`: retains ``resource.topic`` and
    applies :func:`shorten_topic`. See that helper for the guard/soft-fail
    contract; the join key is the raw CAI ``name``.
    """
    return _versioned_field_map(
        client,
        _SUB_ASSET_TYPE,
        lambda resources: (
            shorten_topic(raw, GCP_PROJECT) if (raw := _first_topic(resources)) else None
        ),
    )


def _run_service_images(client: asset_v1.AssetServiceClient) -> dict[str, str]:
    """Map each Cloud Run service's raw CAI ``name`` → its template container image.

    Thin wrapper over :func:`_versioned_field_map` supplying
    :func:`extract_run_image`. No shortening — the image passes through verbatim
    so it byte-matches the live spec for a zero-change import.
    """
    return _versioned_field_map(client, _RUN_ASSET_TYPE, extract_run_image)


def _verify_caller_dep(request: Request) -> str:
    """Thin wrapper around :func:`driftscribe_lib.auth.verify_caller` so tests
    can swap it via ``app.dependency_overrides`` without monkeypatching the
    shared library module."""
    return verify_caller(request, own_url=OWN_URL, allowed_callers=ALLOWED_CALLERS)


class DescribeRequest(BaseModel):
    """Empty by design — see module docstring, Layer 2.

    ``extra="forbid"`` makes pydantic raise on any unexpected field; FastAPI
    converts that to HTTP 422.
    """

    model_config = ConfigDict(extra="forbid")


def _load_declared() -> tuple[list[DeclaredIdentity], bool]:
    """Read the baked-in ``iac/`` dir and extract the declared-identity set.

    Returns ``(declared, parse_ok)`` where ``parse_ok`` is False if any ``*.tf``
    failed to parse (surfaced by :func:`build_inventory` as
    ``declared_set_status="parse_error"``)."""
    files: dict[str, str] = {}
    if IAC_DIR.is_dir():
        for tf in sorted(IAC_DIR.glob("*.tf")):
            files[tf.name] = tf.read_text(encoding="utf-8")
    declared, parse_errors = extract_declared_identities(files)
    return declared, len(parse_errors) == 0


def _search_all(client: asset_v1.AssetServiceClient) -> list[CaiResource]:
    """Enumerate the project's CAI resources via a minimal-masked search."""
    request = asset_v1.SearchAllResourcesRequest(
        scope=f"projects/{GCP_PROJECT}",
        read_mask={"paths": _READ_MASK_PATHS},
    )
    return [
        CaiResource(name=r.name, asset_type=r.asset_type, location=r.location)
        for r in client.search_all_resources(request=request)
    ]


app = FastAPI(title="DriftScribe Infra-Reader Agent")

# Per-request trace id from inbound X-Trace-Id (or a fresh UUIDv4 hex), bound to
# a ContextVar so every log.* call in the request carries it.
install_trace_middleware(app)


@app.get("/healthz")
def healthz() -> dict[str, bool]:
    """Liveness probe — intentionally unauthenticated so Cloud Run's built-in
    health checks work without minting an ID token."""
    return {"ok": True}


@app.post("/describe")
def describe(
    _body: DescribeRequest,
    caller: str = Depends(_verify_caller_dep),
) -> dict:
    """Return the bounded project inventory summary, IaC-labeled.

    CAI permission/availability failures soft-fail to a 200 (see module
    docstring); the declared set degrades independently via
    ``declared_set_status``."""
    log.info("describe request from %s project=%s", caller, GCP_PROJECT)
    declared, parse_ok = _load_declared()
    try:
        client = asset_v1.AssetServiceClient()
        resources = _search_all(client)
    except gax.GoogleAPICallError as e:
        # PermissionDenied is a GoogleAPICallError subclass, so this one handler
        # covers both the no-IAM case and generic transient backend failures.
        log.warning("cloud asset unavailable: %s", e)
        return {
            "error": "cloud_asset_unavailable",
            "detail": str(e),
            "project": GCP_PROJECT,
        }

    # Per-type versioned enrichment: each runs only when its type is present, and
    # strictly soft-fails — the PRIMARY inventory must NEVER degrade because of
    # it. The two blocks are INDEPENDENT (each in its own try/except): a failure
    # of one must not skip the other. Any failure (API error, unexpected shapes)
    # degrades to an empty map, so samples simply lack the field and the crew
    # falls back to asking.
    if any(r.asset_type == _SUB_ASSET_TYPE for r in resources):
        try:
            topics = _subscription_topics(client)
        except Exception:  # noqa: BLE001 — enrichment must never break the primary inventory
            log.warning("subscription topic enrichment failed", exc_info=True)
            topics = {}
        if topics:
            resources = [
                dataclasses.replace(r, topic=topics.get(r.name))
                if r.asset_type == _SUB_ASSET_TYPE
                else r
                for r in resources
            ]

    if any(r.asset_type == _RUN_ASSET_TYPE for r in resources):
        try:
            images = _run_service_images(client)
        except Exception:  # noqa: BLE001 — enrichment must never break the primary inventory
            log.warning("run service image enrichment failed", exc_info=True)
            images = {}
        if images:
            resources = [
                dataclasses.replace(r, image=images.get(r.name))
                if r.asset_type == _RUN_ASSET_TYPE
                else r
                for r in resources
            ]

    return build_inventory(
        resources,
        declared,
        project=GCP_PROJECT,
        iac_snapshot_sha=IAC_SNAPSHOT_SHA,
        declared_parse_ok=parse_ok,
    )
