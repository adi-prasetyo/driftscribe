export interface WorkloadPrompts {
  workload: string;
  display_name: string;
  descriptor: string;
  recheck_prompt: string;
  chat_prompt: string | null;
  chat_prompt_distinct: boolean;
  source_dir: string;
  revision: string;
  demo_note: string;
}

export function parseWorkloadPrompts(body: unknown): WorkloadPrompts | null {
  if (typeof body !== 'object' || body === null) return null;
  const b = body as Record<string, unknown>;
  const str = (k: string) => (typeof b[k] === 'string' ? (b[k] as string) : null);
  const workload = str('workload');
  const display_name = str('display_name');
  const recheck_prompt = str('recheck_prompt');
  if (workload === null || display_name === null || recheck_prompt === null) return null;
  if (typeof b.chat_prompt_distinct !== 'boolean') return null;
  // chat_prompt: a non-string, non-null value coerces to null (str() returns null); only distinct=true+null is rejected above.
  const chat_prompt = b.chat_prompt === null ? null : str('chat_prompt');
  if (b.chat_prompt_distinct && chat_prompt === null) return null;
  return {
    workload, display_name,
    // Cosmetic fields (descriptor/source_dir/revision/demo_note): a non-string or absent value degrades to '' rather than failing the parse — they never block rendering.
    descriptor: str('descriptor') ?? '',
    recheck_prompt,
    chat_prompt,
    chat_prompt_distinct: b.chat_prompt_distinct,
    source_dir: str('source_dir') ?? '',
    revision: str('revision') ?? '',
    demo_note: str('demo_note') ?? '',
  };
}
