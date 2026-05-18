"""Single source of truth for what counts as a 'secret-like' env var name OR value.

Used by:
- agent.validator (to refuse docs_pr proposals that would document a secret)
- agent.renderer (to redact values in PR/issue bodies for secret-named vars
  OR for vars whose values look like credentials)
"""

import re

# Name-based heuristic: env var names that conventionally hold credentials.
# Includes URL/URI/CONNECTION because `DATABASE_URL=postgres://u:p@host/db`
# would otherwise render with the embedded password.
SECRET_NAME_PATTERN = re.compile(
    r"(SECRET|TOKEN|KEY|PASSWORD|PASSWD|CRED|PRIVATE|AUTH|BEARER|JWT|SIGNATURE"
    r"|SALT|DSN|OAUTH|URL|URI|CONNECTION|CONNSTR)",
    re.IGNORECASE,
)

# Value-based heuristic: URLs with userinfo (`scheme://user:pass@host`),
# which are credentials regardless of the var's name.
_CREDENTIALED_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^/@\s]*:[^/@\s]*@", re.IGNORECASE)


def is_secret_name(name: str) -> bool:
    return bool(SECRET_NAME_PATTERN.search(name))


def value_looks_credentialed(value: str | None) -> bool:
    """True if the value resembles a credential (e.g. URL with embedded auth)."""
    if not value:
        return False
    return bool(_CREDENTIALED_URL.search(value))


def should_redact(name: str, value: str | None) -> bool:
    """Combined check: redact if name is secret-like OR value looks credentialed."""
    return is_secret_name(name) or value_looks_credentialed(value)
