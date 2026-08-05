// shared namespace — strings produced by the CROSS-surface lib helpers
// (format.ts iac/decision status + tokens, approval.ts CTA labels, workloads.ts
// crew descriptors/summaries/lifecycle, autonomyStore.ts autonomy note). Owned by
// the i18n foundation so no surface agent collides with them. Keys are prefixed
// `shared.`. Filled by the foundation stage.
//
// EN values are moved BYTE-FOR-BYTE from the code they used to live in (see the
// call sites in format.ts / approval.ts / workloads.ts) — the EN catalog is the
// app's original inline text, so the whole unit-test suite (pinned to EN via
// tests/unit/setup.ts) keeps asserting the same strings.
export const shared = {
  en: {
    'shared.iac.applied': 'applied',
    // Names the APPLY, not the rebuild (the internal enum stays
    // `waiting_for_rebake`; the cryptic insider term "re-bake" is gone).
    //
    // "awaiting rebuild" asserted something no client can see: the coordinator
    // never observes the external build, it writes this status at merge and
    // leaves it until the operator's second submit resumes the apply
    // (agent/main.py:7187), so the label went stale the moment the build
    // finished. It was also wrong the other way round while merge_state is
    // "pending", where the immediate wait is the MERGE. "awaiting apply" is the
    // one claim true across the whole window and BOTH merge_state variants,
    // which is what this label has to be (see the help text's note below).
    // Same fix as the ledger's `awaiting_apply` — this surface was missed on
    // that pass (Codex review r4). The help text below still explains the
    // rebuild step, where saying so is accurate and useful.
    'shared.iac.awaitingApply': 'awaiting apply',
    'shared.iac.failed': 'failed',
    'shared.iac.failedStateSuspect': 'failed (state suspect)',
    'shared.iac.ambiguous': 'ambiguous',

    // Accurate for BOTH waiting_for_rebake variants — recorded with
    // merge_state="pending" (before the irreversible merge / kept on merge
    // failure) AND merge_state="merged" (after) — so it must NOT assert the merge
    // already happened (agent/main.py records the pending pointer pre-merge).
    'shared.iac.help.awaitingApply':
      'Create/adopt changes apply in two steps: the PR is merged, then the ' +
      "agent's apply worker is rebuilt from the merged code and re-checks the " +
      "plan before applying. A later 'applied' step confirms completion.",
    // Plain `failed` (NOT the state-suspect variant): the apply aborted but the
    // tofu-apply worker PROVED the live state stayed clean (TofuStepError, vs
    // ApplyStateSuspect's "may be mutated"). We deliberately do NOT point the
    // operator at the underlying OpenTofu error — promising a location would be
    // false (see format.ts IAC_STATUS_HELP for the full rationale).
    'shared.iac.help.failed':
      "The apply didn't complete, but DriftScribe verified your live infrastructure " +
      'was left unchanged, so it is safe to fix the cause and retry. (Unlike "failed ' +
      '(state suspect)", the state was proven clean.)',
    'shared.iac.help.failedStateSuspect':
      "The apply didn't finish cleanly and the live infrastructure state may have " +
      'changed (or a lock was held), so the result is uncertain. Re-running ' +
      're-checks the live state before retrying.',
    'shared.iac.help.ambiguous':
      "DriftScribe couldn't confirm the final result of this apply (e.g. the change " +
      'merged but the apply outcome was unclear). View the reasoning to see what ' +
      'happened before retrying.',

    'shared.iac.superseded': 'superseded',
    'shared.iac.help.superseded':
      'Superseded by PR #{pr}, which is applied and merged. ' +
      "This plan is stale (its resource already exists), so there's nothing to do here.",

    'shared.iac.appliedMerged': 'applied & merged',
    'shared.iac.help.done': "This change is live and merged. There's nothing more to do here.",

    'shared.iac.appliedMergePending': 'applied · merge pending',
    // Must NOT promise that a plain retry clears a permanent branch-protection block —
    // mirrors agent/main.py `_iac_merge_step`'s own operator wording.
    'shared.iac.help.mergePending':
      "The apply succeeded, but its pull request hasn't merged yet. Open the approval " +
      'page to check the merge status, or retry once any branch-protection block is resolved.',

    // The complete set of actions the backend writes, in plain language — see
    // decisionActionLabel. Neutral nouns for the KIND of action, never its
    // outcome: the same string serves a proposed, an applied and an expired
    // row, and only that row's own status column knows which it is.
    'shared.decision.noOp': 'No action needed',
    'shared.decision.rollback': 'Rollback',
    'shared.decision.iacApply': 'Infrastructure change',
    'shared.decision.recheck': 'Re-check',
    // Per-variable contract verdicts (agent/models.py:ContractStatus), shown in
    // the diff card's STATUS column. Phrased from the OPERATOR's view — what the
    // ops-contract permits for this variable — rather than transliterating the
    // enum ("present/absent" describes the contract entry, not the situation).
    'shared.contract.match': 'Matches the contract',
    'shared.contract.presentAllowManual': 'Manual change allowed',
    'shared.contract.presentDisallowManual': 'Manual change not allowed',
    'shared.contract.absent': 'Not in the contract',
    'shared.decision.noOpHelp':
      'DriftScribe checked and the live state already matched what was expected, ' +
      'so there was nothing to fix: no pull request, issue, or rollback was needed. ' +
      'This entry is the record that the check ran and found everything in order.',

    'shared.tokens': '{n} tok',

    'shared.approve.supersededBy': 'superseded by #{pr} →',
    // ds-22k. These are reached ONLY for a non-superseded
    // `waiting_for_rebake` row, which the backend records at MERGE — so the
    // operator has already approved this once. Before merge confirmation the
    // POST retries that merge; afterwards it resumes the apply. Distinct from
    // `approval.iacCta.reviewApprove`, which fires on a FRESHLY authored PR with
    // no decision row at all — that one really is a first approval and keeps its
    // wording.
    'shared.approve.continueChange': 'Continue this change →',
    'shared.approve.applyChange': 'Apply this change →',
    'shared.approve.viewHistory': 'View approval history →',
    'shared.approve.viewFailure': 'View failure details →',
    'shared.approve.goToPage': 'Go to approval page →',

    'shared.crew.drift.lifecycle':
      'Guards what is live. Runs on its own, reacting when it detects drift.',
    'shared.crew.upgrade.lifecycle': 'Keeps it current. Proposes dependency upgrades.',
    'shared.crew.provision.lifecycle':
      'Stands infrastructure up. You describe a change; it opens the IaC PR.',
    'shared.crew.explore.lifecycle': 'Explains it. Read-only answers across the whole system.',

    // A descriptor's job is to DISTINGUISH its crew, and two of these did not
    // (ds-p84, ds-dfp). They now run four abreast in the composer's crew menu,
    // which is the surface that made it obvious.
    //
    // Anchor: "Cloud Run config" named a subject three crews share — Explore and
    // Provision both hold drift_read_live_env. What is Anchor's alone is that it
    // RULES on drift; it is the only holder of drift_patch_docs and
    // drift_propose_rollback. Not bare "drift", which PR #194 exists because of:
    // the word is overloaded between Anchor's configuration drift and the
    // estate's "drift (not in IaC)", and an operator once asked Anchor the
    // estate's sense and got a true "no drift" that read as the whole estate
    // being clean.
    //
    // Provision: "infra edits" asserted the exact thing Provision's own summary
    // denies two lines down — "It never touches live infra directly." Its
    // distinguishing property is the MEDIUM: it writes iac/-only HCL and opens
    // one PR, and live change happens downstream in the gated tofu-apply worker.
    // Paired with Anchor's old descriptor the two taught the lane model
    // backwards (Anchor↔config files, Provision↔live infra).
    'shared.crew.drift.descriptor': 'Cloud Run config drift',
    'shared.crew.upgrade.descriptor': 'dependencies',
    'shared.crew.provision.descriptor': 'IaC PRs',
    'shared.crew.explore.descriptor': 'read-only',

    'shared.crew.drift.summary':
      "Detects configuration drift between a Cloud Run service's live env vars and " +
      'the declared ops-contract.yaml, then proposes docs PRs for sanctioned changes ' +
      'or rollbacks for unsanctioned ones. Event-triggered via Eventarc: it runs when ' +
      'the service changes, not on a polling loop.',
    'shared.crew.upgrade.summary':
      "Checks the repo's package.json on demand for outdated or vulnerable " +
      'dependencies and proposes upgrade PRs.',
    'shared.crew.provision.summary':
      'Authors OpenTofu (IaC) changes from a chat request and opens one iac/-only PR ' +
      'for the gated apply pipeline. Never touches live infra directly.',
    'shared.crew.explore.summary':
      'Read-only investigation across infra and code — and the crew to ask when you ' +
      'just want to understand how DriftScribe itself works. Inspects live env vars, ' +
      'the ops-contract, the dependency lockfile, and developer docs, then reports. ' +
      'Changes nothing.',

  },
  ja: {
    'shared.iac.applied': '適用済み',
    'shared.iac.awaitingApply': '適用待ち',
    'shared.iac.failed': '失敗',
    'shared.iac.failedStateSuspect': '失敗（状態要確認）',
    'shared.iac.ambiguous': '結果不明',

    'shared.iac.help.awaitingApply':
      '新規作成または IaC 管理への取り込みに伴う変更は、2段階で適用されます。まず PR がマージされ、その後エージェントの' +
      '適用ワーカーがマージ済みのコードから再構築され、適用前に IaC プランを再確認します。' +
      '後続の「適用済み」のステップが完了を示します。',
    'shared.iac.help.failed':
      '適用は完了しませんでしたが、DriftScribe は実環境のインフラが変更されていないことを' +
      '確認済みです。原因を修正して再試行しても安全です。（「失敗（状態要確認）」とは異なり、' +
      '状態がクリーンであることが証明されています。）',
    'shared.iac.help.failedStateSuspect':
      '適用が正常に終了せず、実環境のインフラ状態が変更された可能性があります' +
      '（またはロックが保持されていました）。そのため結果は不確実です。' +
      '再実行時には、再試行の前に実環境の状態を再確認します。',
    'shared.iac.help.ambiguous':
      'DriftScribe はこの適用の最終結果を確認できませんでした' +
      '（例：変更はマージされたものの、適用結果が不明瞭だった場合など）。' +
      '再試行の前に、推論を見て何が起きたかを確認してください。',

    'shared.iac.superseded': '置き換え済み',
    'shared.iac.help.superseded':
      'PR #{pr} に置き換え済みで、そちらはすでに適用・マージされています。' +
      'この IaC プランは古くなっており（対象のリソースはすでに存在します）、' +
      'ここで行うことは何もありません。',

    'shared.iac.appliedMerged': '適用済み・マージ済み',
    'shared.iac.help.done': 'この変更はすでに実環境に反映され、マージ済みです。ここで行うことはもうありません。',

    'shared.iac.appliedMergePending': '適用済み・マージ待ち',
    'shared.iac.help.mergePending':
      '適用は成功しましたが、そのプルリクエストはまだマージされていません。' +
      '承認ページを開いてマージ状況を確認するか、ブランチ保護によるブロックが解消されてから' +
      '再試行してください。',

    'shared.decision.noOp': '対応不要',
    'shared.decision.rollback': 'ロールバック',
    'shared.decision.iacApply': 'インフラ変更',
    'shared.decision.recheck': '再チェック',
    'shared.contract.match': '契約どおり',
    'shared.contract.presentAllowManual': '手動変更は許可',
    'shared.contract.presentDisallowManual': '手動変更は不可',
    'shared.contract.absent': '契約に未記載',
    'shared.decision.noOpHelp':
      'DriftScribe が確認したところ、実環境の状態はすでに想定どおりであったため、' +
      '修正の必要はありませんでした。プルリクエスト、Issue、ロールバックのいずれも' +
      '不要でした。この記録は、チェックが実行され、すべて問題ないことが確認されたことを' +
      '示しています。',

    'shared.tokens': '{n} トークン',

    'shared.approve.supersededBy': '#{pr} に置き換え済み →',
    'shared.approve.continueChange': 'この変更を続行する →',
    'shared.approve.applyChange': 'この変更を適用する →',
    'shared.approve.viewHistory': '承認履歴を見る →',
    'shared.approve.viewFailure': '失敗の詳細を見る →',
    'shared.approve.goToPage': '承認ページへ →',

    'shared.crew.drift.lifecycle': '稼働中のインフラを守ります。自ら動作し、ドリフトを検知すると反応します。',
    'shared.crew.upgrade.lifecycle': '最新の状態を保ちます。依存関係のアップグレードを提案します。',
    'shared.crew.provision.lifecycle': 'インフラを構築します。変更内容を伝えると、IaC のプルリクエストを作成します。',
    'shared.crew.explore.lifecycle': 'システムについて説明します。システム全体を対象に、読み取り専用で回答します。',

    // See the EN block for why these two changed. JA has to make the SAME claim,
    // not a looser one: the slot runs parallel across four crews and two
    // locales, so a JA string that says less than its EN twin shows.
    // Provision's is 'IaC PR の作成' rather than the shorter 'IaC の変更'
    // precisely because the PR is the distinguishing part — and because
    // 'インフラの変更' stays in use nearby for the approval-gate prose and the
    // pending-changes panel, where it accurately means live infrastructure
    // changing.
    'shared.crew.drift.descriptor': 'Cloud Run の設定ドリフト',
    'shared.crew.upgrade.descriptor': '依存関係',
    'shared.crew.provision.descriptor': 'IaC PR の作成',
    'shared.crew.explore.descriptor': '読み取り専用',

    'shared.crew.drift.summary':
      'Cloud Run サービスの実環境にある環境変数と、宣言された ops-contract.yaml とのドリフトを' +
      '検知し、許可された変更には docs PR を、許可されていない変更にはロールバックを提案します。' +
      'Eventarc によるイベント駆動で、ポーリングではなくサービスの変更時に実行されます。',
    'shared.crew.upgrade.summary':
      'リポジトリの package.json を要求に応じて確認し、古い依存関係や脆弱性のある依存関係を' +
      '検出して、アップグレード PR を提案します。',
    'shared.crew.provision.summary':
      'チャットでの依頼から OpenTofu（IaC）の変更を作成し、承認ゲート付きの適用パイプラインに向けて、' +
      'iac/ 配下のみを変更する PR を1件開きます。実環境のインフラを直接変更することはありません。',
    'shared.crew.explore.summary':
      'インフラとコード全体を対象にした読み取り専用の調査を行い、DriftScribe 自体の仕組みを' +
      '知りたいときに頼れるエージェントチームです。実環境の環境変数、ops-contract、依存関係のロックファイル、' +
      '開発者向けドキュメントを確認し、結果を報告します。何も変更しません。',

    // JA carries no grammatical plural, so .one/.other are identical (i18n.ts
    // `plural()` convention). '・' (not '·') per the glossary's JA punctuation
    // list.

  },
};
