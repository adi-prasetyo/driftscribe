"""Static regression guards for the Eventarc drift-trigger wiring.

The Eventarc path failed silently in prod for two reasons that NO Python test
could catch (the handler unit tests mock ``verify_oauth2_token``), because the
bugs live in shell + YAML:

1. ``setup_secrets.sh`` §10 hardcoded the **v2** ``UpdateService`` filter, but
   the env (gcloud / CI deploys, and the demo's own
   ``gcloud run services update payment-demo`` drift-injection) emits **v1**
   ``ReplaceService``. The trigger went ACTIVE and delivered nothing.

2. ``cloudbuild.yaml`` stamped ``EVENTARC_AUDIENCE`` as the BARE coordinator URL,
   but Eventarc's Pub/Sub push subscription mints the OIDC token with
   ``aud = <service URL>/eventarc`` (service URL + ``--destination-run-path``).
   ``verify_oauth2_token`` exact-matches ``aud`` → every real delivery 401s.

We parse the files (never call gcloud). If a future edit reintroduces either
bug, drift detection silently stops — these tests fail loudly instead.
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
SETUP_SCRIPT = _ROOT / "infra" / "scripts" / "setup_secrets.sh"
CLOUDBUILD = _ROOT / "infra" / "cloudbuild.yaml"


def _setup() -> str:
    return SETUP_SCRIPT.read_text()


def _cloudbuild() -> str:
    return CLOUDBUILD.read_text()


# --------------------------------------------------------------------------
# cloudbuild.yaml — EVENTARC_AUDIENCE must be path-suffixed
# --------------------------------------------------------------------------
def test_cloudbuild_stamps_path_suffixed_audience():
    """The deploy stamps ``EVENTARC_AUDIENCE`` with the ``/eventarc`` path."""
    text = _cloudbuild()
    assert "EVENTARC_AUDIENCE=$${COORD_URL}/eventarc" in text, (
        "cloudbuild must stamp EVENTARC_AUDIENCE=<coord_url>/eventarc — Eventarc "
        "mints the OIDC aud as the push endpoint (service URL + path)."
    )


def test_cloudbuild_never_stamps_bare_url_audience():
    """Regression: a bare ``EVENTARC_AUDIENCE=$${COORD_URL}`` (no path) 401s every
    real delivery. Anchored negative lookahead so the path-suffixed assignment
    above does NOT count as a violation."""
    text = _cloudbuild()
    bare = re.search(r"EVENTARC_AUDIENCE=\$\$\{COORD_URL\}(?!/eventarc)", text)
    assert bare is None, (
        "found a bare EVENTARC_AUDIENCE=$${COORD_URL} (no /eventarc) — this is "
        "the exact mis-stamp that makes Eventarc deliveries 401."
    )


# --------------------------------------------------------------------------
# setup_secrets.sh §10 — both filters, service-agent, retry, phase-gate
# --------------------------------------------------------------------------
def test_setup_creates_v1_replace_trigger():
    """The v1 ReplaceService trigger (what the env actually emits) is wired with
    the v1 ``namespaces/...`` resourceName."""
    text = _setup()
    assert "google.cloud.run.v1.Services.ReplaceService" in text
    assert "namespaces/${PROJECT}/services/payment-demo" in text


def test_setup_creates_v2_update_trigger():
    """The v2 UpdateService trigger (rollback worker / console / newer clients)
    is wired with the v2 ``projects/.../locations/...`` resourceName."""
    text = _setup()
    assert "google.cloud.run.v2.Services.UpdateService" in text
    assert (
        "projects/${PROJECT}/locations/${REGION}/services/payment-demo" in text
    )


def test_setup_provisions_eventarc_service_agent():
    """The Eventarc service agent identity + role are provisioned explicitly
    (the auto-grant on API enable is not reliably present; without the role,
    ``triggers create`` fails FAILED_PRECONDITION)."""
    text = _setup()
    assert (
        "gcloud beta services identity create --service=eventarc.googleapis.com"
        in text
    )
    assert "roles/eventarc.serviceAgent" in text


def test_setup_retries_only_service_agent_precondition():
    """The create retry loop keys on the EXACT service-agent FAILED_PRECONDITION
    text — not a blanket retry that would mask a bad filter or a deployer
    permission error."""
    text = _setup()
    assert "Permission denied while using the Eventarc Service Agent" in text


def test_setup_repairs_drifted_trigger():
    """A pre-existing trigger with the wrong filter is recreated, not skipped —
    otherwise a project carrying the old dead v2-only trigger stays broken."""
    text = _setup()
    assert "config drifted — recreating" in text
    assert re.search(r"gcloud eventarc triggers delete", text)


def test_setup_eventarc_is_phase_gated():
    """``SETUP_EVENTARC=0`` skips the block so a C4 re-run does not touch drift
    triggers; default is ON for a fresh bootstrap."""
    text = _setup()
    assert 'if [[ "${SETUP_EVENTARC:-1}" == "1" ]]; then' in text


def test_setup_no_longer_ships_v2_only_trigger():
    """Regression: the original §10 shipped ONLY the v2 filter. Both variants
    must be present now (this test fails if someone deletes the v1 trigger and
    reverts to the dead v2-only design)."""
    text = _setup()
    assert "google.cloud.run.v1.Services.ReplaceService" in text, (
        "v1 ReplaceService trigger missing — the demo/CI drift path emits v1; "
        "a v2-only setup is silently dead."
    )
