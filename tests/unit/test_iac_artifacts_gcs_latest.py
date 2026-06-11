"""Tests for the GCS-listing latest-plan resolver (ClickOps item 12).

The resolver picks the newest metadata.json for a PR by (run_id, attempt,
generation) — GitHub run ids are globally monotonic, attempts monotonic
within a run; generation breaks the (impossible in practice) exact tie.
"""
import hashlib
import json
from types import SimpleNamespace

import pytest

from agent.iac_artifacts import IacPlanView, find_latest_plan_meta_in_gcs, load_plan_view_from_gcs

SHA_A = "a" * 40
SHA_B = "b" * 40


class FakeListingClient:
    def __init__(self, blobs):
        self._blobs = blobs
        self.calls = []

    def list_blobs(self, bucket_name, prefix=None):
        self.calls.append((bucket_name, prefix))
        return [b for b in self._blobs if b.name.startswith(prefix or "")]


def _blob(name, generation=1):
    return SimpleNamespace(name=name, generation=generation)


def test_picks_highest_run_id():
    client = FakeListingClient([
        _blob(f"pr-7/{SHA_A}/run-100-1/metadata.json", 11),
        _blob(f"pr-7/{SHA_B}/run-200-1/metadata.json", 5),
    ])
    got = find_latest_plan_meta_in_gcs(7, bucket_name="bkt", client=client)
    assert got == (f"pr-7/{SHA_B}/run-200-1/metadata.json", 5)
    assert client.calls == [("bkt", "pr-7/")]


def test_attempt_breaks_run_tie():
    client = FakeListingClient([
        _blob(f"pr-7/{SHA_A}/run-100-1/metadata.json", 1),
        _blob(f"pr-7/{SHA_A}/run-100-3/metadata.json", 2),
    ])
    got = find_latest_plan_meta_in_gcs(7, bucket_name="bkt", client=client)
    assert got == (f"pr-7/{SHA_A}/run-100-3/metadata.json", 2)


def test_ignores_non_metadata_and_malformed_names():
    client = FakeListingClient([
        _blob(f"pr-7/{SHA_A}/run-100-1/plan.json", 1),
        _blob(f"pr-7/{SHA_A}/run-100-1/plan.tfplan", 1),
        _blob("pr-7/evil/run-1-1/metadata.json", 1),          # sha not hex40
        _blob(f"pr-7/{SHA_A}/run-0-1/metadata.json", 1),       # run id 0
        _blob(f"pr-77/{SHA_A}/run-999-1/metadata.json", 1),    # other PR (prefix-safe)
        _blob(f"pr-7/{SHA_A}/run-100-1/metadata.json", 4),
    ])
    got = find_latest_plan_meta_in_gcs(7, bucket_name="bkt", client=client)
    assert got == (f"pr-7/{SHA_A}/run-100-1/metadata.json", 4)


def test_none_when_no_artifacts():
    client = FakeListingClient([])
    assert find_latest_plan_meta_in_gcs(7, bucket_name="bkt", client=client) is None


@pytest.mark.parametrize("bad", [0, -1, "7", 1.5, None, True])
def test_rejects_non_positive_int_pr(bad):
    with pytest.raises(ValueError):
        find_latest_plan_meta_in_gcs(bad, bucket_name="bkt",
                                     client=FakeListingClient([]))


# --------------------------------------------------------------------------- #
# Task 2: load_plan_view_from_gcs tests
# --------------------------------------------------------------------------- #


def _c2v1_metadata(pr=7, *, plan_json_bytes):
    # Mirrors driftscribe_lib.iac_plan_metadata.build_metadata's field set.
    sha = SHA_A
    return {
        "schema_version": "c2.v1",
        "repo": "adi-prasetyo/driftscribe",
        "pr_number": pr,
        "head_sha": sha,
        "base_sha": SHA_B,
        "workflow_run_id": "100",
        "workflow_run_attempt": "1",
        "artifact_uri_plan": f"gs://driftscribe-hack-2026-tofu-artifacts/pr-{pr}/{sha}/run-100-1/plan.tfplan",
        "artifact_uri_json": f"gs://driftscribe-hack-2026-tofu-artifacts/pr-{pr}/{sha}/run-100-1/plan.json",
        # NOTE: run id + attempt are STRINGS in c2.v1 (iac_plan_metadata.py
        # _check "must be a digit string") — int values fail _assert_c2v1_metadata.
        "generation_plan": "1",
        "generation_json": "2",
        "plan_sha256": "c" * 64,
        "plan_json_sha256": hashlib.sha256(plan_json_bytes).hexdigest(),
        "opentofu_version": "1.12.0",
        "provider_lockfile_sha256": "d" * 64,
    }


class FakeGcsClient(FakeListingClient):
    """Listing + generation-pinned fetch, matching fetch_gcs_object's calls."""

    def __init__(self, blobs, objects):
        super().__init__(blobs)
        self._objects = objects  # {(name, int(generation)): bytes}

    def bucket(self, name):
        outer = self

        class _B:
            def blob(self, object_name, generation=None):
                class _Blob:
                    def download_as_bytes(self, raw_download=True, if_generation_match=None):
                        return outer._objects[(object_name, if_generation_match)]
                return _Blob()
        return _B()


def _fixture_client(pr=7, plan_json=None):
    plan_json_bytes = json.dumps(plan_json if plan_json is not None
                                 else {"resource_changes": []}).encode()
    md = _c2v1_metadata(pr, plan_json_bytes=plan_json_bytes)
    meta_name = f"pr-{pr}/{SHA_A}/run-100-1/metadata.json"
    return FakeGcsClient(
        blobs=[_blob(meta_name, 3)],
        objects={
            (meta_name, 3): json.dumps(md).encode(),
            (f"pr-{pr}/{SHA_A}/run-100-1/plan.json", 2): plan_json_bytes,
        },
    )


def test_load_from_gcs_happy_path_verifies_and_summarizes():
    bucket = "driftscribe-hack-2026-tofu-artifacts"
    view = load_plan_view_from_gcs(7, bucket_name=bucket, client=_fixture_client())
    assert isinstance(view, IacPlanView)
    assert view.unverifiable is False
    assert view.integrity_ok is True
    assert view.denylist_violations == []
    assert view.change_summary is not None and view.change_summary.entries == ()
    assert view.tofu_show_text == ""  # no C2 comment on this path — by design
    assert view.generation_metadata == "3"
    assert view.artifact_uri_metadata.endswith("/metadata.json")


def test_load_from_gcs_none_when_no_artifact():
    bucket = "driftscribe-hack-2026-tofu-artifacts"
    assert load_plan_view_from_gcs(7, bucket_name=bucket,
                                   client=FakeGcsClient([], {})) is None


def test_load_from_gcs_unverifiable_on_malformed_metadata():
    bucket = "driftscribe-hack-2026-tofu-artifacts"
    client = _fixture_client()
    meta_name = f"pr-7/{SHA_A}/run-100-1/metadata.json"
    client._objects[(meta_name, 3)] = b"not json"
    view = load_plan_view_from_gcs(7, bucket_name=bucket, client=client)
    assert view is not None and view.unverifiable is True


def test_load_from_gcs_unverifiable_on_pr_mismatch():
    # metadata claims a different PR than the listing prefix — refuse.
    bucket = "driftscribe-hack-2026-tofu-artifacts"
    plan_json_bytes = json.dumps({"resource_changes": []}).encode()
    md = _c2v1_metadata(9, plan_json_bytes=plan_json_bytes)  # claims PR 9
    meta_name = f"pr-7/{SHA_A}/run-100-1/metadata.json"      # listed under PR 7
    # build_metadata cross-checks URIs against pr_number, so craft URIs for 9
    # but store under 7 — the loader must catch the mismatch itself.
    client = FakeGcsClient(
        blobs=[_blob(meta_name, 3)],
        objects={(meta_name, 3): json.dumps(md).encode()},
    )
    view = load_plan_view_from_gcs(7, bucket_name=bucket, client=client)
    assert view is not None and view.unverifiable is True


def test_load_from_gcs_unverifiable_on_path_identity_mismatch():
    # Codex must-fix 1: a (stale/copied) metadata object whose CONTENT is a
    # valid c2.v1 doc for the right PR but whose identity fields don't match
    # the object path it was listed at — e.g. run-100-1 content stored under a
    # newer-looking run-999-1 path. Trusting it would redirect the plan.json
    # fetch to a different run. Fail-closed unverifiable.
    bucket = "driftscribe-hack-2026-tofu-artifacts"
    plan_json_bytes = json.dumps({"resource_changes": []}).encode()
    md = _c2v1_metadata(7, plan_json_bytes=plan_json_bytes)  # run_id 100, attempt 1
    meta_name = f"pr-7/{SHA_A}/run-999-1/metadata.json"      # path claims run 999
    client = FakeGcsClient(
        blobs=[_blob(meta_name, 3)],
        objects={(meta_name, 3): json.dumps(md).encode()},
    )
    view = load_plan_view_from_gcs(7, bucket_name=bucket, client=client)
    assert view is not None and view.unverifiable is True


def test_load_from_gcs_unverifiable_on_repo_mismatch():
    bucket = "driftscribe-hack-2026-tofu-artifacts"
    view = load_plan_view_from_gcs(
        7, bucket_name=bucket, client=_fixture_client(),
        expected_repo="someone-else/other-repo",
    )
    assert view is not None and view.unverifiable is True


def test_load_from_gcs_repo_check_passes_and_is_optional():
    bucket = "driftscribe-hack-2026-tofu-artifacts"
    ok = load_plan_view_from_gcs(
        7, bucket_name=bucket, client=_fixture_client(),
        expected_repo="adi-prasetyo/driftscribe",
    )
    assert ok is not None and ok.unverifiable is False
    # expected_repo=None (unset GITHUB_REPO, e.g. local dev) skips the check.
    skipped = load_plan_view_from_gcs(
        7, bucket_name=bucket, client=_fixture_client(), expected_repo=None,
    )
    assert skipped is not None and skipped.unverifiable is False


def test_load_from_gcs_integrity_mismatch_flagged():
    bucket = "driftscribe-hack-2026-tofu-artifacts"
    client = _fixture_client()
    client._objects[(f"pr-7/{SHA_A}/run-100-1/plan.json", 2)] = b'{"tampered": 1}'
    view = load_plan_view_from_gcs(7, bucket_name=bucket, client=client)
    assert view is not None and view.integrity_ok is False
