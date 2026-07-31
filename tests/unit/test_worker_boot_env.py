"""ds-2n1 — every worker main booted with its own config, whatever the order.

Worker mains capture config at import and Python caches modules, so the FIRST
importer in a pytest process decides a worker's config for every test that runs
after it. The modules disagree about what that config should be — every one of
the nine wants its own ``OWN_URL``, ``infra_reader`` needs a different
``GCP_PROJECT``, ``ALLOWED_CALLERS`` varies four ways — so before
``workers/_testenv.py`` existed the outcome was whatever collection order
happened to produce.

Measured on the commit before this file landed: a full-suite run booted **six of
nine** workers with the notifier's ``OWN_URL``, and gave the infra reader the
reader's project id on top of that. Nothing failed, because the tests that check
boot-time capture ``monkeypatch.setattr(mod, "OWN_URL", ...)`` first — so they
were asserting on a value the fixture had just written. The bug had eaten its
own guard, which is the specific thing this file is here to prevent recurring.

Two properties, deliberately separate:

- **Correct boot** — each worker's captured config equals its canon. This is
  the property that was false.
- **No leak** — importing a worker main leaves ``os.environ`` exactly as it
  found it. This is what lets a single module import several workers, and what
  stops any one suite from deciding config for the suites after it.

Why the imports happen inside the test functions: a module-scope import here
would make THIS file the first importer of every worker, which would fix the
ordering for the whole run and hide the very thing being measured. At test time
collection is finished, so what these assertions read is what the rest of the
suite actually got.
"""
from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import sys

import pytest

from workers._testenv import (
    WORKER_BOOT_ENV,
    WorkerBootEnvError,
    boot_env,
    import_worker_main,
)

_MODULES = sorted(WORKER_BOOT_ENV)


class _BootScopeVisitor(ast.NodeVisitor):
    """Collect ``os.environ`` reads that happen at IMPORT time.

    The distinction a plain ``ast.walk`` cannot make: a ``def``'s *body* runs
    when called, but its decorators and default arguments are evaluated when
    the ``def`` statement executes — at import. So the body is skipped and
    those are still visited. Class bodies run at import and are visited whole;
    methods inside them are ``FunctionDef``s and get the same treatment as any
    other.

    Annotations are scanned too, which is **conservative rather than exact**:
    three of the nine worker mains use ``from __future__ import annotations``,
    under which annotations are never evaluated at all. Over-counting there is
    the safe direction — it can only demand a canon entry that turns out to be
    unnecessary, never miss a real read.
    """

    def __init__(self, found: set[str], dynamic: list[str], source: pathlib.Path) -> None:
        self._found = found
        self._dynamic = dynamic
        self._source = source

    def _skip_body(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in [*node.args.defaults, *(node.args.kw_defaults or [])]:
            if default is not None:
                self.visit(default)
        if node.returns is not None:
            self.visit(node.returns)
        args = node.args
        every_arg = [
            *args.args,
            *args.posonlyargs,
            *args.kwonlyargs,
            # `*args` / `**kwargs` annotations are scanned like any other, and
            # omitting them was a hole — they are just held on their own
            # attributes rather than in one of the lists.
            *(a for a in (args.vararg, args.kwarg) if a is not None),
        ]
        for arg in every_arg:
            if arg.annotation is not None:
                self.visit(arg.annotation)

    visit_FunctionDef = _skip_body  # noqa: N815 - ast.NodeVisitor dispatch name
    visit_AsyncFunctionDef = _skip_body  # noqa: N815 - ast.NodeVisitor dispatch name

    def visit_Lambda(self, node: ast.Lambda) -> None:
        """Same rule as a ``def``: the body runs when called, defaults do not.

        A module-scope ``f = lambda: os.environ["X"]`` reads nothing at import.
        Missing this was a false positive — it would have demanded a canon
        entry for a runtime-only read.
        """
        for default in [*node.args.defaults, *(node.args.kw_defaults or [])]:
            if default is not None:
                self.visit(default)

    def _record(self, key: ast.expr, where: int) -> None:
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            self._found.add(key.value)
        else:
            self._dynamic.append(f"{self._source}:{where}: {ast.unparse(key)}")

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if ast.unparse(node.value) == "os.environ":
            self._record(node.slice, node.lineno)
            self.visit(node.slice)  # the KEY may itself contain a read
            return  # ...but `node.value` is accounted for — do not re-flag it
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if ast.unparse(node.func) in ("os.environ.get", "os.getenv") and node.args:
            self._record(node.args[0], node.lineno)
            for child in [*node.args, *node.keywords]:
                self.visit(child)
            return  # `node.func` is accounted for
        self.generic_visit(node)

    # ---- aliasing: refuse rather than silently miss ---------------------- #
    #
    # Recognition above is by exact spelling, so any way of getting at the same
    # object under another name would read env at import while this visitor saw
    # nothing — a silent false negative, the one failure mode a completeness
    # guard must not have.
    #
    # Deliberately conservative in three places, all fail-closed: a keyword-form
    # `os.getenv(key="A")` is refused (only the positional literal is
    # recognized), `os.environ["X"] = ...` is recorded like a read because the
    # context is not inspected, and `os.environ.copy()` or passing the mapping
    # around is refused. Each exposes ambient config beyond the canonicalized
    # keys, so refusing costs a house-style constraint and buys no blind spot.
    #
    # The two recognized forms consume their own `os.environ` node above, so
    # ANY other appearance of it reaches `visit_Attribute` below and is
    # rejected. That covers the aliasing shapes generically rather than one
    # `ast` node type at a time — `env = os.environ`, `env: dict = os.environ`,
    # `(env := os.environ)`, `a, b = os.environ, 1`, `f(os.environ)`. An earlier
    # version enumerated `ast.Assign` only and missed the first three; my own
    # probe found them, which is the argument for handling the expression
    # rather than its syntactic context.
    #
    # `from os import getenv` binds a bare name with no `os.` attribute to see,
    # so imports are checked separately.

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == "os" and alias.asname not in (None, "os"):
                self._alias(f"import os as {alias.asname}", node.lineno)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "os":
            for alias in node.names:
                # `*` brings in `environ` and `getenv` without naming either,
                # which is the one import form the attribute check cannot see
                # through — the bound names carry no `os.` prefix.
                if alias.name in ("environ", "getenv", "*"):
                    self._alias(f"from os import {alias.name}", node.lineno)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if ast.unparse(node) in ("os.environ", "os.getenv", "os.environ.get"):
            self._alias(f"{ast.unparse(node)} used as a value", node.lineno)
            return
        self.generic_visit(node)

    def _alias(self, what: str, where: int) -> None:
        self._dynamic.append(
            f"{self._source}:{where}: `{what}` hides env reads from this check "
            f"— write `import os` and `os.environ[\"KEY\"]` instead"
        )


def test_every_worker_main_has_a_canon() -> None:
    """The table covers every deployable worker, so none can opt out silently.

    Discovered from the filesystem rather than hardcoded: a tenth worker added
    without a boot env entry fails here instead of racing quietly.
    """
    workers_dir = pathlib.Path(__file__).resolve().parents[2] / "workers"
    on_disk = {
        f"workers.{p.parent.name}.main"
        for p in workers_dir.glob("*/main.py")
    }
    assert on_disk == set(WORKER_BOOT_ENV)


@pytest.mark.parametrize("module", _MODULES)
def test_canon_covers_every_direct_import_time_env_read(module: str) -> None:
    """Every var a worker reads at import has a canonical test value.

    The discovery test above catches a new *worker*; this catches a new *var*,
    which is the likelier drift and the one that bites quietly. An optional var
    left out of the canon is not neutral: the module's ``os.environ.get``
    default only applies when nothing exported it, so a developer with
    ``UPGRADE_MERGE_METHOD`` set boots a different worker than CI does and gets
    a failure nobody else can reproduce. Both real gaps this found
    (``UPGRADE_MERGE_METHOD`` / ``UPGRADE_REQUIRED_CHECKS``, and five on the
    tofu applier) were exactly that shape.

    Scope matters, and an earlier version of this test got it wrong: it claimed
    ``ast.walk`` stops at a ``def``. It does not — ``walk`` descends into
    function bodies, so runtime-only reads would have been demanded in the
    canon too. (No worker has one today, which is why nothing failed; the claim
    was false regardless.) :class:`_BootScopeVisitor` below is the real thing:
    it skips function *bodies* while still visiting decorators and defaults,
    which execute when the ``def`` statement runs, and it visits class bodies,
    which execute at import. Annotations are scanned conservatively — see
    :class:`_BootScopeVisitor` for why that is over-counting, not exactness.

    The whole worker package is scanned, not only ``main.py``. No sibling
    module reads env at import today (checked), but ``tofu_apply`` imports
    ``gcs_fetch`` and ``tofu_runner`` at boot, so one that started to would be
    just as load-bearing and just as invisible.

    **Stated limit, and it is a real one.** Static traversal cannot follow a
    *call*, so an env read reached through one is invisible here — including a
    module-scope call to a function defined in the same file, and every
    worker's module-scope ``setup_logging()``, which reads ``LOG_LEVEL``. That
    one is canonicalized by hand in ``_SHARED`` precisely because this test
    would never have found it. The claim is therefore "every DIRECT
    import-time read in the worker package", not "every boot input".
    """
    package = pathlib.Path(module.replace(".", "/")).parent
    read: set[str] = set()
    dynamic: list[str] = []

    for source in sorted(package.glob("*.py")):
        visitor = _BootScopeVisitor(read, dynamic, source)
        visitor.visit(ast.parse(source.read_text()))

    # Anything this check cannot verify must FAIL, not be skipped — a
    # completeness guard that silently ignores what it cannot read is not one.
    # Two kinds land here and the message covers both: a key it cannot resolve
    # statically, and an aliasing form that would hide reads from it entirely.
    assert not dynamic, (
        f"{module} has env access this check cannot verify: {dynamic}"
    )
    assert read - set(boot_env(module)) == set(), (
        f"{module} reads these at import with no canonical value: "
        f"{sorted(read - set(boot_env(module)))}. Add them to "
        f"WORKER_BOOT_ENV — use UNSET if the module's own default is what the "
        f"tests should exercise. A default is not a reason to skip it: it only "
        f"applies when nothing exported the var, so leaving it out means the "
        f"developer's shell decides."
    )


@pytest.mark.parametrize("module", _MODULES)
def test_worker_boots_with_its_own_config(module: str) -> None:
    """The worker's captured config is its canon, not another worker's.

    In a full-suite run the module is already imported and this reads what it
    really booted with. Run alone, it imports under the canon and passes
    trivially — which is why the full-suite run is the one that matters.
    """
    mod = import_worker_main(module)
    canon = boot_env(module)

    assert mod.OWN_URL == canon["OWN_URL"].rstrip("/")
    if hasattr(mod, "GCP_PROJECT"):
        assert mod.GCP_PROJECT == canon["GCP_PROJECT"]


@pytest.mark.parametrize("module", _MODULES)
def test_worker_allowlist_is_its_own(module: str) -> None:
    """``ALLOWED_CALLERS`` differs four ways across the workers.

    Checked separately from :func:`test_worker_boots_with_its_own_config`
    because the module parses it into a ``frozenset`` (splits on commas, strips
    blanks) rather than storing it verbatim — a different claim, so a different
    assertion. Mirroring the parse here rather than hardcoding the members is
    what makes the tofu applier's two-caller allowlist checkable by the same
    test as everyone else's one.
    """
    mod = import_worker_main(module)
    expected = frozenset(
        e.strip() for e in boot_env(module)["ALLOWED_CALLERS"].split(",") if e.strip()
    )
    assert mod.ALLOWED_CALLERS == expected


def test_importing_a_worker_leaves_the_process_env_untouched() -> None:
    """No leak, in-process — but note what this does and does not reach.

    By the time this runs, the parametrized tests above have imported every
    worker, so every call here is a cache hit and the import/restore path is
    not exercised at all. Kept because a cache hit still must not touch the
    env, and covered properly by the subprocess test below. (Codex caught that
    this test alone was doing less than its name suggests.)
    """
    before = dict(os.environ)
    for module in _MODULES:
        import_worker_main(module)
    assert dict(os.environ) == before


def test_the_real_import_path_restores_env_in_a_fresh_process() -> None:
    """No leak on the path that actually sets env: a first import.

    Runs in a subprocess because this one cannot be tested in-process — the
    worker modules are already imported, and the whole point is to observe what
    the *import* does. Covers both halves of the restore, which are different
    code paths and fail differently:

    - a key that was ALREADY SET before the import must come back with its
      original value (``OWN_URL`` here, exported to a junk value);
    - an ``UNSET`` key that was set must come back set, even though the helper
      had to delete it to let the module's default apply
      (``ARTIFACT_BUCKET``);
    - a key that was ABSENT must be absent again, not left behind as "".
    """
    probe = """
import os, sys
sys.path.insert(0, ".")
from workers._testenv import import_worker_main
before = dict(os.environ)
m = import_worker_main("workers.tofu_apply.main")
# Imported AFTER the helper, deliberately. `gcs_fetch` is a boot sibling of
# tofu_apply's main; importing it first would import it under the hostile
# exported env, and if it ever starts reading ARTIFACT_BUCKET itself the
# assertion below would compare a wrong value to the same wrong value. Codex
# reproduced exactly that. Ordering it here means the sibling is whatever the
# canonical boot produced.
from workers.tofu_apply import gcs_fetch
# The module got the canon despite the hostile exports — asserted against the
# exact expected values, not merely "different from the hostile one".
assert m.OWN_URL == "https://tofu-apply.example.com", m.OWN_URL
assert m.ARTIFACT_BUCKET == gcs_fetch.ARTIFACT_BUCKET, m.ARTIFACT_BUCKET
assert m.PLAN_APPROVALS_DB is None, m.PLAN_APPROVALS_DB
# ...and the process env is exactly as it was found.
assert dict(os.environ) == before, {
    k: (before.get(k), os.environ.get(k))
    for k in set(before) | set(os.environ)
    if before.get(k) != os.environ.get(k)
}
print("ok")
"""
    env = {
        **os.environ,
        "OWN_URL": "https://exported-junk.example.com",  # present before
        "ARTIFACT_BUCKET": "someone-elses-bucket",  # present, but UNSET canon
    }
    env.pop("PLAN_APPROVALS_DB", None)  # absent before, must stay absent
    run = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=env,
        cwd=pathlib.Path(__file__).resolve().parents[2],
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "ok"


def test_import_bypass_is_detected() -> None:
    """A worker imported outside the helper is caught, not silently accepted.

    The guard has to work on the CACHE path, because that is the only path a
    bypass can take: the offending plain import already put the module in
    ``sys.modules``, so the helper never runs the import at all. Simulated by
    swapping in a stand-in module carrying the wrong ``OWN_URL`` — the same
    state a stray ``from workers.reader.main import app`` produces when it wins
    the race.
    """
    module = "workers.reader.main"
    real = import_worker_main(module)

    class _MisbootedStandIn:
        OWN_URL = "https://notifier.example.com"  # another worker's value
        GCP_PROJECT = "test-proj"

    sys.modules[module] = _MisbootedStandIn  # type: ignore[assignment]
    try:
        with pytest.raises(WorkerBootEnvError, match="booted with OWN_URL"):
            import_worker_main(module)
    finally:
        sys.modules[module] = real


def test_bypass_is_detected_beyond_own_url() -> None:
    """A correct OWN_URL does not excuse the rest of the config.

    The first version of this guard checked OWN_URL and GCP_PROJECT only, which
    left a fail-open in the fix itself: a bypassing import that ran while a
    correct OWN_URL happened to be set could still capture a wrong
    ``NOTIFY_WEBHOOK_URL`` or ``APPROVAL_HMAC_KEY`` from an exported shell env
    and sail through. Both non-URL cases are checked here — a plain string and
    the parsed ``frozenset`` — because they take different branches.
    """
    module = "workers.notifier.main"
    real = import_worker_main(module)

    class _WrongWebhook:
        OWN_URL = "https://notifier.example.com"  # correct
        GCP_PROJECT = "test-proj"  # correct
        NOTIFY_WEBHOOK_URL = "https://someone-elses-webhook.example.com"

    class _WrongAllowlist:
        OWN_URL = "https://notifier.example.com"
        GCP_PROJECT = "test-proj"
        NOTIFY_WEBHOOK_URL = "https://webhook.example.com/test"
        ALLOWED_CALLERS = frozenset({"attacker@example.com"})

    try:
        sys.modules[module] = _WrongWebhook  # type: ignore[assignment]
        with pytest.raises(WorkerBootEnvError, match="NOTIFY_WEBHOOK_URL"):
            import_worker_main(module)

        sys.modules[module] = _WrongAllowlist  # type: ignore[assignment]
        with pytest.raises(WorkerBootEnvError, match="ALLOWED_CALLERS"):
            import_worker_main(module)
    finally:
        sys.modules[module] = real


def test_boot_scope_visitor_knows_what_runs_at_import() -> None:
    """Pin the visitor's scope rules — each line here is a different rule.

    Written because the previous implementation's docstring described rules it
    did not have. These cases are the ones where "does this run at import?" is
    genuinely non-obvious, and each was checked against the answer by hand.
    """

    def scan(source: str) -> tuple[set[str], int]:
        found: set[str] = set()
        dynamic: list[str] = []
        _BootScopeVisitor(found, dynamic, pathlib.Path("probe.py")).visit(ast.parse(source))
        return found, len(dynamic)

    # Runs at import.
    assert scan('import os\nV = os.environ["A"]\n')[0] == {"A"}
    assert scan('import os\nif True:\n    V = os.environ["A"]\n')[0] == {"A"}
    assert scan('import os\nclass C:\n    v = os.environ["A"]\n')[0] == {"A"}
    assert scan('import os\ndef g(a=os.environ["A"]): pass\n')[0] == {"A"}
    assert scan('import os\n@os.environ["A"]\ndef g(): pass\n')[0] == {"A"}
    assert scan('import os\nV = [os.environ["A"] for _ in (1,)]\n')[0] == {"A"}

    # Does NOT run at import — a body only executes when called.
    assert scan('import os\ndef g():\n    return os.environ["A"]\n')[0] == set()
    assert scan('import os\nf = lambda: os.environ["A"]\n')[0] == set()

    # Variadic annotations are scanned conservatively, like any other.
    assert scan('import os\ndef g(*a: os.environ["A"], **k: os.environ["B"]): pass\n')[0] == {
        "A",
        "B",
    }

    # Unreadable key at import scope: loud, not silent.
    assert scan('import os\nk = "A"\nV = os.environ[k]\n')[1] == 1
    # ...but an unreadable key inside a function body is not our business.
    assert scan('import os\ndef g(k):\n    return os.environ[k]\n')[1] == 0

    # Aliasing would make reads invisible, so it is refused rather than missed.
    # The last four all bind the same object and an earlier `ast.Assign`-only
    # version of this check caught only one of them.
    assert scan('from os import getenv\nV = getenv("A")\n')[1] == 1
    assert scan('from os import environ\nV = environ["A"]\n')[1] == 1
    assert scan('import os as _os\nV = _os.getenv("A")\n')[1] == 1
    assert scan('import os\nenv = os.environ\nV = env["A"]\n')[1] == 1
    assert scan('import os\nenv: dict = os.environ\n')[1] == 1
    assert scan('import os\nV = (env := os.environ)["A"]\n')[1] == 1
    assert scan('import os\na, b = os.environ, 1\n')[1] == 1
    assert scan('import os\nV = dict(os.environ)\n')[1] == 1
    assert scan('import os\nenv = {}\nenv |= os.environ\n')[1] == 1
    # A wildcard import binds `environ`/`getenv` without naming them, so it is
    # the one form no attribute check can see through.
    assert scan('from os import *\nV = environ["A"]\n')[1] == 1

    # The supported forms must stay silent, or the check is unusable.
    assert scan('import os\nV = os.environ["A"]\n')[1] == 0
    assert scan('import os\nV = os.environ.get("A", "d")\n')[1] == 0
    assert scan('import os\nV = os.getenv("A")\n')[1] == 0
    # ...including when a read is nested inside another read's key.
    nested = scan('import os\nV = os.environ[os.environ["OUTER"]]\n')
    assert nested[0] == {"OUTER"} and nested[1] == 1  # inner key is dynamic


def test_a_present_but_unusable_value_is_not_excused() -> None:
    """Absent means "not kept under that name". Present-and-wrong means mis-boot.

    The first version conflated them by reading ``getattr(mod, key, None)`` and
    skipping anything non-string, so a module holding ``OWN_URL = None`` or
    ``ALLOWED_CALLERS = None`` passed verification — a fail-open Codex found in
    my own fix. A ``_MISSING`` sentinel tells the two apart.

    The empty allowlist is included for the opposite reason to the one I first
    wrote down: ``verify_caller`` checks ``any(compare_digest(email, a) for a in
    allowed)``, so an empty set matches nobody and 403s *everyone*. Not a
    security hole — a total outage. And only ``tofu_editor`` refuses to boot on
    one; the other eight start up and then reject every request, which is
    exactly the sort of thing worth catching in a test rather than in prod.
    """
    module = "workers.reader.main"
    real = import_worker_main(module)
    canon = boot_env(module)
    shared = {
        "GCP_PROJECT": canon["GCP_PROJECT"],
        "TARGET_SERVICE": canon["TARGET_SERVICE"],
        "TARGET_REGION": canon["TARGET_REGION"],
        "LOG_LEVEL": canon["LOG_LEVEL"],
    }
    good_callers = frozenset({canon["ALLOWED_CALLERS"]})

    cases = {
        "OWN_URL": {**shared, "OWN_URL": None, "ALLOWED_CALLERS": good_callers},
        "ALLOWED_CALLERS=None": {
            **shared, "OWN_URL": canon["OWN_URL"], "ALLOWED_CALLERS": None,
        },
        "ALLOWED_CALLERS=empty": {
            **shared, "OWN_URL": canon["OWN_URL"], "ALLOWED_CALLERS": frozenset(),
        },
        # Not iterable at all. Its own branch because it is the one that used
        # to escape as a raw TypeError out of the error-message construction
        # rather than as a verdict.
        "ALLOWED_CALLERS=notiterable": {
            **shared, "OWN_URL": canon["OWN_URL"], "ALLOWED_CALLERS": 123,
        },
    }
    try:
        for label, attrs in cases.items():
            sys.modules[module] = type("_StandIn", (), attrs)  # type: ignore[assignment]
            with pytest.raises(WorkerBootEnvError, match=label.split("=")[0]):
                import_worker_main(module)
    finally:
        sys.modules[module] = real


def test_bypass_is_detected_even_when_every_visible_value_is_right() -> None:
    """Provenance, not values, is what actually closes the door.

    A value check can only see config the worker keeps under its env var's own
    name. Codex demonstrated three bypasses through the gaps: upgrade_docs
    renames ``UPGRADE_TARGET_REPO`` to ``TARGET_REPO``, tofu_apply renames
    ``TF_VAR_tofu_state_kms_key`` to ``TOFU_STATE_KMS_KEY``, and infra_reader
    turns ``IAC_DIR`` into a ``Path`` — so a plain import carrying an attacker's
    repo, KMS key, or iac directory passed every comparison.

    The stand-in below is that state exactly: every checkable value correct,
    the renamed one wrong. Only "this is not the object I imported" catches it.
    """
    module = "workers.upgrade_docs.main"
    real = import_worker_main(module)
    canon = boot_env(module)

    class _RenamedValueWrong:
        OWN_URL = canon["OWN_URL"]
        GCP_PROJECT = canon["GCP_PROJECT"]
        GITHUB_TOKEN = canon["GITHUB_TOKEN"]
        UPGRADE_MERGE_METHOD = canon["UPGRADE_MERGE_METHOD"]
        UPGRADE_REQUIRED_CHECKS = canon["UPGRADE_REQUIRED_CHECKS"]
        ALLOWED_CALLERS = frozenset({canon["ALLOWED_CALLERS"]})
        # The worker stores UPGRADE_TARGET_REPO under this name, so no
        # env-name comparison can reach it.
        TARGET_REPO = "attacker/example"

    sys.modules[module] = _RenamedValueWrong  # type: ignore[assignment]
    try:
        with pytest.raises(WorkerBootEnvError, match="not imported by"):
            import_worker_main(module)
    finally:
        sys.modules[module] = real


def test_unknown_worker_names_its_fix() -> None:
    """An unregistered module gets pointed at the table, not a bare KeyError."""
    with pytest.raises(KeyError, match="WORKER_BOOT_ENV"):
        boot_env("workers.nonexistent.main")
