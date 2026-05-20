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


def redact_text(text: str | None) -> str | None:
    """Return ``text`` with credentialed URLs replaced.

    Targets the same pattern as :func:`value_looks_credentialed` —
    URLs of the form ``scheme://user:pass@host`` — but operates on
    arbitrary free-form strings (thought summaries, tool-result
    previews, MCP errors). Replaces only the userinfo segment so the
    URL stays parseable for the reader (host + path remain) but the
    secret is gone.
    """
    if not text:
        return text
    return _CREDENTIALED_URL.sub(
        lambda m: m.group(0).split(":", 1)[0] + "://<redacted>@", text
    )


# Metadata keys known never to carry secrets — passed through as-is.
# Adding to this allowlist is a security review decision, not a casual edit.
_SAFE_METADATA_KEYS: frozenset[str] = frozenset({
    "event", "trace_id", "workload", "tool_name", "mcp_tool", "mcp_server",
    "prompt_token_count", "candidates_token_count", "thoughts_token_count",
    "total_token_count", "doc_count", "latency_ms", "timestamp", "level",
    "logger", "result_ok", "insert_id",
})


def redact_dict(payload: dict | None) -> dict:
    """Key-aware shallow redaction for dicts (e.g. tool args).

    Applies :func:`should_redact` to each (k, v) pair so secret-keyed
    entries get replaced with '<redacted>'. Non-secret-keyed entries
    pass through as-is. Used at the boundary of structured tool args.
    """
    if not payload:
        return {}
    out: dict = {}
    for k, v in payload.items():
        s = v if isinstance(v, str) else None
        out[k] = "<redacted>" if should_redact(str(k), s) else v
    return out


def redact_event(payload: object) -> object:
    """Recursively redact every string in a structured log payload.

    Strings outside the metadata allowlist are run through
    :func:`redact_text`. Dicts redact the WHOLE value (regardless of
    type) when the KEY name looks secret-like — so
    `{"PASSWORD": {"raw": "abc"}}` becomes `{"PASSWORD": "<redacted>"}`,
    not `{"PASSWORD": {"raw": "abc"}}` after recursion. (Codex v3
    review CRITICAL: the previous version checked `should_redact`
    only on string values, letting structured-container secrets like
    `{"PASSWORD": {"raw": "abc"}}` leak through.) Lists recurse.
    Non-string scalars (int, float, bool, None) pass through.

    Call this BEFORE `_log.info(..., extra=...)` in the ADK event
    loop. Also call again at render time as defense-in-depth in case
    a future emit site forgets.
    """
    if isinstance(payload, dict):
        out: dict = {}
        for k, v in payload.items():
            if k in _SAFE_METADATA_KEYS:
                out[k] = v
                continue
            # KEY-name check FIRST — applies regardless of value type.
            # If the key looks secret-like, the value is gone, even
            # if it's a nested dict, a list, a number, or None.
            if is_secret_name(str(k)):
                out[k] = "<redacted>"
                continue
            out[k] = redact_event(v)
        return out
    if isinstance(payload, list):
        return [redact_event(v) for v in payload]
    if isinstance(payload, str):
        # Free-form string: strip credentialed-URL userinfo but keep
        # the rest. Full-mask happens only via the key-name check
        # above (so a secret-named container's whole value is gone).
        return redact_text(payload)
    return payload
