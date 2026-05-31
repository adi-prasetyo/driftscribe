"""Fetch C2 plan artifacts from GCS by PINNED object generation (Phase C4).

The inverse of ``tools/iac_plan_artifact_upload.py``. GCS I/O is deliberately
kept OUT of ``driftscribe_lib`` (C3 Decision D fenced network I/O out of the
lib); the lib's pure ``verify_artifact_integrity`` consumes the bytes this
returns. Worker-local on purpose.

Three correctness properties (all load-bearing):

- ``generation=N`` on ``bucket.blob()`` pins the read to that exact object
  revision (the artifact bucket has Object Versioning ON, so an overwritten
  generation is still fetchable by number).
- ``raw_download=True`` returns the un-decoded stored bytes (no gzip
  transcoding) so ``hashlib.sha256(bytes)`` matches the ``sha256sum`` the C2
  plan-builder computed over the on-disk file (.github/workflows/iac.yml).
- ``if_generation_match=N`` makes the GET conditional — a ``PreconditionFailed``
  if the server-resolved generation differs (belt-and-suspenders against a
  future SDK ignoring the constructor arg).

Plus a fail-closed locator validator: the parsed bucket MUST equal the known
artifact bucket and the object path MUST match
``pr-<N>/<head_sha>/run-<id>-<attempt>/<basename>`` — even though the URI is
HMAC-signed, this is cheap SSRF defense if the signing path is ever weakened.

This module never compares SHA-256 (that is the lib's ``verify_artifact_integrity``);
it only fetches the exact bytes and fails closed on any fetch ambiguity.
"""
from __future__ import annotations

import re
from typing import Any

ARTIFACT_BUCKET = "driftscribe-hack-2026-tofu-artifacts"

# pr-<N>/<head_sha>/run-<id>-<attempt>/<basename> — mirrors the upload-side
# _OBJECT_PREFIX_RE (tools/iac_plan_artifact_upload.py) with a trailing basename.
# ``iac-tree.json`` is the C6 sidecar (carries the iac/-tree hash).
_OBJECT_RE = re.compile(
    r"^pr-[1-9][0-9]*/[0-9a-f]{40}/run-[1-9][0-9]*-[1-9][0-9]*/"
    r"(metadata\.json|plan\.tfplan|plan\.json|iac-tree\.json)$"
)

_METADATA_BASENAME = "metadata.json"
_SIDECAR_BASENAME = "iac-tree.json"


class GcsFetchError(Exception):
    """Raised on any fetch ambiguity — bad URI, wrong bucket, missing/mismatched
    generation, 404. The caller (the /apply or /propose handler) converts this to
    a fail-closed refusal; the artifact is NOT trusted."""


def parse_gs_uri(uri: str) -> tuple[str, str]:
    """``gs://<bucket>/<object>`` → ``(bucket, object)``. Fail-closed."""
    if not isinstance(uri, str) or not uri.startswith("gs://"):
        raise GcsFetchError(f"not a gs:// uri: {uri!r}")
    rest = uri[len("gs://") :]
    bucket, _, obj = rest.partition("/")
    if not bucket or not obj:
        raise GcsFetchError(f"malformed gs:// uri (need bucket and object): {uri!r}")
    return bucket, obj


def validate_artifact_uri(uri: str, *, expected_basename: str) -> tuple[str, str]:
    """Validate a (signed) artifact URI fail-closed and return ``(bucket, object)``.

    Asserts the bucket is exactly :data:`ARTIFACT_BUCKET` and the object path
    matches the ``pr-<N>/<sha>/run-<id>-<attempt>/<basename>`` scheme with the
    expected basename (``metadata.json`` / ``plan.tfplan`` / ``plan.json`` /
    ``iac-tree.json``).
    """
    bucket, obj = parse_gs_uri(uri)
    if bucket != ARTIFACT_BUCKET:
        raise GcsFetchError(f"unexpected bucket {bucket!r} (want {ARTIFACT_BUCKET!r})")
    if not _OBJECT_RE.fullmatch(obj):
        raise GcsFetchError(f"object path does not match the artifact scheme: {obj!r}")
    if not obj.endswith("/" + expected_basename):
        raise GcsFetchError(f"object basename is not {expected_basename!r}: {obj!r}")
    return bucket, obj


def _to_int_generation(generation: Any) -> int:
    """Coerce a (numeric-string) generation to int, fail-closed."""
    try:
        return int(generation)
    except (TypeError, ValueError):
        raise GcsFetchError(f"generation must be a numeric string (got {generation!r})")


def fetch_object_pinned(bucket: Any, object_name: str, generation: Any) -> bytes:
    """Fetch the EXACT object bytes pinned to ``generation``.

    ``bucket`` is a ``google.cloud.storage.Bucket`` (or a test double with a
    compatible ``.blob(name, generation=...)`` → ``.download_as_bytes(...)``).
    Raises :class:`GcsFetchError` on a missing/mismatched generation or 404 —
    the SDK's ``NotFound`` / ``PreconditionFailed`` are lazily imported so unit
    tests need no ``google-cloud-storage`` install.
    """
    gen = _to_int_generation(generation)
    blob = bucket.blob(object_name, generation=gen)
    try:
        return blob.download_as_bytes(raw_download=True, if_generation_match=gen)
    except GcsFetchError:
        raise  # already a fail-closed fetch error (e.g. a test double) — propagate as-is
    except Exception as e:  # noqa: BLE001 — ANY download failure fails CLOSED
        # Convert EVERY download failure to GcsFetchError — not just NotFound /
        # PreconditionFailed (missing/mismatched generation) but ALSO transport /
        # permission / retry-exhaustion errors (Forbidden, ServiceUnavailable, ...).
        # The /apply + /propose gates call this POST-CLAIM, so a raw SDK exception
        # escaping here would 500 and strand the burned approval at phase="claimed"
        # with NO terminal audit (adversarial review C6a-3 — closes the new C6 sidecar
        # fetch AND the pre-existing C5 plan/json/metadata fetch exposure). The caller
        # always treats GcsFetchError as a fail-closed refusal, so collapsing the
        # error classes loses nothing.
        raise GcsFetchError(
            f"fetch of {object_name}@{gen} failed ({type(e).__name__}): {e}"
        ) from e


def derive_sidecar_uri(metadata_uri: str) -> str:
    """Derive the C6 ``iac-tree.json`` sidecar URI from the (HMAC-signed)
    ``metadata.json`` URI by swapping ONLY the trailing basename, then re-validate
    the result fail-closed.

    The sidecar lives in the SAME run dir as ``metadata.json``, so deriving its
    locator from the signed metadata path means the coordinator supplies only the
    sidecar *generation* (an unsigned endpoint field) — never its URI. Combined with
    the worker's field cross-check against the signed metadata, a compromised
    coordinator cannot point the worker at an arbitrary object."""
    bucket, obj = validate_artifact_uri(metadata_uri, expected_basename=_METADATA_BASENAME)
    sidecar_obj = obj[: -len(_METADATA_BASENAME)] + _SIDECAR_BASENAME
    sidecar_uri = f"gs://{bucket}/{sidecar_obj}"
    # Re-validate the derived URI (bucket + path scheme + basename) fail-closed.
    validate_artifact_uri(sidecar_uri, expected_basename=_SIDECAR_BASENAME)
    return sidecar_uri


def fetch_artifact(bucket: Any, *, signed_uri: str, generation: Any, expected_basename: str) -> bytes:
    """Validate the signed URI + fetch its exact bytes pinned to ``generation``.

    The one call the /apply + /propose handlers use per artifact: it ties the
    locator validation (bucket + path scheme + basename) to the pinned fetch so
    a caller cannot skip the allowlist check.
    """
    _bucket, obj = validate_artifact_uri(signed_uri, expected_basename=expected_basename)
    return fetch_object_pinned(bucket, obj, generation)
