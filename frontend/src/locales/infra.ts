// infra namespace — InfraDiagram, CoverageMeter, DriftDiffCard, infra_graph.ts.
// Arbitrary backend prose (degraded_reason, caveat) stays pass-through (EN).
// Filled by the i18n fan-out.
//
// EN values are moved BYTE-FOR-BYTE from the code they used to live in (the
// unit-test suite is pinned to EN via tests/unit/setup.ts) so the existing
// assertions keep passing unchanged. `.one`/`.other` pairs follow i18n.ts's
// `plural()` convention (JA carries identical text in both — no grammatical
// plural); a few counts need an extra non-`n` param (e.g. a pass-through
// resource-type `label`), so those are keyed by hand with the same suffix
// rather than routed through `plural()`.
export const infra = {
  en: {
    'infra.panel.title': 'Infrastructure',
    'infra.badge.loading': 'loading…',
    'infra.badge.unavailable': 'unavailable',
    'infra.badge.driftTitle': 'Drift in supported resource types',
    'infra.badge.inSyncTitle': 'In supported resource types',
    'infra.badge.outOfScopeTitle': "These resources are in types DriftScribe doesn't manage",
    'infra.badge.outOfScope': 'out of scope',
    'infra.badge.unmatchedTitle': 'Declared in IaC, not found in the latest inventory',
    'infra.badge.unmatchedCount': '{n} IaC unmatched',

    'infra.count.drift': '{n} drift',
    'infra.label.inSync': 'in sync',
    'infra.label.notTracked': 'not tracked',
    'infra.label.countsOnly': 'counts-only',
    'infra.label.systemManaged': 'system-managed',
    'infra.label.notAdoptableType': 'not an adoptable type',

    'infra.summary.count': '{managed}/{resources} managed',
    'infra.summary.countWithPct': '{managed}/{resources} managed · {pct}%',

    'infra.preview.lead':
      'Previewing PR #{pr}. Dashed nodes show what approving this change ' +
      'would do. The live map does not change until the change is applied.',
    'infra.preview.hiddenMore': ' · +{n} more not shown',
    'infra.preview.exit': 'Exit preview',
    'infra.preview.error': 'Could not load the change preview.',
    'infra.preview.unavailable.noPlan': 'No pending plan was found for PR #{pr}. Nothing to preview.',
    'infra.preview.unavailable.artifactError':
      'The plan for PR #{pr} could not be verified, so it cannot be previewed. ' +
      'Open the approval page for details.',
    'infra.preview.unavailable.resolved':
      'PR #{pr} has already reached a final outcome. The map below shows what is live now.',
    'infra.preview.unavailable.summaryUnavailable':
      'This plan could not be summarized into a preview. Review the approval page instead.',
    'infra.preview.rendering': 'Rendering diagram…',

    'infra.legend.title': 'Legend',
    'infra.legend.managed': 'managed in IaC',
    'infra.legend.drift': 'adoptable drift',
    'infra.legend.openPr': 'Open PR',
    'infra.legend.ghostCreate': 'will be created',
    'infra.legend.ghostUpdate': 'will be modified',
    'infra.legend.ghostDestroy': 'will be destroyed',
    'infra.legend.helpAriaLabel': 'Explain the resource colors and tags',
    'infra.legend.help':
      'Every box is a real resource in your project. Green means managed in IaC: ' +
      'it is defined in OpenTofu, so DriftScribe tracks it and can change it through ' +
      'the approval flow. Yellow means adoptable drift: the resource exists, is not ' +
      'in any .tf file, and is a type DriftScribe can import, so it has an Adopt ' +
      'button. Grey is neutral: counts-only rows hide sensitive names such as ' +
      'secrets and show only a number; named rows tagged system-managed are ' +
      'protected (the OpenTofu state and artifact buckets DriftScribe owns, or ' +
      'resources a Google service creates automatically, such as Cloud Build ' +
      'buckets and the Pub/Sub pairs Eventarc uses to deliver trigger events: ' +
      'the denylist blocks changing or adopting them); and rows in a type ' +
      'DriftScribe cannot import are marked ' +
      'not an adoptable type. Those grey rows are real but not counted as drift.' +
      ' A blue marker means an adoption PR is already open for that resource: open ' +
      'it from the card or the band at the top to review and approve, instead of ' +
      'adopting it again.',

    'infra.pending.ariaLabel': 'Open infrastructure changes',
    'infra.pending.title': 'Open infra changes ({n})',
    'infra.pending.prLink': 'PR #{pr} →',

    'infra.unmatched.title': 'Declared in IaC, not found live',
    'infra.unmatched.lead':
      'These declarations did not match the latest Cloud Asset Inventory snapshot. ' +
      'Index lag or an unapplied IaC change can cause this.',
    'infra.unmatched.ariaLabel': 'IaC declarations not found in the latest inventory',
    'infra.unmatched.investigate': 'Investigate',
    'infra.unmatched.investigateHint':
      'Ask Provision to investigate this declaration (opens a draft, sends nothing)',
    'infra.unmatched.trailer': '+{n} more declarations not shown',

    'infra.disabledHint': 'Unavailable while the chat is busy or reviewing past reasoning.',

    'infra.hero.noSupportedResources': 'No resources in supported types yet.',
    'infra.hero.noResourcesIndexed': 'No resources indexed yet.',
    'infra.hero.degraded':
      'Infrastructure inventory is unavailable right now{reason}. Cloud Asset ' +
      'Inventory may still be initializing. Try refreshing in a moment.',
    // Reason-wrapper for infra.hero.degraded's `{reason}` param. Split into its
    // own key so JA can own its punctuation (full-width parens, no leading
    // space) instead of the component hand-formatting ASCII " (reason)" and
    // baking it into every locale (InfraDiagram.svelte's degraded-hero line).
    'infra.degraded.withReason': ' ({reason})',
    'infra.hero.loading': 'Loading inventory…',
    'infra.hero.unavailable': 'Inventory unavailable.',
    'infra.hero.scopeNote': "{total} total resources indexed · {other} in types DriftScribe doesn't manage",
    'infra.hero.refresh': 'Refresh',
    'infra.hero.refreshing': 'Refreshing…',

    'infra.error.reachCoordinator': 'Could not reach the coordinator.',
    'infra.error.requestFailed': 'Request failed ({status}).',
    'infra.error.malformed': 'Malformed response.',
    'infra.error.renderDiagram': 'Could not render the diagram.',

    'infra.card.startHere': 'Start here',
    'infra.card.managedTag': 'managed',
    'infra.card.pendingLink': 'Review pending adoption (PR #{pr}) →',
    'infra.card.pendingTag': 'PR open',
    'infra.card.adoptButton': 'Adopt into IaC',
    'infra.card.systemManagedCount': '{n} system-managed',
    'infra.card.protected': '· protected',
    'infra.card.systemManagedMore': '+{n} more not shown',
    'infra.card.trailerUnmanaged': '+{n} more unmanaged {label}(s) not shown',
    'infra.card.trailerNotShown': '+{n} more {label}(s) not shown',
    'infra.card.hiddenCountLine.one': '{n} {label} · hidden',
    'infra.card.hiddenCountLine.other': '{n} {label}s · hidden',
    'infra.card.notListedLine.one': '{n} {label} · not individually listed',
    'infra.card.notListedLine.other': '{n} {label}s · not individually listed',

    'infra.other.lead': "Other resources DriftScribe doesn't manage",
    'infra.other.typeCount.one': '{n} type',
    'infra.other.typeCount.other': '{n} types',
    'infra.other.resourceCount.one': '{n} resource',
    'infra.other.resourceCount.other': '{n} resources',
    'infra.meta.separator': ' · ',

    'infra.coverage.subjectDefault': 'your infrastructure',
    'infra.coverage.subjectSupported': 'your supported infrastructure',
    'infra.coverage.ariaLabel': 'IaC coverage',
    // '{{PCT}}' is a literal marker (not a `{param}` — interpolate() leaves an
    // unrecognized `{word}` untouched), split out in the component to slot in
    // the separately-styled/tested percentage <strong>. Keeps this a single
    // whole-sentence key per locale (EN leads with the number; JA needs it
    // mid-sentence) with no empty catalog value.
    'infra.coverage.headline': '{{PCT}} of {subject} is under IaC management',
    'infra.coverage.ariaValueText': '{pct}% of {subject}, {managed} of {resources} resources managed',
    'infra.coverage.detail': '{managed} of {resources} resources managed',
    'infra.coverage.detailWithDrift': '{managed} of {resources} resources managed · {drift} not yet in IaC',

    'infra.driftDiff.ariaLabel': 'Environment drift detail',
    'infra.driftDiff.label': 'Drift detail',
    'infra.driftDiff.colVar': 'Var',
    'infra.driftDiff.colExpected': 'Expected',
    'infra.driftDiff.colLive': 'Live',
    'infra.driftDiff.colStatus': 'Status',

    // lib/infra_graph.ts — the preview-banner counts line (overlayCountsLine)
    // and the Mermaid ghost-graph chrome (toMermaid). Resource NAMES /
    // type_label / group.label stay pass-through data (never keyed here); only
    // the static verbs/separators/placeholders are localized. Every t(...)
    // result still routes through escapeMermaidLabel at the call site.
    'infra.graph.overlay.create': '{n} will be created',
    'infra.graph.overlay.update': '{n} will be modified',
    'infra.graph.overlay.replace': '{n} will be replaced',
    'infra.graph.overlay.destroy': '{n} will be destroyed',
    'infra.graph.overlay.import': '{n} will be imported',
    'infra.graph.overlay.forget': '{n} will leave management',
    'infra.graph.overlay.change': '{n} will change',
    'infra.graph.overlay.sep': ' · ',
    'infra.graph.overlay.noChanges': 'No infrastructure changes',

    'infra.graph.verb.create': 'will be created',
    'infra.graph.verb.import': 'will be imported',
    'infra.graph.verb.update': 'will be modified',
    'infra.graph.verb.change': 'will change',
    'infra.graph.verb.forget': 'will leave IaC management',
    'infra.graph.verb.destroy': 'will be destroyed',
    'infra.graph.verb.replace': 'will be replaced',

    'infra.graph.hidden': 'hidden',
    'infra.graph.resource.one': 'resource',
    'infra.graph.resource.other': 'resources',
    'infra.graph.more': '+{n} more',
    'infra.graph.morePlanned': '+{n} more planned change(s)',
    'infra.graph.plannedChanges': 'Planned changes',
    'infra.graph.empty': 'No resources indexed yet',
  },
  ja: {
    'infra.panel.title': 'インフラ',
    'infra.badge.loading': '読み込み中…',
    'infra.badge.unavailable': '利用不可',
    'infra.badge.driftTitle': '対象リソース内のドリフト',
    'infra.badge.inSyncTitle': '対象リソース内で同期済み',
    'infra.badge.outOfScopeTitle': 'DriftScribe の管理対象外のリソース種別です',
    'infra.badge.outOfScope': '対象外',
    'infra.badge.unmatchedTitle': 'IaC には定義されていますが、最新のインベントリには見つかりません',
    'infra.badge.unmatchedCount': 'IaC と一致しない宣言：{n}件',

    'infra.count.drift': '{n}件のドリフト',
    'infra.label.inSync': '同期済み',
    'infra.label.notTracked': '未追跡',
    'infra.label.countsOnly': '件数のみ',
    'infra.label.systemManaged': 'システム管理',
    'infra.label.notAdoptableType': 'IaC 管理への取り込み対象外',

    'infra.summary.count': 'IaC 管理済み：{managed}/{resources}件',
    'infra.summary.countWithPct': 'IaC 管理済み：{managed}/{resources}件・{pct}%',

    'infra.preview.lead':
      'PR #{pr} をプレビュー中です。破線のノードは、この変更を承認した場合に何が起こるかを' +
      '示します。実環境のマップは、変更が適用されるまで変わりません。',
    'infra.preview.hiddenMore': '・ほかに{n}件あります（表示されていません）',
    'infra.preview.exit': 'プレビューを終了',
    'infra.preview.error': '変更のプレビューを読み込めませんでした。',
    'infra.preview.unavailable.noPlan':
      'PR #{pr} の承認待ちの IaC プランが見つかりませんでした。プレビューする内容はありません。',
    'infra.preview.unavailable.artifactError':
      'PR #{pr} の IaC プランを検証できなかったため、プレビューできません。詳細は承認ページを開いて' +
      '確認してください。',
    'infra.preview.unavailable.resolved':
      'PR #{pr} の処理はすでに完了しています。以下のマップには現在の実環境が表示されています。',
    'infra.preview.unavailable.summaryUnavailable':
      'この IaC プランからプレビューを生成できませんでした。代わりに承認ページを確認してください。',
    'infra.preview.rendering': '図を描画中…',

    'infra.legend.title': '凡例',
    'infra.legend.managed': 'IaC 管理済み',
    'infra.legend.drift': 'IaC 管理に取り込み可能なドリフト',
    'infra.legend.openPr': '開いている PR',
    'infra.legend.ghostCreate': '作成されます',
    'infra.legend.ghostUpdate': '変更されます',
    'infra.legend.ghostDestroy': '削除されます',
    'infra.legend.helpAriaLabel': '色とタグの意味を説明',
    'infra.legend.help':
      'すべてのボックスは、プロジェクト内の実際のリソースです。緑は IaC 管理済みを意味します。' +
      'OpenTofu で定義されているため、DriftScribe がそのリソースを追跡し、承認フローを通じて' +
      '変更できます。黄色は IaC 管理に取り込み可能なドリフトを意味します。そのリソースは存在しますが、' +
      'どの .tf ファイルにも定義されておらず、DriftScribe が IaC 管理に取り込める種類のため、取り込むボタンが' +
      '表示されます。グレーは中立的な状態です。件数のみの行は、シークレットなどの機密性の高い名前を' +
      '隠して件数のみを表示します。「システム管理」タグの付いた名前付きの行は保護されています' +
      '（DriftScribe 自身が所有する OpenTofu の状態管理・成果物用バケット、または Google の' +
      'サービスが自動的に作成するリソースです。例えば Cloud Build のバケットや、Eventarc が' +
      'トリガーイベントの配信に使う Pub/Sub のペアです。これらは拒否リストにより変更や IaC 管理への取り込みが' +
      'ブロックされています）。また、DriftScribe が取り込めない種類のリソースの行には' +
      '「IaC 管理への取り込み対象外」と表示されます。これらのグレーの行は実在しますが、ドリフトとしては' +
      '数えられません。青いマーカーは、そのリソースの取り込み用プルリクエストがすでに開いている' +
      'ことを意味します。再度取り込むのではなく、カードまたは上部のバンドから開いて確認・承認して' +
      'ください。',

    'infra.pending.ariaLabel': '開いているインフラの変更',
    'infra.pending.title': '開いているインフラの変更（{n}件）',
    'infra.pending.prLink': 'PR #{pr} →',

    'infra.unmatched.title': 'IaC に定義済み・実環境で未検出',
    'infra.unmatched.lead':
      'これらの宣言は、最新の Cloud Asset Inventory のスナップショットと一致しませんでした。' +
      'インデックスの遅延や、まだ適用されていない IaC の変更が原因である可能性があります。',
    'infra.unmatched.ariaLabel': '最新のインベントリに見つからない IaC の宣言',
    'infra.unmatched.investigate': '調査する',
    'infra.unmatched.investigateHint':
      'Provision にこの宣言の調査を依頼します（下書きを開くだけで送信しません）',
    'infra.unmatched.trailer': 'ほかに {n}件の宣言があります（表示されていません）',

    'infra.disabledHint': 'チャットが処理中、または過去の実行を表示している間は利用できません。',

    'infra.hero.noSupportedResources': '対応する種類のリソースはまだありません。',
    'infra.hero.noResourcesIndexed': 'まだリソースがインデックスされていません。',
    'infra.hero.degraded':
      'インフラのインベントリは現在利用できません{reason}。Cloud Asset Inventory がまだ初期化中の' +
      '可能性があります。しばらくしてから再度お試しください。',
    'infra.degraded.withReason': '（{reason}）',
    'infra.hero.loading': 'インベントリを読み込み中…',
    'infra.hero.unavailable': 'インベントリを利用できません。',
    'infra.hero.scopeNote': '合計{total}件のリソースをインデックス済み・うち{other}件は DriftScribe の管理対象外です',
    'infra.hero.refresh': '更新',
    'infra.hero.refreshing': '更新中…',

    'infra.error.reachCoordinator': 'コーディネーターに接続できませんでした。',
    'infra.error.requestFailed': 'リクエストに失敗しました（{status}）。',
    'infra.error.malformed': '応答の形式が正しくありません。',
    'infra.error.renderDiagram': '図を描画できませんでした。',

    'infra.card.startHere': 'はじめに',
    'infra.card.managedTag': 'IaC 管理済み',
    'infra.card.pendingLink': '保留中の IaC 管理への取り込みを確認（PR #{pr}）→',
    'infra.card.pendingTag': 'PR あり',
    'infra.card.adoptButton': 'IaC 管理に取り込む',
    'infra.card.systemManagedCount': 'システム管理 {n}件',
    'infra.card.protected': '・保護対象',
    'infra.card.systemManagedMore': 'ほかに {n}件あります（表示されていません）',
    'infra.card.trailerUnmanaged': 'ほかに IaC 未管理の {label} が {n}件あります（表示されていません）',
    'infra.card.trailerNotShown': 'ほかに {label} が {n}件あります（表示されていません）',
    'infra.card.hiddenCountLine.one': '{label}：{n}件・非表示',
    'infra.card.hiddenCountLine.other': '{label}：{n}件・非表示',
    'infra.card.notListedLine.one': '{label}：{n}件・個別には表示されません',
    'infra.card.notListedLine.other': '{label}：{n}件・個別には表示されません',

    'infra.other.lead': 'DriftScribe の管理対象外のリソース',
    'infra.other.typeCount.one': '{n}種類',
    'infra.other.typeCount.other': '{n}種類',
    'infra.other.resourceCount.one': '{n}件のリソース',
    'infra.other.resourceCount.other': '{n}件のリソース',
    'infra.meta.separator': '・',

    'infra.coverage.subjectDefault': '稼働中のインフラ全体',
    'infra.coverage.subjectSupported': '対象のインフラ',
    'infra.coverage.ariaLabel': 'IaC 管理率',
    'infra.coverage.headline': '{subject}の {{PCT}} が IaC 管理下にあります。',
    'infra.coverage.ariaValueText': '{subject}の IaC 管理率は {pct}%、全{resources}件中{managed}件が IaC 管理済みです',
    'infra.coverage.detail': '{resources}件中{managed}件が IaC 管理済み',
    'infra.coverage.detailWithDrift': '{resources}件中{managed}件が IaC 管理済み・{drift}件は IaC 未管理',

    'infra.driftDiff.ariaLabel': '環境変数の差分の詳細',
    'infra.driftDiff.label': '環境変数の差分',
    'infra.driftDiff.colVar': '変数',
    'infra.driftDiff.colExpected': '期待値',
    'infra.driftDiff.colLive': '実環境',
    'infra.driftDiff.colStatus': 'ステータス',

    'infra.graph.overlay.create': '{n}件を作成予定',
    'infra.graph.overlay.update': '{n}件を変更予定',
    'infra.graph.overlay.replace': '{n}件を置き換え予定',
    'infra.graph.overlay.destroy': '{n}件を削除予定',
    'infra.graph.overlay.import': '{n}件を取り込み予定',
    'infra.graph.overlay.forget': '{n}件を IaC 管理から除外予定',
    'infra.graph.overlay.change': '{n}件を変更予定',
    'infra.graph.overlay.sep': '・',
    'infra.graph.overlay.noChanges': 'インフラの変更はありません。',

    'infra.graph.verb.create': '作成予定',
    'infra.graph.verb.import': '取り込み予定',
    'infra.graph.verb.update': '変更予定',
    'infra.graph.verb.change': '変更予定',
    'infra.graph.verb.forget': 'IaC 管理から除外予定',
    'infra.graph.verb.destroy': '削除予定',
    'infra.graph.verb.replace': '置き換え予定',

    'infra.graph.hidden': '非表示',
    'infra.graph.resource.one': 'リソース',
    'infra.graph.resource.other': 'リソース',
    'infra.graph.more': 'ほかに{n}件',
    'infra.graph.morePlanned': 'ほかに{n}件の予定されている変更',
    'infra.graph.plannedChanges': '予定されている変更',
    'infra.graph.empty': 'まだリソースがインデックスされていません',
  },
};
