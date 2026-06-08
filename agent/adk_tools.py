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

import functools
import re
import secrets
import time
from pathlib import Path
from typing import Any, Callable, NamedTuple

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


def read_project_inventory_tool() -> dict:
    """Ask the Infra-Reader Agent for the whole-project resource inventory.

    No arguments — the worker has the target project pinned via env, and its
    DescribeRequest schema is ``extra="forbid"`` (Layer 2). Returns a bounded
    summary: counts by asset type, each resource labeled declared-in-IaC vs
    not, plus declared_not_found with reason codes. Read-only: the worker holds
    only cloudasset.viewer + serviceUsageConsumer — no mutation, no tofu state,
    no KMS. The summary is CAI-sourced (eventually consistent, partial
    coverage) — present it with its freshness_caveat, and present
    declared_not_found entries as "things to check," never confirmed drift.
    """
    return worker_client.call("infra_reader", {})


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

    SECURITY (PR 2): the rollback worker renders the ``reason`` on the
    operator approval page, and the chat LLM sees live env UNREDACTED
    (``read_live_env_tool`` returns the reader's raw ``env``), so a secret the
    model quoted in ``reason`` would leak onto that page. Unlike the autonomous
    ``_do_rollback`` path, this tool has no ``EnvDiff`` context for a
    value-scoped scrub, and the reader returns raw env — so we do NOT forward
    the model-authored ``reason``. Instead we send a safe reason derived only
    from the (non-secret) ``target_revision``. The model's full rationale stays
    visible in the chat conversation / trace; only the worker-stored,
    operator-rendered string is replaced.
    """
    _ = reason  # accepted for the model's tool contract; intentionally NOT forwarded
    safe_reason = (
        f"Rollback to {target_revision} proposed via DriftScribe chat; "
        "see the conversation/trace for the rationale."
    )
    return worker_client.call(
        "rollback",
        {"target_revision": target_revision, "reason": safe_reason},
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

    **Best-effort, non-fatal.** Notification is the last and least-critical
    step of any workflow — the substantive work (PR opened, rollback
    proposed) has already completed by the time the agent notifies. A
    failing or unreachable webhook must NOT fail the whole /chat turn, so
    a worker error is swallowed and returned as a soft
    ``{"delivered": False, "error": ...}`` result. Report it to the user
    but do not retry the notification within the same turn.
    """
    try:
        return worker_client.call(
            "notifier",
            {"channel": channel, "severity": severity, "body": body},
        )
    except worker_client.WorkerClientError as e:
        return {
            "delivered": False,
            "error": f"notification not delivered ({e.worker} returned {e.status_code})",
            "worker": e.worker,
            "status_code": e.status_code,
        }


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


# --------------------------------------------------------------------------- #
# Upgrade workload tools (Phase 17.C.4)
# --------------------------------------------------------------------------- #
#
# The LLM-facing tool surface for upgrade is deliberately authority-clean
# (Codex 2026-05-20 follow-up — task 17.C.4 step 3):
#
# - ``upgrade_read_dependencies_tool`` takes NO arguments. Repo + lockfile
#   path are derived from :data:`agent.workloads.registry.UPGRADE_TARGET_REGISTRY`
#   via :func:`agent.upgrade_contract.load_upgrade_contract`.
# - ``upgrade_propose_pr_tool`` accepts only the LLM-decision content
#   (``package_name``, ``target_version``, ``advisory_url``, ``body``).
#   Repo, lockfile path, branch, base, and title are derived server-side.
#
# The worker re-validates ``target_repo`` / ``lockfile_path`` / ``branch``
# / ``base`` / ``title`` at request time (Layer 2 defense in depth) — but
# that defense MUST NOT be the primary authority boundary. Letting the
# LLM pick the branch name (for example) would invite ``branch="main"``
# / ``branch="../../.."`` style foot-guns; pre-binding the values here
# is what makes the worker's recheck genuinely defense-in-depth.


@functools.lru_cache(maxsize=1)
def _get_upgrade_target():
    """Resolve and cache the upgrade workload's authoritative target.

    Loads the workload's ``contract.yaml`` via
    :func:`agent.upgrade_contract.load_upgrade_contract` and returns the
    resolved :class:`~agent.workloads.UpgradeTarget` record (``target_repo``
    + ``lockfile_path`` + ``advisory_source``). Cached process-wide so
    the tool callable doesn't redo the YAML parse on every LLM
    invocation. Tests that need a clean state call
    ``_get_upgrade_target.cache_clear()`` (analogous to
    :func:`get_settings.cache_clear`).

    It resolves ONLY the upgrade workload's contract path (via
    :func:`agent.workloads.workload_contract_path`) and parses that
    contract — it deliberately does NOT call ``load_workload("upgrade")``.
    Full upgrade resolution would resolve the upgrade workload's mutation
    workers (``upgrade_docs``) and the notifier, requiring their URL env
    vars. This tool is also exposed by the chat-only, read-only
    ``explore`` workload, which must function without those write-worker
    env vars set; coupling a read tool to write-worker config would break
    read-only/partial-deploy isolation (Codex review 2026-05-25). The
    contract-path resolver keeps the same path-traversal + name-match
    safety as ``load_workload``, minus the worker resolution.
    """
    # Lazy imports — these modules pull pydantic + yaml validation; the
    # tool callable only needs them on the upgrade workload path.
    from agent.upgrade_contract import load_upgrade_contract
    from agent.workloads import workload_contract_path

    contract_path = workload_contract_path("upgrade")
    if contract_path is None:
        # Defense in depth: the upgrade workload's YAML pins
        # ``contract_file: contract.yaml`` so this branch is unreachable
        # in a well-formed deploy. Surface a clear error so a future
        # YAML refactor that drops the field fails loud instead of
        # producing a confusing ``None`` deref downstream.
        raise RuntimeError(
            "upgrade workload manifest is missing contract_file — "
            "cannot derive target_repo / lockfile_path for the LLM "
            "tool surface"
        )
    contract = load_upgrade_contract(contract_path)
    return contract.resolve_target()


def upgrade_read_dependencies_tool() -> dict:
    """Ask the Upgrade Reader Agent for the demo target's deps + advisories.

    No arguments by design — ``target_repo`` and ``lockfile_path`` are
    authority fields and live in
    :data:`agent.workloads.registry.UPGRADE_TARGET_REGISTRY`. The LLM
    never sees a way to redirect this call at a different repo or path.

    The worker re-validates both fields against its env-pinned
    ``UPGRADE_TARGET_REPO`` allowlist (Layer 2 defense in depth — see
    :mod:`workers.upgrade_reader.main`); the coordinator surface here
    is what keeps that re-validation genuinely defensive instead of
    primary.
    """
    target = _get_upgrade_target()
    return worker_client.call(
        "upgrade_reader",
        {
            "target_repo": target.target_repo,
            "lockfile_path": target.lockfile_path,
        },
    )


def upgrade_propose_pr_tool(
    package_name: str,
    target_version: str,
    advisory_url: str,
    body: str,
) -> dict:
    """Ask the Upgrade Docs Agent to open a dependency-upgrade PR.

    The LLM only picks the *decision content* — which package, which
    target version, which advisory URL, and the prose body. Every
    authority field is derived server-side:

    - ``target_repo`` / ``lockfile_path``: from
      :data:`agent.workloads.registry.UPGRADE_TARGET_REGISTRY` via the
      cached upgrade contract.
    - ``branch``: ``upgrade/{package_name}-{ver_dashed}`` so all PRs from
      this worker are observability-scoped to the upgrade workload.
      Matches the worker's :data:`ALLOWED_BRANCH_PREFIX` (``upgrade/``).
    - ``base``: hardcoded to ``"main"``. The worker's
      :func:`_check_base` re-asserts this.
    - ``title``: ``upgrade({package_name}): {target_version}``. The
      worker's :data:`ALLOWED_TITLE_PREFIX` enforces the ``upgrade``
      prefix.

    Layer 2 (payload-intent policy): the worker's ``PatchRequest``
    schema has ``extra="forbid"`` and the post-LLM validator
    (:mod:`workers.upgrade_docs.validator`) re-checks semver no-downgrade,
    patch/minor-only jumps, GHSA URL shape, and package_name existence
    in the lockfile. The validator is the authority for those rules;
    this tool's job is just to keep the LLM out of the routing fields.
    """
    target = _get_upgrade_target()
    # Branch slug: replace every ``.`` in the semver triple with ``-`` so
    # ``4.17.21`` becomes ``4-17-21``. Keeps the branch ref Git-safe and
    # matches the convention pinned in the Phase 17 plan §17.C.4 step 3.
    ver_dashed = target_version.replace(".", "-")
    branch = f"upgrade/{package_name}-{ver_dashed}"
    # Title: deliberately omits the current version. The current version
    # isn't visible without re-reading the lockfile, and the worker only
    # requires the ``upgrade`` prefix — keeping the title simple is
    # preferable to threading the lockfile read through this tool.
    title = f"upgrade({package_name}): {target_version}"
    return worker_client.call(
        "upgrade_docs",
        {
            "target_repo": target.target_repo,
            "lockfile_path": target.lockfile_path,
            "package_name": package_name,
            "target_version": target_version,
            "advisory_url": advisory_url,
            "branch": branch,
            "base": "main",
            "title": title,
            "body": body,
        },
    )


def upgrade_close_pr_tool(pr_number: int, reason: str) -> dict:
    """Ask the Upgrade Docs Agent to close an existing upgrade PR.

    Use when the operator wants to withdraw/abandon an upgrade PR this
    workload opened (superseded, no longer wanted, opened in error). You
    pick ONLY the PR number and a short human ``reason``; ``target_repo``
    is derived server-side. The worker refuses to close anything that
    isn't a DriftScribe upgrade PR — it must carry the ``driftscribe``
    label, sit on an ``upgrade/`` head branch, and target ``main`` — so
    this tool can never close an unrelated collaborator's PR.

    **Best-effort, non-fatal.** A policy refusal (missing label, wrong
    branch, PR not found) or transport error is returned as a soft
    ``{"closed": False, "error": ...}`` dict rather than raised — so the
    operator sees *why* the close was refused instead of the chat turn
    failing with a 502. Report the outcome; do not retry within the same
    turn.
    """
    target = _get_upgrade_target()
    try:
        return worker_client.call_close_pr(target.target_repo, pr_number, reason)
    except worker_client.WorkerClientError as e:
        return {
            "closed": False,
            "error": f"could not close PR #{pr_number}: {e.body or e.status_code}",
            "worker": e.worker,
            "status_code": e.status_code,
        }


def upgrade_merge_pr_tool(pr_number: int) -> dict:
    """Ask the Upgrade Docs Agent to merge an existing upgrade PR.

    Use ONLY when the operator explicitly asks to merge an upgrade PR this
    workload opened — never auto-merge after proposing one. You pick ONLY
    the PR number; ``target_repo``, the squash merge strategy, and the
    required-check allowlist are all pinned server-side. The worker
    merges fail-closed: it refuses unless the PR is a DriftScribe upgrade
    PR (``driftscribe`` label, ``upgrade/`` head, ``main`` base), open,
    conflict-free, and its required CI check (``lint-test``) has passed on
    the head commit.

    **Best-effort, non-fatal.** A provenance refusal, a not-ready refusal
    (checks pending/failed, merge conflict, draft), a not-found, or a
    transport error is returned as a soft ``{"merged": False, "error":
    ...}`` dict rather than raised — so the operator sees *why* the merge
    was refused instead of the chat turn failing with a 502. Report the
    outcome verbatim; do not retry within the same turn.
    """
    target = _get_upgrade_target()
    try:
        return worker_client.call_merge_pr(target.target_repo, pr_number)
    except worker_client.WorkerClientError as e:
        return {
            "merged": False,
            "error": f"could not merge PR #{pr_number}: {e.body or e.status_code}",
            "worker": e.worker,
            "status_code": e.status_code,
        }


# --------------------------------------------------------------------------- #
# Phase D — iac-editor authoring tool
# --------------------------------------------------------------------------- #


def _get_iac_editor_target() -> str:
    """Resolve the iac-editor workload's authoritative target repo.

    Authority field — derived here, never from the LLM. Pin lives in the
    registry (IAC_EDITOR_TARGET) with an IAC_EDITOR_TARGET_REPO_OVERRIDE escape
    hatch for e2e; the tofu-editor worker re-pins IAC_EDITOR_TARGET_REPO at boot
    and re-validates, so this coordinator surface is defense-in-depth, not the
    sole boundary.

    Unlike :func:`_get_upgrade_target` this is NOT lru-cached: the registry
    resolver reads the override env at call time (so e2e / tests can redirect it
    without a process restart), and resolving a single string slug is cheap.
    """
    from agent.workloads.registry import resolve_iac_editor_target  # lazy, mirrors _get_upgrade_target

    return resolve_iac_editor_target()


class IacPrAuthority(NamedTuple):
    """The server-pinned authority/routing fields of an iac-editor PR.

    ``target_repo`` is the registry-pinned editor target; ``branch`` is the
    computed collision-safe ``infra/`` branch. The LLM supplies NEITHER — both
    are derived by :func:`derive_iac_pr_authority`. Packaged as a tuple so the
    one derivation can serve both the single-agent tool and the D5 fan-out
    orchestrator without either re-deriving (or drifting on) these fields.
    """

    target_repo: str
    branch: str


def derive_iac_pr_authority(
    title: str,
    *,
    clock: Callable[[], float] | None = None,
    rng: Callable[[], str] | None = None,
) -> IacPrAuthority:
    """Derive the server-pinned PR authority (target_repo + collision-safe
    ``infra/`` branch) for an iac-editor PR. The LLM never supplies these.

    This is the SINGLE source of the derivation: both
    :func:`open_infra_pr_tool` (the single-agent tool) and the D5 fan-out
    orchestrator (``agent.fanout.run_provision_fanout_stream``) call it, so the
    two authoring paths CANNOT drift apart in how they pin ``target_repo`` or
    compute the branch. The branch is ``infra/{slug(title)}-{ts}-{hex}``:

    - the ``infra/`` prefix scopes the PR to the editor (the worker's
      ``validate_branch`` re-checks),
    - the slug is the title lowercased with every non-``[a-z0-9_-]`` run
      collapsed to ``-`` and the ends stripped, capped at 80 chars (and
      re-stripped so it can't end on a ``-`` before the ``-{ts}`` suffix) —
      keeping the tail well under the worker's 200-char limit for any title
      length, and falling back to the literal ``infra`` for a slug-empty title,
    - ``-{ts}-{hex}`` is a collision-safe suffix (a second-resolution unix
      timestamp + 2 random bytes).

    ``clock``/``rng`` are injectable ONLY so the derivation is deterministically
    testable; both default to the real ``time.time`` / ``secrets.token_hex(2)``.
    """
    _clock = clock or time.time
    _rng = rng or (lambda: secrets.token_hex(2))
    target_repo = _get_iac_editor_target()
    slug = (_BRANCH_SLUG.sub("-", title.lower()).strip("-")[:80].strip("-")) or "infra"
    branch = f"infra/{slug}-{int(_clock())}-{_rng()}"
    return IacPrAuthority(target_repo=target_repo, branch=branch)


def iac_pr_next_steps(pr_number: object) -> str:
    """The operator next-steps reminder appended after an infra PR opens.

    Shared by the single-agent :func:`open_infra_pr_tool` and the D5 fan-out
    orchestrator (``agent.fanout``) so both authoring paths give IDENTICAL
    instructions. The real ``pr_number`` is substituted into the approval path
    when it is a positive int — so the operator gets a usable
    ``/iac-approvals/<N>`` link instead of the literal ``<pr_number>``
    placeholder — falling back to the placeholder when the worker did not
    return a number. (``bool`` is excluded explicitly: it subclasses ``int``.)
    """
    where = (
        f"/iac-approvals/{pr_number}"
        if isinstance(pr_number, int)
        and not isinstance(pr_number, bool)
        and pr_number > 0
        else "/iac-approvals/<pr_number>"
    )
    return (
        "Operator: dispatch the C2 plan-builder on this PR number, then review & "
        f"approve at {where}. A PR that creates NEW resources also needs an "
        "operator re-bake (C6) before it can apply."
    )


def iac_pr_pointer(result: object) -> dict | None:
    """Extract the operator-facing approval pointer from an ``open_infra_pr``
    worker result, or ``None`` when the result is not a CONFIRMED opened PR.

    Returns ``{"pr_number": <int>, "pr_url": <str>}`` ONLY when ``result`` is a
    dict carrying a positive, non-bool ``pr_number`` AND a non-empty string
    ``pr_url``. Any other shape (missing/None/bool/non-positive/non-int number,
    missing/empty/non-str url, or a non-dict) yields ``None``.

    This is the SINGLE shared validation behind the first-authoring approval CTA:
    both the single-agent stream (``agent.adk_agent.run_chat_stream``) and the D5
    fan-out orchestrator (``agent.fanout``) feed their ``open_infra_pr`` result
    through it so the terminal ``done.iac_pr`` field is identical and is never
    surfaced for a malformed / unconfirmed PR. (``bool`` is excluded explicitly:
    it subclasses ``int``.)
    """
    if not isinstance(result, dict):
        return None
    pr_number = result.get("pr_number")
    pr_url = result.get("pr_url")
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number <= 0:
        return None
    if not isinstance(pr_url, str) or not pr_url:
        return None
    return {"pr_number": pr_number, "pr_url": pr_url}


def open_infra_pr_tool(files: list[dict], title: str, body: str) -> dict:
    """Ask the tofu-editor to open ONE iac/-only infrastructure PR.

    The LLM supplies ONLY the decision content: the list of file writes
    (``{"path","content"}`` under iac/, .tf/.md), the PR title, and the body.
    Every authority/routing field is derived server-side and the LLM can never
    influence it:

    - ``target_repo``: registry pin (IAC_EDITOR_TARGET / override) — never LLM.
    - ``branch``: computed ``infra/{slug(title)}-{ts}-{hex}`` (collision-safe, and
      the ``infra/`` prefix scopes the PR to the editor; the worker's
      ``validate_branch`` re-checks).
    - ``base``: pinned ``"main"`` inside :func:`call_open_infra_pr`.
    - ``label``: ``driftscribe-infra``, applied worker-side.

    The tofu-editor worker re-validates every file (iac/-prefix, suffix,
    foundation, traversal, size, AGENT-mode static gate incl. the secret ban)
    before any GitHub call, so this tool deliberately does NOT pre-validate — a
    bad request surfaces the worker's 403/422 to the model as a feedback loop.

    After the PR opens, the operator must: dispatch the C2 plan-builder workflow
    on the PR number, then review + approve at ``/iac-approvals/<pr_number>``; a
    PR that CREATES new resources additionally needs an operator re-bake (C6)
    before it can apply.
    """
    # Authority/routing fields are derived server-side via the SHARED helper
    # (see derive_iac_pr_authority) — the same one the D5 fan-out orchestrator
    # uses, so the two authoring paths can never drift on how they pin the repo
    # or compute the branch. The LLM influences NONE of these.
    authority = derive_iac_pr_authority(title)
    result = worker_client.call_open_infra_pr(
        target_repo=authority.target_repo,
        branch=authority.branch,
        title=title,
        body=body,
        files=files,
    )
    # Compact, LLM-useful result + the required next-steps reminder (the real
    # pr_number is substituted into the /iac-approvals/<N> path so the operator
    # gets a usable link, not a literal placeholder).
    pr_number = result.get("pr_number")
    return {
        "status": result.get("status"),
        "pr_number": pr_number,
        "pr_url": result.get("pr_url"),
        "branch": result.get("branch", authority.branch),
        "next_steps": "PR opened. " + iac_pr_next_steps(pr_number),
    }
