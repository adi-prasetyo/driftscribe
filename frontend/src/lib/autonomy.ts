// Autonomy dial — wire types + parsing for GET/POST /autonomy.
// Mirrors the backend contract (agent/autonomy.py): absent-doc default and
// fail-closed semantics live SERVER-side; the client renders what it is told
// and treats anything structurally unexpected as 'unknown'.

import type { TranslateFn, MessageKey } from './i18n';

export const AUTONOMY_MODES = ['observe', 'propose', 'propose_apply'] as const;
export type AutonomyMode = (typeof AUTONOMY_MODES)[number];

// Mode label/blurb text used to live in static Records (MODE_LABELS/MODE_BLURBS),
// read directly by callers. i18n requires them to be resolved at RENDER time
// (the component tracks $t), so they are now key-lookup helpers instead —
// callers pass the reactive `$t`. See docs/i18n-glossary.md's autonomy-mode row:
// observe=監視のみ, propose=提案, propose_apply=提案＋適用.
const MODE_LABEL_KEYS: Record<AutonomyMode, MessageKey> = {
  observe: 'capability.mode.observe.label',
  propose: 'capability.mode.propose.label',
  propose_apply: 'capability.mode.proposeApply.label',
};
export function modeLabel(mode: AutonomyMode, t: TranslateFn): string {
  return t(MODE_LABEL_KEYS[mode]);
}

const MODE_BLURB_KEYS: Record<AutonomyMode, MessageKey> = {
  observe: 'capability.mode.observe.blurb',
  propose: 'capability.mode.propose.blurb',
  propose_apply: 'capability.mode.proposeApply.blurb',
};
export function modeBlurb(mode: AutonomyMode, t: TranslateFn): string {
  return t(MODE_BLURB_KEYS[mode]);
}

// Progressive-disclosure explainer copy (AutonomyControl). Names the mechanism
// operators kept missing, in two parts that mirror tour.ts CONTROLS_LINE:
// (1) Anchor is the ONLY workload that self-triggers (a watched service changes
//     → a drift check runs with no human; AUTONOMOUS_TRIGGER_WORKLOADS={"drift"});
// (2) the dial is GLOBAL — the same ceiling also bounds the rest of the crew on
//     the chat requests you make here, so it is NOT scoped to the per-message
//     workload picker in the composer. The earlier copy stated (2) abstractly
//     ("all of the agent's activity") and read as Anchor-only; this names the
//     scope concretely. Phrased to describe the designed behavior without
//     implying the agent continuously polls — the check is event-triggered,
//     not a watcher loop. Text lives in the `capability` catalog now; these
//     helpers are the reactive accessors the component calls with `$t`.
export function autonomyExplainerHeading(t: TranslateFn): string {
  return t('capability.autonomyExplainer.heading');
}
export function autonomyExplainerBody(t: TranslateFn): string {
  return t('capability.autonomyExplainer.body');
}

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
