# Repo audit — 2026-07-07

A four-area audit of DriftScribe run ahead of the hackathon judging window (public demo open, judges 7/21-24, finals 8/19). Each area is a separate file. Findings cite `file:line`; the load-bearing ones were spot-checked against the source (noted inline). Nothing was changed by the audit — these are read-only findings for a follow-up reviewer to triage.

| Area | File | Headline |
|---|---|---|
| Security | [`2026-07-07-security.md`](2026-07-07-security.md) | **1 CRITICAL** — anonymous `/chat` can self-approve a live Cloud Run rollback (HITL bypass; verified). The `tofu apply` gate IS safe. Also 2 HIGH (PR self-merge, LLM cost), 4 MEDIUM, 4 LOW. |
| Backend | [`2026-07-07-backend.md`](2026-07-07-backend.md) | 1 HIGH (blocking sync JWKS fetch in `async /eventarc` stalls the event loop), 2 MEDIUM (unbounded `_TRACE_CACHE`; unhandled GitHub Advisory call), 1 LOW. |
| Frontend | [`2026-07-07-frontend.md`](2026-07-07-frontend.md) | XSS surface clean; 2 MEDIUM (no SSE stream cancellation; `AuthPanel` reimplements an inaccessible modal), 1 LOW (tour focus). |
| Tests & CI | [`2026-07-07-tests-ci.md`](2026-07-07-tests-ci.md) | Suites green (3292 py / 979 fe / 47 worker), but only `lint-test` is a merge-required check — `frontend`/`worker`/`ui-smoke` run on every PR without gating merge. Other gaps: e2e is dispatch-only; 5 untested Svelte components; bootstrap scripts. |

## Cross-cutting takeaways
- **ACT FIRST: security C1 is a live anonymous HITL bypass during the open demo window.** An anonymous `/chat` user can drive the drift crew to mint a rollback approval, read the single-use token from the reply, and self-approve a live Cloud Run traffic shift on `payment-demo`. Verified link-by-link in code. Blast radius is the demo service (not prod/coordinator), but the broken property is critical-class. Fix reuses infra that already exists (scrub approval tokens from the demo-anonymous `/chat`/SSE/`/conversations` surfaces, exactly as `/trace` already does). Interim mitigation: dial autonomy below `propose_apply` while the window is open.
- **The `tofu apply` gate genuinely holds.** approve → merge PR → `tofu apply` is protected by three independent layers (allowlist exclusion, mandatory CF-Access JWT with no token fallback, artifact-pinned CSRF), confirmed in code. C1 is the *older rollback* flow, which was never given the same treatment.
- **Cost is the other standing exposure.** Anonymous `POST /chat` drives real Gemini spend behind a per-IP, fail-open limiter — make sure a hard GCP/Vertex budget cap is the real backstop (security H2).
- **Cheapest correctness win:** backend #1 (`asyncio.to_thread` the `/eventarc` token verify) — one line, removes an event-loop stall on the live autonomous path.
- **Cheapest CI win:** add `frontend`, `worker`, and `ui-smoke` as required status checks — branch protection currently requires only `lint-test` (+ GitGuardian), so a red frontend/ui-smoke run doesn't block merge (tests-ci gap #1; a one-minute settings change).
- **The codebase is mature and heavily Codex-reviewed.** All auditors independently noted strong, postmortem-driven engineering (the `/trace` two-phase timeout, Firestore CAS transactions, worker isolation, artifact-pinned CSRF, href hygiene, layered prompt pinning). The findings are what slipped through a generally high bar — with C1 the notable exception, a whole flow that predates the anonymous-demo threat model and never got retrofitted.

## Also produced this session (agent tooling, outside the repo)
Not committed here (they live under `~/.claude/skills/`), but part of the same task:
- **`CLAUDE.md`** (repo root, committed) — the repo had no orientation doc; added architecture map, invariants, commands, conventions.
- **`driftscribe-crews`** skill — editing crew prompts/tools (byte-golden pins, ADK-sends-docstrings, tool-name grounding).
- **`driftscribe-demo-ops`** skill — operating the public demo window (`DEMO_ALLOWLIST`, `DEMO_MODE`, edge-gate toggle, demo-reset cron); closes the recurring "new route 401s through the demo" bug class.
