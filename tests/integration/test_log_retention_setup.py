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


def _read_script() -> str:
    return SETUP_SCRIPT.read_text()


def test_setup_script_extends_default_bucket_retention_to_365_days():
    body = _read_script()
    # The full command, possibly broken across continuation lines.
    pattern = re.compile(
        r"gcloud\s+logging\s+buckets\s+update\s+_Default[\s\S]*?--retention-days=365",
        re.MULTILINE,
    )
    assert pattern.search(body), (
        "expected `gcloud logging buckets update _Default ... --retention-days=365` "
        "in setup_secrets.sh"
    )


def test_setup_script_pins_default_bucket_location_to_global():
    """`_Default` lives in `--location=global` — pinning the location
    prevents `gcloud` from prompting interactively on first run."""
    body = _read_script()
    # Find the buckets-update block and confirm --location=global appears in it.
    match = re.search(
        r"(gcloud\s+logging\s+buckets\s+update\s+_Default[\s\S]*?)(?=\n# |\Z)",
        body,
    )
    assert match is not None
    assert "--location=global" in match.group(1)
