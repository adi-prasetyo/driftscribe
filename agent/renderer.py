import re

from agent.models import ContractStatus, DecisionProposal, EnvDiff
from agent.secret_guard import redact_text, should_redact, value_looks_credentialed

# A conservative ``owner/repo`` shape (exactly one slash, GitHub-legal chars) so a
# misconfigured ``github_repo`` can't form a surprising URL. Defense in depth: the
# frontend re-validates the host via safeGithubHref before it becomes an anchor.
_REPO_SHAPE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

_REDACTED = "`(value redacted: secret-like)`"
#: JSON-field counterpart to ``_REDACTED``. That one is a Markdown TABLE CELL
#: (backticked, so it renders as code inside the evidence table); this one goes
#: into a structured ``diffs[]`` value that consumers read as data, where
#: backticks would be noise. Matches the marker
#: :func:`_scrub_secret_values_from_rationale` already substitutes, so a value
#: redacted in the rationale and in its diff entry reads identically.
_REDACTED_PLAIN = "(redacted)"


def _escape_markdown_cell(s: str) -> str:
    """Escape characters that would break a markdown table cell.

    - `|` is escaped (column separator).
    - Backticks are escaped (closes inline-code span).
    - CR/LF are replaced with the literal text ``\\n`` so a multi-line value
      doesn't shatter the row.
    """
    return (
        s.replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
        .replace("|", "\\|")
        .replace("`", "\\`")
    )


def _format_value_cell(name: str, value: str | None) -> str:
    """Render a value cell. Redacts when name is secret-like OR value looks
    like a credential (e.g. URL with ``user:pass@`` authority).

    Empty string is NOT collapsed to "—" — an empty live value is a real drift
    signal (var was explicitly unset) and must be distinguishable from missing.
    """
    if should_redact(name, value):
        return _REDACTED if value is not None else "`—`"
    if value is None:
        return "`—`"
    return f"`{_escape_markdown_cell(value)}`"


def _format_name_cell(name: str) -> str:
    return f"`{_escape_markdown_cell(name)}`"


def _format_pr_cell(name: str, url: str | None, diff_values: tuple[str | None, ...]) -> str:
    if url is None:
        return "—"
    # Redact if name is secret-like, value looks credentialed, or the URL itself
    # carries credentials (e.g. ?token=...).
    if should_redact(name, url) or any(value_looks_credentialed(v) for v in diff_values):
        return "(redacted)"
    return _escape_markdown_cell(url)


def _diff_row(d: EnvDiff) -> str:
    return (
        f"| {_format_name_cell(d.name)} | {_format_value_cell(d.name, d.expected)} | "
        f"{_format_value_cell(d.name, d.live)} | "
        f"`{d.contract_status.value}` | "
        f"{_format_pr_cell(d.name, d.recent_pr_match, (d.expected, d.live, d.debug_config_value))} | "
        f"{_format_value_cell(d.name, d.debug_config_value)} |"
    )


def _evidence_table(proposal: DecisionProposal) -> str:
    header = "| Var | Expected | Live | Status | Recent PR | /debug/config |\n|---|---|---|---|---|---|"
    rows = "\n".join(_diff_row(d) for d in proposal.env_diffs)
    return f"{header}\n{rows}"


def _scrub_secret_values_from_rationale(rationale: str, diffs: list[EnvDiff]) -> str:
    """If the LLM rationale string contains any sensitive value, replace it
    with a redaction marker. Sensitive = value from a secret-named var, or
    a credentialed URL, or any recent_pr_match URL for a secret-named var.

    Defense-in-depth against the LLM quoting the actual secret in prose.
    """
    scrubbed = rationale
    seen: set[str] = set()

    def _scrub(v: str | None) -> None:
        nonlocal scrubbed
        if v and v not in seen and len(v) >= 4:
            scrubbed = scrubbed.replace(v, "(redacted)")
            seen.add(v)

    for d in diffs:
        for v in (d.expected, d.live, d.debug_config_value):
            if should_redact(d.name, v):
                _scrub(v)
        # PR URL for a secret-named var (it might appear in rationale prose too)
        if should_redact(d.name, d.recent_pr_match):
            _scrub(d.recent_pr_match)
    return scrubbed


def _coerce_env_diffs(raw: object) -> list[EnvDiff]:
    """Rebuild ``EnvDiff`` objects from a persisted decision's ``diffs[]``
    (plain dicts from ``model_dump``) so they can feed
    :func:`_scrub_secret_values_from_rationale` at serve time.

    Defensive — the doc is whatever Firestore holds (possibly malformed or
    legacy). Non-dict entries are skipped. Missing/invalid ``contract_status``
    defaults to ``ABSENT`` (the scrubber never reads it). A non-string ``name``
    becomes ``""`` so a credentialed-URL value is still caught by value
    (``value_looks_credentialed``), not dropped. Non-string value fields
    collapse to ``None``.
    """
    if not isinstance(raw, list):
        return []
    out: list[EnvDiff] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            status = ContractStatus(item.get("contract_status"))
        except (ValueError, TypeError):
            status = ContractStatus.ABSENT

        def _s(key: str) -> str | None:
            v = item.get(key)
            return v if isinstance(v, str) else None

        name = item.get("name")
        out.append(
            EnvDiff(
                name=name if isinstance(name, str) else "",
                expected=_s("expected"),
                live=_s("live"),
                contract_status=status,
                debug_config_value=_s("debug_config_value"),
                recent_pr_match=_s("recent_pr_match"),
            )
        )
    return out


def scrub_decision_rationale(decision: object) -> object:
    """Serve-time defense: return the decision doc with its free-text
    ``rationale`` scrubbed of any secret-like value present in its own
    ``diffs[]``. Closes the raw-rationale leak on every decision serve/return
    boundary (GET /trace, /decisions, /runs; POST /recheck), including
    already-persisted docs — no Firestore backfill. (POST /eventarc fast-acks
    since 2026-07-29 and no longer serves a decision body, so it's off this
    list.)

    ALSO redacts secret-like values inside ``diffs[]`` — see
    :func:`_scrub_diff_secret_values`. The doc is otherwise returned verbatim
    (the decision is unredacted by design; ``rendered_body`` is already
    scrubbed at persist). Never mutates the input: returns it unchanged BY
    IDENTITY when there is nothing to scrub, else a shallow copy carrying the
    new ``rationale`` and/or ``diffs``. Accepts ``object`` and returns non-dict
    inputs as-is; never raises.

    Intentionally idempotent: an already-redacted rationale stays unchanged
    (the raw diff values are gone, so :func:`_scrub_secret_values_from_rationale`
    finds nothing to replace and we return the same object).
    """
    if not isinstance(decision, dict):
        return decision
    out = decision
    rationale = decision.get("rationale")
    if isinstance(rationale, str) and rationale:
        scrubbed = _scrub_secret_values_from_rationale(
            rationale, _coerce_env_diffs(decision.get("diffs"))
        )
        if scrubbed != rationale:
            out = {**out, "rationale": scrubbed}
    diffs = _scrub_diff_secret_values(decision.get("diffs"))
    if diffs is not None:
        out = {**out, "diffs": diffs}
    return out


#: Value cells inside a persisted ``diffs[]`` entry. ``name`` is deliberately
#: absent — the VARIABLE NAME is not the secret and the operator needs it to
#: know what drifted; it is also the input to ``should_redact``.
_DIFF_VALUE_KEYS: tuple[str, ...] = (
    "expected",
    "live",
    "debug_config_value",
    "recent_pr_match",
)


def _scrub_diff_secret_values(raw: object) -> list[object] | None:
    """Redact secret-like values inside a persisted ``diffs[]``, or ``None``
    when nothing needed changing (so the caller can keep identity).

    The same value was being treated three different ways in ONE
    document: redacted in ``rendered_body`` (``_format_value_cell`` applies
    ``should_redact``), redacted in ``rationale``
    (:func:`_scrub_secret_values_from_rationale`), and published RAW in
    ``diffs[]``. That is not a policy, it is an oversight — the two scrubs
    beside it already decided this value class is not for publication, and
    ``GET /decisions`` is served to anonymous visitors during a public demo
    window (``infra/cloudflare/worker/src/proxy.js``).

    Same rule as its two neighbours (``should_redact``: secret-like NAME or
    credentialed-looking VALUE), so a non-secret drift value — ``PAYMENT_MODE``
    moving to ``live``, the whole point of the demo — still renders in full.
    This is not a new redaction policy; it is the existing one, applied
    consistently.

    NOT ds-x1w, and does not close it. That bead asks for a strictly stronger
    boundary — no OBSERVED env value on the approval doc or the rendered
    approval page by any lane — on the grounds that ``is_secret_name`` cannot
    see a credential hiding under an innocuous name. This function inherits
    exactly that weakness, and it guards a different surface (the ``/decisions``
    family, not ``agent/templates/approval.html``). Coherence fix here;
    ds-x1w's value-free-``reason`` decision is still open.

    Serve-time, like the rationale scrub, so it also covers rows already in
    Firestore with no backfill.
    """
    if not isinstance(raw, list):
        return None
    out: list[object] = []
    changed = False
    for entry in raw:
        if not isinstance(entry, dict):
            out.append(entry)
            continue
        name = entry.get("name")
        # A non-str name cannot be classified by ``is_secret_name``; treat it as
        # unknown and let ``should_redact`` judge the VALUE alone rather than
        # skipping the entry (fail toward redaction, not away from it).
        name_s = name if isinstance(name, str) else ""
        patched: dict[str, object] | None = None
        for key in _DIFF_VALUE_KEYS:
            value = entry.get(key)
            if not isinstance(value, str):
                continue
            if not should_redact(name_s, value):
                continue
            # Already redacted ⇒ NOT a change. ``should_redact`` still returns
            # True for the marker itself when the var is secret-NAMED, so
            # without this the function would rewrite ``(redacted)`` to
            # ``(redacted)``, report "changed", and hand back a fresh object
            # every call — breaking the idempotency/identity contract
            # :func:`scrub_decision_rationale` promises (and which its callers
            # rely on to avoid copying every row on every serve).
            if value == _REDACTED_PLAIN:
                continue
            if patched is None:
                patched = dict(entry)
            patched[key] = _REDACTED_PLAIN
        if patched is None:
            out.append(entry)
        else:
            out.append(patched)
            changed = True
    return out if changed else None


def scrub_rationale_text(rationale: str, env_diffs: list[EnvDiff]) -> str:
    """Public wrapper over the rationale scrubber for callers holding typed
    ``EnvDiff`` objects (the rollback worker ``reason`` boundary, where the
    approval page renders the string). Decision-doc callers should use
    :func:`scrub_decision_rationale` instead."""
    return _scrub_secret_values_from_rationale(rationale, env_diffs)


# The Rollback Worker's ``ProposeRequest.reason`` bound (ds-j0i). Kept equal to
# the worker's ``max_length`` on purpose: the worker cap is the trust-boundary
# invariant, this clamp is the guarantee that ordinary producer output conforms
# to the deployed wire contract. Raising one without the other re-opens the
# outage, so they are asserted equal by test_propose_reason_boundary.
ROLLBACK_REASON_MAX_CHARS = 2000

# Unicode characters, matching pydantic's ``max_length`` unit — NOT encoded
# bytes. A JA rationale would fail a byte-based clamp while passing the worker's.
_OMISSION_MARKER = "\n\n[… {n} characters omitted by DriftScribe …]\n\n"

# ds-j0i: ``DecisionProposal.rationale`` has no minimum length while the worker
# requires ``min_length=1``, so an empty model rationale is its own 422 — the
# same outage by a different route. This is what the operator sees instead.
ROLLBACK_REASON_ABSENT = (
    "DriftScribe proposed this rollback without a model rationale. Inspect the "
    "recorded change evidence and target revision before approving."
)


def clamp_middle_out(
    text: str, cap: int, *, reserve: int = 0
) -> tuple[str, str, str] | None:
    """Split ``text`` middle-out to fit ``cap``. Pure arithmetic, no policy.

    Returns ``(head, marker, tail)``, or ``None`` when ``text`` already fits and
    no cut is needed. Callers join the three parts; the split is exposed rather
    than the joined string because a Markdown-aware caller must repair the head
    and the tail INDEPENDENTLY (an opener stranded in the head is a different
    artifact from an orphan closer stranded in the tail), and it cannot recover
    that boundary from joined output — model text may contain the marker itself.

    ``reserve`` is space the caller will spend on its own repairs, subtracted
    from the budget BEFORE the cut. That ordering is the whole point: a clamp
    that cuts to ``cap`` and then appends a fence closer emits ``cap + 4`` and
    recreates the 422 it exists to prevent. The Notifier reserves its closer
    unconditionally for the same reason (``workers/notifier/main.py``).

    Half the remaining budget goes to each side, odd character to the tail. Tail
    preservation is not symmetry for its own sake — see
    :func:`normalize_rollback_reason` for what lives at the end of a rationale.
    """
    if cap <= 0:
        # A generic helper cannot inherit the single-caller fallback that used
        # to live below: ``text[:negative]`` slices from the END under Python's
        # negative-index semantics and would emit almost the whole string while
        # claiming to have clamped it. Callers pass a module constant, so a
        # non-positive cap is a programming error, not an input to absorb.
        raise ValueError(f"cap must be positive, got {cap}")
    # ``cap``, NOT ``cap - reserve``. The reserve pays for repairs a CUT makes,
    # so it has no bearing on whether a cut is needed — subtracting it here
    # truncated bodies the consumer would have accepted verbatim (9993..10000
    # against a 10000 cap, found in Codex review of this change).
    if len(text) <= cap:
        return None

    # Size the marker against the WHOLE length first, so the real marker (whose
    # number is smaller) can never be longer than the space reserved for it.
    reserved = len(_OMISSION_MARKER.format(n=len(text))) + reserve
    budget = cap - reserved
    if budget <= 0:
        # Cap too small to hold a marker at all. Emit head-only, still bounded.
        return text[: max(0, cap - reserve)], "", ""
    head_budget = budget // 2
    tail_budget = budget - head_budget
    omitted = len(text) - head_budget - tail_budget
    return (
        text[:head_budget],
        _OMISSION_MARKER.format(n=omitted),
        text[len(text) - tail_budget :],
    )


def normalize_rollback_reason(scrubbed: str) -> str:
    """Bound an already-SCRUBBED rationale to the worker's ``reason`` contract.

    ds-j0i, proven on prod 2026-07-31: the coordinator sent this field
    unbounded while ``workers/rollback`` then declared
    ``reason: Field(min_length=1, max_length=500)``. A 581-char model rationale
    produced a pydantic ``string_too_long`` 422, the approval was never minted,
    and autonomous self-heal died one step after the ds-q38 fix had correctly
    evicted the poisoned row. (The cap is now 2000 on both sides; ``min_length``
    still makes an EMPTY rationale its own 422, handled below.)

    **Order matters: scrub FIRST, then clamp.** Redaction replaces values with
    ``(redacted)`` and so changes length; clamping first could hand the worker a
    string that is over the cap again after scrubbing, or cut a credential in
    half so the scrubber no longer matches it.

    **Truncation preserves the TAIL, not just the head.** The model puts its
    operator caveat last — the rationale that triggered this bug ended "...the
    configuration of the chosen candidate is unverified and should be checked by
    the operator prior to approval." A head-only truncation silently drops
    exactly the sentence an operator most needs before approving a rollback, so
    roughly half the budget is reserved for the tail and the omission is stated
    in DriftScribe's own voice rather than left to look like the model stopped
    talking.

    No second model call to summarize: that could discard the very caveat this
    is protecting.
    """
    text = (scrubbed or "").strip()
    if not text:
        return ROLLBACK_REASON_ABSENT
    parts = clamp_middle_out(text, ROLLBACK_REASON_MAX_CHARS)
    if parts is None:
        return text
    head, marker, tail = parts
    return (
        head
        + marker
        + tail
    )


# The Notifier's ``NotifyRequest.body`` bound (ds-thm). Mirrored here and
# pinned equal by tests/unit/test_worker_bound_mirrors.py. Mirrored rather than
# imported for the reason stated at ``agent/validator.py``: the coordinator must
# not take a BUILD-time dependency on a worker package. A TEST-time import is
# fine, and is exactly what keeps the two honest.
NOTIFIER_BODY_MAX_CHARS = 10000

# Fence repair, deliberately DUPLICATED from ``workers/notifier/main.py``
# instead of hoisted into ``driftscribe_lib``. That package is split
# coordinator/worker, so sharing this would couple their deploys (the
# infra-drift worker-skew canary is the prior incident) to share a heuristic
# the two sides may legitimately want to evolve apart.
#
# Runs of three-or-more backticks, not occurrences of the literal "```": a
# six-backtick run is ONE delimiter opening a block, while ``count("```")``
# reports two and concludes, wrongly, that nothing needs repairing.
_FENCE_RUN = re.compile(r"`{3,}")
_FENCE_OPEN = "```\n"
# Reserved UNCONDITIONALLY, before the cut. A clamp that cuts to the cap and
# THEN prepends an opener emits cap+4 and recreates the 422 it exists to
# prevent. Only the TAIL repair needs budget — the head repair below removes
# text rather than adding it. Cost is 4 unused characters on the common path.
_FENCE_RESERVE = len(_FENCE_OPEN)


def _fence_delimiters(text: str) -> int:
    return len(_FENCE_RUN.findall(text))


def _close_open_fence_in_head(head: str) -> str:
    """Drop an unmatched fence opener from ``head`` instead of closing it.

    The obvious repair — append ``\\n``` `` — is WRONG for a longer delimiter:
    CommonMark requires a closing fence at least as long as the opening one, so
    a block opened with four or six backticks is NOT closed by three. It looks
    repaired to a backtick counter while a real parser leaves the block open
    through the approval URL, rendering it inert. Codex reproduced exactly that
    against a Markdown parser; the six-backtick test that "passed" was
    asserting regex parity, not parse state.

    Removing the unmatched run and everything after it in the head is correct
    for ANY delimiter length, and it can only shrink the output — which is why
    the head needs no share of the reserve. The discarded text was the start of
    a code block whose remainder the cut had already deleted.
    """
    runs = list(_FENCE_RUN.finditer(head))
    if len(runs) % 2 == 0:
        return head
    return head[: runs[-1].start()]


def normalize_notifier_body(body: str) -> str:
    """Bound a notification body to the Notifier's ``body`` contract (ds-thm).

    The Notifier already fits bodies to Discord's 2000-char ``content`` limit,
    so this is not that cut — it is the one that keeps the request from being
    rejected by ``NotifyRequest`` at 10000 with a 422, before any of the
    worker's own handling runs.

    **Why this cut must be Markdown-aware even though the worker repairs
    fences.** ``_discord_safe_content`` repairs only the ``content`` field. The
    handler sets ``text`` to the FULL body, on purpose — *"Only ``content`` is
    capped. ``text`` deliberately carries the FULL body … so a non-Discord
    receiver loses nothing to a limit that isn't theirs."* So a cut here that
    strands a code fence reaches Slack and every generic receiver as malformed
    Markdown, with nothing downstream to fix it.

    Two distinct artifacts, repaired independently:

    - an opener retained in the HEAD whose closer fell in the deleted middle —
      DROPPED, not closed (see :func:`_close_open_fence_in_head`: a four- or
      six-backtick opener cannot be closed by three);
    - an orphan closer retained in the TAIL whose opener was deleted — that
      closer would otherwise act as an OPENER in the reassembled text. Prepend
      a three-backtick opener so it closes what it was always closing. Safe at
      any delimiter length, because a closing fence may be LONGER than its
      opener. This faithfully reproduces the original: a tail holding an orphan
      closer *was* inside a code block before the cut.

    After both repairs the delimiter count preceding the tail's final segment
    is even, so the approval URL sits outside any block — which is the property
    ``tests/integration/test_notify_preserves_approval_url.py`` pins end to end
    against the real renderer.

    **Honest about what this is:** a parity heuristic, not a Markdown parser,
    exactly as the Notifier's own copy says of itself. It counts backtick runs
    that may be inline spans rather than fences, and it ignores ``~~~`` fences
    entirely. It is a best effort on a last-resort path — the alternative to a
    cut here is no notification at all.
    """
    text = body or ""
    parts = clamp_middle_out(text, NOTIFIER_BODY_MAX_CHARS, reserve=_FENCE_RESERVE)
    if parts is None:
        return text
    head, marker, tail = parts
    head = _close_open_fence_in_head(head)
    if _fence_delimiters(tail) % 2:
        tail = _FENCE_OPEN + tail
    return head + marker + tail


# The Upgrade Docs worker's ``ClosePrRequest.reason`` bound (ds-thm). Mirrored;
# pinned equal by tests/unit/test_worker_bound_mirrors.py.
UPGRADE_CLOSE_REASON_MAX_CHARS = 1000

# The worker requires ``min_length=1``, and the model can omit the reason
# entirely. Closing the PR is the action the OPERATOR asked for and the reason
# is auxiliary audit context, so the action is preserved rather than failed —
# but the text discloses that nothing was supplied instead of inventing a
# motive the model never gave.
UPGRADE_CLOSE_REASON_ABSENT = (
    "Closed via DriftScribe at the operator's request; no reason was supplied."
)


def normalize_close_reason(reason: str) -> str:
    """Bound a model-authored PR-close reason to the worker's contract."""
    text = (reason or "").strip()
    if not text:
        return UPGRADE_CLOSE_REASON_ABSENT
    parts = clamp_middle_out(text, UPGRADE_CLOSE_REASON_MAX_CHARS)
    if parts is None:
        return text
    head, marker, tail = parts
    return head + marker + tail


# Tokenized rollback-approval link, wherever it hides in a served string
# (decision ``rendered_body``, a model reply echoed into a trace event, a
# tool-result preview). The single-use approval TOKEN is the secret — the
# ``/approvals/{id}`` path is not — so only the ``?t=`` value is replaced
# and the surrounding text stays readable.
_APPROVAL_LINK_TOKEN_RE = re.compile(
    r"(/approvals/[A-Za-z0-9_-]+\?t=)[^\s&<>\"'()\[\]]+"
)

# Depth bound mirrors ``secret_guard._REDACT_MAX_DEPTH``: a pathological
# payload must never RecursionError inside a serve path. Beyond the bound the
# value is REPLACED (fail-closed — this walker exists to remove secrets).
_APPROVAL_REDACT_MAX_DEPTH = 64


def redact_approval_tokens_deep(payload: object, _depth: int = 0) -> object:
    """Recursively replace rollback-approval ``?t=`` token values in every
    string of a JSON-able payload.

    Surviving callers after the 2026-07-09 operator-seat reversal (the anonymous
    /decisions and /trace serve-time scrubs were removed): the ``/runs`` +
    model-facing decisions-history scrub (via :func:`scrub_decision_approval`),
    the cross-crew ``read_conversations`` untrusted-text redaction, and the
    Cloud Logging final-response log-preview redactor.

    Conventions mirror :func:`scrub_decision_rationale`: returns the input
    BY IDENTITY when nothing matches — some callers apply this per-request to
    payloads that also live in a server-side cache, so the walker must never
    mutate and never hand back a changed object unnecessarily. Never raises;
    non-container scalars pass through.
    """
    if _depth > _APPROVAL_REDACT_MAX_DEPTH:
        return "<redacted:depth>"
    if isinstance(payload, str):
        scrubbed = _APPROVAL_LINK_TOKEN_RE.sub(r"\1<redacted>", payload)
        return payload if scrubbed == payload else scrubbed
    if isinstance(payload, dict):
        out = {k: redact_approval_tokens_deep(v, _depth + 1) for k, v in payload.items()}
        return payload if all(out[k] is payload[k] for k in payload) else out
    if isinstance(payload, list):
        out = [redact_approval_tokens_deep(v, _depth + 1) for v in payload]
        return payload if all(a is b for a, b in zip(out, payload)) else out
    return payload


def scrub_decision_approval(decision: object) -> object:
    """Strip the tokenized rollback approval link from a decision doc.

    Rollback decisions persist ``approval.approval_url`` carrying the live
    single-use ``?t=`` token, and ``rendered_body`` embeds a URL carrying the
    SAME token. The two are no longer the same string: since ds-hdt the
    autonomous lane's ``approval_url`` is host-less (``/approvals/{id}?t=…``)
    while ``rendered_body`` keeps the worker's absolute form, because that body
    is what goes to the webhook. Both must be scrubbed — the token is the
    credential, and it is present in each.

    SURVIVING SCOPE (after the 2026-07-09 operator-seat decision, docs/plans/
    2026-07-09-operator-seat-demo-window.md): the anonymous demo-window scrubs of
    ``GET /decisions`` and ``/trace`` were REMOVED — a visitor holds the operator
    seat, so those reads now carry the live link, same as the operator. Two
    callers remain:

    * the unauthenticated ``GET /runs/{id}`` — always scrubbed (enumerable id,
      no auth, nothing in the UI consumes it), and
    * the model-facing decisions-history read tool (``agent/adk_tools.py``) —
      keeping ≤15-min-dead history links out of model context costs nothing.

    The ``approval_url`` KEY is dropped (not token-redacted in place) so the SPA
    rail renders no dead CTA (``approveHref`` null-checks it);
    ``approval_id``/``expires_at`` stay — they are not secret. Every other string
    in the doc goes through :func:`redact_approval_tokens_deep` (rendered_body,
    anything echoed).

    Conventions mirror :func:`scrub_decision_rationale`: identity on
    no-change, copy-on-change, never mutates the input, never raises,
    non-dict passthrough.
    """
    if not isinstance(decision, dict):
        return decision
    out = redact_approval_tokens_deep(decision)
    approval = out.get("approval") if isinstance(out, dict) else None
    if isinstance(approval, dict) and "approval_url" in approval:
        out = {
            **out,
            "approval": {k: v for k, v in approval.items() if k != "approval_url"},
        }
    return out


def scrub_pr_body(body: object) -> object:
    """Serve-time scrub for an iac PR body before it is cached/served in the
    open-trace "what this change did" disclosure (2026-06-27 follow-up).

    The body is AGENT-authored markdown (rendered from a template, not user
    free-text — see ``render_iac_pr_body``/``render_docs_pr_body``), so the
    secret risk is low. This is belt-and-braces, NOT robust arbitrary-secret
    redaction: it strips credentialed-URL userinfo (:func:`redact_text`) and any
    rollback approval ``?t=`` token (:func:`redact_approval_tokens_deep`). The
    real containment is that the body is template-authored, the endpoint is
    token-gated, and the SPA renders it as escaped ``<pre>`` (no XSS).

    Conventions mirror :func:`scrub_decision_rationale`: None / non-str / empty
    pass through unchanged; never raises. Scrub happens BEFORE the cache write,
    so the stored doc never holds an un-scrubbed body."""
    if not isinstance(body, str) or not body:
        return body
    out = redact_text(body)  # credentialed-URL userinfo → <redacted>@
    return redact_approval_tokens_deep(out)  # rollback ?t= token → <redacted>


def attach_iac_pr_link(decision: object, repo: str) -> object:
    """Serve-time: for an ``iac_apply`` decision, attach a ``github.url`` pointing
    at the GitHub PR, derived from the TRUSTED config ``repo`` + the persisted
    ``pr_number``. Lets the operator rail link a row to its PR.

    The URL is fully derivable, so it is NEVER persisted — attaching it at serve
    time (GET /decisions) covers every row, including pre-existing docs, with no
    Firestore migration and no staleness risk. Reuses the same ``github.url`` shape
    that drift_issue/docs_pr rows carry (the frontend re-validates the host via
    ``safeGithubHref``).

    Conventions mirror :func:`scrub_decision_rationale`: returns the input unchanged
    BY IDENTITY when there is nothing to do (non-dict, non-iac_apply, a ``github``
    field already present, an invalid ``pr_number`` or ``repo``), else a shallow
    copy with the new ``github``. Never mutates the input (``list_decisions`` hands
    back live dicts), never raises.
    """
    if not isinstance(decision, dict):
        return decision
    if decision.get("action") != "iac_apply" or "github" in decision:
        return decision
    pr_number = decision.get("pr_number")
    # ``type(...) is int`` excludes bool (type(True) is bool) so True can't pass as 1.
    if type(pr_number) is not int or pr_number <= 0:
        return decision
    if not isinstance(repo, str) or not _REPO_SHAPE.match(repo):
        return decision
    return {**decision, "github": {"url": f"https://github.com/{repo}/pull/{pr_number}"}}


def render_docs_pr_body(p: DecisionProposal) -> str:
    rationale = _scrub_secret_values_from_rationale(p.rationale, p.env_diffs)
    return f"""\
## DriftScribe — sanctioned change detected

{rationale}

### Changes

{_evidence_table(p)}

### Confidence

{p.confidence:.2f}

> Generated by DriftScribe. The change appears sanctioned per `ops-contract.yaml`.
> Please review and merge to keep documentation in sync with production.
"""


def render_drift_issue_body(p: DecisionProposal) -> str:
    rationale = _scrub_secret_values_from_rationale(p.rationale, p.env_diffs)
    return f"""\
## DriftScribe — unsanctioned production drift

{rationale}

### Drift

{_evidence_table(p)}

### Recommended action

- Investigate why production differs from the operational contract.
- If the change is intentional, update `ops-contract.yaml` (set `allow_manual_change: true` and provide an `operator_note`, or revise `value`) and re-run DriftScribe.
- If the change is **not** intentional, roll back via `gcloud run services update --update-env-vars`.

> DriftScribe will not update documentation while the contract is violated.
"""


def render_rollback_body(p: DecisionProposal, approval_url: str) -> str:
    """Render the operator-facing approval body for a ROLLBACK decision.

    Delivered by the Notifier worker (severity="approval"). The body surfaces
    the approval URL minted by the Rollback Worker's ``/propose`` response so
    the operator can click through to ``{COORDINATOR_URL}/approvals/{id}`` and
    Approve / Reject the proposed traffic shift.

    ``approval_url`` is passed in (not derived) because the renderer is a pure
    function — it has no access to Firestore or the HMAC key, and the worker
    response is the only place the URL is minted. The caller (Task 13.3) reads
    ``result["approval_url"]`` from the worker response and threads it here.

    Markdown discipline:
    - The approval URL is wrapped in ``<...>`` (markdown autolink form) so
      long URLs don't line-break in some renderers.
    - ``target_revision`` is shown inside an inline code span — Cloud Run
      revision names are alphanumeric + hyphens, so they don't break tables.
    - The rationale is scrubbed via :func:`_scrub_secret_values_from_rationale`
      so an LLM that quoted a secret value in prose doesn't leak it here.
    """
    rationale = _scrub_secret_values_from_rationale(p.rationale, p.env_diffs)
    return f"""\
## DriftScribe — rollback proposed (approval required)

{rationale}

### Rollback details

- **Service:** `payment-demo`
- **Target revision:** `{p.target_revision}`
- **Reason:** hard contract violation — see rationale above and the evidence
  table below.

### Evidence

{_evidence_table(p)}

### Operator approval required

Click to review and approve / reject the rollback:

<{approval_url}>

This approval link expires in 15 minutes. After expiry, DriftScribe must
re-propose to mint a fresh token.

> Approving this rollback will swing **100% of traffic** on `payment-demo`
> to revision `{p.target_revision}`. Rejecting leaves traffic on the current
> revision and DriftScribe will not retry automatically.
"""


def render_escalation_issue_body(p: DecisionProposal) -> str:
    rationale = _scrub_secret_values_from_rationale(p.rationale, p.env_diffs)
    return f"""\
## DriftScribe — uncertain change requires review

{rationale}

### Observed (no contract entry, no recent PR mention)

{_evidence_table(p)}

### What I don't know

I observed variables in production that are **not in the operational contract**, and I could not find a recent merged PR that mentions them by exact name. I need a human to confirm intent before I touch documentation.

### Reviewer action

- If this change was intentional: add the var(s) to `ops-contract.yaml` with the appropriate `allow_manual_change` and `operator_note`, then re-run DriftScribe.
- If this change was unauthorized: roll back the affected Cloud Run service, then re-run DriftScribe.

> Generated by DriftScribe.
"""
