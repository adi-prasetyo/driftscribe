#!/usr/bin/env bash
# Round 2. Round 1 proved the SAME-address rebind is inert: an import block whose
# target already exists in prior state is silently skipped, and the plan degrades
# to a bare ["create"].
#
# This round tests the DIFFERENT-address rebind: retire address x by simply
# dropping it from config (relying on refresh to notice the object is gone), and
# import the renamed object into a NEW address y -- all in one PR, with no
# `removed` block and therefore no forget/delete verb to author.
#
# Case 7 is a positive control proving the import block this script writes does
# fire when the target is absent from prior state.

set -uo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
S="$BASE/docker2"
OUT="$BASE/out"
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

banner() { echo; echo "############ $* ############"; echo; }

docker volume rm -f "$OLD" "$NEW" >/dev/null 2>&1

banner "INIT + STAGE 1 (state binds docker_volume.x -> $OLD)"
tofu init -no-color -input=false >/dev/null 2>&1
cat > main.tf <<EOF
resource "docker_volume" "x" {
  name = "$OLD"
}
EOF
tofu apply -no-color -input=false -auto-approve 2>&1 | tail -3
cp terraform.tfstate base.tfstate

restore() { cp base.tfstate terraform.tfstate; rm -f terraform.tfstate.backup; }
empty_state() { rm -f terraform.tfstate terraform.tfstate.backup; }

simulate_rename() {
  docker volume rm -f "$OLD" "$NEW" >/dev/null 2>&1
  docker volume create "$NEW" >/dev/null
  echo "world: $OLD=GONE, $NEW=present"
}

# new address y, optional import
write_y() {
  cat > main.tf <<EOF
resource "docker_volume" "y" {
  name = "$NEW"
}
EOF
  if [ "${1:-}" = "import" ]; then
    cat >> main.tf <<EOF

import {
  to = docker_volume.y
  id = "$NEW"
}
EOF
  fi
  echo "--- main.tf ---"; cat main.tf; echo "---------------"
}

plan_case() {
  local name="$1"; shift
  tofu plan -no-color -input=false -out="$name.tfplan" "$@" > "$OUT/$name.planout.txt" 2>&1
  local rc=$?
  echo "exit=$rc"
  tail -32 "$OUT/$name.planout.txt"
  [ $rc -eq 0 ] && tofu show -json "$name.tfplan" > "$OUT/$name.json" 2>/dev/null && echo "wrote $OUT/$name.json"
}

# ===== CASE 5 - one-PR different-address rebind (the live question) =====
banner "CASE 5 - state has x->OLD(gone); config declares ONLY y=NEW + import y<-NEW"
restore; simulate_rename
write_y import
plan_case case5_newaddr_import

# ===== CASE 6 - control: same, without the import block =====
banner "CASE 6 - control: same world, config declares ONLY y=NEW, NO import"
restore; simulate_rename
write_y
plan_case case6_newaddr_noimport

# ===== CASE 7 - positive control: import fires when target absent from state =====
banner "CASE 7 - positive control: EMPTY state, config y=NEW + import"
empty_state; simulate_rename
write_y import
plan_case case7_import_positive_control

# ===== CASE 8 - the old object still exists (rename not yet done live) =====
banner "CASE 8 - x->OLD and OLD STILL EXISTS; config declares ONLY y=NEW + import"
restore
docker volume rm -f "$OLD" "$NEW" >/dev/null 2>&1
docker volume create "$OLD" >/dev/null; docker volume create "$NEW" >/dev/null
echo "world: $OLD=present, $NEW=present"
write_y import
plan_case case8_old_still_live

banner "TEARDOWN"
docker volume rm -f "$OLD" "$NEW" >/dev/null 2>&1
echo "fixture volumes remaining: $(docker volume ls --format '{{.Name}}' | grep -c dsfixture)"
