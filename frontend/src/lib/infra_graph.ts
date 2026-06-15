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
}

export interface InfraGroup {
  asset_type: string;
  /** Friendly type label, e.g. "Cloud Run service". */
  label: string;
  count: number;
  managed: number;
  drift: number;
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

const VERB_SUFFIX: Record<OverlayVerb, string> = {
  create: 'will be created',
  import: 'will be imported',
  update: 'will be modified',
  change: 'will change',
  forget: 'will leave IaC management',
  destroy: 'will be destroyed',
  replace: 'will be replaced',
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
const COUNTS_ORDER: Array<{ key: keyof OverlayCounts; phrase: string }> = [
  { key: 'create', phrase: 'will be created' },
  { key: 'update', phrase: 'will be modified' },
  { key: 'replace', phrase: 'will be replaced' },
  { key: 'destroy', phrase: 'will be destroyed' },
  { key: 'import', phrase: 'will be imported' },
  { key: 'forget', phrase: 'will leave management' },
  { key: 'change', phrase: 'will change' },
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

/**
 * True when an overlay actually contributes something to draw. A degraded/empty
 * live graph with a renderable overlay still produces a planned-changes-only
 * diagram (a CAI outage must not blind the preview).
 */
export function overlayRenderable(overlay: PlanOverlay | null | undefined): boolean {
  return !!overlay && overlay.available && (overlay.entries.length > 0 || overlay.hidden > 0);
}

/**
 * Calm operator phrasing of the change counts — non-zero verbs only, ` · `-joined
 * in a fixed order. No singular/plural inflection (the numbers read fine). An
 * all-zero overlay → "No infrastructure changes".
 */
export function overlayCountsLine(counts: OverlayCounts): string {
  const parts: string[] = [];
  for (const { key, phrase } of COUNTS_ORDER) {
    const n = counts[key];
    if (n > 0) parts.push(`${n} ${phrase}`);
  }
  return parts.length > 0 ? parts.join(' · ') : 'No infrastructure changes';
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

/** "Adopt the Storage bucket `name` in asia-northeast1 into IaC management." */
export function adoptPrefill(
  groupLabel: string,
  nodeLabel: string,
  location: string | null,
): string {
  const type = normalizeForPrompt(groupLabel, 40);
  const name = normalizeForPrompt(nodeLabel, 254);
  const loc = location ? normalizeForPrompt(location, 40) : '';
  const where = loc ? ` in ${loc}` : '';
  return `Adopt the ${type} \`${name}\`${where} into IaC management.`;
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
        prefill: adoptable ? adoptPrefill(g.label, n.label, n.location) : '',
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
export function toMermaid(graph: InfraGraph, overlay?: PlanOverlay): string {
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
  // parts are escaped; the verb suffix is a trusted literal appended after.
  const ghostNodeLine = (e: OverlayEntry, inFallback: boolean): string => {
    const mid = `n${counter++}`;
    const base = e.sensitive
      ? `${e.type_label} (name hidden)`
      : shortName(e.name) || e.address;
    const escaped = escapeMermaidLabel(base);
    const label = inFallback
      ? `${escapeMermaidLabel(e.type_label)}: ${escaped} · ${VERB_SUFFIX[e.verb]}`
      : `${escaped} · ${VERB_SUFFIX[e.verb]}`;
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
          : pluralize('resource', group.count);
        const mid = `n${counter++}`;
        inner.push(`${mid}["${escapeMermaidLabel(`${group.count} ${word} · hidden`)}"]:::hidden`);
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
          inner.push(`${mid}["${escapeMermaidLabel(node.label)} · ${VERB_SUFFIX[hit.verb]}"]:::${VERB_CLASS[hit.verb]}`);
        } else {
          const cls = node.managed ? 'managed' : 'drift';
          inner.push(`${mid}["${escapeMermaidLabel(node.label)}"]:::${cls}`);
        }
        drew = true;
      }
      const more = group.truncated_in_group ?? 0;
      if (more > 0) {
        const mid = `n${counter++}`;
        inner.push(`${mid}["${escapeMermaidLabel(`+${more} more`)}"]:::hidden`);
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
      inner.push(`${mid}["${escapeMermaidLabel(`+${overlay.hidden} more planned change(s)`)}"]:::hidden`);
      drew = true;
    }
    if (inner.length > 0) {
      lines.push('subgraph sgplan["Planned changes"]');
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
    lines.push('empty["No resources indexed yet"]:::hidden');
  }

  return lines.join('\n');
}
