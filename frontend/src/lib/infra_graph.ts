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

// classDef colors are literal hex (Mermaid can't read CSS custom props in a
// classDef). These mirror the design tokens: green = managed/ok, amber =
// drift/warn, neutral = hidden/counts-only.
const CLASS_DEFS = [
  'classDef managed fill:#ecf6ef,stroke:#1f8a4c,color:#176b3b;',
  'classDef drift fill:#fcf3dc,stroke:#9a6b00,color:#7d5700;',
  'classDef hidden fill:#efeeea,stroke:#d8d7d1,color:#6b6b66;',
].join('\n');

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

/**
 * Compose a Mermaid `flowchart` source string from the graph DTO. Pure — no DOM,
 * no Mermaid runtime (the component lazy-imports mermaid and feeds it this
 * string). Returns a valid (possibly node-light) diagram for any input.
 */
export function toMermaid(graph: InfraGraph): string {
  const lines: string[] = ['flowchart LR', CLASS_DEFS];

  // Map the server's node id → the mermaid id we actually emit, so Phase-4
  // edges (server-supplied DTO ids) can resolve to the rendered nodes.
  const idMap = new Map<string, string>();
  let counter = 0;
  let drew = false;

  graph.groups.forEach((group, gi) => {
    const sgId = `sg${gi}`;
    const inner: string[] = [];

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
    } else {
      for (const node of group.nodes) {
        const mid = `n${counter++}`;
        idMap.set(node.id, mid);
        const cls = node.managed ? 'managed' : 'drift';
        inner.push(`${mid}["${escapeMermaidLabel(node.label)}"]:::${cls}`);
        drew = true;
      }
      const more = group.truncated_in_group ?? 0;
      if (more > 0) {
        const mid = `n${counter++}`;
        inner.push(`${mid}["${escapeMermaidLabel(`+${more} more`)}"]:::hidden`);
        drew = true;
      }
    }

    if (inner.length > 0) {
      lines.push(`subgraph ${sgId}["${escapeMermaidLabel(group.label)}"]`);
      lines.push('direction LR');
      lines.push(...inner);
      lines.push('end');
    }
  });

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
