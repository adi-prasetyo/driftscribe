import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import IacApprovalCta from '../../src/components/IacApprovalCta.svelte';

// Component tests for IacApprovalCta — the amber first-authoring CTA that
// surfaces a "Review & approve" link immediately after the coordinator opens
// an infrastructure PR (before any decision row exists in the rail).

afterEach(cleanup);

const CAGE_NOTE =
  "Before anything applies, this change must pass the self-protection " +
  "denylist and get your explicit approval. The denylist blocks any " +
  "DriftScribe control-plane changes, any IAM changes, and any deletes, " +
  "replacements, or un-managing.";

describe('IacApprovalCta — denylist cage teaser note', () => {
  it('renders iac-cta-cage-note with the exact sentence when prNumber is valid', () => {
    const { getByTestId } = render(IacApprovalCta, { props: { prNumber: 88 } });
    const note = getByTestId('iac-cta-cage-note');
    expect(note.textContent?.replace(/\s+/g, ' ').trim()).toBe(CAGE_NOTE);
  });

  it('cage note is absent when prNumber is invalid (CTA hidden)', () => {
    const { queryByTestId } = render(IacApprovalCta, { props: { prNumber: 'not-a-number' } });
    expect(queryByTestId('iac-cta-cage-note')).toBeNull();
  });
});
