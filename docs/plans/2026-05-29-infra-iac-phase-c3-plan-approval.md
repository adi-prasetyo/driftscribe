# Phase C3 — plan-bound approval schema (`driftscribe_lib/approvals.py`)

**Status:** PLAN (draft for Codex review → user sign-off → implementation)
**Date:** 2026-05-29
**Depends on:** C1 (denylist, merged `ed26d7a`), C2 (plan-builder + `c2.v1` metadata, proven live `26620367059`)
**Feeds:** C4 (`tofu-apply` worker), C5 (coordinator + approval page)

---

## §0. What C3 is — and what it deliberately is NOT

C3 is the **library/schema slice** of the gated-apply machinery. It adds, *alongside* the
existing rollback-shaped `Approval`, a **typed, plan-bound approval** that cryptographically
binds one human approval to **exactly one immutable `c2.v1` plan artifact** produced by the C2
plan-builder. It is pure Python + Firestore-schema only: **no live GCP, no GCS I/O, no
`tofu` subprocess, no network** — every line is exercisable with in-memory bytes, dicts, and a
fake Firestore client (the proven `tests/unit/test_approval_store.py` harness).

**In scope (C3):**

1. **Promote the `c2.v1` schema into the lib** so both `tools/` (CLI) and `driftscribe_lib/`
   (the C3 validator + the future C4 worker container) share one canonical definition.
2. **Plan-approval schema** in `driftscribe_lib/approvals.py`: canonical signed-payload builder,
   the plan-bound HMAC, the artifact-integrity recompute primitive, `PlanApproval` record +
   `PlanApprovalStore`, and a pure `verify_plan_approval` token check.
3. Tests, CODEOWNERS, and the `iac/README.md` C3 subsection.

**Explicitly deferred (NOT C3):**

- **C4 (apply worker):** the GCS fetch-by-generation, the denylist *re-run on fetched bytes*,
  the lockfile/OpenTofu-version freshness check, and `tofu apply`. Also the
  `iac_plan_denylist`→lib promotion (the worker needs it at runtime — see §3.6).
- **C5 (coordinator):** the `/propose`-request orchestration, the approval-page render, and the
  capture of the operator identity.

This fence keeps C3 small, fully offline-testable, and a clean contract for C4/C5.

---

## §1. Reconciling the locked design (§6–7) with the C3 hardening contracts

Two authoritative sources govern C3. They agree; the second **strengthens** the first.

**Locked decisions — `docs/plans/2026-05-27-infra-iac-agent-design.md` §6–7 (do not relitigate):**

- §6.3 **Approval identity = the binary plan.** Bind `plan_sha256` (binary bytes) +
  `artifact_generation` + `head_sha`; `plan.json` is audit/policy only.
- §6.4 **Approval ownership = the apply worker, NOT the coordinator.** The worker holds the
  HMAC key and runs **both** `create` (at `/propose`, after independently verifying the
  artifact) and `claim` (at `/apply`). The coordinator requests a proposal and renders the
  page but **cannot mint a valid approval alone**. Same trust split as `workers/rollback`.
- §6.5 **Consumer (apply):** claim (single-use transactional flip) → re-fetch by pinned
  generation → recompute + re-compare digests → re-run denylist → freshness check → apply the
  **saved binary plan** (`tofu apply plan.tfplan`, no re-plan — avoids action TOCTOU, §6.6).
- §6.7 **Freshness:** a non-mutating refresh-only plan before apply; refuse on drift.
- §7 sketch: `compute_plan_token_hmac(token, approval_id, plan_sha256, artifact_generation, head_sha, key)`;
  preserve single-use + 15-min TTL + HMAC-stored-never-token + constant-time compare;
  **new schema, do not overload `target_revision`.**

**C3 hardening contracts — Codex thread `019e7174`, post-C2-green (recorded in memory):**

1. Bind the approval to the metadata **object generation**, not just contents. Since
   `metadata.json` cannot contain its own generation, C3 must sign
   `artifact_uri_metadata + generation_metadata` (supplied out-of-band from the PR comment/UI),
   and the consumer fetches exactly that generation.
2. The consumer must **fetch** `plan.tfplan` + `plan.json` by the generations inside metadata
   and **recompute + compare** `plan_sha256` / `plan_json_sha256`, rejecting on mismatch
   before signing/applying.
3. **Sign a canonical payload** — all 15 `c2.v1` fields + approval window + approver identity +
   the two metadata locators — never free-form text.
4. **Re-run the C1 denylist** on the fetched `plan.json` — treat artifacts as untrusted even
   though C2 gated them.

**Reconciliation:** §7's 3-field binding is the *floor*; contract #3 widens it to the **full
canonical payload** (a strict superset — binding more is strictly stronger and removes any
"forgot to bind a field" gap). The binary-plan-is-identity principle (§6.3) is preserved because
`plan_sha256` is inside the canonical payload. Contracts #1/#2/#4 land where the *I/O* lives:
the **recompute primitive** is a C3 pure function (§3.4); the **fetch + denylist re-run +
freshness** are C4 (§3.6). C3 ships the schema both endpoints sign/verify through.

---

## §2. Deliverable 1 — promote the `c2.v1` schema into the lib

**Why:** C3 must validate the 15 `c2.v1` fields before signing (contracts #3/#4 — treat
operator-supplied metadata as untrusted). The validators live in `tools/iac_plan_metadata.py`
today, but:

- The only existing cross-edge is **`tools/ → driftscribe_lib/`** (`tools/iac_static_gate.py:23`
  imports `driftscribe_lib.iac_hcl`); **nothing in `driftscribe_lib/` imports `tools/`**. A
  `lib → tools` import would invert the layering.
- `tools/` is **not an installed package** — `pyproject.toml [tool.setuptools] packages =
  ["agent","checker","driftscribe_lib"]`; `from tools…` resolves only from the repo root (how
  tests run), **not** from the installed/containerized dist.
- **No worker container ships `tools/`** (every `workers/*/Dockerfile` copies `driftscribe_lib/`
  only). The future C4 worker that re-validates metadata could not `import tools.*` at runtime.

This is the exact pattern already set by `driftscribe_lib/iac_hcl.py` (lib-owned shared schema
consumed by both `tools/iac_static_gate.py` and `driftscribe_lib/infra_inventory.py`).

**Change:**

- **New `driftscribe_lib/iac_plan_metadata.py`** — the schema + validators moved verbatim:
  `METADATA_SCHEMA_VERSION = "c2.v1"`, the regexes (`_HEX40/_HEX64/_REPO/_DIGITS/_SEMVER_3/_POSITIVE_DIGITS`),
  `MetadataInput`, `_check`, `build_metadata`, `serialize_metadata`. Add
  `__all__ = ["METADATA_SCHEMA_VERSION","MetadataInput","build_metadata","serialize_metadata"]`.
- **Shrink `tools/iac_plan_metadata.py`** to a thin shell: keep the module docstring, then
  `from driftscribe_lib.iac_plan_metadata import (METADATA_SCHEMA_VERSION, MetadataInput, build_metadata, serialize_metadata)  # noqa: F401`,
  and **keep the CLI tail verbatim** (`_read_env`, `_main`, `if __name__ == "__main__"`). This
  preserves `python -m tools.iac_plan_metadata` (used at `.github/workflows/iac.yml:421`)
  byte-for-byte and keeps `test_iac_plan_metadata.py` / `test_iac_plan_metadata_cli.py` green
  (names re-exported, CLI retained).

**Zero on-the-wire change:** the 15 keys, field names, and `serialize_metadata` byte-shape are
untouched, so `iac_plan_artifact_upload.py`, the workflow, and live `metadata.json` are
unaffected. Add a `tests/unit/test_iac_plan_metadata_lib.py` to lock the canonical lib location,
and a `.github/CODEOWNERS` line for the new file.

---

## §3. Deliverable 2 — the plan-approval schema (the security core)

All additive in `driftscribe_lib/approvals.py`. The existing `Approval` / `ApprovalStore` /
`compute_token_hmac` (rollback) are **not modified** — pure addition, zero rollback regression.

### 3.1 The canonical signed payload (`c3.v1`)

A versioned envelope; the 15 `c2.v1` fields nest under `metadata` so the C4 consumer can compare
the fetched metadata against the signed copy in one shot (contract #1):

```json
{
  "approval_schema_version": "c3.v1",
  "metadata":  { … the exact 15 c2.v1 fields, re-validated via the promoted lib … },
  "artifact_uri_metadata": "gs://driftscribe-hack-2026-tofu-artifacts/pr-<N>/<sha>/run-<id>-<a>/metadata.json",
  "generation_metadata": "<numeric string>",
  "approver": "<authenticated operator subject>",   // SIGNED (Decision B = D2); see §4 caveat
  "issued_at":  "<RFC3339 UTC, +00:00, no microseconds>",
  "expires_at": "<RFC3339 UTC, +00:00, no microseconds>"
}
```

```python
_RFC3339_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")  # no microseconds

def new_approval_window(*, now: dt.datetime, ttl_minutes: int = 15) -> tuple[str, str]:
    """The SINGLE place the window is computed. `now` is injected (testable, no
    hidden clock). Returns (issued_at, expires_at) as frozen-format RFC3339 UTC
    strings; create() derives the stored dt expiry by parsing expires_at back —
    so the signed window and the stored window can never diverge (Codex blocker)."""
    iso = lambda d: d.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()
    return iso(now), iso(now + dt.timedelta(minutes=ttl_minutes))

def build_plan_approval_payload(*, metadata: dict, artifact_uri_metadata: str,
                                generation_metadata: str, approver: str,
                                issued_at: str, expires_at: str) -> dict:
    # fail-closed: schema_version == "c2.v1"; keys == the 15 c2.v1 keys exactly;
    # re-validate every field by round-tripping through MetadataInput + build_metadata
    #   (single canonical source of validation truth — treat operator-supplied metadata as
    #    untrusted; this is what makes contract #3's signed payload tamper-evident);
    #   ValueError on any malformed field;
    # generation_metadata is a numeric string; artifact_uri_metadata ends "/metadata.json"
    #   under the same run dir as artifact_uri_plan/json; issued_at/expires_at match _RFC3339_UTC;
    # approver is a non-empty string.
    ...

def canonicalize_payload(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
```

Determinism mirrors `serialize_metadata` (sorted keys, pure function of input) but uses the
**compact** form for signing — the most whitespace-stable canonical bytes. The datetime format
is frozen: **UTC, `+00:00`, no microseconds** (validated by `_RFC3339_UTC`) so the signed window
is byte-reproducible. **The canonical form + the datetime format are wire-breaking once any
approval is minted live; frozen at C3 commit.**

### 3.2 The plan-bound HMAC (two layers, domain-separated)

```python
_PLAN_APPROVAL_DOMAIN = "driftscribe-plan-approval-v1"

def compute_plan_approval_hmac(token: str, approval_id: str,
                               payload_sha256: str, hmac_key: str) -> str:
    msg = f"{_PLAN_APPROVAL_DOMAIN}|{token}|{approval_id}|{payload_sha256}".encode("utf-8")
    return hmac.new(hmac_key.encode("utf-8"), msg, hashlib.sha256).hexdigest()
```

- **Layer 1 — canonical payload → `payload_sha256 = sha256(canonical).hexdigest()`.** Collapses
  the whole 19-field artifact identity (incl. window + approver) into one fixed-width value that
  slots into the rollback HMAC's "target" position. Canonical JSON has no delimiter-collision
  surface (keys quoted+sorted, values JSON-escaped) — the right tool for binding many fields,
  unlike a `|`-joined string over free-text URIs/repo names.
- **Layer 2 — `f"{DOMAIN}|{token}|{approval_id}|{payload_sha256}"`.** Mirrors
  `compute_token_hmac` (`approvals.py:106`) verbatim, gaining all three proven properties:
  Firestore-exfil resistance (raw token returned once, never stored), single-target binding
  (now the whole plan identity), and approval_id binding (no cross-approval token reuse). The
  `|` is U+007C; `token` (urlsafe b64), `approval_id` (uuid4 string — hex + hyphens), and
  `payload_sha256` (64-hex) never emit it → unambiguous. The **domain tag** guarantees a rollback token can never validate
  as a plan-approval token even under a shared key (clean namespace separation).

This binds `plan_sha256 + generation_metadata + head_sha` (§6.3/§7) *and* every other field —
a strict superset of the locked floor.

### 3.3 The record + store

```python
@dataclass
class PlanApproval:                 # Firestore doc shape, 1:1 (PlanApproval(approval_id=id, **doc))
    approval_id: str
    status: str                     # "pending" | "used" | "denied"
    token_hmac: str
    payload_canonical: str          # the EXACT signed bytes — source of truth
    payload_sha256: str             # sha256(payload_canonical) — HMAC input + audit fingerprint
    expires_at: dt.datetime         # PARSED from payload.expires_at — single source (no dual TTL)
    created_at: dt.datetime
    created_by: str                 # proposing principal (audit) — e.g. coordinator SA
    # denormalized-for-the-page (all derivable from payload_canonical):
    pr_number: int
    head_sha: str
    artifact_uri_metadata: str
    generation_metadata: str
    # terminal-transition audit (written at claim time; NOT HMAC inputs):
    used_at: dt.datetime | None = None
    used_by: str | None = None      # actor that drove /apply (C4 verifies it vs signed approver)
    denied_at: dt.datetime | None = None
    denied_by: str | None = None
    operation_name: str | None = None  # the apply operation id, written post-apply (C4)
```

`PlanApprovalStore(project, client=None)` over a **new `plan_approvals` collection** (separate
from `approvals`; different schema, lets the C4 worker hold the plan key without touching the
rollback collection). Mirrors `ApprovalStore` exactly:

- `create(*, payload: dict, hmac_key, created_by) -> tuple[PlanApproval, str]` — uuid4 id,
  `secrets.token_urlsafe(32)` returned **once**, `payload_canonical = canonicalize_payload(payload)`,
  `payload_sha256 = sha256(...)`, `token_hmac` stored (never the raw token), `status="pending"`.
  **No `ttl_minutes` param** — the expiry comes from `payload["expires_at"]` (parsed into the
  stored `expires_at` dt), so the signed window is the *only* source of truth (Codex blocker).
- `get(approval_id) -> PlanApproval | None` — plain read.
- `claim_pending(approval_id, *, used_by: str, used_at: dt.datetime) -> …` /
  `claim_denied(approval_id, *, denied_by: str, denied_at: dt.datetime) -> …` — transactional
  `pending → used/denied` via the shared `@firestore.transactional _claim`, writing the audit
  pair **atomically with the status flip**. Names kept identical to rollback for grep-ability.

`build_plan_approval_payload` is called by the **caller** (C4 `/propose`) and passed into
`create()`, so `create()` signs exactly the bytes the caller built — no double-source. The
window inside that payload was produced by `new_approval_window` (§3.1), the single clock site.

### 3.4 The artifact-integrity recompute primitive (contract #2, pure)

```python
class ArtifactIntegrityError(Exception):
    def __init__(self, artifact: str, expected_sha256: str, actual_sha256: str): ...

def verify_artifact_integrity(*, plan_tfplan_bytes: bytes, plan_json_bytes: bytes,
                              expected_plan_sha256: str, expected_plan_json_sha256: str) -> None:
    # hashlib.sha256(raw_bytes).hexdigest() == sha256sum (iac.yml:369-370) — NO normalization;
    # hmac.compare_digest on lowercase hex; raise ArtifactIntegrityError on first mismatch.
```

C4 fetches the bytes and calls this; C3 owns the comparison so it is unit-testable with literal
bytes. (The `expected_*` come from the signed `metadata` block, themselves `_HEX64`-shaped.)

### 3.5 The token verify primitive (pure)

```python
def verify_plan_approval(presented_token: str, stored: PlanApproval, hmac_key: str) -> bool:
    digest = _digest_canonical(stored.payload_canonical)   # recompute from the source of truth
    expected = compute_plan_approval_hmac(presented_token, stored.approval_id, digest, hmac_key)
    return hmac.compare_digest(expected, stored.token_hmac)

def plan_approval_is_expired(stored: PlanApproval, *, now: dt.datetime) -> bool:
    # reads expires_at from the HMAC-bound payload_canonical — NOT the denormalized
    # stored.expires_at (which a Firestore-write attacker could push to the future)
    signed_expires = _parse_rfc3339_utc(json.loads(stored.payload_canonical)["expires_at"])
    return signed_expires < now
```

`verify_plan_approval` recomputes the digest from **`payload_canonical`** (the source of
truth), so a Firestore edit of `payload_canonical`, `token_hmac`, *or* the denormalized
`payload_sha256` all fail the constant-time compare; mirrors `workers/rollback/main.py:473-484`
step 4. **The expiry decision MUST use `plan_approval_is_expired` (the signed window)** — the
denormalized `stored.expires_at` dt is NOT HMAC-bound and is display/index-only.

### 3.6 The C4 consumer contract (documented here, built in C4)

The C4 worker holds the key and runs both endpoints. Ordering follows the **locked §6.4/§6.5**
(claim-first, mirroring rollback's verify-token-then-claim), not a verify-everything-then-claim
order — so there is no TOCTOU between the gates and the single-use flip, and a post-claim
verification failure fails closed (burns the approval; operator re-proposes):

**`/propose`** (coordinator requests it with the **authenticated** operator subject — Decision B
= D2; the worker independently verifies before minting, §6.4):

```
authenticate operator subject (trusted signal, NOT coordinator-asserted text — see §4)
  → fetch metadata.json @ generation_metadata (out-of-band locator, contract #1)
  → fetch plan.tfplan @ generation_plan + plan.json @ generation_json (the metadata generations)
  → verify_artifact_integrity(...)                                              (contract #2)
  → re-run denylist on fetched plan.json: non-empty == refuse                   (contract #4)
  → (issued_at, expires_at) = new_approval_window(now); approver = operator subject
  → payload = build_plan_approval_payload(metadata, locators, approver, window)
  → PlanApprovalStore.create(payload, hmac_key, created_by) → (record, raw_token)
```

**`/apply`** (request carries `approval_id` + raw token + the **current** authenticated actor;
no artifact fields — §6.4):

**Every apply-time decision reads from `signed_payload(stored)` (the HMAC-bound dict), NEVER the
denormalized `PlanApproval` dataclass fields** (`stored.expires_at`, `generation_metadata`,
`head_sha`, `pr_number`, `payload_sha256` are display/index-only and not HMAC-bound).

```
store.get → status=="pending"
  → verify_plan_approval (HMAC compare_digest — establishes the signed bytes are trusted)
  → sp = signed_payload(stored)                       # the trusted dict; all reads below use it
  → not expired via plan_approval_is_expired(stored, now)   (SIGNED window, not stored.expires_at)
  → current actor == sp["approver"]   (D2 enforcement — the binding only has teeth here)
  → store.claim_pending(used_by=actor, used_at=now)  (single-use; burns BEFORE heavy re-checks)
  → re-fetch metadata @ sp["generation_metadata"]; rebuild payload + canonicalize;
       compare_digest vs stored payload_canonical                                (contract #1)
  → re-fetch plan.tfplan/plan.json @ sp["metadata"]["generation_*"];
       verify_artifact_integrity(... sp["metadata"]["plan_sha256"] ...)          (contract #2)
  → re-run denylist on fetched plan.json: non-empty == abort                      (contract #4)
  → freshness: refresh-only plan, refuse on drift (§6.7)
  → tofu apply plan.tfplan (§6.5/6.6) → write operation_name
```

Order rationale (Codex): `verify_plan_approval` comes BEFORE `plan_approval_is_expired` /
`approver` / claim because those read the signed window + approver, which are only trustworthy
once the HMAC over `payload_canonical` has verified.

**Deny** mirrors rollback's hardened path: token + HMAC verified **before** `claim_denied`; the
coordinator never flips Firestore state directly (the pre-11.9 availability bug).

The **denylist re-run** needs `tools.iac_plan_denylist` importable **at worker runtime** — but
containers ship `driftscribe_lib/` only. So C4 must **promote `iac_plan_denylist` into the lib**
(same thin-re-export pattern as §2). Deferred to C4 because the re-run executes only inside the
worker, on fetched bytes; C3 neither imports nor wraps the denylist (keeps the `lib → tools`
edge out of C3 entirely). The denylist public API C4 will call: `load_plan_json(text) ->
(dict|None, Violation|None)` then `evaluate(DenylistInput(plan=parsed)) -> list[Violation]`.

---

## §4. Threat model — what each binding stops

| Attack | Defense |
|---|---|
| Stale/older saved plan replayed (OpenTofu skill: encryption ≠ replay defense) | Approval binds `generation_*` + `head_sha`; consumer fetches exactly those generations + freshness check (C4). |
| Swap to a *different* plan generation after approval | `payload_sha256` (which covers `plan_sha256`, all generations, `head_sha`) changes → HMAC compare fails. |
| Approve benign plan, push malicious commit, apply | `head_sha` is inside the signed payload; the saved binary plan is what's applied (no re-plan). |
| Firestore exfiltration alone | Only `token_hmac` is stored; minting `/apply` also needs the Secret-Manager HMAC key (held by C4). |
| Cross-approval token reuse | `approval_id` is in the HMAC (rollback 11.9 property). |
| Rollback token presented as a plan-approval token | `_PLAN_APPROVAL_DOMAIN` tag separates the namespaces. |
| Tampered metadata fields / field injection | Canonical JSON (no delimiter ambiguity) + fail-closed re-validation through `build_metadata`. |
| Double-apply / replay of a used approval | Transactional single-use `pending → used` flip (assumes Firestore write-integrity — see below). |
| Tampered artifact bytes in the bucket | `verify_artifact_integrity` recompute vs signed `plan_sha256`/`plan_json_sha256` (C4). |
| Forbidden change slips through C2 | Denylist **re-run** on fetched `plan.json` at apply (C4). |
| Post-mint Firestore edit of the **signed** `approver`/window/locators (inside `payload_canonical`) | Changes the recomputed digest → HMAC mismatch (tamper-evident even before full operator-auth). |
| Post-mint edit of a **denormalized** field (`stored.expires_at`, `payload_sha256`, `pr_number`, …) | NOT HMAC-bound, but never trusted for a decision: expiry reads the signed window via `plan_approval_is_expired`; `payload_sha256` is audit-only (verify recomputes from `payload_canonical`). |

C3 supplies the *bindings + primitives*; the **fetch/denylist/freshness/apply** enforcement is C4.

**Scope boundary on the Firestore-write attacker (Codex):** the unsigned-denormalized-field
defenses above hold against a Firestore-write attacker who lacks the HMAC key. But the mutable
`status` field is *trusted state* — an attacker with Firestore write **and** a still-valid raw
token (which is never stored, so they'd have to already possess it) could flip `used → pending`
to re-spend it. That is inherent to a Firestore-backed single-use state machine and is out of
C3's cryptographic scope; the control is Firestore IAM (only the C4 worker SA writes
`plan_approvals`). It is not a new authority-gain — the raw token is the authority either way.

**Acknowledged residual gap (Codex):** signed `approver` is genuine cryptographic non-repudiation
**only if** `/apply` receives and verifies a *trusted* operator identity against the signed value.
If the coordinator merely asserts the operator subject as text and C4 authenticates only the
coordinator service, a **compromised coordinator can spend the token without a human** — the
signed approver then degrades to tamper-evident audit, not proof a specific person clicked.
Closing this fully requires a trusted operator-auth signal end-to-end (a **C5** capability:
e.g. IAP/OIDC-asserted operator identity forwarded to `/apply`). C3 commits to the **D2 wire
format now** (approver is in the signed payload from `c3.v1` day one — adding it later would be
wire-breaking) so the binding exists; its enforcement strength tracks C5's operator-auth.

---

## §5. Decisions (Codex-reviewed, thread `019e725d`)

All five resolved in the Codex plan review; folded into §3–§4 above.

**A. HMAC input form → bind `payload_sha256`** (digest of the canonical payload) as the third
HMAC component. Truest mirror of rollback's single-value target; gives a stored audit fingerprint.

**B. `approver` → D2 (sign it).** *Codex blocker:* D1 (cleartext-only) is a downgrade of
contract #3 — "do not pretend `approver` is signed security data." So C3 bakes the **D2 wire
format**: `approver` is inside the signed `c3.v1` payload from day one (adding it later is
wire-breaking). The §6.4 timing tension is resolved by a **propose-on-approve** flow: the
coordinator captures the authenticated operator subject and passes it to `/propose`, which signs
it; `/apply` then verifies the **current** actor equals the signed `approver`. The full
non-repudiation guarantee depends on a trusted operator-auth signal end-to-end (C5) — see the §4
residual-gap note. **This is the one decision with real downstream scope (it commits C5 to
operator authentication); surfaced to the user for sign-off.**

**C. `ensure_ascii=True`** (the C2 regexes constrain all fields to ASCII; lossless + portable).
Frozen with the canonical form.

**D. Scope fence → confirmed.** C3 ships only schema + pure primitives. The denylist re-run +
`iac_plan_denylist`→lib promotion + all GCS fetch are deferred to C4.

**E. Naming → keep `claim_pending`/`claim_denied`** (mirror rollback for grep-ability).

**Plus, from the Codex review (folded in):** single-source window via `new_approval_window`
(§3.1) + `create()` parsing `expires_at` (no dual TTL); claim-first ordering per §6.5 (§3.6);
audit fields `used_at/used_by/denied_at/denied_by/operation_name` (§3.3); deny is HMAC-verified
(§3.6); frozen no-microseconds RFC3339 datetime (§3.1).

---

## §6. Test matrix (`tests/unit/`, all offline)

- **`test_iac_plan_metadata_lib.py`** — import from `driftscribe_lib`, lock canonical location;
  re-run a representative slice of the existing metadata tests against the lib module.
- **`test_plan_approvals.py`** (reuse the `test_approval_store.py` fakes + `_bypass_transactional`):
  - `build_plan_approval_payload`: happy path; rejects bad `schema_version`, wrong key set, each
    malformed field, bad `generation_metadata`, bad locator URI.
  - `canonicalize_payload`: deterministic / key-order-independent / round-trip identity.
  - `compute_plan_approval_hmac`: deterministic; differs per token / approval_id / payload /
    key; **domain separation** (a rollback `compute_token_hmac` digest never equals it).
  - `verify_artifact_integrity`: pass; raises on each artifact's mismatch; lowercase-hex contract.
  - `PlanApprovalStore`: create returns `(PlanApproval, raw_token)`; **HMAC stored, raw token
    never persisted**; distinct ids/tokens; `get` hit/miss; `claim_pending` flips once + None on
    replay/missing/non-pending; `claim_denied` symmetric; denied-then-pending both refuse.
  - `verify_plan_approval`: true on valid; false on wrong token / tampered `payload_sha256` / key.
- **Regression:** existing `test_approval_store.py`, `test_iac_plan_metadata*.py`, and the
  workflow-structure tests stay green; full `pytest -k "iac or approval"` green; `ruff` clean.

---

## §7. Blast radius, invariants, non-goals

**Touched:** new `driftscribe_lib/iac_plan_metadata.py`; shrink `tools/iac_plan_metadata.py` to
re-export + CLI; add to `driftscribe_lib/approvals.py`; new `tests/unit/test_plan_approvals.py`
+ `test_iac_plan_metadata_lib.py`; `.github/CODEOWNERS`; `iac/README.md` C3 subsection.
**Untouched:** rollback `Approval`/`ApprovalStore`/`compute_token_hmac`; C1 `iac_plan_denylist.py`
(referenced read-only as the C4 contract); C2 `metadata.json` wire format (15 keys, byte-shape);
all deployed services. No live GCP. No Dockerfile change (lib already shipped to workers).

**Invariants (lock against regression):** (1) `c2.v1` 15-key wire format preserved exactly;
(2) `_PLAN_APPROVAL_DOMAIN` + canonical form frozen; (3) raw token never persisted; (4) single
canonical source of metadata validation (lib); (5) `driftscribe_lib/` imports nothing from
`tools/`; (6) rollback path byte-identical.

**Non-goals:** GCS fetch, denylist re-run, freshness, `tofu apply` (C4); approval-page +
operator-auth (C5); HMAC-key provisioning/rotation in Secret Manager (C4 operator step).

---

## §8. Implementation order

1. Promote `iac_plan_metadata` to lib + thin re-export; run metadata + CLI tests green (§2).
2. Add the payload builder + `canonicalize_payload` + validators; TDD (§3.1).
3. Add `compute_plan_approval_hmac` + domain tag + `verify_plan_approval`; TDD (§3.2/3.5).
4. Add `verify_artifact_integrity` + `ArtifactIntegrityError`; TDD (§3.4).
5. Add `PlanApproval` + `PlanApprovalStore`; TDD against the fake-Firestore harness (§3.3).
6. `.github/CODEOWNERS` + `iac/README.md` C3 subsection; document the §3.6 C4 contract.
7. Two-stage subagent review per the C1/C2 pattern (spec-compliance then code-quality);
   `ruff` cleanup; Codex completed-work review on the same thread.
