"""ADK-facing tool wrappers (Phase 11.7 — worker-delegating rewrite).

Every mutating tool routes through :mod:`agent.worker_client` to one of
the four worker services (reader / docs / rollback / notifier). The
coordinator has zero direct GCP-mutation or GitHub-mutation surface —
the rewrite is what makes the Layer 1 (IAM) trim safe to ship.

Two tools remain coordinator-internal:

- :func:`search_recent_prs_tool` — read-only PR history via the
  coordinator's fine-grained GitHub PAT. Kept here to save one
  network hop per /chat call; there's no IAM benefit to delegating
  a read-only operation.
- :func:`load_contract_tool` — the contract is baked into the
  coordinator's container at build time. Reading it locally is the
  only sensible thing to do.

Layer 0 invariant: the ADK runner's tool set is
:data:`agent.adk_agent.COORDINATOR_TOOLS` — the EXHAUSTIVE list. Adding
a tool here without registering it there leaves it inert (good).
Adding it here AND there without updating the Phase 11.4b inventory
test triggers a CI failure (good). Don't try to be clever; the
inventory test is the safety net.
"""
from __future__ import annotations

import re
import secrets
import time
from pathlib import Path
from typing import Any

from agent import worker_client
from agent.config import get_settings
from agent.contract import load_contract
from agent.github_actions import get_repo


# --------------------------------------------------------------------------- #
# Worker-delegating tools
# --------------------------------------------------------------------------- #


def read_live_env_tool() -> dict:
    """Ask the Reader Agent for the live env + active revision.

    No arguments — the Reader Agent has the target service / region /
    project hardcoded via env at boot. Layer 2 (payload-intent policy):
    the worker's ``ReadRequest`` schema has ``extra="forbid"``, so even
    if the LLM tried to smuggle a service name in here, the worker
    would 422.
    """
    return worker_client.call("reader", {})


def propose_rollback_tool(target_revision: str, reason: str) -> dict:
    """Ask the Rollback Agent to create a HITL approval.

    Returns the worker's response, which includes:

    - ``approval_id``: UUID of the Firestore doc
    - ``approval_token``: single-use raw token (43 chars). The LLM
      should NOT echo this back to the operator — instead, present
      the ``approval_url`` which contains the token as a query param.
    - ``approval_url``: ``{COORDINATOR_URL}/approvals/{id}?t=<token>``
      The operator clicks this to render the decision page; the
      coordinator's approval handler dispatches the actual /execute.
    - ``expires_at``: 15-min TTL.

    The coordinator does NOT execute rollbacks directly. This tool
    only PROPOSES — the operator must visit ``approval_url`` and
    press Approve.
    """
    return worker_client.call(
        "rollback",
        {"target_revision": target_revision, "reason": reason},
    )


# Match git refspec rules (https://git-scm.com/docs/git-check-ref-format):
# allow ASCII letters/digits/`_`/`-`; collapse runs of disallowed chars to `-`.
# Mirrors the legacy main.py branch slug regex.
_BRANCH_SLUG = re.compile(r"[^a-z0-9_-]+")


def patch_docs_tool(
    file_path: str,
    new_content: str,
    title: str,
    body: str,
) -> dict:
    """Ask the Docs Agent to open a docs PR.

    ``file_path`` MUST match the worker's path allowlist
    (``^demo/docs/[^/]+\\.md$``). The worker refuses anything else at
    Layer 2 — this tool does NOT pre-validate, so the LLM sees the
    worker's 403 directly if it picks a bad path. That's the desired
    feedback loop (model learns the constraint from the error).

    The branch name is computed here rather than asked of the LLM
    because the only constraint that matters is collision avoidance
    (timestamp + random suffix) — letting the LLM pick a branch name
    is a foot-gun that yields ``branch="../../etc/passwd"`` exploits
    at worst and noisy unmemorable branch names at best.
    """
    slug = _BRANCH_SLUG.sub("-", Path(file_path).name.lower()).strip("-") or "docs"
    branch = f"driftscribe/{slug}-{int(time.time())}-{secrets.token_hex(2)}"
    return worker_client.call(
        "docs",
        {
            "file_path": file_path,
            "new_content": new_content,
            "branch": branch,
            "base": "main",
            "title": title,
            "body": body,
        },
    )


def notify_tool(channel: str, severity: str, body: str) -> dict:
    """Ask the Notifier Agent to post a webhook notification.

    ``channel`` must be one of ``info | alert | approval`` and
    ``severity`` must be one of ``low | medium | high | critical`` —
    enforced by the worker's ``NotifyRequest`` schema. The webhook URL
    is hardcoded on the worker (Layer 2 — "URL is the capability")
    and cannot be influenced from here.
    """
    return worker_client.call(
        "notifier",
        {"channel": channel, "severity": severity, "body": body},
    )


# --------------------------------------------------------------------------- #
# Coordinator-internal read-only tools
# --------------------------------------------------------------------------- #


def search_recent_prs_tool(keywords: list[str], days: int = 7) -> list[dict]:
    """Read-only PR history search.

    Uses the coordinator's own GitHub PAT (read-only fine-grained
    token in Secret Manager). NOT delegated to a worker because:

    1. Reading public PR metadata is the lowest-privilege operation
       in the system — there is no IAM win from a worker for it.
    2. Saving the network hop matters on a /chat call where the LLM
       may search PRs multiple times in a single turn.

    Keywords match via case-sensitive ``\\b<keyword>\\b`` to mirror the
    classifier's strict matching semantics — the LLM-driven path and
    the deterministic classifier must agree on what "PR mentions this
    var" means.
    """
    if not keywords:
        return []
    s = get_settings()
    if not s.github_repo:
        return []
    # Empty-string token coerced to None for PyGithub compatibility
    # (newer PyGithub raises on ``Github("")``).
    repo = get_repo(s.github_token or None, s.github_repo)
    patterns = [re.compile(rf"\b{re.escape(k)}\b") for k in keywords]
    # Iterate updated-desc and filter rather than break on out-of-window
    # — a PR can be touched recently while merged outside the window,
    # and a fresher in-window PR can come later in the stream.
    import datetime as dt

    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    out: list[dict] = []
    for pr in repo.get_pulls(state="closed", sort="updated", direction="desc"):
        if pr.merged_at is None or pr.merged_at < since:
            continue
        title = pr.title or ""
        body_text = pr.body or ""
        if any(p.search(f"{title} {body_text}") for p in patterns):
            out.append(
                {
                    "title": title,
                    "body": body_text,
                    "url": pr.html_url,
                    "merged": True,
                }
            )
    return out


def load_contract_tool() -> dict[str, Any]:
    """Return the baked-in ops contract as a dict.

    The contract path comes from settings (``CONTRACT_PATH``, default
    ``demo/ops-contract.yaml``). Returns a dict (not :class:`OpsContract`)
    so the LLM reasons over the raw YAML shape; the deterministic
    classifier path uses :func:`agent.contract.load_contract` directly.
    """
    s = get_settings()
    contract = load_contract(Path(s.contract_path))
    return contract.model_dump(mode="json")
