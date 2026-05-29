"""Structure assertions for the tofu-apply worker Dockerfile (Phase C4).

The Dockerfile is the first to bake in a `tofu` binary + the iac/ source; these
pins are security-relevant (the fidelity gate refuses any plan whose
opentofu_version differs from the baked binary), so lock them with a cheap test.
"""
from __future__ import annotations

from pathlib import Path

_DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"


def _text() -> str:
    return _DOCKERFILE.read_text(encoding="utf-8")


def test_tofu_version_pinned_to_1_12_0() -> None:
    t = _text()
    assert "ARG TOFU_VERSION=1.12.0" in t
    # the fidelity gate compares against this; it must match iac/.terraform.lock.hcl pin era.


def test_multistage_and_repo_pinned_checksum_verify() -> None:
    t = _text()
    assert "AS tofu-builder" in t
    assert "AS providers" in t
    # the zip is verified against a REPO-CONTROLLED pinned SHA-256 (not a fetched
    # SHA256SUMS) — code-reviewed checksum baseline, fail-build-on-mismatch.
    assert "ARG TOFU_SHA256=" in t
    assert "sha256sum -c" in t
    assert "8d7650fd42b6d790f9f747604393ccd0a9035376bccc4f1688b905d7c5bb1137" in t


def test_only_the_verified_binary_is_copied_to_runtime() -> None:
    t = _text()
    assert "COPY --from=tofu-builder /usr/local/bin/tofu /usr/local/bin/tofu" in t


def test_bakes_iac_with_prefetched_providers() -> None:
    t = _text()
    # providers baked from the committed lockfile (hermetic init/apply, no registry fetch).
    assert "tofu init -backend=false" in t and "-lockfile=readonly" in t
    assert "COPY --from=providers /app/iac/ ./iac/" in t


def test_copies_worker_sources_and_lib() -> None:
    t = _text()
    for src in ("main.py", "gcs_fetch.py", "tofu_runner.py"):
        assert f"workers/tofu_apply/{src}" in t
    assert "COPY driftscribe_lib/ ./driftscribe_lib/" in t


def test_runtime_env_and_cmd() -> None:
    t = _text()
    assert "ENV IAC_DIR=/app/iac" in t
    assert "uvicorn workers.tofu_apply.main:app" in t
