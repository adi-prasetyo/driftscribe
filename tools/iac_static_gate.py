"""Static HCL gate for DriftScribe infra PRs (design doc §5.1).

Pre-`tofu init` policy that closes the PR-controlled-HCL code-execution
surface: agent-authored infra PRs may only touch ``iac/``, may not add
providers, may not declare modules, and may not contain provisioners or
other arbitrary-execution constructs. Pure functions here; the CLI wrapper
(``python -m tools.iac_static_gate``) supplies the git diff in CI.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any

import hcl2

# Built-in pseudo-providers OpenTofu/Terraform resolve without a source — they
# are not real external providers and must not trip the allowlist.
BUILTIN_PROVIDERS = frozenset({"terraform", "tofu"})

# hcl2 8.x is a lossy round-trip: block *labels* arrive as keys wrapped in
# literal double-quotes (e.g. ``'"google_x"'``) and string *values* arrive
# quote-wrapped too (e.g. ``'"hashicorp/google"'``). Each block body also gets
# a synthetic ``__is_block__`` sentinel key. The helpers below normalize all of
# that so the rule logic deals in bare identifiers/strings.
_BLOCK_SENTINEL = "__is_block__"

IAC_PREFIX = "iac/"

# Foundation files only the operator/bootstrap mode may touch (Codex rev: must
# cover everything that sets authority — backend, encryption, provider
# project/region/creds, variables, and import targets — or an agent PR could
# redirect the project without touching versions.tf).
PROTECTED_FOUNDATION = (
    "iac/.terraform.lock.hcl",
    "iac/versions.tf",      # backend + encryption config
    "iac/providers.tf",     # provider project/region/credentials
    "iac/variables.tf",     # variable definitions/defaults
    "iac/imports.tf",       # import targets
)

# In AGENT mode, ONLY plain `.tf` files (and `.md` READMEs) may be changed under
# iac/. Everything else OpenTofu also loads is a bypass surface and is rejected
# outright (Codex rev): `.tofu`/`.tofu.json` (and `.tofu` OVERRIDES a same-named
# `.tf`), `.tf.json`, and any `*.tfvars`/`*.auto.tfvars` (auto-loaded variable
# values).
ALLOWED_AGENT_SUFFIX = ".tf"
ALLOWED_AGENT_DOC_SUFFIX = ".md"
REJECTED_IAC_SUFFIXES = (".tofu", ".tofu.json", ".tf.json", ".tfvars")  # + *.auto.tfvars

ALLOWED_PROVIDERS = frozenset({"google"})  # + builtin (terraform/tofu) names
# When a provider is declared with a source, it must be the canonical one
# (Codex rev: name-only check lets `google = { source = "evil/google" }` pass).
REQUIRED_PROVIDER_SOURCES = {"google": "hashicorp/google"}


class GateMode(enum.Enum):
    AGENT = "agent"        # driftscribe-infra label + infra/ branch — strict rules
    OPERATOR = "operator"  # human-authored bootstrap — foundation edits allowed


@dataclass(frozen=True)
class Violation:
    rule: str       # short machine id, e.g. "path-outside-iac"
    detail: str     # human message (file/line/context)


@dataclass(frozen=True)
class GateInput:
    mode: GateMode
    changed_paths: tuple[str, ...]           # repo-relative, from `git diff --name-only`
    hcl_files: dict[str, str]                # path -> file content, only iac/*.tf{,.json}


def _is_disallowed_iac_suffix(path: str) -> bool:
    """True if an ``iac/`` path is a file type OpenTofu loads but the gate
    bans in AGENT mode.

    The longer compound suffixes (``.tofu.json``/``.tf.json``) and the
    ``*.auto.tfvars`` glob are checked explicitly so they win over the
    generic ``.tf``/``.json`` logic. Anything that is not a plain ``.tf``
    or a ``.md`` README is rejected.
    """
    if path.endswith(".auto.tfvars"):
        return True
    if path.endswith(REJECTED_IAC_SUFFIXES):
        return True
    # Allowlist: only plain `.tf` and `.md` survive.
    return not (path.endswith(ALLOWED_AGENT_SUFFIX) or path.endswith(ALLOWED_AGENT_DOC_SUFFIX))


def _unwrap(value: Any) -> Any:
    """Strip the literal surrounding double-quotes hcl2 8.x leaves on string
    scalars and block labels (``'"hashicorp/google"'`` -> ``hashicorp/google``).

    Non-strings (and strings without the wrapping quotes) pass through
    unchanged so the helper is safe to call on any label/value.
    """
    if isinstance(value, str) and len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def _block_label(key: str) -> str:
    """Normalize a block-label dict key (a quote-wrapped string) to its bare
    identifier."""
    return _unwrap(key)


def _parse(path: str, content: str) -> dict | None:
    """Parse HCL via hcl2, returning the dict or ``None`` on any failure.

    Fail-closed: a parse error must surface as an ``hcl-parse-error``
    Violation, never an exception, so the caller records the violation and
    skips structural checks for this file. We catch broadly (lark raises
    several exception types) so no malformed input can crash the gate.
    """
    try:
        return hcl2.loads(content)
    except Exception:  # noqa: BLE001 - fail-closed: any parse failure is a violation
        return None


def _iter_blocks(parsed: dict, kind: str) -> list[dict]:
    """Return the list of top-level blocks of a given kind (``resource``,
    ``data``, ``provider``, ``module``, ``terraform``).

    hcl2 represents repeated top-level blocks as a list of dicts; a missing
    kind yields an empty list.
    """
    blocks = parsed.get(kind)
    if blocks is None:
        return []
    if isinstance(blocks, list):
        return [b for b in blocks if isinstance(b, dict)]
    if isinstance(blocks, dict):
        return [blocks]
    return []


def _collect_providers(parsed: dict) -> list[tuple[str, str | None]]:
    """Collect declared providers as ``(name, source_or_None)`` pairs from
    both ``terraform.required_providers`` and top-level ``provider`` blocks.

    ``required_providers`` keys are bare identifiers mapping to a body that
    may carry a ``source``. Top-level ``provider "<name>"`` block labels are
    quote-wrapped and carry no source. Builtins are excluded by the caller.
    """
    found: list[tuple[str, str | None]] = []

    # (a) terraform { required_providers { <name> = { source = ... } } }
    for tf_block in _iter_blocks(parsed, "terraform"):
        for rp in _iter_blocks(tf_block, "required_providers"):
            for name, body in rp.items():
                if name == _BLOCK_SENTINEL:
                    continue
                source = None
                if isinstance(body, dict):
                    raw = body.get("source")
                    if raw is not None:
                        source = _unwrap(raw)
                found.append((name, source))

    # (b) top-level provider "<name>" { ... } — label is quote-wrapped, no source
    for prov_block in _iter_blocks(parsed, "provider"):
        for label in prov_block:
            if label == _BLOCK_SENTINEL:
                continue
            found.append((_block_label(label), None))

    return found


def evaluate(gi: GateInput) -> list[Violation]:
    """Return all violations (empty = pass).

    Fail-closed: a parse error is a Violation, not an exception.
    """
    violations: list[Violation] = []

    for p in gi.changed_paths:
        # The gate only governs `iac/`. In AGENT mode anything outside is a
        # violation; in OPERATOR mode it's simply out of scope (CODEOWNERS
        # governs e.g. `.github/`), so skip it entirely.
        if not p.startswith(IAC_PREFIX):
            if gi.mode is GateMode.AGENT:
                violations.append(Violation("path-outside-iac", p))
            continue

        if gi.mode is GateMode.AGENT:
            # Foundation files (lockfile/backend/encryption/provider/vars/
            # imports) are operator-only. Checked before the file-type rule
            # so the lockfile (not a `.tf`) reports as a foundation edit.
            if p in PROTECTED_FOUNDATION:
                violations.append(Violation("foundation-edit-agent-mode", p))
                continue
            if _is_disallowed_iac_suffix(p):
                violations.append(Violation("disallowed-file-type", p))
                continue

    # Content checks run in BOTH modes against the supplied HCL files (provider
    # declarations legitimately live in foundation files; a clean operator PR
    # with google/hashicorp/google passes). Fail-closed on parse errors.
    for path, content in gi.hcl_files.items():
        parsed = _parse(path, content)
        if parsed is None:
            violations.append(Violation("hcl-parse-error", path))
            continue

        for name, source in _collect_providers(parsed):
            if name in BUILTIN_PROVIDERS:
                continue
            if name not in ALLOWED_PROVIDERS:
                violations.append(
                    Violation("disallowed-provider", f"{path}: provider {name!r} not allowlisted")
                )
                continue
            # Allowed name: if it declares a source it must be the canonical one
            # (a spoofed `google = { source = "evil/google" }` is rejected).
            required = REQUIRED_PROVIDER_SOURCES.get(name)
            if source is not None and required is not None and source != required:
                violations.append(
                    Violation(
                        "disallowed-provider-source",
                        f"{path}: provider {name!r} source {source!r} != {required!r}",
                    )
                )

    return violations
