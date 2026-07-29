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

WHY ABSENCE IS NEVER A PASS: an earlier cut guarded every comparison with
``if value is not None``, so ``COORDINATOR_URL: null`` and a document with
no ``status`` block at all sailed through and the postcondition could
report "all invariants hold" without proving any revision was serving. A
check that cannot see its subject must FAIL, not abstain — the same rule
the desk's `unknown` state exists for.
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


def _env_values(doc: dict) -> dict[str, object]:
    """Plain-valued env vars. Values are returned AS FOUND (including ``None``)
    so the caller can reject a null rather than mistake it for absence. A var
    carrying ``valueFrom`` (a secret) is excluded so it can never satisfy a
    value comparison."""
    out: dict[str, object] = {}
    for entry in _container(doc).get("env") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise AssertionFailure(f"malformed env entry: {entry!r}")
        if "value" in entry:
            out[entry["name"]] = entry["value"]
    return out


def _secret_refs(doc: dict) -> dict[str, object]:
    out: dict[str, object] = {}
    for entry in _container(doc).get("env") or []:
        ref = (entry or {}).get("valueFrom", {}).get("secretKeyRef")
        if ref:
            out[entry["name"]] = ref.get("name")
    return out


def _nonempty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


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
        elif not _nonempty_str(env[name]):
            failures.append(f"{name} is null or empty ({env[name]!r}) — cannot be verified")

    def _exact(name: str, expected: str, note: str = "") -> None:
        """Compare only when the value is a usable string; an absent/null one
        has already been reported above and must not be reported twice."""
        value = env.get(name)
        if _nonempty_str(value) and value != expected:
            failures.append(f"{name} is {value!r}, expected {expected!r}{note}")

    _exact(
        "COORDINATOR_URL",
        expect_coordinator_url,
        " — every operator approval link is built from this value",
    )
    _exact("TARGET_SERVICE", expect_target_service)
    _exact("TARGET_REGION", region)
    _exact("GCP_PROJECT", project)
    # The ID-token allowlist. Presence is not enough: an empty or foreign
    # caller list is a live auth misconfiguration, not a cosmetic one.
    _exact("ALLOWED_CALLERS", f"driftscribe-agent@{project}.iam.gserviceaccount.com")

    # OWN_URL is what verify_caller checks the inbound audience against; a
    # placeholder or a URL for a DIFFERENT service fails every call closed.
    # status.url is stable across revisions, so this is safe mid-rollout.
    status = doc.get("status") or {}
    status_url = status.get("url")
    if not _nonempty_str(status_url):
        failures.append("status.url is missing — cannot verify OWN_URL")
    own = env.get("OWN_URL")
    if _nonempty_str(own):
        if "placeholder" in own:
            failures.append(f"OWN_URL is still a placeholder: {own!r}")
        elif _nonempty_str(status_url) and own != status_url:
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
    if not any(
        isinstance(t, dict) and t.get("latestRevision") and t.get("percent") == 100
        for t in traffic
    ):
        failures.append(
            f"{SERVICE} does not serve latestRevision=100% (traffic={traffic!r}); "
            "a plain deploy would not serve the new image — see the config header"
        )

    # Deliberately NOT asserted in preflight: overall Ready. A failed latest
    # revision with an older healthy one serving is exactly the state this
    # pipeline is needed to recover from, so demanding health here would lock
    # the operator out of the fix.
    if phase == "postcondition":
        image = _container(doc).get("image")
        if not _nonempty_str(image):
            failures.append("deployed image is missing from the service document")
        elif not image.endswith(f":{tag}"):
            failures.append(f"deployed image {image!r} does not carry tag {tag!r}")

        latest_ready = status.get("latestReadyRevisionName")
        latest_created = status.get("latestCreatedRevisionName")
        served = status.get("traffic") or []
        if not _nonempty_str(latest_ready):
            failures.append(
                "status.latestReadyRevisionName is missing — cannot prove the new "
                "revision is serving"
            )
        else:
            # Required INDEPENDENTLY, not merely compared when present: a
            # comparison that only runs `if _nonempty_str(latest_created)`
            # abstains on a missing field and reports success. That is the
            # same defect as the null-value guards above, one level deeper.
            if not _nonempty_str(latest_created):
                failures.append(
                    "status.latestCreatedRevisionName is missing — cannot prove the "
                    "revision just created is the one that became ready"
                )
            elif latest_created != latest_ready:
                failures.append(
                    f"latest created revision {latest_created!r} is not the latest READY "
                    f"revision ({latest_ready!r}) — the new revision did not come up"
                )
            # Membership alone is not enough: a tagged entry can sit at 0%.
            if not any(
                isinstance(t, dict)
                and t.get("revisionName") == latest_ready
                and t.get("percent") == 100
                for t in served
            ):
                failures.append(
                    f"latest ready revision {latest_ready!r} is not serving 100% "
                    f"(status.traffic={served!r})"
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
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        # Empty output or a non-JSON error body on stdout lands here. Fail
        # closed with a readable message rather than a raw traceback.
        print(f"ERROR [{phase}] could not read {path}: {exc}", file=sys.stderr)
        return 1
    if not isinstance(doc, dict):
        print(f"ERROR [{phase}] {path} is not a service object", file=sys.stderr)
        return 1
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
