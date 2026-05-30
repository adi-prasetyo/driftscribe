#!/usr/bin/env bash
# OPERATOR-RUN: creates live GCP networking/IAM in driftscribe-hack-2026. Review before running. Do NOT run in CI.
#
# Phase C5c — give the coordinator (driftscribe-agent) Direct VPC egress so it
# can reach its internal-ingress Cloud Run workers (notably the tofu-apply
# worker, ingress=internal). A valid OIDC token does NOT help: internal ingress
# is a NETWORK control, so the only missing layer is networking.
#
# The design (parent plan §3.2 + this phase's plan §2): the coordinator gets
# Direct VPC egress with --vpc-egress=private-ranges-only; the worker stays
# ingress=internal. Because *.run.app resolves PUBLICLY, private-ranges-only
# alone routes those names AROUND the VPC and internal ingress refuses them. The
# fix is a Cloud DNS PRIVATE zone for run.app. attached to the VPC that
# redirects run.app / *.run.app to the private.googleapis.com VIP 199.36.153.8/30,
# which private-ranges-only DOES route through the VPC. Public destinations still
# egress directly (preserving Vertex / GitHub / Cloudflare / notifier egress with
# NO Cloud NAT).
#
# What it creates (all idempotent — safe to re-run; FAIL-CLOSED on collision):
#   1. APIs        compute.googleapis.com + dns.googleapis.com
#   2. VPC         driftscribe-vpc (--subnet-mode=custom)
#   3. Subnet      driftscribe-coord-an1 (asia-northeast1, 10.8.0.0/24, PGA on)
#   4. DNS zone    run-app (PRIVATE, run.app. → VIP A + *.run.app. → CNAME)
#   5. Routes      verify 0.0.0.0/0 → default-internet-gateway; add 199.36.153.8/30
#                  → default-internet-gateway only if not already covered
#   6. IAM         roles/compute.networkUser to the Cloud Run service agent on the
#                  subnet (Direct VPC egress needs the SERVICE agent to use the
#                  subnet); optional extra deploy principal via NETWORK_USER_MEMBER
#
# This script is DELIBERATELY out-of-band (NOT in iac/): a VPC create would trip
# C4's fidelity guard and break the zero-diff import. It does NOT touch the
# running coordinator — the staged --no-traffic redeploy that actually attaches
# the VPC is printed at the end, never executed here.
#
# Usage:
#   infra/scripts/setup_coordinator_vpc.sh
#   DRY_RUN=1 infra/scripts/setup_coordinator_vpc.sh      # print every gcloud, run nothing
#   PROJECT=driftscribe-hack-2026 REGION=asia-northeast1 \
#     infra/scripts/setup_coordinator_vpc.sh
#
# All knobs default to the real prod values; override via env var to dry-run
# against a throwaway project. NEVER run this against a project you don't own.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=infra/scripts/_setup_lib.sh
source "${SCRIPT_DIR}/_setup_lib.sh"

# --------------------------------------------------------------------------
# Parameters — defaults are the real prod values; override via env to test
# against a throwaway project.
# --------------------------------------------------------------------------
PROJECT="${PROJECT:-driftscribe-hack-2026}"
REGION="${REGION:-asia-northeast1}"
PROJECT_NUMBER="${PROJECT_NUMBER:-1079423440495}"

NETWORK="${NETWORK:-driftscribe-vpc}"
SUBNET="${SUBNET:-driftscribe-coord-an1}"
SUBNET_RANGE="${SUBNET_RANGE:-10.8.0.0/24}"

# Cloud DNS PRIVATE zone redirecting run.app to the private.googleapis.com VIP.
DNS_ZONE="${DNS_ZONE:-run-app}"
DNS_NAME="run.app."
# private.googleapis.com VIP block (199.36.153.8/30 = .8 .9 .10 .11). We use
# `private` (.8/30), NOT `restricted` (.4/30) — we are not in a VPC-SC perimeter.
VIP_IPS=(199.36.153.8 199.36.153.9 199.36.153.10 199.36.153.11)
VIP_RANGE="199.36.153.8/30"

# The Cloud Run service agent — Direct VPC egress needs THIS identity to use the
# subnet. Normally covered by roles/run.serviceAgent, but we grant networkUser
# on the subnet explicitly to be safe.
RUN_SERVICE_AGENT="service-${PROJECT_NUMBER}@serverless-robot-prod.iam.gserviceaccount.com"
# Optional extra principal (e.g. a non-owner deploy SA) to also grant
# networkUser on the subnet. Empty = grant only the Cloud Run service agent.
NETWORK_USER_MEMBER="${NETWORK_USER_MEMBER:-}"

# The tofu-apply worker URL the staged redeploy wires into TOFU_APPLY_URL, and
# the coordinator service the redeploy/smoke target. Printed, never executed.
TOFU_APPLY_URL="${TOFU_APPLY_URL:-https://driftscribe-tofu-apply-u272wv52kq-an.a.run.app}"
COORDINATOR_SERVICE="${COORDINATOR_SERVICE:-driftscribe-agent}"
COORDINATOR_BASE_URL="${COORDINATOR_BASE_URL:-https://driftscribe-agent-u272wv52kq-an.a.run.app}"
# The --tag c5c gives the no-traffic revision a stable callable URL (a plain
# 0%-traffic revision has none): https://c5c---<service>-<hash>-an.a.run.app
COORDINATOR_TAGGED_URL="${COORDINATOR_TAGGED_URL:-https://c5c---driftscribe-agent-u272wv52kq-an.a.run.app}"

# --------------------------------------------------------------------------
# Validate the identifiers we interpolate into gcloud --filter / grep regexes
# (NETWORK, SUBNET) against the GCP resource-name grammar. This both catches
# typos early and neutralizes any regex/DSL metacharacter injection from an
# env-var override (the filters below embed ${NETWORK} unquoted in a regex).
# --------------------------------------------------------------------------
for _name_var in NETWORK SUBNET DNS_ZONE; do
  _val="${!_name_var}"
  if [[ ! "$_val" =~ ^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$ ]]; then
    echo "ERROR: ${_name_var}='${_val}' is not a valid GCP resource name" >&2
    echo "       (lowercase letter start; lowercase/digits/hyphens; ≤63)." >&2
    exit 1
  fi
done

# --------------------------------------------------------------------------
# DRY_RUN gate — when DRY_RUN=1, every mutating gcloud is PRINTED, not run.
# Read-only describe gates always run (they have no side effects and we need
# their output to decide idempotency / fail-closed). `run_cmd` wraps mutations;
# describe-then-act gates call gcloud describe directly.
# --------------------------------------------------------------------------
DRY_RUN="${DRY_RUN:-}"
run_cmd() {
  if [ -n "$DRY_RUN" ]; then
    printf '  [DRY_RUN] '
    printf '%q ' "$@"
    printf '\n'
  else
    "$@"
  fi
}

# Normalize a whitespace-separated IP list → sorted, single-spaced, no trailing
# space (order-insensitive set comparison).
norm_ips() { printf '%s' "$1" | tr ' ' '\n' | sed '/^$/d' | sort | paste -sd' ' -; }

# Idempotently ensure the run.app. zone holds EXACTLY our two records:
#   run.app.   A     300  <VIP_IPS>          (apex A → private.googleapis.com VIP)
#   *.run.app. CNAME 300  run.app.           (wildcard CNAME — Google's pattern)
# Recoverable + fail-closed (BLOCKER #2 fix): match→skip, both-absent→add
# atomically, partial/mismatch→abort (never overwrite a zone we don't fully own).
# Called for BOTH a freshly-created zone AND a pre-existing-and-verified-ours
# zone, so a run interrupted between zone-create and record-add self-heals.
ensure_dns_records() {
  local existing_a expected_a existing_cname tx_file
  expected_a="$(printf '%s ' "${VIP_IPS[@]}")"
  existing_a="$(gcloud dns record-sets list --project="$PROJECT" --zone="$DNS_ZONE" \
    --name="$DNS_NAME" --type=A --format='value(rrdatas[])' 2>/dev/null \
    | tr '\n;' '  ' || echo "")"
  existing_cname="$(gcloud dns record-sets list --project="$PROJECT" --zone="$DNS_ZONE" \
    --name="*.${DNS_NAME}" --type=CNAME --format='value(rrdatas[0])' 2>/dev/null || echo "")"

  if [ "$(norm_ips "$existing_a")" = "$(norm_ips "$expected_a")" ] \
     && [ "$existing_cname" = "$DNS_NAME" ]; then
    echo "  DNS records already present and match (run.app. A + *.run.app. CNAME) — not mutating"
    return 0
  fi
  if [ -n "$existing_a" ] || [ -n "$existing_cname" ]; then
    echo "ERROR: zone ${DNS_ZONE} has partial/mismatched run.app records:" >&2
    echo "       run.app. A   = '${existing_a:-<none>}' (want '${expected_a}')" >&2
    echo "       *.run.app. CNAME = '${existing_cname:-<none>}' (want '${DNS_NAME}')" >&2
    echo "       Refusing to overwrite a zone we don't fully own (fail-closed)." >&2
    echo "       Reconcile by hand, or delete the stray records and re-run." >&2
    exit 1
  fi

  # Both records absent — add them atomically.
  if [ -n "$DRY_RUN" ]; then
    echo "  [DRY_RUN] would add: run.app. A 300 ${VIP_IPS[*]} ; *.run.app. CNAME 300 ${DNS_NAME}"
    return 0
  fi
  # Dedicated transaction file (not the default ./transaction.yaml — avoids CWD
  # pollution and lets the trap clean up deterministically on any failure).
  tx_file="$(mktemp -u)"
  trap 'rm -f "$tx_file"; gcloud dns record-sets transaction abort --project="$PROJECT" --zone="$DNS_ZONE" --transaction-file="$tx_file" >/dev/null 2>&1 || true' ERR
  gcloud dns record-sets transaction start \
    --project="$PROJECT" --zone="$DNS_ZONE" --transaction-file="$tx_file"
  gcloud dns record-sets transaction add "${VIP_IPS[@]}" \
    --project="$PROJECT" --zone="$DNS_ZONE" --transaction-file="$tx_file" \
    --name="$DNS_NAME" --type=A --ttl=300
  gcloud dns record-sets transaction add "$DNS_NAME" \
    --project="$PROJECT" --zone="$DNS_ZONE" --transaction-file="$tx_file" \
    --name="*.${DNS_NAME}" --type=CNAME --ttl=300
  gcloud dns record-sets transaction execute \
    --project="$PROJECT" --zone="$DNS_ZONE" --transaction-file="$tx_file"
  trap - ERR
  rm -f "$tx_file"
  echo "  DNS records: run.app. A → VIP + *.run.app. CNAME → run.app. (TTL 300) added"
}

# --------------------------------------------------------------------------
# 0. Pre-flight: confirm the project exists and the caller can act on it.
# --------------------------------------------------------------------------
if ! gcloud projects describe "$PROJECT" >/dev/null 2>&1; then
  echo "ERROR: project ${PROJECT} does not exist or the active gcloud" >&2
  echo "       account lacks describe permission on it. Authenticate with an" >&2
  echo "       owner of ${PROJECT} (gcloud auth login) and re-run." >&2
  exit 1
fi

CALLER_EMAIL="$(gcloud config get-value account 2>/dev/null)"
if [ -z "$CALLER_EMAIL" ]; then
  echo "ERROR: gcloud has no active account configured. Run 'gcloud auth login'." >&2
  exit 1
fi
echo "Provisioning coordinator VPC egress for ${PROJECT} (region ${REGION}) as ${CALLER_EMAIL}..."
if [ -n "$DRY_RUN" ]; then
  echo "  DRY_RUN=1 — mutating gcloud commands are PRINTED, not executed."
fi

# --------------------------------------------------------------------------
# 1. APIs — compute (VPC/subnet/route) + dns (private zone). Idempotent.
# --------------------------------------------------------------------------
run_cmd gcloud services enable compute.googleapis.com dns.googleapis.com \
  --project="$PROJECT"
echo "  APIs: compute.googleapis.com + dns.googleapis.com enabled"

# --------------------------------------------------------------------------
# 2. VPC driftscribe-vpc (--subnet-mode=custom). FAIL-CLOSED on collision:
#    if it already exists, verify it is custom-mode and do NOT mutate it; if
#    it exists but is NOT custom-mode, ABORT.
# --------------------------------------------------------------------------
# Separate existence (describe rc) from mode. The mode field is the REST
# `autoCreateSubnetworks` boolean (custom ⇒ False, auto ⇒ True, legacy ⇒ absent)
# — NOT `subnetworkRangeMode`, which is not a real field and would silently
# yield "" (breaking idempotency: a re-run would re-create the existing VPC).
if gcloud compute networks describe "$NETWORK" --project="$PROJECT" >/dev/null 2>&1; then
  EXISTING_AUTO="$(gcloud compute networks describe "$NETWORK" \
    --project="$PROJECT" --format='value(autoCreateSubnetworks)' 2>/dev/null \
    | tr '[:upper:]' '[:lower:]')"
  if [ "$EXISTING_AUTO" = "false" ]; then
    echo "  VPC ${NETWORK} already exists (custom-mode) — not mutating"
  else
    echo "ERROR: VPC ${NETWORK} exists but is not custom-mode" >&2
    echo "       (autoCreateSubnetworks='${EXISTING_AUTO:-<unset/legacy>}', want false)." >&2
    echo "       Refusing to mutate a pre-existing non-custom VPC (fail-closed on" >&2
    echo "       collision). Pick a different NETWORK or reconcile by hand." >&2
    exit 1
  fi
else
  run_cmd gcloud compute networks create "$NETWORK" \
    --project="$PROJECT" \
    --subnet-mode=custom \
    --bgp-routing-mode=regional
  echo "  VPC ${NETWORK}: created (custom-mode)"
fi

# --------------------------------------------------------------------------
# 3. Subnet driftscribe-coord-an1 (asia-northeast1, 10.8.0.0/24, PGA on).
#    Direct VPC egress consumes ~1 IP/instance; /24 is generous. If the subnet
#    exists, verify region + range + private-ip-google-access match; else ABORT.
# --------------------------------------------------------------------------
EXISTING_SUBNET="$(gcloud compute networks subnets describe "$SUBNET" \
  --project="$PROJECT" --region="$REGION" \
  --format='value(ipCidrRange,privateIpGoogleAccess)' 2>/dev/null || echo "")"
if [ -z "$EXISTING_SUBNET" ]; then
  run_cmd gcloud compute networks subnets create "$SUBNET" \
    --project="$PROJECT" \
    --network="$NETWORK" \
    --region="$REGION" \
    --range="$SUBNET_RANGE" \
    --enable-private-ip-google-access
  echo "  subnet ${SUBNET} (${REGION}, ${SUBNET_RANGE}, PGA on): created"
else
  # value(a,b) is TAB-separated; awk splits on whitespace. PGA renders as a
  # capitalized bool — lowercase-compare to be version-robust.
  EXISTING_RANGE="$(echo "$EXISTING_SUBNET" | awk '{print $1}')"
  EXISTING_PGA="$(echo "$EXISTING_SUBNET" | awk '{print tolower($2)}')"
  if [ -z "$EXISTING_RANGE" ] || [ -z "$EXISTING_PGA" ]; then
    echo "ERROR: could not parse existing subnet ${SUBNET} describe output" >&2
    echo "       ('${EXISTING_SUBNET}'). Refusing to proceed (fail-closed)." >&2
    exit 1
  fi
  if [ "$EXISTING_RANGE" != "$SUBNET_RANGE" ] || [ "$EXISTING_PGA" != "true" ]; then
    echo "ERROR: subnet ${SUBNET} (${REGION}) exists but does not match expected:" >&2
    echo "       range=${EXISTING_RANGE} (want ${SUBNET_RANGE}), PGA=${EXISTING_PGA} (want true)." >&2
    echo "       Refusing to mutate (fail-closed on collision)." >&2
    exit 1
  fi
  echo "  subnet ${SUBNET} already exists (${EXISTING_RANGE}, PGA=${EXISTING_PGA}) — matches, not mutating"
fi

# --------------------------------------------------------------------------
# 4. Cloud DNS PRIVATE zone run-app: run.app. → VIP (A) + *.run.app. → CNAME.
#    Google's documented pattern is apex-A + wildcard-CNAME, NOT wildcard-A.
#    FAIL-CLOSED: if a run.app. private zone already exists, verify its
#    visibility/attached-network/records match expected; do NOT silently add or
#    overwrite records in a pre-existing zone (could break unrelated resources).
# --------------------------------------------------------------------------
EXISTING_ZONE_DNSNAME="$(gcloud dns managed-zones describe "$DNS_ZONE" \
  --project="$PROJECT" --format='value(dnsName)' 2>/dev/null || echo "")"
if [ -z "$EXISTING_ZONE_DNSNAME" ]; then
  run_cmd gcloud dns managed-zones create "$DNS_ZONE" \
    --project="$PROJECT" \
    --dns-name="$DNS_NAME" \
    --description="DriftScribe C5c: redirect run.app to private.googleapis.com VIP" \
    --visibility=private \
    --networks="$NETWORK"
  echo "  DNS zone ${DNS_ZONE} (${DNS_NAME}, private, network=${NETWORK}): created"
else
  # The zone exists — verify it is OURS before trusting it. Never mutate a
  # pre-existing run.app. zone (it could belong to unrelated infra).
  if [ "$EXISTING_ZONE_DNSNAME" != "$DNS_NAME" ]; then
    echo "ERROR: DNS zone ${DNS_ZONE} exists but dnsName=${EXISTING_ZONE_DNSNAME} (want ${DNS_NAME})." >&2
    echo "       Refusing to touch a zone that is not run.app. (fail-closed)." >&2
    exit 1
  fi
  ZONE_VIS="$(gcloud dns managed-zones describe "$DNS_ZONE" \
    --project="$PROJECT" --format='value(visibility)' 2>/dev/null || echo "")"
  ZONE_NETS="$(gcloud dns managed-zones describe "$DNS_ZONE" \
    --project="$PROJECT" \
    --format='value(privateVisibilityConfig.networks[].networkUrl)' 2>/dev/null || echo "")"
  if [ "$ZONE_VIS" != "private" ]; then
    echo "ERROR: DNS zone ${DNS_ZONE} exists but visibility=${ZONE_VIS} (want private)." >&2
    echo "       Refusing to overwrite a pre-existing run.app. zone (fail-closed)." >&2
    exit 1
  fi
  # networkUrl ends with /<NETWORK>; NETWORK is validated above (no regex metachars).
  if ! echo "$ZONE_NETS" | grep -qE "/${NETWORK}\$"; then
    echo "ERROR: DNS zone ${DNS_ZONE} is not attached to network ${NETWORK}." >&2
    echo "       Attached networks: ${ZONE_NETS:-<none>}." >&2
    echo "       Refusing to mutate a pre-existing zone (fail-closed)." >&2
    exit 1
  fi
  echo "  DNS zone ${DNS_ZONE} already exists (private, attached to ${NETWORK}) — verifying records"
fi

# Ensure the two records exist and match — idempotent + recoverable + fail-closed.
# Called whether the zone was just created OR pre-existed-and-verified-ours, so a
# run interrupted between zone-create and record-add self-heals on the next run.
ensure_dns_records

# --------------------------------------------------------------------------
# 5. Routes. Custom-mode VPCs auto-create a 0.0.0.0/0 → default-internet-gateway
#    route (PGA reaches the VIP via it); just VERIFY it is present. Add an
#    explicit 199.36.153.8/30 → default-internet-gateway route ONLY if no
#    covering route is already present.
# --------------------------------------------------------------------------
# The route `network` field is a full resource URL ending in /networks/<NAME>;
# anchor the regex on that path segment (NETWORK validated above).
NET_FILTER="network~/networks/${NETWORK}\$"
DEFAULT_ROUTE="$(gcloud compute routes list \
  --project="$PROJECT" \
  --filter="${NET_FILTER} AND destRange=0.0.0.0/0 AND nextHopGateway~default-internet-gateway" \
  --format='value(name)' 2>/dev/null | head -n1 || echo "")"
if [ -n "$DEFAULT_ROUTE" ]; then
  echo "  route: 0.0.0.0/0 → default-internet-gateway present (${DEFAULT_ROUTE})"
else
  echo "  WARNING: no 0.0.0.0/0 → default-internet-gateway route found on ${NETWORK}." >&2
  echo "           A custom-mode VPC normally auto-creates one; PGA reaches the VIP" >&2
  echo "           via it. The explicit VIP route below covers the VIP regardless." >&2
fi

# Explicit VIP route. Idempotent by NAME first (so a re-run never hits a
# "route already exists" create error regardless of filter quirks), then by
# coverage (skip if the default 0.0.0.0/0 already covers the VIP block).
VIP_ROUTE_NAME="${NETWORK}-vip-igw"
if gcloud compute routes describe "$VIP_ROUTE_NAME" --project="$PROJECT" >/dev/null 2>&1; then
  echo "  route ${VIP_ROUTE_NAME} already exists — not mutating"
else
  VIP_COVERED="$(gcloud compute routes list \
    --project="$PROJECT" \
    --filter="${NET_FILTER} AND (destRange=0.0.0.0/0 OR destRange=${VIP_RANGE})" \
    --format='value(name)' 2>/dev/null | head -n1 || echo "")"
  if [ -n "$VIP_COVERED" ]; then
    echo "  route: ${VIP_RANGE} already covered (${VIP_COVERED}) — not adding explicit route"
  else
    run_cmd gcloud compute routes create "$VIP_ROUTE_NAME" \
      --project="$PROJECT" \
      --network="$NETWORK" \
      --destination-range="$VIP_RANGE" \
      --next-hop-gateway=default-internet-gateway
    echo "  route: ${VIP_RANGE} → default-internet-gateway created (${VIP_ROUTE_NAME})"
  fi
fi

# --------------------------------------------------------------------------
# 6. IAM — roles/compute.networkUser on the subnet for the Cloud Run service
#    agent (Direct VPC egress needs the SERVICE agent to use the subnet).
#    Subnet-scoped (not project-wide). Idempotent (server-side). Optionally
#    grant an additional deploy principal via NETWORK_USER_MEMBER.
# --------------------------------------------------------------------------
run_cmd gcloud compute networks subnets add-iam-policy-binding "$SUBNET" \
  --project="$PROJECT" --region="$REGION" \
  --member="serviceAccount:${RUN_SERVICE_AGENT}" \
  --role="roles/compute.networkUser"
echo "  ${RUN_SERVICE_AGENT}: compute.networkUser on subnet ${SUBNET} (Cloud Run service agent)"

if [ -n "$NETWORK_USER_MEMBER" ]; then
  run_cmd gcloud compute networks subnets add-iam-policy-binding "$SUBNET" \
    --project="$PROJECT" --region="$REGION" \
    --member="$NETWORK_USER_MEMBER" \
    --role="roles/compute.networkUser"
  echo "  ${NETWORK_USER_MEMBER}: compute.networkUser on subnet ${SUBNET} (extra deploy principal)"
fi

# --------------------------------------------------------------------------
# 7. Next steps — PRINTED, never executed. The staged redeploy + smoke are
#    operator-gated (they touch the live coordinator).
# --------------------------------------------------------------------------
cat <<EOF

================================================================
setup_coordinator_vpc.sh: complete

(a) DNS RESOLUTION PROOF — only a VPC-attached caller resolves the private
    zone, so prove it from inside the coordinator (or any instance on
    ${NETWORK}). After the staged redeploy, the reachability endpoint below is
    the authoritative proof; a raw DNS check from a VPC-attached VM is:

      dig +short run.app                  # expect one of: ${VIP_IPS[*]}
      dig +short driftscribe-tofu-apply-u272wv52kq-an.a.run.app
                                          # expect a VIP via the *.run.app CNAME

(b) STAGED REDEPLOY (--no-traffic, tagged c5c — gives the 0%-traffic revision a
    stable callable URL). Run with the freshly built coordinator image:

  gcloud run services update ${COORDINATOR_SERVICE} \\
    --image=<IMAGE> \\
    --network=${NETWORK} \\
    --subnet=${SUBNET} \\
    --vpc-egress=private-ranges-only \\
    --update-env-vars TOFU_APPLY_URL=${TOFU_APPLY_URL} \\
    --no-traffic --tag c5c \\
    --region=${REGION} --project=${PROJECT}

(c) SMOKE the tagged no-traffic revision (it runs WITH the VPC config, so this
    truly exercises the egress path). GO = HTTP 200 with {"go": true}:

  curl -H "X-DriftScribe-Token: \$TOKEN" \\
    ${COORDINATOR_TAGGED_URL}/iac-apply/reachability

  NO-GO (502/{"go":false} or 503) → abandon the tagged revision (no
  update-traffic; untag), keep live on the old revision, diagnose.
================================================================
EOF
