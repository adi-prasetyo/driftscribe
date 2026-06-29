import { describe, it, expect } from 'vitest';
import { findPendingPr, type PendingApproval } from '../../src/lib/infra_graph';

const APPROVALS: PendingApproval[] = [
  {
    pr_number: 168,
    title: 'Adopt topic',
    url: 'u',
    asset_type: 'pubsub.googleapis.com/Topic',
    resource_name: 'adopt-probe-topic',
  },
  // band-only entry (freehand infra PR): no resource to attach to a card
  { pr_number: 171, title: 'Alerting', url: 'u', asset_type: '', resource_name: '' },
];

describe('findPendingPr', () => {
  it('matches a card row by asset_type + name', () => {
    expect(findPendingPr(APPROVALS, 'pubsub.googleapis.com/Topic', 'adopt-probe-topic')).toBe(168);
  });

  it('returns null when nothing matches', () => {
    expect(findPendingPr(APPROVALS, 'storage.googleapis.com/Bucket', 'x')).toBeNull();
  });

  it('requires asset_type to match too, not just the name', () => {
    expect(findPendingPr(APPROVALS, 'storage.googleapis.com/Bucket', 'adopt-probe-topic')).toBeNull();
  });

  it('never matches a resource-less (band-only) entry', () => {
    expect(findPendingPr(APPROVALS, '', '')).toBeNull();
  });

  it('is blank-name safe', () => {
    expect(findPendingPr(APPROVALS, 'pubsub.googleapis.com/Topic', '')).toBeNull();
  });

  it('normalizes full-path names via shortName on both sides', () => {
    // The infra-graph node label may be a full resource path; the approval's
    // resource_name is the bare short name. shortName() normalizes both.
    expect(
      findPendingPr(APPROVALS, 'pubsub.googleapis.com/Topic', 'projects/p/topics/adopt-probe-topic'),
    ).toBe(168);
  });

  it('tolerates null/undefined approvals', () => {
    expect(findPendingPr(null, 'pubsub.googleapis.com/Topic', 'adopt-probe-topic')).toBeNull();
    expect(findPendingPr(undefined, 'pubsub.googleapis.com/Topic', 'adopt-probe-topic')).toBeNull();
  });
});
