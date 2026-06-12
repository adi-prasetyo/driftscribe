# Infra-reader: lift container concurrency 1 → 8 (deploy-churn papercut)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stop the infra-reader worker from head-of-line blocking under deploy-churn describe traffic, by raising `--concurrency` from 1 to 8 — codified in both deploy files (with a new parity pin test), then applied live as a flags-only revision.

**Architecture:** Config-only change. The worker's single substantive endpoint is stateless per request, so cross-request serialization buys nothing and costs everything: with `--max-instances=1 --concurrency=1` every CAI search (seconds each; a cold `/infra/graph` was measured ≈25s end-to-end) blocks all other describe traffic — the SPA graph panel's polling, chat `read_project_inventory` calls, and adoption flows all queue behind one slot. Raise the slot count; change nothing else.

**Tech Stack:** Cloud Run flags in `infra/cloudbuild.infra-reader.yaml` + `infra/cloudbuild.yaml`, pytest YAML parity pin, `gcloud run services update` for the live cutover.

---

## Background (the papercut)

Recorded during item-9 Phase 4 live work (2026-06-11): "concurrency-1 infra-reader degrades under deploy-churn describe traffic." During deploys, the operator UI's Infrastructure panel keeps polling `/infra/graph` (RefreshScheduler: refresh-on-change + light polling while open) while chat work also calls `read_project_inventory` — every one of those is a `worker_client.call("infra_reader", {})` → `POST /describe`, and the worker serializes them globally.

## Grounding facts

1. `infra/cloudbuild.infra-reader.yaml:100-102` (canonical targeted deploy) — `--min-instances=0 --max-instances=1 --concurrency=1`. `infra/cloudbuild.yaml:688-690` (full-stack mirror) — identical flags. The targeted file's header says it "mirrors the infra-reader deploy step in infra/cloudbuild.yaml" — a claim no test enforces today.
2. `workers/infra_reader/main.py` — ONE substantive endpoint, `POST /describe` (`:114`), a **sync** `def` handler (Starlette runs it in the threadpool, so in-process concurrency is real once the Cloud Run cap allows it). Per request it: parses the baked `iac/` dir (`_load_declared`, read-only), constructs a **fresh `AssetServiceClient`** (`:128`), pages `search_all_resources` with a 3-field read mask, and builds the inventory. **No module-level mutable state** — only boot-time constants. Nothing about the handler requires serialization.
3. The estate is ~467 CAI resources (item-14 live note) at 3 masked fields each — per-request memory is KBs; 512Mi holds 8 concurrent requests trivially. The work is I/O-bound paging, so 1 vCPU is not a bottleneck at 8 slots.
4. Coordinator call sites — all funnel through `worker_client.call("infra_reader", {})` (endpoint map `worker_client.py:98`): the `read_project_inventory` chat tool (`adk_tools.py:78`; explore + provision, incl. adoption flows) and the `/infra/graph` proxy (`main.py:1925`; SPA panel + RefreshScheduler).
5. Contrast with workers that keep `--concurrency=1` deliberately: tofu-apply (sole live mutator, claim-first state-lock discipline), tofu-editor / docs / rollback / upgrade-docs (GitHub mutators), reader (out of scope — same shape as infra-reader but no observed degradation; not named by the papercut). The coordinator runs `--concurrency=2` with a documented Phase-22 rationale (`infra/cloudbuild.yaml:269-276`) — precedent that concurrency values here are per-service reasoned, not uniform.
6. No test pins any worker's deploy flags today (checked: only iac-workflow concurrency tests exist).
7. CAI `SearchAllResources` quota is per-minute and far above 8 concurrent pages; the read mask keeps responses minimal.
8. Live service state: `driftscribe-infra-reader` rev `00014-qnl`, `containerConcurrency: 1`, maxScale 1, 1 vCPU / 512Mi. A flags-only `gcloud run services update --concurrency=8` creates a config-only revision from the SAME image — no rebuild, no code deploy.

## Design decisions

1. **`--concurrency=8`, nothing else.** Keep `--max-instances=1` (the problem is head-of-line blocking, not throughput; a second instance would double cold-start surface and CAI traffic for no observed need), `--min-instances=0`, memory/CPU unchanged. 8 = comfortably above the realistic concurrent caller count (graph poll + a chat call + a deploy-churn burst) while keeping the single 1-vCPU instance honest for thread-pooled sync handlers.
2. **Change BOTH deploy files** and add the missing **parity pin test** (design per Codex 019eb9ca must-fix 1+2 — no blanket exclusions): parse both YAMLs, locate the infra-reader `run deploy` step in each, split args into a `--flag → value` mapping plus a parsed `--set-env-vars` key/value dict, **normalize `${NAME}` references through each file's own `substitutions` defaults** (so `--region=${_REGION}` in the targeted file compares equal to the full file's literal `asia-northeast1`; both files default `_TAG: manual` so `--image` compares equal too). Assert: (a) the flag mappings are EQUAL — including `--image` and `--region`; (b) env-var KEY sets are equal and every value is equal EXCEPT `IAC_SNAPSHOT_SHA` (legitimately `${_IAC_SNAPSHOT_SHA}` vs the `$COMMIT_SHA` builtin, which substitutions can't normalize); (c) `--concurrency` is `8` in both. This turns the header's "mirrors" claim into an enforced invariant for the auth-critical env config (`GCP_PROJECT`/`OWN_URL`/`ALLOWED_CALLERS`) as well as the flags, and pins the new value.
3. **Comment at both flag sites** (short — per Codex nit):
   ```
   # concurrency=8: /describe is a stateless read-only CAI search — no
   # cross-request state, so serialization only caused head-of-line
   # blocking under deploy-churn describe traffic. Keep MUTATOR workers
   # at concurrency=1.
   ```
4. **Other workers untouched** (fact 5). The reader worker's possible same-shape lift is explicitly out of scope — no observed degradation, and per-service reasoning is the doctrine.
5. **Live apply = flags-only update after merge** (`gcloud run services update driftscribe-infra-reader --region=asia-northeast1 --project=driftscribe-hack-2026 --concurrency=8`), so live state matches the codified files and the next full deploy preserves it. No image rebuild, no coordinator rebake (no code or prompt changed anywhere).
6. **Live verify (Codex must-fix 3 — 200 alone proves nothing):** `/infra/graph` soft-fails to a degraded 200 (`degraded: true`, `error: infra_reader_unavailable`) on `WorkerClientError`, and `worker_client` has a 30s timeout — a burst that times out would still look "all 200". So: (a) `gcloud run services describe` pre/post diff confirms ONLY `containerConcurrency` changed (1 → 8) on the new serving revision, same image, same env; (b) single authenticated `/infra/graph` fetch returns 200 with `degraded` falsy and plausible totals; (c) 4 parallel authenticated `/infra/graph` fetches: ALL four have `degraded` falsy + real totals, wall time well under 4× the single fetch (qualitative overlap check).

## Out of scope

- Any other worker's scaling flags (incl. reader). Coordinator concurrency. Caching/dedup of describe calls in the coordinator (a different, larger fix; not needed once blocking is gone). KMS copy (backlog 4).

## Tasks

### Task 1: parity pin test (fails on current files)

**Files:** Create `tests/unit/test_worker_deploy_flags.py`.

```python
"""Deploy-flag parity pins for the infra-reader worker.

infra/cloudbuild.infra-reader.yaml's header claims it "mirrors the
infra-reader deploy step in infra/cloudbuild.yaml" — these pins enforce
the claim, and pin the deploy-churn fix (concurrency=8, 2026-06-12 plan).

Comparison semantics (Codex 019eb9ca): flags are compared as a mapping
after normalizing ``${NAME}`` through each file's own ``substitutions``
defaults (so ``--region=${_REGION}`` equals the literal region, and
``--image=...:${_TAG}`` compares equal since both files default ``_TAG``).
``--set-env-vars`` is parsed into key/value pairs: key sets must match
and every VALUE must match except ``IAC_SNAPSHOT_SHA``, which is
legitimately ``${_IAC_SNAPSHOT_SHA}`` vs the ``$COMMIT_SHA`` builtin —
that one key's value is the only thing this pin does not see.
"""
import re
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _infra_reader_deploy(cloudbuild_path: Path) -> tuple[dict, dict]:
    """Return (flags, env_vars) for the infra-reader gcloud run deploy step."""
    doc = yaml.safe_load(cloudbuild_path.read_text(encoding="utf-8"))
    subs = doc.get("substitutions") or {}

    def _normalize(value: str) -> str:
        return re.sub(
            r"\$\{(\w+)\}", lambda m: subs.get(m.group(1), m.group(0)), value
        )

    for step in doc["steps"]:
        args = step.get("args") or []
        if not (
            step.get("entrypoint") == "gcloud"
            and "deploy" in args
            and "driftscribe-infra-reader" in args
        ):
            continue
        flags: dict[str, str] = {}
        env: dict[str, str] = {}
        for a in args:
            if not isinstance(a, str) or not a.startswith("--"):
                continue
            key, _, value = a.partition("=")
            value = _normalize(value)
            if key == "--set-env-vars":
                env = dict(kv.split("=", 1) for kv in value.split(","))
            else:
                flags[key] = value
        return flags, env
    raise AssertionError(f"no infra-reader deploy step found in {cloudbuild_path}")


def test_infra_reader_deploy_flags_match_between_files():
    t_flags, t_env = _infra_reader_deploy(
        _REPO_ROOT / "infra" / "cloudbuild.infra-reader.yaml"
    )
    f_flags, f_env = _infra_reader_deploy(_REPO_ROOT / "infra" / "cloudbuild.yaml")
    assert t_flags == f_flags
    assert set(t_env) == set(f_env)
    for key in t_env:
        if key == "IAC_SNAPSHOT_SHA":
            continue  # ${_IAC_SNAPSHOT_SHA} vs $COMMIT_SHA builtin — see module doc
        assert t_env[key] == f_env[key], key


def test_infra_reader_concurrency_is_8():
    for fname in ("cloudbuild.infra-reader.yaml", "cloudbuild.yaml"):
        flags, _ = _infra_reader_deploy(_REPO_ROOT / "infra" / fname)
        assert flags.get("--concurrency") == "8", fname
```

Steps: write the test → run `.venv/bin/pytest tests/unit/test_worker_deploy_flags.py -q` → expect **the concurrency test to FAIL** (`--concurrency=1` today) and **the parity test to PASS on current files** — confirm this BEFORE the flag edit (if parity fails, the two files have drifted somewhere real, or the normalization is wrong; STOP and report rather than "fixing" silently).

### Task 2: flip the flag in both files

1. `infra/cloudbuild.infra-reader.yaml:102`: `--concurrency=1` → `--concurrency=8`, insert the decision-3 comment above it.
2. `infra/cloudbuild.yaml:690`: same edit, same comment.
3. Run the pin tests — PASS. Full suite `.venv/bin/pytest -q` + `.venv/bin/ruff check --no-cache .`
4. Check `docs/runbooks/infra-reader.md`: if it states the scaling flags, update the value; if it doesn't, leave it.
5. Commit: `feat(infra-reader): lift container concurrency 1→8 — stop deploy-churn head-of-line blocking`

## Ship steps

1. Branch `fix/infra-reader-concurrency`, PR, CI watch, Codex completed-work review (same thread), squash-merge.
2. Live apply (flags-only, no build): `gcloud run services update driftscribe-infra-reader --region=asia-northeast1 --project=driftscribe-hack-2026 --concurrency=8` → confirm new revision serving with `containerConcurrency: 8`.
3. Live verify per decision 6 (config + single fetch + 4-parallel burst).
4. Memory (new infra-reader revision pointer) + closing report.
