"""Static HCL gate for DriftScribe infra PRs (design doc §5.1).

Pre-`tofu init` policy that closes the PR-controlled-HCL code-execution
surface: agent-authored infra PRs may only touch ``iac/``, may not add
providers, may not declare modules, and may not contain provisioners or
other arbitrary-execution constructs. Pure functions here; the CLI wrapper
(``python -m tools.iac_static_gate``) supplies the git diff in CI.

JSON-syntax HCL (``.tf.json``/``.tofu.json``) is NOT structurally analyzed in
v1 — ``hcl2.loads`` only parses native-syntax HCL. Such files remain
hard-rejected in agent mode via the ``disallowed-file-type`` rule; in operator
mode JSON config is governed by human review + CODEOWNERS (design §5.1). The
CLI therefore reads only ``.tf`` content (see ``_HCL_CONTENT_SUFFIXES``).
"""
from __future__ import annotations

import argparse
import enum
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

import hcl2

# Built-in pseudo-providers OpenTofu/Terraform resolve without a source — they
# are not real external providers and must not trip the allowlist.
BUILTIN_PROVIDERS = frozenset({"terraform", "tofu"})

# hcl2 8.x is a lossy round-trip: block *labels* arrive as keys wrapped in
# literal double-quotes (e.g. ``'"google_x"'``) and string *values* arrive
# quote-wrapped too (e.g. ``'"hashicorp/google"'``). It also injects synthetic
# dunder-metadata keys into parsed blocks — ``__is_block__`` always, plus
# ``__comments__``/``__inline_comments__`` (and ``__start_line__``/
# ``__end_line__`` on some shapes) WHENEVER the source contains comments. None
# of these are semantic names: iterating a block's keys as provider/resource/
# module names must skip them all, or a commented foundation file yields false
# positives (a provider literally named ``__inline_comments__``). The helpers
# below normalize labels and filter every ``__dunder__`` meta key.


def _is_meta_key(key: str) -> bool:
    """True for an hcl2-injected dunder-metadata key (``__is_block__``,
    ``__comments__``, ``__inline_comments__``, ``__start_line__``, …).

    Scoped deliberately narrowly to the ``__...__`` shape: real HCL
    identifiers (provider/resource/module/data names) cannot be a leading- and
    trailing-double-underscore token, so this never masks a real disallowed
    provider/module/resource — it only drops hcl2's own metadata.
    """
    return isinstance(key, str) and key.startswith("__") and key.endswith("__")


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

# Resource types whose entire purpose (paired with provisioners) is to run
# arbitrary commands during apply — banned outright regardless of body.
ARBITRARY_EXECUTION_RESOURCE_TYPES = frozenset({"null_resource", "terraform_data"})
# Nested block keys inside a resource body that smuggle execution.
ARBITRARY_EXECUTION_BLOCK_KEYS = frozenset({"provisioner", "connection"})
# `data "<type>"` sources that read outside the declared config (command
# execution / cross-state read).
FORBIDDEN_DATA_SOURCE_TYPES = frozenset({"external", "terraform_remote_state"})
# `dynamic` blocks are banned in v1: a `dynamic "provisioner"` would smuggle
# execution past a naive key check (design §5.1).
DYNAMIC_BLOCK_KEY = "dynamic"


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
    hcl_files: dict[str, str]                # path -> file content, native-syntax iac/*.tf


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
                if _is_meta_key(name):
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
            if _is_meta_key(label):
                continue
            found.append((_block_label(label), None))

    return found


def _body_has_block(body: dict, key: str) -> bool:
    """True if a (possibly nested) block body contains a block of ``key`` at
    any depth.

    hcl2 nests block bodies as lists of dicts (e.g. ``dynamic`` content,
    ``provisioner`` bodies). We recurse through every nested dict so a
    forbidden construct hidden inside another block (e.g. a ``provisioner``
    inside a ``dynamic ... content``) is still caught.

    Intentional over-approximation: this treats a scalar *attribute* named
    ``provisioner``/``connection``/``dynamic`` as if it were a block. Those are
    reserved block names in HCL, so a real config never uses them as plain
    attributes; over-rejecting here is the safe direction for a security gate.
    """
    if not isinstance(body, dict):
        return False
    if key in body:
        return True
    for k, v in body.items():
        if _is_meta_key(k):
            continue
        if isinstance(v, dict):
            if _body_has_block(v, key):
                return True
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict) and _body_has_block(item, key):
                    return True
    return False


def _iter_typed_blocks(parsed: dict, kind: str):
    """Yield ``(type, body)`` for each ``resource``/``data`` block.

    Shape: ``kind: [ { '"type"': { '"name"': {body...} } } ]``. Labels are
    quote-wrapped; the body is the innermost dict. A block may carry multiple
    names under one type, and a file may repeat a type across blocks.
    """
    for block in _iter_blocks(parsed, kind):
        for type_label, by_name in block.items():
            if _is_meta_key(type_label):
                continue
            rtype = _block_label(type_label)
            if not isinstance(by_name, dict):
                continue
            for name_label, body in by_name.items():
                if _is_meta_key(name_label):
                    continue
                yield rtype, (body if isinstance(body, dict) else {})


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
            # (a spoofed `google = { source = "evil/google" }` is rejected). By
            # design (v1), an allowed provider with NO source declared passes —
            # `tofu init -lockfile=readonly` is the guard that an unpinned/new
            # provider can't actually be resolved in CI.
            required = REQUIRED_PROVIDER_SOURCES.get(name)
            if source is not None and required is not None and source != required:
                violations.append(
                    Violation(
                        "disallowed-provider-source",
                        f"{path}: provider {name!r} source {source!r} != {required!r}",
                    )
                )

        # Module ban (v1): any `module` block at all is forbidden. Banning only
        # remote modules would force recursive parsing of local modules to
        # enforce the same rules inside them (design §5.1); v1 bans all modules.
        for module_block in _iter_blocks(parsed, "module"):
            for label in module_block:
                if _is_meta_key(label):
                    continue
                violations.append(
                    Violation(
                        "module-block-forbidden",
                        f"{path}: module {_block_label(label)!r} (all modules forbidden in v1)",
                    )
                )

        # Arbitrary-execution / dynamic-block ban on resource blocks.
        for rtype, body in _iter_typed_blocks(parsed, "resource"):
            if rtype in ARBITRARY_EXECUTION_RESOURCE_TYPES:
                violations.append(
                    Violation(
                        "arbitrary-execution",
                        f"{path}: resource type {rtype!r} (command-execution resource)",
                    )
                )
            for key in ARBITRARY_EXECUTION_BLOCK_KEYS:
                if _body_has_block(body, key):
                    violations.append(
                        Violation(
                            "arbitrary-execution",
                            f"{path}: resource {rtype!r} contains a {key!r} block",
                        )
                    )
            if _body_has_block(body, DYNAMIC_BLOCK_KEY):
                violations.append(
                    Violation(
                        "dynamic-block-forbidden",
                        f"{path}: resource {rtype!r} contains a 'dynamic' block",
                    )
                )

        # Forbidden data sources (command execution / cross-state read).
        for dtype, _body in _iter_typed_blocks(parsed, "data"):
            if dtype in FORBIDDEN_DATA_SOURCE_TYPES:
                violations.append(
                    Violation(
                        "forbidden-data-source",
                        f"{path}: data source {dtype!r} is forbidden",
                    )
                )

    return violations


# Suffixes whose content the CLI reads + hands to evaluate for structural
# checks. Only native-syntax HCL is parsed: hcl2.loads cannot parse JSON-syntax
# HCL (.tf.json/.tofu.json), so reading those would only ever yield a spurious
# hcl-parse-error. JSON-syntax HCL is out of scope for v1 structural analysis
# (see module docstring) — it stays hard-rejected in agent mode via
# disallowed-file-type, and operator-mode JSON config is governed by human
# review + CODEOWNERS (design §5.1). .tfvars/.tofu.json etc. are likewise caught
# by the path/file-type rules without needing their content.
_HCL_CONTENT_SUFFIXES = (".tf",)


def _git_diff_names(base: str, head: str, *, cwd: str | None = None) -> tuple[str, ...]:
    """Return changed repo-relative paths between two commits.

    Uses ``git diff --name-only base...head`` (three-dot: changes on the head
    side since the merge-base), matching how a PR diff is computed in CI.

    ``-c core.quotePath=false`` disables git's default C-style quoting of
    non-ASCII bytes — without it a path like ``iac/café.tf`` comes back as the
    literal ``"iac/caf\303\251.tf"`` and the subsequent ``git show`` reads the
    wrong path, leaving the file's content unscanned (a gate bypass). ``-z``
    NUL-delimits names so paths containing spaces or newlines survive too.
    """
    out = subprocess.run(
        ["git", "-c", "core.quotePath=false", "diff", "--name-only", "-z",
         f"{base}...{head}"],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout
    return tuple(name for name in out.split("\0") if name)


def _git_show(head: str, path: str, *, cwd: str | None = None) -> str | None:
    """Return the content of ``path`` at ``head``, or ``None`` if it does not
    exist there (e.g. the change deleted it). Deleted files have no content
    to gate, so they are simply omitted from ``hcl_files``.

    ``-c core.quotePath=false`` keeps the pathspec interpretation consistent
    with :func:`_git_diff_names` for non-ASCII names.
    """
    proc = subprocess.run(
        ["git", "-c", "core.quotePath=false", "show", f"{head}:{path}"],
        cwd=cwd, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: git diff -> evaluate -> exit code.

    ``--mode`` is taken explicitly (CI derives it from the PR label/branch);
    passing it in keeps the gate testable. Reads the post-change content of
    changed ``iac/`` HCL files at ``--head`` and runs :func:`evaluate`. Prints
    each violation and returns ``1`` if any, else ``0``.
    """
    parser = argparse.ArgumentParser(
        prog="python -m tools.iac_static_gate",
        description="Static HCL gate for DriftScribe infra PRs (design doc §5.1).",
    )
    parser.add_argument("--base", required=True, help="base commit SHA (merge-base side)")
    parser.add_argument("--head", required=True, help="head commit SHA (PR branch tip)")
    parser.add_argument(
        "--mode", required=True, choices=[m.value for m in GateMode],
        help="agent (strict) or operator (foundation edits allowed)",
    )
    args = parser.parse_args(argv)

    mode = GateMode(args.mode)
    changed_paths = _git_diff_names(args.base, args.head)

    # Read content only for changed iac/ HCL files that still exist at head.
    hcl_files: dict[str, str] = {}
    for p in changed_paths:
        if not p.startswith(IAC_PREFIX):
            continue
        if not p.endswith(_HCL_CONTENT_SUFFIXES):
            continue
        content = _git_show(args.head, p)
        if content is not None:
            hcl_files[p] = content

    violations = evaluate(GateInput(mode=mode, changed_paths=changed_paths, hcl_files=hcl_files))

    if violations:
        print(f"iac static gate: {len(violations)} violation(s) (mode={mode.value}):")
        for v in violations:
            print(f"  [{v.rule}] {v.detail}")
        return 1

    print(f"iac static gate: PASS (mode={mode.value}, {len(changed_paths)} changed path(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
