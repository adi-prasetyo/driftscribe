#!/usr/bin/env python3
"""Fail-closed assertions for the targeted rollback-worker deploy.

Invoked twice by ``infra/cloudbuild.rollback-update.yaml``: once as a
``preflight`` (read-only, BEFORE anything is built) and once as a
``postcondition`` (AFTER the image swap). It reads the JSON of
``gcloud run services describe driftscribe-rollback``.

WHY THIS IS A FILE AND NOT INLINE BASH: the whole value of the targeted
config is that it changes the image and NOTHING else, and the assertion
that proves it is the part most likely to be quietly wrong. As a module it
is unit-testable against crafted service documents, so the assertions
themselves can be mutation-verified rather than trusted.

WHY EXACT EQUALITY, NOT PRESENCE: the regression this exists to catch is
``COORDINATOR_URL`` being repointed from the Cloudflare-fronted demo domain
to the coordinator's run.app host (see the config header). That regression
leaves the NAME ``COORDINATOR_URL`` present and every substring check
passing, which is exactly why presence checks are not enough here.
"""
from __future__ import annotations

import json
import sys

#: Env vars the worker reads. Verified 2026-07-29 against
#: workers/rollback/main.py + driftscribe_lib/ (LOG_LEVEL is optional and
#: intentionally absent: it has a working default).
REQUIRED_ENV = (
    "ALLOWED_CALLERS",
    "COORDINATOR_URL",
    "GCP_PROJECT",
    "OWN_URL",
    "TARGET_REGION",
    "TARGET_SERVICE",
)

#: The HMAC key must arrive as a SECRET reference, never as a literal value.
SECRET_ENV = "APPROVAL_HMAC_KEY"
SECRET_NAME = "approval-hmac-key"

SERVICE = "driftscribe-rollback"


class AssertionFailure(Exception):
    """A checked invariant did not hold. Message is operator-facing."""


def _container(doc: dict) -> dict:
    try:
        return doc["spec"]["template"]["spec"]["containers"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise AssertionFailure(f"malformed service document: {exc}") from exc


def _env_values(doc: dict) -> dict[str, str]:
    """Plain-valued env vars only. A var carrying ``valueFrom`` (a secret) is
    deliberately excluded so it can never satisfy a value comparison."""
    out: dict[str, str] = {}
    for entry in _container(doc).get("env") or []:
        if not isinstance(entry, dict):
            raise AssertionFailure(f"malformed env entry: {entry!r}")
        if "value" in entry:
            out[entry["name"]] = entry["value"]
    return out


def _secret_refs(doc: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for entry in _container(doc).get("env") or []:
        ref = (entry or {}).get("valueFrom", {}).get("secretKeyRef")
        if ref:
            out[entry["name"]] = ref.get("name", "")
    return out


def check_service(
    doc: dict,
    *,
    phase: str,
    expect_coordinator_url: str,
    expect_target_service: str,
    region: str,
    project: str,
    tag: str,
) -> list[str]:
    """Return a list of failure strings; empty means every invariant held."""
    failures: list[str] = []
    env = _env_values(doc)

    for name in REQUIRED_ENV:
        if name not in env:
            failures.append(f"{name} missing from {SERVICE}")

    # The one that matters most — exact, not substring.
    actual = env.get("COORDINATOR_URL")
    if actual is not None and actual != expect_coordinator_url:
        failures.append(
            f"COORDINATOR_URL is {actual!r}, expected {expect_coordinator_url!r} "
            "— every operator approval link is built from this value"
        )

    if (ts := env.get("TARGET_SERVICE")) is not None and ts != expect_target_service:
        failures.append(f"TARGET_SERVICE is {ts!r}, expected {expect_target_service!r}")
    if (tr := env.get("TARGET_REGION")) is not None and tr != region:
        failures.append(f"TARGET_REGION is {tr!r}, expected {region!r}")
    if (gp := env.get("GCP_PROJECT")) is not None and gp != project:
        failures.append(f"GCP_PROJECT is {gp!r}, expected {project!r}")

    # OWN_URL is what verify_caller checks the inbound audience against; a
    # placeholder or a URL for a DIFFERENT service fails every call closed.
    own, status_url = env.get("OWN_URL"), (doc.get("status") or {}).get("url")
    if own is not None:
        if "placeholder" in own:
            failures.append(f"OWN_URL is still a placeholder: {own!r}")
        elif status_url and own != status_url:
            failures.append(f"OWN_URL is {own!r} but the service serves {status_url!r}")

    if _secret_refs(doc).get(SECRET_ENV) != SECRET_NAME:
        failures.append(
            f"{SECRET_ENV} must be a secretKeyRef to {SECRET_NAME!r}, got "
            f"{_secret_refs(doc).get(SECRET_ENV)!r}"
        )
    if SECRET_ENV in env:
        failures.append(f"{SECRET_ENV} is set as a LITERAL value, not a secret ref")

    # Traffic: this config has no --no-traffic/promote flow precisely because
    # the service serves LATEST. If that ever stops being true, a plain deploy
    # would create a revision serving 0% while the build reports success.
    traffic = (doc.get("spec") or {}).get("traffic") or []
    if not any(t.get("latestRevision") and t.get("percent") == 100 for t in traffic):
        failures.append(
            f"{SERVICE} does not serve latestRevision=100% (traffic={traffic!r}); "
            "a plain deploy would not serve the new image — see the config header"
        )

    if phase == "postcondition":
        image = _container(doc).get("image", "")
        if not image.endswith(f":{tag}"):
            failures.append(f"deployed image {image!r} does not carry tag {tag!r}")
        status = doc.get("status") or {}
        latest, serving = status.get("latestReadyRevisionName"), [
            t.get("revisionName") for t in status.get("traffic") or []
        ]
        if latest and serving and latest not in serving:
            failures.append(
                f"latest ready revision {latest!r} is not serving (serving={serving!r})"
            )
    return failures


def main(argv: list[str]) -> int:
    if len(argv) != 8:
        print(
            "usage: assert_rollback_service.py <service.json> <preflight|postcondition> "
            "<expect_coordinator_url> <expect_target_service> <region> <project> <tag>",
            file=sys.stderr,
        )
        return 2
    path, phase, coord, target, region, project, tag = argv[1:]
    if phase not in ("preflight", "postcondition"):
        print(f"unknown phase {phase!r}", file=sys.stderr)
        return 2
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    try:
        failures = check_service(
            doc,
            phase=phase,
            expect_coordinator_url=coord,
            expect_target_service=target,
            region=region,
            project=project,
            tag=tag,
        )
    except AssertionFailure as exc:
        print(f"ERROR [{phase}] {exc}", file=sys.stderr)
        return 1
    for line in failures:
        print(f"ERROR [{phase}] {line}", file=sys.stderr)
    if failures:
        return 1
    print(f"OK [{phase}] {SERVICE}: all invariants hold")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
