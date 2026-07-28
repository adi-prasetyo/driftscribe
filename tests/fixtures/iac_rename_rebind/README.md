# Rename / rebind plan fixtures

Plan shapes for a **renamed** cloud resource: the case where a live object no
longer answers to the address `iac/` declares, and a differently-named object has
taken its place. PR #244 shipped visibility for this ("declared in IaC, not found
live") and deferred reconciliation. These fixtures pin *why* it is deferred, so
the deferral rests on measured behavior instead of reasoning about the plan graph.

Consumed by `tests/unit/test_iac_rename_rebind.py`.

## Why this needed measuring

Attribute drift and identity drift are not the same problem. When a Cloud Run
service's env var drifts, the Terraform address still denotes the same object, so
both reconciliation directions are just `apply`. When a bucket is renamed, the
address denotes nothing, and no amount of `apply` rebinds it — that takes a
state-level operation.

The open question was whether a rename could be reconciled in a **single PR**
whose plan carries no destructive verb: rewrite the declaration and add an
`import` block for the renamed object, yielding one `importing` entry with
`actions == ["no-op"]` — the exact shape the C1 denylist already admits for
ordinary adoption. If so, no policy floor would need to move.

Whether OpenTofu actually orders the plan that way is state-dependent, so it was
measured rather than argued.

## Result

| Scenario | `resource_changes` | `resource_drift` | C1 | Freshness gate |
|---|---|---|---|---|
| Old gone, import at a **new** address | `y: ["no-op"] +importing` | `x: ["delete"]` | **pass** | **refuse** (material) |
| Old gone, import at the **same** address | `x: ["create"]` | `x: ["delete"]` | **pass** | **refuse** (material) |
| Old **still live**, import at a new address | `x: ["delete"]`, `y: ["no-op"] +importing` | — | **block** ×2 | n/a |
| Ordinary adoption (control) | `y: ["no-op"] +importing` | — | pass | not engaged |

Two findings, both load-bearing:

1. **A same-address rebind does not work.** An `import` block whose target is
   already present in prior state is silently skipped — not an error, and not an
   import. Once refresh drops the vanished old object, the plan degrades to a bare
   `create`, which on apply would collide with the already-existing renamed
   object. Reproduced identically on two unrelated providers, so this is core
   plan-graph ordering rather than provider behavior.

2. **A new-address rebind produces exactly the hoped-for `resource_changes`, and
   is still stopped — by a different gate.** C1 reads `resource_changes` and
   nothing else, so it passes. The vanished old object appears under
   `resource_drift` as a `delete`, and the tofu-apply freshness gate
   (`classify_refresh_drift`) calls that material and refuses.

So the safety property holds, but it is the **freshness gate**, not C1, that
holds it. Any future work that relaxes freshness must add an identity-drift rule
to C1 in the same change, or the rename path opens. That is the regression the
accompanying tests exist to catch.

## Provenance

Generated with **OpenTofu 1.12.0** — the version pinned in
`.github/workflows/iac.yml` — via the scripts in `generate/`.

Fixtures are typed `google_storage_bucket` because C1's adoptable allowlist is
GCP-only, but the plans were produced with local providers so no cloud
credentials or real project state were involved:

- **`kreuzwerker/docker`** (`docker_volume`) — the primary. It matches
  `google_storage_bucket` structurally: `name` is the physical identity and
  forces replacement, the remote object genuinely exists and can be deleted out
  of band, and import is by bare name.
- **`hashicorp/tfcoremock`** — cross-check. HashiCorp's provider built to
  exercise Terraform/OpenTofu *core* behavior; remote objects are JSON files, so
  "the object vanished" is `rm`. It reproduced both findings byte-identically.

`hashicorp/local` was tried first and rejected: `local_file` returns
"Resource Import Not Implemented".

### What is verbatim and what is authored

Be precise about this when reading the fixtures:

- **Verbatim from the observed runs:** which addresses appear under
  `resource_changes` vs `resource_drift`, their `actions` tuples, and the
  presence and content of `importing`. This is the empirical payload, and
  `generate/emit_fixtures.py` re-asserts it on every regeneration — it refuses to
  write a fixture whose structure no longer matches the recorded plan.
- **Authored:** the attribute bodies, which are realistic `google_storage_bucket`
  values. The observed runs necessarily carried `docker_volume` attributes
  (`driver`, `mountpoint`, `driver_opts`), which would be nonsense under a GCS
  type.

One deliberate omission: the `rename_old_live_new_address_import` run carried an
incidental `resource_drift` `update` on `x` caused by a docker readback artifact
(`driver_opts` null↔empty) with no GCS analogue. It is dropped from the fixture.
C1 blocks that scenario on `resource_changes` regardless, so the verdict is
unaffected.

The unmodified observed plans are kept under `raw/` so the transcription can be
audited without rerunning anything.

### Reproducing

Needs OpenTofu 1.12.0, a working docker daemon, and registry access. Creates and
removes two throwaway local docker volumes; touches nothing else.

```bash
cd <scratch dir>
DS_OLD=driftscribe-demo-assets-old DS_NEW=driftscribe-demo-assets-new \
  bash run_docker_fixture.sh      # cases 1-4 (same-address rebind + controls)
DS_OLD=driftscribe-demo-assets-old DS_NEW=driftscribe-demo-assets-new \
  bash run_docker_fixture2.sh     # cases 5-8 (new-address rebind + controls)
bash run_tfcoremock_fixture.sh    # provider-independence cross-check
python3 emit_fixtures.py          # transcribe + re-assert structure
```
