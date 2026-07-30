#!/usr/bin/env python3
"""Fail-closed assertions for the targeted notifier-worker deploy.

Invoked twice by ``infra/cloudbuild.notifier-update.yaml``: once as a
``preflight`` (read-only, BEFORE anything is built) and once as a
``postcondition`` (AFTER the image swap). It reads the JSON of
``gcloud run services describe driftscribe-notifier``.

Sibling of ``assert_rollback_service.py`` and deliberately built to the same
rules — read that file's header for the reasoning. The two rules worth
restating, because both were learned by shipping the bug:

- **A check that cannot see its subject must FAIL, not abstain.** Guarding a
  comparison with ``if value is not None`` lets a null field and a missing
  ``status`` block sail through, so the postcondition can report "all
  invariants hold" without ever proving a revision is serving.
- **Exact equality, not presence.** A misconfiguration usually leaves the
  NAME in place; it is the value that went wrong.

WHAT IS SPECIFIC TO THE NOTIFIER: knowing ``NOTIFY_WEBHOOK_URL`` IS this
worker's entire authorization model — "the URL is the capability". So the
single most important invariant is that it arrives as a ``secretKeyRef`` and
NEVER as a literal ``value``. A literal would place a live credential in the
service document, readable by anything holding ``run.services.get``, echoed by
``gcloud run services describe``, and captured in build logs. That was close to
harmless while the configured URL was ``httpbin.org/status/200``; from
2026-07-30 the secret holds a real Discord webhook URL, and anyone who has it
can post into the operator's channel.
"""
from __future__ import annotations

import json
import sys

#: Env vars the worker reads. Verified 2026-07-30 against
#: workers/notifier/main.py + driftscribe_lib/ (LOG_LEVEL is optional and
#: intentionally absent: it has a working default). NOTIFY_WEBHOOK_URL is NOT
#: listed here because it must never be a plain value — see SECRET_ENV.
REQUIRED_ENV = (
    "ALLOWED_CALLERS",
    "GCP_PROJECT",
    "OWN_URL",
)

#: The webhook URL must arrive as a SECRET reference, never as a literal.
SECRET_ENV = "NOTIFY_WEBHOOK_URL"

SERVICE = "driftscribe-notifier"


class AssertionFailure(Exception):
    """A checked invariant did not hold. Message is operator-facing."""


def _container(doc: dict) -> dict:
    try:
        return doc["spec"]["template"]["spec"]["containers"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise AssertionFailure(f"malformed service document: {exc}") from exc


def _env_values(doc: dict) -> dict[str, object]:
    """Plain-valued env vars, returned AS FOUND (including ``None``) so the
    caller can reject a null rather than mistake it for absence. A var carrying
    ``valueFrom`` is excluded so a secret can never satisfy a value
    comparison — and, conversely, so a literal masquerading as a secret is
    visible to the SECRET_ENV check below."""
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
    expect_secret: str,
    expect_service_account: str,
    tag: str,
) -> list[str]:
    """Return a list of failure strings; empty means every invariant held."""
    failures: list[str] = []
    env = _env_values(doc)

    for name in REQUIRED_ENV:
        if name not in env:
            failures.append(f"{name} missing from {SERVICE}")
        elif not _nonempty_str(env[name]):
            failures.append(
                f"{name} is null or empty ({env[name]!r}) — cannot be verified"
            )

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
            failures.append(
                f"OWN_URL is {own!r} but the service serves {status_url!r}"
            )

    # The ID-token allowlist. Presence is not enough: an empty or foreign
    # caller list is a live auth misconfiguration, not a cosmetic one. Only
    # the coordinator may ever reach this worker.
    allowed = env.get("ALLOWED_CALLERS")
    if _nonempty_str(allowed) and not str(allowed).startswith("driftscribe-agent@"):
        failures.append(
            f"ALLOWED_CALLERS is {allowed!r}; expected the coordinator SA only"
        )

    # THE invariant this script exists for — see the module docstring.
    actual_ref = _secret_refs(doc).get(SECRET_ENV)
    if actual_ref != expect_secret:
        failures.append(
            f"{SECRET_ENV} must be a secretKeyRef to {expect_secret!r}, got "
            f"{actual_ref!r}"
        )
    if SECRET_ENV in env:
        # Reported separately from the check above: this is not "the wrong
        # secret", it is a live credential sitting in plaintext in the service
        # document. The value itself is deliberately NOT echoed.
        failures.append(
            f"{SECRET_ENV} is set as a LITERAL value, not a secret ref — the "
            "webhook URL is a credential and must not be in the service config"
        )

    # Least-privilege runtime identity. notifier-agent-sa holds no
    # project-level roles, only secretAccessor on the one secret above; a
    # deploy that silently fell back to the default compute SA would hand this
    # worker far more reach than its threat model allows.
    sa = (doc.get("spec") or {}).get("template", {}).get("spec", {}).get(
        "serviceAccountName"
    )
    if not _nonempty_str(sa):
        failures.append("serviceAccountName is missing — cannot verify identity")
    elif sa != expect_service_account:
        failures.append(
            f"serviceAccountName is {sa!r}, expected {expect_service_account!r}"
        )

    # Traffic: this config has no --no-traffic/promote flow precisely because
    # the service serves LATEST. If that ever stops being true, a plain deploy
    # would create a revision serving 0% while the build reports success.
    traffic = (doc.get("spec") or {}).get("traffic") or []
    # `is True`, not truthiness: a document carrying the STRING "false" is
    # truthy, and this checker's job is to fail closed on anything unexpected.
    if not any(
        isinstance(t, dict)
        and t.get("latestRevision") is True
        and t.get("percent") == 100
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
            # comparison that only runs when the field exists abstains on a
            # missing field and reports success.
            if not _nonempty_str(latest_created):
                failures.append(
                    "status.latestCreatedRevisionName is missing — cannot prove the "
                    "revision just created is the one that became ready"
                )
            elif latest_created != latest_ready:
                failures.append(
                    f"latest created revision {latest_created!r} is not the latest "
                    f"READY revision ({latest_ready!r}) — the new revision did not "
                    "come up"
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
    if len(argv) != 6:
        print(
            "usage: assert_notifier_service.py <service.json> "
            "<preflight|postcondition> <expect_secret> <expect_service_account> "
            "<tag>",
            file=sys.stderr,
        )
        return 2
    path, phase, expect_secret, expect_sa, tag = argv[1:]
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
            expect_secret=expect_secret,
            expect_service_account=expect_sa,
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
