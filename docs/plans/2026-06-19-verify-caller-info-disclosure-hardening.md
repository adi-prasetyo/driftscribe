# Plan: harden `driftscribe_lib.auth.verify_caller` — remove info disclosure (parity with `verify_oidc_caller`)

Date: 2026-06-19
Branch: `fix/verify-caller-no-email-echo` (worktree `.worktrees/verify-caller-hygiene`)

## Problem (the declared follow-up)

`driftscribe_lib/auth.py::verify_caller` (the coordinator→worker inter-service guard,
used by 9 workers) leaks information in its error details:

- **403** `detail=f"caller {email!r} not in allowed_callers"` — **echoes the caller's
  own SA email** (the declared follow-up).
- **401** `detail=f"invalid token: {e}"` — echoes the verification exception (which
  Google-auth check failed: audience/expiry/signature) — same class of disclosure.

Its newer sibling `verify_oidc_caller` (added in the tier-3 work, Codex-audited) already
does this correctly: uniform `"invalid token"` 401, `"caller service account not allowed"`
403 (no echo), constant-time `hmac.compare_digest` allowlist check with an `isinstance(str)`
guard, and catches both `ValueError` and `GoogleAuthError`.

## Blast-radius finding (verified by grep)

- 9 workers call `verify_caller` via a thin `_verify_caller_dep` wrapper:
  upgrade_docs, upgrade_reader, rollback, notifier, docs, reader, infra_reader,
  tofu_editor, tofu_apply.
- The coordinator (`agent/main.py`) does **not** call `verify_caller` directly.
- **No test asserts the real `verify_caller` detail.** The worker tests that contain
  `"...not in allowed_callers"` / `"invalid token: audience mismatch"` are local
  `deny_caller` / `deny_audience` **fakes** injected via `dependency_overrides`; they
  only `assert status_code == 401|403`, never the detail, and never call the real
  function. (The memory note "pinned by 8 worker test files" was imprecise — they are
  illustrative fakes, not real pins.)
- ⇒ Changing the real function's details breaks **nothing**.

## Scope decision

Do the full alignment with the audited sibling (same class of fix, trivially cheap),
not just the literal 403:

1. **403** → `"caller service account not allowed"` (no email echo). [declared]
2. **401 invalid-token** → uniform `"invalid token"` (no exception echo).
3. Allowlist check → `isinstance(presented, str) and any(hmac.compare_digest(...))`
   (constant-time; non-str email → 403 not a 500 path).
4. Catch `(ValueError, google_auth_exceptions.GoogleAuthError)` on verify (a JWKS
   `TransportError` currently propagates as 500; sibling collapses it to 401).
5. Explicit empty-token → 401 (currently empty falls through to verify→ValueError→401;
   explicit is cleaner and avoids calling verify with an empty string).
6. **Contract preserved**: returns the verified caller **email str** on success (NOT the
   claims dict — that difference from the sibling is the documented contract for the 9
   `_verify_caller_dep` wrappers, which type the result as `str`); same 401/403 trigger
   conditions.

## Tests (TDD, red first)

- NEW `tests/unit/test_verify_caller.py`, mirroring `test_oidc_caller.py`:
  happy→returns email; 401 missing/non-bearer/empty/ValueError/GoogleAuthError;
  403 not-in-allowlist; 403 non-str email (no 500); **403 detail does NOT echo the
  presented email** (the declared fix); 401 detail does NOT echo the exception text.
- Update the illustrative worker fakes (`deny_caller` 403 string ×8, `deny_audience`
  401 string ×3) to the new wording so the test suite stays self-consistent and nobody
  copies the echo pattern out of a fake. (Optional for green; done for hygiene.)

## Deploy

`driftscribe_lib/auth.py` is baked into 9 worker images. For a near-zero-risk hygiene
fix (403 only triggers for an *already-authenticated* but non-allowlisted SA hitting an
*internal-ingress* worker, leaking only that caller's own identity), recommend: **merge
now; let it roll out with future worker rebuilds; do NOT mass-redeploy 9 workers solely
for this.** Coordinator deploy not needed. Surface the choice to the operator.
