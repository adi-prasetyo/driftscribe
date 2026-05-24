#!/usr/bin/env bash
# Idempotent one-shot setup of:
#   - DNS CNAME driftscribe.adp-app.com → driftscribe-agent (Cloud Run)
#   - Access Identity Provider (Google OAuth)
#   - Access Application gating driftscribe.adp-app.com
#   - Access Policy with the operator-side email allowlist
#
# Host-header rewriting (so Cloud Run accepts the request) is handled by
# the companion Worker in infra/cloudflare/worker/ — Origin Rules can't
# rewrite the Host header on Free CF plans ("not entitled to use the
# HostHeader override"). Deploy the Worker separately with `wrangler deploy`.
#
# Required env vars (load via `set -a; source .env; set +a`):
#   CLOUDFLARE_DRIFTSCRIBE_API_TOKEN
#   CLOUDFLARE_DRIFTSCRIBE_OAUTH_CLIENT_ID
#   CLOUDFLARE_DRIFTSCRIBE_OAUTH_CLIENT_SECRET
#
# Token must hold these scopes against the adp-app.com zone + this account:
#   Zone:DNS:Edit, Zone:Zone:Read,
#   Account:Access: Apps and Policies:Edit,
#   Account:Access: Identity Providers:Edit

set -euo pipefail

ACCOUNT_ID="7e8265bd122c779322afe1b236623346"
ZONE_ID="0147f3be9ad4c023a2859d9bfb6b6a97"
HOST="driftscribe.adp-app.com"
ORIGIN="driftscribe-agent-u272wv52kq-an.a.run.app"
TEAM_DOMAIN="adp-app.cloudflareaccess.com"
ALLOWED_EMAILS=("adp.app.claude@gmail.com" "theghostsquad00@gmail.com")

API="https://api.cloudflare.com/client/v4"
: "${CLOUDFLARE_DRIFTSCRIBE_API_TOKEN:?must export CLOUDFLARE_DRIFTSCRIBE_API_TOKEN}"
: "${CLOUDFLARE_DRIFTSCRIBE_OAUTH_CLIENT_ID:?must export CLOUDFLARE_DRIFTSCRIBE_OAUTH_CLIENT_ID}"
: "${CLOUDFLARE_DRIFTSCRIBE_OAUTH_CLIENT_SECRET:?must export CLOUDFLARE_DRIFTSCRIBE_OAUTH_CLIENT_SECRET}"

AUTH=(-H "Authorization: Bearer ${CLOUDFLARE_DRIFTSCRIBE_API_TOKEN}")
JSON=(-H "Content-Type: application/json")

# Strict call: any non-success response is fatal.
cf_call() {
  local out
  out=$(curl -sS "${AUTH[@]}" "${JSON[@]}" "$@")
  if [[ "$(jq -r .success <<<"$out")" != "true" ]]; then
    echo "Cloudflare API call failed:" >&2
    printf '  curl %s\n' "$*" >&2
    echo "$out" | jq . >&2
    exit 1
  fi
  echo "$out"
}

# Forgiving call: returns the body whether success or 404 (caller handles).
cf_try() {
  curl -sS "${AUTH[@]}" "${JSON[@]}" "$@"
}

# --- 1. DNS CNAME ----------------------------------------------------------
echo ">>> [1/4] DNS CNAME $HOST -> $ORIGIN (proxied)"
existing=$(cf_call "$API/zones/$ZONE_ID/dns_records?type=CNAME&name=$HOST" \
  | jq -r '.result[0].id // ""')
if [[ -n "$existing" ]]; then
  echo "    exists (id=$existing); skipping"
else
  body=$(jq -nc --arg name "$HOST" --arg origin "$ORIGIN" '{
    type:"CNAME", name:$name, content:$origin, proxied:true, ttl:1,
    comment:"driftscribe transparency UI (managed by infra/cloudflare/setup-access.sh)"
  }')
  rid=$(cf_call -X POST "$API/zones/$ZONE_ID/dns_records" --data "$body" \
    | jq -r .result.id)
  echo "    created (id=$rid)"
fi

# --- 2. Access Identity Provider (Google OAuth) ----------------------------
echo ">>> [2/4] Access IdP (Google OAuth)"
IDP_NAME="driftscribe-google"
idps=$(cf_call "$API/accounts/$ACCOUNT_ID/access/identity_providers")
idp_id=$(jq -r --arg n "$IDP_NAME" '.result[] | select(.name==$n) | .id' <<<"$idps" | head -1)
if [[ -n "$idp_id" ]]; then
  echo "    exists (id=$idp_id); skipping"
else
  body=$(jq -nc --arg name "$IDP_NAME" \
                --arg cid "$CLOUDFLARE_DRIFTSCRIBE_OAUTH_CLIENT_ID" \
                --arg sec "$CLOUDFLARE_DRIFTSCRIBE_OAUTH_CLIENT_SECRET" '{
    name:$name, type:"google",
    config:{ client_id:$cid, client_secret:$sec }
  }')
  idp_id=$(cf_call -X POST "$API/accounts/$ACCOUNT_ID/access/identity_providers" \
    --data "$body" | jq -r .result.id)
  echo "    created (id=$idp_id)"
fi

# --- 3. Access Application -------------------------------------------------
echo ">>> [3/4] Access Application for $HOST"
apps=$(cf_call "$API/accounts/$ACCOUNT_ID/access/apps")
app_id=$(jq -r --arg host "$HOST" '.result[] | select(.domain==$host) | .id' <<<"$apps" | head -1)
if [[ -n "$app_id" ]]; then
  echo "    exists (id=$app_id); skipping"
else
  body=$(jq -nc --arg name "DriftScribe transparency UI" \
                --arg host "$HOST" \
                --arg idp "$idp_id" '{
    name:$name, domain:$host, type:"self_hosted",
    session_duration:"24h",
    allowed_idps:[$idp], auto_redirect_to_identity:true,
    app_launcher_visible:true
  }')
  app_id=$(cf_call -X POST "$API/accounts/$ACCOUNT_ID/access/apps" --data "$body" \
    | jq -r .result.id)
  echo "    created (id=$app_id)"
fi

# --- 4. Access Policy (email allowlist) ------------------------------------
echo ">>> [4/4] Access Policy"
POL_NAME="driftscribe-allowlist"
policies=$(cf_call "$API/accounts/$ACCOUNT_ID/access/apps/$app_id/policies")
pol_id=$(jq -r --arg n "$POL_NAME" '.result[] | select(.name==$n) | .id' <<<"$policies" | head -1)
emails_json=$(printf '%s\n' "${ALLOWED_EMAILS[@]}" | jq -R '{email:{email:.}}' | jq -s .)

if [[ -n "$pol_id" ]]; then
  body=$(jq -nc --arg n "$POL_NAME" --argjson inc "$emails_json" '{
    name:$n, decision:"allow", include:$inc
  }')
  cf_call -X PUT "$API/accounts/$ACCOUNT_ID/access/apps/$app_id/policies/$pol_id" \
    --data "$body" >/dev/null
  echo "    updated (id=$pol_id) — emails: ${ALLOWED_EMAILS[*]}"
else
  body=$(jq -nc --arg n "$POL_NAME" --argjson inc "$emails_json" '{
    name:$n, decision:"allow", include:$inc
  }')
  pol_id=$(cf_call -X POST "$API/accounts/$ACCOUNT_ID/access/apps/$app_id/policies" \
    --data "$body" | jq -r .result.id)
  echo "    created (id=$pol_id) — emails: ${ALLOWED_EMAILS[*]}"
fi

cat <<EOF

Access setup done.

  Public URL:    https://$HOST/ui/transparency
  Team domain:   https://$TEAM_DOMAIN
  Allowed:       ${ALLOWED_EMAILS[*]}

NEXT STEPS:
  1. Deploy the Host-rewrite Worker:
       cd infra/cloudflare/worker && wrangler deploy
  2. Add the JWT-verification middleware to the coordinator so signed-in
     users don't also have to paste X-DriftScribe-Token.
  3. Open the public URL in an incognito window to verify the Google
     sign-in → transparency UI flow.
EOF
