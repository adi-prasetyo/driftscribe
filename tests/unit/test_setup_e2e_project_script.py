"""Phase 20 Task 20.0 — meta-tests pinning the shape of setup_e2e_project.sh
and the parameterization of infra/cloudbuild.yaml.

These tests are deliberately text-shape assertions (not executions). They run in
plain pytest with no GCP access; they verify the operator-facing script and the
deploy manifest carry the right secret/SA/region/role strings so a drift between
the runbook and the build manifest is caught at CI time rather than at deploy
time.
"""
from pathlib import Path


def test_setup_e2e_project_script_exists():
    script = Path("infra/scripts/setup_e2e_project.sh")
    assert script.exists()
    assert script.stat().st_mode & 0o111


def test_setup_e2e_project_sources_shared_lib():
    body = Path("infra/scripts/setup_e2e_project.sh").read_text()
    assert "source" in body and "_setup_lib.sh" in body


def test_setup_e2e_project_creates_all_eight_prod_secrets():
    """Phase 20 fix: setup must create every secret cloudbuild.yaml mounts."""
    body = Path("infra/scripts/setup_e2e_project.sh").read_text()
    for secret in (
        "coordinator-shared-token",
        "github-pat",
        "developer-knowledge-api-key",
        "docs-agent-github-pat",
        "approval-hmac-key",
        "driftscribe-webhook-url",
        "upgrade-reader-github-pat",
        "upgrade-docs-github-pat",
    ):
        assert secret in body, f"missing secret: {secret}"


def test_setup_e2e_project_uses_real_sa_names():
    """Use the actual SA names from cloudbuild.yaml, not invented short names."""
    body = Path("infra/scripts/setup_e2e_project.sh").read_text()
    for sa in (
        "driftscribe-agent",
        "reader-agent-sa",
        "docs-agent-sa",
        "rollback-agent-sa",
        "notifier-agent-sa",
        "upgrade-reader-sa",
        "upgrade-docs-sa",
        "e2e-runner-sa",
    ):
        assert sa in body, f"missing SA: {sa}"
    # No invented short names. The legitimate "reader" SAs are
    # `reader-agent-sa` (drift workload) and `upgrade-reader-sa`
    # (upgrade workload). A bare `reader-sa@...` email would be the
    # invented short name; allow `reader-sa@` only when prefixed with
    # `agent-` or `upgrade-`.
    assert "coord-sa" not in body
    import re as _re
    for hit in _re.finditer(r"([\w-]*)reader-sa@", body):
        prefix = hit.group(1)
        assert prefix in ("agent-", "upgrade-"), (
            f"unexpected reader-sa@ shape in body — prefix={prefix!r}; "
            "only reader-agent-sa@ or upgrade-reader-sa@ are valid"
        )
    # No old/wrong names.
    assert "OPERATOR_TOKEN" not in body
    assert "HITL_HMAC_KEY" not in body
    assert "GEMINI_API_KEY" not in body


def test_setup_e2e_project_grants_logging_viewer():
    body = Path("infra/scripts/setup_e2e_project.sh").read_text()
    assert "roles/logging.viewer" in body


def test_setup_e2e_project_initializes_firestore():
    body = Path("infra/scripts/setup_e2e_project.sh").read_text()
    assert "firestore" in body.lower()
    assert "asia-northeast1" in body


def test_setup_e2e_project_extends_log_retention_to_365():
    body = Path("infra/scripts/setup_e2e_project.sh").read_text()
    assert "365" in body


def test_cloudbuild_has_target_service_substitution():
    body = Path("infra/cloudbuild.yaml").read_text()
    assert "_TARGET_SERVICE:" in body
    assert "$_TARGET_SERVICE" in body or "${_TARGET_SERVICE}" in body


def test_cloudbuild_has_target_github_repo_substitution():
    body = Path("infra/cloudbuild.yaml").read_text()
    assert "_TARGET_GITHUB_REPO:" in body
    assert "_UPGRADE_TARGET_REPO:" in body


def test_cloudbuild_deploys_parameterized_service_name():
    """Phase 20 fix: the 'gcloud run deploy <name>' line must use the substitution,
    not the literal 'payment-demo'. Otherwise the e2e build still deploys prod's
    target. We verify by counting literal occurrences in the deploy block region.
    """
    body = Path("infra/cloudbuild.yaml").read_text()
    # The substitution appears wherever the drift target service is named.
    assert "$_TARGET_SERVICE" in body
    # No `- payment-demo` literal as a deploy arg (the substitution replaces it).
    # Allow the literal default in the substitutions: block at the top.
    deploy_block_starts = [i for i, line in enumerate(body.splitlines())
                            if line.strip() == "- deploy"]
    for idx in deploy_block_starts:
        # The next non-blank line is the service name arg.
        for j in range(idx + 1, min(idx + 4, len(body.splitlines()))):
            line = body.splitlines()[j].strip()
            if line.startswith("- ") and line != "- deploy":
                # If this deploy block targets the drift demo, it must use the substitution.
                if "payment-demo" in line and "$_TARGET_SERVICE" not in line:
                    raise AssertionError(
                        f"Found literal payment-demo as deploy arg (line {j+1}); "
                        f"expected $_TARGET_SERVICE"
                    )
                break


def test_cloudbuild_coordinator_sets_upgrade_target_repo_override():
    """The coordinator's --set-env-vars must include UPGRADE_TARGET_REPO_OVERRIDE
    so the registry override matches the worker-side authority."""
    body = Path("infra/cloudbuild.yaml").read_text()
    assert "UPGRADE_TARGET_REPO_OVERRIDE=$_UPGRADE_TARGET_REPO" in body \
        or "UPGRADE_TARGET_REPO_OVERRIDE=${_UPGRADE_TARGET_REPO}" in body


def test_cloudbuild_use_adk_is_parameterized():
    """USE_ADK must be a substitution so E2E can flip to true while prod stays false."""
    body = Path("infra/cloudbuild.yaml").read_text()
    assert "_USE_ADK:" in body, "missing _USE_ADK substitution"
    assert "USE_ADK=$_USE_ADK" in body or "USE_ADK=${_USE_ADK}" in body, \
        "coordinator env must use the substitution, not a literal"
    assert "USE_ADK=false" not in body, "literal USE_ADK=false would shadow the substitution"


def test_cloudbuild_demo_target_image_tag_parameterized():
    """Phase 20: payment-demo image push/deploy must reference $_TARGET_SERVICE,
    not the literal 'payment-demo' in the image tag.
    """
    body = Path("infra/cloudbuild.yaml").read_text()
    # The substitution default IS 'payment-demo', so 'driftscribe/payment-demo:' may
    # appear only as a comment or as the default expansion. The literal in deploy
    # args is what we guard against.
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        # Match the image arg shape that's specifically the drift target.
        if "driftscribe/payment-demo:" in stripped and "$_TARGET_SERVICE" not in stripped:
            raise AssertionError(
                f"Found literal driftscribe/payment-demo: image ref outside a comment; "
                f"line: {line!r}"
            )


def test_e2e_runbook_documents_use_adk_true():
    """The e2e-environment runbook MUST tell operators to pass _USE_ADK=true."""
    body = Path("docs/runbooks/e2e-environment.md").read_text()
    assert "_USE_ADK=true" in body, \
        "runbook must include _USE_ADK=true in the E2E build command"
