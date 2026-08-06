#!/usr/bin/env bash
# DriftScribe second-PC parity transfer (ds-4b5).
#
# Purpose:
#   A `git clone` is not enough to work on this repo. Five payloads that the
#   daily workflow depends on live outside git:
#
#     1. the four driftscribe-* skills   (~/.claude/skills/, user-level)
#     2. the project memory dir          (~/.claude/projects/<slug>/memory/)
#     3. .env                            (gitignored, 22 secrets)
#     4. the beads issue DB              (.beads/embeddeddolt, gitignored)
#     5. the driftscribe MCP servers     (~/.claude.json, project-scoped)
#
#   This script packs them on the source machine and restores them on the
#   target. It does NOT move credentials you can simply re-issue (gcloud ADC,
#   gh, claude login) — `import` prints those as a checklist instead.
#
# Why beads is here at all:
#   .beads/config.yaml deliberately has no sync.remote, because
#   adi-prasetyo/driftscribe is a PUBLIC repo and `bd dolt push` would publish
#   every issue title, body and note to refs/dolt/data. So beads travels in
#   this archive or not at all. Do not "fix" that by adding a remote.
#
# Two archives, split by sensitivity:
#   <stamp>-payload.tar.zst        plain — skills, memory, global CLAUDE.md.
#                                  No credentials; safe on any channel.
#   <stamp>-secrets.tar.zst.gpg    AES256 passphrase — .env, repo .claude/,
#                                  the MCP block (carries an API key), the
#                                  beads export, ~/.claude/settings.json.
#
# Usage:
#   ./scripts/parity.sh export [--out DIR] [--full-beads]
#   ./scripts/parity.sh import <stamp-prefix> [--force]
#   ./scripts/parity.sh verify
#
#   # source machine
#   ./scripts/parity.sh export --out ~/transfer
#   # -> ~/transfer/driftscribe-parity-20260806-1145-payload.tar.zst
#   # -> ~/transfer/driftscribe-parity-20260806-1145-secrets.tar.zst.gpg
#
#   # target machine, from inside a fresh clone
#   ./scripts/parity.sh import ~/transfer/driftscribe-parity-20260806-1145
#   ./scripts/parity.sh verify
#
# Passphrase:
#   gpg prompts interactively. Set PARITY_PASSPHRASE to run unattended.
#   Never pass it on the command line — it lands in your shell history.

set -euo pipefail

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

readonly SELF="$(basename "$0")"

# Staging dir is script-global, not function-local: the EXIT trap fires after
# the function's scope is gone, and a local would be unbound under `set -u`.
STAGE=""
cleanup() { [[ -n "$STAGE" && -d "$STAGE" ]] && rm -rf "$STAGE"; return 0; }
trap cleanup EXIT

log()  { printf '  %s\n' "$*"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*" >&2; }
ok()   { printf '\033[32m  ✓ %s\033[0m\n' "$*"; }
bad()  { printf '\033[31m  ✗ %s\033[0m\n' "$*"; }
die()  { printf '\033[31m%s: %s\033[0m\n' "$SELF" "$*" >&2; exit 1; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

# Claude Code derives a project's state-dir name from its absolute path by
# replacing both '/' and '_' with '-'. /home/adi/adp_app -> -home-adi-adp-app.
# The slug therefore CHANGES if the repo lives at a different path on the
# target machine, which is why it is recomputed on both ends rather than
# carried in the archive.
project_slug() {
  printf '%s' "$1" | tr '/_' '--'
}

# Repo root, resolved from this script rather than $PWD so it works from
# anywhere (and from a worktree).
repo_root() {
  cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd
}

# Fail closed if we are not actually sitting in driftscribe. Restoring this
# payload into the wrong tree would scatter secrets across an unrelated repo.
assert_driftscribe() {
  local root="$1"
  [[ -f "$root/pyproject.toml" ]] || die "not a driftscribe checkout (no pyproject.toml): $root"
  grep -qi '^name *= *"driftscribe"' "$root/pyproject.toml" \
    || die "pyproject.toml is not driftscribe's: $root"
}

# `bd import` mutates, and mutating bd commands commit whatever is staged
# under a message of bd's choosing. CLAUDE.md requires an empty index first.
assert_empty_index() {
  local root="$1"
  if [[ -n "$(git -C "$root" diff --cached --name-only)" ]]; then
    die "git index is not empty — bd would sweep it into a commit. Commit or stash first."
  fi
}

gpg_encrypt() {
  local in="$1" out="$2"
  if [[ -n "${PARITY_PASSPHRASE:-}" ]]; then
    gpg --batch --yes --pinentry-mode loopback --passphrase "$PARITY_PASSPHRASE" \
        --symmetric --cipher-algo AES256 --output "$out" "$in"
  else
    gpg --symmetric --cipher-algo AES256 --output "$out" "$in"
  fi
}

gpg_decrypt() {
  local in="$1" out="$2"
  if [[ -n "${PARITY_PASSPHRASE:-}" ]]; then
    gpg --batch --yes --pinentry-mode loopback --passphrase "$PARITY_PASSPHRASE" \
        --output "$out" --decrypt "$in"
  else
    gpg --output "$out" --decrypt "$in"
  fi
}

# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

do_export() {
  local out_dir="$PWD" full_beads=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --out)         out_dir="${2:?--out needs a directory}"; shift 2 ;;
      --full-beads)  full_beads=1; shift ;;
      *)             die "unknown flag for export: $1" ;;
    esac
  done

  need_cmd tar; need_cmd zstd; need_cmd gpg; need_cmd jq; need_cmd bd

  local root; root="$(repo_root)"
  assert_driftscribe "$root"

  local slug; slug="$(project_slug "$root")"
  local mem_dir="$HOME/.claude/projects/$slug/memory"
  local stamp; stamp="driftscribe-parity-$(date +%Y%m%d-%H%M)"

  mkdir -p "$out_dir"
  STAGE="$(mktemp -d)"; local stage="$STAGE"
  mkdir -p "$stage/payload" "$stage/secrets"

  head_ "Packing from $root"

  # --- plain payload -------------------------------------------------------
  local n_skills=0
  mkdir -p "$stage/payload/skills"
  for s in "$HOME"/.claude/skills/driftscribe-*; do
    [[ -d "$s" ]] || continue
    cp -a "$s" "$stage/payload/skills/"
    n_skills=$((n_skills + 1))
  done
  [[ $n_skills -gt 0 ]] || warn "no driftscribe-* skills found under ~/.claude/skills"
  log "skills           $n_skills"

  local n_mem=0
  if [[ -d "$mem_dir" ]]; then
    cp -a "$mem_dir" "$stage/payload/memory"
    n_mem="$(find "$mem_dir" -type f | wc -l | tr -d ' ')"
  else
    warn "no memory dir at $mem_dir"
  fi
  log "memory files     $n_mem"

  if [[ -f "$HOME/.claude/CLAUDE.md" ]]; then
    cp -a "$HOME/.claude/CLAUDE.md" "$stage/payload/global-CLAUDE.md"
    log "global CLAUDE.md yes"
  fi

  # --- secrets -------------------------------------------------------------
  [[ -f "$root/.env" ]] && cp -a "$root/.env" "$stage/secrets/env" \
    || warn "no .env at repo root"

  # Repo-root .claude/ is gitignored and operator-env.md holds "secrets
  # recipes" per .gitignore — encrypted side, not plain.
  [[ -d "$root/.claude" ]] && cp -a "$root/.claude" "$stage/secrets/repo-claude"

  # settings.json is reference-only on restore; it also carries an internal
  # hostname in the permission allowlist, so it rides encrypted.
  [[ -f "$HOME/.claude/settings.json" ]] \
    && cp -a "$HOME/.claude/settings.json" "$stage/secrets/settings.json"

  # Project-scoped MCP servers. The key is this repo's absolute path; import
  # rewrites it for the target. google-dev-knowledge carries an API key.
  jq --arg p "$root" '.projects[$p].mcpServers // {}' "$HOME/.claude.json" \
    > "$stage/secrets/mcp-driftscribe.json"
  local n_mcp; n_mcp="$(jq 'length' "$stage/secrets/mcp-driftscribe.json")"
  log "mcp servers      $n_mcp"

  # --all keeps infra beads, templates, gates and memories, which plain
  # `bd export` drops. Read-only command; safe with a dirty index.
  #
  # bd resolves its workspace from CWD, not from any flag — so every bd call
  # here runs in a subshell pinned to $root. Without this, running the script
  # from another repo silently reads (or writes) THAT repo's beads DB.
  ( cd "$root" && bd export --all -o "$stage/secrets/beads-all.jsonl" ) >/dev/null 2>&1
  local n_beads; n_beads="$(wc -l < "$stage/secrets/beads-all.jsonl" | tr -d ' ')"
  log "beads records    $n_beads"

  if [[ $full_beads -eq 1 ]]; then
    # Full Dolt history — needed only if you want `dolt_history_issues`
    # recovery on the target. ~14M.
    cp -a "$root/.beads/embeddeddolt" "$stage/secrets/embeddeddolt"
    log "beads dolt dir   included (--full-beads)"
  fi

  # --- manifest ------------------------------------------------------------
  cat > "$stage/payload/MANIFEST.txt" <<EOF
driftscribe parity archive
  created     $(date -Is)
  source host $(hostname)
  source path $root
  source slug $slug
  git HEAD    $(git -C "$root" rev-parse --short HEAD) on $(git -C "$root" rev-parse --abbrev-ref HEAD)
  bd version  $(bd version 2>/dev/null | head -1)
  skills      $n_skills
  memory      $n_mem files
  beads       $n_beads records (--all)
  mcp servers $n_mcp
  full beads  $([[ $full_beads -eq 1 ]] && echo yes || echo no)
EOF
  cp "$stage/payload/MANIFEST.txt" "$stage/secrets/MANIFEST.txt"

  # --- archives ------------------------------------------------------------
  tar -C "$stage/payload" -cf - . | zstd -q -o "$out_dir/$stamp-payload.tar.zst"

  local sec_tmp="$stage/secrets.tar.zst"
  tar -C "$stage/secrets" -cf - . | zstd -q -o "$sec_tmp"
  gpg_encrypt "$sec_tmp" "$out_dir/$stamp-secrets.tar.zst.gpg"
  rm -f "$sec_tmp"

  head_ "Wrote"
  log "$out_dir/$stamp-payload.tar.zst        ($(du -h "$out_dir/$stamp-payload.tar.zst" | cut -f1))"
  log "$out_dir/$stamp-secrets.tar.zst.gpg    ($(du -h "$out_dir/$stamp-secrets.tar.zst.gpg" | cut -f1))"

  head_ "On the target machine"
  log "git clone https://github.com/adi-prasetyo/driftscribe.git && cd driftscribe"
  log "./scripts/parity.sh import <path>/$stamp"
}

# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

# Copy src -> dst, refusing to clobber unless --force.
restore() {
  local src="$1" dst="$2" force="$3" label="$4"
  [[ -e "$src" ]] || { warn "$label: not in archive, skipped"; return 0; }
  if [[ -e "$dst" && $force -ne 1 ]]; then
    warn "$label: $dst exists, left alone (--force to overwrite)"
    return 0
  fi
  mkdir -p "$(dirname "$dst")"
  rm -rf "$dst"
  cp -a "$src" "$dst"
  ok "$label -> $dst"
}

do_import() {
  local prefix="${1:?usage: $SELF import <stamp-prefix> [--force]}"; shift || true
  local force=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --force) force=1; shift ;;
      *)       die "unknown flag for import: $1" ;;
    esac
  done

  need_cmd tar; need_cmd zstd; need_cmd gpg; need_cmd jq; need_cmd bd

  local payload="$prefix-payload.tar.zst"
  local secrets="$prefix-secrets.tar.zst.gpg"
  [[ -f "$payload" ]] || die "missing $payload"
  [[ -f "$secrets" ]] || die "missing $secrets"

  local root; root="$(repo_root)"
  assert_driftscribe "$root"
  assert_empty_index "$root"

  local slug; slug="$(project_slug "$root")"
  STAGE="$(mktemp -d)"; local stage="$STAGE"
  mkdir -p "$stage/payload" "$stage/secrets"

  zstd -dc "$payload" | tar -C "$stage/payload" -xf -

  local sec_tmp="$stage/secrets.tar.zst"
  gpg_decrypt "$secrets" "$sec_tmp"
  zstd -dc "$sec_tmp" | tar -C "$stage/secrets" -xf -
  rm -f "$sec_tmp"

  head_ "Archive"
  sed 's/^/  /' "$stage/payload/MANIFEST.txt"

  head_ "Restoring into $root (slug: $slug)"

  # 1. skills
  if [[ -d "$stage/payload/skills" ]]; then
    for s in "$stage/payload/skills"/*; do
      [[ -d "$s" ]] || continue
      restore "$s" "$HOME/.claude/skills/$(basename "$s")" "$force" "skill $(basename "$s")"
    done
  fi

  # 2. memory — slug recomputed for THIS machine's repo path
  restore "$stage/payload/memory" "$HOME/.claude/projects/$slug/memory" "$force" "memory"

  # 3. .env
  restore "$stage/secrets/env" "$root/.env" "$force" ".env"
  [[ -f "$root/.env" ]] && chmod 600 "$root/.env"

  # 4. repo-root .claude/ (bd prime hook + operator notes)
  restore "$stage/secrets/repo-claude" "$root/.claude" "$force" "repo .claude/"

  # 5. MCP servers — merged under this machine's repo path, never the source's
  if [[ -s "$stage/secrets/mcp-driftscribe.json" ]] \
     && [[ "$(jq 'length' "$stage/secrets/mcp-driftscribe.json")" != "0" ]]; then
    local cfg="$HOME/.claude.json"
    if [[ -f "$cfg" ]]; then
      cp -a "$cfg" "$cfg.bak-$(date +%Y%m%d-%H%M%S)"
      local tmp="$stage/claude.json"
      jq --arg p "$root" --slurpfile m "$stage/secrets/mcp-driftscribe.json" \
        '.projects[$p] = ((.projects[$p] // {}) | .mcpServers = ((.mcpServers // {}) + $m[0]))' \
        "$cfg" > "$tmp"
      # Validate before replacing — a truncated ~/.claude.json is painful.
      jq -e . "$tmp" >/dev/null || die "refusing to write malformed ~/.claude.json"
      mv "$tmp" "$cfg"
      ok "mcp servers merged into ~/.claude.json under $root (backup written)"
    else
      warn "no ~/.claude.json — run claude once, then re-run import"
    fi
  fi

  # 6. beads
  if [[ -d "$stage/secrets/embeddeddolt" ]]; then
    restore "$stage/secrets/embeddeddolt" "$root/.beads/embeddeddolt" "$force" "beads dolt dir"
    chmod 700 "$root/.beads" 2>/dev/null || true
  elif [[ -f "$stage/secrets/beads-all.jsonl" ]]; then
    cp "$stage/secrets/beads-all.jsonl" "$root/.beads/issues.jsonl"
    chmod 700 "$root/.beads" 2>/dev/null || true

    # A fresh clone has config.yaml and metadata.json but no database —
    # config.yaml leaves issue-prefix commented out, so the real prefix comes
    # from metadata.json's dolt_database ("ds"). Without an init, `bd import`
    # dies with "issue_prefix config is missing".
    local prefix; prefix="$(jq -r '.dolt_database // "ds"' "$root/.beads/metadata.json" 2>/dev/null || echo ds)"
    if ! ( cd "$root" && bd list --limit 1 ) >/dev/null 2>&1; then
      warn "beads DB not initialized — running: bd init --prefix $prefix"
      warn "(bd init self-commits to git; the index was verified empty above)"
      if ! ( cd "$root" && bd init --prefix "$prefix" ) >/dev/null 2>&1; then
        bad "bd init failed — run it by hand, then: bd import .beads/issues.jsonl"
      fi
    fi

    # Subshell pinned to $root: bd picks its workspace from CWD, so an
    # unpinned import would write into whatever repo you happened to be
    # standing in — the one bug in this script that could destroy real data.
    # Failure is reported, never swallowed.
    local bd_out
    if bd_out="$( cd "$root" && bd import "$root/.beads/issues.jsonl" 2>&1 )"; then
      ok "beads imported ($(wc -l < "$root/.beads/issues.jsonl" | tr -d ' ') records)"
    else
      bad "beads import FAILED — the issues did not transfer:"
      printf '%s\n' "$bd_out" | sed 's/^/      /' >&2
    fi
  fi

  # 7. reference-only: never silently overwrite the target's own config
  for f in settings.json:settings.json global-CLAUDE.md:CLAUDE.md; do
    local src_name="${f%%:*}" dst_name="${f##*:}"
    local src="$stage/payload/$src_name"
    [[ -f "$src" ]] || src="$stage/secrets/$src_name"
    [[ -f "$src" ]] || continue
    if [[ -e "$HOME/.claude/$dst_name" ]]; then
      cp -a "$src" "$HOME/.claude/$dst_name.from-source"
      warn "$dst_name exists — source copy left at ~/.claude/$dst_name.from-source (merge by hand)"
    else
      cp -a "$src" "$HOME/.claude/$dst_name"
      ok "$dst_name -> ~/.claude/$dst_name"
    fi
  done

  cat <<'EOF'

Still to do by hand — none of this can be copied:

  toolchain   uv · node (nvm) · gh · gcloud SDK · tofu · docker
              bd + dolt (brew) · codex CLI · jq · rg
  deps        make install && (cd frontend && npm ci) && npx playwright install
  auth        gcloud auth login
              gcloud auth application-default login      # ADC, needed for USE_ADK=true
              gcloud config set project driftscribe-hack-2026
              gh auth login                              # adi-prasetyo (+ adi-nifco)
              claude   # then log in
  plugins     /plugin marketplace add anthropics/claude-plugins-official
              /plugin marketplace add obra/superpowers-marketplace
              /plugin marketplace add wshobson/agents
              /plugin marketplace add ykdojo/claude-code-tips
              then copy enabledPlugins from the settings.json reference copy

Then: ./scripts/parity.sh verify
EOF
}

# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

do_verify() {
  local root; root="$(repo_root)"
  assert_driftscribe "$root"
  local slug; slug="$(project_slug "$root")"
  local fails=0

  head_ "Payloads"

  local n; n="$(find "$HOME/.claude/skills" -maxdepth 1 -name 'driftscribe-*' 2>/dev/null | wc -l | tr -d ' ')"
  if [[ "$n" -eq 4 ]]; then ok "skills: 4"; else bad "skills: $n (expected 4)"; fails=$((fails+1)); fi

  local mem="$HOME/.claude/projects/$slug/memory"
  if [[ -f "$mem/MEMORY.md" ]]; then
    ok "memory: $(find "$mem" -type f | wc -l | tr -d ' ') files at $mem"
  else
    bad "memory: no MEMORY.md at $mem"; fails=$((fails+1))
  fi

  if [[ -f "$root/.env" ]]; then
    ok "env: $(grep -cvE '^\s*(#|$)' "$root/.env") keys"
  else
    bad "env: missing"; fails=$((fails+1))
  fi

  if command -v bd >/dev/null 2>&1; then
    local open; open="$( ( cd "$root" && bd list --status=open --json ) 2>/dev/null | jq 'length' 2>/dev/null || echo '?')"
    if [[ "$open" != "?" && "$open" != "0" ]]; then ok "beads: $open open"
    else bad "beads: no open issues found"; fails=$((fails+1)); fi
  else
    bad "beads: bd not installed"; fails=$((fails+1))
  fi

  if [[ -f "$HOME/.claude.json" ]]; then
    local mcp; mcp="$(jq -r --arg p "$root" '.projects[$p].mcpServers // {} | keys | join(", ")' "$HOME/.claude.json")"
    if [[ -n "$mcp" ]]; then ok "mcp: $mcp"; else bad "mcp: none registered for $root"; fails=$((fails+1)); fi
  fi

  head_ "Toolchain"
  for c in uv node npm gh gcloud tofu docker bd dolt codex jq rg; do
    if command -v "$c" >/dev/null 2>&1; then ok "$c"; else bad "$c"; fails=$((fails+1)); fi
  done

  head_ "Auth"
  if [[ -f "$HOME/.config/gcloud/application_default_credentials.json" ]]; then
    ok "gcloud ADC present"
  else
    bad "gcloud ADC missing — gcloud auth application-default login"; fails=$((fails+1))
  fi
  local proj; proj="$(gcloud config get-value project 2>/dev/null || true)"
  if [[ "$proj" == "driftscribe-hack-2026" ]]; then ok "gcloud project: $proj"
  else bad "gcloud project: ${proj:-unset} (expected driftscribe-hack-2026)"; fails=$((fails+1)); fi
  if gh auth status >/dev/null 2>&1; then ok "gh authenticated"
  else bad "gh not authenticated"; fails=$((fails+1)); fi

  head_ "Guard"
  if grep -q '^\s*sync:' "$root/.beads/config.yaml" 2>/dev/null; then
    bad "a sync.remote appeared in .beads/config.yaml — this repo is PUBLIC, remove it"
    fails=$((fails+1))
  else
    ok "no beads sync.remote (correct — repo is public)"
  fi

  head_ "Result"
  if [[ $fails -eq 0 ]]; then
    ok "parity looks complete — now run: make test && make ui-smoke"
  else
    bad "$fails check(s) failed"
    return 1
  fi
}

# ---------------------------------------------------------------------------

case "${1:-}" in
  export) shift; do_export "$@" ;;
  import) shift; do_import "$@" ;;
  verify) shift; do_verify "$@" ;;
  *)
    sed -n '2,48p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
