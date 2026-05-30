"""Phase C5f — meta-tests pinning the IAM/secret hardening shape of the prod
bootstrap scripts. Text-shape assertions (no GCP access), run in plain pytest.

They guard the invariants Codex flagged as load-bearing:

* **BLOCKER-1 (regression guard):** the prod bootstrap scripts must never grant a
  BARE, project-wide `roles/datastore.user` to the coordinator / rollback / apply
  SAs again — a re-run would silently re-open all-database access and undo the
  plan_approvals isolation. datastore.user is granted ONLY via the conditioned
  helper (which lives in `_setup_lib.sh`), scoped to a single database.
* The dedicated `payment-demo-runtime` SA gets actAs from BOTH `tofu-apply-sa` AND
  `rollback-agent-sa` (the C4 plan named only the former — rollback was the gap).
* The un-conditioned datastore.user removal is GATED behind `SETUP_PLAN_APPROVALS_DB=1`
  (a deliberate, verified cutover — never a default-rerun side effect).
* The `github-pat` operator doc reflects the fine-grained, Contents:write reality.

Note: `setup_e2e_project.sh` deliberately KEEPS un-conditioned datastore.user — the
e2e project has only the (default) database and never runs the tofu-apply worker, so
there is nothing to isolate there. These guards therefore target the prod scripts.
"""
from pathlib import Path

LIB = Path("infra/scripts/_setup_lib.sh")
SECRETS = Path("infra/scripts/setup_secrets.sh")
IAC_BACKEND = Path("infra/scripts/setup_iac_backend.sh")
PROD = Path("infra/scripts/setup_prod_project.sh")

# The three PROD bootstrap scripts that previously granted project-wide
# datastore.user and now must route every such grant through the conditioned helper.
PROD_SCRIPTS = (SECRETS, IAC_BACKEND, PROD)


def _body(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Shared lib helpers exist.
# --------------------------------------------------------------------------- #


def test_lib_defines_named_db_and_conditioned_helpers() -> None:
    body = _body(LIB)
    for fn in (
        "create_named_firestore_db_idempotent()",
        "grant_datastore_user_for_db()",
        "remove_unconditioned_datastore_user()",
    ):
        assert fn in body, f"missing helper: {fn}"


def test_conditioned_helper_uses_resource_name_condition() -> None:
    body = _body(LIB)
    assert 'resource.name == \\"projects/${project}/databases/${db_id}\\"' in body
    # The removal targets ONLY the no-condition binding.
    assert "--condition=None" in body


# --------------------------------------------------------------------------- #
# BLOCKER-1 regression guard: no bare project-wide datastore.user in prod scripts.
# --------------------------------------------------------------------------- #


def test_no_bare_datastore_user_grant_in_prod_scripts() -> None:
    """The prod bootstrap scripts must not contain a literal `roles/datastore.user`
    grant — it is granted ONLY via the conditioned helper (in _setup_lib.sh).
    A bare grant would re-open all-database access on the next re-run."""
    for script in PROD_SCRIPTS:
        assert "roles/datastore.user" not in _body(script), (
            f"{script} contains a bare roles/datastore.user grant — must use "
            "grant_datastore_user_for_db (conditioned) instead"
        )


def test_coordinator_and_rollback_conditioned_to_default() -> None:
    body = _body(SECRETS)
    assert 'grant_datastore_user_for_db "$PROJECT" "serviceAccount:${COORD_SA}" "(default)"' in body
    assert 'grant_datastore_user_for_db "$PROJECT" "serviceAccount:${ROLLBACK_SA}" "(default)"' in body


def test_apply_sa_conditioned_to_named_db() -> None:
    body = _body(IAC_BACKEND)
    assert "create_named_firestore_db_idempotent" in body
    assert 'grant_datastore_user_for_db "$PROJECT" "serviceAccount:${APPLY_SA}" "$PLAN_APPROVALS_DB"' in body


# --------------------------------------------------------------------------- #
# Un-conditioned removal is gated behind the explicit cutover flag.
# --------------------------------------------------------------------------- #


def test_unconditioned_removal_is_gated() -> None:
    for script in (SECRETS, IAC_BACKEND, PROD):
        body = _body(script)
        if "remove_unconditioned_datastore_user" in body:
            assert "SETUP_PLAN_APPROVALS_DB" in body, (
                f"{script} removes the un-conditioned grant but not behind the "
                "SETUP_PLAN_APPROVALS_DB cutover gate"
            )


def test_secrets_removal_gate_defaults_off() -> None:
    body = _body(SECRETS)
    assert 'if [[ "${SETUP_PLAN_APPROVALS_DB:-0}" == "1" ]]; then' in body


# --------------------------------------------------------------------------- #
# Dedicated payment-demo runtime SA + actAs for BOTH mutators (the §3.7 fix).
# --------------------------------------------------------------------------- #


def test_dedicated_runtime_sa_created() -> None:
    body = _body(SECRETS)
    assert 'PD_RUNTIME_SA_NAME="${PD_RUNTIME_SA_NAME:-payment-demo-runtime}"' in body
    assert "create_service_account_idempotent" in body
    assert "DriftScribe payment-demo runtime (minimal)" in body


def test_actas_granted_to_both_apply_and_rollback() -> None:
    """Cloud Run requires actAs on the runtime SA for ANY update (incl. rollback's
    traffic-only update_service), so BOTH SAs must get it on the dedicated SA.

    Assert by membership, not an exact loop header: the default-compute-SA
    retirement (Phase 3) legitimately adds ``$BUILD_DEPLOY_SA`` to the same loop
    (cloudbuild-deploy-sa now deploys payment-demo, so it too needs actAs on the
    dedicated runtime SA). The load-bearing invariant is that a single actAs loop
    over the dedicated SA covers BOTH the apply and rollback mutators."""
    body = _body(SECRETS)
    loop = next(
        (ln for ln in body.splitlines() if ln.strip().startswith("for member in") and "; do" in ln),
        None,
    )
    assert loop is not None, "no `for member in ...; do` actAs loop found in setup_secrets.sh"
    assert '"$APPLY_SA"' in loop, f"apply SA missing from actAs loop: {loop!r}"
    assert '"$ROLLBACK_SA"' in loop, f"rollback SA missing from actAs loop: {loop!r}"
    assert "$PD_RUNTIME_SA_DEDICATED" in body
    assert 'roles/iam.serviceAccountUser' in body


def test_prod_project_rollback_actas_covers_dedicated_sa() -> None:
    """The fresh-prod operator heredoc must grant rollback actAs on the dedicated
    runtime SA (Codex IMPORTANT-5), AND — because a fresh service still runs as the
    default compute SA until the C5g repoint applies — ALSO on the LIVE-resolved
    runtime SA so a rollback works in the transition window (Adversarial IMPORTANT-2).
    """
    body = _body(PROD)
    # The dedicated SA is covered.
    assert "payment-demo-runtime@$PROJECT.iam.gserviceaccount.com" in body
    # The live runtime SA is resolved (not hardcoded to one identity), with the
    # default compute SA only as a fallback for the transition window.
    assert "template.serviceAccount" in body
    assert 'for RUNTIME_SA in "\\$LIVE_RUNTIME_SA"' in body


# --------------------------------------------------------------------------- #
# github-pat operator doc reflects the fine-grained, Contents:write reality.
# --------------------------------------------------------------------------- #


def test_github_pat_doc_is_fine_grained_contents_write() -> None:
    body = _body(SECRETS)
    assert "Fine-grained PAT for the coordinator" in body
    assert "Contents: write" in body
    # The stale "Classic PAT ... read-only PR search" wording must be gone.
    assert "Classic PAT for the coordinator's read-only PR search" not in body
