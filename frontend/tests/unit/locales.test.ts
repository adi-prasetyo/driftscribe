import { describe, it, expect } from 'vitest';
import { NAMESPACES, enMessages, jaMessages } from '../../src/locales';

// Catalog invariants that keep the fan-out consistent: EN⇔JA key parity per
// namespace, no empty values, and globally-unique keys (a key defined in two
// namespaces would be silently overwritten by the merge in locales/index.ts).

describe('locale catalogs', () => {
  for (const [name, ns] of Object.entries(NAMESPACES)) {
    describe(`namespace: ${name}`, () => {
      const enKeys = Object.keys(ns.en).sort();
      const jaKeys = Object.keys(ns.ja).sort();

      it('has identical EN and JA key sets', () => {
        expect(jaKeys).toEqual(enKeys);
      });

      it('has no empty values', () => {
        for (const [k, v] of Object.entries(ns.en)) {
          expect(v, `en:${name}:${k}`).not.toBe('');
        }
        for (const [k, v] of Object.entries(ns.ja)) {
          expect(v, `ja:${name}:${k}`).not.toBe('');
        }
      });
    });
  }

  it('has no duplicate keys across namespaces', () => {
    const seen = new Map<string, string>();
    const dups: string[] = [];
    for (const [name, ns] of Object.entries(NAMESPACES)) {
      for (const k of Object.keys(ns.en)) {
        if (seen.has(k)) dups.push(`${k} (in ${seen.get(k)} and ${name})`);
        else seen.set(k, name);
      }
    }
    expect(dups).toEqual([]);
  });

  it('merged EN and JA catalogs agree on keys', () => {
    expect(Object.keys(jaMessages).sort()).toEqual(Object.keys(enMessages).sort());
  });
});
