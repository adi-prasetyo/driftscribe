// desk namespace — the header's three-way view nav (Desk / Estate / Chat)
// added in the composite-redesign Task 2.2 (docs/plans/2026-07-28-composite-
// redesign-implementation.md). The desk/estate placeholder sections reuse
// these same nav labels for their heading text; real content lands in Phase 3
// (ApprovalDesk.svelte) and Phase 4 (EstateView.svelte) — this file only needs
// to survive the skeleton until then.
export const desk = {
  en: {
    'desk.nav.ariaLabel': 'Primary navigation',
    'desk.nav.desk': 'Desk',
    'desk.nav.estate': 'Estate',
    'desk.nav.chat': 'Chat',
  },
  ja: {
    'desk.nav.ariaLabel': 'メインナビゲーション',
    'desk.nav.desk': 'デスク',
    // The mockup called this 推定図, but 推定 reads as "estimation/inference"
    // — closer to "estimation diagram" than to "your infrastructure". Operator
    // decision 2026-07-28: use インフラ, matching the term this same domain
    // already ships under (infra.panel.title). Phase 4's EstateView keeps it.
    'desk.nav.estate': 'インフラ',
    'desk.nav.chat': 'チャット',
  },
};
