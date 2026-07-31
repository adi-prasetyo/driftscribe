// desk namespace — the header's view nav, plus everything on the landing page
// it leads to: ApprovalDesk (band, hero, ledger) and EstateView.
//
// The nav arrived three-way (Desk / Estate / Chat) in the composite-redesign
// Task 2.2 (docs/plans/2026-07-28-composite-redesign-implementation.md). The
// 2026-07-31 merge (docs/plans/2026-07-31-desk-estate-merge.md) made the estate
// a SECTION of the desk rather than a view of its own, so the nav is two-way
// and `desk.nav.estate` is gone. `desk.estate.*` keys stayed exactly where they
// were — the section is unchanged, only its address is.
export const desk = {
  en: {
    'desk.nav.ariaLabel': 'Primary navigation',
    'desk.nav.desk': 'Desk',
    'desk.nav.chat': 'Chat',
    // SealStamp's accessible name (Task 3.2). The glyph text stays 承認 in
    // both locales — it's a hanko, not a translated word — but a screen
    // reader needs an EN-legible name for the "approved" state it marks.
    'desk.seal.ariaLabel': 'Approved',
    // InstrumentBand (Task 3.3) — the three-number pulse across the top of
    // the desk. Visible labels sit under 44px numerals so they stay
    // short; the *Aria keys are separate because a bare numeral read aloud
    // ("nine") is meaningless without what it counts, so each stat button's
    // accessible name pairs the figure with its meaning explicitly rather
    // than relying on visible-text concatenation order.
    'desk.band.managedLabel': 'Managed by IaC',
    'desk.band.driftLabel': 'Drift detected',
    'desk.band.awaitingLabel': 'Awaiting your approval',
    // awaiting is the band's one inert figure (ds-s61 — the queue it counts is
    // directly below it), so it is the one stat whose name promises nothing.
    // managed/drift have no plain variant at all: they are always controls, and
    // a control's name must say where it leads (see the next block).
    'desk.band.awaitingAria': '{n} awaiting your approval',
    // ds-7ag.2 — an INTERACTIVE stat names its destination, because the
    // aria-label overrides all descendant text: the visible hover hint is
    // invisible to a screen reader, so the accessible name has to make the same
    // promise. The `Desk` suffix is vestigial: it named the CONTEXT that
    // selected this variant back when the band also rendered on a standalone
    // estate view. The 2026-07-31 merge left one page and one variant; the
    // keys keep their names because renaming them across every locale buys
    // nothing an operator can see.
    'desk.band.managedAriaDesk': '{n} managed by IaC — view infrastructure map',
    'desk.band.driftAriaDesk': '{n} drift detected — view infrastructure map',
    // The VISIBLE hover/focus hint on an interactive numeral (plan Task 3). The
    // numerals read as figures, so nothing said a click went anywhere. Keyed by
    // DESTINATION so the string's wording and its key agree. (Two siblings are
    // gone: "to the queue below" went with ds-s61's retirement of the awaiting
    // jump, and "View on desk" with the estate view itself.)
    'desk.band.statHintEstate': 'View infrastructure →',
    // Read instead of the *Aria keys above when a figure is not yet known
    // (ds-eh6). The visible numeral becomes an em dash, which a screen reader
    // announces as nothing at all, so these carry the state in words.
    'desk.band.awaitingUnknownAria': 'Awaiting your approval: not yet known',
    // Unknown AND still a control: managed/drift stay clickable while unknown
    // (the map is where you go to find out), so their accessible name carries
    // the destination as well.
    'desk.band.managedUnknownAriaDesk':
      'Managed by IaC: not yet known — view infrastructure map',
    'desk.band.driftUnknownAriaDesk': 'Drift detected: not yet known — view infrastructure map',
    // LedgerStrip (Task 3.4) — the "Recent record" strip beneath the desk
    // hero. `openTitle`/`appliedTitle` cover the two states this module
    // classifies with fixed copy; `noted` rows fall back to
    // decisionActionLabel's per-action text instead of a fixed string here.
    'desk.ledger.heading': 'Recent record',
    'desk.ledger.appliedTitle': 'You approved · applied',
    'desk.ledger.openTitle': 'Awaiting your approval',
    'desk.ledger.failedTitle': 'You approved · did not apply',
    // Deliberately not "failed": the operation may still be running or may
    // have succeeded with the response lost. Saying "failed" here would be a
    // second false claim in the opposite direction (ds-2mc).
    'desk.ledger.unconfirmedTitle': 'You approved · outcome unconfirmed',
    // One-way: the strip opens to the whole snapshot and does not re-collapse.
    // `n` is every decision the snapshot holds, not the number still hidden —
    // "Show all 12" says what you will get; "Show 8 more" makes you do the sum.
    'desk.ledger.showMore': 'Show all {n}',
    // DecisionRecord (ds-jns PR 2) — one decision opened on the desk: the
    // ledger row's accordion body, and the pinned card a bare `?reasoning=`
    // link lands on. Both lines are quiet statements of fact, not errors —
    // the reasoning above them may have loaded perfectly.
    //
    // `incomplete` is reachable without anything having gone wrong: a
    // `?reasoning=` link can legitimately name a CHAT turn's trace, which is
    // reasoning with no decision doc behind it.
    'desk.record.incomplete': 'No decision record is attached to this trace.',
    // Only ever shown once the overview has SETTLED — claiming a record is
    // older than a list that has not loaded yet would be a guess.
    'desk.record.outOfWindow': 'This decision is older than the records listed below.',
    // ---- unresolved rollback outcome (desk rule 2.5) ----
    'desk.unresolved.who': 'You approved',
    'desk.unresolved.failed.detail': 'Did not apply',
    'desk.unresolved.failed.headline': 'The rollback did not apply.',
    'desk.unresolved.failed.body':
      'The approval was used, but the traffic change did not go through. Nothing was rolled back. You can ask for a new rollback.',
    'desk.unresolved.unknown.detail': 'Outcome unconfirmed',
    'desk.unresolved.unknown.headline': "The rollback's outcome is unconfirmed.",
    'desk.unresolved.unknown.body':
      'The traffic change was accepted but took longer than we waited, so we cannot confirm it either way. Check the service in Cloud Run before acting on this.',

    // ApprovalDesk (Task 3.5) — the three-state front door composed of the
    // band above + a per-state body + the ledger strip below. Deliberately
    // NO fabricated narrative: every string below is either generic (true
    // regardless of which resource/service is involved) or interpolates a
    // REAL field the decision/approval doc actually carries (pr, time) — see
    // lib/desk.ts's own header comment on why "no fictional timestamps"
    // extends to display copy here, not just the selection logic.
    'desk.region.ariaLabel': 'Approval desk',

    // Rollback proposals are ANCHOR-only (drift/Anchor is the one crew that
    // runs autonomously and can emit a rollback — see workload_crew_rename),
    // so naming it here is a true statement, not a guess.
    'desk.pending.rollback.who': 'Anchor is proposing a fix',
    'desk.pending.rollback.headline': 'A rollback proposal is waiting for your decision.',
    // An iac_apply PR can come from Patch (on-demand) or Provision (from a
    // chat request) — the desk has no reliable field to attribute WHICH crew
    // authored a given PR, so this stays crew-neutral rather than guessing.
    'desk.pending.iac.who': 'An infrastructure change is waiting for your review',
    // Fallback headline for the decisions-derived arm (rule 2b), which never
    // carries a PR title — see DeskPendingIacProvenance's header comment.
    'desk.pending.iac.headlineFallback': 'Infrastructure change PR #{pr} is waiting for your approval.',
    'desk.pending.prMeta': 'PR #{pr}',
    'desk.pending.subtitleProposedAt': 'Proposed {time}',
    // Both anchors point at the SAME href (deskModel's `href`) — the actual
    // Approve/Reject controls live on that HMAC-gated page (agent/main.py's
    // approval_post decision=approve|reject), never in-app. See the
    // iac_reject_nonbinding_semantics note: Reject there persists nothing.
    // ds-hdt. States the fact (no notification went out) and its
    // consequence (you are seeing this because you looked), without
    // alarming: the proposal itself is unaffected and still actionable.
    'desk.pending.notifyFailed':
      'No notification could be sent for this proposal, so it has been waiting here unannounced.',
    'desk.pending.approveCta': 'Approve this proposal',
    'desk.pending.rejectCta': 'Reject',
    // The mockup's `.why` line reads "view the reasoning behind this (N
    // steps)". The step count is dropped deliberately: it lives in the trace
    // this link would open, so printing it would mean either fetching every
    // trace the desk might show or guessing. Same discipline as the rest of
    // this namespace (ds-wd2.15). Wording tracks the rail's existing
    // `shared.rail.traceButton.viewReasoning` so one product means one phrase.
    'desk.pending.viewReasoning': 'view the reasoning behind this →',

    'desk.stamped.who': 'You approved',
    'desk.stamped.rollback.detail': 'Rollback applied',
    'desk.stamped.iac.detail': 'Change applied',
    'desk.stamped.rollback.headline': 'The proposed rollback was applied.',
    'desk.stamped.iac.headlineFallback': 'Infrastructure change PR #{pr} was applied.',
    'desk.stamped.iac.headlineGeneric': 'An infrastructure change was applied.',
    'desk.stamped.audit': 'Applied {time}',

    // The resting state's watch line — see ApprovalDesk's header comment:
    // "nothing needs you right now" is the product's promise kept, so this
    // line (not the calm headline) is what proves the agent is still awake.
    'desk.resting.headline': 'Nothing needs your decision right now.',
    'desk.resting.watching': 'The agent is watching',
    'desk.resting.lastScan': 'last scan {time}',
    // Rendered instead of a fabricated time when graph.generated_at is null
    // — calm must never look dead, but it must also never invent a scan time
    // that didn't happen (Task 3.5 spec).
    'desk.resting.scanPending': 'scan time pending',
    // Distinct from scanPending: that one promises something in flight. This is
    // for a cycle that FINISHED without a usable graph, which would otherwise
    // sit on "pending" until the next 45s poll (Codex review of #258).
    'desk.resting.scanUnavailable': 'scan time unavailable',
    // A graph fetch failed this cycle, so the timestamp beside it is the last
    // GOOD scan, not a current one. Without this the line ages into a quiet
    // false claim that the estate was just checked (ds-eh6).
    'desk.resting.scanStale': '(not refreshed just now)',
    'desk.resting.resourceCount': '{n} resources',

    // ---- unknown: we have not established whether anything needs the
    // operator (ds-eh6). Deliberately NOT phrased as an error or an empty
    // state. `resting` is a promise kept; these two are the honest admission
    // that the promise has not been checked yet, which is a different thing
    // and must never be rendered as the first.
    'desk.unknown.loading.headline': 'Checking whether anything needs your decision…',
    'desk.unknown.loading.body': 'Reading the estate and the decision record.',
    // "could not confirm", never "nothing is pending" and never "something
    // failed" — the one thing known here is the absence of knowledge.
    'desk.unknown.degraded.headline': "We couldn't confirm whether anything needs your decision.",
    'desk.unknown.degraded.body':
      'Part of the record could not be read just now, so a waiting proposal may not be shown here. This retries on its own.',
    // Only shown when scope.drift === 0 — a true "nothing new" claim, not a
    // fixed decoration (see ApprovalDesk: this segment is conditional).
    'desk.resting.noNewDrift': 'no new drift',

    // EstateView (Task 4.1, mockup "SCREEN 2 — 推定図"). Rows are grouped by
    // STATUS (drift first, then managed) and flattened across resource types
    // — see lib/estate.ts's estateModel(). JA copy is verbatim from the
    // mockup wherever the mockup has a matching string.
    'desk.estate.ariaLabel': 'Estate',
    'desk.estate.loading': 'Loading the estate…',
    'desk.estate.degraded': 'The estate map is temporarily unavailable.',
    'desk.estate.driftGroup': 'Drift — not managed by IaC ({n})',
    'desk.estate.managedGroup': 'Managed by IaC ({n})',
    'desk.estate.untrackedGroup': 'Not managed, not adoptable ({n})',
    'desk.estate.adoptButton': 'Open an adoption PR',
    // Shown in the Adopt button's place when the pending-approvals lane was
    // unreliable this cycle. Offering Adopt there would claim no adoption PR
    // exists, which is exactly what we failed to establish (Codex review #258).
    'desk.estate.adoptUnavailable': 'adoption status unknown',
    'desk.estate.prPending': 'PR #{pr} awaiting review',
    'desk.estate.driftMore': '…{n} more drift',
    'desk.estate.systemManagedFold': 'System-managed resources ({n}) · created by Google',
    // Pluralized on the TYPE count via i18n.ts's `.one`/`.other` convention —
    // a live estate very often has exactly one out-of-scope type, and the
    // single-form string rendered "…across 1 types…" in the EN estate shot.
    // The call site composes params itself (it needs `other` alongside
    // `types`, which `plural()` doesn't pass) — same precedent as shared.ts.
    'desk.estate.otherResources.one': "{other} more resources in 1 type DriftScribe doesn't manage",
    'desk.estate.otherResources.other':
      "{other} more resources across {types} types DriftScribe doesn't manage",
    'desk.estate.legendManaged': 'Managed by IaC',
    'desk.estate.legendDrift': 'Not managed by IaC · drift',
  },
  ja: {
    'desk.nav.ariaLabel': 'メインナビゲーション',
    'desk.nav.desk': 'デスク',
    'desk.nav.chat': 'チャット',
    'desk.seal.ariaLabel': '承認済み',
    // Visible labels straight from the mockup (docs/plans/2026-07-28-
    // composite-mockup.html "instrument band"). Aria variants prefix the
    // count so a screen reader announces "9件、IaC管理下" rather than a bare
    // label with no number.
    'desk.band.managedLabel': 'IaC 管理下',
    'desk.band.driftLabel': 'ドリフト検出',
    'desk.band.awaitingLabel': 'あなたの承認待ち',
    // 承認待ちだけが操作できない数値（ds-s61 — 対象のキューがすぐ下にあるため）。
    'desk.band.awaitingAria': '{n}件、あなたの承認待ち',
    // ds-7ag.2 — 操作できる数値だけが遷移先を名乗る（EN 側の命名規則コメント参照）。
    // `Desk` サフィックスは名残：2026-07-31 の統合前は文脈を表していた。
    'desk.band.managedAriaDesk': '{n}件、IaC 管理下 — インフラを見る',
    'desk.band.driftAriaDesk': '{n}件、ドリフト検出 — インフラを見る',
    'desk.band.statHintEstate': 'インフラを見る →',
    'desk.band.awaitingUnknownAria': 'あなたの承認待ち：未取得',
    'desk.band.managedUnknownAriaDesk': 'IaC 管理下：未取得 — インフラを見る',
    'desk.band.driftUnknownAriaDesk': 'ドリフト検出：未取得 — インフラを見る',
    'desk.ledger.heading': '最近の記録',
    'desk.ledger.appliedTitle': 'あなたが承認 → 適用完了',
    'desk.ledger.openTitle': 'あなたの承認待ち',
    'desk.ledger.failedTitle': 'あなたが承認 → 適用されず',
    'desk.ledger.unconfirmedTitle': 'あなたが承認 → 結果は未確認',
    'desk.ledger.showMore': '{n}件すべて表示',
    'desk.record.incomplete': 'このトレースには判断の記録が紐づいていません。',
    'desk.record.outOfWindow': 'この判断は、下の一覧より古い記録です。',
    // ---- unresolved rollback outcome (desk rule 2.5) ----
    'desk.unresolved.who': 'あなたが承認しました',
    'desk.unresolved.failed.detail': '適用されず',
    'desk.unresolved.failed.headline': 'ロールバックは適用されませんでした。',
    'desk.unresolved.failed.body':
      '承認は使用されましたが、トラフィックの切り替えは行われませんでした。ロールバックは実行されていません。必要であれば、あらためてロールバックを依頼できます。',
    'desk.unresolved.unknown.detail': '結果は未確認',
    'desk.unresolved.unknown.headline': 'ロールバックの結果を確認できていません。',
    'desk.unresolved.unknown.body':
      'トラフィックの切り替えは受理されましたが、待機時間内に完了を確認できませんでした。成功・失敗のいずれとも断定できません。対応の前に Cloud Run で当該サービスの状態をご確認ください。',

    'desk.region.ariaLabel': '承認デスク',

    'desk.pending.rollback.who': 'Anchor が提案しています',
    'desk.pending.rollback.headline': '承認が必要なロールバック提案があります。',
    'desk.pending.iac.who': 'インフラ変更があなたの確認を待っています',
    'desk.pending.iac.headlineFallback': 'インフラ変更 PR #{pr} があなたの承認を待っています。',
    'desk.pending.prMeta': 'PR #{pr}',
    'desk.pending.subtitleProposedAt': '提案 {time}',
    'desk.pending.notifyFailed':
      'この提案の通知は送信できませんでした。お知らせのないまま、ここでお待ちしていました。',
    'desk.pending.approveCta': 'この提案を承認する',
    'desk.pending.rejectCta': '却下する',
    'desk.pending.viewReasoning': 'この提案に至った推論を見る →',

    'desk.stamped.who': 'あなたが承認しました',
    'desk.stamped.rollback.detail': 'ロールバック適用',
    'desk.stamped.iac.detail': '適用完了',
    'desk.stamped.rollback.headline': '提案されたロールバックを適用しました。',
    'desk.stamped.iac.headlineFallback': 'インフラ変更 PR #{pr} を適用しました。',
    'desk.stamped.iac.headlineGeneric': 'インフラ変更を適用しました。',
    'desk.stamped.audit': '適用 {time}',

    'desk.resting.headline': 'いま、あなたの判断を待つ提案はありません。',
    'desk.resting.watching': 'エージェントは監視を継続中',
    'desk.resting.lastScan': '最終走査 {time}',
    'desk.resting.scanPending': '走査時刻 取得中',
    // 取得中 means "currently acquiring" — wrong once the fetch has finished
    // and failed. 取得できず states the settled failure instead.
    'desk.resting.scanUnavailable': '走査時刻 取得できず',
    'desk.resting.scanStale': '（今回は更新できていません）',
    'desk.resting.resourceCount': '{n} リソース',

    // ---- unknown（ds-eh6）
    'desk.unknown.loading.headline': '判断が必要な提案があるか確認しています…',
    'desk.unknown.loading.body': 'インフラの状態と決定の記録を読み込んでいます。',
    'desk.unknown.degraded.headline': '判断が必要な提案があるか、確認できませんでした。',
    'desk.unknown.degraded.body':
      '記録の一部を取得できなかったため、承認をお待ちしている提案がここに表示されていない可能性があります。自動的に再試行します。',
    'desk.resting.noNewDrift': '新規ドリフトなし',

    // The mockup called this 推定図, but 推定 reads as "estimation/inference"
    // — closer to "estimation diagram" than to "your infrastructure". Operator
    // decision 2026-07-28: use インフラ, matching the term this same domain
    // already ships under (infra.panel.title). It named the nav TAB first; the
    // 2026-07-31 merge deleted that tab, and the decision moved here with the
    // term, since this is now the section's only name.
    'desk.estate.ariaLabel': 'インフラ',
    'desk.estate.loading': 'インフラ情報を読み込み中…',
    'desk.estate.degraded': 'インフラ図は一時的に取得できません。',
    'desk.estate.driftGroup': 'ドリフト — IaC 未管理 {n} 件',
    'desk.estate.managedGroup': '管理下 — {n} 件',
    'desk.estate.untrackedGroup': '未管理（取り込み対象外） {n} 件',
    'desk.estate.adoptButton': '取り込み PR を作成',
    'desk.estate.adoptUnavailable': '取り込み状況を確認できません',
    'desk.estate.prPending': 'PR #{pr} レビュー待ち',
    'desk.estate.driftMore': '…ほか {n} 件のドリフト',
    'desk.estate.systemManagedFold': 'システム管理リソース（Google が自動作成） {n}件',
    // JA carries no grammatical plural, so .one/.other are identical text
    // (i18n.ts `plural()` convention) — both forms still get catalogued.
    'desk.estate.otherResources.one': '他に DriftScribe が管理しない {types} 種類、{other} 件のリソースがあります',
    'desk.estate.otherResources.other':
      '他に DriftScribe が管理しない {types} 種類、{other} 件のリソースがあります',
    'desk.estate.legendManaged': 'IaC 管理下',
    'desk.estate.legendDrift': 'IaC 未管理 ・ ドリフト',
  },
};
