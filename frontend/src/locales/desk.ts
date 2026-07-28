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
    // SealStamp's accessible name (Task 3.2). The glyph text stays 承認 in
    // both locales — it's a hanko, not a translated word — but a screen
    // reader needs an EN-legible name for the "approved" state it marks.
    'desk.seal.ariaLabel': 'Approved',
    // InstrumentBand (Task 3.3) — the three-number pulse across the top of
    // the desk/estate. Visible labels sit under 44px numerals so they stay
    // short; the *Aria keys are separate because a bare numeral read aloud
    // ("nine") is meaningless without what it counts, so each stat button's
    // accessible name pairs the figure with its meaning explicitly rather
    // than relying on visible-text concatenation order.
    'desk.band.managedLabel': 'Managed by IaC',
    'desk.band.driftLabel': 'Drift detected',
    'desk.band.awaitingLabel': 'Awaiting your approval',
    'desk.band.managedAria': '{n} managed by IaC',
    'desk.band.driftAria': '{n} drift detected',
    'desk.band.awaitingAria': '{n} awaiting your approval',
    // LedgerStrip (Task 3.4) — the "Recent record" strip beneath the desk
    // hero. `openTitle`/`appliedTitle` cover the two states this module
    // classifies with fixed copy; `noted` rows fall back to
    // decisionActionLabel's per-action text instead of a fixed string here.
    'desk.ledger.heading': 'Recent record',
    'desk.ledger.appliedTitle': 'You approved · applied',
    'desk.ledger.openTitle': 'Awaiting your approval',
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
    'desk.seal.ariaLabel': '承認済み',
    // Visible labels straight from the mockup (docs/plans/2026-07-28-
    // composite-mockup.html "instrument band"). Aria variants prefix the
    // count so a screen reader announces "9件、IaC管理下" rather than a bare
    // label with no number.
    'desk.band.managedLabel': 'IaC 管理下',
    'desk.band.driftLabel': 'ドリフト検出',
    'desk.band.awaitingLabel': 'あなたの承認待ち',
    'desk.band.managedAria': '{n}件、IaC 管理下',
    'desk.band.driftAria': '{n}件、ドリフト検出',
    'desk.band.awaitingAria': '{n}件、あなたの承認待ち',
    'desk.ledger.heading': '最近の記録',
    'desk.ledger.appliedTitle': 'あなたが承認 → 適用完了',
    'desk.ledger.openTitle': 'あなたの承認待ち',
  },
};
