// secret_guard.ts — TS port of agent/secret_guard.py's redaction heuristic.
//
// SOURCE OF TRUTH: agent/secret_guard.py. These two regexes are duplicated
// verbatim from SECRET_NAME_PATTERN and _CREDENTIALED_URL. The parity test
// (tests/unit/secret_guard.test.ts) pins this port to the Python behaviour; if
// the Python heuristic changes, change BOTH the regex here and the test.
//
// Used by lib/diff.ts (displayDiffValue) so the operator UI redacts env-var
// values with EXACTLY the rule the backend renderer uses when it writes the
// GitHub PR/issue body — the inline env-diff card discloses nothing the operator
// couldn't already see by opening that artifact.

// Name-based: env var names that conventionally hold credentials. Includes
// URL/URI/CONNECTION because `DATABASE_URL=postgres://u:p@h/db` would otherwise
// render with the embedded password.
const SECRET_NAME =
  /(SECRET|TOKEN|KEY|PASSWORD|PASSWD|CRED|PRIVATE|AUTH|BEARER|JWT|SIGNATURE|SALT|DSN|OAUTH|URL|URI|CONNECTION|CONNSTR)/i;

// Value-based: URLs with userinfo (`scheme://user:pass@host`) are credentials
// regardless of the var's name.
const CREDENTIALED_URL = /\b[a-z][a-z0-9+.-]*:\/\/[^/@\s]*:[^/@\s]*@/i;

export function isSecretName(name: string): boolean {
  return SECRET_NAME.test(name);
}

export function valueLooksCredentialed(value: string | null | undefined): boolean {
  if (!value) return false;
  return CREDENTIALED_URL.test(value);
}

/** Combined: redact if the name is secret-like OR the value looks credentialed. */
export function shouldRedact(name: string, value: string | null | undefined): boolean {
  return isSecretName(name) || valueLooksCredentialed(value);
}
