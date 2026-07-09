"""Tests for the Infra-Reader Agent worker (Phase B).

Covers the contract laid out in the plan:

- ``/healthz`` is unauthenticated → ``{"ok": True}``.
- ``/describe`` happy path: a fake CAI iterator returns the payment-demo Cloud
  Run service (declared in the baked-in ``iac/`` dir) + one unmanaged service →
  ``declared_in_iac >= 1`` and the payment-demo sample is labeled ``iac=True``.
- read_mask: the request handed to ``search_all_resources`` carries exactly the
  ``["name", "asset_type", "location"]`` paths (verified against
  google-cloud-asset==4.3.0: ``read_mask={"paths": [...]}`` dict-coercion yields
  a ``FieldMask`` whose ``.paths`` is that list).
- pagination: the client iterator yields results across two "pages"; the counts
  aggregate over the whole iterator.
- ``extra="forbid"``: POST with an unexpected field → 422.
- auth: with NO dependency override + missing/invalid token → 401/403 (mirrors
  ``workers/reader/tests/test_read.py``).
- degradation (CAI): ``search_all_resources`` raising ``PermissionDenied`` or a
  generic ``GoogleAPICallError`` subclass → HTTP 200 soft-fail body, NOT 5xx.
- degradation (declared parse): a malformed ``*.tf`` in ``IAC_DIR`` → the live
  inventory is still returned, carrying ``declared_set_status="parse_error"``.
- ``iac_snapshot_sha`` is echoed from the ``IAC_SNAPSHOT_SHA`` env.

We bypass auth in happy-path tests via ``app.dependency_overrides`` (same idiom
as the Reader's tests) and monkeypatch ``asset_v1.AssetServiceClient`` so no real
Google credentials / network are touched.
"""
import os
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from google.api_core import exceptions as gax

# Env MUST be set before importing workers.infra_reader.main — the module reads
# GCP_PROJECT / OWN_URL / ALLOWED_CALLERS at import time and KeyErrors if
# missing (mirrors the Reader's fail-fast boot). IAC_DIR points at the baked-in
# repo iac/ dir so the happy-path declared set resolves the payment-demo import.
_REPO_ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("GCP_PROJECT", "driftscribe-hack-2026")
os.environ.setdefault("OWN_URL", "https://infra-reader.example.com")
os.environ.setdefault(
    "ALLOWED_CALLERS",
    "coordinator@driftscribe-hack-2026.iam.gserviceaccount.com",
)
os.environ.setdefault("IAC_DIR", str(_REPO_ROOT / "iac"))
os.environ.setdefault("IAC_SNAPSHOT_SHA", "test-sha-abc123")

from workers.infra_reader import main as infra_main  # noqa: E402
from workers.infra_reader.main import _verify_caller_dep, app  # noqa: E402

_RUN_SERVICE = "run.googleapis.com/Service"
_SUB_TYPE = "pubsub.googleapis.com/Subscription"
_PAYMENT_DEMO_NAME = (
    "//run.googleapis.com/projects/driftscribe-hack-2026/"
    "locations/asia-northeast1/services/payment-demo"
)
_SUB_NAME = (
    "//pubsub.googleapis.com/projects/driftscribe-hack-2026/"
    "subscriptions/adopt-probe-sub"
)
_ALLOWED = "coordinator@driftscribe-hack-2026.iam.gserviceaccount.com"


class _FakeResource:
    """Minimal stand-in for a CAI ResourceSearchResult — only the masked fields.

    ``versioned_resources`` defaults to () so a primary-search double never
    carries enrichment payload; the subscription-enrichment doubles pass a list
    of objects whose ``.resource`` is a plain dict (a stand-in for the proto-plus
    ``MapComposite`` the real client returns — both support ``.get``).
    """

    def __init__(self, name, asset_type, location, versioned_resources=()):
        self.name = name
        self.asset_type = asset_type
        self.location = location
        self.versioned_resources = versioned_resources


class _FakeVersioned:
    """Stand-in for a CAI VersionedResource — only ``.resource`` is read."""

    def __init__(self, resource):
        self.resource = resource


class _FakeAsset:
    """Minimal stand-in for a CAI ListAssets ``Asset`` (RESOURCE content).

    Only ``.name``, ``.asset_type`` and ``.resource.location`` are read by
    ``_list_buckets``; ``.resource`` is a tiny object exposing ``.location`` (the
    real Asset's ``resource`` is a proto ``Resource`` with a ``location`` field).
    """

    def __init__(self, name, asset_type="storage.googleapis.com/Bucket", location=""):
        self.name = name
        self.asset_type = asset_type
        self.resource = type("_R", (), {"location": location})()


def _make_fake_client(
    results, *, raises=None, capture=None,
    buckets=None, list_raises=None, list_capture=None,
):
    """Build a fake AssetServiceClient class returning ``results`` from search.

    ``capture`` (a dict) records the ``request`` kwarg so tests can assert the
    read_mask. ``raises`` (an exception instance) makes search_all_resources
    raise instead of yielding. ``buckets`` seeds the ListAssets bucket-supplement
    (default empty); ``list_raises`` makes ``list_assets`` raise; ``list_capture``
    records its request.
    """

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def search_all_resources(self, request=None, **kwargs):
            if capture is not None:
                capture["request"] = request
                capture["kwargs"] = kwargs
            if raises is not None:
                raise raises
            # Return an iterable (mimics the client's paging iterator, which
            # transparently yields across pages).
            return iter(results)

        def list_assets(self, request=None, **kwargs):
            if list_capture is not None:
                list_capture["request"] = request
            if list_raises is not None:
                raise list_raises
            return iter(buckets or [])

    return _FakeClient


@pytest.fixture(autouse=True)
def _pin_module_constants(monkeypatch):
    """Force the module-level env-derived constants to the known test values.

    main.py reads GCP_PROJECT / IAC_DIR / IAC_SNAPSHOT_SHA at import time. In a
    unified pytest run another worker's test module may have populated those env
    vars (e.g. ``GCP_PROJECT=test-proj`` from the Reader's tests) *before* this
    module was imported, so the ``os.environ.setdefault`` calls at the top of
    this file become no-ops and the constants carry the other worker's values.
    Pinning the constants here (the same rationale as the Reader's
    ``test_real_verify_caller_dep_wired_with_env``) keeps these tests honest
    regardless of pytest's worker-test collection order.
    """
    monkeypatch.setattr(infra_main, "GCP_PROJECT", "driftscribe-hack-2026")
    monkeypatch.setattr(infra_main, "IAC_DIR", _REPO_ROOT / "iac")
    monkeypatch.setattr(infra_main, "IAC_SNAPSHOT_SHA", "test-sha-abc123")


@pytest.fixture
def client():
    """TestClient with auth bypassed; per-test CAI patching done in the test."""
    app.dependency_overrides[_verify_caller_dep] = lambda: _ALLOWED
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_healthz_does_not_require_auth(client):
    def boom():
        raise HTTPException(status_code=401, detail="should not be called")

    app.dependency_overrides[_verify_caller_dep] = boom
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_describe_happy_path_labels_payment_demo_as_iac(client, monkeypatch):
    results = [
        _FakeResource(_PAYMENT_DEMO_NAME, _RUN_SERVICE, "asia-northeast1"),
        _FakeResource(
            "//run.googleapis.com/projects/driftscribe-hack-2026/"
            "locations/asia-northeast1/services/unmanaged-svc",
            _RUN_SERVICE,
            "asia-northeast1",
        ),
    ]
    monkeypatch.setattr(
        infra_main.asset_v1, "AssetServiceClient", _make_fake_client(results)
    )
    r = client.post("/describe", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project"] == "driftscribe-hack-2026"
    assert body["total_resources"] == 2
    assert body["declared_in_iac"] >= 1
    samples = body["by_type"][_RUN_SERVICE]["sample"]
    by_name = {s["name"]: s for s in samples}
    assert by_name["payment-demo"]["iac"] is True
    assert by_name["unmanaged-svc"]["iac"] is False


def test_describe_read_mask_is_exactly_name_asset_type_location(client, monkeypatch):
    capture: dict = {}
    monkeypatch.setattr(
        infra_main.asset_v1,
        "AssetServiceClient",
        _make_fake_client([], capture=capture),
    )
    r = client.post("/describe", json={})
    assert r.status_code == 200, r.text
    req = capture["request"]
    # google-cloud-asset 4.3.0 coerces read_mask={"paths": [...]} into a
    # FieldMask whose .paths preserves order.
    assert list(req.read_mask.paths) == ["name", "asset_type", "location"]
    assert req.scope == "projects/driftscribe-hack-2026"


def test_describe_pagination_aggregates_across_pages(client, monkeypatch):
    # Two "pages" worth of results — the client iterator yields all of them.
    page1 = [
        _FakeResource(
            f"//run.googleapis.com/projects/driftscribe-hack-2026/"
            f"locations/asia-northeast1/services/svc-{i}",
            _RUN_SERVICE,
            "asia-northeast1",
        )
        for i in range(3)
    ]
    page2 = [
        _FakeResource(
            f"//run.googleapis.com/projects/driftscribe-hack-2026/"
            f"locations/asia-northeast1/services/svc-{i}",
            _RUN_SERVICE,
            "asia-northeast1",
        )
        for i in range(3, 5)
    ]
    monkeypatch.setattr(
        infra_main.asset_v1,
        "AssetServiceClient",
        _make_fake_client(page1 + page2),
    )
    r = client.post("/describe", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_resources"] == 5
    assert body["by_type"][_RUN_SERVICE]["count"] == 5


def test_describe_iac_snapshot_sha_echoed(client, monkeypatch):
    monkeypatch.setattr(
        infra_main.asset_v1, "AssetServiceClient", _make_fake_client([])
    )
    r = client.post("/describe", json={})
    assert r.status_code == 200, r.text
    assert r.json()["iac_snapshot_sha"] == "test-sha-abc123"


def test_describe_extra_fields_rejected(client):
    r = client.post("/describe", json={"x": 1})
    assert r.status_code == 422, r.text


def test_missing_or_invalid_token_returns_401_or_403():
    # No dependency override — exercise the real _verify_caller_dep, which
    # delegates to verify_caller. A missing/invalid bearer token must be a real
    # 401/403 (auth failures stay hard errors, unlike CAI degradation).
    c = TestClient(app)
    r = c.post("/describe", json={})
    assert r.status_code in (401, 403), r.text


def test_describe_permission_denied_soft_fails_200(client, monkeypatch):
    monkeypatch.setattr(
        infra_main.asset_v1,
        "AssetServiceClient",
        _make_fake_client([], raises=gax.PermissionDenied("no cloudasset.viewer")),
    )
    r = client.post("/describe", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["error"] == "cloud_asset_unavailable"
    assert body["project"] == "driftscribe-hack-2026"


def test_describe_generic_api_error_soft_fails_200(client, monkeypatch):
    # A generic GoogleAPICallError subclass (e.g. ServiceUnavailable) also
    # soft-fails to a 200 so worker_client.call sees a 2xx and chat can narrate
    # the partial degradation.
    monkeypatch.setattr(
        infra_main.asset_v1,
        "AssetServiceClient",
        _make_fake_client([], raises=gax.ServiceUnavailable("backend down")),
    )
    r = client.post("/describe", json={})
    assert r.status_code == 200, r.text
    assert r.json()["error"] == "cloud_asset_unavailable"


# --------------------------------------------------------------------------- #
# Subscription→topic enrichment (adopt-sub-topic-prefill). The enrichment is a
# SECOND scoped search dispatched only when subscriptions are present; the
# dispatch fake below discriminates the two calls by whether the request carries
# ``asset_types`` (only the enrichment search sets it).
# --------------------------------------------------------------------------- #


def _make_dispatch_client(primary, enrichment, *, capture=None, enrich_raises=None):
    """Fake client whose ``search_all_resources`` returns ``primary`` for the
    minimal-masked inventory search and ``enrichment`` for the subscription-only
    enrichment search (the one that sets ``asset_types``).
    """

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def search_all_resources(self, request=None, **kwargs):
            is_enrichment = bool(getattr(request, "asset_types", None))
            if capture is not None:
                capture.setdefault("requests", []).append(request)
                capture["enrichment" if is_enrichment else "primary"] = request
            if is_enrichment:
                if enrich_raises is not None:
                    raise enrich_raises
                return iter(enrichment)
            return iter(primary)

        def list_assets(self, request=None, **kwargs):
            return iter(())  # bucket supplement unused by these enrichment tests

    return _FakeClient


def test_first_topic_pure_extractor_good_malformed_empty():
    # good row → its topic; malformed / topic-less / non-dict rows skipped; the
    # first non-empty string topic wins.
    assert infra_main._first_topic([{"topic": "projects/p/topics/t"}]) == "projects/p/topics/t"
    assert (
        infra_main._first_topic(
            [{"pushConfig": {}}, "not-a-mapping", {"topic": ""}, {"topic": "good"}]
        )
        == "good"
    )
    assert infra_main._first_topic([]) is None
    assert infra_main._first_topic([{"topic": 123}]) is None  # non-string topic ignored


def test_describe_subscription_sample_gains_topic(client, monkeypatch):
    primary = [_FakeResource(_SUB_NAME, _SUB_TYPE, "global")]
    enrichment = [
        _FakeResource(
            _SUB_NAME, _SUB_TYPE, "global",
            versioned_resources=[
                _FakeVersioned(
                    {"topic": "projects/driftscribe-hack-2026/topics/adopt-probe-topic",
                     "pushConfig": {"pushEndpoint": "https://secret.example/hook"}}
                )
            ],
        )
    ]
    monkeypatch.setattr(
        infra_main.asset_v1,
        "AssetServiceClient",
        _make_dispatch_client(primary, enrichment),
    )
    r = client.post("/describe", json={})
    assert r.status_code == 200, r.text
    sample = r.json()["by_type"][_SUB_TYPE]["sample"][0]
    # In-project topic shortened to its bare name; nothing else from the
    # versioned resource (the push endpoint) leaks into the sample.
    assert sample["topic"] == "adopt-probe-topic"
    assert "pushConfig" not in sample
    assert "pushEndpoint" not in str(sample)


def test_describe_enrichment_request_pins_asset_types_and_read_mask(client, monkeypatch):
    capture: dict = {}
    primary = [_FakeResource(_SUB_NAME, _SUB_TYPE, "global")]
    monkeypatch.setattr(
        infra_main.asset_v1,
        "AssetServiceClient",
        _make_dispatch_client(primary, [], capture=capture),
    )
    r = client.post("/describe", json={})
    assert r.status_code == 200, r.text
    enrich = capture["enrichment"]
    assert list(enrich.asset_types) == ["pubsub.googleapis.com/Subscription"]
    assert list(enrich.read_mask.paths) == ["name", "versioned_resources"]
    assert enrich.scope == "projects/driftscribe-hack-2026"


def test_describe_skips_enrichment_when_no_enrichable_types(client, monkeypatch):
    # An estate with no enrichable types (no subscriptions AND no run services)
    # must never pay for a second scoped search — only the primary runs. A bucket
    # is neither, so it exercises the skip for BOTH enrichment blocks.
    capture: dict = {}
    bucket = _FakeResource(
        "//storage.googleapis.com/some-bucket",
        "storage.googleapis.com/Bucket",
        "asia-northeast1",
    )
    monkeypatch.setattr(
        infra_main.asset_v1,
        "AssetServiceClient",
        _make_dispatch_client([bucket], [], capture=capture),
    )
    r = client.post("/describe", json={})
    assert r.status_code == 200, r.text
    assert "enrichment" not in capture      # only the primary search ran
    assert len(capture["requests"]) == 1


def test_describe_malformed_enrichment_row_skips_only_that_row(client, monkeypatch):
    # A malformed enrichment row (versioned_resources=None) preceding a valid one
    # must skip ONLY the bad row — the valid subscription still gets its topic.
    # Guards the plan's "individually malformed rows are skipped while the rest
    # proceed" contract (Codex completed-work review).
    sub_bad = _SUB_NAME  # //…/subscriptions/adopt-probe-sub
    sub_good = "//pubsub.googleapis.com/projects/driftscribe-hack-2026/subscriptions/orders-sub"
    primary = [
        _FakeResource(sub_bad, _SUB_TYPE, "global"),
        _FakeResource(sub_good, _SUB_TYPE, "global"),
    ]
    enrichment = [
        # Malformed: versioned_resources is None (not iterable) → row skipped.
        _FakeResource(sub_bad, _SUB_TYPE, "global", versioned_resources=None),
        _FakeResource(
            sub_good, _SUB_TYPE, "global",
            versioned_resources=[_FakeVersioned({"topic": "projects/driftscribe-hack-2026/topics/order-events"})],
        ),
    ]
    monkeypatch.setattr(
        infra_main.asset_v1,
        "AssetServiceClient",
        _make_dispatch_client(primary, enrichment),
    )
    r = client.post("/describe", json={})
    assert r.status_code == 200, r.text
    by_name = {s["name"]: s for s in r.json()["by_type"][_SUB_TYPE]["sample"]}
    assert "topic" not in by_name["adopt-probe-sub"]     # bad row skipped
    assert by_name["orders-sub"]["topic"] == "order-events"  # valid row survived


def test_describe_enrichment_row_that_raises_is_skipped(client, monkeypatch):
    # Directly exercise the per-row `except` path: a versioned wrapper whose
    # `.resource` access raises must skip only its row (Codex note — the None
    # case is smoothed by `or ()` and never reaches the except branch).
    class _RaisingVersioned:
        @property
        def resource(self):
            raise RuntimeError("unreadable versioned resource")

    sub_bad = _SUB_NAME
    sub_good = "//pubsub.googleapis.com/projects/driftscribe-hack-2026/subscriptions/orders-sub"
    primary = [
        _FakeResource(sub_bad, _SUB_TYPE, "global"),
        _FakeResource(sub_good, _SUB_TYPE, "global"),
    ]
    enrichment = [
        _FakeResource(sub_bad, _SUB_TYPE, "global", versioned_resources=[_RaisingVersioned()]),
        _FakeResource(
            sub_good, _SUB_TYPE, "global",
            versioned_resources=[_FakeVersioned({"topic": "projects/driftscribe-hack-2026/topics/order-events"})],
        ),
    ]
    monkeypatch.setattr(
        infra_main.asset_v1,
        "AssetServiceClient",
        _make_dispatch_client(primary, enrichment),
    )
    r = client.post("/describe", json={})
    assert r.status_code == 200, r.text
    by_name = {s["name"]: s for s in r.json()["by_type"][_SUB_TYPE]["sample"]}
    assert "topic" not in by_name["adopt-probe-sub"]         # raising row skipped
    assert by_name["orders-sub"]["topic"] == "order-events"  # valid row survived


def test_describe_enrichment_failure_keeps_full_inventory_without_topic(client, monkeypatch):
    # The enrichment call raising must NOT degrade the primary inventory: the
    # subscription is still counted, just without a topic key (crew falls back
    # to asking).
    primary = [
        _FakeResource(_SUB_NAME, _SUB_TYPE, "global"),
        _FakeResource(_PAYMENT_DEMO_NAME, _RUN_SERVICE, "asia-northeast1"),
    ]
    monkeypatch.setattr(
        infra_main.asset_v1,
        "AssetServiceClient",
        _make_dispatch_client(
            primary, [], enrich_raises=gax.ServiceUnavailable("cai backend down")
        ),
    )
    r = client.post("/describe", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_resources"] == 2
    sample = body["by_type"][_SUB_TYPE]["sample"][0]
    assert "topic" not in sample


def test_describe_malformed_iac_marks_parse_error(client, monkeypatch, tmp_path):
    # Point IAC_DIR at a tmp dir with one malformed *.tf. The live inventory is
    # still returned, but declared_set_status flags the degraded declared set.
    (tmp_path / "broken.tf").write_text(
        'resource "x" "y" { unterminated = ', encoding="utf-8"
    )
    monkeypatch.setattr(infra_main, "IAC_DIR", tmp_path)
    results = [_FakeResource(_PAYMENT_DEMO_NAME, _RUN_SERVICE, "asia-northeast1")]
    monkeypatch.setattr(
        infra_main.asset_v1, "AssetServiceClient", _make_fake_client(results)
    )
    r = client.post("/describe", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_resources"] == 1
    assert body["declared_set_status"] == "parse_error"


# --------------------------------------------------------------------------- #
# Cloud Run service→image enrichment (adopt-run-image-prefill). A THIRD-ish
# scoped search: same shared `_versioned_field_map` plumbing as the subscription
# topic enrichment, dispatched only when run services are present. Because BOTH
# enrichments now set `asset_types`, tests that exercise both need a dispatch
# fake that discriminates by the scoped type, not just "is enrichment".
# --------------------------------------------------------------------------- #

_RUN_NAME = (
    "//run.googleapis.com/projects/driftscribe-hack-2026/"
    "locations/asia-northeast1/services/adopt-probe-svc"
)
_CP_RUN_NAME = (
    "//run.googleapis.com/projects/driftscribe-hack-2026/"
    "locations/asia-northeast1/services/driftscribe-agent"
)


def _run_versioned(image, *, v2=False):
    """A `_FakeVersioned` carrying a run Service versioned resource with `image`.

    `v2=True` uses the flatter v2 shape (template.containers) to exercise the
    extractor's fallback path. Also seeds an env secret + SA email so tests can
    assert those never leak out of the extractor.
    """
    container = {"image": image, "env": [{"name": "API_KEY", "value": "s3cr3t"}]}
    if v2:
        resource = {"template": {"containers": [container]}}
    else:
        resource = {"spec": {"template": {"spec": {"containers": [container]}}}}
    resource["serviceAccountName"] = "runtime@driftscribe-hack-2026.iam.gserviceaccount.com"
    return _FakeVersioned(resource)


def _make_typed_dispatch_client(primary, by_type, *, capture=None, raises_for=None):
    """Fake client dispatching each scoped enrichment search by its asset_type.

    ``by_type`` maps an asset_type → that scoped search's results; ``raises_for``
    maps an asset_type → an exception to raise for that search. The primary
    (no-``asset_types``) search returns ``primary``.
    """
    raises_for = raises_for or {}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def search_all_resources(self, request=None, **kwargs):
            types = list(getattr(request, "asset_types", None) or [])
            if capture is not None:
                capture.setdefault("requests", []).append(request)
            if not types:
                return iter(primary)
            atype = types[0]
            if capture is not None:
                capture[atype] = request
            if atype in raises_for:
                raise raises_for[atype]
            return iter(by_type.get(atype, []))

        def list_assets(self, request=None, **kwargs):
            return iter(())  # bucket supplement unused by these enrichment tests

    return _FakeClient


def test_extract_run_image_v1_shape_happy_path():
    assert (
        infra_main.extract_run_image(
            [{"spec": {"template": {"spec": {"containers": [
                {"image": "gcr.io/cloudrun/hello"}]}}}}]
        )
        == "gcr.io/cloudrun/hello"
    )


def test_extract_run_image_v2_fallback_shape():
    assert (
        infra_main.extract_run_image(
            [{"template": {"containers": [{"image": "gcr.io/p/img:tag"}]}}]
        )
        == "gcr.io/p/img:tag"
    )


def test_extract_run_image_multi_container_takes_first():
    assert (
        infra_main.extract_run_image(
            [{"spec": {"template": {"spec": {"containers": [
                {"image": "first"}, {"image": "second"}]}}}}]
        )
        == "first"
    )


def test_extract_run_image_empty_malformed_and_nonstring_are_none():
    assert infra_main.extract_run_image([]) is None
    # non-mapping row, then a shape missing the containers path → None
    assert infra_main.extract_run_image(["not-a-mapping", {"spec": {}}]) is None
    # containers present but empty → None
    assert (
        infra_main.extract_run_image(
            [{"spec": {"template": {"spec": {"containers": []}}}}]
        )
        is None
    )
    # non-string image is ignored
    assert (
        infra_main.extract_run_image(
            [{"spec": {"template": {"spec": {"containers": [{"image": 123}]}}}}]
        )
        is None
    )


def test_extract_run_image_on_real_proto_versioned_resource_shape():
    # Pins the LIVE proto-plus shape (Codex review): a real google-cloud-asset
    # VersionedResource whose `.resource` is a google.protobuf.Struct
    # (MapComposite / RepeatedComposite), NOT the plain-dict doubles the other
    # extractor tests use. Guards against a future proto-plus marshaling change
    # silently breaking extraction — the run adopt recipe has never been proven
    # live, so this is the tightest static pin available.
    v1 = infra_main.asset_v1.VersionedResource(
        resource={"spec": {"template": {"spec": {"containers": [
            {"image": "gcr.io/cloudrun/hello"}]}}}}
    )
    assert infra_main.extract_run_image([v1.resource]) == "gcr.io/cloudrun/hello"
    v2 = infra_main.asset_v1.VersionedResource(
        resource={"template": {"containers": [{"image": "gcr.io/p/img:tag"}]}}
    )
    assert infra_main.extract_run_image([v2.resource]) == "gcr.io/p/img:tag"


def test_describe_run_sample_gains_image(client, monkeypatch):
    primary = [_FakeResource(_RUN_NAME, _RUN_SERVICE, "asia-northeast1")]
    by_type = {_RUN_SERVICE: [
        _FakeResource(_RUN_NAME, _RUN_SERVICE, "asia-northeast1",
                      versioned_resources=[_run_versioned("gcr.io/cloudrun/hello")])
    ]}
    monkeypatch.setattr(
        infra_main.asset_v1, "AssetServiceClient",
        _make_typed_dispatch_client(primary, by_type),
    )
    r = client.post("/describe", json={})
    assert r.status_code == 200, r.text
    sample = r.json()["by_type"][_RUN_SERVICE]["sample"][0]
    assert sample["name"] == "adopt-probe-svc"
    assert sample["image"] == "gcr.io/cloudrun/hello"
    # Nothing else from the versioned resource leaks (env secret / SA email).
    assert "s3cr3t" not in str(sample)
    assert "serviceAccountName" not in str(sample)


def test_describe_run_sample_gains_image_v2_shape(client, monkeypatch):
    primary = [_FakeResource(_RUN_NAME, _RUN_SERVICE, "asia-northeast1")]
    by_type = {_RUN_SERVICE: [
        _FakeResource(_RUN_NAME, _RUN_SERVICE, "asia-northeast1",
                      versioned_resources=[_run_versioned("gcr.io/p/img:tag", v2=True)])
    ]}
    monkeypatch.setattr(
        infra_main.asset_v1, "AssetServiceClient",
        _make_typed_dispatch_client(primary, by_type),
    )
    r = client.post("/describe", json={})
    assert r.status_code == 200, r.text
    assert r.json()["by_type"][_RUN_SERVICE]["sample"][0]["image"] == "gcr.io/p/img:tag"


def test_describe_run_enrichment_request_pins_asset_types_and_read_mask(client, monkeypatch):
    capture: dict = {}
    primary = [_FakeResource(_RUN_NAME, _RUN_SERVICE, "asia-northeast1")]
    monkeypatch.setattr(
        infra_main.asset_v1, "AssetServiceClient",
        _make_typed_dispatch_client(primary, {}, capture=capture),
    )
    r = client.post("/describe", json={})
    assert r.status_code == 200, r.text
    enrich = capture[_RUN_SERVICE]
    assert list(enrich.asset_types) == ["run.googleapis.com/Service"]
    assert list(enrich.read_mask.paths) == ["name", "versioned_resources"]
    assert enrich.scope == "projects/driftscribe-hack-2026"


def test_describe_control_plane_service_image_suppressed(client, monkeypatch):
    # DriftScribe's own coordinator is control-plane: even when the enrichment
    # reads its image, build_inventory suppresses it at emission so it never
    # reaches the anonymous-visible graph JSON.
    primary = [_FakeResource(_CP_RUN_NAME, _RUN_SERVICE, "asia-northeast1")]
    by_type = {_RUN_SERVICE: [
        _FakeResource(_CP_RUN_NAME, _RUN_SERVICE, "asia-northeast1",
                      versioned_resources=[_run_versioned("gcr.io/driftscribe-hack-2026/coordinator")])
    ]}
    monkeypatch.setattr(
        infra_main.asset_v1, "AssetServiceClient",
        _make_typed_dispatch_client(primary, by_type),
    )
    r = client.post("/describe", json={})
    assert r.status_code == 200, r.text
    sample = r.json()["by_type"][_RUN_SERVICE]["sample"][0]
    assert sample["name"] == "driftscribe-agent"
    assert "image" not in sample


def test_describe_run_failure_does_not_skip_subscription_enrichment(client, monkeypatch):
    # Independence (run fails → sub still enriches). The run image search raises;
    # the subscription topic must still be joined and the full inventory kept.
    primary = [
        _FakeResource(_RUN_NAME, _RUN_SERVICE, "asia-northeast1"),
        _FakeResource(_SUB_NAME, _SUB_TYPE, "global"),
    ]
    by_type = {_SUB_TYPE: [
        _FakeResource(_SUB_NAME, _SUB_TYPE, "global",
                      versioned_resources=[_FakeVersioned(
                          {"topic": "projects/driftscribe-hack-2026/topics/adopt-probe-topic"})])
    ]}
    monkeypatch.setattr(
        infra_main.asset_v1, "AssetServiceClient",
        _make_typed_dispatch_client(
            primary, by_type,
            raises_for={_RUN_SERVICE: gax.ServiceUnavailable("cai backend down")},
        ),
    )
    r = client.post("/describe", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_resources"] == 2
    assert "image" not in body["by_type"][_RUN_SERVICE]["sample"][0]
    assert body["by_type"][_SUB_TYPE]["sample"][0]["topic"] == "adopt-probe-topic"


def test_describe_subscription_failure_does_not_skip_run_enrichment(client, monkeypatch):
    # The mirror (sub fails → run still enriches).
    primary = [
        _FakeResource(_RUN_NAME, _RUN_SERVICE, "asia-northeast1"),
        _FakeResource(_SUB_NAME, _SUB_TYPE, "global"),
    ]
    by_type = {_RUN_SERVICE: [
        _FakeResource(_RUN_NAME, _RUN_SERVICE, "asia-northeast1",
                      versioned_resources=[_run_versioned("gcr.io/cloudrun/hello")])
    ]}
    monkeypatch.setattr(
        infra_main.asset_v1, "AssetServiceClient",
        _make_typed_dispatch_client(
            primary, by_type,
            raises_for={_SUB_TYPE: gax.ServiceUnavailable("cai backend down")},
        ),
    )
    r = client.post("/describe", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["by_type"][_RUN_SERVICE]["sample"][0]["image"] == "gcr.io/cloudrun/hello"
    assert "topic" not in body["by_type"][_SUB_TYPE]["sample"][0]


# --------------------------------------------------------------------------- #
# Bucket-discovery hardening (ListAssets supplement). GCS buckets propagate into
# the SearchAllResources index very slowly (hours), so `_list_buckets` backfills
# them from the fresher ListAssets index. The supplement unions by raw CAI name,
# soft-fails independently, and is bucket-scoped (no other type touched).
# --------------------------------------------------------------------------- #

_BUCKET_TYPE = "storage.googleapis.com/Bucket"
_RECEIPTS_BUCKET = "//storage.googleapis.com/driftscribe-hack-2026-receipts"


def test_describe_listassets_bucket_missing_from_search_surfaces(client, monkeypatch):
    # The exact failure this fix targets: the bucket is ABSENT from the primary
    # SearchAllResources inventory but PRESENT in ListAssets. It must surface in
    # the inventory (counted, sampled, flagged not-in-iac) via the supplement.
    primary = [_FakeResource(_PAYMENT_DEMO_NAME, _RUN_SERVICE, "asia-northeast1")]
    buckets = [_FakeAsset(_RECEIPTS_BUCKET, location="asia-northeast1")]
    monkeypatch.setattr(
        infra_main.asset_v1, "AssetServiceClient",
        _make_fake_client(primary, buckets=buckets),
    )
    r = client.post("/describe", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_resources"] == 2          # service + supplemented bucket
    assert body["by_type"][_BUCKET_TYPE]["count"] == 1
    sample = body["by_type"][_BUCKET_TYPE]["sample"][0]
    assert sample["name"] == "driftscribe-hack-2026-receipts"
    assert sample["iac"] is False                # not declared → adoptable drift


def test_describe_listassets_bucket_already_in_search_not_duplicated(client, monkeypatch):
    # A bucket present in BOTH indexes must be counted once — the union dedups by
    # raw CAI name (byte-identical across the two APIs, verified live).
    primary = [_FakeResource(_RECEIPTS_BUCKET, _BUCKET_TYPE, "asia-northeast1")]
    buckets = [_FakeAsset(_RECEIPTS_BUCKET, location="asia-northeast1")]
    monkeypatch.setattr(
        infra_main.asset_v1, "AssetServiceClient",
        _make_fake_client(primary, buckets=buckets),
    )
    r = client.post("/describe", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_resources"] == 1
    assert body["by_type"][_BUCKET_TYPE]["count"] == 1


def test_describe_listassets_request_pins_parent_asset_types_content_type(client, monkeypatch):
    list_capture: dict = {}
    monkeypatch.setattr(
        infra_main.asset_v1, "AssetServiceClient",
        _make_fake_client([], list_capture=list_capture),
    )
    r = client.post("/describe", json={})
    assert r.status_code == 200, r.text
    req = list_capture["request"]
    assert list(req.asset_types) == ["storage.googleapis.com/Bucket"]
    assert req.parent == "projects/driftscribe-hack-2026"
    assert int(req.content_type) == int(infra_main.asset_v1.ContentType.RESOURCE)


def test_describe_listassets_supplement_soft_fails_keeps_inventory(client, monkeypatch):
    # ListAssets raising must NOT degrade the primary inventory (mirrors the
    # enrichment soft-fail contract): the service from search survives, 200.
    primary = [_FakeResource(_PAYMENT_DEMO_NAME, _RUN_SERVICE, "asia-northeast1")]
    monkeypatch.setattr(
        infra_main.asset_v1, "AssetServiceClient",
        _make_fake_client(primary, list_raises=gax.ServiceUnavailable("cai down")),
    )
    r = client.post("/describe", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_resources"] == 1
    assert body["by_type"][_RUN_SERVICE]["count"] == 1
