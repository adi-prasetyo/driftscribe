"""Tests for driftscribe_lib.adopt_recipe — the probe-proven adopt renderer.

Covers:
- Golden exact-bytes per type (bucket / topic / subscription / run service)
- Validation matrix rows from the plan §2
- Rendered output parses and passes the static gate
- Identity consistency with extract_declared_identities
- ID-shape drift pin against tools.iac_static_gate.ADOPT_IMPORT_ID_SHAPES
- Key-set equality with ADOPTABLE_RESOURCE_TYPES
- rtype→CAI-asset-type drift pin against iac_hcl._SUPPORTED_RESOURCE_ASSET_TYPES
"""
from __future__ import annotations

import pytest

from driftscribe_lib.adopt_recipe import (
    AdoptRecipeError,
    FINAL_REFUSAL_MARKER,
    _ID_SHAPES,
    _RTYPE_TO_ASSET_TYPE,
    render_adoption,
)

_PROJECT = "driftscribe-hack-2026"

# ---------------------------------------------------------------------------
# Goldens — byte-exact probe-proven shapes (§0.2 / §Task 1)
# ---------------------------------------------------------------------------

_GOLDEN_BUCKET = """\
# Adopted into IaC management by DriftScribe (zero-change import).
# The import block is retained as a permanent audit record
# (adopt design 2026-06-11 §3).
resource "google_storage_bucket" "adopt_my_bucket" {
  name     = "my-bucket"
  project  = var.project_id
  location = "asia-northeast1"
}

import {
  to = google_storage_bucket.adopt_my_bucket
  id = "my-bucket"
}
"""

_GOLDEN_TOPIC = """\
# Adopted into IaC management by DriftScribe (zero-change import).
# The import block is retained as a permanent audit record
# (adopt design 2026-06-11 §3).
resource "google_pubsub_topic" "adopt_my_topic" {
  project = var.project_id
  name    = "my-topic"
}

import {
  to = google_pubsub_topic.adopt_my_topic
  id = "projects/driftscribe-hack-2026/topics/my-topic"
}
"""

_GOLDEN_SUB = """\
# Adopted into IaC management by DriftScribe (zero-change import).
# The import block is retained as a permanent audit record
# (adopt design 2026-06-11 §3).
resource "google_pubsub_subscription" "adopt_my_sub" {
  project = var.project_id
  name    = "my-sub"
  topic   = "projects/driftscribe-hack-2026/topics/my-topic"
}

import {
  to = google_pubsub_subscription.adopt_my_sub
  id = "projects/driftscribe-hack-2026/subscriptions/my-sub"
}
"""

_GOLDEN_RUN = """\
# Adopted into IaC management by DriftScribe (zero-change import).
# The import block is retained as a permanent audit record
# (adopt design 2026-06-11 §3).
resource "google_cloud_run_v2_service" "adopt_my_svc" {
  name     = "my-svc"
  location = "asia-northeast1"
  project  = var.project_id

  template {
    containers {
      image = "gcr.io/cloudrun/hello"
    }
  }

  lifecycle {
    ignore_changes = [client, client_version, scaling]
  }
}

import {
  to = google_cloud_run_v2_service.adopt_my_svc
  id = "projects/driftscribe-hack-2026/locations/asia-northeast1/services/my-svc"
}
"""

_VARIABLES_STUB = f"""\
variable "project_id" {{
  default = "{_PROJECT}"
}}
"""


# ---------------------------------------------------------------------------
# Golden exact-bytes tests
# ---------------------------------------------------------------------------

def test_golden_bucket():
    r = render_adoption(
        "google_storage_bucket",
        "my-bucket",
        _PROJECT,
        location="asia-northeast1",
    )
    assert r.content == _GOLDEN_BUCKET
    assert r.path == "iac/adopt_bucket_my_bucket.tf"
    assert r.address == "google_storage_bucket.adopt_my_bucket"
    assert r.import_id == "my-bucket"
    assert "Adopt Cloud Storage bucket my-bucket into IaC management" in r.title


def test_golden_topic():
    r = render_adoption(
        "google_pubsub_topic",
        "my-topic",
        _PROJECT,
    )
    assert r.content == _GOLDEN_TOPIC
    assert r.path == "iac/adopt_topic_my_topic.tf"
    assert r.address == "google_pubsub_topic.adopt_my_topic"
    assert r.import_id == f"projects/{_PROJECT}/topics/my-topic"
    assert "Adopt Pub/Sub topic my-topic" in r.title


def test_golden_subscription():
    r = render_adoption(
        "google_pubsub_subscription",
        "my-sub",
        _PROJECT,
        topic="my-topic",
    )
    assert r.content == _GOLDEN_SUB
    assert r.path == "iac/adopt_subscription_my_sub.tf"
    assert r.address == "google_pubsub_subscription.adopt_my_sub"
    assert r.import_id == f"projects/{_PROJECT}/subscriptions/my-sub"
    assert "Adopt Pub/Sub subscription my-sub" in r.title


def test_golden_run():
    r = render_adoption(
        "google_cloud_run_v2_service",
        "my-svc",
        _PROJECT,
        location="asia-northeast1",
        image="gcr.io/cloudrun/hello",
    )
    assert r.content == _GOLDEN_RUN
    assert r.path == "iac/adopt_service_my_svc.tf"
    assert r.address == "google_cloud_run_v2_service.adopt_my_svc"
    assert r.import_id == f"projects/{_PROJECT}/locations/asia-northeast1/services/my-svc"
    assert "Adopt Cloud Run service my-svc" in r.title


# ---------------------------------------------------------------------------
# Subscription full-path topic normalization
# ---------------------------------------------------------------------------

def test_subscription_full_path_topic_normalized():
    """Full-path topic input with the SAME project normalizes to the golden."""
    r_short = render_adoption(
        "google_pubsub_subscription",
        "my-sub",
        _PROJECT,
        topic="my-topic",
    )
    r_full = render_adoption(
        "google_pubsub_subscription",
        "my-sub",
        _PROJECT,
        topic=f"projects/{_PROJECT}/topics/my-topic",
    )
    assert r_short.content == r_full.content


def test_subscription_cross_project_topic_rejected():
    """Full-path topic input with a DIFFERENT project is rejected."""
    with pytest.raises(AdoptRecipeError, match="cross-project"):
        render_adoption(
            "google_pubsub_subscription",
            "my-sub",
            _PROJECT,
            topic="projects/other-project-12345/topics/my-topic",
        )


def test_subscription_full_path_topic_malformed_segment_rejected():
    """The extracted topic segment of a full-path input is re-validated.

    ``[^/\\s]+`` alone admits HCL template metacharacters: without the
    re-validation, ``a%{b`` would render ``topic = "...a%{b"`` — an
    incomplete template directive that does not parse as HCL (Opus review
    catch). Must be rejected at render time, not at the worker.
    """
    with pytest.raises(AdoptRecipeError, match="not a valid topic name"):
        render_adoption(
            "google_pubsub_subscription",
            "my-sub",
            _PROJECT,
            topic=f"projects/{_PROJECT}/topics/a%{{b",
        )


# ---------------------------------------------------------------------------
# Missing required params
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("rtype", "kwargs", "match"),
    [
        # bucket: location required
        ("google_storage_bucket", {}, "location"),
        # subscription: topic required
        ("google_pubsub_subscription", {}, "topic"),
        # run: location required
        ("google_cloud_run_v2_service", {"image": "gcr.io/cloudrun/hello"}, "location"),
        # run: image required
        ("google_cloud_run_v2_service", {"location": "asia-northeast1"}, "image"),
    ],
)
def test_missing_required_param_rejected(rtype, kwargs, match):
    with pytest.raises(AdoptRecipeError, match=match):
        render_adoption(rtype, "my-resource", _PROJECT, **kwargs)


# ---------------------------------------------------------------------------
# Forbidden extra params
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("rtype", "kwargs"),
    [
        # topic: location forbidden
        ("google_pubsub_topic", {"location": "asia-northeast1"}),
        # topic: image forbidden
        ("google_pubsub_topic", {"image": "gcr.io/cloudrun/hello"}),
        # bucket: topic forbidden
        ("google_storage_bucket", {"location": "asia-northeast1", "topic": "my-topic"}),
        # bucket: image forbidden
        ("google_storage_bucket", {"location": "asia-northeast1", "image": "img"}),
        # sub: image forbidden
        ("google_pubsub_subscription", {"topic": "my-topic", "image": "img"}),
    ],
)
def test_forbidden_param_rejected(rtype, kwargs):
    with pytest.raises(AdoptRecipeError):
        render_adoption(rtype, "my-resource", _PROJECT, **kwargs)


# ---------------------------------------------------------------------------
# Non-adoptable type rejected, naming the allowlist
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "rtype",
    [
        "google_service_account",
        "google_compute_instance",
        "google_sql_database_instance",
    ],
)
def test_non_adoptable_type_rejected_names_allowlist(rtype):
    """The rejection is param-explicit: it tells the model HOW to pass
    resource_type (canonical values + human names) so a wrong-parameter call
    is never read as a product limitation (live e2e 2026-06-11 catch)."""
    with pytest.raises(
        AdoptRecipeError, match="not an adoptable resource type"
    ) as exc_info:
        render_adoption(rtype, "my-resource", _PROJECT, location="us-central1")
    assert "google_storage_bucket (Cloud Storage bucket)" in str(exc_info.value)


@pytest.mark.parametrize(
    ("alias", "canonical", "kwargs"),
    [
        ("Cloud Storage bucket", "google_storage_bucket",
         {"location": "asia-northeast1"}),
        ("bucket", "google_storage_bucket", {"location": "asia-northeast1"}),
        ("GCS bucket", "google_storage_bucket", {"location": "asia-northeast1"}),
        ("Pub/Sub topic", "google_pubsub_topic", {}),
        ("topic", "google_pubsub_topic", {}),
        ("Pub/Sub subscription", "google_pubsub_subscription",
         {"topic": "my-topic"}),
        ("subscription", "google_pubsub_subscription", {"topic": "my-topic"}),
        ("Cloud Run service", "google_cloud_run_v2_service",
         {"location": "asia-northeast1", "image": "gcr.io/cloudrun/hello"}),
        ("run service", "google_cloud_run_v2_service",
         {"location": "asia-northeast1", "image": "gcr.io/cloudrun/hello"}),
    ],
)
def test_friendly_type_alias_renders_identically_to_canonical(
    alias, canonical, kwargs
):
    """LLM-facing robustness: a friendly resource_type alias produces the
    EXACT same rendering as the canonical HCL type string (live e2e
    2026-06-11: the model passed the human name and gave up on rejection)."""
    r_alias = render_adoption(alias, "my-res", _PROJECT, **kwargs)
    r_canonical = render_adoption(canonical, "my-res", _PROJECT, **kwargs)
    assert r_alias == r_canonical


# ---------------------------------------------------------------------------
# Injection chars rejected
# ---------------------------------------------------------------------------

# chars banned in ALL params
_BANNED_ALL = ['"', "${", "\n", "\\", " "]
# slash additionally banned in name and location (but NOT image or topic-short-form)
_BANNED_NAME_LOCATION = ["/"]


@pytest.mark.parametrize("bad_char", _BANNED_ALL)
def test_injection_chars_rejected_in_name(bad_char):
    with pytest.raises(AdoptRecipeError):
        render_adoption("google_pubsub_topic", f"my{bad_char}topic", _PROJECT)


@pytest.mark.parametrize("bad_char", _BANNED_NAME_LOCATION)
def test_slash_rejected_in_name(bad_char):
    with pytest.raises(AdoptRecipeError):
        render_adoption("google_pubsub_topic", f"my{bad_char}topic", _PROJECT)


@pytest.mark.parametrize("bad_char", _BANNED_ALL)
def test_injection_chars_rejected_in_location(bad_char):
    with pytest.raises(AdoptRecipeError):
        render_adoption(
            "google_storage_bucket",
            "my-bucket",
            _PROJECT,
            location=f"asia{bad_char}northeast1",
        )


@pytest.mark.parametrize("bad_char", _BANNED_NAME_LOCATION)
def test_slash_rejected_in_location(bad_char):
    with pytest.raises(AdoptRecipeError):
        render_adoption(
            "google_storage_bucket",
            "my-bucket",
            _PROJECT,
            location=f"asia{bad_char}northeast1",
        )


@pytest.mark.parametrize("bad_char", _BANNED_ALL)
def test_injection_chars_rejected_in_image(bad_char):
    with pytest.raises(AdoptRecipeError):
        render_adoption(
            "google_cloud_run_v2_service",
            "my-svc",
            _PROJECT,
            location="asia-northeast1",
            image=f"gcr.io/cloudrun/hello{bad_char}",
        )


def test_slash_allowed_in_image():
    """Slash is allowed in image (artifact ref paths contain /)."""
    r = render_adoption(
        "google_cloud_run_v2_service",
        "my-svc",
        _PROJECT,
        location="asia-northeast1",
        image="gcr.io/cloudrun/hello",
    )
    assert "/" in r.content


@pytest.mark.parametrize("bad_char", _BANNED_ALL)
def test_injection_chars_rejected_in_topic(bad_char):
    with pytest.raises(AdoptRecipeError):
        render_adoption(
            "google_pubsub_subscription",
            "my-sub",
            _PROJECT,
            topic=f"my{bad_char}topic",
        )


# ---------------------------------------------------------------------------
# Static gate clean
# ---------------------------------------------------------------------------

def test_rendered_output_parses_and_passes_static_gate():
    """All 4 rendered outputs: hcl2 parses + gate returns no violations."""
    from tools.iac_static_gate import GateInput, GateMode, evaluate

    cases = [
        render_adoption("google_storage_bucket", "my-bucket", _PROJECT, location="asia-northeast1"),
        render_adoption("google_pubsub_topic", "my-topic", _PROJECT),
        render_adoption("google_pubsub_subscription", "my-sub", _PROJECT, topic="my-topic"),
        render_adoption(
            "google_cloud_run_v2_service",
            "my-svc",
            _PROJECT,
            location="asia-northeast1",
            image="gcr.io/cloudrun/hello",
        ),
    ]
    for r in cases:
        gi = GateInput(
            mode=GateMode.AGENT,
            changed_paths=(r.path,),
            hcl_files={r.path: r.content},
        )
        violations = evaluate(gi)
        assert violations == [], f"Gate violations for {r.address}: {[v.rule for v in violations]}"


# ---------------------------------------------------------------------------
# Identity consistency
# ---------------------------------------------------------------------------

def test_rendered_identity_consistency():
    """extract_declared_identities returns a high-conf import entry for the
    import id, AND the derived_resource path resolves to the same identity
    (so the de-dup correctly collapses them to the higher-confidence entry).

    The de-dup in extract_declared_identities keeps ONE entry per
    (asset_type, identity) pair, preferring high over derived. So for a correct
    adoption both paths resolve to the SAME identity string, and the final list
    has the high-confidence import_id entry (derived gets merged into it).
    We verify: (a) high-conf entry present, (b) derived path would also resolve
    correctly by parsing the resource block alone (without the import block).
    """
    from driftscribe_lib.iac_hcl import extract_declared_identities, parse_hcl, iter_typed_blocks, _variable_defaults, _derive_identity

    cases = [
        (render_adoption("google_storage_bucket", "my-bucket", _PROJECT, location="asia-northeast1"),),
        (render_adoption("google_pubsub_topic", "my-topic", _PROJECT),),
        (render_adoption("google_pubsub_subscription", "my-sub", _PROJECT, topic="my-topic"),),
        (render_adoption(
            "google_cloud_run_v2_service", "my-svc", _PROJECT,
            location="asia-northeast1", image="gcr.io/cloudrun/hello",
        ),),
    ]
    for (r,) in cases:
        files = {r.path: r.content, "variables.tf": _VARIABLES_STUB}
        identities, parse_errors = extract_declared_identities(files)
        assert not parse_errors, f"parse errors for {r.address}: {parse_errors}"

        # (a) High-confidence import_id entry must be present
        high_conf = [d for d in identities if d.confidence == "high" and d.identity == r.import_id]
        assert high_conf, f"No high-conf identity {r.import_id!r} for {r.address}"

        # (b) The derived_resource path also resolves to the same identity:
        # parse just the resource content (without import block) and check.
        parsed = parse_hcl(r.content)
        assert parsed is not None, f"Content of {r.address} failed to parse"
        var_defaults = _variable_defaults({r.path: parsed, "variables.tf": parse_hcl(_VARIABLES_STUB)})
        rtype = r.address.split(".")[0]
        found_derived = None
        for t, name, body in iter_typed_blocks(parsed, "resource"):
            if t == rtype:
                found_derived = _derive_identity(rtype, body, var_defaults)
                break
        assert found_derived == r.import_id, (
            f"Derived identity {found_derived!r} != import_id {r.import_id!r} for {r.address}"
        )


# ---------------------------------------------------------------------------
# ID-shape drift pin
# ---------------------------------------------------------------------------

def test_id_shapes_match_static_gate():
    """_ID_SHAPES patterns must equal ADOPT_IMPORT_ID_SHAPES patterns (drift pin)."""
    from tools.iac_static_gate import ADOPT_IMPORT_ID_SHAPES

    assert set(_ID_SHAPES.keys()) == set(ADOPT_IMPORT_ID_SHAPES.keys()), (
        "Key sets diverged between adopt_recipe._ID_SHAPES and "
        "iac_static_gate.ADOPT_IMPORT_ID_SHAPES"
    )
    for rtype in _ID_SHAPES:
        assert _ID_SHAPES[rtype].pattern == ADOPT_IMPORT_ID_SHAPES[rtype].pattern, (
            f"Pattern mismatch for {rtype}: "
            f"{_ID_SHAPES[rtype].pattern!r} != {ADOPT_IMPORT_ID_SHAPES[rtype].pattern!r}"
        )


def test_id_shapes_key_set_equals_adoptable_resource_types():
    """_ID_SHAPES key set == ADOPTABLE_RESOURCE_TYPES (drift pin)."""
    from driftscribe_lib.iac_plan_denylist import ADOPTABLE_RESOURCE_TYPES

    assert set(_ID_SHAPES.keys()) == set(ADOPTABLE_RESOURCE_TYPES), (
        f"Key mismatch: _ID_SHAPES={sorted(_ID_SHAPES)}, "
        f"ADOPTABLE_RESOURCE_TYPES={sorted(ADOPTABLE_RESOURCE_TYPES)}"
    )


def test_rtype_to_asset_type_drift_pin():
    """_RTYPE_TO_ASSET_TYPE must match iac_hcl._SUPPORTED_RESOURCE_ASSET_TYPES
    for the four adoptable types."""
    from driftscribe_lib.iac_hcl import _SUPPORTED_RESOURCE_ASSET_TYPES
    from driftscribe_lib.iac_plan_denylist import ADOPTABLE_RESOURCE_TYPES

    for rtype in ADOPTABLE_RESOURCE_TYPES:
        assert rtype in _RTYPE_TO_ASSET_TYPE, f"{rtype} not in _RTYPE_TO_ASSET_TYPE"
        assert _RTYPE_TO_ASSET_TYPE[rtype] == _SUPPORTED_RESOURCE_ASSET_TYPES[rtype], (
            f"Asset type mismatch for {rtype}: "
            f"{_RTYPE_TO_ASSET_TYPE[rtype]!r} != {_SUPPORTED_RESOURCE_ASSET_TYPES[rtype]!r}"
        )


# ---------------------------------------------------------------------------
# AdoptRendering is a frozen dataclass
# ---------------------------------------------------------------------------

def test_adopt_rendering_is_frozen():
    import dataclasses
    r = render_adoption("google_pubsub_topic", "my-topic", _PROJECT)
    assert dataclasses.is_dataclass(r)
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        r.path = "something"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# preflight_conflicts tests
# ---------------------------------------------------------------------------

def test_preflight_no_conflict_returns_none():
    """Clean IaC tree → preflight returns None (no conflict)."""
    from driftscribe_lib.adopt_recipe import preflight_conflicts

    r = render_adoption("google_pubsub_topic", "my-topic", _PROJECT)
    iac_files = {
        "iac/existing.tf": (
            'resource "google_pubsub_topic" "existing" {\n'
            '  project = var.project_id\n'
            '  name    = "other-topic"\n'
            '}\n'
        ),
        "iac/variables.tf": _VARIABLES_STUB,
    }
    assert preflight_conflicts(r, iac_files, _PROJECT) is None


def test_preflight_path_collision_rejected():
    from driftscribe_lib.adopt_recipe import preflight_conflicts

    r = render_adoption("google_pubsub_topic", "my-topic", _PROJECT)
    # Inject a file with the SAME path as the rendering
    iac_files = {r.path: "# existing file\n", "iac/variables.tf": _VARIABLES_STUB}
    result = preflight_conflicts(r, iac_files, _PROJECT)
    assert result is not None
    assert "already exists" in result.lower() or r.path in result


def test_preflight_address_collision_rejected():
    from driftscribe_lib.adopt_recipe import preflight_conflicts

    r = render_adoption("google_pubsub_topic", "my-topic", _PROJECT)
    # Inject a file that declares the same address
    iac_files = {
        "iac/other.tf": (
            'resource "google_pubsub_topic" "adopt_my_topic" {\n'
            '  project = var.project_id\n'
            '  name    = "my-topic"\n'
            '}\n'
        ),
        "iac/variables.tf": _VARIABLES_STUB,
    }
    result = preflight_conflicts(r, iac_files, _PROJECT)
    assert result is not None
    assert r.address in result or "already declared" in result.lower()


def test_preflight_identity_collision_rejected():
    from driftscribe_lib.adopt_recipe import preflight_conflicts

    r = render_adoption("google_pubsub_topic", "my-topic", _PROJECT)
    # Inject a file with an import block for the same id
    iac_files = {
        "iac/other.tf": (
            'resource "google_pubsub_topic" "other_name" {\n'
            '  project = var.project_id\n'
            '  name    = "my-topic"\n'
            '}\n\n'
            'import {\n'
            '  to = google_pubsub_topic.other_name\n'
            f'  id = "projects/{_PROJECT}/topics/my-topic"\n'
            '}\n'
        ),
        "iac/variables.tf": _VARIABLES_STUB,
    }
    result = preflight_conflicts(r, iac_files, _PROJECT)
    assert result is not None
    assert "already" in result.lower() or r.import_id in result


def test_preflight_project_mismatch_rejected():
    from driftscribe_lib.adopt_recipe import preflight_conflicts

    r = render_adoption("google_pubsub_topic", "my-topic", _PROJECT)
    # Simulate variables.tf with a different project_id default
    iac_files = {
        "iac/variables.tf": (
            'variable "project_id" {\n'
            '  default = "different-project-99"\n'
            '}\n'
        ),
    }
    result = preflight_conflicts(r, iac_files, _PROJECT)
    assert result is not None
    assert "project" in result.lower() or "mismatch" in result.lower()


def test_preflight_parse_error_fails_closed():
    from driftscribe_lib.adopt_recipe import preflight_conflicts

    r = render_adoption("google_pubsub_topic", "my-topic", _PROJECT)
    iac_files = {
        "iac/broken.tf": "this is not valid HCL {{{",
        "iac/variables.tf": _VARIABLES_STUB,
    }
    result = preflight_conflicts(r, iac_files, _PROJECT)
    assert result is not None  # fail-closed: parse error → reject
    assert "parse" in result.lower() or "broken" in result.lower() or "error" in result.lower()


# ---------------------------------------------------------------------------
# find_import_violations tests
# ---------------------------------------------------------------------------

def test_find_import_violations_clean_file_returns_empty():
    from driftscribe_lib.adopt_recipe import find_import_violations

    files = [
        {
            "path": "iac/topic.tf",
            "content": (
                'resource "google_pubsub_topic" "my_topic" {\n'
                '  project = var.project_id\n'
                '  name    = "my-topic"\n'
                '}\n'
            ),
        }
    ]
    assert find_import_violations(files) == []


def test_find_import_violations_import_block_detected():
    from driftscribe_lib.adopt_recipe import find_import_violations

    files = [
        {
            "path": "iac/adopt.tf",
            "content": (
                'resource "google_pubsub_topic" "my_topic" {\n'
                '  project = var.project_id\n'
                '  name    = "my-topic"\n'
                '}\n\n'
                'import {\n'
                '  to = google_pubsub_topic.my_topic\n'
                '  id = "projects/p/topics/my-topic"\n'
                '}\n'
            ),
        }
    ]
    violations = find_import_violations(files)
    assert len(violations) == 1
    assert "iac/adopt.tf" in violations[0]
    assert "import" in violations[0].lower()


def test_find_import_violations_unparseable_fails_closed():
    from driftscribe_lib.adopt_recipe import find_import_violations

    files = [
        {"path": "iac/broken.tf", "content": "this is not valid HCL {{{"},
    ]
    violations = find_import_violations(files)
    assert len(violations) == 1
    assert "iac/broken.tf" in violations[0]


def test_find_import_violations_skips_md_files():
    from driftscribe_lib.adopt_recipe import find_import_violations

    files = [
        {
            "path": "iac/README.md",
            "content": "# This has an import mention but is not HCL\n",
        }
    ]
    assert find_import_violations(files) == []


def test_find_import_violations_multiple_files():
    from driftscribe_lib.adopt_recipe import find_import_violations

    clean = {"path": "iac/clean.tf", "content": 'resource "x" "y" {}\n'}
    bad = {
        "path": "iac/bad.tf",
        "content": (
            'resource "google_pubsub_topic" "t" {\n'
            '  name = "t"\n}\n\n'
            'import {\n  to = google_pubsub_topic.t\n  id = "p"\n}\n'
        ),
    }
    violations = find_import_violations([clean, bad])
    assert len(violations) == 1
    assert "iac/bad.tf" in violations[0]


class TestControlPlaneRefusal:
    def test_control_plane_bucket_is_rejected_with_explicit_reason(self):
        with pytest.raises(AdoptRecipeError) as ei:
            render_adoption(
                "google_storage_bucket",
                "driftscribe-hack-2026-tofu-artifacts",
                "driftscribe-hack-2026",
                location="asia-northeast1",
            )
        msg = str(ei.value)
        assert "cannot be adopted" in msg
        assert "denylist" in msg
        # Explicitly NOT parameter feedback — the model must not retry.
        assert msg.endswith(FINAL_REFUSAL_MARKER)

    def test_state_bucket_suffix_also_rejected(self):
        with pytest.raises(AdoptRecipeError):
            render_adoption(
                "google_storage_bucket",
                "acme-prod-tofu-state",
                "driftscribe-hack-2026",
                location="asia-northeast1",
            )

    def test_service_managed_bucket_is_rejected(self):
        # A Google-service-managed bucket (Cloud Build staging) is refused at
        # the tool boundary with an HONEST reason — auto-CREATED by a Google
        # service, NOT framed as DriftScribe's own control plane.
        with pytest.raises(AdoptRecipeError) as ei:
            render_adoption(
                "google_storage_bucket",
                "driftscribe-hack-2026_cloudbuild",
                "driftscribe-hack-2026",
                location="us",
            )
        msg = str(ei.value)
        assert "cannot be adopted" in msg
        assert "denylist" in msg
        assert "Google service" in msg
        assert "control plane" not in msg.lower()  # honest framing, not borrowed
        assert msg.endswith(FINAL_REFUSAL_MARKER)

    def test_service_managed_prefix_bucket_is_rejected(self):
        with pytest.raises(AdoptRecipeError):
            render_adoption(
                "google_storage_bucket",
                "gcf-v2-sources-12345-asia-northeast1",
                "driftscribe-hack-2026",
                location="asia-northeast1",
            )

    def test_control_plane_service_is_rejected(self):
        with pytest.raises(AdoptRecipeError) as ei:
            render_adoption(
                "google_cloud_run_v2_service",
                "driftscribe-agent",
                "driftscribe-hack-2026",
                location="asia-northeast1",
                image="gcr.io/x/y:z",
            )
        msg = str(ei.value)
        assert "cannot be adopted" in msg
        assert msg.endswith(FINAL_REFUSAL_MARKER)

    def test_topic_named_like_a_service_still_renders(self):
        # Type-scoped, exactly like the denylist: no control-plane Pub/Sub
        # identity rule exists, so this import is admitted — and the recipe
        # must keep rendering it.
        r = render_adoption(
            "google_pubsub_topic", "driftscribe-agent", "driftscribe-hack-2026"
        )
        assert "import" in r.content

    def test_ordinary_bucket_still_renders(self):
        r = render_adoption(
            "google_storage_bucket",
            "acme-assets",
            "driftscribe-hack-2026",
            location="asia-northeast1",
        )
        assert "acme-assets" in r.content
