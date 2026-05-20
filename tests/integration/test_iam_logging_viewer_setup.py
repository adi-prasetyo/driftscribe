"""Pin the `roles/logging.viewer` IAM grant in setup_secrets.sh.

Single invariant: the bootstrap script binds `roles/logging.viewer` to
the coordinator runtime SA (`$COORD_SA`) so the `/trace` endpoint can
call `logEntries.list` from Cloud Run. We do NOT call gcloud from the
test — we parse the script body. The intent is a regression guard: if
a future edit drops or weakens this grant, every `/trace` request from
Cloud Run silently 403s (works locally under ADC, fails in prod).

`roles/logging.viewer` is the smallest role that grants
`logging.logEntries.list` + `logging.logs.list` project-wide and is
strictly read-only — pinning the exact role keeps a future "let's just
use logging.admin" drift out of the bootstrap.
"""
from __future__ import annotations

import re
from pathlib import Path

SETUP_SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "setup_secrets.sh"

# Anchored pattern shared by the positive and negative tests.
#   ^\s*role=     — line must start with (optional) whitespace then
#                   `role=`, so a `#`-prefixed comment cannot match.
#   "roles/logging\.viewer"
#                 — the exact role string. The escaped dot rejects a
#                   would-be `roles/loggingxviewer` typo.
#   (?!\w)        — the character right after `viewer` must NOT extend
#                   the identifier, so a stricter role like
#                   `roles/logging.viewerWithEverything` cannot match.
# Anchors mirror the rationale in test_log_retention_setup.py:
# false-pass paths from commented-out blocks and identifier-prefix
# overlap. Do not "simplify" them out — the negative tests below pin
# both.
_ROLE_PATTERN = re.compile(
    r'^\s*role="roles/logging\.viewer"(?!\w)',
    re.MULTILINE,
)

# Pin the describe-then-act grant block: the `add-iam-policy-binding`
# call MUST reference the same `$role` variable (not a hardcoded
# duplicate string that could drift) and include `--condition=None`
# (otherwise gcloud emits an unbound-condition warning on projects with
# any existing conditional binding).
_GRANT_PATTERN = re.compile(
    r"^\s*gcloud\s+projects\s+add-iam-policy-binding\s+\"\$PROJECT\""
    r"[\s\S]*?--role=\"\$\{role\}\""
    r"[\s\S]*?--condition=None",
    re.MULTILINE,
)


def _read_script() -> str:
    return SETUP_SCRIPT.read_text()


def test_setup_script_pins_logging_viewer_role():
    body = _read_script()
    # See _ROLE_PATTERN — anchored to reject commented-out blocks and
    # identifier-prefix overlap like `roles/logging.viewerSomethingElse`.
    assert _ROLE_PATTERN.search(body), (
        'expected `role="roles/logging.viewer"` assignment in setup_secrets.sh'
    )


def test_setup_script_grants_logging_viewer_to_coord_sa():
    """The grant must target the coordinator runtime SA (`$COORD_SA`)
    — workers and humans get no project-wide logging read role."""
    body = _read_script()
    # `sa_email="${COORD_SA}"` is the indirection: pin both that the
    # role variable points at `$COORD_SA` and that the binding uses
    # `$sa_email` (so changing the source variable propagates).
    assert re.search(
        r'^\s*sa_email="\$\{COORD_SA\}"',
        body,
        re.MULTILINE,
    ), 'expected `sa_email="${COORD_SA}"` in setup_secrets.sh'
    assert "--member=\"serviceAccount:${sa_email}\"" in body, (
        "expected the IAM binding to use the ${sa_email} variable"
    )


def test_setup_script_uses_describe_then_act_for_logging_viewer():
    """Pin the idempotency guard: the script MUST check for the
    existing binding before calling `add-iam-policy-binding`, otherwise
    re-runs produce noisy duplicate-grant lines instead of the
    deterministic skip message the rest of the script enforces."""
    body = _read_script()
    # The lookup uses `get-iam-policy` + the role/member filter from
    # the plan pseudocode. Pin the filter shape so a future edit can't
    # silently downgrade it to a full-policy fetch.
    assert re.search(
        r"gcloud\s+projects\s+get-iam-policy\s+\"\$PROJECT\""
        r"[\s\S]*?--filter=\"bindings\.role=\$\{role\}\s+AND\s+"
        r"bindings\.members=serviceAccount:\$\{sa_email\}\"",
        body,
    ), "expected describe-then-act lookup with role+member filter"

    # And pin the grant call uses `--condition=None` (see _GRANT_PATTERN
    # docstring for the rationale).
    assert _GRANT_PATTERN.search(body), (
        "expected `add-iam-policy-binding ... --role=\"${role}\" ... --condition=None`"
    )


def test_setup_script_emits_skip_message_for_existing_binding():
    """Pin the operator-facing skip message so re-runs are visibly
    idempotent — matches the pattern from §11 (log retention)."""
    body = _read_script()
    assert "logging.viewer: already bound to ${sa_email} — skipping" in body


def test_regex_rejects_commented_out_role_assignment():
    """Pin that the regex doesn't false-pass on a commented-out block —
    the same anchoring class as test_log_retention_setup.py."""
    commented = (
        '# role="roles/logging.viewer"\n'
        '#   gcloud projects add-iam-policy-binding ...\n'
    )
    assert _ROLE_PATTERN.search(commented) is None


def test_regex_rejects_logging_admin_role():
    """`logging.viewer` is strictly read-only; pin the role so a
    future edit can't silently upgrade it to `logging.admin` (which
    would let a compromised coordinator delete sinks)."""
    upgraded = 'role="roles/logging.admin"'
    assert _ROLE_PATTERN.search(upgraded) is None


def test_regex_rejects_identifier_prefix_overlap():
    """`viewer` is a prefix of any hypothetical `viewerSomethingElse`
    role name; without the `(?!\\w)` anchor a stricter role would
    silently pass. This pins the negative-lookahead."""
    overlap = 'role="roles/logging.viewerWithEverything"'
    assert _ROLE_PATTERN.search(overlap) is None
