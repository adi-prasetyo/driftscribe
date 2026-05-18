"""Single source of truth for what counts as a 'secret-like' env var name.

Used by:
- agent.validator (to refuse docs_pr proposals that would document a secret)
- agent.renderer (to redact values in PR/issue bodies for secret-named vars)
"""

import re

SECRET_NAME_PATTERN = re.compile(
    r"(SECRET|TOKEN|KEY|PASSWORD|PASSWD|CRED|PRIVATE|AUTH|BEARER|JWT|SIGNATURE|SALT|DSN|OAUTH)",
    re.IGNORECASE,
)


def is_secret_name(name: str) -> bool:
    return bool(SECRET_NAME_PATTERN.search(name))
