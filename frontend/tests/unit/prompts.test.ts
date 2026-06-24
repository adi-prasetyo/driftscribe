import { describe, it, expect } from 'vitest';
import { parseWorkloadPrompts } from '../../src/lib/prompts';

const OK = {
  workload: 'drift', display_name: 'Anchor', descriptor: 'Cloud Run config',
  recheck_prompt: 'a', chat_prompt: 'b', chat_prompt_distinct: true,
  source_dir: 'workloads/drift', revision: 'driftscribe-agent-00094-7cr',
  demo_note: 'Demo: ...',
};

describe('parseWorkloadPrompts', () => {
  it('accepts a well-formed payload', () => {
    expect(parseWorkloadPrompts(OK)?.display_name).toBe('Anchor');
  });
  it('accepts null chat_prompt when not distinct', () => {
    const p = parseWorkloadPrompts({ ...OK, chat_prompt: null, chat_prompt_distinct: false });
    expect(p?.chat_prompt).toBeNull();
    expect(p?.chat_prompt_distinct).toBe(false);
  });
  it('rejects a non-object / missing required fields', () => {
    expect(parseWorkloadPrompts(null)).toBeNull();
    expect(parseWorkloadPrompts({ workload: 'drift' })).toBeNull();
  });
  it('rejects the inconsistent distinct=true + chat_prompt=null payload', () => {
    expect(parseWorkloadPrompts({ ...OK, chat_prompt: null, chat_prompt_distinct: true })).toBeNull();
  });
  it('tolerates distinct=false + a non-null chat_prompt (renders as single-prompt)', () => {
    const p = parseWorkloadPrompts({ ...OK, chat_prompt: 'leftover', chat_prompt_distinct: false });
    expect(p).not.toBeNull();
    expect(p?.chat_prompt_distinct).toBe(false);
  });
});
