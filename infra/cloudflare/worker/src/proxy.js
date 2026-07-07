// Passthrough proxy: rewrites the request URL to the Cloud Run hostname
// so Cloud Run accepts the Host header without a custom-domain mapping.
//
// The original request flow:
//   browser -> CF (driftscribe.adp-app.com) -> CF Access auth -> this Worker
//   -> fetch(<rewritten url>, request) -> Cloud Run
//
// `fetch(url, request)` re-issues the request to the new URL; CF derives
// SNI + Host from the rewritten URL, so Cloud Run sees a request it owns.
// All other headers (including Cf-Access-* set by Access) flow through.
//
// DEMO MODE (hackathon judging window — see
// docs/plans/2026-06-12-hackathon-judge-readiness-design.md):
// during the window CF Access carries a bypass policy, so anonymous
// visitors arrive with no Cf-Access-Jwt-Assertion and every verify_token
// route would 401. With DEMO_MODE="1" the Worker:
//   1. strips any browser-supplied X-DriftScribe-Token — the operator
//      token must never be accepted from the public hostname, and
//   2. injects the real token from the DEMO_TOKEN Worker secret, but
//      only on the explicit method+path allowlist below, and only when
//      the request carries no CF Access JWT, and
//   3. rate-limits the anonymous POST /chat path per IP (hackathon A.4
//      cost rail) BEFORE injecting the token — 429 with Retry-After.
// It never strips or synthesizes Cf-Access-Jwt-Assertion: real JWTs flow
// through untouched for require_cf_operator (the IaC approve), and the
// injected static token cannot satisfy that check. Injected requests are
// marked X-DriftScribe-Demo-Anonymous: 1 so origin/UI can render demo
// states; the marker is stripped from inbound requests so it cannot be
// spoofed.
//
// Window flip: set DEMO_MODE in wrangler.toml + `wrangler deploy`. The
// secret is one-time: `wrangler secret put DEMO_TOKEN`.

const ORIGIN = "driftscribe-agent-u272wv52kq-an.a.run.app";

const TOKEN_HEADER = "X-DriftScribe-Token";
const CF_JWT_HEADER = "Cf-Access-Jwt-Assertion";
const MARKER_HEADER = "X-DriftScribe-Demo-Anonymous";

// Routes anonymous visitors may reach with the injected operator token.
// Everything else is forwarded sanitized and the origin's own auth
// decides (401). Deliberately excluded: POST /pause + POST /autonomy
// (kill-switch / autonomy dial stay operator-only) and POST /recheck
// (cost amplification). POST /iac-approvals/{n} needs a CF JWT at origin,
// which this Worker never fabricates.
export const DEMO_ALLOWLIST = [
  ["GET", /^\/decisions$/],
  ["GET", /^\/infra\/graph$/],
  ["GET", /^\/infra\/graph\/preview$/],
  ["GET", /^\/infra\/pending-approvals$/],
  ["GET", /^\/capabilities$/],
  ["GET", /^\/pause$/],
  ["GET", /^\/autonomy$/],
  ["GET", /^\/trace\/[^/]+$/],
  ["GET", /^\/trace\/[^/]+\/pr-body$/],
  ["GET", /^\/conversations$/],
  ["GET", /^\/conversations\/[^/]+$/],
  ["POST", /^\/chat$/],
];

export function demoAllowed(method, pathname) {
  return DEMO_ALLOWLIST.some(([m, re]) => m === method && re.test(pathname));
}

// Hackathon A.4: per-IP rate limit on the anonymous POST /chat path only —
// a /chat run holds long Gemini calls, so it is the one allowlisted route
// where volume costs real money. Keyed on CF-Connecting-IP (set by
// Cloudflare, not spoofable from the client). Fail-open by design: the
// limiter is best-effort defense-in-depth, and a limiter outage must not
// take the demo down. The binding lives in wrangler.toml (CHAT_RATE_LIMIT).
async function chatRateLimited(request, env) {
  if (!env.CHAT_RATE_LIMIT) return false;
  try {
    const key = request.headers.get("CF-Connecting-IP") || "unknown";
    const { success } = await env.CHAT_RATE_LIMIT.limit({ key });
    return !success;
  } catch {
    return false;
  }
}

function rateLimitResponse() {
  return new Response(
    JSON.stringify({
      detail:
        "Rate limit: the demo allows a few chat runs per minute per visitor. Please wait a moment and try again.",
    }),
    {
      status: 429,
      headers: {
        "Content-Type": "application/json",
        "Retry-After": "60",
      },
    },
  );
}

export default {
  async fetch(request, env) {
    // new URL() normalizes the path (e.g. /chat/../pause -> /pause), and
    // the SAME object is both matched against the allowlist and forwarded,
    // so there is no gap between the path we check and the path origin sees.
    const url = new URL(request.url);
    url.hostname = ORIGIN;
    url.protocol = "https:";
    url.port = "";

    if (env?.DEMO_MODE !== "1") {
      return fetch(url.toString(), request);
    }

    const proxied = new Request(url.toString(), request);
    proxied.headers.delete(TOKEN_HEADER);
    proxied.headers.delete(MARKER_HEADER);

    // Missing DEMO_TOKEN fails safe: requests stay sanitized and
    // allowlisted routes 401 — loud misconfig, no privilege granted.
    if (
      env.DEMO_TOKEN &&
      !proxied.headers.has(CF_JWT_HEADER) &&
      demoAllowed(request.method, url.pathname)
    ) {
      // Rate-limit BEFORE granting the token, and only on the anonymous
      // /chat path — operator (CF JWT) traffic never reaches this branch.
      if (
        request.method === "POST" &&
        url.pathname === "/chat" &&
        (await chatRateLimited(request, env))
      ) {
        return rateLimitResponse();
      }
      proxied.headers.set(TOKEN_HEADER, env.DEMO_TOKEN);
      proxied.headers.set(MARKER_HEADER, "1");
    }

    return fetch(proxied);
  },
};
