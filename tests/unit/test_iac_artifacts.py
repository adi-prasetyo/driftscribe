"""Unit tests for ``agent.iac_artifacts`` (Phase C5e-1).

Coordinator-side read + advisory verify of the C2 plan-builder artifact. Four
surfaces under test:

- :func:`parse_c2_pr_comment` — a GOLDEN round-trip against the real
  ``tools.iac_plan_diff_summary.format_summary`` producer, plus fail-closed on
  missing marker / missing field / duplicate field, and tofu-show extraction.
- :func:`validate_artifact_uri` — fail-closed bucket / path / basename checks.
- :func:`fetch_gcs_object` — generation pinning plumbed through an injected client
  double; non-numeric generation and NotFound → :class:`IacArtifactError`.
- :func:`load_plan_view` — happy path (integrity_ok, no denylist, 15 fields),
  sha-mismatch, denylist trip, malformed metadata, and a fetch error all map to the
  right view without crashing.
- :func:`find_latest_c2_comment` — newest-matching wins, none-match → None,
  GithubException → IacArtifactError.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from agent.iac_artifacts import (
    C2CommentRef,
    IacArtifactError,
    fetch_gcs_object,
    find_latest_c2_comment,
    load_plan_view,
    parse_c2_pr_comment,
    validate_artifact_uri,
)
from driftscribe_lib.iac_plan_metadata import MetadataInput, build_metadata
from tools.iac_plan_diff_summary import SummaryInput, format_summary

BUCKET = "driftscribe-hack-2026-tofu-artifacts"
SHA = "a" * 40
PREFIX = f"gs://{BUCKET}/pr-12/{SHA}/run-100-1/"
URI_PLAN = PREFIX + "plan.tfplan"
URI_JSON = PREFIX + "plan.json"
URI_META = PREFIX + "metadata.json"
GEN_PLAN = "1700000000000001"
GEN_JSON = "1700000000000002"
GEN_META = "1700000000000003"
PLAN_SHA = "b" * 64
PLAN_JSON_SHA = "c" * 64
OPENTOFU = "1.12.0"


def _summary_input(plan_text: str = "diff line one\ndiff line two\n") -> SummaryInput:
    return SummaryInput(
        plan_text=plan_text,
        head_sha=SHA,
        plan_sha256=PLAN_SHA,
        plan_json_sha256=PLAN_JSON_SHA,
        generation_plan=GEN_PLAN,
        generation_json=GEN_JSON,
        generation_metadata=GEN_META,
        artifact_uri_plan=URI_PLAN,
        artifact_uri_json=URI_JSON,
        artifact_uri_metadata=URI_META,
        opentofu_version=OPENTOFU,
    )


# --------------------------------------------------------------------------- #
# Fakes for GCS + GitHub
# --------------------------------------------------------------------------- #


class _FakeBlob:
    def __init__(self, store: dict[tuple[str, int], bytes], name: str, generation: int):
        self._store = store
        self._name = name
        self._gen = generation

    def download_as_bytes(self, *, raw_download: bool, if_generation_match: int) -> bytes:
        assert raw_download is True
        # The handler must pin BOTH the blob generation and if_generation_match
        # to the SAME number — that is the pinning contract under test.
        assert if_generation_match == self._gen, "if_generation_match must match blob gen"
        key = (self._name, self._gen)
        if key not in self._store:
            # Mimic the SDK's NotFound surface so fetch_gcs_object converts it.
            from google.api_core.exceptions import NotFound

            raise NotFound(f"no object {key}")
        return self._store[key]


class _FakeBucket:
    def __init__(self, name: str, store: dict[tuple[str, int], bytes]):
        self.name = name
        self._store = store

    def blob(self, name: str, generation: int) -> _FakeBlob:
        return _FakeBlob(self._store, name, generation)


class _FakeStorageClient:
    """Records (name, generation) → bytes keyed objects; serves them via the same
    ``.bucket().blob(name, generation=).download_as_bytes(...)`` shape as the real
    SDK so the production code path is exercised unchanged."""

    def __init__(self, bucket_name: str = BUCKET):
        self._bucket_name = bucket_name
        self.store: dict[tuple[str, int], bytes] = {}
        self.requested_buckets: list[str] = []

    def put(self, object_name: str, generation: str | int, data: bytes) -> None:
        self.store[(object_name, int(generation))] = data

    def bucket(self, name: str) -> _FakeBucket:
        self.requested_buckets.append(name)
        return _FakeBucket(name, self.store)


class _FakeComment:
    def __init__(self, body: str, comment_id: int, created_at: datetime):
        self.body = body
        self.id = comment_id
        self.created_at = created_at


class _FakeIssue:
    def __init__(self, comments: list[_FakeComment]):
        self._comments = comments

    def get_comments(self) -> list[_FakeComment]:
        return self._comments


class _FakeRepo:
    def __init__(self, comments: list[_FakeComment]):
        self._issue = _FakeIssue(comments)

    def get_issue(self, pr_number: int) -> _FakeIssue:
        return self._issue


# --------------------------------------------------------------------------- #
# parse_c2_pr_comment — golden round-trip + fail-closed
# --------------------------------------------------------------------------- #


def test_parse_golden_round_trip() -> None:
    """Build a real comment with the producer, parse it back, every field matches."""
    body = format_summary(_summary_input(plan_text="~ google_x.foo\n  + bar = 1\n"))
    ref = parse_c2_pr_comment(body, comment_id=99)
    assert ref is not None
    assert ref.head_sha == SHA
    assert ref.plan_sha256 == PLAN_SHA
    assert ref.plan_json_sha256 == PLAN_JSON_SHA
    assert ref.generation_plan == GEN_PLAN
    assert ref.generation_json == GEN_JSON
    assert ref.generation_metadata == GEN_META
    assert ref.artifact_uri_plan == URI_PLAN
    assert ref.artifact_uri_json == URI_JSON
    assert ref.artifact_uri_metadata == URI_META
    assert ref.opentofu_version == OPENTOFU
    assert ref.comment_id == 99
    assert "~ google_x.foo" in ref.tofu_show_text
    assert "+ bar = 1" in ref.tofu_show_text


def test_parse_extracts_tofu_show_with_wide_fence() -> None:
    """A diff containing a 3-backtick run forces ``_pick_fence`` to widen the
    fence to >=4; the parser's ``(`{3,})`` open + backreference close must still
    capture the body."""
    plan = "before ``` after\nsecond line\n"
    body = format_summary(_summary_input(plan_text=plan))
    ref = parse_c2_pr_comment(body)
    assert ref is not None
    assert "before ``` after" in ref.tofu_show_text
    assert "second line" in ref.tofu_show_text


def test_parse_missing_marker_returns_none() -> None:
    assert parse_c2_pr_comment("just a normal PR comment, no marker") is None
    assert parse_c2_pr_comment("") is None
    assert parse_c2_pr_comment(None) is None  # type: ignore[arg-type]


def test_parse_missing_required_field_returns_none() -> None:
    body = format_summary(_summary_input())
    # Drop the head_sha bullet entirely.
    mangled = "\n".join(
        line for line in body.splitlines() if not line.startswith("- **head_sha:**")
    )
    assert parse_c2_pr_comment(mangled) is None


def test_parse_malformed_field_returns_none() -> None:
    body = format_summary(_summary_input())
    # Corrupt the head_sha to a non-40-hex value.
    mangled = body.replace(SHA, "not-a-sha", 1)
    assert parse_c2_pr_comment(mangled) is None


def test_parse_bad_generation_returns_none() -> None:
    body = format_summary(_summary_input())
    mangled = body.replace(GEN_META, "nope")
    assert parse_c2_pr_comment(mangled) is None


def test_parse_non_gs_uri_returns_none() -> None:
    body = format_summary(_summary_input())
    mangled = body.replace(URI_META, "https://evil.example.com/metadata.json")
    assert parse_c2_pr_comment(mangled) is None


def test_parse_duplicate_field_returns_none() -> None:
    """A duplicated required bullet is malformed — we cannot know which copy is
    authoritative, so fail-closed."""
    body = format_summary(_summary_input())
    dup_line = f"- **head_sha:** `{SHA}`"
    # Inject a second head_sha bullet.
    mangled = body.replace(dup_line, dup_line + "\n" + dup_line, 1)
    assert parse_c2_pr_comment(mangled) is None


def test_parse_missing_tofu_show_is_not_fatal() -> None:
    """The diff block is best-effort — a comment with valid header but no
    <details> block still parses, with an empty tofu_show_text."""
    body = format_summary(_summary_input())
    header_only = body.split("<details>", 1)[0]
    ref = parse_c2_pr_comment(header_only)
    assert ref is not None
    assert ref.tofu_show_text == ""


# --------------------------------------------------------------------------- #
# validate_artifact_uri
# --------------------------------------------------------------------------- #


def test_validate_uri_good() -> None:
    bucket, obj = validate_artifact_uri(
        URI_META, bucket_name=BUCKET, expected_basename="metadata.json"
    )
    assert bucket == BUCKET
    assert obj == f"pr-12/{SHA}/run-100-1/metadata.json"


def test_validate_uri_wrong_bucket() -> None:
    bad = f"gs://other-bucket/pr-12/{SHA}/run-100-1/metadata.json"
    with pytest.raises(IacArtifactError):
        validate_artifact_uri(bad, bucket_name=BUCKET, expected_basename="metadata.json")


def test_validate_uri_bad_path() -> None:
    bad = f"gs://{BUCKET}/random/path/metadata.json"
    with pytest.raises(IacArtifactError):
        validate_artifact_uri(bad, bucket_name=BUCKET, expected_basename="metadata.json")


def test_validate_uri_wrong_basename() -> None:
    with pytest.raises(IacArtifactError):
        # URI_META is a metadata.json path but we ask for plan.json.
        validate_artifact_uri(URI_META, bucket_name=BUCKET, expected_basename="plan.json")


def test_validate_uri_not_gs_scheme() -> None:
    with pytest.raises(IacArtifactError):
        validate_artifact_uri(
            "https://x/y", bucket_name=BUCKET, expected_basename="plan.json"
        )


# --------------------------------------------------------------------------- #
# fetch_gcs_object
# --------------------------------------------------------------------------- #


def test_fetch_pins_generation_and_returns_bytes() -> None:
    client = _FakeStorageClient()
    obj = f"pr-12/{SHA}/run-100-1/plan.json"
    client.put(obj, GEN_JSON, b"the bytes")
    out = fetch_gcs_object(BUCKET, obj, GEN_JSON, client=client)
    assert out == b"the bytes"
    assert client.requested_buckets == [BUCKET]


def test_fetch_generation_pinning_is_string_or_int() -> None:
    """``generation`` may arrive as a numeric string (from the comment) or int —
    both pin to the same blob revision."""
    client = _FakeStorageClient()
    obj = f"pr-12/{SHA}/run-100-1/plan.json"
    client.put(obj, GEN_JSON, b"x")
    assert fetch_gcs_object(BUCKET, obj, GEN_JSON, client=client) == b"x"
    assert fetch_gcs_object(BUCKET, obj, int(GEN_JSON), client=client) == b"x"


def test_fetch_non_numeric_generation_raises() -> None:
    client = _FakeStorageClient()
    with pytest.raises(IacArtifactError):
        fetch_gcs_object(BUCKET, "obj", "not-a-number", client=client)


def test_fetch_not_found_raises_artifact_error() -> None:
    client = _FakeStorageClient()  # empty store → blob raises NotFound
    with pytest.raises(IacArtifactError):
        fetch_gcs_object(BUCKET, "missing", GEN_JSON, client=client)


# --------------------------------------------------------------------------- #
# load_plan_view
# --------------------------------------------------------------------------- #


def _benign_plan_json() -> str:
    """A plan.json with one no-op Cloud Run change — passes the C1 denylist."""
    return json.dumps(
        {
            "resource_changes": [
                {
                    "address": "google_cloud_run_v2_service.payment_demo",
                    "type": "google_cloud_run_v2_service",
                    "change": {
                        "actions": ["no-op"],
                        "before": {"name": "payment-demo"},
                        "after": {"name": "payment-demo"},
                    },
                }
            ]
        }
    )


def _iam_violating_plan_json() -> str:
    """A plan.json with an IAM member change — trips iam-change-forbidden-v1."""
    return json.dumps(
        {
            "resource_changes": [
                {
                    "address": "google_project_iam_member.evil",
                    "type": "google_project_iam_member",
                    "change": {
                        "actions": ["create"],
                        "before": None,
                        "after": {"role": "roles/owner"},
                    },
                }
            ]
        }
    )


def _seed_view_client(plan_json_text: str, *, plan_json_sha: str | None = None):
    """Build a fake client holding metadata.json (pinned to GEN_META) + plan.json
    (pinned to GEN_JSON). The metadata's ``plan_json_sha256`` defaults to the REAL
    digest of ``plan_json_text`` so the happy path verifies; pass ``plan_json_sha``
    to force a mismatch."""
    plan_bytes = plan_json_text.encode("utf-8")
    real_sha = hashlib.sha256(plan_bytes).hexdigest()
    md = build_metadata(
        MetadataInput(
            repo="adi-p/driftscribe",
            pr_number=12,
            head_sha=SHA,
            base_sha="d" * 40,
            workflow_run_id="100",
            workflow_run_attempt="1",
            artifact_uri_plan=URI_PLAN,
            artifact_uri_json=URI_JSON,
            generation_plan=GEN_PLAN,
            generation_json=GEN_JSON,
            plan_sha256=PLAN_SHA,
            plan_json_sha256=(plan_json_sha or real_sha),
            opentofu_version=OPENTOFU,
            provider_lockfile_sha256="e" * 64,
        )
    )
    client = _FakeStorageClient()
    obj_meta = f"pr-12/{SHA}/run-100-1/metadata.json"
    obj_json = f"pr-12/{SHA}/run-100-1/plan.json"
    client.put(obj_meta, GEN_META, json.dumps(md).encode("utf-8"))
    client.put(obj_json, GEN_JSON, plan_bytes)
    return client, md


def _ref() -> C2CommentRef:
    return C2CommentRef(
        head_sha=SHA,
        plan_sha256=PLAN_SHA,
        plan_json_sha256=PLAN_JSON_SHA,
        generation_plan=GEN_PLAN,
        generation_json=GEN_JSON,
        generation_metadata=GEN_META,
        artifact_uri_plan=URI_PLAN,
        artifact_uri_json=URI_JSON,
        artifact_uri_metadata=URI_META,
        opentofu_version=OPENTOFU,
        comment_id=7,
        tofu_show_text="some diff",
    )


def test_load_plan_view_happy_path() -> None:
    client, md = _seed_view_client(_benign_plan_json())
    view = load_plan_view(_ref(), bucket_name=BUCKET, client=client)
    assert view.unverifiable is False
    assert view.integrity_ok is True
    assert view.denylist_violations == []
    # 15 c2.v1 fields populated.
    assert len(view.metadata) == 15
    assert view.metadata["schema_version"] == "c2.v1"
    # Convenience accessors.
    assert view.head_sha == SHA
    assert view.artifact_uri_metadata == URI_META
    assert view.generation_metadata == GEN_META
    assert view.plan_json_sha256 == md["plan_json_sha256"]
    assert view.tofu_show_text == "some diff"


def test_load_plan_view_sha_mismatch_sets_integrity_false() -> None:
    client, _md = _seed_view_client(_benign_plan_json(), plan_json_sha="f" * 64)
    view = load_plan_view(_ref(), bucket_name=BUCKET, client=client)
    # Metadata is still valid c2.v1 (the wrong sha is a syntactically valid hex64),
    # so the view is verifiable but integrity fails.
    assert view.unverifiable is False
    assert view.integrity_ok is False


def test_load_plan_view_denylist_violation() -> None:
    client, _md = _seed_view_client(_iam_violating_plan_json())
    view = load_plan_view(_ref(), bucket_name=BUCKET, client=client)
    assert view.unverifiable is False
    assert view.integrity_ok is True  # bytes match; the policy is what fails
    assert view.denylist_violations
    rules = {r for r, _detail in view.denylist_violations}
    assert "iam-change-forbidden-v1" in rules


def test_load_plan_view_malformed_metadata_sets_unverifiable() -> None:
    """Metadata that is not c2.v1 (bad field) → unverifiable, no crash, Approve
    suppressed."""
    client, _md = _seed_view_client(_benign_plan_json())
    # Overwrite the metadata object with a malformed dict (bad head_sha).
    bad = {"schema_version": "c2.v1", "head_sha": "not-hex"}
    obj_meta = f"pr-12/{SHA}/run-100-1/metadata.json"
    client.put(obj_meta, GEN_META, json.dumps(bad).encode("utf-8"))
    view = load_plan_view(_ref(), bucket_name=BUCKET, client=client)
    assert view.unverifiable is True


def test_load_plan_view_wrong_schema_version_sets_unverifiable() -> None:
    client, md = _seed_view_client(_benign_plan_json())
    md2 = dict(md)
    md2["schema_version"] = "c1.v0"
    obj_meta = f"pr-12/{SHA}/run-100-1/metadata.json"
    client.put(obj_meta, GEN_META, json.dumps(md2).encode("utf-8"))
    view = load_plan_view(_ref(), bucket_name=BUCKET, client=client)
    assert view.unverifiable is True


def test_load_plan_view_fetch_error_sets_unverifiable() -> None:
    """An empty store → metadata fetch raises NotFound → IacArtifactError → the
    view is unverifiable (the GET must still render always-200)."""
    client = _FakeStorageClient()  # nothing seeded
    view = load_plan_view(_ref(), bucket_name=BUCKET, client=client)
    assert view.unverifiable is True
    assert view.integrity_ok is False


def test_load_plan_view_plan_json_fetch_error_sets_unverifiable() -> None:
    """Metadata present + valid, but plan.json missing → unverifiable."""
    client, _md = _seed_view_client(_benign_plan_json())
    # Remove the plan.json object so only metadata resolves.
    obj_json = f"pr-12/{SHA}/run-100-1/plan.json"
    del client.store[(obj_json, int(GEN_JSON))]
    view = load_plan_view(_ref(), bucket_name=BUCKET, client=client)
    assert view.unverifiable is True


# --------------------------------------------------------------------------- #
# find_latest_c2_comment
# --------------------------------------------------------------------------- #


def _at(seconds: int) -> datetime:
    return datetime(2026, 5, 30, 12, 0, seconds, tzinfo=timezone.utc)


def test_find_latest_newest_matching_wins() -> None:
    body = format_summary(_summary_input())
    # Two matching comments + one non-matching, out of created_at order.
    older = format_summary(_summary_input()).replace(SHA, "b" * 40)
    comments = [
        _FakeComment(older, comment_id=1, created_at=_at(1)),
        _FakeComment("not a marker comment", comment_id=2, created_at=_at(5)),
        _FakeComment(body, comment_id=3, created_at=_at(3)),
    ]
    ref = find_latest_c2_comment(_FakeRepo(comments), 12)
    assert ref is not None
    # The newest *matching* (created_at=3, id=3) wins over the older match (id=1)
    # and ignores the newer non-matching comment (id=2).
    assert ref.comment_id == 3
    assert ref.head_sha == SHA


def test_find_latest_none_match_returns_none() -> None:
    comments = [
        _FakeComment("nothing here", comment_id=1, created_at=_at(1)),
        _FakeComment("still nothing", comment_id=2, created_at=_at(2)),
    ]
    assert find_latest_c2_comment(_FakeRepo(comments), 12) is None


def test_find_latest_empty_returns_none() -> None:
    assert find_latest_c2_comment(_FakeRepo([]), 12) is None


def test_find_latest_github_exception_raises_artifact_error(monkeypatch) -> None:
    """A PyGithub GithubException during comment listing surfaces as
    IacArtifactError so the route maps it cleanly."""
    from github import GithubException

    class _BoomRepo:
        def get_issue(self, pr_number: int):
            raise GithubException(500, "boom", None)

    with pytest.raises(IacArtifactError):
        find_latest_c2_comment(_BoomRepo(), 12)


def test_find_latest_non_github_exception_propagates() -> None:
    """A non-GithubException (genuine programming error) is NOT swallowed."""

    class _TypeErrorRepo:
        def get_issue(self, pr_number: int):
            raise RuntimeError("not a github error")

    with pytest.raises(RuntimeError):
        find_latest_c2_comment(_TypeErrorRepo(), 12)
