#!/usr/bin/env bash
# Cross-check on hashicorp/tfcoremock -- HashiCorp's provider built specifically
# to exercise Terraform/OpenTofu CORE behavior. Remote objects are JSON files
# under terraform.resource/, so "the object vanished out of band" is `rm`.
#
# Replicates the two decisive docker cases:
#   1' same-address rebind  -> expect import inert, bare create
#   5' new-address rebind   -> expect importing + ["no-op"], old vanishes via drift

set -uo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
S="$BASE/tfcoremock"
OUT="$BASE/out"
AAA="aaaaaaaa-0000-0000-0000-000000000001"
BBB="bbbbbbbb-0000-0000-0000-000000000002"

rm -rf "$S"; mkdir -p "$S" "$OUT"
cd "$S" || exit 1

cat > providers.tf <<'EOF'
terraform {
  required_version = ">= 1.12"
  required_providers {
    tfcoremock = {
      source  = "hashicorp/tfcoremock"
      version = "0.5.0"
    }
  }
}

provider "tfcoremock" {}
EOF

banner() { echo; echo "############ $* ############"; echo; }

res() { # $1 addr, $2 id
  cat > main.tf <<EOF
resource "tfcoremock_simple_resource" "$1" {
  id     = "$2"
  string = "hello"
}
EOF
}
add_import() { # $1 addr, $2 id
  cat >> main.tf <<EOF

import {
  to = tfcoremock_simple_resource.$1
  id = "$2"
}
EOF
}

plan_case() {
  local name="$1"; shift
  tofu plan -no-color -input=false -out="$name.tfplan" "$@" > "$OUT/$name.planout.txt" 2>&1
  local rc=$?
  echo "exit=$rc"; tail -20 "$OUT/$name.planout.txt"
  [ $rc -eq 0 ] && tofu show -json "$name.tfplan" > "$OUT/$name.json" 2>/dev/null && echo "wrote $OUT/$name.json"
}

banner "INIT"
tofu init -no-color -input=false >/dev/null 2>&1 || { tofu init -no-color -input=false; exit 1; }

banner "build object AAA (state binds x -> AAA)"
res x "$AAA"
tofu apply -no-color -input=false -auto-approve 2>&1 | tail -3
cp terraform.tfstate base_x.tfstate

banner "build object BBB on disk (separate state, then discard)"
rm -f terraform.tfstate terraform.tfstate.backup
res y "$BBB"
tofu apply -no-color -input=false -auto-approve 2>&1 | tail -3
rm -f terraform.tfstate terraform.tfstate.backup

echo "--- objects on disk ---"; ls terraform.resource/

banner "simulate rename: AAA vanishes, BBB remains"
cp base_x.tfstate terraform.tfstate
rm -f "terraform.resource/$AAA.json"
ls terraform.resource/

# ---- CASE 1' same-address rebind
banner "CASE 1' - config x=BBB + import to x"
res x "$BBB"; add_import x "$BBB"
plan_case mock_case1_same_addr

# ---- CASE 5' new-address rebind
banner "CASE 5' - config y=BBB + import to y (x dropped from config)"
cp base_x.tfstate terraform.tfstate; rm -f terraform.tfstate.backup
res y "$BBB"; add_import y "$BBB"
plan_case mock_case5_new_addr

banner "DONE"
