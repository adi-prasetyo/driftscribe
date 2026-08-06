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

## ⚠ `bd` commits your git index — stage nothing before running it

Mutating `bd` commands (`bd create`, `bd close`, `bd update`, `bd dolt remote …`) commit
**everything currently staged**, not just beads files, under a message of bd's choosing
(e.g. `bd: clear sync.remote`). Observed twice on 2026-07-28 during setup.

**Before any `bd` command, `git status` must show an empty index.** Commit or stash first.
Read-only commands — `bd ready`, `bd list`, `bd show`, `bd prime` — are safe.

This matters more here than elsewhere: **multiple agents share the `/home/adi/driftscribe`
main worktree**, so a sweep can capture work that isn't yours. Side worktrees live under
`.worktrees/` and `.claude/worktrees/` and have their own indexes.

## Local conventions that override the managed block below

- **`~/.claude/projects/-home-adi/memory/` remains authoritative** for persistent
  cross-project and user-level knowledge. Do **not** replace it with `bd remember`,
  regardless of what a regenerated Beads block says.
- **Beads syncs to a SEPARATE PRIVATE repo**, never to this one. Since 2026-08-06,
  `sync.remote` = `adi-prasetyo/driftscribe-beads` (private), so `bd dolt push` /
  `bd dolt pull` are safe and are how the second machine stays in sync.
  `driftscribe` itself is **public**, and `bd dolt push` writes the entire issue
  database — every title, body and note — to `refs/dolt/data`. So: never point
  `sync.remote` at this repo's origin, and verify any new target is private
  (`gh repo view <target> --json isPrivate`) before changing it.
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
