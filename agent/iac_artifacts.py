"""Coordinator-side read of the C2 plan-builder artifact (Phase C5e-1).

The coordinator's read-only counterpart to the ``tofu-apply`` worker's fetch +
verify path. The GET ``/iac-approvals/{pr_number}`` route (C5e-2) renders an
approval page from the artifact a C2 ``tofu plan`` run already produced — without
minting an approval, issuing a worker token, or reading ``plan_approvals``. This
module supplies the three pieces that route needs:

1. **Parse the C2 PR comment** (:func:`parse_c2_pr_comment`,
   :func:`find_latest_c2_comment`) — extract the immutable artifact identity the
   plan-builder wrote, pure + fail-closed.
2. **Fetch the artifacts from GCS** (:func:`fetch_gcs_object`) — generation-pinned
   raw download with a fail-closed URI allowlist.
3. **Load + advisory-verify** (:func:`load_plan_view`) — fetch metadata.json +
   plan.json, recompute the ``plan_json`` digest, re-run the C1 denylist, and
   return an :class:`IacPlanView` for the template. The verification here is
   ADVISORY: the ``tofu-apply`` worker remains the authoritative both-artifact
   verifier at ``/apply``. The GET fetches only ``plan.json`` (not
   ``plan.tfplan``) because the read-only page never needs the binary plan, so we
   deliberately do NOT call ``driftscribe_lib.approvals.verify_artifact_integrity``
   (which requires BOTH artifacts) — we recompute the single ``plan_json`` digest
   here and let the worker do the full two-artifact check before it mutates.

**Deployment decoupling.** This module is part of the *coordinator* service, which
ships ``driftscribe_lib/`` but NOT ``workers/``. The worker's fetch/validate logic
(``workers/tofu_apply/gcs_fetch.py``) is a different deployable, so we mirror its
validation scheme LOCALLY here rather than importing it. The two copies share the
same regex + bucket-equality + basename rules by convention, not by code.

**Fail-closed everywhere.** Any fetch or validation failure surfaces as
:class:`IacArtifactError` (helper level) or, in :func:`load_plan_view`, as a view
with ``unverifiable=True`` so the GET route can always return 200 while suppressing
the Approve button.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from typing import Any


class IacArtifactError(Exception):
    """Fail-closed signal for any C2-artifact fetch or validation failure.

    The GET route maps this to a suppressed-Approve render (always-200); the POST
    route maps it to a 403. Raised by the fetch + URI-validation helpers; caught
    inside :func:`load_plan_view` and converted to ``unverifiable=True``.
    """


# --------------------------------------------------------------------------- #
# Fail-closed GCS URI validation — mirrors workers/tofu_apply/gcs_fetch.py.
#
# The object-path regex is byte-identical to the worker's _OBJECT_RE. The bucket
# name is NOT hardcoded here (unlike the worker's ARTIFACT_BUCKET const): the
# coordinator derives it from config as ``{project}-tofu-artifacts`` and passes it
# in, so this module never embeds the project name.
# --------------------------------------------------------------------------- #

_OBJECT_RE = re.compile(
    r"^pr-[1-9][0-9]*/[0-9a-f]{40}/run-[1-9][0-9]*-[1-9][0-9]*/(metadata\.json|plan\.tfplan|plan\.json)$"
)


def parse_gs_uri(uri: str) -> tuple[str, str]:
    """``gs://<bucket>/<object>`` → ``(bucket, object)``. Fail-closed."""
    if not isinstance(uri, str) or not uri.startswith("gs://"):
        raise IacArtifactError(f"not a gs:// uri: {uri!r}")
    rest = uri[len("gs://") :]
    bucket, _, obj = rest.partition("/")
    if not bucket or not obj:
        raise IacArtifactError(f"malformed gs:// uri (need bucket and object): {uri!r}")
    return bucket, obj


def validate_artifact_uri(
    uri: str, *, bucket_name: str, expected_basename: str
) -> tuple[str, str]:
    """Validate a (signed) artifact URI fail-closed and return ``(bucket, object)``.

    Asserts the bucket is exactly ``bucket_name`` (config-derived, NOT hardcoded)
    and the object path matches the ``pr-<N>/<sha>/run-<id>-<attempt>/<basename>``
    scheme with the expected basename. Cheap SSRF defense even though the URI is
    HMAC-signed upstream.
    """
    bucket, obj = parse_gs_uri(uri)
    if bucket != bucket_name:
        raise IacArtifactError(f"unexpected bucket {bucket!r} (want {bucket_name!r})")
    if not _OBJECT_RE.fullmatch(obj):
        raise IacArtifactError(f"object path does not match the artifact scheme: {obj!r}")
    if not obj.endswith("/" + expected_basename):
        raise IacArtifactError(f"object basename is not {expected_basename!r}: {obj!r}")
    return bucket, obj


def _to_int_generation(generation: Any) -> int:
    """Coerce a (numeric-string) generation to int, fail-closed."""
    try:
        return int(generation)
    except (TypeError, ValueError) as e:
        raise IacArtifactError(
            f"generation must be a numeric string (got {generation!r})"
        ) from e


def fetch_gcs_object(
    bucket_name: str, object_name: str, generation: str | int, *, client: Any = None
) -> bytes:
    """Fetch the EXACT object bytes pinned to ``generation``.

    ``client`` is a ``google.cloud.storage.Client`` (or a test double exposing
    ``.bucket(name).blob(name, generation=...).download_as_bytes(...)``). When
    ``None`` we lazily build a real ``storage.Client()`` — the project is picked up
    from ADC env (``GOOGLE_CLOUD_PROJECT`` / the runtime SA), so this module never
    embeds the project name.

    ``generation`` is pinned BOTH on ``.blob(generation=N)`` (read that exact
    revision) and via ``if_generation_match=N`` (conditional GET — a server-resolved
    mismatch raises ``PreconditionFailed``). ``raw_download=True`` returns the
    un-decoded stored bytes so ``hashlib.sha256`` matches the digest C2 computed
    over the on-disk file.

    Raises :class:`IacArtifactError` on a non-numeric generation or on the SDK's
    ``NotFound`` / ``PreconditionFailed``; lets any other exception propagate.
    """
    gen = _to_int_generation(generation)
    if client is None:
        from google.cloud import storage  # lazy: tests inject a double

        client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(object_name, generation=gen)
    try:
        return blob.download_as_bytes(raw_download=True, if_generation_match=gen)
    except Exception as e:  # noqa: BLE001 — narrow to GCS exceptions below, re-raise others
        try:
            from google.api_core.exceptions import NotFound, PreconditionFailed
        except Exception:  # pragma: no cover - SDK always present in the container
            raise e from None
        if isinstance(e, (NotFound, PreconditionFailed)):
            raise IacArtifactError(
                f"fetch of {object_name}@{gen} failed ({type(e).__name__}): {e}"
            ) from e
        raise


# --------------------------------------------------------------------------- #
# C2 PR-comment parsing — mirrors tools/iac_plan_diff_summary.py::format_summary.
# Pure, no I/O, fail-closed (returns None on any malformed/missing required field).
# --------------------------------------------------------------------------- #

_MARKER = "### DriftScribe IaC — `tofu plan` (Phase C2 plan-builder)"

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_DIGITS = re.compile(r"^[0-9]+$")

# Header bullets, verbatim from format_summary. Each is matched ANCHORED to a line
# (MULTILINE) and we require EXACTLY ONE match — a duplicate field is malformed.
_RE_HEAD_SHA = re.compile(r"^- \*\*head_sha:\*\* `([^`]*)`", re.MULTILINE)
_RE_PLAN_SHA = re.compile(
    r"^- \*\*plan_sha256:\*\* `([^`]*)` \(generation `([^`]*)`\)", re.MULTILINE
)
_RE_PLAN_JSON_SHA = re.compile(
    r"^- \*\*plan_json_sha256:\*\* `([^`]*)` \(generation `([^`]*)`\)", re.MULTILINE
)
_RE_META_GEN = re.compile(r"^- \*\*metadata generation:\*\* `([^`]*)`", re.MULTILINE)
_RE_URI_PLAN = re.compile(r"^- \*\*artifact plan\.tfplan:\*\* `([^`]*)`", re.MULTILINE)
_RE_URI_JSON = re.compile(r"^- \*\*artifact plan\.json:\*\* `([^`]*)`", re.MULTILINE)
_RE_URI_META = re.compile(r"^- \*\*artifact metadata\.json:\*\* `([^`]*)`", re.MULTILINE)
_RE_OPENTOFU = re.compile(r"^- \*\*opentofu:\*\* `([^`]*)`", re.MULTILINE)
# C6 sidecar (iac-tree.json). OPTIONAL in C6a-2 (old comments without these still
# parse); C6b-1 makes the coordinator REQUIRE generation_iac_tree on the create path.
_RE_IAC_TREE_GEN = re.compile(r"^- \*\*iac-tree generation:\*\* `([^`]*)`", re.MULTILINE)
_RE_IAC_TREE_HASH = re.compile(r"^- \*\*iac_tree_hash:\*\* `([^`]*)`", re.MULTILINE)

# The tofu-show <details> block: a backtick run (>=3) on its own line opens, a
# matching run closes. The fence width is chosen by format_summary._pick_fence.
_RE_TOFU_SHOW = re.compile(
    r"<details><summary>tofu show</summary>\n\n(`{3,})\n(.*?)\n\1\n</details>",
    re.DOTALL,
)


@dataclass(frozen=True)
class C2CommentRef:
    """Parsed identity fields from a C2 plan-builder PR comment.

    Every field except ``comment_id`` / ``tofu_show_text`` is REQUIRED and
    validated by :func:`parse_c2_pr_comment` (which returns ``None`` if any is
    missing or malformed). ``comment_id`` is the PyGithub comment ``.id`` (so the
    later signed-form token can pin which comment was rendered); ``tofu_show_text``
    is best-effort (empty string when the diff block is absent).
    """

    head_sha: str
    plan_sha256: str
    plan_json_sha256: str
    generation_plan: str
    generation_json: str
    generation_metadata: str
    artifact_uri_plan: str
    artifact_uri_json: str
    artifact_uri_metadata: str
    opentofu_version: str
    comment_id: int | None
    tofu_show_text: str
    # C6 sidecar identity (optional pre-C6 / on comments that predate it). The
    # coordinator passes generation_iac_tree to the worker on the create path and pins
    # iac_tree_hash into the CSRF form token; the worker independently re-derives the
    # sidecar URI from the signed metadata + cross-checks it (these are not a worker
    # security input — see the C6 plan §2).
    generation_iac_tree: str | None = None
    iac_tree_hash: str | None = None


def _single_match(rx: re.Pattern[str], body: str) -> list[str] | None:
    """Return the capture groups of the SOLE match, or ``None``.

    ``None`` when there are zero matches OR more than one (a duplicated field is
    treated as malformed — fail-closed, since we cannot know which copy is
    authoritative).
    """
    found = rx.findall(body)
    if len(found) != 1:
        return None
    m = found[0]
    return list(m) if isinstance(m, tuple) else [m]


def _optional_single_match(rx: re.Pattern[str], body: str) -> tuple[str | None, bool]:
    """Parse an OPTIONAL single-capture field. Returns ``(value, ok)``:
    absent → ``(None, True)`` (fine — optional); exactly one → ``(value, True)``;
    DUPLICATE → ``(None, False)`` (malformed, fail-closed). Used for the C6 sidecar
    lines, which may be absent on a pre-C6 comment."""
    found = rx.findall(body)
    if not found:
        return None, True
    if len(found) != 1:
        return None, False
    m = found[0]
    return (m if isinstance(m, str) else m[0]), True


def parse_c2_pr_comment(body: str, *, comment_id: int | None = None) -> C2CommentRef | None:
    """Parse a C2 plan-builder marker comment into a :class:`C2CommentRef`.

    Pure (no I/O). Returns ``None`` if the marker line is absent, or if ANY required
    field (the two sha256s, the three generations, the three ``gs://`` URIs,
    ``head_sha``, ``opentofu_version``) is missing, duplicated, or malformed.

    The ``tofu show`` diff is best-effort: a missing/unparseable diff block yields
    an empty ``tofu_show_text`` and does NOT fail the parse.
    """
    if not isinstance(body, str) or _MARKER not in body:
        return None

    head = _single_match(_RE_HEAD_SHA, body)
    plan = _single_match(_RE_PLAN_SHA, body)
    plan_json = _single_match(_RE_PLAN_JSON_SHA, body)
    meta_gen = _single_match(_RE_META_GEN, body)
    uri_plan = _single_match(_RE_URI_PLAN, body)
    uri_json = _single_match(_RE_URI_JSON, body)
    uri_meta = _single_match(_RE_URI_META, body)
    opentofu = _single_match(_RE_OPENTOFU, body)

    if None in (head, plan, plan_json, meta_gen, uri_plan, uri_json, uri_meta, opentofu):
        return None

    # mypy/readability: every group list is non-None past the guard above.
    head_sha = head[0]
    plan_sha256, generation_plan = plan[0], plan[1]
    plan_json_sha256, generation_json = plan_json[0], plan_json[1]
    generation_metadata = meta_gen[0]
    artifact_uri_plan = uri_plan[0]
    artifact_uri_json = uri_json[0]
    artifact_uri_metadata = uri_meta[0]
    opentofu_version = opentofu[0]

    # Field-level validation — fail-closed on any malformed value.
    if not _HEX40.fullmatch(head_sha):
        return None
    if not (_HEX64.fullmatch(plan_sha256) and _HEX64.fullmatch(plan_json_sha256)):
        return None
    if not all(
        _DIGITS.fullmatch(g)
        for g in (generation_plan, generation_json, generation_metadata)
    ):
        return None
    if not all(
        u.startswith("gs://")
        for u in (artifact_uri_plan, artifact_uri_json, artifact_uri_metadata)
    ):
        return None
    if not opentofu_version:
        return None

    # C6 sidecar (optional). Absent → None (a pre-C6 comment); present-but-malformed
    # or duplicated → fail-closed (the whole parse returns None).
    generation_iac_tree, ok_gi = _optional_single_match(_RE_IAC_TREE_GEN, body)
    iac_tree_hash, ok_hi = _optional_single_match(_RE_IAC_TREE_HASH, body)
    if not (ok_gi and ok_hi):
        return None
    if generation_iac_tree is not None and not _DIGITS.fullmatch(generation_iac_tree):
        return None
    if iac_tree_hash is not None and not _HEX64.fullmatch(iac_tree_hash):
        return None

    tofu_show_text = ""
    m = _RE_TOFU_SHOW.search(body)
    if m is not None:
        tofu_show_text = m.group(2)

    return C2CommentRef(
        head_sha=head_sha,
        plan_sha256=plan_sha256,
        plan_json_sha256=plan_json_sha256,
        generation_plan=generation_plan,
        generation_json=generation_json,
        generation_metadata=generation_metadata,
        artifact_uri_plan=artifact_uri_plan,
        artifact_uri_json=artifact_uri_json,
        artifact_uri_metadata=artifact_uri_metadata,
        opentofu_version=opentofu_version,
        comment_id=comment_id,
        tofu_show_text=tofu_show_text,
        generation_iac_tree=generation_iac_tree,
        iac_tree_hash=iac_tree_hash,
    )


def find_latest_c2_comment(repo: Any, pr_number: int) -> C2CommentRef | None:
    """Return the LATEST C2 marker comment on ``pr_number`` (newest wins), or ``None``.

    ``repo`` is a PyGithub ``Repository``. Lists the PR's issue comments, parses each
    with :func:`parse_c2_pr_comment`, and returns the parse of the comment with the
    greatest ``created_at`` among those that parse. A ``github.GithubException`` is
    re-raised as :class:`IacArtifactError` so the route maps it cleanly.
    """
    try:
        comments = repo.get_issue(pr_number).get_comments()
        candidates: list[tuple[Any, C2CommentRef]] = []
        for c in comments:
            ref = parse_c2_pr_comment(c.body, comment_id=c.id)
            if ref is not None:
                candidates.append((c.created_at, ref))
    except Exception as e:  # noqa: BLE001 — narrow to GithubException below, re-raise others
        try:
            from github import GithubException
        except Exception:  # pragma: no cover - PyGithub always present in the service
            raise
        if isinstance(e, GithubException):
            raise IacArtifactError(f"github comment listing failed: {e}") from e
        raise
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    return candidates[-1][1]


# --------------------------------------------------------------------------- #
# Plan view — fetch + advisory verify for the GET render.
# --------------------------------------------------------------------------- #

# The 14 c2.v1 metadata fields (excluding the output-only schema_version) that
# build_metadata validates, plus schema_version = 15 total in the rendered view.
_METADATA_INPUT_KEYS = (
    "repo",
    "pr_number",
    "head_sha",
    "base_sha",
    "workflow_run_id",
    "workflow_run_attempt",
    "artifact_uri_plan",
    "artifact_uri_json",
    "generation_plan",
    "generation_json",
    "plan_sha256",
    "plan_json_sha256",
    "opentofu_version",
    "provider_lockfile_sha256",
)


@dataclass
class IacPlanView:
    """Advisory render model for the GET ``/iac-approvals/{pr_number}`` page.

    ``metadata`` is the validated c2.v1 metadata dict (the 15 fields incl.
    ``schema_version``); convenience properties expose the identity fields the
    template + the later signed-form token need directly. ``integrity_ok`` is the
    recomputed ``plan_json`` digest match; ``denylist_violations`` is the re-run C1
    result; ``unverifiable`` is set on ANY fetch/validation failure and tells the
    template to suppress Approve (the GET still returns 200).
    """

    metadata: dict[str, Any] = field(default_factory=dict)
    tofu_show_text: str = ""
    integrity_ok: bool = False
    denylist_violations: list[tuple[str, str]] = field(default_factory=list)
    unverifiable: bool = False

    @property
    def head_sha(self) -> str:
        return str(self.metadata.get("head_sha", ""))

    @property
    def artifact_uri_metadata(self) -> str:
        return self._artifact_uri_metadata

    @property
    def generation_metadata(self) -> str:
        return self._generation_metadata

    @property
    def plan_sha256(self) -> str:
        return str(self.metadata.get("plan_sha256", ""))

    @property
    def plan_json_sha256(self) -> str:
        return str(self.metadata.get("plan_json_sha256", ""))

    @property
    def generation_iac_tree(self) -> str:
        """C6 sidecar generation (empty if the comment predates C6 / has no sidecar)."""
        return self._generation_iac_tree

    @property
    def iac_tree_hash(self) -> str:
        """C6 expected iac/-tree hash from the comment (advisory — for the page +
        the CSRF pin; the WORKER re-derives + cross-checks the real sidecar)."""
        return self._iac_tree_hash

    @property
    def has_create(self) -> bool:
        """True iff the plan CREATEs a resource (routes through the C6 merge-first
        two-step flow). Uses the shared ``plan_has_create`` predicate so the
        coordinator and worker agree; fail-closed (an unparsed plan ⇒ create-class)."""
        from driftscribe_lib.iac_plan_classify import plan_has_create

        return plan_has_create(self._plan_json)

    # The metadata URI + generation are taken from the C2 comment ref (the
    # metadata dict itself does NOT carry artifact_uri_metadata / generation_metadata
    # — those live only in the comment), so load_plan_view stashes them here.
    _artifact_uri_metadata: str = ""
    _generation_metadata: str = ""
    # C6 sidecar identity (from the comment ref) + the parsed plan.json (for has_create).
    _generation_iac_tree: str = ""
    _iac_tree_hash: str = ""
    _plan_json: dict | None = None


def load_plan_view(
    ref: C2CommentRef, *, bucket_name: str, client: Any = None
) -> IacPlanView:
    """Fetch + advisory-verify the C2 artifact referenced by ``ref``.

    Steps (all fail-closed; any :class:`IacArtifactError` → ``unverifiable=True``,
    never an exception, so the GET route stays always-200):

    1. Validate + fetch ``metadata.json`` pinned to ``ref.generation_metadata``.
    2. Assert it is c2.v1: round-trip the 14 input keys through
       ``driftscribe_lib.iac_plan_metadata.build_metadata`` (validates all fields +
       URI formats) and assert ``schema_version == "c2.v1"``. A malformed metadata
       sets ``unverifiable=True`` (suppress Approve) rather than crashing.
    3. Validate + fetch ``plan.json`` pinned to the metadata's ``generation_json``.
    4. Recompute ``sha256(plan_json)`` and constant-time compare to
       ``metadata.plan_json_sha256`` → ``integrity_ok``. (We do NOT use
       ``verify_artifact_integrity`` — it requires plan.tfplan too; the read-only
       GET fetches only plan.json. The worker remains the authoritative
       both-artifact verifier at /apply.)
    5. Re-run the C1 denylist on plan.json → ``denylist_violations``.
    """
    view = IacPlanView(
        tofu_show_text=ref.tofu_show_text,
        _artifact_uri_metadata=ref.artifact_uri_metadata,
        _generation_metadata=ref.generation_metadata,
        _generation_iac_tree=ref.generation_iac_tree or "",
        _iac_tree_hash=ref.iac_tree_hash or "",
    )

    # Step 1: fetch metadata.json.
    try:
        _bucket, meta_obj = validate_artifact_uri(
            ref.artifact_uri_metadata,
            bucket_name=bucket_name,
            expected_basename="metadata.json",
        )
        meta_bytes = fetch_gcs_object(
            bucket_name, meta_obj, ref.generation_metadata, client=client
        )
        md = json.loads(meta_bytes.decode("utf-8"))
    except (IacArtifactError, ValueError, UnicodeDecodeError):
        view.unverifiable = True
        return view

    # Step 2: assert c2.v1 via build_metadata round-trip.
    if not _assert_c2v1_metadata(md):
        view.unverifiable = True
        return view
    view.metadata = md

    # Step 3: fetch plan.json (URI + generation come from the validated metadata).
    try:
        _bucket, json_obj = validate_artifact_uri(
            md["artifact_uri_json"],
            bucket_name=bucket_name,
            expected_basename="plan.json",
        )
        plan_json_bytes = fetch_gcs_object(
            bucket_name, json_obj, md["generation_json"], client=client
        )
    except IacArtifactError:
        view.unverifiable = True
        return view

    # Step 4: recompute integrity (single-artifact, advisory — see docstring).
    actual = hashlib.sha256(plan_json_bytes).hexdigest()
    view.integrity_ok = hmac.compare_digest(actual, str(md["plan_json_sha256"]))

    # Step 5: re-run the C1 denylist.
    from driftscribe_lib.iac_plan_denylist import DenylistInput, evaluate, load_plan_json

    parsed, parse_v = load_plan_json(plan_json_bytes.decode("utf-8", errors="replace"))
    # Stash the parsed plan for the C6 has_create classification (None if it didn't
    # parse → has_create fail-closes to True, and the POST refuses on the parse-error
    # denylist violation below anyway).
    view._plan_json = parsed if parse_v is None else None
    if parse_v is not None:
        view.denylist_violations = [(parse_v.rule, parse_v.detail)]
    else:
        try:
            violations = evaluate(DenylistInput(plan=parsed))
        except Exception as e:  # noqa: BLE001 — lib is fail-closed on policy; any bug → deny
            view.denylist_violations = [("denylist-evaluation-error", str(e))]
        else:
            view.denylist_violations = [(v.rule, v.detail) for v in violations]

    return view


def _assert_c2v1_metadata(md: Any) -> bool:
    """True iff ``md`` is a valid c2.v1 metadata dict.

    Constructs a ``MetadataInput`` from the 14 input keys (``build_metadata`` adds
    ``schema_version`` in its output, so it is NOT a constructor field) and calls
    ``build_metadata`` — which raises ``ValueError`` on any malformed field/format.
    Also asserts the fetched dict declares ``schema_version == "c2.v1"``. Returns
    ``False`` (rather than raising) on any failure so the caller can set
    ``unverifiable``.
    """
    from driftscribe_lib.iac_plan_metadata import (
        METADATA_SCHEMA_VERSION,
        MetadataInput,
        build_metadata,
    )

    if not isinstance(md, dict):
        return False
    if md.get("schema_version") != METADATA_SCHEMA_VERSION:
        return False
    try:
        inp = MetadataInput(**{k: md[k] for k in _METADATA_INPUT_KEYS})
        build_metadata(inp)
    except (ValueError, KeyError, TypeError):
        return False
    return True
