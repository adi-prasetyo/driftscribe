"""Guards on the coordinator incremental-update Cloud Build deploy config.

`infra/cloudbuild.coordinator-update.yaml` is the narrow deploy path for the
LIVE coordinator (`driftscribe-agent`). Phase-3 (C5g carry-forward 6b) folds the
AVAILABILITY-CRITICAL IaC operator-auth/merge env into this config so a fresh
deploy can never silently drop them:

* ``COORDINATOR_ORIGIN`` — empty ⇒ EVERY approval POST is refused (CSRF check).
* ``IAC_REQUIRED_CHECKS`` — empty ⇒ the IaC merge is DISABLED (an unchecked
  head is never merged). Its value is itself comma-separated, so the env string
  must use gcloud's ``^@^`` custom-delimiter form (a plain comma would split it
  into bogus separate env vars).
* ``IAC_MERGE_METHOD`` — the squash default, codified for reproducibility.

Assertions operate on the YAML-parsed structure + the bash step's script text,
not arbitrary comments, so explanatory prose can't trip the guards.
"""
from pathlib import Path

import yaml

CONFIG = Path("infra/cloudbuild.coordinator-update.yaml")


def _load() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def _update_script() -> str:
    """The bash script of the `gcloud run services update` step."""
    data = _load()
    for step in data.get("steps", []):
        if step.get("entrypoint") != "bash":
            continue
        args = step.get("args", [])
        script = args[-1] if args else ""
        if "run services update" in script and "${_SERVICE}" in script:
            return str(script)
    raise AssertionError("no bash `gcloud run services update` step found")


def test_substitution_defaults() -> None:
    """The availability-critical IaC vars have non-empty, correct defaults."""
    subs = _load().get("substitutions", {})
    assert subs.get("_COORDINATOR_ORIGIN") == "https://driftscribe.adp-app.com"
    assert subs.get("_IAC_REQUIRED_CHECKS") == "static-gate,tofu,lint-test"
    assert subs.get("_IAC_MERGE_METHOD") == "squash"


def test_iac_env_vars_threaded_into_update() -> None:
    """COORDINATOR_ORIGIN / IAC_REQUIRED_CHECKS / IAC_MERGE_METHOD are wired into
    the --update-env-vars string from their substitutions, alongside CF Access."""
    script = _update_script()
    assert "COORDINATOR_ORIGIN=${_COORDINATOR_ORIGIN}" in script
    assert "IAC_REQUIRED_CHECKS=${_IAC_REQUIRED_CHECKS}" in script
    assert "IAC_MERGE_METHOD=${_IAC_MERGE_METHOD}" in script
    # CF Access env must still be present (not displaced by the new vars).
    assert "CF_ACCESS_TEAM_DOMAIN=${_CF_ACCESS_TEAM_DOMAIN}" in script
    assert "CF_ACCESS_AUD_TAG=${_CF_ACCESS_AUD_TAG}" in script


def test_custom_delimiter_used_for_comma_safe_value() -> None:
    """IAC_REQUIRED_CHECKS is comma-separated, so the env string MUST use the
    ``^@^`` custom delimiter and join pairs with ``@`` — not the default comma,
    which would split the value into spurious env vars."""
    script = _update_script()
    assert "ENV_VARS=\"^@^" in script, "env string must start with the ^@^ delimiter prefix"
    # Pairs are @-joined (not comma-joined).
    assert "${_CF_ACCESS_AUD_TAG}@COORDINATOR_ORIGIN=" in script
    # The optional TOFU_APPLY_URL append uses the same @ delimiter.
    assert "@TOFU_APPLY_URL=${_TOFU_APPLY_URL}" in script


def test_update_env_vars_consumes_the_delimited_string() -> None:
    """The built ENV_VARS string is passed through --update-env-vars (so the
    ^@^ prefix actually reaches gcloud)."""
    script = _update_script()
    assert '--update-env-vars="$${ENV_VARS}"' in script
