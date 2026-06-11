// Autonomy dial — wire types + parsing for GET/POST /autonomy.
// Mirrors the backend contract (agent/autonomy.py): absent-doc default and
// fail-closed semantics live SERVER-side; the client renders what it is told
// and treats anything structurally unexpected as 'unknown'.

export const AUTONOMY_MODES = ['observe', 'propose', 'propose_apply'] as const;
export type AutonomyMode = (typeof AUTONOMY_MODES)[number];

export const MODE_LABELS: Record<AutonomyMode, string> = {
  observe: 'Observe',
  propose: 'Propose',
  propose_apply: 'Propose + Apply',
};

export const MODE_BLURBS: Record<AutonomyMode, string> = {
  observe: 'Watch and report only — no pull requests, no issues, no applies.',
  propose: 'Open pull requests and issues for your review — applies stay off.',
  propose_apply: 'Propose changes and apply them after your approval (current default).',
};

export interface AutonomyDoc {
  mode: AutonomyMode;
  reason: string | null;
  actor: string | null;
  updated_at: string | null;
  read_error: boolean;
}

export function parseAutonomyDoc(body: unknown): AutonomyDoc | null {
  if (typeof body !== 'object' || body === null) return null;
  const b = body as Record<string, unknown>;
  if (!AUTONOMY_MODES.includes(b.mode as AutonomyMode)) return null;
  return {
    mode: b.mode as AutonomyMode,
    reason: typeof b.reason === 'string' ? b.reason : null,
    actor: typeof b.actor === 'string' ? b.actor : null,
    updated_at: typeof b.updated_at === 'string' ? b.updated_at : null,
    read_error: b.read_error === true,
  };
}
