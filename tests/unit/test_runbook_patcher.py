import pytest
from agent.runbook_patcher import patch_runbook
from agent.contract import OpsContract, EnvVarRule, DocsRef
from agent.models import EnvDiff, ContractStatus


def _contract(extra=None):
    rules = {
        "FEATURE_NEW_CHECKOUT": EnvVarRule(
            value="false",
            docs=DocsRef(file="demo/docs/runbook.md", section="Feature Flags"),
            allow_manual_change=True,
            operator_note="Operator-toggleable: enables the new checkout flow.",
        ),
    }
    if extra:
        rules.update(extra)
    return OpsContract(
        service="payment-demo",
        environment="production",
        cloud_run_service="payment-demo",
        region="asia-northeast1",
        github_repo="theghostsquad00/driftscribe",
        expected_env=rules,
    )


STARTING_RUNBOOK = """\
# payment-demo Runbook

## Runtime Configuration

- `PAYMENT_MODE=mock` controls real vs mock payments.

## Feature Flags

- `FEATURE_NEW_CHECKOUT=false` — **Operator note:** Operator-toggleable. Enables the new checkout flow. Safe to flip without a redeploy.
"""


def test_patch_updates_value_in_section():
    diff = EnvDiff(
        name="FEATURE_NEW_CHECKOUT",
        expected="false",
        live="true",
        contract_status=ContractStatus.PRESENT_ALLOW_MANUAL,
    )
    new = patch_runbook(STARTING_RUNBOOK, [diff], _contract())
    assert "FEATURE_NEW_CHECKOUT=true" in new
    assert "FEATURE_NEW_CHECKOUT=false" not in new
    assert "## Feature Flags" in new
    assert "operator" in new.lower()


def test_patch_is_idempotent_when_already_up_to_date():
    diff = EnvDiff(
        name="FEATURE_NEW_CHECKOUT",
        expected="false",
        live="false",
        contract_status=ContractStatus.MATCH,
    )
    new = patch_runbook(STARTING_RUNBOOK, [diff], _contract())
    assert new == STARTING_RUNBOOK


def test_patch_runs_twice_produces_same_output():
    # Applying the same drift twice must converge
    diff = EnvDiff(
        name="FEATURE_NEW_CHECKOUT",
        expected="false",
        live="true",
        contract_status=ContractStatus.PRESENT_ALLOW_MANUAL,
    )
    once = patch_runbook(STARTING_RUNBOOK, [diff], _contract())
    twice = patch_runbook(once, [diff], _contract())
    assert once == twice


def test_patch_appends_new_var_to_section_if_missing():
    diff = EnvDiff(
        name="FEATURE_NEW_CHECKOUT",
        expected=None,
        live="true",
        contract_status=ContractStatus.ABSENT,
        recent_pr_match="https://github.com/x/x/pull/1",
    )
    minimal_runbook = "# Runbook\n\n## Feature Flags\n\n(none yet)\n"
    new = patch_runbook(minimal_runbook, [diff], _contract())
    assert "FEATURE_NEW_CHECKOUT=true" in new


def test_patch_creates_section_stub_when_section_missing_entirely():
    diff = EnvDiff(
        name="FEATURE_NEW_CHECKOUT",
        expected="false",
        live="true",
        contract_status=ContractStatus.PRESENT_ALLOW_MANUAL,
    )
    no_section_runbook = "# Runbook\n\n(empty)\n"
    new = patch_runbook(no_section_runbook, [diff], _contract())
    assert "## Feature Flags" in new
    assert "FEATURE_NEW_CHECKOUT=true" in new


def test_patch_handles_multiple_vars_in_same_section():
    extra = {
        "FEATURE_BETA_UI": EnvVarRule(
            value="false",
            docs=DocsRef(file="demo/docs/runbook.md", section="Feature Flags"),
            allow_manual_change=True,
            operator_note="Beta UI toggle.",
        ),
    }
    contract = _contract(extra)
    runbook = """\
# Runbook

## Feature Flags

- `FEATURE_NEW_CHECKOUT=false` — **Operator note:** Operator-toggleable.
- `FEATURE_BETA_UI=false` — **Operator note:** Beta UI toggle.
"""
    diffs = [
        EnvDiff(
            name="FEATURE_NEW_CHECKOUT",
            expected="false",
            live="true",
            contract_status=ContractStatus.PRESENT_ALLOW_MANUAL,
        ),
        EnvDiff(
            name="FEATURE_BETA_UI",
            expected="false",
            live="true",
            contract_status=ContractStatus.PRESENT_ALLOW_MANUAL,
        ),
    ]
    new = patch_runbook(runbook, diffs, contract)
    assert "FEATURE_NEW_CHECKOUT=true" in new
    assert "FEATURE_BETA_UI=true" in new
    # Neither replaced the other
    assert new.count("`FEATURE_NEW_CHECKOUT=") == 1
    assert new.count("`FEATURE_BETA_UI=") == 1


def test_patch_preserves_empty_string_value():
    # An empty live value (operator unset the flag) must still be patched in
    diff = EnvDiff(
        name="FEATURE_NEW_CHECKOUT",
        expected="false",
        live="",
        contract_status=ContractStatus.PRESENT_ALLOW_MANUAL,
    )
    new = patch_runbook(STARTING_RUNBOOK, [diff], _contract())
    assert "FEATURE_NEW_CHECKOUT=" in new
    # Old value must be gone
    assert "FEATURE_NEW_CHECKOUT=false" not in new


def test_patch_refuses_to_write_secret_named_var():
    # Defense-in-depth: the validator should block these, but if one slips
    # through, the patcher must not write the value into the runbook.
    extra = {
        "API_TOKEN": EnvVarRule(
            value="placeholder",
            docs=DocsRef(file="demo/docs/runbook.md", section="Runtime Configuration"),
            allow_manual_change=True,
            operator_note="rotate quarterly",
        ),
    }
    diff = EnvDiff(
        name="API_TOKEN",
        expected="placeholder",
        live="actual-secret-value",
        contract_status=ContractStatus.PRESENT_ALLOW_MANUAL,
    )
    with pytest.raises(ValueError, match="secret"):
        patch_runbook(STARTING_RUNBOOK, [diff], _contract(extra))


def test_patch_does_not_treat_fenced_code_as_section_header():
    # A literal "## Feature Flags" inside a fenced example must not be
    # mistaken for the real section
    runbook = """\
# Runbook

## Examples

```markdown
## Feature Flags
- this is just an example
```

## Feature Flags

- `FEATURE_NEW_CHECKOUT=false` — **Operator note:** Operator-toggleable.
"""
    diff = EnvDiff(name="FEATURE_NEW_CHECKOUT", expected="false", live="true",
                   contract_status=ContractStatus.PRESENT_ALLOW_MANUAL)
    new = patch_runbook(runbook, [diff], _contract())
    # Real section was patched
    assert "FEATURE_NEW_CHECKOUT=true" in new
    # Fenced example is intact
    assert "```markdown" in new
    assert "this is just an example" in new
    # Fence wasn't broken open
    assert new.count("```") == 2


def test_patch_is_atomic_on_secret_failure():
    # If diffs[1] is a secret-named var, diffs[0] must NOT be partially applied.
    # The atomic pre-check raises BEFORE any diff is applied.
    extra = {
        "API_TOKEN": EnvVarRule(
            value="placeholder",
            docs=DocsRef(file="demo/docs/runbook.md", section="Runtime Configuration"),
            allow_manual_change=True,
            operator_note="rotate quarterly",
        ),
    }
    diffs = [
        EnvDiff(name="FEATURE_NEW_CHECKOUT", expected="false", live="true",
                contract_status=ContractStatus.PRESENT_ALLOW_MANUAL),
        EnvDiff(name="API_TOKEN", expected="placeholder", live="oops",
                contract_status=ContractStatus.PRESENT_ALLOW_MANUAL),
    ]
    with pytest.raises(ValueError, match="secret"):
        patch_runbook(STARTING_RUNBOOK, diffs, _contract(extra))


def test_sanitize_operator_note_collapses_newlines():
    # Defense-in-depth: if a multiline operator_note somehow gets past the
    # contract validator (e.g. older serialized contract loaded by other tooling),
    # the patcher must collapse it to one line before rendering.
    # Tests the private helper directly since the contract layer now blocks
    # multiline notes at construction time.
    from agent.runbook_patcher import _sanitize_operator_note
    out = _sanitize_operator_note("line one\nline two\n  - looks like a bullet")
    assert "\n" not in out
    assert "\r" not in out
    assert "line one" in out
    assert "line two" in out
    assert "looks like a bullet" in out
    assert not out.startswith("-")


def test_patch_no_trailing_em_dash_when_no_operator_note():
    # A var without operator_note (allow_manual=False) shouldn't render a
    # dangling em-dash at the end of the bullet
    extra = {
        "DEBUG_PORT": EnvVarRule(
            value="9090",
            docs=DocsRef(file="demo/docs/runbook.md", section="Runtime Configuration"),
            allow_manual_change=False,
        ),
    }
    diff = EnvDiff(name="DEBUG_PORT", expected="9090", live="9999",
                   contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL)
    new = patch_runbook(STARTING_RUNBOOK, [diff], _contract(extra))
    matching = [ln for ln in new.splitlines() if "DEBUG_PORT" in ln]
    assert len(matching) == 1
    assert not matching[0].rstrip().endswith("—")
