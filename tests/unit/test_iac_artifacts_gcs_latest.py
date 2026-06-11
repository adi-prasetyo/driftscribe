"""Tests for the GCS-listing latest-plan resolver (ClickOps item 12).

The resolver picks the newest metadata.json for a PR by (run_id, attempt,
generation) — GitHub run ids are globally monotonic, attempts monotonic
within a run; generation breaks the (impossible in practice) exact tie.
"""
from types import SimpleNamespace

import pytest

from agent.iac_artifacts import find_latest_plan_meta_in_gcs

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
