"""Pin the `_Default` log-bucket retention extension in setup_secrets.sh.

Single invariant: the bootstrap script invokes `gcloud logging buckets
update _Default` with `--retention-days=365`. We do NOT call gcloud
from the test — we parse the script body. The intent is a regression
guard: if a future edit drops or shortens this call, every DriftScribe
log line older than 30 days disappears silently.

365 days was chosen for hackathon scope: it's $0.01/GiB-mo beyond day
30, sits inside Cloud Logging's max-without-CMEK cap, and matches the
default table-expiration intent we'd want from any future BQ archive.
"""
from __future__ import annotations

import re
from pathlib import Path

SETUP_SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "setup_secrets.sh"

# Anchored pattern shared by the positive and negative tests.
#   ^\s*       — line must start with (optional) whitespace then `gcloud`,
#                so a `#`-prefixed comment line cannot match.
#   (?!\d)     — the digit right after `365` must NOT be another digit,
#                so a typo like `--retention-days=3650` cannot match.
# Anchors exist because Codex review of 133145c0 found both false-pass
# paths. Do not "simplify" them out — the negative tests below pin both.
_RETENTION_PATTERN = re.compile(
    r"^\s*gcloud\s+logging\s+buckets\s+update\s+_Default[\s\S]*?--retention-days=365(?!\d)",
    re.MULTILINE,
)


def _read_script() -> str:
    return SETUP_SCRIPT.read_text()


def test_setup_script_extends_default_bucket_retention_to_365_days():
    body = _read_script()
    # See _RETENTION_PATTERN — anchored to reject commented-out blocks
    # and digit-extended typos like `3650`.
    assert _RETENTION_PATTERN.search(body), (
        "expected `gcloud logging buckets update _Default ... --retention-days=365` "
        "in setup_secrets.sh"
    )


def test_setup_script_pins_default_bucket_location_to_global():
    """`_Default` lives in `--location=global` — pinning the location
    prevents `gcloud` from prompting interactively on first run."""
    body = _read_script()
    # `^\s*` rejects a commented-out block (a `#` line cannot match
    # `gcloud` after whitespace) — mirrors the anchoring rationale in
    # _RETENTION_PATTERN above.
    match = re.search(
        r"^\s*(gcloud\s+logging\s+buckets\s+update\s+_Default[\s\S]*?)(?=\n# |\Z)",
        body,
        re.MULTILINE,
    )
    assert match is not None
    assert "--location=global" in match.group(1)


def test_regex_rejects_commented_out_update_block():
    """Pin that the regex doesn't false-pass on a commented-out block —
    that's the historical bug (Codex review of 133145c0) the anchors fix."""
    commented = (
        "# gcloud logging buckets update _Default \\\n"
        "#   --project=\"$PROJECT\" \\\n"
        "#   --location=global \\\n"
        "#   --retention-days=365 >/dev/null\n"
    )
    assert _RETENTION_PATTERN.search(commented) is None


def test_regex_rejects_retention_days_3650():
    """`365` is a prefix of `3650`; without the digit-end anchor a typo
    would silently pass. This pins the negative-lookahead."""
    typo = "gcloud logging buckets update _Default --retention-days=3650"
    assert _RETENTION_PATTERN.search(typo) is None
