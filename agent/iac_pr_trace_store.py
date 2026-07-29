"""Per-PR record of the reasoning trace that AUTHORED an infrastructure PR.

The approval desk's pending card offers "view the reasoning behind this →". On the
rollback arm that link is sound because the rollback decision is written BY the
proposing run, so its ``trace_id`` really is the authoring reasoning. The iac arms
have no such handle: an ``iac_apply`` decision's ``trace_id`` is stamped inside the
**approve/apply POST**, a different HTTP request from the crew run that authored the
PR, and the listing arm (``GET /infra/pending-approvals``) has no decision document
at all. This store supplies the missing association.

Design notes (``iac_pr_source_cache.py`` is the shape template — same lazy client,
Protocol + in-memory twin, fail-soft posture):

* **One document per (repo, PR)**, written at the moment the editor worker confirms a
  NEWLY OPENED PR — the same ``iac_pr_pointer`` gate that decides whether to notify the
  operator. The pointer is derived from the worker's own result, never from model
  prose, so the association cannot be forged by the LLM.

* ⚠️ **The key is repo-scoped, and that is load-bearing.** PR numbers are
  repository-local, and the two ends of this feature genuinely disagree about which
  repository they mean: authoring targets ``resolve_iac_editor_target()``, which honors
  ``IAC_EDITOR_TARGET_REPO_OVERRIDE``, while the pending listing reads
  ``Settings.github_repo``. The codebase already treats that divergence as live (see
  ``adk_tools.py``'s dupe-guard, which queries the target repo "not
  settings.github_repo", and the test that pins it). Keying on the bare PR number would
  let target-repo B's trace render as evidence on listing-repo A's PR #7 — an
  over-claim, which is the one failure mode this whole feature must not have. A missing
  link is fine; a *wrong* link is not.

* **The body re-states its own identity.** ``repo`` and ``pr_number`` are stored
  verbatim and re-checked on read against what the caller asked for, even though the
  document id already derives from both. This is **schema integrity, not
  authentication**: it rules out a digest collision, a corrupt record, or a document
  keyed by a future version of ``_doc_id`` that disagrees with this one. It does NOT
  defend against an authorized Firestore writer — the id is derived from public inputs,
  so anyone with write access to the default database can compute it and store a
  matching body. Firestore IAM is the real boundary there, as it is for every other
  collection the coordinator reads.

* **First writer wins** (:meth:`set_if_absent`), via ``create()`` with ``AlreadyExists``
  narrowly swallowed — the same idiom as ``state_store.record_event``. This guards
  races and any future call path. (It is *not* justified by the adoption dupe-guard,
  which returns ``status: "rejected"`` and never reaches the authoring tail.)

* **Fail-soft both directions.** A read error degrades to ``None`` (the card simply
  renders no link) and a write error is logged and swallowed. This is evidence, not a
  gate: nothing is released on the strength of this record, and its absence is a
  silent, honest "we do not know". Contrast the rollback approval record (ds-y5i),
  which is fail-CLOSED precisely because a credential ships on its strength. The
  identity checks above are the reason fail-soft is safe here — availability may
  degrade, but identity may not.

* The Firestore client is constructed **lazily on first use** so the backend-selection
  branch can instantiate the store without GCP creds.

The accessor lives in THIS module rather than in ``agent.main`` (where
``iac_pr_source_cache``'s does), because ``adk_tools`` and ``fanout`` write while
``main`` reads: hanging it off ``main`` would point the dependency the wrong way and
let writers and readers bind different in-memory instances.

The coordinator runtime SA already holds ``roles/datastore.user``.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Protocol

log = logging.getLogger("driftscribe.agent.iac_pr_trace_store")

# Dedicated collection (one doc per repo+PR) — separate from ``iac_pr_source``, whose
# documents have a different lifecycle (overwritten on every push, head_sha-gated).
_COLLECTION = "iac_pr_trace"

# The wire format of a trace id (driftscribe_lib.logging.new_trace_id → uuid4().hex),
# and exactly what the SPA's ``?reasoning=`` deep link accepts
# (frontend/src/lib/deeplink.ts::isReplayableTraceId). Validated on the way IN so a
# malformed id never burns the first-writer slot, and again on the way OUT so a
# document written by an older or foreign writer cannot reach the DTO.
_HEX32_RE = re.compile(r"\A[0-9a-f]{32}\Z")


def is_replayable_trace_id(value: object) -> bool:
    """True for a trace id the SPA can round-trip through ``?reasoning=``.

    Kept in sync with ``isReplayableTraceId`` (frontend/src/lib/deeplink.ts): the desk
    must never be offered a link that opens once but fails to restore when shared, so
    both ends apply the same shape.
    """
    return isinstance(value, str) and _HEX32_RE.match(value) is not None


def _doc_id(repo: str, pr_number: int) -> str:
    """Firestore document id for one (repo, PR) pair.

    A digest, not ``f"{repo}#{pr}"``, because a repo slug contains ``/`` — which is a
    path separator in a Firestore document id, not a legal character in one. The NUL
    separator keeps the pair unambiguous (no repo name can contain it), so
    ``("a/b", 12)`` and ``("a/b1", 2)`` cannot collide by concatenation.
    """
    return hashlib.sha256(f"{repo}\x00{pr_number}".encode()).hexdigest()


class IacPrTraceStore(Protocol):
    def get(self, repo: str, pr_number: int) -> str | None: ...
    # True iff THIS call durably created the record. False covers both "another writer
    # got there first" and a swallowed write failure — callers treat them identically
    # (there is nothing useful to do about either).
    def set_if_absent(self, repo: str, pr_number: int, trace_id: str) -> bool: ...


class InMemoryIacPrTraceStore:
    """Process-local double. Used when no GCP project is configured (and, via the test
    injection seam, in tests). Applies the SAME identity and shape checks as the
    Firestore twin, so a test cannot pass against behavior prod does not have."""

    def __init__(self) -> None:
        self._traces: dict[str, str] = {}

    def get(self, repo: str, pr_number: int) -> str | None:
        trace_id = self._traces.get(_doc_id(repo, pr_number))
        return trace_id if is_replayable_trace_id(trace_id) else None

    def set_if_absent(self, repo: str, pr_number: int, trace_id: str) -> bool:
        if not repo or not is_replayable_trace_id(trace_id):
            return False
        key = _doc_id(repo, pr_number)
        if key in self._traces:
            return False
        self._traces[key] = trace_id
        return True


class FirestoreIacPrTraceStore:
    """Firestore-backed store. ``client`` is injectable for tests; when omitted, a real
    client is constructed lazily on first ``get``/``set_if_absent`` (inside the
    fail-soft guard, so a construction/auth error degrades to a miss too)."""

    def __init__(self, project: str, client: Any = None) -> None:
        self._project = project
        self._client = client
        self._collection_ref = None  # built lazily

    def _collection(self):
        if self._collection_ref is None:
            if self._client is None:
                from google.cloud import firestore

                self._client = firestore.Client(project=self._project)
            self._collection_ref = self._client.collection(_COLLECTION)
        return self._collection_ref

    def get(self, repo: str, pr_number: int) -> str | None:
        try:
            snap = self._collection().document(_doc_id(repo, pr_number)).get()
            if not snap.exists:
                return None
            data = snap.to_dict() or {}
            # Identity re-check — schema integrity, not authentication. Rules out a
            # digest collision, a corrupt record, or a document keyed by a _doc_id that
            # disagrees with this one. See the module docstring for what it does NOT do.
            #
            # Type-strict on purpose: Python equality would accept a stored ``True``
            # for requested PR 1, and ``7.0`` for 7. A schema check that tolerates the
            # wrong type is not checking the schema.
            stored_pr = data.get("pr_number")
            if type(stored_pr) is not int or isinstance(stored_pr, bool):
                log.warning(
                    "iac_pr_trace_identity_mismatch", extra={"pr_number": pr_number}
                )
                return None
            if data.get("repo") != repo or stored_pr != pr_number:
                log.warning(
                    "iac_pr_trace_identity_mismatch", extra={"pr_number": pr_number}
                )
                return None
            trace_id = data.get("trace_id")
            return trace_id if is_replayable_trace_id(trace_id) else None
        except Exception as e:  # noqa: BLE001 — evidence lookup must never raise into the request
            # Log the exception TYPE, not str(e): a PermissionDenied's message embeds
            # the full document resource path (project id + collection).
            log.warning("iac_pr_trace_read_failed", extra={"error": type(e).__name__})
            return None

    def set_if_absent(self, repo: str, pr_number: int, trace_id: str) -> bool:
        if not repo or not is_replayable_trace_id(trace_id):
            # Never persist a shape the read side would refuse anyway — that would burn
            # the first-writer slot on a value that can never render a link.
            log.warning("iac_pr_trace_write_refused", extra={"pr_number": pr_number})
            return False
        try:
            from google.api_core.exceptions import AlreadyExists
            from google.cloud import firestore

            # ``create`` (not ``set``) is what makes this first-writer-wins: it raises
            # AlreadyExists rather than overwriting.
            self._collection().document(_doc_id(repo, pr_number)).create(
                {
                    "repo": repo,
                    "pr_number": pr_number,
                    "trace_id": trace_id,
                    "created_at": firestore.SERVER_TIMESTAMP,
                }
            )
            return True
        except AlreadyExists:
            # Expected, not a fault: an earlier writer already recorded this PR.
            log.debug("iac_pr_trace_already_recorded", extra={"pr_number": pr_number})
            return False
        except Exception as e:  # noqa: BLE001 — a write failure must not fail PR authoring
            log.warning("iac_pr_trace_write_failed", extra={"error": type(e).__name__})
            return False


# --------------------------------------------------------------------------- #
# Process-wide singleton + test seam (see the module docstring for why it lives
# here rather than in agent.main).
# --------------------------------------------------------------------------- #
_store_singleton: "IacPrTraceStore | None" = None
_store_override: "IacPrTraceStore | None" = None


def get_iac_pr_trace_store() -> IacPrTraceStore:
    """Return the process-wide authoring-trace store singleton.

    Gates on ``gcp_project`` (NOT ``dry_run``), matching every other Firestore-backed
    store here: a DRY_RUN deployment with a project still has real Firestore, and a
    project-less one has none regardless of the flag.
    """
    global _store_singleton
    if _store_override is not None:
        return _store_override
    if _store_singleton is None:
        from agent.config import get_settings

        s = get_settings()
        if s.gcp_project:
            _store_singleton = FirestoreIacPrTraceStore(project=s.gcp_project)
        else:
            _store_singleton = InMemoryIacPrTraceStore()
    return _store_singleton


def record_authoring_trace(repo: str, pr_number: int) -> bool:
    """Record the CURRENT request's trace as the authoring reasoning for ``pr_number``.

    The single entry point both authoring paths call, so the rule below is stated once
    rather than duplicated at two call sites that could drift.

    ⚠️ Reads :func:`get_trace_id`, **never** ``current_trace_id_or_new`` — the latter
    MINTS a fresh id when the ContextVar is unset, which would persist an id with no
    logged reasoning behind it and render a link that opens an empty timeline. No trace
    in context → no record → no link.

    Never raises: an evidence write must not be able to break PR authoring. The import
    sits INSIDE the guard too — a module-level-import failure is unlikely, but "never
    raises" has to mean never.
    """
    try:
        from driftscribe_lib.logging import get_trace_id

        trace_id = get_trace_id()
        if not is_replayable_trace_id(trace_id):
            log.warning("iac_pr_trace_unavailable", extra={"pr_number": pr_number})
            return False
        return get_iac_pr_trace_store().set_if_absent(repo, pr_number, trace_id)
    except Exception as e:  # noqa: BLE001 — never break PR authoring over evidence
        log.warning("iac_pr_trace_record_failed", extra={"error": type(e).__name__})
        return False


def _set_iac_pr_trace_store_for_tests(store: IacPrTraceStore) -> None:
    """Test-only: inject a store via the override seam."""
    global _store_override
    _store_override = store


def _reset_iac_pr_trace_store_for_tests() -> None:
    """Test-only: clear the singleton + injection override."""
    global _store_singleton, _store_override
    _store_singleton = None
    _store_override = None
