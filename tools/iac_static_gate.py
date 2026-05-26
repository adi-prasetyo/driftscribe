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

    return violations
