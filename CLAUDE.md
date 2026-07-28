# DriftScribe — agent instructions

An AI DevOps agent that watches a Google Cloud estate and **proposes** fixes but
never applies a risky change on its own.

**Start with [`docs/OVERVIEW.md`](docs/OVERVIEW.md).** Four dedicated skills exist —
invoke them when the work touches their area:

| Skill | Use for |
|---|---|
| `driftscribe-crews` | Workload system prompts in `workloads/*/`, crew tools, operator-facing crew copy |
| `driftscribe-deploy` | Shipping the coordinator (`driftscribe-agent` Cloud Run service) to prod |
| `driftscribe-demo-ops` | The public demo / judging window, Cloudflare Worker proxy, `DEMO_MODE` |
| `driftscribe-live-probe` | Reproducing or verifying behavior on PROD (the `/chat` SSE stream, deployed SPA) |

## Local conventions that override the managed block below

- **`~/.claude/projects/-home-adi/memory/` remains authoritative** for persistent
  cross-project and user-level knowledge. Do **not** replace it with `bd remember`,
  regardless of what a regenerated Beads block says.
- **Beads here is local-only.** No `sync.remote` is configured and this repo is
  public — do not run `bd dolt push` or add a Dolt remote without asking first.
- The Beads block below is managed by `bd` and is regenerated on upgrade. If a
  `bd remember` / "do NOT use MEMORY.md" line reappears inside it, it is stock
  boilerplate — this section wins.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:6cd5cc61 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->
