# Phase 17 upgrade-workload demo target

This directory is the **intentionally-vulnerable** target the upgrade
workload acts on for the Phase 17 demo. It is not real production code
and is not referenced by the main agent / worker images — only the
upgrade workers read this `package.json` (via the GitHub Contents API)
when exercising the dependency-upgrade flow.

## Why this directory exists

DriftScribe Phase 17 extends the agent framework with a second workload
(`upgrade`) that detects vulnerable dependencies and proposes upgrade
PRs. To demo that flow end-to-end we need:

1. A `package.json` with a **specific, pinned version** of a real
   package that has a **published GitHub Advisory**, so the upgrade
   reader's call to the GitHub Advisory Database returns a real GHSA
   reference.
2. A target the upgrade-docs worker can safely open a PR against
   without affecting any other part of the repo. The post-LLM
   validator (`workers/upgrade_docs/validator.py`, Phase 17.C.3a)
   pins the writable path to exactly `demo/upgrade-target/package.json`
   — every other path is refused.

## The demonstrated vulnerability

| Field                | Value                                              |
| -------------------- | -------------------------------------------------- |
| Package              | `lodash`                                           |
| Pinned version       | `4.17.20`                                          |
| Advisory ID          | `GHSA-35jh-r3h4-6jhm`                              |
| CVE                  | `CVE-2021-23337`                                   |
| Severity             | High                                               |
| Type                 | Command injection via `_.template` (prototype-pollution-adjacent) |
| First patched version | `4.17.21`                                         |
| Advisory URL         | <https://github.com/advisories/GHSA-35jh-r3h4-6jhm> |

The agent should propose a patch-jump bump from `4.17.20` to `4.17.21`
(the lowest patched version), with a PR body citing the GHSA URL. The
upgrade is patch-level so it routes through the `upgrade_pr` decision
rule, not `escalation`.

## Why `lodash` and not something else

Candidates considered:

- **`lodash@4.17.20`** *(chosen)* — single high-severity advisory,
  no transitive complexity, advisory page is stable and easy to link to,
  upgrade path is a clean patch bump (`4.17.20` → `4.17.21`). The
  upgrade workflow makes the smallest possible code change in the demo.
- `axios@0.21.0` — also viable, but the patched version (`0.21.1`)
  is a patch bump on an unusual major (`0.x`), which sometimes confuses
  semver tooling and would muddy the demo's "patch jump" narrative.
- `minimist@1.2.5` — extremely well-known but the advisory was
  reclassified across versions; the simplest demo wants a single
  unambiguous advisory.
- `node-fetch@2.6.0` — multiple advisories of varying severity make
  the "match exactly one GHSA" pin harder.

We pick lodash because it gives the demo the cleanest possible
single-advisory single-package single-patch-bump narrative. If the
GHSA ever gets de-listed or its severity is reclassified, swap the
package and update the table above.

## Do not "fix" this file

Do NOT bump the lodash version here. The pin is the *whole point* —
the demo shows the agent proposing the bump, not the bump itself
landing. CI that runs `npm audit` on this directory is expected to
fail; that's the demo working as designed.

If `npm audit` for the demo target ever surprises you with extra
findings, it's because the GHSA database evolved. Check the advisory
URL above and update the table; only change the pinned version if the
narrative needs it.

## Wiring into the agent

The path-and-repo binding lives in
`agent/workloads/registry.py::UPGRADE_TARGET_REGISTRY["phase17_demo"]`:

```python
UpgradeTarget(
    target_repo="adi-prasetyo/driftscribe",
    lockfile_path="demo/upgrade-target/package.json",
    advisory_source="github",
)
```

The upgrade workload's `contract.yaml` references this entry by
symbolic name (`target_name: phase17_demo`). Authority lives in code,
decision rules live in YAML — see the contract file's header for the
Codex 2026-05-20 design rationale.
