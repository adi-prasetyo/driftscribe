// infra_graph.ts — types for the GET /infra/graph DTO + a PURE Mermaid composer.
//
// The coordinator returns a deliberately-shaped, redaction-safe graph DTO (see
// driftscribe_lib/infra_graph.build_graph); this module mirrors that shape and
// turns it into a Mermaid `flowchart` string CLIENT-SIDE. Keeping composition
// here (not server-side) means the server never emits Mermaid syntax, and this
// stays a small, fully unit-testable pure function.
//
// SECURITY (design §4.3): resource names flow into Mermaid node labels and are
// treated as UNTRUSTED. Every label is escaped to numeric HTML entities before
// it enters the diagram source, and InfraDiagram.svelte initializes Mermaid with
// `securityLevel: 'strict'`. Secret/sensitive groups are counts-only — the DTO
// carries no per-secret node, so no secret name can reach a label.
//
// Phase 1 is NODE-ONLY: `edges` is always [] from the server. `toMermaid` already
// renders an `edges` array generically so Phase 4 (partial topology) is a
// server-side change only.

import { plural, type MessageKey, type TranslateFn } from './i18n';

export interface InfraNode {
  /** Server-assigned, already-safe render handle (e.g. "g0n1"). */
  id: string;
  /** Human resource name — UNTRUSTED; escaped before it enters the diagram. */
  label: string;
  asset_type: string;
  /** Declared-in-IaC (green) vs drift (amber). */
  managed: boolean;
  location: string | null;
  /**
   * Server-marked: non-adoptable by identity — either DriftScribe's own
   * control-plane infrastructure (its Cloud Run services / the -tofu-state and
   * -tofu-artifacts buckets) OR a bucket a Google service auto-creates (Cloud
   * Build / App Engine / Cloud Functions / Cloud Run source-deploy staging).
   * The always-on denylist refuses any plan that would change or import it, so
   * adopt surfaces suppress the CTA. Optional + fail-safe: a stale coordinator
   * response without the field shows the button and C2 still blocks the plan.
   */
  control_plane?: boolean;
  /**
   * Pub/Sub subscription only: the topic it belongs to, joined in server-side so
   * the Adopt prefill can hand it to the Provision crew (which REQUIRES a topic
   * to adopt a subscription) instead of stalling to ask. Present ONLY on a
   * subscription node the enrichment could read; optional = stale-coordinator-safe
   * (same pattern as `control_plane`) — absent → the prefill just omits it.
   */
  topic?: string | null;
  /**
   * Cloud Run service only: the template container image, joined server-side so
   * the Adopt prefill can hand it to the Provision crew (which REQUIRES the exact
   * image to adopt a service) instead of stalling to ask. Present ONLY on a
   * NON-control-plane run node the enrichment could read (the server suppresses
   * DriftScribe's own service images); optional = stale-coordinator-safe (same
   * pattern as `topic`) — absent → the prefill just omits it.
   */
  image?: string | null;
}

export interface InfraGroup {
  asset_type: string;
  /** Friendly type label, e.g. "Cloud Run service". */
  label: string;
  count: number;
  managed: number;
  drift: number;
  /**
   * Actionable drift: unmanaged resources of an adoptable type that are NOT
   * control-plane / service-managed — i.e. the ones a real Adopt row is offered
   * for. `drift` counts EVERY unmanaged resource (incl. control-plane and
   * non-adoptable types); this is the subset the badge/sort/scope use so the
   * count matches what the rows present as adoptable. Optional — a stale
   * coordinator without the field falls back to raw drift for adoptable types
   * (over-report, never hide) and 0 for non-adoptable types.
   */
  drift_adoptable?: number;
  /** Secret/sensitive types: counts-only, `nodes` is []. */
  sensitive: boolean;
  nodes: InfraNode[];
  /** count − shown when the per-type sample was capped server-side. */
  truncated_in_group?: number;
  /**
   * Server-marked: this group's type is adoptable into IaC (single source of
   * truth: driftscribe_lib ADOPTABLE_RESOURCE_TYPES, never sensitive). Optional
   * — a stale coordinator response without the field simply renders no Adopt
   * buttons (fail-quiet, not wrong; Phase-4 design, Codex review 019eb572).
   */
  adoptable?: boolean;
  /**
   * Guided adoption order (item 10): server-assigned rank (1 = start here) and
   * plain-language hint, present ONLY on adoptable groups. Optional — a stale
   * coordinator response simply renders the unsorted list with no hints.
   */
  adopt_rank?: number;
  adopt_hint?: string;
}

export interface InfraEdge {
  from: string;
  to: string;
  kind?: string;
}

/**
 * An IaC declaration that did not match a live CAI resource (design
 * 2026-07-11-unmatched-iac-declarations). NOT a live resource: it never enters
 * InfraNode / resourceCards / coverage / adoption. Evidence to investigate, not
 * proof of deletion or rename.
 */
export interface UnmatchedDeclaration {
  /** Server-assigned render handle (e.g. "u0"). */
  id: string;
  asset_type: string;
  /** Friendly type label, e.g. "Storage bucket". */
  type_label: string;
  /** Declaration short name (last identity segment) — UNTRUSTED; text-only. */
  label: string;
  /** HCL address — UNTRUSTED; present only when the server had a non-empty one. */
  address?: string;
}

export interface UnmatchedDeclarations {
  /** Total eligible non-sensitive declarations before the server-side cap. */
  count: number;
  /** Capped, sorted entries (≤ server cap). */
  entries: UnmatchedDeclaration[];
  /** max(0, count − entries.length) — "+N more not shown". */
  truncated: number;
}

export interface InfraGraph {
  generated_at: string | null;
  project: string | null;
  caveat: string;
  iac_snapshot_sha?: string | null;
  degraded: boolean;
  degraded_reason: string | null;
  detail?: string | null;
  totals: { resources: number; managed: number; drift: number };
  groups: InfraGroup[];
  edges: InfraEdge[];
  truncated?: Record<string, unknown>;
  declared_set_status?: string;
  /**
   * IaC declarations not found in the latest CAI snapshot (optional, present
   * only when non-empty). SEPARATE from `groups[*].nodes`: never counted in
   * totals/coverage/adoption, never rendered as a resource card row. A stale
   * coordinator simply omits the field.
   */
  unmatched_declarations?: UnmatchedDeclarations;
}

// ---------------------------------------------------------------------------
// Plan-overlay (ghost-node) DTO — mirrors driftscribe_lib.infra_graph.plan_overlay.
// The /infra/graph/preview route returns this for a pending IaC PR; the client
// composes dashed "ghost" nodes onto the live map in toMermaid (Decision 5).
// ---------------------------------------------------------------------------

/** A planned-change verb (matches PlanSummary's vocabulary). */
export type OverlayVerb =
  | 'create'
  | 'update'
  | 'destroy'
  | 'replace'
  | 'import'
  | 'forget'
  | 'change';

export interface OverlayEntry {
  verb: OverlayVerb;
  rtype: string;
  type_label: string;
  /** Real GCP resource name — UNTRUSTED; blank when sensitive or unextractable. */
  name: string;
  /** HCL address — UNTRUSTED; blank when sensitive. Client fallback when name is "". */
  address: string;
  /** Matching CAI asset type, or null → fallback "Planned changes" subgraph. */
  asset_type: string | null;
  /** Secret-material: render a name-free ghost. */
  sensitive: boolean;
  location: string | null;
}

export interface OverlayCounts {
  create: number;
  update: number;
  destroy: number;
  replace: number;
  import: number;
  forget: number;
  change: number;
}

export interface PlanOverlay {
  pr_number: number;
  available: boolean;
  reason: string | null;
  counts: OverlayCounts;
  hidden: number;
  entries: OverlayEntry[];
}

// classDef colors are literal hex (Mermaid can't read CSS custom props in a
// classDef). These mirror the design tokens: green = managed/ok, amber =
// drift/warn, neutral = hidden/counts-only.
const CLASS_DEFS = [
  'classDef managed fill:#ecf6ef,stroke:#1f8a4c,color:#176b3b;',
  'classDef drift fill:#fcf3dc,stroke:#9a6b00,color:#7d5700;',
  'classDef hidden fill:#efeeea,stroke:#d8d7d1,color:#6b6b66;',
].join('\n');

// Ghost classDefs (Decision 5): dashed swatches mirroring the green/amber/red
// design tokens as literal hex (same precedent as CLASS_DEFS). Appended ONLY
// when an overlay actually contributes ghosts (entries or hidden > 0) so the
// no-overlay output stays byte-identical to today.
const GHOST_CLASS_DEFS = [
  'classDef ghostCreate fill:#ecf6ef,stroke:#1f8a4c,color:#176b3b,stroke-width:2px,stroke-dasharray:6 4;',
  'classDef ghostUpdate fill:#fcf3dc,stroke:#9a6b00,color:#7d5700,stroke-width:2px,stroke-dasharray:6 4;',
  'classDef ghostDestroy fill:#fdeef0,stroke:#c5303f,color:#9e2531,stroke-width:2px,stroke-dasharray:6 4;',
].join('\n');

// Verb → ghost class + label suffix. The suffix is a TRUSTED literal appended
// AFTER escaping the dynamic label parts (it contains no escape-relevant chars).
const VERB_CLASS: Record<OverlayVerb, 'ghostCreate' | 'ghostUpdate' | 'ghostDestroy'> = {
  create: 'ghostCreate',
  import: 'ghostCreate',
  update: 'ghostUpdate',
  change: 'ghostUpdate',
  forget: 'ghostUpdate',
  destroy: 'ghostDestroy',
  replace: 'ghostDestroy',
};

const VERB_SUFFIX: Record<OverlayVerb, MessageKey> = {
  create: 'infra.graph.verb.create',
  import: 'infra.graph.verb.import',
  update: 'infra.graph.verb.update',
  change: 'infra.graph.verb.change',
  forget: 'infra.graph.verb.forget',
  destroy: 'infra.graph.verb.destroy',
  replace: 'infra.graph.verb.replace',
};

// Verbs that reclass a label-matching live node (else add a ghost). create /
// import always ADD — a same-labeled live node would be CAI lag or coincidence,
// not the same resource, so claiming identity would lie.
const RECLASS_VERBS: ReadonlySet<OverlayVerb> = new Set([
  'update',
  'change',
  'forget',
  'destroy',
  'replace',
]);

// overlayCountsLine ordering (Decision 5): operator-calm, non-zero verbs only.
const COUNTS_ORDER: Array<{ key: keyof OverlayCounts; msgKey: MessageKey }> = [
  { key: 'create', msgKey: 'infra.graph.overlay.create' },
  { key: 'update', msgKey: 'infra.graph.overlay.update' },
  { key: 'replace', msgKey: 'infra.graph.overlay.replace' },
  { key: 'destroy', msgKey: 'infra.graph.overlay.destroy' },
  { key: 'import', msgKey: 'infra.graph.overlay.import' },
  { key: 'forget', msgKey: 'infra.graph.overlay.forget' },
  { key: 'change', msgKey: 'infra.graph.overlay.change' },
];

// Cap a label so one pathological name can't blow out the diagram. Truncate the
// RAW string (before escaping) so we never cut inside a multi-char entity.
const MAX_LABEL = 60;

// Single-pass escape to Mermaid ENTITY CODES (`#NN;` — Mermaid's own escape
// syntax, NOT HTML `&#NN;`). The security goal is to strip every char that could
// break out of a node-label/subgraph-title/edge-label context; Mermaid decodes
// the codes back to the original glyph at render. Numeric (not named) codes avoid
// ambiguity, and a single regex+lookup pass means replacement text is never
// re-scanned — so escaping `#` itself can't double-encode or create a bypass.
const ENTITY: Record<string, string> = {
  '&': '#38;',
  '"': '#34;',
  "'": '#39;',
  '<': '#60;',
  '>': '#62;',
  '#': '#35;',
  '[': '#91;',
  ']': '#93;',
  '{': '#123;',
  '}': '#125;',
  '(': '#40;',
  ')': '#41;',
  '|': '#124;',
  '`': '#96;',
  '\\': '#92;',
};

export function escapeMermaidLabel(raw: string): string {
  const clamped = raw.length > MAX_LABEL ? raw.slice(0, MAX_LABEL) + '…' : raw;
  return clamped
    .replace(/[\r\n\t]+/g, ' ')
    .replace(/[&"'<>#[\]{}()|`\\]/g, (c) => ENTITY[c] ?? c)
    .trim();
}

function pluralize(word: string, n: number): string {
  return n === 1 ? word : `${word}s`;
}

/** True when the graph has at least one drawable node (real or counts-only). */
export function hasRenderableNodes(graph: InfraGraph): boolean {
  if (graph.degraded) return false;
  return graph.groups.some((g) => g.nodes.length > 0 || g.count > 0);
}

/** Last `/`-segment of a name (handles full-path provider names like SA emails). */
function shortName(name: string): string {
  if (!name) return '';
  const idx = name.lastIndexOf('/');
  return idx === -1 ? name : name.slice(idx + 1);
}

/** An open infra PR awaiting operator approval (GET /infra/pending-approvals).
 *  asset_type/resource_name are blank for a freehand (non-adoption) infra PR:
 *  those appear in the panel band only, never joined to a resource card. */
export interface PendingApproval {
  pr_number: number;
  title: string;
  url: string;
  asset_type: string;
  resource_name: string;
}

/** PR number of an open adoption PR matching this resource row, or null.
 *  Joins on (asset_type, short name) — shortName() normalizes both sides so a
 *  full-path node label still matches the bare resource_name. Guards against the
 *  resource-less band-only entries (blank asset_type/name never match a row). */
export function findPendingPr(
  approvals: PendingApproval[] | null | undefined,
  assetType: string,
  name: string,
): number | null {
  if (!approvals || !assetType || !name) return null;
  const target = shortName(name);
  for (const a of approvals) {
    if (
      a.asset_type &&
      a.resource_name &&
      a.asset_type === assetType &&
      shortName(a.resource_name) === target
    ) {
      return a.pr_number;
    }
  }
  return null;
}

/**
 * True when an overlay actually contributes something to draw. A degraded/empty
 * live graph with a renderable overlay still produces a planned-changes-only
 * diagram (a CAI outage must not blind the preview).
 */
export function overlayRenderable(overlay: PlanOverlay | null | undefined): boolean {
  return !!overlay && overlay.available && (overlay.entries.length > 0 || overlay.hidden > 0);
}

/**
 * Calm operator phrasing of the change counts — non-zero verbs only, joined
 * in a fixed order by the localized separator. No singular/plural inflection
 * (the numbers read fine). An all-zero overlay → "No infrastructure changes".
 */
export function overlayCountsLine(counts: OverlayCounts, t: TranslateFn): string {
  const parts: string[] = [];
  for (const { key, msgKey } of COUNTS_ORDER) {
    const n = counts[key];
    if (n > 0) parts.push(t(msgKey, { n }));
  }
  return parts.length > 0 ? parts.join(t('infra.graph.overlay.sep')) : t('infra.graph.overlay.noChanges');
}

/**
 * Parse `preview_pr` from a `location.search` string. Returns the positive
 * integer value, or null for absent / zero / negative / non-integer / junk.
 * The raw value must be all digits (so "1.5", "-3", "abc" reject; "00012" → 12).
 */
export function previewPrFromSearch(search: string): number | null {
  const raw = new URLSearchParams(search).get('preview_pr');
  if (raw === null || !/^\d+$/.test(raw)) return null;
  const n = Number(raw);
  return Number.isSafeInteger(n) && n > 0 ? n : null;
}

// ---------------------------------------------------------------------------
// Adopt affordance (Phase 4 — adopt button UI). The map's Mermaid SVG is rendered
// with securityLevel:'strict' / htmlLabels:false, so the Adopt action CANNOT be an
// in-SVG click target; instead InfraDiagram renders an "Unmanaged resources" action
// list in normal DOM, derived from the graph DTO by these PURE helpers (keeps the
// component thin + unit-testable). Design: docs/plans/2026-06-11-adopt-button-ui.md
// §1 + §2.4; Codex review 019eb572.
// ---------------------------------------------------------------------------

export interface AdoptRow {
  nodeId: string;
  groupLabel: string;
  nodeLabel: string;
  adoptable: boolean;
  /** IaC control-plane infrastructure — denylist-refused, so never adoptable. */
  controlPlane: boolean;
  /** Chat prefill — composed ONLY for adoptable rows, else ''. */
  prefill: string;
}

/**
 * Normalize an untrusted fragment for inclusion in the agent prompt (Codex
 * review 019eb572 must-fix 2): strip C0/C1 control chars (incl. CR/LF/tab —
 * collapse each run to a single space), collapse whitespace runs, trim, and cap
 * length. The node-label cap (254) is the adopt_recipe name validator's own max,
 * so a VALID adopt name is never truncated; location/group-label cap at 40. This
 * is NOT an HTML escape — the only sinks are a text input + a JSON prompt field
 * (Svelte text interpolation), so backticks/quotes/angle-brackets pass through.
 */
export function normalizeForPrompt(raw: string, max: number): string {
  return raw
    // C0 (incl. CR/LF/tab/NUL) + DEL + C1 control chars -> space; runs collapse below.
    .replace(/[\u0000-\u001F\u007F-\u009F]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, max);
}

// Pub/Sub topics and subscriptions are global — render_adoption FORBIDS a
// `location` for them (adopt_recipe `_enforce_forbidden`), so a prefill that said
// "in global" would invite the crew to pass a location and eat a rejected/retry
// loop. `prefillLocation` returns null for those two types so the prefill omits
// the location clause; every other type's location passes through unchanged.
const PUBSUB_LOCATIONLESS: ReadonlySet<string> = new Set([
  'pubsub.googleapis.com/Topic',
  'pubsub.googleapis.com/Subscription',
]);

/** Location to render in an adopt prefill for `assetType` (null = omit it). */
export function prefillLocation(assetType: string, location: string | null): string | null {
  return PUBSUB_LOCATIONLESS.has(assetType) ? null : location;
}

/**
 * "Adopt the Storage bucket `name` in asia-northeast1 into IaC management."
 * For a Pub/Sub subscription the caller passes the node's `topic`, appending
 * " Its topic is `<topic>`."; for a Cloud Run service it passes the node's
 * `image`, appending " Its image is `<image>`." — each so the Provision crew can
 * adopt without stalling to ask (the tool REQUIRES the topic / the image). Both
 * are UNTRUSTED server data, runtime-guarded and normalized like every other
 * fragment (image cap 512 — `_IMAGE_RE` allows refs up to 512 chars, so a lower
 * cap could truncate a valid ref into a rejected one). A subscription and a run
 * service never co-occur in one node, but the clause order is deterministic.
 */
export function adoptPrefill(
  groupLabel: string,
  nodeLabel: string,
  location: string | null,
  topic: string | null = null,
  image: string | null = null,
): string {
  const type = normalizeForPrompt(groupLabel, 40);
  const name = normalizeForPrompt(nodeLabel, 254);
  const loc = location ? normalizeForPrompt(location, 40) : '';
  const where = loc ? ` in ${loc}` : '';
  const topicClause =
    typeof topic === 'string' && topic
      ? ` Its topic is \`${normalizeForPrompt(topic, 254)}\`.`
      : '';
  const imageClause =
    typeof image === 'string' && image
      ? ` Its image is \`${normalizeForPrompt(image, 512)}\`.`
      : '';
  return `Adopt the ${type} \`${name}\`${where} into IaC management.${topicClause}${imageClause}`;
}

/**
 * Drift (unmanaged) nodes across NON-sensitive groups, in render order. Sensitive
 * groups are counts-only and carry no node names by design, so they yield no rows.
 * A row's `adoptable` is `g.adoptable === true` (a stale coordinator response
 * without the field → false); the prefill is composed ONLY for adoptable rows.
 */
export function adoptRows(graph: InfraGraph): AdoptRow[] {
  const rows: AdoptRow[] = [];
  for (const g of graph.groups) {
    if (g.sensitive) continue;
    const groupAdoptable = g.adoptable === true;
    for (const n of g.nodes) {
      if (n.managed) continue;
      const controlPlane = n.control_plane === true;
      const adoptable = groupAdoptable && !controlPlane;
      rows.push({
        nodeId: n.id,
        groupLabel: g.label,
        nodeLabel: n.label,
        adoptable,
        controlPlane,
        prefill: adoptable
          ? adoptPrefill(g.label, n.label, prefillLocation(g.asset_type, n.location), n.topic ?? null, n.image ?? null)
          : '',
      });
    }
  }
  return rows;
}

/**
 * Effective adoption rank of a group, or null when unranked: not adoptable,
 * field missing (stale coordinator), or junk (this is a fail-quiet client
 * boundary — only a positive safe integer counts, so a malformed rank can
 * never sort ahead of the real rank 1 and steal "Start here"; Codex 019eb608).
 * Sorting with `rank ?? Infinity` keeps unranked groups after ranked ones, in
 * their original (stable) order.
 */
export function adoptGroupRank(g: InfraGroup): number | null {
  if (g.adoptable !== true) return null;
  return typeof g.adopt_rank === 'number' &&
    Number.isSafeInteger(g.adopt_rank) &&
    g.adopt_rank > 0
    ? g.adopt_rank
    : null;
}

// ---------------------------------------------------------------------------
// Unmatched-declaration investigation prefill (design
// 2026-07-11-unmatched-iac-declarations §Task 3). Composes a read-only Provision
// draft asking the crew to investigate whether any visible same-type unmanaged
// resource might be an intended replacement for a declaration that vanished from
// CAI — WITHOUT assuming a rename or making changes. Pure + unit-testable; the
// untrusted-fragment stance (normalizeForPrompt, text-only sink) is unchanged.
// ---------------------------------------------------------------------------

/** Max same-type unmanaged candidates listed in the investigation prefill. */
const INVESTIGATE_CANDIDATE_CAP = 5;

/**
 * A read-only Provision prefill for investigating an unmatched IaC declaration.
 * Lists visible live nodes of the SAME asset_type that are unmanaged,
 * non-control-plane, and non-empty after normalization — as candidates to
 * INSPECT, never as asserted matches. Preserves graph order, de-duplicates
 * labels, caps at five and appends a bounded "(and more may exist)" when the
 * visible set is larger. Never claims the list is complete (CAI and per-type
 * sampling are both bounded / eventually consistent), and explicitly forbids
 * assuming a rename or making any change before the operator confirms intent.
 */
export function investigateUnmatchedPrefill(
  declaration: UnmatchedDeclaration,
  graph: InfraGraph,
): string {
  const type = normalizeForPrompt(declaration.type_label, 40);
  const name = normalizeForPrompt(declaration.label, 254);
  const addr = declaration.address ? normalizeForPrompt(declaration.address, 254) : '';
  const addrClause = addr ? ` (\`${addr}\`)` : '';

  const seen = new Set<string>();
  const candidates: string[] = [];
  for (const g of graph.groups) {
    if (g.asset_type !== declaration.asset_type) continue;
    for (const n of g.nodes) {
      if (n.managed || n.control_plane === true) continue;
      const label = normalizeForPrompt(n.label, 254);
      if (!label || seen.has(label)) continue;
      seen.add(label);
      candidates.push(label);
    }
  }
  const shown = candidates.slice(0, INVESTIGATE_CANDIDATE_CAP);
  const more = candidates.length > INVESTIGATE_CANDIDATE_CAP;

  const candidatesSentence =
    shown.length === 0
      ? 'No unmanaged resources of the same type are currently visible.'
      : `Visible unmanaged resources of the same type: ${shown
          .map((c) => `\`${c}\``)
          .join(', ')}${more ? ' (and more may exist)' : ''}.`;

  return (
    `Investigate why IaC declares the ${type} \`${name}\`${addrClause} but it was ` +
    `not found in the latest Cloud Asset Inventory. ${candidatesSentence} ` +
    `Determine whether any may be an intended replacement, but do not assume a ` +
    `rename, change files, or open a PR. Report the evidence and ask me to ` +
    `confirm the relationship first.`
  );
}

// ---------------------------------------------------------------------------
// Resource cards (card-grid view; design 2026-06-24-infra-resource-cards). One
// card per group: managed AND drift nodes become rows in the same card, drift
// rows carrying the Adopt affordance. This replaces the Mermaid resource map on
// the normal path (Mermaid is kept only for the PR-preview ghost overlay). PURE —
// InfraDiagram renders the returned model; the security note above (untrusted
// names → text interpolation only, never an HTML sink) applies unchanged.
// ---------------------------------------------------------------------------

export type ResourceRowStatus = 'managed' | 'drift' | 'control_plane' | 'untracked';

export interface ResourceCardRow {
  /** each-key — server-assigned, unique. */
  nodeId: string;
  /** UNTRUSTED resource name — reaches Svelte text interpolation + the chat input only. */
  label: string;
  /**
   * managed → in IaC. drift → actionable (adoptable type, non-control-plane;
   * amber + Adopt). control_plane → system-managed (denylist-refused; collapsed
   * into the card's systemManaged disclosure, NOT the inline rows). untracked →
   * unmanaged but a non-adoptable type (neutral, no amber, no Adopt).
   */
  status: ResourceRowStatus;
  /** drift AND the group is adoptable AND the node is not control-plane. */
  adoptable: boolean;
  /** Chat prefill — composed ONLY for adoptable rows, else ''. */
  prefill: string;
}

export interface ResourceCard {
  /** each-key — UNIQUE (the friendly label is not: two "Project" asset types share one). */
  assetType: string;
  label: string;
  /** Counts-only secret card: rows === [], a "N … hidden" line stands in. */
  sensitive: boolean;
  /**
   * The group's `adoptable` flag (an adoptable resource TYPE — single source of
   * truth: ADOPTABLE_RESOURCE_TYPES). NB this is type-level: an adoptable card
   * can still hold control-plane rows whose individual Adopt CTA is suppressed.
   * Drives the scope split (see :func:`splitCards`).
   */
  adoptable: boolean;
  count: number;
  managed: number;
  drift: number;
  /**
   * Actionable drift for this card (adoptable, non-control-plane). Drives the
   * "N drift" badge, the drift-first sort tier, and the scope totals — so the
   * headline number matches the adoptable rows shown. Derived from the group's
   * `drift_adoptable` (see :func:`actionableDrift`).
   */
  actionableDrift: number;
  /** Inline rows: managed + adoptable-drift + untracked. Control-plane excluded. */
  rows: ResourceCardRow[];
  /**
   * Control-plane / service-managed rows (sampled), folded into a collapsed
   * per-card disclosure so DriftScribe's own ~10 Cloud Run services (and the
   * -tofu-state / service-created buckets) don't bury the 1–2 actionable rows.
   * Never adoptable; keeps the resource TYPE coherent in one card rather than
   * splitting it into the "Other resources" section (design 2026-07-03).
   */
  systemManaged: ResourceCardRow[];
  /**
   * TRUE control-plane count for the disclosure summary + "+N more" trailer —
   * ≥ systemManaged.length. For an adoptable card it is inferred from the group
   * totals (raw drift − actionableDrift = the non-adoptable unmanaged remainder),
   * so node sampling can't under-count it; 0 when there are none.
   */
  systemManagedTotal: number;
  /**
   * For an adoptable card: max(0, actionableDrift − actionable rows shown) — the
   * "+N more unmanaged not shown" trailer. Control-plane / non-adoptable rows
   * never enter this figure (0 for non-adoptable cards).
   */
  hiddenUnmanaged: number;
  /** Guided-order rank (1 = start here), or null when unranked. */
  rank: number | null;
}

/**
 * Actionable drift for a group: the count of unmanaged, non-control-plane
 * resources of an adoptable type. Uses the server's `drift_adoptable` when
 * present; otherwise fails safe — an adoptable type falls back to raw drift
 * (over-report, never hide an unmanaged resource), a non-adoptable type to 0
 * (it can never be adopted). Pure + total.
 */
function actionableDrift(g: InfraGroup): number {
  if (typeof g.drift_adoptable === 'number' && Number.isFinite(g.drift_adoptable)) {
    return Math.max(0, g.drift_adoptable);
  }
  return g.adoptable === true && Number.isFinite(g.drift) ? Math.max(0, g.drift) : 0;
}

// Sort tier: cards with ACTIONABLE drift first (rank-ordered within), then
// neutral non-sensitive cards (in-sync, control-plane-only, or non-adoptable),
// then counts-only sensitive last. Tier 0 is keyed on actionable drift — not raw
// drift — so a card whose only unmanaged resources are control-plane or a
// non-adoptable type does not jump above genuinely in-sync cards.
function cardTier(card: ResourceCard): number {
  if (card.sensitive) return 2;
  return card.actionableDrift > 0 ? 0 : 1;
}

/**
 * One card per group, derived from the /infra/graph DTO. Managed and drift nodes
 * become rows in the same card; sensitive groups become counts-only cards. Cards
 * are sorted drift-first then by adopt rank (stable for ties → server order for
 * unranked). A degraded graph yields []. Every group the backend reports with at
 * least one resource (count > 0) yields a card — matching hasRenderableNodes, so
 * a type with resources never collapses to the "No resources indexed yet" note;
 * a count === 0 group (pathological) is dropped (5-lens review w4jj7t4a5).
 *
 * `hiddenUnmanaged` counts only the hidden ACTIONABLE-drift delta
 * (actionableDrift − actionable rows shown). Managed and control-plane rows
 * never enter that subtraction, so the "+N more unmanaged" trailer never
 * over-promises adoptable work that isn't there (parity with the old adopt
 * trailer; Codex review 019eb572 round-2 invariant).
 */
export function resourceCards(graph: InfraGraph): ResourceCard[] {
  if (graph.degraded) return [];
  const cards: ResourceCard[] = [];
  for (const g of graph.groups) {
    if (g.count === 0) continue; // a type the backend reported with no resources
    if (g.sensitive) {
      cards.push({
        assetType: g.asset_type,
        label: g.label,
        sensitive: true,
        adoptable: false,
        count: g.count,
        managed: g.managed,
        drift: g.drift,
        actionableDrift: 0,
        rows: [],
        systemManaged: [],
        systemManagedTotal: 0,
        hiddenUnmanaged: 0,
        rank: null,
      });
      continue;
    }
    const groupAdoptable = g.adoptable === true;
    const rows: ResourceCardRow[] = [];
    const systemManaged: ResourceCardRow[] = [];
    let actionableShown = 0;
    for (const n of g.nodes) {
      if (n.managed) {
        rows.push({ nodeId: n.id, label: n.label, status: 'managed', adoptable: false, prefill: '' });
        continue;
      }
      if (n.control_plane === true) {
        // Collapsed into the card's systemManaged disclosure, not the inline rows.
        systemManaged.push({ nodeId: n.id, label: n.label, status: 'control_plane', adoptable: false, prefill: '' });
        continue;
      }
      if (groupAdoptable) {
        actionableShown += 1;
        rows.push({
          nodeId: n.id,
          label: n.label,
          status: 'drift',
          adoptable: true,
          prefill: adoptPrefill(g.label, n.label, prefillLocation(g.asset_type, n.location), n.topic ?? null, n.image ?? null),
        });
        continue;
      }
      // Unmanaged, but a non-adoptable type: neutral, never amber, never Adopt.
      rows.push({ nodeId: n.id, label: n.label, status: 'untracked', adoptable: false, prefill: '' });
    }
    const cardActionableDrift = actionableDrift(g);
    // Only adoptable cards carry a "+N more unmanaged" trailer, and it counts
    // only the actionable drift not yet shown as a row.
    const hiddenUnmanaged = groupAdoptable
      ? Math.max(0, cardActionableDrift - actionableShown)
      : 0;
    // True control-plane count for the disclosure summary. For an adoptable card
    // the group-level figure (raw unmanaged − actionable unmanaged) is the exact
    // non-adoptable remainder and survives node sampling; guard g.drift against
    // non-finite JSON. Non-adoptable cards have no derivable figure, so the sample
    // stands. max() keeps it ≥ the rows we actually hold.
    const rawDrift = Number.isFinite(g.drift) ? g.drift : 0;
    const systemManagedTotal = Math.max(
      systemManaged.length,
      groupAdoptable ? Math.max(0, rawDrift - cardActionableDrift) : 0,
    );
    cards.push({
      assetType: g.asset_type,
      label: g.label,
      sensitive: false,
      adoptable: groupAdoptable,
      count: g.count,
      managed: g.managed,
      drift: g.drift,
      actionableDrift: cardActionableDrift,
      rows,
      systemManaged,
      systemManagedTotal,
      hiddenUnmanaged,
      rank: adoptGroupRank(g),
    });
  }
  // Sort by tier, then by adopt_rank WITHIN the drift tier only. JS sort is
  // stable, so unranked drift cards — and every in-sync / counts-only card — keep
  // their server order. Rank is deliberately ignored outside tier 0: the backend
  // can emit adopt_rank on an adoptable type whose drift is currently 0, and an
  // in-sync card must not jump its neighbours by adoption guide (Codex 019ef9e9).
  cards.sort((a, b) => {
    const ta = cardTier(a);
    const tb = cardTier(b);
    if (ta !== tb) return ta - tb;
    if (ta !== 0) return 0;
    return (a.rank ?? Number.POSITIVE_INFINITY) - (b.rank ?? Number.POSITIVE_INFINITY);
  });
  return cards;
}

/**
 * The asset_type of the first card (in sorted order) that is ranked AND still has
 * a clickable Adopt row → the "Start here" chip target. A ranked card whose every
 * drift row is control-plane (denylist-refused) cannot claim it. null when the
 * server sent no ranks (stale coordinator).
 */
export function startHereAssetType(cards: ResourceCard[]): string | null {
  return cards.find((c) => c.rank != null && c.rows.some((r) => r.adoptable))?.assetType ?? null;
}

/**
 * A card is PRIMARY (in DriftScribe's scope, shown by default) iff its type is
 * adoptable OR it holds at least one managed resource. The `managed > 0` arm is
 * a defensive invariant: a managed resource is NEVER folded out of the default
 * view, even if a future `.tf` declares a type DriftScribe can't yet adopt.
 * Everything else (incl. sensitive secrets, which are never adopted) is OTHER —
 * folded behind the "Other resources" disclosure (design 2026-06-25 scope-split).
 */
function isPrimaryCard(card: ResourceCard): boolean {
  return card.adoptable || card.managed > 0;
}

export interface CardSplit {
  primary: ResourceCard[];
  other: ResourceCard[];
}

/**
 * Partition the cards into the default-shown PRIMARY set and the folded-away
 * OTHER set, preserving the `resourceCards` sort order within each (so the
 * drift-first + adopt-rank ordering still drives the primary grid and
 * `startHereAssetType` is unaffected). Pure — degraded → both empty.
 */
export function splitCards(cards: ResourceCard[]): CardSplit {
  const primary: ResourceCard[] = [];
  const other: ResourceCard[] = [];
  for (const card of cards) {
    (isPrimaryCard(card) ? primary : other).push(card);
  }
  return { primary, other };
}

export interface ScopeTotals {
  /**
   * Coverage-meter denominator: Σ (managed + actionableDrift) over PRIMARY cards
   * — the migratable in-scope resources, NOT raw Σ count. Control-plane /
   * service-managed and unmanaged non-adoptable rows are excluded so that
   * `managed + drift === resources` (the header reconciles) and 100% is reachable.
   */
  resources: number;
  /** Σ managed over PRIMARY cards. */
  managed: number;
  /** Σ drift over PRIMARY cards — matches the per-card drift badges. */
  drift: number;
  /** Authoritative project-wide indexed total (graph.totals.resources, Codex MF1). */
  totalResources: number;
  /** max(0, totalResources − resources) — resources outside DriftScribe's scope. */
  outOfScope: number;
  /** Σ count over OTHER cards — what the disclosure actually lists. */
  otherResources: number;
  /** Number of OTHER cards (resource types DriftScribe doesn't manage). */
  otherTypes: number;
}

/**
 * Scope-aware totals for the panel headline (design 2026-06-25 scope-split Q2):
 * coverage is computed over the PRIMARY (in-scope) cards, while the project-wide
 * `totalResources` comes from the authoritative backend total — NOT the card
 * sums — so a server-side truncation can't make the muted "indexed total"
 * under-report (Codex plan-review MF1). `outOfScope` is the honest "not in
 * DriftScribe's managed scope" figure (total − scope, clamped ≥ 0);
 * `otherResources` is the sum the disclosure itself lists. Pure.
 */
export function scopeTotals(cards: ResourceCard[], totalResources: number): ScopeTotals {
  const { primary, other } = splitCards(cards);
  const sum = (cs: ResourceCard[], pick: (c: ResourceCard) => number): number =>
    cs.reduce((acc, c) => acc + pick(c), 0);
  // Denominator = managed + ACTIONABLE drift per primary card (NOT raw
  // card.count). The meter's numerator is `managed` and its "N not yet in IaC"
  // is `drift` (= actionableDrift), so managed + drift MUST equal resources or
  // the header ("9 of 29 managed · 7 not yet in IaC") stops adding up. Raw count
  // also folds in control-plane / service-managed rows (DriftScribe's own Cloud
  // Run services, the -tofu-state bucket) and unmanaged non-adoptable rows —
  // none of them migratable, so counting them left 13 resources invisible in the
  // breakdown AND made 100% unreachable. Excluding them restores the invariant
  // and lets adopting the last drift resource reach 100% (coverage.ts intent).
  const managed = sum(primary, (c) => c.managed);
  const resources = sum(primary, (c) => c.managed + c.actionableDrift);
  const total = Number.isFinite(totalResources) ? Math.max(0, totalResources) : 0;
  return {
    resources,
    managed,
    // Σ primary.actionableDrift (not raw drift, not resources − managed):
    // matches the per-card "N drift" badges, which count only adoptable,
    // non-control-plane resources (Codex completed-work review, nice-to-have 1).
    drift: sum(primary, (c) => c.actionableDrift),
    totalResources: total,
    outOfScope: Math.max(0, total - resources),
    otherResources: sum(other, (c) => c.count),
    otherTypes: other.length,
  };
}

/**
 * Compose a Mermaid `flowchart` source string from the graph DTO. Pure — no DOM,
 * no Mermaid runtime (the component lazy-imports mermaid and feeds it this
 * string). Returns a valid (possibly node-light) diagram for any input.
 *
 * With `overlay` omitted the output is BYTE-IDENTICAL to the no-overlay era
 * (regression-pinned). When `overlay` is supplied, dashed ghost nodes for the
 * pending IaC plan are composed onto the map (Decision 5): reclass a matching
 * live node for mutate/destroy verbs, else add a ghost; create/import always
 * add; unmapped/groupless/overflow ghosts land in a "Planned changes" subgraph.
 */
/**
 * Mermaid class for a live (non-ghost) node, mirroring the card row semantics so
 * the preview map and the card grid agree on what amber means: managed → green;
 * adoptable, non-control-plane drift → amber `drift`; everything else
 * (control-plane / non-adoptable unmanaged) → neutral `hidden`. Pure.
 */
function liveNodeClass(group: InfraGroup, node: InfraNode): 'managed' | 'drift' | 'hidden' {
  if (node.managed) return 'managed';
  if (group.adoptable === true && node.control_plane !== true) return 'drift';
  return 'hidden';
}

export function toMermaid(graph: InfraGraph, overlay: PlanOverlay | undefined, t: TranslateFn): string {
  const hasGhosts = overlayRenderable(overlay);
  const lines: string[] = ['flowchart LR', CLASS_DEFS];
  if (hasGhosts) lines.push(GHOST_CLASS_DEFS);

  // Map the server's node id → the mermaid id we actually emit, so Phase-4
  // edges (server-supplied DTO ids) can resolve to the rendered nodes.
  const idMap = new Map<string, string>();
  let counter = 0;
  let drew = false;

  // Partition overlay entries: reclass/add candidates keyed by target asset
  // type (mapped + a live group will exist), and a fallback list for unmapped
  // or groupless entries. Built once so the group loop and the trailing
  // fallback subgraph each consume their slice.
  const byAssetType = new Map<string, OverlayEntry[]>();
  const planOnly: OverlayEntry[] = [];
  if (hasGhosts && overlay) {
    const liveAssetTypes = new Set(graph.groups.map((g) => g.asset_type));
    for (const e of overlay.entries) {
      if (e.asset_type !== null && liveAssetTypes.has(e.asset_type)) {
        const arr = byAssetType.get(e.asset_type);
        if (arr) arr.push(e);
        else byAssetType.set(e.asset_type, [e]);
      } else {
        planOnly.push(e);
      }
    }
  }

  // Render one ghost node line (added, not reclassed). `inFallback` prefixes the
  // type_label so a "Planned changes" ghost is self-describing. All dynamic
  // parts are escaped, INCLUDING the localized verb suffix — catalog content is
  // no longer a hardcoded literal, so it goes through the same boundary as
  // every other label (Codex terminology review requirement).
  const ghostNodeLine = (e: OverlayEntry, inFallback: boolean): string => {
    const mid = `n${counter++}`;
    const base = e.sensitive
      ? `${e.type_label} (name hidden)`
      : shortName(e.name) || e.address;
    const escaped = escapeMermaidLabel(base);
    const suffix = escapeMermaidLabel(t(VERB_SUFFIX[e.verb]));
    const label = inFallback
      ? `${escapeMermaidLabel(e.type_label)}: ${escaped} · ${suffix}`
      : `${escaped} · ${suffix}`;
    return `${mid}["${label}"]:::${VERB_CLASS[e.verb]}`;
  };

  graph.groups.forEach((group, gi) => {
    const sgId = `sg${gi}`;
    const inner: string[] = [];
    const groupGhosts = byAssetType.get(group.asset_type) ?? [];
    // Reclass candidates (mutate/destroy verbs with a non-empty name); the rest
    // are added. Track which were consumed by a reclass so they aren't re-added.
    const reclassByLabel = new Map<string, OverlayEntry>();
    const addGhosts: OverlayEntry[] = [];
    if (!group.sensitive) {
      for (const e of groupGhosts) {
        if (RECLASS_VERBS.has(e.verb) && e.name) {
          // Keyed by both the full name and its short form so a live label that
          // equals either matches. Last-wins on duplicate names; the server
          // contract (one entry per resource address) makes collisions pathological.
          reclassByLabel.set(e.name, e);
          reclassByLabel.set(shortName(e.name), e);
        } else {
          addGhosts.push(e);
        }
      }
    } else {
      // Sensitive groups never match a real name; every ghost is added name-free.
      addGhosts.push(...groupGhosts);
    }

    if (group.sensitive || group.nodes.length === 0) {
      // Counts-only (secret) OR an empty/capped group with a known count: one
      // neutral placeholder node — never a real resource name.
      if (group.count > 0) {
        const word = group.sensitive
          ? pluralize(group.label.toLowerCase(), group.count)
          : plural(t, 'infra.graph.resource', group.count);
        const mid = `n${counter++}`;
        inner.push(
          `${mid}["${escapeMermaidLabel(`${group.count} ${word} · ${t('infra.graph.hidden')}`)}"]:::hidden`,
        );
        drew = true;
      }
      // A reclass can't land on a counts-only/empty group (no nodes to match);
      // those entries degrade to added ghosts alongside the placeholder. Set
      // dedupes (like the non-empty branch): an entry whose full name and
      // shortName differ is stored under TWO keys and must add only ONE ghost.
      for (const e of new Set(reclassByLabel.values())) addGhosts.push(e);
    } else {
      const reclassed = new Set<OverlayEntry>();
      for (const node of group.nodes) {
        const mid = `n${counter++}`;
        idMap.set(node.id, mid);
        const hit = reclassByLabel.get(node.label);
        if (hit) {
          reclassed.add(hit);
          const suffix = escapeMermaidLabel(t(VERB_SUFFIX[hit.verb]));
          inner.push(
            `${mid}["${escapeMermaidLabel(node.label)} · ${suffix}"]:::${VERB_CLASS[hit.verb]}`,
          );
        } else {
          inner.push(`${mid}["${escapeMermaidLabel(node.label)}"]:::${liveNodeClass(group, node)}`);
        }
        drew = true;
      }
      const more = group.truncated_in_group ?? 0;
      if (more > 0) {
        const mid = `n${counter++}`;
        inner.push(`${mid}["${escapeMermaidLabel(t('infra.graph.more', { n: more }))}"]:::hidden`);
        drew = true;
      }
      // Reclass entries that matched no live node degrade to added ghosts.
      for (const e of new Set(reclassByLabel.values())) {
        if (!reclassed.has(e)) addGhosts.push(e);
      }
    }

    // Added ghosts (create/import, and reclass fall-throughs) live inside this
    // group's subgraph — even when the group was otherwise counts-only/empty.
    for (const e of addGhosts) {
      inner.push(ghostNodeLine(e, false));
      drew = true;
    }

    if (inner.length > 0) {
      lines.push(`subgraph ${sgId}["${escapeMermaidLabel(group.label)}"]`);
      lines.push('direction LR');
      lines.push(...inner);
      lines.push('end');
    }
  });

  // Fallback "Planned changes" subgraph: unmapped/groupless ghosts + the
  // hidden-overflow placeholder.
  if (hasGhosts && overlay && (planOnly.length > 0 || overlay.hidden > 0)) {
    const inner: string[] = [];
    for (const e of planOnly) {
      inner.push(ghostNodeLine(e, true));
      drew = true;
    }
    if (overlay.hidden > 0) {
      const mid = `n${counter++}`;
      inner.push(
        `${mid}["${escapeMermaidLabel(t('infra.graph.morePlanned', { n: overlay.hidden }))}"]:::hidden`,
      );
      drew = true;
    }
    if (inner.length > 0) {
      lines.push(`subgraph sgplan["${escapeMermaidLabel(t('infra.graph.plannedChanges'))}"]`);
      lines.push('direction LR');
      lines.push(...inner);
      lines.push('end');
    }
  }

  // Edges (Phase 4): drawn only when both endpoints resolved to emitted nodes.
  for (const edge of graph.edges ?? []) {
    const from = idMap.get(edge.from);
    const to = idMap.get(edge.to);
    if (from && to) {
      const arrow = edge.kind
        ? `${from} -->|${escapeMermaidLabel(edge.kind)}| ${to}`
        : `${from} --> ${to}`;
      lines.push(arrow);
    }
  }

  // Always return a parseable diagram, even with nothing to draw.
  if (!drew) {
    lines.push(`empty["${escapeMermaidLabel(t('infra.graph.empty'))}"]:::hidden`);
  }

  return lines.join('\n');
}
