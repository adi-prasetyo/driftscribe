"""Post-LLM deterministic validator for the upgrade-docs worker (Phase 17.C.3a).

Runs after the LLM decides to call the upgrade-docs worker, before any
GitHub write. Pure (no I/O); fails closed.

The validator is a defense-in-depth layer between the agent's decision
and the worker's GitHub call. Opening a PR is reversible, but it IS a
repo write — the LLM should not be the only gate. Each rule below is
deterministic, hardcoded, and pinned by tests (positive + negative per
rule) in :mod:`workers.upgrade_docs.tests.test_post_llm_validator`.

Five rules, evaluated in order; the first failure short-circuits:

1. ``lockfile_path`` matches :data:`_LOCKFILE_PATH_RE` (defense-in-depth
   duplicate of ``main._check_lockfile_path``'s regex — if either guard
   is ever changed without updating the other, the validator's tests
   surface the drift independently of the handler's tests).
2. ``package_name`` is present in ``current_lockfile["dependencies"]``
   — refuse to mutate packages that aren't already declared (adding
   new deps is out of scope for this worker).
3. ``target_version > current_version`` (semver — no downgrades; equal
   versions also rejected since they cannot be a real upgrade).
4. ``version_jump`` ∈ {patch, minor} — major bumps refused at the
   validator with a message that names ``escalation`` (matches
   ``ACTION_REGISTRY`` vocabulary; agent should have routed major
   bumps to the ``escalation`` action, and if it didn't this is the
   fail-closed catch).
5. ``advisory_url`` matches :data:`_GHSA_ADVISORY_RE` —
   ``https://github.com/advisories/GHSA-...`` only. Hardcoded so a
   caller-supplied arbitrary URL cannot be cited in the PR body.

Status-code convention mirrors the rest of the upgrade-docs worker:
policy violations → 403, schema-shaped failures (unparseable version,
unknown package) → 422.

The validator is transport-agnostic: rules raise
:class:`UpgradeValidationError` carrying ``status_code`` + ``reason``;
the FastAPI handler converts to ``HTTPException``. Keeping the validator
free of FastAPI imports makes it trivially unit-testable and reusable
if the worker ever grows a non-HTTP entry point.

Worker isolation invariant: this module MUST NOT import ``agent.*``. The
ACTION vocabulary it references in error messages is hardcoded by string
("escalation") — the worker stays compileable and deployable without the
coordinator's authority code. The cross-workload contract between the
validator's vocabulary and the registry's vocabulary is pinned by
``tests/unit/test_upgrade_contract.py`` and by the contract loader's
own model validator (see :mod:`agent.upgrade_contract`).
"""
from __future__ import annotations

import re


# Hardcoded defense-in-depth regex duplicating ``main._LOCKFILE_PATH_RE``.
# Deliberately duplicated rather than imported from ``main`` so the
# validator's tests pin the regex independently — if ``main.py`` is
# refactored to widen the path allowlist, the validator's tests fail
# loudly here even before integration runs.
_LOCKFILE_PATH_RE = re.compile(r"demo/upgrade-target/package\.json\Z")

# GHSA advisory URL shape. Matches the canonical GitHub Security
# Advisory link form GHSA emits: scheme + host + path are all literal,
# the GHSA id is ``GHSA-`` followed by ASCII letters/digits/hyphens.
# Using ``^...$`` plus the absence of any wildcards on host/scheme
# means the only thing the validator passes is a github.com advisory
# URL — no other host, no userinfo, no port, no query string.
_GHSA_ADVISORY_RE = re.compile(r"^https://github\.com/advisories/GHSA-[a-zA-Z0-9-]+$")

# Bare semver triple with optional prerelease/build metadata suffix.
# Phase 17 uses the npm demo target which pins bare ``M.m.p`` versions;
# we tolerate prerelease/build suffixes (``-beta.1``, ``+build.5``) by
# stripping them via the non-capturing group, then compare on the
# (major, minor, patch) triple only. Full SemVer 2.0.0 ordering of
# prerelease segments is out of scope for the Phase 17 demo.
_VERSION_TRIPLE_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


class UpgradeValidationError(Exception):
    """Raised by :func:`validate_upgrade_request` on a rule failure.

    Carries ``status_code`` (403 for policy violations, 422 for
    schema-shaped failures) and ``reason`` (a human-readable error
    string the handler forwards as the HTTP detail). The validator
    deliberately does NOT raise ``HTTPException`` itself — keeping it
    transport-agnostic means it's unit-testable without FastAPI in
    play and reusable from any future non-HTTP entry point.
    """

    def __init__(self, status_code: int, reason: str) -> None:
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason


def parse_version(v: str) -> tuple[int, int, int]:
    """Parse a bare ``M.m.p`` (with optional ``-prerelease`` or
    ``+build`` suffix) into a ``(major, minor, patch)`` triple.

    Raises:
        UpgradeValidationError: ``status_code=422`` if ``v`` does not
            match :data:`_VERSION_TRIPLE_RE`. The unparseable-version
            path is 422 rather than 403 because the failure is shape-
            shaped: the input doesn't satisfy the schema the validator
            needs. (Compare with 403 for "version parsed fine, but the
            transition is a downgrade / major bump.")
    """
    m = _VERSION_TRIPLE_RE.match(v)
    if not m:
        raise UpgradeValidationError(
            status_code=422,
            reason=(
                f"version {v!r} is not a parseable semver triple "
                "(expected M.m.p with optional -prerelease/+build suffix)"
            ),
        )
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def validate_upgrade_request(
    *,
    lockfile_path: str,
    package_name: str,
    target_version: str,
    advisory_url: str,
    current_lockfile: dict,
) -> None:
    """Run the five deterministic rules in order; raise on first failure.

    Each rule has a positive + at least one negative test in
    ``tests/test_post_llm_validator.py``. The rules are evaluated in
    the order documented at the module top — earlier rules guard cheap
    invariants (regex shape), later rules need the parsed lockfile +
    version triples. Short-circuiting on the first failure keeps error
    messages crisp (the operator sees the first thing that's wrong,
    not a concatenated list).

    Args:
        lockfile_path: The lockfile path the request wants to mutate.
            Must match the worker's hardcoded path allowlist.
        package_name: The dependency key inside ``dependencies`` to
            bump. Must already be present in ``current_lockfile``.
        target_version: The version string to write into the lockfile.
            Must parse as a semver triple and be strictly greater than
            the current version on patch or minor axes only.
        advisory_url: The GHSA advisory URL cited in the PR body. Must
            be a github.com/advisories/GHSA-... URL.
        current_lockfile: The parsed ``package.json`` already read from
            GitHub by the handler. Pass-through avoids a second read.

    Raises:
        UpgradeValidationError: The first rule to fail. Inspect
            ``status_code`` (403/422) and ``reason`` for details.
    """
    # Rule 1: lockfile_path regex. Defense-in-depth duplicate of the
    # main.py guard — both must reject the same set; the test suite
    # pins each side independently so a one-sided refactor surfaces
    # immediately.
    if not _LOCKFILE_PATH_RE.fullmatch(lockfile_path):
        raise UpgradeValidationError(
            status_code=403,
            reason=f"lockfile_path not in allowlist: {lockfile_path!r}",
        )

    # Rule 2: package_name must already exist in the lockfile's
    # dependencies. Worker only bumps existing deps — adding new ones
    # is outside its action surface. (Supersedes the prior inline
    # safety net at main.py:320-334 which has been removed.)
    deps = current_lockfile.get("dependencies") or {}
    if package_name not in deps:
        raise UpgradeValidationError(
            status_code=422,
            reason=(
                f"package_name {package_name!r} not present in "
                "lockfile dependencies (cannot add new deps; worker "
                "only bumps existing ones)"
            ),
        )

    # Parse both versions before rules 3 and 4 — both rules consume
    # the parsed triples. If either side is unparseable we surface 422
    # via :func:`parse_version`.
    current_version_raw = deps[package_name]
    current_triple = parse_version(current_version_raw)
    target_triple = parse_version(target_version)

    # Rule 3: target > current. Reject downgrades and equal versions.
    # Equal is rejected because it cannot be a real upgrade — either
    # the agent decided wrong, or someone is using the worker as a
    # whitespace-formatting tool. Either way: 403.
    if target_triple <= current_triple:
        raise UpgradeValidationError(
            status_code=403,
            reason=(
                f"target_version {target_version!r} is not greater than "
                f"current version {current_version_raw!r} (no downgrades; "
                "equal versions also refused)"
            ),
        )

    # Rule 4: version_jump ∈ {patch, minor}. Major bumps must route to
    # the ``escalation`` action — if the agent didn't escalate and
    # instead called us with a major bump, this is the fail-closed
    # catch.
    if target_triple[0] > current_triple[0]:
        raise UpgradeValidationError(
            status_code=403,
            reason=(
                f"major version bump refused at validator "
                f"({current_version_raw!r} -> {target_version!r}); "
                "agent should have routed this to the 'escalation' action"
            ),
        )

    # Rule 5: advisory_url shape. Hardcoded to GHSA only — a caller-
    # supplied arbitrary URL cannot be cited in the PR body.
    if not _GHSA_ADVISORY_RE.fullmatch(advisory_url):
        raise UpgradeValidationError(
            status_code=403,
            reason=(
                "advisory_url must be a github.com/advisories/GHSA-... "
                f"URL: got {advisory_url!r}"
            ),
        )
