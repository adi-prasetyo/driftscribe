# Hackathon judge-readiness — design (2026-06-12)

Decisions and work plan for making DriftScribe submission-ready for the
DevOps × AI Agent Hackathon 2026 (Findy / Google Cloud Japan).

## Submission facts (from the full rules read, 2026-06-12)

Source: https://findy.notion.site/devops-ai-agent-hackathon-2026 (all
collapsed toggles included).

- **Deadline 2026-07-10 (Fri) 23:59 JST.** Final entry = Google Form with
  three URLs: (1) **public** GitHub repo, (2) **deployed project URL that
  judges can operate** (「動作確認できる状態にしておくこと」), (3) ProtoPedia
  work page. Resubmission allowed — latest timestamp wins.
- ProtoPedia required fields: title, 概要, **demo video (YouTube/Vimeo)**,
  **system-architecture diagram upload**, dev tools, tag `findy_hackathon`,
  story (①課題と背景 ②想定ユーザー ③特徴).
- **一次審査 7/13–7/17 is Findy office staff** — non-experts will click the
  deployed URL. 二次審査 7/21–7/24 (external judges). Top-10 announced 7/30;
  finals 8/19 at Google Shibuya.
- Judging criteria include **usability** (access friction costs points) and
  implementation strength (実運用への配慮 — the safety rails are a story
  asset, tell them).

## Decisions (made 2026-06-12)

1. **Keep the DriftScribe name.** OpsPilot rejected: generic, existing
   product collisions, post-Copilot me-too reading, loses the unique search
   hit. Breadth is conveyed by tagline/README copy instead. Brand work must
   land **before the demo video is recorded** (the video bakes the name in).
2. **Open-access window for judging.** No shared demo credentials; judges
   click the URL and it works. Time-boxed: open by 7/10, close ~7/30,
   reopen for finals 8/19 if selected.

## Key technical finding — naive CF Access bypass breaks the app

Custom-domain chain: browser → CF Access → CF Worker passthrough proxy
(`infra/cloudflare/worker/src/proxy.js`) → Cloud Run coordinator. The
coordinator itself validates `Cf-Access-Jwt-Assertion` (team domain + AUD)
in `agent/auth.py:verify_token`, falling back to the operator token header.
A bare Access **bypass** policy strips the JWT → every authed route 401s
for anonymous visitors.

**Fix:** keep the bypass at Access, and have the Worker inject
`X-DriftScribe-Token` server-side when no CF JWT is present (token held as
a Worker secret, behavior gated by a Worker env flag so the window is one
config flip). The token never reaches the browser.

Worker hardening (Codex must-fix, thread 019ebb82):

- **Sanitize inbound headers.** The current proxy forwards everything.
  Demo mode must build fresh `Headers`, **delete any browser-supplied
  `X-DriftScribe-Token`**, and only then set it from the Worker secret —
  never let a forged operator token reach origin via the public hostname.
  Never touch or synthesize `Cf-Access-Jwt-Assertion`: real CF JWTs flow
  through for `require_cf_operator`; the injected static token cannot
  satisfy it (JWT is validated cryptographically server-side).
- **Allowlist injection by method+path**, not blanket. Anonymous
  allowlist: `GET /decisions`, `GET /infra/graph`(+`/preview`),
  `GET /capabilities`, `GET /pause`, `GET /autonomy`, `GET /trace/{id}`,
  `POST /chat`. **Excluded:** `POST /pause`, `POST /autonomy` (visitors
  could disable the kill-switch or raise autonomy), `POST /recheck`
  (cost amplification — exclude, or per-IP throttle hard if the demo
  needs it).
- Nice-to-have: mark injected requests with a header like
  `X-DriftScribe-Demo-Anonymous: 1` so origin/UI can render demo states.

Route audit (`agent/main.py`):

- `verify_token` (CF JWT or `X-DriftScribe-Token`): `POST /chat`,
  `/decisions`, `/infra/graph`(+preview), `/trace/{id}`, `/capabilities`,
  `/recheck`, `/pause` (GET+POST), `/autonomy` (GET+POST),
  `/iac-apply/reachability`.
- `require_cf_operator` (strict CF JWT, **stays operator-only**):
  `POST /iac-approvals/{pr_number}` — the IaC approve. Anonymous judges
  browse the approval page but cannot approve; the demo video shows the
  operator approve→apply step.
- **Unauthenticated by design** (document, keep in mind for the window):
  `GET /` (SPA), `/ui/transparency-legacy`, `/runs/{decision_id}`,
  `GET+POST /approvals/{approval_id}` (HMAC token in URL/form),
  `GET /iac-approvals/{pr_number}`, `/eventarc` (Google ID-token).

### The rollback-approval hole (Codex catch — must address before opening)

`GET/POST /approvals/{approval_id}` is authenticated **only** by the
plan-bound HMAC token carried in the URL/form — no CF dependency. If an
anonymous `/chat` session mints a drift-rollback proposal, the timeline
hands the visitor a tokenized approval link they can click through —
**executing a rollback** on `payment-demo`. "Strangers cannot mutate GCP"
is therefore only true if we close this:

- Before opening the window, pin the autonomy dial to a level that does
  not mint rollback approvals for anonymous sessions (observe/propose),
  and keep `POST /autonomy` excluded from injection so visitors can't
  raise it back.
- Decide at implementation whether a judge-driven rollback of the demo
  service is *desired* (it is a bounded, impressive demo) — if so, gate it
  deliberately, don't inherit it by accident.

**Verified at A.2 implementation (2026-06-12), with one correction to the
assumption above:** `drift_propose_rollback` is tier `propose`
(`agent/workloads/registry.py` TOOL_TIERS), so at dial=`propose` an
anonymous `/chat` session CAN still mint a rollback approval and receive
the tokenized link in its timeline. The hole is closed one step later and
that closure is already shipped: `POST /approvals/{approval_id}` approve
refuses **409** below Propose+Apply (`agent/main.py` approval_post — gate
landed with the ClickOps item-11 dial), the `GET /approvals/{id}` page
renders the Approve button disabled with the dial note, and reject stays
allowed (safety direction). With `POST /autonomy` excluded from Worker
injection (A.1), visitors cannot raise the dial back.

**Second Codex catch (A.2 completed-work review) — cross-session token
harvest, closed at A.2:** rollback decisions persist
`approval.approval_url` (the live single-use `?t=` token) and
`rendered_body` embeds the same URL, and `GET /decisions` /
`GET /trace/{id}` / the unauthenticated `GET /runs/{id}` served the doc
with only a rationale scrub. With anonymous `/decisions` allowlisted
(A.1), a visitor could harvest OTHER sessions' tokenized links — deny a
pending operator rollback today, execute one if the dial were ever at
Propose+Apply. Closed by a serve-time scrub
(`agent/renderer.py scrub_decision_approval` +
`redact_approval_tokens_deep`): drops `approval.approval_url` (the rail
then renders no CTA) and redacts `?t=` values in every doc/event string.
Applied on `/decisions` + `/trace` when the request carries the
Worker-injected `X-DriftScribe-Demo-Anonymous` marker (present exactly on
injected anonymous requests; spoofing it only redacts your own view), and
ALWAYS on `/runs/{id}` (unauthenticated, decision_ids enumerable via the
demo-window `/decisions`, no UI consumer). Operators (CF JWT via Access,
or run.app + token — never marked) keep the rail CTA; the `/trace`
in-process cache stores unscrubbed and the scrub is per-request
copy-on-change. A visitor's OWN `/chat` session still receives its own
approval link in the timeline — accepted: approve 409s at the pin, and
denying your own proposal is the safety direction. **Window pin = `propose`, not `observe`:** at
propose the demo stays alive (judges watch investigate→propose, including
PR authoring on the e2e target), while GCP mutation is impossible —
rollback execute 409s, `upgrade_merge_pr` (the only `apply`-tier tool) is
stripped from the agent, and the IaC apply needs a CF JWT the Worker
never fabricates. Residual at propose: judges can drive propose-tier
GitHub churn (PRs/issues on `driftscribe-e2e-target`) — bounded by the
A.4 per-IP rate limit, and it IS the demo. Pre-window steps (add to the
A.3 flip runbook): `POST /autonomy {"mode":"propose"}` as the operator
before enabling `DEMO_MODE`; and when running the `wrangler deploy` that
flips `DEMO_MODE="1"`, verify the deploy output lists
`env.CHAT_RATE_LIMIT` as a first-class **Rate Limit (5 requests/60s)**
binding — the limiter fails open by design, so a binding that silently
stopped resolving would silently remove the cost rail (Codex, A.4
review).

### Judge UX on approval pages

`GET /iac-approvals/{pr_number}` can render an active Approve form that an
anonymous click then fails with a raw 401. In demo mode, suppress the
Approve form when the request lacks a CF JWT and show an
"operator-only — demonstrated in the video" note instead.

**Implemented (A.2, 2026-06-12):** a new `operator_only` rung in the GET's
gate ladder fires when CF Access is configured AND the request carries no
`Cf-Access-Jwt-Assertion` header (presence-only by design — cryptographic
verification stays on the POST; a forged header buys exactly the
pre-existing behavior, a form whose submit 401s). No coordinator-side
demo flag needed: the condition is *always* faithful, because the POST
can never succeed without the JWT — outside the window only direct
run.app probes hit it, and the note is accurate there too. The rung sits
*above* the operator-state rungs (token/dry-run/pause/dial) so judges see
"operator-only" rather than dial-speak while the dial is pinned, *below*
the artifact hard-stops (a bad artifact is everyone's alarm), and the
terminal-decision lookup still runs on the anonymous-only path so
historical rows render their honest "already applied" banner. No CSRF
form token is minted for anonymous viewers. CF Access unconfigured
(local dev/tests) → rung inert.

### Cost/abuse controls (before opening)

- Per-IP Cloudflare rate limit specifically on `POST /chat` (SSE runs hold
  long model calls — a generic hostname limit is not enough).
- Prompt max-length cap on `/chat` if not already enforced.
- GCP billing alert + Gemini spend sanity check; the pause button is the
  operator kill-switch (and stays operator-only).

**Implemented (A.4, 2026-06-12):**

- *Rate limit:* a Workers Rate Limiting binding (`CHAT_RATE_LIMIT`,
  5 requests / 60 s / IP, `wrangler.toml`) rather than a zone WAF rule —
  the free-plan zone rule offers one rule with 10 s windows and would
  throttle the operator too. The Worker consults the limiter only on the
  anonymous demo-mode `POST /chat` token-injection path, *before*
  granting the token: operators with a CF JWT are never throttled, the
  cheap read routes stay unthrottled, and outside the window CF Access
  gates everything anyway. Exceeding it returns 429 + `Retry-After: 60`
  with a JSON `detail`; the SPA renders a friendly "wait a moment"
  message. Keyed on `CF-Connecting-IP` (set by Cloudflare, not
  spoofable). Fail-open on binding absence or limiter outage — it is an
  abuse rail, not an auth boundary; the limiter is per-colo and
  approximate, which is fine for that job. Direct `run.app` traffic
  bypasses the Worker but carries no demo token, so anonymous `/chat`
  401s there.
- *Prompt cap:* `ChatRequest.prompt` capped at 8000 chars (~2k tokens —
  a pasted log or diff fits; only a deliberately huge body 422s, in
  validation before any run starts). `session_id` capped at 128
  belt-and-braces so no other field can smuggle an unbounded payload.
- *Billing alert:* already existed — budget "driftscribe-hack-2026
  monthly alert" on the billing account, ¥2000/month scoped to this
  project, 50/90/100% current-spend thresholds, default notifications
  (billing-admin email).
- *Gemini spend:* the coordinator runs Gemini via Vertex AI
  (`GOOGLE_GENAI_USE_VERTEXAI=true`), so model spend bills this project
  and the budget covers it. June month-to-date (checked 6/12): 91
  Vertex requests, 325k input + 29k output tokens — well under ¥150.

## Work items

### A. Access window (implement in June — staging + smoke well before the
video; only the *opening* waits for 7/10)

1. Worker demo mode: header sanitization + allowlist-based
   `X-DriftScribe-Token` injection as specified above (`DEMO_MODE` flag +
   Worker secret).
2. Demo-mode approval-page UX: suppress the IaC Approve form for non-CF
   requests; verify the rollback-approval hole is closed (autonomy pinned,
   `POST /autonomy` excluded).
3. CF Access: add the time-boxed bypass policy (flip on 7/10, off ~7/30,
   on again for 8/19 finals if selected). **Note (Codex, A.1 review):** an
   Everyone-bypass removes the CF JWT for *operators* on this hostname
   too, so `POST /iac-approvals/{n}` 401s for everyone during the window.
   Acceptable — the approve is demonstrated in the video; if an approve is
   needed mid-window, flip bypass off briefly or wait for close.
4. Rails: per-IP rate limit on `POST /chat`, prompt length cap, GCP
   billing alert, Gemini spend check.
5. Live-verify the anonymous flow (incognito probe: SPA loads, chat works,
   decisions rail renders, IaC approve correctly suppressed, rollback
   approval not *executable* — minting at dial=propose is expected, the
   approve must 409 and `/decisions`/`/trace`/`/runs` must serve no `?t=`
   token (the A.2 serve-time scrub), `POST /pause`/`POST /autonomy`
   refused).

### B. Branding (now, before video)

5. Sharpen the README/README.ja first paragraph + SPA tagline to convey the
   four workloads + the safety/transparency story. **README.ja.md matters —
   the judges are Japanese.**

### C. Repos / accounts

6. `driftscribe-e2e-target` → public after a secrets scan (fixes the
   404 upgrade-PR links in the decisions rail for judges).
7. Optional polish: `driftscribe-bot` machine account holding the agent PAT
   so authored PRs aren't under the personal handle (GitHub ToS allows one
   machine account). Low priority; do not transfer the main repo (Cloud
   Build connection/triggers/PAT breakage).
8. Operator: dedicated email for the ProtoPedia account (keeps the personal
   address off the public work page).

### D. Submission assets

9. Demo video (required): storyboard → record **after** B lands. Show the
   full author→approve→apply loop incl. the operator-only approve.
10. Architecture diagram: export an image from
    `docs/architecture/architecture.html` for the required ProtoPedia upload.
11. ProtoPedia page copy: draft 概要 + story (課題/ユーザー/特徴) from
    README/OVERVIEW; tag `findy_hackathon`.
12. File the final Google Form by **7/9** (one-day buffer).

## Open questions

- Is a judge-driven rollback of `payment-demo` a desired demo (gated
  deliberately) or excluded entirely? Default per Codex: excluded
  (autonomy pinned to observe/propose during the window).
  **Resolved at A.2 (2026-06-12): excluded — window pin is `propose`;
  judges see the full propose flow (including the rollback approval page,
  Approve disabled) but execution refuses 409. See the rollback-hole
  section above.**
- Does `/chat` already enforce a prompt max length, or does demo mode need
  to add one? **Answered 2026-06-12: no — `ChatRequest.prompt: str` has no
  `max_length` (`agent/main.py:4256`). Work item A.4 must add the cap.**

## Review

Codex thread `019ebb82-bbb4-7b83-859b-fb73347d5363` (plan review,
2026-06-12): approach sound; must-fixes folded above — Worker header
sanitization, allowlist-based injection, `POST /pause`/`POST /autonomy`
exclusion, the `/approvals/{approval_id}` rollback hole + autonomy
pinning, IaC approval-page demo UX, `POST /chat` per-IP rate limit +
prompt cap, and pulling item A forward from 7/8 into June. Verified
against code: token header name (`X-DriftScribe-Token`, `agent/auth.py`)
and the tokenized no-CF `/approvals/{approval_id}` routes
(`agent/main.py:2640,4136`).
