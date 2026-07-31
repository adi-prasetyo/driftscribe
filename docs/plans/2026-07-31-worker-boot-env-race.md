# ds-2n1 — worker boot env: one door instead of nine races

Follow-on to [ds-thm](2026-07-31-producer-consumer-bound-audit.md) §5b, which
filed this rather than fixing it.

## The defect

Every worker `main` reads its config at **import** and fails closed:

```python
OWN_URL = os.environ["OWN_URL"].rstrip("/")
```

That is deliberate — it mirrors the Cloud Run revision refusing to boot on a
missing var. The consequence for tests is that env must be in place *before* the
import, too early for a fixture, so each test module set it at module scope with
`os.environ.setdefault`.

`setdefault` is **first-import-wins for the whole pytest process**, and the
modules disagree: every one of the nine workers wants its own `OWN_URL`,
`infra_reader` needs `GCP_PROJECT=driftscribe-hack-2026` where everyone else
wants `test-proj`, and `ALLOWED_CALLERS` differs four ways.

## Measured before touching anything

The bead described this as latent. It was not:

| worker | `OWN_URL` it booted with | own canon |
|---|---|---|
| docs | `https://notifier.example.com` | `docs.example.com` |
| infra_reader | `https://notifier.example.com` | `infra-reader.example.com` |
| reader | `https://notifier.example.com` | `reader.example.com` |
| tofu_apply | `https://notifier.example.com` | `tofu-apply.example.com` |
| tofu_editor | `https://notifier.example.com` | `tofu-editor.example.com` |
| upgrade_reader | `https://notifier.example.com` | `upgrade-reader.example.com` |
| notifier, rollback, upgrade_docs | correct | — |

`infra_reader` also booted with `GCP_PROJECT=test-proj`, not its own project id.

**Six of nine wrong on `OWN_URL`, and the suite was green.** (An earlier draft
of this doc said seven; Codex recounted from a clean `origin/main` probe and was
right. `infra_reader`'s wrong `GCP_PROJECT` is a *seventh defect*, not a seventh
worker.) The reason is the part worth
keeping: a layer of compensating `monkeypatch.setattr(mod, "OWN_URL", ...)` had
grown up inside the very tests named
`test_real_verify_caller_dep_wired_with_env` — the ones whose whole purpose is
to prove the dependency forwards the *boot-captured* value. They set the value,
then asserted on it. The bug had eaten its own guard.

## Why `setdefault` → `os.environ[...]` is not the fix

Python caches modules. Once `workers.docs.main` is in `sys.modules`, the
**first** importer's env is baked in and no later assignment — forced or
not — changes it. Any fix that leaves the canon duplicated across 22 call sites
still depends on which of them runs first.

## The fix: `workers/_testenv.py`

One module owns each worker's canonical boot env, and one function is the only
supported way in:

```python
from workers._testenv import import_worker_main
import_worker_main("workers.reader.main")
```

It forces that worker's env, imports, then **restores the process env exactly as
it found it** — including unsetting keys that were previously unset. Restoring
is what makes it composable: `test_worker_bound_mirrors.py` imports three
workers in a row and leaks none of them into the suites that follow.

Because the canon is centralized, a `sys.modules` cache hit is safe to return —
it was necessarily imported under the same values. That only holds while
everyone uses the door, so `import_worker_main` **verifies** the module's
captured config and raises `WorkerBootEnvError` naming the likely cause when a
plain import got there first.

That verification took two goes to get right, and the failure mode is the
interesting part.

**Attempt 1** checked `OWN_URL` and `GCP_PROJECT` only — a fail-open in the fix
itself. **Attempt 2** widened it to every canon key the module captured under
the same name, with un-checkable ones falling out of an `isinstance` test.
Codex then demonstrated that attempt 2 was *still* fail-open, with three working
bypasses: `upgrade_docs` renames `UPGRADE_TARGET_REPO` to `TARGET_REPO`,
`tofu_apply` renames `TF_VAR_tofu_state_kms_key` to `TOFU_STATE_KMS_KEY`, and
`infra_reader` turns `IAC_DIR` into a `Path`. A plain import carrying an
attacker's repo, KMS key, or iac directory passed every comparison.

The lesson is that **comparing values can only ever see what the worker chose to
keep under the env var's own name** — the exclusion list is not a list I can
finish. **Attempt 3 checks provenance**: the helper records the module object it
imported, and refuses any cache hit that is not that object. Value comparison is
kept, and runs first, because a wrong value gives a far better message than
"wrong object".

Attempt 3 had two fail-opens of its own, both found by Codex and both closed:
`getattr(mod, key, None)` plus a `isinstance(actual, str)` skip meant a module
holding `OWN_URL = None` or `ALLOWED_CALLERS = None` passed. A `_MISSING`
sentinel now distinguishes *absent* — not retained under the env key's own name,
whether renamed on capture (`UPGRADE_TARGET_REPO` -> `TARGET_REPO`), consumed
transitively (`LOG_LEVEL`), or simply unused by that worker, all legitimate and
all covered by provenance — from *present and unusable* (a mis-boot).
Comparison is on
`str(actual)` so a `Path`-typed capture still compares. `ALLOWED_CALLERS` keeps
its own branch: parsed to a `frozenset` and differing four ways. An earlier
draft here said an empty allowlist "would admit everybody" — that is backwards,
and Codex caught it. `verify_caller` does
`any(compare_digest(email, a) for a in allowed)`, so an empty set matches nobody
and 403s everyone: a total outage rather than a hole. A follow-up draft then
claimed "every worker already refuses to boot with one", which is also wrong —
only `tofu_editor` has that guard; the other eight boot happily and then reject
every request. Rejected here for that reason.

**Scope of the guarantee, stated because it is narrower than "airtight".** A
module `importlib.reload`-ed under different env keeps its identity and would
pass; an attribute mutated after a legitimate import is caught only if it is one
of the visible ones. Codex demonstrated both and recommended attesting them with
an env-key -> captured-attribute resolver table. I did not, deliberately: this
guards against *accidental import order* — the failure that was actually
happening — not against code running adversarially inside the same pytest
process. Nothing reloads a worker main. The docstring now says this plainly
rather than claiming identity "has no gaps", which was the version Codex was
right to reject.

Test-only: worker Dockerfiles `COPY` their sources file-by-file, so this module
never reaches a container.

## What changed

- **New** `workers/_testenv.py` — the canon + `import_worker_main`.
- **New** `tests/unit/test_worker_boot_env.py` — 36 tests. Discovers workers
  from disk so a tenth cannot opt out; parses every `.py` in each worker
  package with a scope-aware `ast` visitor so a new *import-time env read*
  cannot opt out either; checks correct boot, the
  allowlist parse, no env leak (in-process *and* in a fresh subprocess, which
  is the only place the import/restore path is reachable), the visitor's own
  scope rules, and four grades of bypass — wrong `OWN_URL`; correct `OWN_URL`
  with a wrong webhook or allowlist; a value present but unusable
  (`OWN_URL = None`, a `None`/empty/non-iterable allowlist); and every visible
  value correct with only a renamed one wrong, which only provenance catches.
- **22 test modules migrated** off `setdefault`.
- **Compensating pins removed** from the six `test_real_verify_caller_dep_
  wired_with_env` tests, plus the reader's client fixture and the infra
  reader's autouse `_pin_module_constants` (the latter replaced by an explicit
  assertion test). They now assert against what the module really booted with.
  Their docstrings explained why the pin was needed; those explanations are
  replaced rather than left to go stale.
- **`LOG_LEVEL` canonicalized in `_SHARED`.** No worker `main` reads it, but
  every one of them calls `setup_logging()` at module scope, which does — so an
  exported `LOG_LEVEL=DEBUG` changes boot state and the log-assertion tests with
  it. The static check cannot follow a call into another package, so this one
  is pinned by hand and the check's claim narrowed to "every direct import-time
  read in the worker package" rather than "every boot input".
- **Optional import-time vars canonicalized too**, which the first cut missed:
  `TARGET_SERVICE` / `TARGET_REGION` (reader, rollback), `UPGRADE_MERGE_METHOD`
  / `UPGRADE_REQUIRED_CHECKS` (upgrade_docs), and `IAC_DIR` / `CF_ACCESS_*` /
  `ARTIFACT_BUCKET` / `PLAN_APPROVALS_DB` (tofu_apply). A var with a default in
  `main.py` is *not* neutral to leave out — the default only applies when
  nothing exported it. `UPGRADE_MERGE_METHOD=merge` in a developer's shell fails
  `test_merge_happy_path_forwards_policy_and_returns_result` and nobody else can
  reproduce it (verified by dropping the canon entry and re-running). An `UNSET`
  sentinel expresses "must be absent so the module's own default applies",
  which a canon of only-strings could not say.

## Verification

Every claim was checked by making it fail on purpose:

1. **The defect is real** — probe over `sys.modules` in a full run: 6/9 wrong
   on `OWN_URL`, plus `infra_reader`'s project id (table above).
2. **The guard catches a bypass** — reverting `workers/docs/tests/test_patch.py`
   to a plain import produced
   `WorkerBootEnvError: workers.docs.main booted with OWN_URL='https://notifier.example.com', expected 'https://docs.example.com'`.
3. **The un-masked tests now bite** — flipping the reader's canon to a wrong
   value fails `test_real_verify_caller_dep_wired_with_env`. Note it does *not*
   fail the boot-env guard: the guard checks consistency with the canon, the
   worker's own suite pins what the canon must be. Two separate claims, two
   separate tests.
4. **Order independence** — `pytest tests/ workers/` and `pytest workers/
   tests/` both pass; each of the nine worker suites also passes standalone.
5. **Codex's three bypasses replayed** against the hardened helper — all three
   rejected (`upgrade_docs` with `UPGRADE_TARGET_REPO=attacker/example`,
   `infra_reader` with `IAC_DIR=/tmp/not-the-repo`, `tofu_apply` with a wrong
   KMS key).
6. **A polluted shell no longer leaks in** —
   `UPGRADE_MERGE_METHOD=merge UPGRADE_REQUIRED_CHECKS=external-check
   ARTIFACT_BUCKET=... PLAN_APPROVALS_DB=... CF_ACCESS_TEAM_DOMAIN=...` and the
   workers still capture the canon.

An earlier injection attempt is worth recording because it *failed to
reproduce*: reverting `tests/integration/test_notify_preserves_approval_url.py`
to its leaking `setdefault` form changed nothing, because forcing at import
beats a stale value in `os.environ`. The fix defends against the leak; only a
bypass of the door defeats it. Injection 2 was rewritten accordingly.

## Residual

`import_worker_main` cannot name *which* module bypassed it — `sys.modules`
does not record the importer. The error explains how to find it (grep for an
import of that module with no preceding helper call) and notes that running the
failing file alone will pass. Good enough; recording the importer would mean an
import hook, which is a lot of machinery for a diagnostic.

## Why the helper lives in `workers/`, not `tests/`

Codex raised this and called it non-blocking; keeping it here is a deliberate
choice. Worker tests live under `workers/*/tests/`, so a helper in `tests/`
would point the dependency the wrong way — the deployable package's tests
importing the coordinator's test tree. It never ships: all nine Dockerfiles
`COPY` sources file-by-file, and the root distribution excludes `workers`
(`pyproject.toml`). The leading underscore marks it private, and its first
paragraph says test-only.
