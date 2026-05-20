"""Unit tests for the post-LLM deterministic validator (Phase 17.C.3a).

Each rule in :mod:`workers.upgrade_docs.validator` has a positive case
(input that should pass) and at least one negative case (input that
should fail with a specific ``status_code`` + ``reason``). Tests
exercise the validator directly — no FastAPI in the loop — because the
validator is deliberately transport-agnostic. The integration with
``/patch`` is tested in :mod:`workers.upgrade_docs.tests.test_patch`.

Status-code map under test:

- 403: policy violations — bad lockfile_path, downgrade attempts,
  major bumps, non-GHSA advisory URLs.
- 422: schema-shaped failures — missing package, unparseable versions.
"""
from __future__ import annotations

import pytest

from workers.upgrade_docs import validator
from workers.upgrade_docs.validator import (
    UpgradeValidationError,
    parse_version,
    validate_upgrade_request,
)


def _lockfile_with(pkg: str = "lodash", version: str = "4.17.20") -> dict:
    """Minimal lockfile fixture: a single ``dependencies`` entry. The
    validator only inspects ``current_lockfile['dependencies']`` so we
    don't need name/version/scripts metadata here."""
    return {"dependencies": {pkg: version}}


def _valid_kwargs(**overrides) -> dict:
    """A request that satisfies every rule. Tests override one field at
    a time to isolate the rule they're exercising."""
    base = dict(
        lockfile_path="demo/upgrade-target/package.json",
        package_name="lodash",
        target_version="4.17.21",
        advisory_url="https://github.com/advisories/GHSA-35jh-r3h4-6jhm",
        current_lockfile=_lockfile_with(),
    )
    base.update(overrides)
    return base


# Sanity / happy path ------------------------------------------------ #


def test_valid_request_passes() -> None:
    """Baseline: a fully valid request returns None and raises nothing."""
    assert validate_upgrade_request(**_valid_kwargs()) is None


# Rule 1 — lockfile_path regex --------------------------------------- #


def test_valid_lockfile_path_passes() -> None:
    validate_upgrade_request(**_valid_kwargs(
        lockfile_path="demo/upgrade-target/package.json",
    ))


def test_lockfile_path_traversal_raises_403() -> None:
    with pytest.raises(UpgradeValidationError) as exc:
        validate_upgrade_request(**_valid_kwargs(
            lockfile_path="demo/upgrade-target/../infra/X",
        ))
    assert exc.value.status_code == 403
    assert "lockfile_path" in exc.value.reason


def test_lockfile_path_wrong_file_raises_403() -> None:
    with pytest.raises(UpgradeValidationError) as exc:
        validate_upgrade_request(**_valid_kwargs(
            lockfile_path="demo/upgrade-target/package-lock.json",
        ))
    assert exc.value.status_code == 403


# Rule 2 — package_name existence ------------------------------------ #


def test_package_name_present_passes() -> None:
    validate_upgrade_request(**_valid_kwargs(
        package_name="lodash",
        current_lockfile=_lockfile_with(pkg="lodash"),
    ))


def test_package_name_missing_raises_422() -> None:
    with pytest.raises(UpgradeValidationError) as exc:
        validate_upgrade_request(**_valid_kwargs(
            package_name="nonexistent",
            current_lockfile=_lockfile_with(pkg="lodash"),
        ))
    assert exc.value.status_code == 422
    # Reason must name the offending package so the operator sees
    # which key was looked up. (Tests on /patch in test_patch.py already
    # rely on this — see ``test_patch_returns_422_when_package_not_in_dependencies``.)
    assert "nonexistent" in exc.value.reason


def test_package_name_missing_when_dependencies_absent_raises_422() -> None:
    """Edge case: ``dependencies`` key entirely absent. The validator
    treats this as 'no deps present' rather than crashing on a None
    deref — the request-body ``package_name`` cannot match an empty set."""
    with pytest.raises(UpgradeValidationError) as exc:
        validate_upgrade_request(**_valid_kwargs(
            package_name="lodash",
            current_lockfile={},
        ))
    assert exc.value.status_code == 422


# Rule 3 — semver, no downgrade, parseable --------------------------- #


def test_patch_bump_passes() -> None:
    validate_upgrade_request(**_valid_kwargs(
        target_version="4.17.21",
        current_lockfile=_lockfile_with(version="4.17.20"),
    ))


def test_minor_bump_passes() -> None:
    validate_upgrade_request(**_valid_kwargs(
        target_version="4.18.0",
        current_lockfile=_lockfile_with(version="4.17.20"),
    ))


def test_downgrade_raises_403() -> None:
    with pytest.raises(UpgradeValidationError) as exc:
        validate_upgrade_request(**_valid_kwargs(
            target_version="4.17.19",
            current_lockfile=_lockfile_with(version="4.17.20"),
        ))
    assert exc.value.status_code == 403
    assert "downgrade" in exc.value.reason or "not greater" in exc.value.reason


def test_equal_version_raises_403() -> None:
    with pytest.raises(UpgradeValidationError) as exc:
        validate_upgrade_request(**_valid_kwargs(
            target_version="4.17.20",
            current_lockfile=_lockfile_with(version="4.17.20"),
        ))
    assert exc.value.status_code == 403


def test_unparseable_current_raises_422() -> None:
    with pytest.raises(UpgradeValidationError) as exc:
        validate_upgrade_request(**_valid_kwargs(
            target_version="4.17.21",
            current_lockfile=_lockfile_with(version="latest"),
        ))
    assert exc.value.status_code == 422
    assert "latest" in exc.value.reason


def test_unparseable_target_raises_422() -> None:
    with pytest.raises(UpgradeValidationError) as exc:
        validate_upgrade_request(**_valid_kwargs(
            target_version="latest",
            current_lockfile=_lockfile_with(version="4.17.20"),
        ))
    assert exc.value.status_code == 422
    assert "latest" in exc.value.reason


def test_prerelease_suffix_tolerated() -> None:
    """``parse_version`` strips prerelease/build metadata via the regex's
    non-capturing group, then compares on the triple only. Phase 17
    doesn't need full SemVer 2.0.0 prerelease ordering — the npm demo
    target uses bare triples."""
    validate_upgrade_request(**_valid_kwargs(
        target_version="4.17.21",
        current_lockfile=_lockfile_with(version="4.17.20-beta.1"),
    ))


def test_build_suffix_tolerated() -> None:
    """Same idea, but with the ``+build`` form."""
    validate_upgrade_request(**_valid_kwargs(
        target_version="4.17.21+build.5",
        current_lockfile=_lockfile_with(version="4.17.20"),
    ))


# parse_version direct coverage -------------------------------------- #


def test_parse_version_strips_prerelease() -> None:
    assert parse_version("1.2.3-rc.1") == (1, 2, 3)


def test_parse_version_strips_build_metadata() -> None:
    assert parse_version("1.2.3+sha.abc") == (1, 2, 3)


def test_parse_version_rejects_two_segments() -> None:
    with pytest.raises(UpgradeValidationError) as exc:
        parse_version("1.2")
    assert exc.value.status_code == 422


def test_parse_version_rejects_non_numeric() -> None:
    with pytest.raises(UpgradeValidationError) as exc:
        parse_version("abc")
    assert exc.value.status_code == 422


# Rule 4 — version_jump ∈ {patch, minor} ----------------------------- #


def test_major_bump_raises_403_mentions_escalation() -> None:
    """Per plan: the 403 message must mention ``escalation`` so the
    operator (and the LLM, if it sees the error) knows where the
    request should have routed. Pins the vocabulary against
    ACTION_REGISTRY's noun form (``escalation``, not the verb
    ``escalate``)."""
    with pytest.raises(UpgradeValidationError) as exc:
        validate_upgrade_request(**_valid_kwargs(
            target_version="5.0.0",
            current_lockfile=_lockfile_with(version="4.17.20"),
        ))
    assert exc.value.status_code == 403
    assert "escalation" in exc.value.reason


# Rule 5 — advisory_url GHSA shape ----------------------------------- #


def test_ghsa_url_passes() -> None:
    validate_upgrade_request(**_valid_kwargs(
        advisory_url="https://github.com/advisories/GHSA-35jh-r3h4-6jhm",
    ))


def test_non_ghsa_url_raises_403() -> None:
    with pytest.raises(UpgradeValidationError) as exc:
        validate_upgrade_request(**_valid_kwargs(
            advisory_url="https://example.com/cve",
        ))
    assert exc.value.status_code == 403
    assert "advisory_url" in exc.value.reason


def test_http_not_https_raises_403() -> None:
    """The regex anchors on ``https://`` literally — http:// must fail."""
    with pytest.raises(UpgradeValidationError) as exc:
        validate_upgrade_request(**_valid_kwargs(
            advisory_url="http://github.com/advisories/GHSA-x",
        ))
    assert exc.value.status_code == 403


def test_evil_advisory_url_raises_403() -> None:
    """A userinfo-injection ``...@evil.com`` URL must NOT match. The
    GHSA id charset (``[a-zA-Z0-9-]``) excludes ``@`` and ``.`` so the
    regex anchors past the GHSA segment and refuses everything after."""
    with pytest.raises(UpgradeValidationError) as exc:
        validate_upgrade_request(**_valid_kwargs(
            advisory_url="https://github.com/advisories/GHSA-x@evil.com",
        ))
    assert exc.value.status_code == 403


def test_ghsa_with_query_string_raises_403() -> None:
    """No query strings on the advisory URL — the canonical GHSA link
    has none. Rejecting them pins the regex tightly so a future
    'tracking parameter' on the URL can't slip through."""
    with pytest.raises(UpgradeValidationError) as exc:
        validate_upgrade_request(**_valid_kwargs(
            advisory_url="https://github.com/advisories/GHSA-x?utm=evil",
        ))
    assert exc.value.status_code == 403


def test_ghsa_with_fragment_raises_403() -> None:
    with pytest.raises(UpgradeValidationError) as exc:
        validate_upgrade_request(**_valid_kwargs(
            advisory_url="https://github.com/advisories/GHSA-x#evil",
        ))
    assert exc.value.status_code == 403


# Order / short-circuit --------------------------------------------- #


def test_validator_short_circuits_on_first_failure() -> None:
    """Request with TWO violations: bad lockfile_path (rule 1) AND a
    non-GHSA advisory_url (rule 5). The validator must surface the
    first one only — keeps error messages crisp (operator sees the
    first thing wrong, not a concatenated list).
    """
    with pytest.raises(UpgradeValidationError) as exc:
        validate_upgrade_request(**_valid_kwargs(
            lockfile_path="demo/upgrade-target/package-lock.json",  # rule 1
            advisory_url="https://example.com/cve",                  # rule 5
        ))
    # Rule 1 fires first.
    assert exc.value.status_code == 403
    assert "lockfile_path" in exc.value.reason
    # Rule 5's keyword must NOT appear — the validator short-circuited.
    assert "advisory_url" not in exc.value.reason


def test_validator_short_circuits_before_version_parsing() -> None:
    """Rule 2 (missing package) runs before rule 3 (parse versions).
    If we send an unparseable version AND a missing package, rule 2's
    422 must surface (not rule 3's 422). Both happen to be 422 so we
    distinguish by reason content.
    """
    with pytest.raises(UpgradeValidationError) as exc:
        validate_upgrade_request(**_valid_kwargs(
            package_name="nonexistent",       # rule 2
            target_version="not-a-version",   # rule 3 if reached
            current_lockfile=_lockfile_with(pkg="lodash"),
        ))
    assert exc.value.status_code == 422
    assert "nonexistent" in exc.value.reason
    # The version-parse error message contains "parseable"; confirm we
    # short-circuited before reaching parse_version.
    assert "parseable" not in exc.value.reason


# Module wiring ------------------------------------------------------ #


def test_upgrade_validation_error_carries_status_and_reason() -> None:
    """The exception type must expose ``status_code`` and ``reason``
    as attributes so the handler can map them to ``HTTPException``."""
    err = UpgradeValidationError(status_code=403, reason="test reason")
    assert err.status_code == 403
    assert err.reason == "test reason"
    # str() of the exception should include the reason (via Exception.__init__).
    assert "test reason" in str(err)


def test_validator_module_has_no_agent_imports() -> None:
    """Worker-isolation pin: the validator's own source must not import
    from ``agent.*``. The subprocess test in ``test_patch.py`` already
    pins this for the worker entry point as a whole (boot-time
    ``sys.modules`` check in a clean Python process). This is a
    lighter-weight, static pin that fails fast inside this test file
    on a file-text grep so a future edit that adds ``from agent...``
    is caught even before the subprocess test runs.
    """
    src = validator.__file__
    assert src is not None
    with open(src, "r", encoding="utf-8") as f:
        text = f.read()
    assert "from agent" not in text
    assert "import agent" not in text
