"""Unit tests for the pure pending-approval parsing helpers.

No I/O: given a PR's number/title/url/body, derive the adopted resource. The
import-id shapes + rtype→asset-type map are reused from ``adopt_recipe`` so an id
these accept is exactly one the adoption pipeline could have produced.
"""
import pytest

from driftscribe_lib.pending_approvals import (
    build_pending_approval,
    extract_import_id,
    import_id_to_resource,
)


def test_extract_import_id_from_adoption_body():
    body = "Adopts a topic.\n\n**Import id:** `projects/p/topics/adopt-probe-topic`\n\nmore"
    assert extract_import_id(body) == "projects/p/topics/adopt-probe-topic"


def test_extract_import_id_missing_returns_none():
    assert extract_import_id("a freehand PR body with no import line") is None
    assert extract_import_id("") is None
    assert extract_import_id(None) is None


@pytest.mark.parametrize(
    "import_id, asset_type, name",
    [
        ("projects/p/topics/t1", "pubsub.googleapis.com/Topic", "t1"),
        ("projects/p/subscriptions/s1", "pubsub.googleapis.com/Subscription", "s1"),
        ("projects/p/locations/asia/services/svc", "run.googleapis.com/Service", "svc"),
        ("my-bucket", "storage.googleapis.com/Bucket", "my-bucket"),
    ],
)
def test_import_id_to_resource(import_id, asset_type, name):
    assert import_id_to_resource(import_id) == (asset_type, name)


def test_import_id_to_resource_unrecognized_returns_none():
    assert import_id_to_resource("projects/p/widgets/w") is None
    assert import_id_to_resource("") is None


def test_build_pending_approval_adoption():
    body = "x\n\n**Import id:** `projects/p/topics/adopt-probe-topic`\n"
    out = build_pending_approval(168, "Adopt topic", "https://gh/pr/168", body)
    assert out == {
        "pr_number": 168,
        "title": "Adopt topic",
        "url": "https://gh/pr/168",
        "asset_type": "pubsub.googleapis.com/Topic",
        "resource_name": "adopt-probe-topic",
    }


def test_build_pending_approval_freehand_has_blank_resource():
    out = build_pending_approval(170, "Add monitoring", "https://gh/pr/170", "no import")
    assert out["pr_number"] == 170
    assert out["asset_type"] == ""
    assert out["resource_name"] == ""
