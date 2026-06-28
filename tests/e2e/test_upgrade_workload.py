"""E2E: upgrade workload — observe via GitHub branch + title pattern."""
import time

import pytest

from tests.e2e._github_helpers import (
    count_open_upgrade_prs,
    github_cleanup_pr,
    list_open_upgrade_prs,
)


@pytest.mark.e2e
def test_upgrade_reader_chat_returns_reply(coordinator_client):
    """Read-only: /chat workload=upgrade returns a non-empty reply + tool_calls."""
    resp = coordinator_client.post(
        "/chat",
        json={"workload": "upgrade", "prompt": "What advisories exist for lodash?",
              "ephemeral": True},  # read-only probe: don't litter the rail
        timeout=180.0,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("reply"), "expected non-empty reply"
    assert isinstance(body.get("tool_calls"), list)


@pytest.mark.e2e
def test_minor_bump_opens_real_pr(coordinator_client, e2e_github_repo):
    """Minor bump (4.17.20 → 4.17.21) → PR with branch upgrade/lodash-4-17-21 appears."""
    pre_count = count_open_upgrade_prs(e2e_github_repo)

    resp = coordinator_client.post(
        "/chat",
        json={
            "workload": "upgrade",
            "prompt": (
                "please open a PR bumping lodash to the latest patch version "
                "per the advisory. CONFIRM_UPGRADE_PR=1."
            ),
        },
        timeout=300.0,
    )
    assert resp.status_code == 200, resp.text

    deadline = time.monotonic() + 60.0
    new_pr = None
    while time.monotonic() < deadline:
        for pr in list_open_upgrade_prs(e2e_github_repo):
            if pr.head.ref.startswith("upgrade/lodash-"):
                new_pr = pr
                break
        if new_pr:
            break
        time.sleep(3.0)

    assert new_pr is not None, (
        f"expected lodash upgrade PR in {e2e_github_repo}; "
        f"pre_count={pre_count}, post_count={count_open_upgrade_prs(e2e_github_repo)}"
    )
    # Title contract check.
    assert new_pr.title.startswith("upgrade(lodash):"), \
        f"unexpected title: {new_pr.title!r}"

    github_cleanup_pr(f"https://github.com/{e2e_github_repo}/pull/{new_pr.number}")


@pytest.mark.e2e
def test_major_bump_refused_no_pr_opened(coordinator_client, e2e_github_repo):
    """Major bump (→5.0.0) → validator refuses → NO upgrade/lodash- PR."""
    pre_count = count_open_upgrade_prs(e2e_github_repo)
    resp = coordinator_client.post(
        "/chat",
        json={
            "workload": "upgrade",
            "prompt": (
                "please open a PR bumping lodash to 5.0.0 (major release). "
                "CONFIRM_UPGRADE_PR=1."
            ),
        },
        timeout=180.0,
    )
    assert resp.status_code == 200, resp.text

    time.sleep(30.0)
    post_count = count_open_upgrade_prs(e2e_github_repo)
    assert post_count == pre_count, (
        f"validator did not block major bump: upgrade PR count {pre_count} -> {post_count}"
    )
