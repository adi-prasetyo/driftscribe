// coverage.ts — display percentage for the migration coverage meter.
//
// The percentage is a TRUST number for the ClickOps→IaC audience (roadmap Wave
// 1 item 2): "100%" must mean literally every resource is managed, and the
// first adopted resource must visibly move the number off zero. Hence the
// exact-endpoint + [1,99] clamp rules rather than naive rounding.

/**
 * Percentage of `resources` covered by `managed`, shaped for display:
 *  - `null` when there is nothing to measure (resources <= 0, non-finite input)
 *  - exactly 100 / 0 only when literally complete / literally zero
 *  - otherwise rounded, then clamped into [1, 99]
 */
export function coveragePercent(managed: number, resources: number): number | null {
  if (!Number.isFinite(managed) || !Number.isFinite(resources)) return null;
  if (resources <= 0) return null;
  const m = Math.min(Math.max(managed, 0), resources);
  if (m === resources) return 100;
  if (m === 0) return 0;
  return Math.min(99, Math.max(1, Math.round((m / resources) * 100)));
}
