from agent.classifier import classify, ClassificationInput
from agent.contract import OpsContract, EnvVarRule, DocsRef
from agent.models import DecisionAction, ContractStatus


def _contract(env_rules):
    return OpsContract(
        service="payment-demo",
        environment="production",
        cloud_run_service="payment-demo",
        region="asia-northeast1",
        github_repo="theghostsquad00/driftscribe",
        expected_env=env_rules,
    )


def _rule(value="x", allow=False, note=None):
    return EnvVarRule(
        value=value,
        docs=DocsRef(file="demo/docs/runbook.md", section="S"),
        allow_manual_change=allow,
        operator_note=note,
    )


def test_no_op_when_live_matches_contract():
    c = _contract({"PAYMENT_MODE": _rule("mock", allow=False)})
    out = classify(ClassificationInput(contract=c, live_env={"PAYMENT_MODE": "mock"}))
    assert out.action == DecisionAction.NO_OP
    assert out.env_diffs == []


def test_sanctioned_change_when_allow_manual():
    c = _contract({"FEATURE_X": _rule("false", allow=True, note="op note")})
    out = classify(ClassificationInput(contract=c, live_env={"FEATURE_X": "true"}))
    assert out.action == DecisionAction.DOCS_PR
    d = out.env_diffs[0]
    assert d.expected == "false" and d.live == "true"
    assert d.contract_status == ContractStatus.PRESENT_ALLOW_MANUAL
    assert out.target_docs_file == "demo/docs/runbook.md"
    assert out.target_docs_section == "S"


def test_unsanctioned_drift_when_disallow_manual():
    c = _contract({"PAYMENT_MODE": _rule("mock", allow=False)})
    out = classify(ClassificationInput(contract=c, live_env={"PAYMENT_MODE": "live"}))
    assert out.action == DecisionAction.DRIFT_ISSUE
    assert out.env_diffs[0].contract_status == ContractStatus.PRESENT_DISALLOW_MANUAL


def test_escalation_when_var_absent_and_no_pr_match():
    c = _contract({})
    out = classify(ClassificationInput(contract=c, live_env={"NEW_THING": "x"}))
    assert out.action == DecisionAction.ESCALATION
    assert out.env_diffs[0].contract_status == ContractStatus.ABSENT
    assert out.requires_human_review is True


def test_recent_pr_promotes_absent_var_to_docs_pr():
    c = _contract({})
    prs = [{
        "title": "Add NEW_THING flag",
        "body": "Introduces NEW_THING for the checkout flow.",
        "url": "https://github.com/x/x/pull/1",
        "merged": True,
    }]
    out = classify(ClassificationInput(
        contract=c, live_env={"NEW_THING": "x"}, recent_prs=prs,
    ))
    assert out.action == DecisionAction.DOCS_PR
    assert out.env_diffs[0].recent_pr_match == "https://github.com/x/x/pull/1"


def test_recent_pr_must_match_exact_var_name_not_substring():
    # A PR mentioning "FEATURE_NEW" must NOT promote a drift of "FEATURE_NEW_CHECKOUT"
    c = _contract({})
    prs = [{"title": "Add FEATURE_NEW", "body": "FEATURE_NEW=1", "url": "u", "merged": True}]
    out = classify(ClassificationInput(
        contract=c, live_env={"FEATURE_NEW_CHECKOUT": "true"}, recent_prs=prs,
    ))
    # FEATURE_NEW_CHECKOUT is NOT in the PR (only FEATURE_NEW is). Should escalate.
    assert out.action == DecisionAction.ESCALATION


def test_unmerged_pr_does_not_promote():
    c = _contract({})
    prs = [{"title": "Add NEW_THING", "body": "", "url": "u", "merged": False}]
    out = classify(ClassificationInput(
        contract=c, live_env={"NEW_THING": "x"}, recent_prs=prs,
    ))
    assert out.action == DecisionAction.ESCALATION


def test_multi_var_drift_takes_most_serious_action():
    # If any drift is DRIFT_ISSUE, the whole decision is DRIFT_ISSUE
    c = _contract({
        "PAYMENT_MODE": _rule("mock", allow=False),
        "FEATURE_X": _rule("false", allow=True, note="op"),
    })
    live = {"PAYMENT_MODE": "live", "FEATURE_X": "true"}
    out = classify(ClassificationInput(contract=c, live_env=live))
    assert out.action == DecisionAction.DRIFT_ISSUE
    assert len(out.env_diffs) == 2
