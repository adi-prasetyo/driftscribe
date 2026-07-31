"""Deterministic boot env for worker ``main`` modules under test (ds-2n1).

**Test-only.** No worker Dockerfile copies this file — every worker image
lists its sources file-by-file (``COPY workers/<name>/main.py ...``), so this
module exists solely for the pytest process and never reaches a container.

Why it has to exist
-------------------

Every worker ``main`` reads its config at **import** time and fails closed::

    OWN_URL = os.environ["OWN_URL"].rstrip("/")

That is deliberate — it mirrors the Cloud Run revision refusing to boot on a
missing var. The consequence for tests is that env must be in place *before*
the import, which is too early for a fixture, so each test module used to set
it at module scope with ``os.environ.setdefault``.

``setdefault`` makes the value **first-import-wins for the whole pytest
process**, and the modules disagree: every one of the nine workers wants its own
``OWN_URL``, ``infra_reader`` wants ``GCP_PROJECT=driftscribe-hack-2026``
where everyone else wants ``test-proj``, and ``ALLOWED_CALLERS`` differs four
ways. Measured on ``origin/main`` before this module landed, a full-suite run
booted **six of nine** workers with the *notifier's* ``OWN_URL`` and gave
``infra_reader`` the reader's project id on top of that.

Nothing failed, because a layer of compensating ``monkeypatch.setattr(mod,
"OWN_URL", ...)`` had grown up in the very tests that check the boot-time
capture — so those tests asserted against a value the fixture had just written,
not against what the module actually booted with. The bug was invisible *and*
it had already eaten its own guard.

The fix is not a better ``setdefault``. Python caches modules, so the **first**
importer of a worker main decides its config no matter what any later module
sets. The only thing that removes the race is a single door that owns the
canon:

>>> from workers._testenv import import_worker_main
>>> reader_main = import_worker_main("workers.reader.main")

:func:`import_worker_main` forces that worker's canonical env, imports, then
hands the process env back exactly as it found it — so importing a worker main
has no lasting effect on anything that runs later.

Bypasses fail loudly
--------------------

A plain ``from workers.reader.main import app`` still works and still races.
:func:`import_worker_main` therefore **verifies** what the module captured
against the canon and raises :class:`WorkerBootEnvError` on a mismatch, naming
the likely cause. ``tests/unit/test_worker_boot_env.py`` walks every entry in
:data:`WORKER_BOOT_ENV`, so a bypass introduced anywhere in the suite surfaces
there rather than as a puzzling failure in an unrelated worker.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parents[1]


class _Unset:
    """Sentinel: this var must be ABSENT from the env at import.

    Needed because a canon of only-strings cannot express "let the module's own
    default apply". Several workers read optional vars at import
    (``PLAN_APPROVALS_DB``, ``ARTIFACT_BUCKET``) whose absence is the tested
    behaviour — but "absent" is not the same as "absent unless the developer
    happens to export it", and only the sentinel closes that gap.
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "UNSET"


UNSET = _Unset()

# Distinguishes "the module has no such attribute" (a renamed capture, fine)
# from "the attribute is there and holds None" (a mis-boot). `None` cannot tell
# those apart, and conflating them let `OWN_URL = None` pass verification.
_MISSING = object()

# Values every worker agrees on. Kept separate from the per-worker table below
# so a shared value cannot drift between workers by being retyped nine times.
_SHARED: dict[str, str] = {
    "GCP_PROJECT": "test-proj",
    "ALLOWED_CALLERS": "coordinator@test-proj.iam.gserviceaccount.com",
    "GITHUB_TOKEN": "test-token",
    # Not read by any worker `main` directly — every one of them calls
    # `setup_logging()` at module scope, which reads it
    # (`driftscribe_lib/logging.py`). A transitive boot input is still a boot
    # input: an exported LOG_LEVEL=DEBUG changes what the log-assertion tests
    # see. Pinned here because the static completeness check cannot follow a
    # call into another package, so it would never have found this one.
    "LOG_LEVEL": "INFO",
}

# The canonical boot env for each worker main, keyed by dotted module name.
#
# These values are the ones each worker's own test suite already asserted on
# before ds-2n1 — the table is a re-homing of scattered `setdefault` blocks,
# not a redefinition. Where a worker deliberately differs from `_SHARED` (the
# infra reader's project id, the tofu editor's caller, the tofu applier's
# two-caller allowlist) the override is spelled out with the reason.
WORKER_BOOT_ENV: dict[str, dict[str, str | _Unset]] = {
    "workers.docs.main": {
        **_SHARED,
        "OWN_URL": "https://docs.example.com",
        "TARGET_REPO": "adi-prasetyo/driftscribe",
    },
    "workers.infra_reader.main": {
        **_SHARED,
        "OWN_URL": "https://infra-reader.example.com",
        # The infra reader's fixtures are built from real CAI asset names, which
        # embed the live project id — so its tests are only coherent against
        # `driftscribe-hack-2026`, not the `test-proj` everyone else uses.
        "GCP_PROJECT": "driftscribe-hack-2026",
        "ALLOWED_CALLERS": "coordinator@driftscribe-hack-2026.iam.gserviceaccount.com",
        "IAC_DIR": str(_REPO_ROOT / "iac"),
        "IAC_SNAPSHOT_SHA": "test-sha-abc123",
    },
    "workers.notifier.main": {
        **_SHARED,
        "OWN_URL": "https://notifier.example.com",
        "NOTIFY_WEBHOOK_URL": "https://webhook.example.com/test",
    },
    "workers.reader.main": {
        **_SHARED,
        "OWN_URL": "https://reader.example.com",
        # Both have a production default in main.py, so they are set here not
        # to satisfy the import but to pin it: a developer with TARGET_SERVICE
        # exported would otherwise boot the reader against their own service
        # and see failures nobody else can reproduce.
        "TARGET_SERVICE": "payment-demo",
        "TARGET_REGION": "asia-northeast1",
    },
    "workers.rollback.main": {
        **_SHARED,
        "OWN_URL": "https://rollback.example.com",
        "COORDINATOR_URL": "https://coord.example.com",
        "TARGET_SERVICE": "payment-demo",
        "TARGET_REGION": "asia-northeast1",
        # Self-describing placeholder: a random-looking value would trip
        # GitGuardian's generic high-entropy rule on every commit in the PR
        # (the ds-y5i fixture lesson) while adding nothing.
        "APPROVAL_HMAC_KEY": "test-hmac-key",
    },
    "workers.tofu_apply.main": {
        **_SHARED,
        "OWN_URL": "https://tofu-apply.example.com",
        "COORDINATOR_URL": "https://coord.example.com",
        # Two callers: the operator identity the approval tests act as, plus the
        # coordinator SA. Order matters to nothing, but the operator entry is
        # what the caller==approver fallback tests match on.
        "ALLOWED_CALLERS": "alice@corp.example,coordinator@test-proj.iam.gserviceaccount.com",
        "PLAN_APPROVAL_HMAC_KEY": "test-plan-hmac-key",
        "TF_VAR_tofu_state_kms_key": (
            "projects/p/locations/l/keyRings/r/cryptoKeys/tofu-state"
        ),
        # C5b-2: the offline suite runs in "e2e" mode so the no-JWT
        # propose/apply tests take the legacy caller==approver fallback. The
        # enforce-mode tests monkeypatch `m.IAC_OPERATOR_AUTH_MODE` per-test.
        "IAC_OPERATOR_AUTH_MODE": "e2e",
        # Optional at import — pinned so the module's own default is what the
        # suite actually exercises. Without these a developer with any of them
        # exported boots a different applier than CI does, and the failure is
        # unreproducible for everyone else.
        "IAC_DIR": str(_REPO_ROOT / "iac"),
        "CF_ACCESS_TEAM_DOMAIN": "",
        "CF_ACCESS_AUD_TAG": "",
        "ARTIFACT_BUCKET": UNSET,
        "PLAN_APPROVALS_DB": UNSET,
    },
    "workers.tofu_editor.main": {
        **_SHARED,
        "OWN_URL": "https://tofu-editor.example.com",
        "IAC_EDITOR_TARGET_REPO": "adi-prasetyo/driftscribe",
        # The editor is called by the coordinator's own SA, which its tests
        # spell `driftscribe-agent@` rather than the generic `coordinator@`.
        "ALLOWED_CALLERS": "driftscribe-agent@test-proj.iam.gserviceaccount.com",
    },
    "workers.upgrade_docs.main": {
        **_SHARED,
        "OWN_URL": "https://upgrade-docs.example.com",
        "UPGRADE_TARGET_REPO": "adi-prasetyo/driftscribe",
        # Optional at import, but `test_merge.py` asserts the squash method and
        # the lint-test required check by name — so leaving these to the
        # ambient env makes those tests pass or fail on the developer's shell.
        "UPGRADE_MERGE_METHOD": "squash",
        "UPGRADE_REQUIRED_CHECKS": "lint-test",
    },
    "workers.upgrade_reader.main": {
        **_SHARED,
        "OWN_URL": "https://upgrade-reader.example.com",
        "UPGRADE_TARGET_REPO": "adi-prasetyo/driftscribe",
    },
}

def _captured_matches(expected: str, actual: str) -> bool:
    """Is ``actual`` what a worker would capture from ``expected``?

    Workers normalize on capture — ``OWN_URL`` and ``COORDINATOR_URL`` are
    ``rstrip("/")``-ed, ``NOTIFY_WEBHOOK_URL`` is ``strip()``-ed, most are taken
    verbatim. Accepting any of those three forms is deliberately permissive:
    the job here is to catch a *different worker's value*, and no amount of
    whitespace normalization turns one worker's URL into another's.
    """
    return actual in (expected, expected.rstrip("/"), expected.strip())


class WorkerBootEnvError(RuntimeError):
    """A worker main was imported outside :func:`import_worker_main`.

    Raised when a module's captured config does not match its canon — which
    means something imported it first under whatever env happened to be set.
    """


# Modules this helper imported itself, by dotted name. A cache hit is only
# trustworthy if it is one of THESE objects — see `_verify`.
_IMPORTED: dict[str, ModuleType] = {}


def boot_env(module: str) -> dict[str, str | _Unset]:
    """Return a copy of ``module``'s canonical boot env.

    A copy, so a caller that mutates what it gets back cannot edit the canon
    other importers will be verified against.
    """
    try:
        return dict(WORKER_BOOT_ENV[module])
    except KeyError:
        raise KeyError(
            f"{module!r} has no canonical boot env. Add it to "
            f"workers/_testenv.py::WORKER_BOOT_ENV — defining it there rather "
            f"than in the test file is what keeps import order from mattering."
        ) from None


def _verify(module: str, mod: ModuleType) -> None:
    """Reject a module this helper did not import, or that holds wrong values.

    **What this guarantees, precisely.** It catches the failure mode that was
    actually happening: a plain ``from workers.x.main import y`` that won the
    import race and captured another worker's config. Two complementary checks,
    because neither alone is enough:

    - **Values**, first, because a wrong value produces a far better message
      than "wrong object". Comparison is on ``str(actual)`` so a ``Path``-typed
      capture (``IAC_DIR``) still compares, and a capture that is present but
      not a string at all (``OWN_URL = None``) FAILS rather than being skipped.
      An *absent* attribute is skipped — the var is simply not retained under
      its env key's own name, whether renamed on capture
      (``UPGRADE_TARGET_REPO`` -> ``TARGET_REPO``), consumed transitively
      (``LOG_LEVEL``), or unused by this worker. Values cannot see any of it.
    - **Provenance**, second, for exactly those cases. Codex demonstrated three
      real bypasses through them against a value-only check.

    **What it does NOT guarantee**, so nobody reads more into it than is there:
    a module ``importlib.reload``-ed under a different env keeps its identity
    and would pass, and an attribute mutated after a legitimate import is only
    caught if it is one of the visible ones. Neither is defended against, and
    neither should be — this is a test helper against accidental import order,
    not a sandbox against code running inside the same process. Attesting them
    would need a full env-key -> captured-attribute resolver table, which is
    machinery in proportion to a threat that does not exist here.

    ``ALLOWED_CALLERS`` gets its own branch: it is parsed into a ``frozenset``,
    it is the security-relevant value of the set, and it differs four ways.
    """
    canon = WORKER_BOOT_ENV[module]

    for key, expected in canon.items():
        if not isinstance(expected, str):
            # UNSET — the assertion is "absent at import", enforced when this
            # helper does the import. Re-checking the process env on a cache
            # hit would be wrong: the env has been restored by then, so a
            # legitimately-exported value is present again.
            continue
        if key == "ALLOWED_CALLERS":
            continue  # parsed to a frozenset — handled below
        actual = getattr(mod, key, _MISSING)
        if actual is _MISSING:
            # Not retained under the env key's own name: renamed on capture
            # (UPGRADE_TARGET_REPO -> TARGET_REPO), consumed transitively
            # (LOG_LEVEL, read by setup_logging), or simply not used by this
            # worker. Values cannot see any of those; provenance is what covers
            # them.
            continue
        if _captured_matches(expected, str(actual)):
            continue
        raise WorkerBootEnvError(
            f"{module} booted with {key}={actual!r}, expected {expected!r}.\n"
            f"That means {module} was already in sys.modules before "
            f"import_worker_main() ran, so it captured whatever env another "
            f"test module happened to leave set. Python caches modules: the "
            f"FIRST importer decides the config and no later setdefault can "
            f"change it.\n"
            f"Fix the importer, not this check — every module that imports a "
            f"worker main must go through workers._testenv.import_worker_main.\n"
            f"To find it: grep for an import of {module} that is not preceded "
            f"by an import_worker_main call. Running the failing file alone "
            f"will pass, because then nothing has got there first."
        )

    raw_callers = canon["ALLOWED_CALLERS"]
    assert isinstance(raw_callers, str)  # every worker requires it
    expected_callers = frozenset(e.strip() for e in raw_callers.split(",") if e.strip())
    # `_MISSING`, not `None`: every worker defines ALLOWED_CALLERS, so an
    # attribute holding None is a mis-boot, not an absence to be excused.
    actual_callers = getattr(mod, "ALLOWED_CALLERS", _MISSING)
    try:
        parsed_callers = frozenset(actual_callers or ())
    except TypeError:
        # Not iterable at all (an int, say). Still a mis-boot, and it must
        # surface as WorkerBootEnvError rather than a raw TypeError from this
        # function's own error-message construction.
        parsed_callers = None
    if actual_callers is not _MISSING and parsed_callers != expected_callers:
        raise WorkerBootEnvError(
            f"{module} booted with ALLOWED_CALLERS={actual_callers!r}, "
            f"expected {set(expected_callers)!r}. Same cause as above: the "
            f"module was imported outside import_worker_main and captured "
            f"another worker's allowlist."
        )

    if _IMPORTED.get(module) is not mod:
        raise WorkerBootEnvError(
            f"{module} is in sys.modules but was not imported by "
            f"import_worker_main, so nothing guarantees the config it captured "
            f"— and the values this helper can compare are only the ones the "
            f"worker keeps under their env names. A worker that renames a var "
            f"on capture (UPGRADE_TARGET_REPO -> TARGET_REPO) hides a wrong "
            f"value from every check above.\n"
            f"Fix the importer: grep for an import of {module} not preceded by "
            f"an import_worker_main call. Running the failing file alone will "
            f"pass, because then nothing has got there first."
        )


def import_worker_main(module: str) -> ModuleType:
    """Import a worker ``main`` under its canonical boot env and return it.

    Sets the worker's env, imports, then restores the process env to exactly
    what it was — including removing keys that were previously unset. Restoring
    is the half that makes this composable: a module can import three different
    workers in a row, and nothing downstream inherits the last one's config.

    Already-imported modules are returned from ``sys.modules`` and verified
    rather than re-imported, so calling this from several test modules for the
    same worker is free and the first caller's import is the one that counts.

    **Call this before monkeypatching the module's constants, not after.** The
    verification compares what the module currently holds, so an active
    ``monkeypatch.setattr(mod, "OWN_URL", ...)`` would be read as a mis-boot.
    Every call site today is at module scope or the first line of a helper,
    which is the natural order anyway.

    Raises:
        WorkerBootEnvError: the module was imported outside this function and
            captured the wrong config.
        KeyError: ``module`` has no entry in :data:`WORKER_BOOT_ENV`.
    """
    env = boot_env(module)
    cached = sys.modules.get(module)
    if cached is not None:
        _verify(module, cached)
        return cached

    prior: dict[str, str | None] = {k: os.environ.get(k) for k in env}
    try:
        for key, value in env.items():
            if isinstance(value, str):
                os.environ[key] = value
            else:  # UNSET — the module's own default is the tested behaviour
                os.environ.pop(key, None)
        mod = importlib.import_module(module)
    finally:
        for key, old in prior.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
    _IMPORTED[module] = mod
    _verify(module, mod)
    return mod
