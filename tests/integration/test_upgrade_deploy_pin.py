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

Phase 17.E.1 closed the deferred half of this guard: cloudbuild.yaml
now has UPGRADE_TARGET_REPO env entries on both upgrade workers, and
``test_cloudbuild_upgrade_target_repo_matches_registry`` below parses
the file and asserts agreement with
``UPGRADE_TARGET_REGISTRY["phase17_demo"].target_repo``.

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


# --------------------------------------------------------------------------- #
# Phase 17.E.1 — close the deferred cloudbuild env-pin half of this guard.
#
# infra/cloudbuild.yaml carries the deploy-time UPGRADE_TARGET_REPO value
# for both upgrade workers (--set-env-vars line on each deploy step).
# That value must match the coordinator-side authority in
# UPGRADE_TARGET_REGISTRY["phase17_demo"].target_repo. Without this
# guard, an operator could edit the registry to point at a new repo
# without updating the cloudbuild value — the next deploy would put
# the workers out of sync with the coordinator, and every upgrade
# request would 400 at the worker's repo-allowlist re-validation step.
# --------------------------------------------------------------------------- #


def _read_cloudbuild_upgrade_target_repo_envs() -> dict[str, str]:
    """Return a {worker_service_name: target_repo_value} mapping by
    parsing infra/cloudbuild.yaml as plain text.

    We deliberately avoid loading the YAML through a yaml parser: the
    file uses ``${_TAG}`` and ``$PROJECT_ID`` substitution syntax that
    Cloud Build evaluates at submit time, and yaml.safe_load would
    either fail or load those as opaque strings. Plain-text regex
    parsing is more robust here and the format (gcloud
    ``--set-env-vars=...`` flag inside an ``args`` list) is stable.
    """
    from pathlib import Path

    cloudbuild = (
        Path(__file__).resolve().parents[2] / "infra" / "cloudbuild.yaml"
    )
    text = cloudbuild.read_text()

    out: dict[str, str] = {}
    # Find each `gcloud run deploy <service>` block + its `--set-env-vars=...`
    # line. Both upgrade workers' deploy steps share the same UPGRADE_TARGET_REPO=
    # value today, but we extract per-service so a future divergence is caught
    # explicitly per worker.
    for service in ("driftscribe-upgrade-reader", "driftscribe-upgrade-docs"):
        # Block-find: from the `- <service>` deploy arg to the next blank line.
        m = re.search(
            rf"-\s+{re.escape(service)}\b.*?--set-env-vars=([^\n]+)",
            text,
            re.DOTALL,
        )
        assert m, (
            f"Could not locate UPGRADE_TARGET_REPO in cloudbuild.yaml for "
            f"service {service!r}. If the deploy step was moved/renamed, "
            f"update this parser. If the env var was removed, the worker "
            f"will KeyError at boot — restore it."
        )
        env_line = m.group(1)
        # env_line looks like
        # GCP_PROJECT=$PROJECT_ID,UPGRADE_TARGET_REPO=adi-prasetyo/driftscribe,OWN_URL=...
        kv = re.search(r"UPGRADE_TARGET_REPO=([^,\s]+)", env_line)
        assert kv, (
            f"Could not extract UPGRADE_TARGET_REPO=... from the "
            f"--set-env-vars line for {service!r}. Got: {env_line!r}"
        )
        out[service] = kv.group(1)
    return out


def test_cloudbuild_upgrade_target_repo_matches_registry():
    """The deploy-time UPGRADE_TARGET_REPO on both upgrade workers must
    equal UPGRADE_TARGET_REGISTRY["phase17_demo"].target_repo. Otherwise
    the workers are deployed pointing at a different repo than the
    coordinator authority, and every upgrade request bounces at the
    worker's repo allowlist check.

    Closes the TODO(17.E) from the original 17.C.5 deferral.
    """
    from agent.workloads.registry import UPGRADE_TARGET_REGISTRY

    expected = UPGRADE_TARGET_REGISTRY["phase17_demo"].target_repo
    cloudbuild_envs = _read_cloudbuild_upgrade_target_repo_envs()

    for service, deployed_value in cloudbuild_envs.items():
        assert deployed_value == expected, (
            f"cloudbuild.yaml deploys {service} with "
            f"UPGRADE_TARGET_REPO={deployed_value!r}, but the "
            f"coordinator-side authority "
            f'UPGRADE_TARGET_REGISTRY["phase17_demo"].target_repo is '
            f"{expected!r}. Either update cloudbuild.yaml to match the "
            f"registry, or update the registry to match the deploy "
            f"intent (and double-check that's actually what you want)."
        )


def test_cloudbuild_upgrade_workers_present():
    """Phase 17.E.1 should have added both upgrade-worker deploy steps.
    A future PR that accidentally drops one of them would otherwise
    leave the coordinator routing to a missing service and only
    surface as a runtime 503 — surface it at CI instead.
    """
    cloudbuild_envs = _read_cloudbuild_upgrade_target_repo_envs()
    assert "driftscribe-upgrade-reader" in cloudbuild_envs
    assert "driftscribe-upgrade-docs" in cloudbuild_envs
