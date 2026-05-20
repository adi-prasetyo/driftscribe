"""CI guard — registry ⇄ worker-regex alignment for the upgrade workload (Phase 17.C.5).

The upgrade workers (:mod:`workers.upgrade_reader.main` and
:mod:`workers.upgrade_docs.main`) defend their lockfile-path argument
with a regex (``_LOCKFILE_PATH_RE``) applied via ``re.fullmatch`` at
request time (Layer 2 — payload-intent policy). The coordinator-side
authority lives in :data:`agent.workloads.registry.UPGRADE_TARGET_REGISTRY`,
which carries the canonical ``lockfile_path`` the workers actually
expect.

If those two ever drift apart — e.g. someone updates the registry path
to ``demo/upgrade-target-v2/package.json`` without updating the worker
regex — the coordinator's request would reach the worker, and the
worker would reject with 400/403. The failure is detected, but only
after a coordinator deploy + a real upgrade request fires the
mismatched path through the wire. That's a slow feedback loop for a
purely-text invariant that CI can pin at build time.

This file pins three properties:

1. ``UPGRADE_TARGET_REGISTRY["phase17_demo"].lockfile_path`` is
   accepted by :data:`workers.upgrade_reader.main._LOCKFILE_PATH_RE`
   via ``re.fullmatch``.
2. Same path is accepted by
   :data:`workers.upgrade_docs.main._LOCKFILE_PATH_RE`.
3. The two regex patterns are byte-identical. The 17.C.3 plan note
   says "Same shape as ``workers/upgrade_reader``"; pinning byte
   equality means a future copy-paste drift between the two files
   fails CI loudly. (If a future ecosystem split actually needs the
   two regexes to diverge, the test is the right place to encode that
   intent — comment explaining why before relaxing the assertion.)

TODO(17.E): when ``infra/cloudbuild.yaml`` grows ``UPGRADE_TARGET_REPO``
and (if introduced) ``UPGRADE_TARGET_LOCKFILE_PATH`` env entries for the
upgrade workers, extend this test to parse cloudbuild and assert
registry agreement. The plan §17.C.5 step 4 specified parsing cloudbuild
here too, but as of 17.C.5 the upgrade workers don't yet have deploy
infra entries — that lands in 17.E along with the env-pin half of this
guard. Today the workers read ``UPGRADE_TARGET_REPO`` from environment
at boot (see ``workers/upgrade_reader/main.py:76``), so a future deploy
env mismatch would still be caught at worker boot (KeyError on the
env read); CI just can't see it ahead of deploy yet.

Why this lives in tests/integration not tests/unit
--------------------------------------------------

The test imports both the coordinator-side registry AND worker source
modules in the same process. Worker source imports are normally a
worker-isolation smell — the coordinator must not ``from agent import``
into worker code (and vice-versa) because the deploy boundary keeps
those processes separate. But this test isn't *coordinator code*; it's
a CI-only assertion that runs in pytest, never in a worker container.
The worker isolation invariant (``grep -rn "from agent\\|^import agent"
workers/``) stays empty after this file lands.
"""
from __future__ import annotations

import os
import re

import pytest


# Env MUST be set before importing ``workers.upgrade_reader.main`` or
# ``workers.upgrade_docs.main`` — both modules read UPGRADE_TARGET_REPO /
# GITHUB_TOKEN / GCP_PROJECT / OWN_URL / ALLOWED_CALLERS at IMPORT time
# (boot-time fail-fast, matches production semantics — see
# ``workers/upgrade_reader/main.py:76`` and ``workers/upgrade_docs/main.py:93``).
# Without this seeding the first ``from workers.upgrade_*`` import inside a
# test body raises ``KeyError`` BEFORE the assertion logic ever runs.
#
# We use ``setdefault`` so a hosted CI environment that already exports
# these vars (unlikely but possible) keeps its values. The actual values
# don't matter for THIS file — we only ever read ``_LOCKFILE_PATH_RE``,
# not ``TARGET_REPO`` — but the import-time read must succeed. Mirrors
# the pattern in ``workers/upgrade_reader/tests/test_read.py`` and
# ``workers/upgrade_docs/tests/test_patch.py``.
os.environ.setdefault("UPGRADE_TARGET_REPO", "adi-prasetyo/driftscribe")
os.environ.setdefault("GITHUB_TOKEN", "test-token")
os.environ.setdefault("GCP_PROJECT", "test-proj")
os.environ.setdefault("OWN_URL", "https://upgrade-worker.example.com")
os.environ.setdefault(
    "ALLOWED_CALLERS",
    "coordinator@test-proj.iam.gserviceaccount.com",
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def phase17_demo_target():
    """Return the phase17_demo upgrade target from the registry.

    Wrapped as a fixture so the import is scoped to the tests that need
    it — the registry module pulls in ADK tool callables at import time
    via ``agent.adk_tools``; we want the import cost (and any future
    side-effects of that import chain) to be opt-in per test rather
    than at module load.
    """
    from agent.workloads.registry import UPGRADE_TARGET_REGISTRY

    return UPGRADE_TARGET_REGISTRY["phase17_demo"]


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_registry_lockfile_path_accepted_by_upgrade_reader_regex(
    phase17_demo_target,
):
    """Registry path must satisfy ``upgrade_reader._LOCKFILE_PATH_RE.fullmatch``.

    If this assertion fails: either the registry's ``lockfile_path``
    changed without updating the reader's regex (most likely), or the
    reader's regex changed without updating the registry (less likely
    — the registry is the source of truth). Either way: update the
    side that should follow the other, and audit the deploy infra
    (17.E) before merging.
    """
    from workers.upgrade_reader.main import _LOCKFILE_PATH_RE

    path = phase17_demo_target.lockfile_path
    match = _LOCKFILE_PATH_RE.fullmatch(path)
    assert match is not None, (
        f"UPGRADE_TARGET_REGISTRY['phase17_demo'].lockfile_path={path!r} "
        f"does NOT match upgrade_reader's _LOCKFILE_PATH_RE pattern "
        f"{_LOCKFILE_PATH_RE.pattern!r}. The coordinator would dispatch "
        f"a request that the worker would reject with 400 — fix the "
        f"registry path OR the worker regex, then re-run."
    )


def test_registry_lockfile_path_accepted_by_upgrade_docs_regex(
    phase17_demo_target,
):
    """Same invariant for the docs worker. Both workers' regexes are
    consulted (one on /read, one on /patch); a registry path that
    only one of them accepts would let read succeed but propose fail
    (or vice-versa) — a silent partial failure mid-conversation.
    """
    from workers.upgrade_docs.main import _LOCKFILE_PATH_RE

    path = phase17_demo_target.lockfile_path
    match = _LOCKFILE_PATH_RE.fullmatch(path)
    assert match is not None, (
        f"UPGRADE_TARGET_REGISTRY['phase17_demo'].lockfile_path={path!r} "
        f"does NOT match upgrade_docs's _LOCKFILE_PATH_RE pattern "
        f"{_LOCKFILE_PATH_RE.pattern!r}. The coordinator's "
        f"upgrade_propose_pr_tool would dispatch a request that the "
        f"worker would reject with 403 — fix the registry path OR the "
        f"worker regex, then re-run."
    )


def test_upgrade_reader_and_docs_use_identical_lockfile_regex():
    """The two workers' ``_LOCKFILE_PATH_RE`` patterns must be byte-identical.

    The 17.C.3 plan note pinned this as a textual invariant ("Same
    shape as ``workers/upgrade_reader``"). If the two regexes diverge,
    a registry path can be accepted by one worker and rejected by the
    other — that's exactly the mid-conversation partial-failure mode
    the per-worker tests above each detect on their own side, but the
    byte-equality pin here catches the divergence at the source instead
    of relying on a registry path that happens to expose it.

    If a future ecosystem split (e.g. uv.lock for python) genuinely
    requires the two workers to enforce different shapes, this test
    is the right place to encode the intent — replace ``==`` with a
    pair of patterns the two regexes are SUPPOSED to differ along,
    with a comment explaining why.
    """
    from workers.upgrade_docs.main import (
        _LOCKFILE_PATH_RE as DOCS_RE,
    )
    from workers.upgrade_reader.main import (
        _LOCKFILE_PATH_RE as READER_RE,
    )

    assert READER_RE.pattern == DOCS_RE.pattern, (
        f"upgrade_reader and upgrade_docs lockfile regex patterns "
        f"diverged:\n"
        f"  reader: {READER_RE.pattern!r}\n"
        f"  docs:   {DOCS_RE.pattern!r}\n"
        f"The two workers must agree byte-for-byte on what counts as a "
        f"valid lockfile_path or coordinator requests will succeed on "
        f"one worker and fail on the other mid-turn."
    )
    # Defense in depth: pin the flags too. ``re.compile`` defaults to
    # zero flags, but a future ``re.IGNORECASE`` on one side and not the
    # other would still satisfy ``pattern ==`` while accepting different
    # inputs. Pinning ``.flags`` blocks that.
    assert READER_RE.flags == DOCS_RE.flags, (
        f"upgrade_reader and upgrade_docs lockfile regex FLAGS diverged: "
        f"reader={READER_RE.flags!r}, docs={DOCS_RE.flags!r}. Same "
        f"failure mode as a pattern divergence — fix the side that "
        f"shouldn't have changed."
    )


def test_upgrade_lockfile_regex_uses_fullmatch_semantics(phase17_demo_target):
    """Defense in depth: prove the registry path matches via ``fullmatch``,
    not just ``search`` / ``match``.

    The workers use ``re.fullmatch`` at request time (see
    ``workers.upgrade_reader.main._validate_lockfile_path`` and
    ``workers.upgrade_docs.main._check_lockfile_path``). The regex
    itself ends in ``\\Z`` so ``match``, ``search`` and ``fullmatch``
    all behave identically against well-formed inputs today, but a
    future refactor that drops the ``\\Z`` (e.g. someone "simplifies"
    the pattern) without auditing the call sites could leave only
    ``fullmatch`` strict. Pin the strict semantics here so the regex
    can't quietly weaken.
    """
    from workers.upgrade_reader.main import _LOCKFILE_PATH_RE

    path = phase17_demo_target.lockfile_path
    # The registry path must fullmatch — strict.
    assert _LOCKFILE_PATH_RE.fullmatch(path) is not None

    # And a trailing-junk variant must NOT fullmatch — proves the regex
    # is anchored end-of-string. If this assertion ever flips to True,
    # the regex stopped being anchored and the workers' path allowlist
    # widened (real path: an attacker-controlled suffix could route at
    # a different file).
    assert _LOCKFILE_PATH_RE.fullmatch(path + "/../../etc/passwd") is None, (
        f"upgrade_reader's _LOCKFILE_PATH_RE accepted a suffix-extended "
        f"path: {path + '/../../etc/passwd'!r}. The regex must be "
        f"end-of-string anchored (\\Z) so fullmatch rejects trailing "
        f"junk — a regression here would widen the path allowlist."
    )


# --------------------------------------------------------------------------- #
# Module-level sanity — fails fast if a future refactor renames or
# removes the regex constants the tests above rely on.
# --------------------------------------------------------------------------- #


def test_upgrade_workers_expose_lockfile_path_regex_constant():
    """If a future refactor renames ``_LOCKFILE_PATH_RE`` (e.g. to
    ``_LOCKFILE_RE``), every assertion above starts failing with an
    ``AttributeError`` chain that obscures the real cause. This one
    test fails with a clear "the symbol moved" message instead.
    """
    import workers.upgrade_docs.main as docs_mod
    import workers.upgrade_reader.main as reader_mod

    assert hasattr(reader_mod, "_LOCKFILE_PATH_RE"), (
        "workers.upgrade_reader.main no longer exports "
        "_LOCKFILE_PATH_RE — if the symbol was renamed, update the "
        "imports in tests/integration/test_upgrade_deploy_pin.py. "
        "If it was REMOVED, the CI guard above can no longer pin "
        "the registry⇄regex invariant — revisit the design."
    )
    assert hasattr(docs_mod, "_LOCKFILE_PATH_RE"), (
        "workers.upgrade_docs.main no longer exports _LOCKFILE_PATH_RE "
        "— see the reader-side message above for resolution."
    )
    # Both must be re.Pattern objects, not strings — guards against a
    # refactor that stores the raw pattern in a string for some
    # debugging convenience.
    assert isinstance(reader_mod._LOCKFILE_PATH_RE, re.Pattern)
    assert isinstance(docs_mod._LOCKFILE_PATH_RE, re.Pattern)
