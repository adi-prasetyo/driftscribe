"""Deterministic adopt-PR renderer (adopt design Phase 3, docs/plans/2026-06-11-adopt-import-design.md §5).

Renders the resource block + co-located ``import`` block for ONE live
resource, in exactly the shapes the 2026-06-11 fidelity probes proved reach a
pure no-op import plan (docs/plans/2026-06-11-adopt-recipe.md §0.2). The
output is byte-deterministic: no LLM authors adopt HCL. Minimality is
load-bearing — declaring ``labels`` would trigger the provider's
attribution-label injection and break the zero-change promise.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from driftscribe_lib.iac_plan_denylist import (
    CONTROL_PLANE_BUCKET_SUFFIXES,
    CONTROL_PLANE_SERVICE_NAMES,
    is_service_managed_bucket_name,
    is_service_managed_pubsub_name,
)

__all__ = [
    "AdoptRecipeError",
    "AdoptRendering",
    "ADOPT_KINDS",
    "FINAL_REFUSAL_MARKER",
    "render_adoption",
    "find_import_violations",
    "preflight_conflicts",
    "_ID_SHAPES",
    "_RTYPE_TO_ASSET_TYPE",
]

ADOPT_KINDS: dict[str, str] = {
    "google_storage_bucket": "bucket",
    "google_pubsub_topic": "topic",
    "google_pubsub_subscription": "subscription",
    "google_cloud_run_v2_service": "service",
}
_HUMAN: dict[str, str] = {
    "google_storage_bucket": "Cloud Storage bucket",
    "google_pubsub_topic": "Pub/Sub topic",
    "google_pubsub_subscription": "Pub/Sub subscription",
    "google_cloud_run_v2_service": "Cloud Run service",
}

# The terminal sentence of every tool-boundary refusal that is FINAL —
# i.e. NOT retryable parameter feedback. The provision system prompt
# quotes this sentence verbatim so the model can classify a rejected
# result by its reason text; tests/unit/test_adoption_order_prompts.py
# pins the duplication in both directions.
FINAL_REFUSAL_MARKER = "This is not a parameter problem — do not retry."

# Friendly-name canonicalization for the LLM-facing ``resource_type`` param.
# Live e2e (2026-06-11) showed the model passing the HUMAN name ("Cloud
# Storage bucket"), receiving the "'X' is not adoptable yet" rejection, and
# concluding buckets are a product limitation — a wrong-parameter error read
# as a capability gap. Aliases are matched on a normalized form (lowercase,
# spaces/dashes → underscores) and map DETERMINISTICALLY onto the canonical
# HCL type; anything unmatched still gets the (now param-explicit) rejection.
_TYPE_ALIASES: dict[str, str] = {
    "bucket": "google_storage_bucket",
    "storage_bucket": "google_storage_bucket",
    "gcs_bucket": "google_storage_bucket",
    "cloud_storage_bucket": "google_storage_bucket",
    "storage.googleapis.com/bucket": "google_storage_bucket",
    "topic": "google_pubsub_topic",
    "pubsub_topic": "google_pubsub_topic",
    "pub/sub_topic": "google_pubsub_topic",
    "pubsub.googleapis.com/topic": "google_pubsub_topic",
    "subscription": "google_pubsub_subscription",
    "pubsub_subscription": "google_pubsub_subscription",
    "pub/sub_subscription": "google_pubsub_subscription",
    "pubsub.googleapis.com/subscription": "google_pubsub_subscription",
    "service": "google_cloud_run_v2_service",
    "run_service": "google_cloud_run_v2_service",
    "cloud_run_service": "google_cloud_run_v2_service",
    "cloud_run_v2_service": "google_cloud_run_v2_service",
    "run.googleapis.com/service": "google_cloud_run_v2_service",
}


def _canonicalize_resource_type(value: str) -> str | None:
    """Map a canonical HCL type or a friendly alias to the canonical type.

    Returns ``None`` when the value matches neither (the caller raises the
    param-explicit rejection).
    """
    if value in ADOPT_KINDS:
        return value
    normalized = re.sub(r"[\s-]+", "_", value.strip().lower())
    return _TYPE_ALIASES.get(normalized)

# Import-id shapes — DUPLICATED from tools.iac_static_gate.ADOPT_IMPORT_ID_SHAPES
# to satisfy the layering rule (lib must not import from tools/). The drift-pin
# test ``test_id_shapes_match_static_gate`` asserts pattern equality.
_ID_SHAPES: dict[str, re.Pattern[str]] = {
    "google_storage_bucket": re.compile(r"^[^/\s]+$"),
    "google_pubsub_topic": re.compile(r"^projects/[^/\s]+/topics/[^/\s]+$"),
    "google_pubsub_subscription": re.compile(r"^projects/[^/\s]+/subscriptions/[^/\s]+$"),
    "google_cloud_run_v2_service": re.compile(
        r"^projects/[^/\s]+/locations/[^/\s]+/services/[^/\s]+$"
    ),
}

# rtype → CAI asset type — DUPLICATED from iac_hcl._SUPPORTED_RESOURCE_ASSET_TYPES
# for the four adoptable types. The drift-pin test asserts equality.
_RTYPE_TO_ASSET_TYPE: dict[str, str] = {
    "google_storage_bucket": "storage.googleapis.com/Bucket",
    "google_pubsub_topic": "pubsub.googleapis.com/Topic",
    "google_pubsub_subscription": "pubsub.googleapis.com/Subscription",
    "google_cloud_run_v2_service": "run.googleapis.com/Service",
}

# Short type label used in file path (iac/adopt_<short>_<slug>.tf)
_SHORT: dict[str, str] = {
    "google_storage_bucket": "bucket",
    "google_pubsub_topic": "topic",
    "google_pubsub_subscription": "subscription",
    "google_cloud_run_v2_service": "service",
}

# Param shapes — HCL-injection hardening (§1.4):
#   ALL params ban quotes, interpolation, whitespace, backslash.
#   name / location additionally ban slash (path break-out guard).
#   image / topic (short-form) allow / : @ (artifact refs / path segments).
#
# _NAME_RE: [A-Za-z0-9] start, then alnum + ._~+%- (no slash, no whitespace,
#   no quote, no backslash, no ${). Length 1–254.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~+%-]{0,253}$")
# _LOCATION_RE: letter start, then alphanum/dash, length 3–32.
_LOCATION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{1,30}$")
# _IMAGE_RE: allows / : @ for artifact references.
_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{2,511}$")
# _PROJECT_RE: GCP project id shape.
_PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
# _TOPIC_SHORT_RE: short topic name — same as _NAME_RE.
_TOPIC_SHORT_RE = _NAME_RE

# Characters universally banned from all params (as individual substring checks)
_BANNED_UNIVERSAL = ('"', "${", "\n", "\\", " ")


class AdoptRecipeError(ValueError):
    """Operator-plain rejection; the tool surfaces ``str(exc)`` verbatim."""


@dataclass(frozen=True)
class AdoptRendering:
    path: str
    content: str
    address: str
    import_id: str
    title: str
    body: str


def _slug(name: str) -> str:
    """Slugify: lowercase, replace non-alnum with underscore."""
    return re.sub(r"[^a-z0-9]", "_", name.lower())


def _check_universal_banned(value: str, param: str) -> None:
    """Raise AdoptRecipeError if value contains any universally-banned char."""
    for bad in _BANNED_UNIVERSAL:
        if bad in value:
            raise AdoptRecipeError(
                f"Parameter {param!r} contains a forbidden character: {bad!r}."
            )


def _validate_name(name: str, label: str) -> None:
    _check_universal_banned(name, label)
    if not _NAME_RE.fullmatch(name):
        raise AdoptRecipeError(
            f"{name!r} is not a valid {label} — must start with an alphanumeric "
            "character and contain only alphanumeric, '.', '_', '~', '+', '%', '-'."
        )


def _validate_location(location: str) -> None:
    _check_universal_banned(location, "location")
    if not _LOCATION_RE.fullmatch(location):
        raise AdoptRecipeError(
            f"{location!r} is not a valid location — must start with a letter "
            "and contain only alphanumeric and '-' characters."
        )


def _validate_image(image: str) -> None:
    _check_universal_banned(image, "image")
    if not _IMAGE_RE.fullmatch(image):
        raise AdoptRecipeError(
            f"{image!r} is not a valid container image reference."
        )


def _validate_topic_short(topic: str) -> None:
    """Validate a short (bare) topic name."""
    _check_universal_banned(topic, "topic")
    if not _TOPIC_SHORT_RE.fullmatch(topic):
        raise AdoptRecipeError(
            f"{topic!r} is not a valid topic name."
        )


def _reject_control_plane(resource_type: str, name: str) -> None:
    """Refuse non-adoptable identities at the tool boundary (Codex 019eb932).

    Covers DriftScribe's own control-plane identities AND buckets a Google
    service auto-creates (the service-managed-bucket denylist rule). Same
    identity semantics as the denylist rules that would block the C2 plan
    anyway (and as infra_graph's node flag): rejecting HERE means chat gets an
    immediate, honest refusal instead of authoring a PR that is guaranteed to
    be blocked at plan evaluation. Type-scoped exactly like the rules —
    Pub/Sub's only identity rule is the Eventarc trigger-transport prefix
    (service-managed, like the auto-created buckets).
    """
    if resource_type == "google_storage_bucket" and name.endswith(
        CONTROL_PLANE_BUCKET_SUFFIXES
    ):
        raise AdoptRecipeError(
            f"{name!r} cannot be adopted: bucket names ending in -tofu-state "
            "or -tofu-artifacts are IaC control-plane infrastructure, and "
            "the always-on denylist refuses any plan that would change or "
            f"import them. {FINAL_REFUSAL_MARKER}"
        )
    if resource_type == "google_storage_bucket" and is_service_managed_bucket_name(
        name
    ):
        raise AdoptRecipeError(
            f"{name!r} cannot be adopted: it is a bucket that a Google service "
            "auto-creates (Cloud Build, App Engine, Cloud Functions, or Cloud "
            "Run source deploys), not a resource you provisioned — the "
            "always-on denylist refuses any plan that would change or import "
            f"it. {FINAL_REFUSAL_MARKER}"
        )
    if resource_type in (
        "google_pubsub_topic",
        "google_pubsub_subscription",
    ) and is_service_managed_pubsub_name(name):
        raise AdoptRecipeError(
            f"{name!r} cannot be adopted: it is a Pub/Sub resource that "
            "Eventarc creates automatically to deliver a trigger's events, "
            "not a resource you provisioned — the always-on denylist refuses "
            f"any plan that would change or import it. {FINAL_REFUSAL_MARKER}"
        )
    if (
        resource_type == "google_cloud_run_v2_service"
        and name in CONTROL_PLANE_SERVICE_NAMES
    ):
        raise AdoptRecipeError(
            f"{name!r} cannot be adopted: it is one of DriftScribe's own "
            "control-plane services, and the always-on denylist refuses any "
            f"plan that would change or import it. {FINAL_REFUSAL_MARKER}"
        )


def render_adoption(
    resource_type: str,
    name: str,
    project: str,
    *,
    location: str | None = None,
    topic: str | None = None,
    image: str | None = None,
) -> AdoptRendering:
    """Render the resource block + co-located import block for ONE resource.

    Parameters
    ----------
    resource_type:
        One of the four adoptable HCL resource types.
    name:
        The resource's short name (no slash, no whitespace).
    project:
        The runtime GCP project id (server-side constant, not an LLM param).
    location:
        Required for bucket and Cloud Run service; forbidden for topic/subscription.
    topic:
        Required for subscription. Short topic name OR the full-path
        ``projects/<P>/topics/<N>`` form (normalized iff ``<P> == project``).
    image:
        Required for Cloud Run service; forbidden for other types.
    """
    canonical = _canonicalize_resource_type(resource_type)
    if canonical is None:
        choices = ", ".join(
            f"{t} ({_HUMAN[t]})" for t in sorted(ADOPT_KINDS)
        )
        raise AdoptRecipeError(
            f"{resource_type!r} is not an adoptable resource type. Pass "
            f"resource_type as one of: {choices}. If the operator's resource "
            "is none of these, DriftScribe cannot adopt it."
        )
    resource_type = canonical
    if not _PROJECT_RE.fullmatch(project):
        raise AdoptRecipeError("internal: invalid runtime project id")

    _validate_name(name, _HUMAN[resource_type] + " name")
    _reject_control_plane(resource_type, name)

    short = _SHORT[resource_type]
    slug = _slug(name)
    address = f"{resource_type}.adopt_{slug}"
    path = f"iac/adopt_{short}_{slug}.tf"
    human_type = _HUMAN[resource_type]

    if resource_type == "google_storage_bucket":
        _enforce_forbidden("topic", topic, resource_type)
        _enforce_forbidden("image", image, resource_type)
        if not location:
            raise AdoptRecipeError(
                f"I need the location for {name!r} to adopt it. "
                "For example: 'asia-northeast1' or 'US'."
            )
        _validate_location(location)
        import_id = name
        content = _render_bucket(name, project, location, slug)

    elif resource_type == "google_pubsub_topic":
        _enforce_forbidden("location", location, resource_type)
        _enforce_forbidden("image", image, resource_type)
        _enforce_forbidden("topic", topic, resource_type)
        import_id = f"projects/{project}/topics/{name}"
        content = _render_topic(name, project, slug)

    elif resource_type == "google_pubsub_subscription":
        _enforce_forbidden("location", location, resource_type)
        _enforce_forbidden("image", image, resource_type)
        if not topic:
            raise AdoptRecipeError(
                f"I need the topic this subscription {name!r} belongs to. "
                "Please provide the topic name."
            )
        # Normalize full-path topic input
        topic_short = _normalize_topic(topic, project)
        import_id = f"projects/{project}/subscriptions/{name}"
        content = _render_subscription(name, project, topic_short, slug)

    elif resource_type == "google_cloud_run_v2_service":
        _enforce_forbidden("topic", topic, resource_type)
        if not location:
            raise AdoptRecipeError(
                f"I need the location for the Cloud Run service {name!r}. "
                "For example: 'asia-northeast1'."
            )
        if not image:
            raise AdoptRecipeError(
                f"I need the exact container image the Cloud Run service "
                f"{name!r} is running. Please check the service details."
            )
        _validate_location(location)
        _validate_image(image)
        import_id = f"projects/{project}/locations/{location}/services/{name}"
        content = _render_run_service(name, project, location, image, slug)

    else:
        # Unreachable — guarded above
        raise AdoptRecipeError(f"unsupported type: {resource_type!r}")

    # Defense in depth: import id must match the per-type shape
    if not _ID_SHAPES[resource_type].fullmatch(import_id):
        raise AdoptRecipeError(
            f"internal: rendered import id {import_id!r} does not match "
            f"expected shape for {resource_type}"
        )

    title = f"Adopt {human_type} {name} into IaC management (zero-change import)"
    body = _render_body(resource_type, name, import_id, address, path)

    return AdoptRendering(
        path=path,
        content=content,
        address=address,
        import_id=import_id,
        title=title,
        body=body,
    )


def _enforce_forbidden(param: str, value: object, resource_type: str) -> None:
    """Raise if a param that is forbidden for this type is non-empty."""
    if value:
        raise AdoptRecipeError(
            f"Parameter {param!r} is not applicable when adopting a "
            f"{_HUMAN[resource_type]}."
        )


def _normalize_topic(topic: str, project: str) -> str:
    """Return the short topic name from either a short or full-path input.

    Full-path form ``projects/<P>/topics/<N>`` is accepted only when ``<P>``
    equals the runtime project. Any other project raises ``AdoptRecipeError``.
    Short (bare) form is validated and returned unchanged.
    """
    _check_universal_banned(topic, "topic")
    full_path_re = re.compile(r"^projects/([^/\s]+)/topics/([^/\s]+)$")
    m = full_path_re.fullmatch(topic)
    if m:
        given_project, topic_name = m.group(1), m.group(2)
        if given_project != project:
            raise AdoptRecipeError(
                f"cross-project subscription adoption is not supported. "
                f"Topic project {given_project!r} differs from the deployment "
                f"project {project!r}."
            )
        # The extracted segment must pass the same shape rules as a short-form
        # topic: [^/\s]+ alone still admits HCL template metacharacters like
        # "%{" which would render an unparseable file (Opus review catch).
        _validate_topic_short(topic_name)
        return topic_name
    # Short form
    _validate_topic_short(topic)
    return topic


# ---------------------------------------------------------------------------
# HCL renderers — exact probe-proven shapes
# ---------------------------------------------------------------------------

_HEADER = """\
# Adopted into IaC management by DriftScribe (zero-change import).
# The import block is retained as a permanent audit record
# (adopt design 2026-06-11 §3).
"""


def _render_bucket(name: str, project: str, location: str, slug: str) -> str:
    return (
        f"{_HEADER}"
        f'resource "google_storage_bucket" "adopt_{slug}" {{\n'
        f'  name     = "{name}"\n'
        f'  project  = var.project_id\n'
        f'  location = "{location}"\n'
        f'}}\n'
        f'\n'
        f'import {{\n'
        f'  to = google_storage_bucket.adopt_{slug}\n'
        f'  id = "{name}"\n'
        f'}}\n'
    )


def _render_topic(name: str, project: str, slug: str) -> str:
    return (
        f"{_HEADER}"
        f'resource "google_pubsub_topic" "adopt_{slug}" {{\n'
        f'  project = var.project_id\n'
        f'  name    = "{name}"\n'
        f'}}\n'
        f'\n'
        f'import {{\n'
        f'  to = google_pubsub_topic.adopt_{slug}\n'
        f'  id = "projects/{project}/topics/{name}"\n'
        f'}}\n'
    )


def _render_subscription(name: str, project: str, topic_short: str, slug: str) -> str:
    return (
        f"{_HEADER}"
        f'resource "google_pubsub_subscription" "adopt_{slug}" {{\n'
        f'  project = var.project_id\n'
        f'  name    = "{name}"\n'
        f'  topic   = "projects/{project}/topics/{topic_short}"\n'
        f'}}\n'
        f'\n'
        f'import {{\n'
        f'  to = google_pubsub_subscription.adopt_{slug}\n'
        f'  id = "projects/{project}/subscriptions/{name}"\n'
        f'}}\n'
    )


def _render_run_service(
    name: str, project: str, location: str, image: str, slug: str
) -> str:
    return (
        f"{_HEADER}"
        f'resource "google_cloud_run_v2_service" "adopt_{slug}" {{\n'
        f'  name     = "{name}"\n'
        f'  location = "{location}"\n'
        f'  project  = var.project_id\n'
        f'\n'
        f'  template {{\n'
        f'    containers {{\n'
        f'      image = "{image}"\n'
        f'    }}\n'
        f'  }}\n'
        f'\n'
        f'  lifecycle {{\n'
        f'    ignore_changes = [client, client_version, scaling]\n'
        f'  }}\n'
        f'}}\n'
        f'\n'
        f'import {{\n'
        f'  to = google_cloud_run_v2_service.adopt_{slug}\n'
        f'  id = "projects/{project}/locations/{location}/services/{name}"\n'
        f'}}\n'
    )


def _render_body(
    resource_type: str,
    name: str,
    import_id: str,
    address: str,
    path: str,
) -> str:
    """Compose the deterministic PR body."""
    human = _HUMAN[resource_type]
    return (
        f"## Adopt {human} `{name}` into IaC management\n\n"
        f"**What:** Brings the existing live {human} `{name}` under OpenTofu management "
        f"via a zero-change import.\n\n"
        f"**Why:** The resource already exists in GCP but is not tracked in IaC state. "
        f"This PR adds it to the IaC configuration and imports it — nothing in the cloud "
        f"will be modified.\n\n"
        f"**Import id:** `{import_id}`\n\n"
        f"**HCL address:** `{address}`\n\n"
        f"**File:** `{path}`\n\n"
        f"**Zero-change promise:** The C2 plan must show a pure no-op import "
        f"(`actions: [\"no-op\"] + importing`) or the denylist will refuse it. "
        f"If the plan shows changes, the resource's live settings deviate from defaults "
        f"in ways DriftScribe cannot read — do not approve; regenerate with the differing "
        f"settings instead.\n\n"
        f"**Create-class (C6 re-bake required):** An adoption is create-class — after "
        f"approval and merge, the apply worker must be re-baked (C6) before the import "
        f"can apply. Applying it changes NOTHING in the cloud; it only records the "
        f"resource in IaC state.\n\n"
        f"**Import block retention:** The import block is kept permanently as an audit "
        f"record (adopt design 2026-06-11 §3 — retained import blocks become inert "
        f"after the first successful apply).\n\n"
        f"**Reference:** docs/plans/2026-06-11-adopt-import-design.md\n"
    )


# ---------------------------------------------------------------------------
# Freehand-import guard (§1.10 — coordinator-side only)
# ---------------------------------------------------------------------------

def find_import_violations(files: list[dict]) -> list[str]:
    """Return a list of violation strings for any file containing an ``import`` block.

    Called at the two generic authoring sites (``open_infra_pr_tool`` and the
    fan-out merged-files site) before any worker call. Only ``propose_adoption_tool``
    passes ``allow_import=True`` through the shared tail, bypassing this guard.

    Rules:
    - ``.md`` files are skipped (Markdown, not HCL).
    - Any ``.tf`` file that fails to parse → violation (fail-closed).
    - Any ``.tf`` file with an ``import`` block → violation.
    - Non-``.tf`` / non-``.md`` files are ignored (the worker handles them).
    """
    from driftscribe_lib.iac_hcl import iter_blocks, parse_hcl

    violations: list[str] = []
    for f in files:
        path = f.get("path", "")
        if path.endswith(".md"):
            continue
        if not path.endswith(".tf"):
            continue
        content = f.get("content", "")
        parsed = parse_hcl(content)
        if parsed is None:
            violations.append(
                f"{path}: does not parse as HCL — cannot verify import-free"
            )
            continue
        if iter_blocks(parsed, "import"):
            violations.append(f"{path}: contains an import block")
    return violations


# ---------------------------------------------------------------------------
# Main-tree preflight (§1.11)
# ---------------------------------------------------------------------------

def preflight_conflicts(
    rendering: AdoptRendering,
    iac_files: dict[str, str],
    runtime_project: str,
) -> str | None:
    """Check for conflicts between the rendering and the current main-tree IaC files.

    Returns a human-readable rejection reason string, or ``None`` if clean.

    Checks (fail-closed on parse errors):
    a) The rendered path does not already exist in the tree.
    b) The rendered address is not already declared.
    c) The import id is not already a declared identity for the same asset type.
    d) ``variables.tf`` ``project_id`` default equals ``runtime_project``
       (the literal-id / var-body consistency assumption).
    e) Parse errors in the fetched tree → fail-closed (collision checks incomplete).
    """
    from driftscribe_lib.iac_hcl import extract_declared_identities

    # (a) Path collision
    if rendering.path in iac_files:
        return (
            f"File {rendering.path!r} already exists in the IaC tree — "
            "this resource may have already been adopted, or a different resource "
            "has the same slug."
        )

    # (e) Parse errors — fail-closed
    identities, parse_errors = extract_declared_identities(iac_files)
    if parse_errors:
        return (
            f"Could not verify the current IaC tree — the following file(s) failed "
            f"to parse: {', '.join(parse_errors)}. Resolve the parse errors first."
        )

    # (b) Address collision
    declared_addresses = {d.address for d in identities}
    if rendering.address in declared_addresses:
        return (
            f"Address {rendering.address!r} is already declared in the IaC tree. "
            "Choose a different name or check if this resource is already managed."
        )

    # (c) Identity collision: same (asset_type, identity) already declared
    asset_type = _RTYPE_TO_ASSET_TYPE.get(rendering.address.split(".")[0])
    for d in identities:
        if d.identity == rendering.import_id and d.asset_type == asset_type:
            return (
                f"Identity {rendering.import_id!r} ({asset_type}) is already declared "
                f"in the IaC tree (at {d.address!r}). This resource appears to be "
                "already managed or previously adopted."
            )

    # (d) Project-id default mismatch
    proj_default = _extract_project_id_default(iac_files)
    if proj_default is not None and proj_default != runtime_project:
        return (
            f"IaC project_id default {proj_default!r} does not match the deployment "
            f"project {runtime_project!r}. This is a deployment/IaC mismatch — "
            "check your configuration before adopting resources."
        )

    return None


def _extract_project_id_default(iac_files: dict[str, str]) -> str | None:
    """Extract the ``variable "project_id" { default = "..." }`` from the IaC tree.

    Returns the default string or ``None`` if not found / not a literal string.
    """
    from driftscribe_lib.iac_hcl import block_label, is_meta_key, iter_blocks, parse_hcl, unwrap

    for path, content in iac_files.items():
        if "variables" not in path and "variable" not in content:
            continue
        parsed = parse_hcl(content)
        if parsed is None:
            continue
        for var_block in iter_blocks(parsed, "variable"):
            for name, body in var_block.items():
                if is_meta_key(name):
                    continue
                if block_label(name) == "project_id":
                    if isinstance(body, dict):
                        raw_default = body.get("default")
                        if raw_default is not None:
                            return unwrap(str(raw_default)) if not isinstance(raw_default, str) else unwrap(raw_default)
    return None
