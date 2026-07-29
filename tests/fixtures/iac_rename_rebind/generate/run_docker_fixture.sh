#!/usr/bin/env bash
# Rename-rebind plan fixture, docker_volume edition.
#
# Question under test: when OpenTofu state binds address X to remote object OLD,
# OLD no longer exists live, NEW does exist, and the config rewrites X's identity
# attribute to NEW *plus* adds `import { to = X, id = NEW }` -- what plan shape
# results?  Success shape = exactly one entry with `importing` set and
# actions == ["no-op"], which is the shape DriftScribe's C1 denylist already admits.
#
# docker_volume is used because it structurally matches google_storage_bucket:
#   - `name` is the physical identity and forces replacement when changed
#   - the remote object genuinely exists and can be deleted out of band
#   - import is by bare name (same shape as google_storage_bucket's import id)
#
# Nothing here touches GCP, the iac/ working dir, or any real project state.

set -uo pipefail

S="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/docker"
OUT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/out"
OLD="${DS_OLD:-dsfixture-old}"
NEW="${DS_NEW:-dsfixture-new}"

rm -rf "$S"; mkdir -p "$S" "$OUT"
cd "$S" || exit 1

cat > providers.tf <<'EOF'
terraform {
  required_version = ">= 1.12"
  required_providers {
    docker = {
      source = "kreuzwerker/docker"
    }
  }
}

provider "docker" {}
EOF

write_config() {
  # $1 = volume name, $2 = "import" | "noimport", $3 = import id (optional)
  cat > main.tf <<EOF
resource "docker_volume" "x" {
  name = "$1"
}
EOF
  if [ "$2" = "import" ]; then
    cat >> main.tf <<EOF

import {
  to = docker_volume.x
  id = "$3"
}
EOF
  fi
}

banner() { echo; echo "############ $* ############"; echo; }

# ---------------------------------------------------------------- clean slate
docker volume rm -f "$OLD" "$NEW" >/dev/null 2>&1

banner "INIT"
tofu init -no-color -input=false >/dev/null 2>&1 || { echo "INIT FAILED"; tofu init -no-color -input=false; exit 1; }
tofu version -no-color | head -3

# ------------------------------------------------- stage 1: build a real state
banner "STAGE 1 - apply so state binds docker_volume.x -> $OLD"
write_config "$OLD" noimport
tofu apply -no-color -input=false -auto-approve 2>&1 | tail -5
cp terraform.tfstate base.tfstate
echo "--- state binding ---"
tofu state show docker_volume.x 2>&1 | sed -n '1,12p'

restore() {
  cp base.tfstate terraform.tfstate
  rm -f terraform.tfstate.backup
}

# ------------------------- simulate the out-of-band rename: OLD gone, NEW there
simulate_rename() {
  docker volume rm -f "$OLD" >/dev/null 2>&1
  docker volume rm -f "$NEW" >/dev/null 2>&1
  docker volume create "$NEW" >/dev/null
  echo "world: $OLD=$(docker volume inspect "$OLD" >/dev/null 2>&1 && echo present || echo GONE), $NEW=$(docker volume inspect "$NEW" >/dev/null 2>&1 && echo present || echo GONE)"
}

plan_case() {
  # $1 = case name, rest = extra tofu plan flags
  local name="$1"; shift
  echo "--- tofu plan ($name) ---"
  tofu plan -no-color -input=false -out="$name.tfplan" "$@" > "$OUT/$name.planout.txt" 2>&1
  local rc=$?
  echo "exit=$rc"
  tail -30 "$OUT/$name.planout.txt"
  if [ $rc -eq 0 ]; then
    tofu show -json "$name.tfplan" > "$OUT/$name.json" 2>/dev/null
    echo "wrote $OUT/$name.json"
  else
    echo "NO PLAN FILE (planning failed)"
  fi
}

# ============================================================== CASE 1 (the ask)
banner "CASE 1 - rebind: state->OLD(gone), live NEW exists, config->NEW + import block"
restore; simulate_rename
write_config "$NEW" import "$NEW"
plan_case case1_rebind_import

# ====================================== CASE 2 - control: naive rename, no import
banner "CASE 2 - control: same world, config->NEW, NO import block"
restore; simulate_rename
write_config "$NEW" noimport
plan_case case2_naive_rename

# ============ CASE 3 - control: import block on an address already in state, live
banner "CASE 3 - control: OLD still exists, config->OLD + import block targeting OLD"
restore
docker volume rm -f "$OLD" "$NEW" >/dev/null 2>&1
docker volume create "$OLD" >/dev/null
echo "world: $OLD=present"
write_config "$OLD" import "$OLD"
plan_case case3_import_already_managed

# =================== CASE 4 - diagnostic: same as case 1 but refresh disabled
banner "CASE 4 - diagnostic: case 1 with -refresh=false (isolates refresh-removal)"
restore; simulate_rename
write_config "$NEW" import "$NEW"
plan_case case4_rebind_norefresh -refresh=false

# ------------------------------------------------------------------- teardown
banner "TEARDOWN"
docker volume rm -f "$OLD" "$NEW" >/dev/null 2>&1
echo "removed fixture volumes"
docker volume ls --format '{{.Name}}' | grep -c dsfixture || echo "0 dsfixture volumes remain"
