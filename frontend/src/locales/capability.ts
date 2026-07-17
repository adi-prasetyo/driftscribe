// capability namespace — CapabilityCard, capabilities.ts, autonomy.ts,
// AutonomyPill, PausePill, PauseBanner.
//
// Backend capability metadata (gate/rule/tool/worker/action ids from
// GET /capabilities) is localized here via stable-id keys
// (`capability.gate.<id>.*`, `capability.rule.<id>`, `capability.tool.<name>`,
// `capability.worker.<name>`, `capability.action.<name>`,
// `capability.adoptableType.<type>`) that capabilities.ts's lookup helpers
// resolve, falling back to the DTO's own English when an id isn't mapped.
// Arbitrary free prose with no stable id (denylist.summary, denylist.enforced_at,
// iam_note, provenance, and the raw system-prompt text served by
// /workloads/{name}/prompts) is NOT here — CapabilityCard renders it verbatim
// (English) straight from the DTO.
export const capability = {
  en: {
    // ---- Card chrome ----
    'capability.card.title': 'What this agent can and cannot do',
    'capability.card.hint': 'safety cage, generated from enforcement code',
    'capability.error.load': 'Could not load capability data.',

    // ---- Gates section ----
    'capability.gates.heading': 'Always needs your approval',

    // ---- Denylist section ----
    'capability.denylist.heading': "What's always blocked",
    'capability.denylist.enforcedAt': 'checked at: {list}',
    'capability.denylist.adoptableTypes': 'Adoptable (import) types: {list}',

    // Denylist category headings — glossary-pinned (docs/i18n-glossary.md).
    'capability.category.controlPlane': 'Its own control plane is off-limits',
    'capability.category.serviceManaged': 'It leaves Google-created buckets alone',
    'capability.category.iam': 'It cannot change who has access',
    'capability.category.globalV1': 'It cannot destroy or replace anything',
    'capability.category.structural': 'Malformed plans are rejected outright',

    // ---- Workloads section ----
    'capability.workloads.heading': 'What each workload can use',
    'capability.workload.loopLabel': 'In the loop ·',
    'capability.workload.tools': 'Tools',
    'capability.workload.workers': 'Workers',
    'capability.workload.actions': 'Actions',
    'capability.workload.viewPrompt': 'View system prompt',
    'capability.workload.viewPrompts': 'View system prompts',
    'capability.badge.writeCapable': 'write-capable',
    'capability.badge.read': 'read',
    'capability.badge.needsApproval': 'needs approval',
    // The pill vocabulary is the honest one — only a wired trigger reads "Autonomous".
    'capability.pill.autonomous': 'Autonomous · also chat',
    'capability.pill.onDemand': 'On-demand · chat only',

    // ---- Per-crew prompt disclosure ----
    'capability.prompts.unavailable': 'Prompt source is unavailable right now.',
    'capability.prompts.recheckLabel': 'recheck prompt',
    'capability.prompts.chatLabel': 'chat prompt',
    'capability.prompts.noSeparateChat':
      'This crew has no separate chat prompt. It ships a single system prompt file.',
    'capability.prompts.runningArtifact': 'Running artifact ·',

    // ---- Shared across AutonomyPill / PausePill / PauseBanner ----
    'capability.saving': 'Saving…',
    'capability.reasonLabel': 'reason (optional)',
    'capability.reasonMetaLabel': 'reason:',

    // ---- Autonomy modes (autonomy.ts) — glossary-pinned ----
    'capability.mode.observe.label': 'Observe',
    'capability.mode.propose.label': 'Propose',
    'capability.mode.proposeApply.label': 'Propose + Apply',
    'capability.mode.observe.blurb':
      'Watch and report only. No pull requests, no issues, no applies.',
    'capability.mode.propose.blurb':
      'Open pull requests and issues for your review. Applies stay off.',
    'capability.mode.proposeApply.blurb':
      'Routine dependency updates can run end-to-end. Infrastructure changes still ' +
      'wait for your approval (current default).',
    // read_error composition: `${modeLabel('observe')} · fail-closed`.
    'capability.mode.failClosed': '{label} · fail-closed',

    'capability.autonomyExplainer.heading': 'How does the agent act on its own?',
    'capability.autonomyExplainer.body':
      'When a watched service changes (including changes made outside DriftScribe), ' +
      'Anchor runs automatically. No one has to ask, and this dial sets how far it may ' +
      'go in response. The dial is global: the same ceiling also bounds what the rest ' +
      'of the crew may do on the chat requests you make here. Anchor is just the only ' +
      'one that starts a run on its own.',

    // ---- AutonomyPill ----
    'capability.autonomyPill.checking': 'Mode · checking…',
    'capability.autonomyPill.retryAria': 'Autonomy state could not be read. Retry.',
    'capability.autonomyPill.retryTitle':
      'Autonomy state could not be read; the agent is failing closed to Observe. Click to retry.',
    'capability.autonomyPill.stateUnknown': 'State unknown · retry',
    'capability.autonomyPill.toggleAria': 'Autonomy mode: {label}. Change it.',
    'capability.autonomyPill.ariaLabel': 'Autonomy mode',
    'capability.autonomyPill.current': 'Current',
    // Split around the <strong>-wrapped mode name: "Switch to <b>Observe</b>? {blurb}".
    'capability.autonomyPill.confirmSwitchLead': 'Switch to ',
    'capability.autonomyPill.confirmSwitchTrail': '? {blurb}',
    'capability.autonomyPill.confirm': 'Confirm',
    'capability.autonomyPill.reasonPlaceholder': 'why this change? (recorded in the audit log)',
    'capability.autonomyPill.error': 'Could not save. Autonomy state unchanged. Please try again.',
    'capability.autonomyPill.setBy': 'Set by',
    'capability.autonomyPill.readErrorWarn':
      'autonomy state could not be read, failing closed to Observe',

    // ---- PausePill ----
    'capability.pausePill.checking': 'Checking…',
    'capability.pausePill.activeAria':
      'DriftScribe is active. Agent activity allowed within guardrails. Pause DriftScribe.',
    'capability.pausePill.dialogAria': 'Pause DriftScribe',
    'capability.pausePill.active': 'Active',
    'capability.pausePill.hint':
      'Pause all agent activity? New chats, rechecks, and approvals will be refused until you resume.',
    'capability.pausePill.reasonPlaceholder': 'e.g. scheduled maintenance',
    'capability.pausePill.confirmPause': 'Confirm pause',
    'capability.pausePill.error': 'Could not pause. State unchanged. Please try again.',
    'capability.pausePill.paused': 'Paused',
    'capability.pausePill.stateUnknown': 'State unknown',

    // ---- PauseBanner ----
    'capability.pauseBanner.unknown':
      'Pause state unknown. DriftScribe fails closed: changes are blocked until this resolves.',
    // Exact-string pinned (PauseBanner.test.ts): the Icon before it is aria-hidden
    // and contributes no text, so this string alone is the full pause-state text.
    'capability.pauseBanner.pausedState': 'DriftScribe is paused. No new agent activity will start.',
    'capability.pauseBanner.resume': 'Resume',
    'capability.pauseBanner.pausedBy': 'Paused by',
    'capability.pauseBanner.readErrorWarn': 'pause state could not be read, failing closed',
    'capability.pauseBanner.confirmHint':
      'Resume agent activity? DriftScribe will be able to start new chats, rechecks, and approvals.',
    'capability.pauseBanner.confirmResume': 'Confirm resume',
    'capability.pauseBanner.error': 'Could not save. Pause state unchanged. Please try again.',

    // ---- Backend metadata: human gates (agent/capabilities.py::HUMAN_GATES) ----
    'capability.gate.iacApply.title': 'IaC plan apply',
    'capability.gate.iacApply.description':
      // Deliberate EN drift from the backend HUMAN_GATES text: the literal
      // ``…`` markers around `tofu apply` render as plain text here, so both
      // locales drop them (gate descriptions have no markdown pipeline).
      'Before the apply worker runs tofu apply, an operator must approve the exact ' +
      'stored plan via the approval page. The approval is bound to the specific plan by ' +
      'a plan-bound HMAC with a signed expiry window. Approving one plan cannot approve another.',
    'capability.gate.rollback.title': 'Rollback',
    'capability.gate.rollback.description':
      'The rollback worker requires a valid operator approval token before it will execute ' +
      'any Cloud Run rollback. The approval is single-use with a 15-minute TTL and bound to ' +
      'the specific rollback request by HMAC. The worker re-verifies the token at execution time.',

    // ---- Backend metadata: denylist rules (driftscribe_lib/iac_plan_denylist.py::RULE_DESCRIPTIONS) ----
    'capability.rule.planJsonUnparseable': "The plan file isn't valid JSON (fail-closed).",
    'capability.rule.planJsonMissingResourceChanges':
      'The plan has no resource-changes list (fail-closed).',
    'capability.rule.planJsonMalformedChange':
      'A change entry is malformed, or a protected resource hides its identity (fail-closed).',
    'capability.rule.controlPlaneService': 'The Cloud Run services.',
    'capability.rule.controlPlaneSa': 'The service accounts.',
    'capability.rule.controlPlaneBucket':
      'The IaC state and artifact buckets, and every object inside them.',
    'capability.rule.serviceManagedBucket':
      'Cloud Build, App Engine, Cloud Functions, and Cloud Run source-deploy each auto-create ' +
      "their own buckets. Google's to manage, not DriftScribe's to track.",
    'capability.rule.serviceManagedPubsub':
      "Eventarc auto-creates a Pub/Sub topic and subscription to deliver each trigger's " +
      "events. Eventarc's to manage, not DriftScribe's to track.",
    'capability.rule.controlPlaneSecret':
      'The secrets: approval keys, the GitHub token, and every version.',
    'capability.rule.controlPlaneKms': 'The state-encryption KMS key and its key ring.',
    'capability.rule.wifConfigChange': 'Workload Identity Federation pools and providers.',
    'capability.rule.iamChangeForbiddenV1':
      'Any IAM change at all, even on unrelated resources (v1 floor).',
    'capability.rule.importWithChangesForbiddenV1':
      'Adopting a resource must change nothing: if importing it would also modify it, the ' +
      'plan is refused and the coordinator must regenerate config that matches live reality exactly.',
    'capability.rule.importTypeNotAdoptableV1':
      'Only Cloud Storage buckets, Pub/Sub topics and subscriptions, and Cloud Run services ' +
      'can be adopted in v1. Every other type is refused.',
    'capability.rule.importMixedPlanForbiddenV1':
      'An adoption plan may contain nothing but the adoption. Any other change in the same ' +
      'plan is refused.',
    'capability.rule.importBatchForbiddenV1':
      'One adoption at a time: a plan importing more than one resource is refused.',
    'capability.rule.deleteActionForbiddenV1': 'Deleting any resource (v1 floor).',
    'capability.rule.forgetActionForbiddenV1': 'Forgetting a resource from state (v1 floor).',
    'capability.rule.replaceActionForbiddenV1':
      'Replacing a resource: destroy-and-recreate (v1 floor).',
    'capability.rule.unknownActionForbiddenV1':
      'Any action outside the audited OpenTofu vocabulary (fail-closed against new verbs).',

    // ---- Backend metadata: tools (agent/capabilities.py::TOOL_DESCRIPTIONS) ----
    'capability.tool.driftReadLiveEnv':
      'Reads the live Cloud Run environment: deployed image, revision, environment ' +
      'variables, and service configuration.',
    'capability.tool.readProjectInventory':
      'Reads a read-only whole-project asset inventory via the infra-reader worker ' +
      '(Cloud Asset Viewer only, no write access).',
    'capability.tool.driftPatchDocs':
      'Updates the ops-contract documentation to record the current observed state ' +
      'after a drift detection run.',
    'capability.tool.driftProposeRollback':
      'Proposes a rollback; never executes one. It creates an approval that waits for an operator.',
    'capability.tool.notify':
      'Sends a notification via the notifier worker (counted as write-capable because ' +
      'it rides a sending credential).',
    'capability.tool.loadContract':
      'Loads the ops-contract YAML (the declarative ground truth) so the agent can ' +
      'compare it with observed state.',
    'capability.tool.searchRecentPrs':
      "Searches the target repo's recent pull requests (counted as write-capable " +
      'because it rides a repo credential).',
    'capability.tool.loadIacPlan':
      'Reads the latest verified plan artifact for a pending infrastructure PR and ' +
      'summarizes it in plain language. Read-only: cannot approve, reject, or apply anything.',
    'capability.tool.readTeamLog':
      'Reads DriftScribe\'s own decision log as "team memory": what the crews recently ' +
      'did or decided (adoptions, docs PRs, rollbacks, upgrades), newest first, optionally ' +
      'filtered to one PR. Read-only and allowlist-projected: surfaces recorded status and ' +
      'pointers only, never rationale, diffs, or approval tokens. Shows status, not the ' +
      'cause of a failed apply, and not live merge state.',
    'capability.tool.readConversations':
      'Reads recent chat conversations across crews as "team memory": what other crews ' +
      'recently discussed, newest first; pass a crew to filter, a query to title-search, ' +
      'or a conversation_id to read one thread. Read-only and allowlist-projected: turn ' +
      'text is secret-redacted, control/bidi-stripped, and snippet-capped; tool call ' +
      'details and approval tokens are never surfaced.',
    'capability.tool.upgradeReadDependencies':
      "Reads the target repo's dependency lockfile to identify outdated packages.",
    'capability.tool.upgradeProposePr':
      'Opens a dependency-upgrade pull request in the target repo.',
    'capability.tool.upgradeClosePr':
      'Closes an upgrade PR this agent opened, only when it is safe to do so (driftscribe ' +
      'label, upgrade/ branch, correct base).',
    'capability.tool.upgradeMergePr':
      'Merges an upgrade PR this agent opened, only after CI is green on the exact head ' +
      'commit. Fails closed.',
    'capability.tool.searchDeveloperDocs':
      'Searches the developer knowledge base for documentation relevant to the current task.',
    'capability.tool.retrieveDeveloperDoc':
      'Retrieves a specific document from the developer knowledge base by ID.',
    'capability.tool.provisionOpenInfraPr':
      'Authors OpenTofu files under iac/ and opens ONE pull request. Never applies anything; ' +
      'applying happens only through the gated approve-then-apply pipeline.',
    'capability.tool.provisionProposeAdoption':
      'Adopt an existing resource into IaC management via a zero-change import PR. Renders ' +
      'the config deterministically; cannot modify live infrastructure.',
    'capability.tool.getSessionState': 'Reserved; not implemented. No workload can use it.',
    'capability.tool.setSessionState': 'Reserved; not implemented. No workload can use it.',

    // ---- Backend metadata: workers (agent/capabilities.py::WORKER_DESCRIPTIONS) ----
    'capability.worker.driftReader':
      'Reads the live Cloud Run service state for drift detection. Read-only by the ' +
      'scope of calls it makes.',
    'capability.worker.driftDocs':
      'Patches the ops-contract documentation to record observed state.',
    'capability.worker.driftRollback':
      'Executes a Cloud Run rollback to a previous revision. Refuses anything without ' +
      'a valid operator approval token.',
    'capability.worker.infraReader':
      'Reads the whole-project GCP asset inventory. Read-only by IAM (asset viewer only).',
    'capability.worker.notifier':
      'Sends notifications (e.g. Slack or webhook). Carries a sending credential.',
    'capability.worker.upgradeReader':
      "Reads the target repo's dependency lockfile. Read-only by the scope of calls it makes.",
    'capability.worker.upgradeDocs':
      'Opens and manages upgrade pull requests in the target repo.',
    'capability.worker.tofuEditor':
      'Writes iac/-only files and opens PRs; never touches live infrastructure.',

    // ---- Backend metadata: actions (agent/workloads/registry.py::ACTION_REGISTRY) ----
    // "no_op" reuses shared.decision.noOp — see capabilities.ts's ACTION_DISPLAY_NAME_KEYS.
    'capability.action.docsPr': 'Docs PR',
    'capability.action.driftIssue': 'Drift issue',
    'capability.action.escalation': 'Escalate to human',
    'capability.action.rollback': 'Rollback (HITL)',
    'capability.action.upgradePr': 'Dependency upgrade PR',

    // ---- Backend metadata: adoptable resource types (agent/capabilities.py::ADOPTABLE_TYPE_LABELS) ----
    'capability.adoptableType.googleStorageBucket': 'Cloud Storage bucket',
    'capability.adoptableType.googlePubsubTopic': 'Pub/Sub topic',
    'capability.adoptableType.googlePubsubSubscription': 'Pub/Sub subscription',
    'capability.adoptableType.googleCloudRunV2Service': 'Cloud Run service',
  },
  ja: {
    // ---- Card chrome ----
    'capability.card.title': 'このエージェントにできること・できないこと',
    'capability.card.hint': '実際の制御コードから生成された安全機構',
    'capability.error.load': '機能情報を読み込めませんでした。',

    // ---- Gates section ----
    'capability.gates.heading': '必ずあなたの承認が必要',

    // ---- Denylist section ----
    'capability.denylist.heading': '常に禁止される操作',
    'capability.denylist.enforcedAt': 'チェック実施箇所：{list}',
    'capability.denylist.adoptableTypes': 'IaC 管理に取り込めるリソース種別：{list}',

    // Denylist category headings — glossary-pinned (docs/i18n-glossary.md).
    'capability.category.controlPlane': '自身のコントロールプレーンの変更は禁止',
    'capability.category.serviceManaged': 'Google が作成・管理するリソースの変更は禁止',
    'capability.category.iam': 'アクセス権（IAM）の変更は禁止',
    'capability.category.globalV1': 'リソースの削除・置換は禁止',
    'capability.category.structural': '不正な構造のプランは拒否',

    // ---- Workloads section ----
    'capability.workloads.heading': '各ワークロードで使用できる機能',
    'capability.workload.loopLabel': '運用管理ループでの役割・',
    'capability.workload.tools': 'ツール',
    'capability.workload.workers': 'ワーカー',
    'capability.workload.actions': 'アクション',
    'capability.workload.viewPrompt': 'システムプロンプトを見る',
    'capability.workload.viewPrompts': 'システムプロンプトを見る',
    'capability.badge.writeCapable': '書き込み可能',
    'capability.badge.read': '読み取り',
    'capability.badge.needsApproval': '承認が必要',
    'capability.pill.autonomous': '自律動作・チャットも可',
    'capability.pill.onDemand': 'オンデマンド・チャットのみ',

    // ---- Per-crew prompt disclosure ----
    'capability.prompts.unavailable': 'プロンプトのソースは現在利用できません。',
    'capability.prompts.recheckLabel': '再チェック用プロンプト',
    'capability.prompts.chatLabel': 'チャット用プロンプト',
    'capability.prompts.noSeparateChat':
      'このエージェントチームには独立したチャット用プロンプトはありません。システムプロンプトは1つのファイルにまとまっています。',
    'capability.prompts.runningArtifact': '実行中のアーティファクト・',

    // ---- Shared across AutonomyPill / PausePill / PauseBanner ----
    'capability.saving': '保存中…',
    'capability.reasonLabel': '理由（任意）',
    'capability.reasonMetaLabel': '理由：',

    // ---- Autonomy modes (autonomy.ts) — glossary-pinned ----
    'capability.mode.observe.label': '監視のみ',
    'capability.mode.propose.label': '提案',
    'capability.mode.proposeApply.label': '提案＋適用',
    'capability.mode.observe.blurb':
      '監視して報告するだけです。プルリクエストも Issue も作成せず、適用も行いません。',
    'capability.mode.propose.blurb':
      'あなたの確認用にプルリクエストと Issue を作成します。適用は行いません。',
    'capability.mode.proposeApply.blurb':
      '日常的な依存関係のアップグレードは最後まで自動で実行できます。インフラの変更は' +
      '引き続きあなたの承認を待ちます（現在のデフォルト）。',
    'capability.mode.failClosed': '{label}（フェイルクローズ）',

    'capability.autonomyExplainer.heading': 'エージェントはどのようなときに自ら動作しますか？',
    'capability.autonomyExplainer.body':
      '監視対象のサービスが変更されると（DriftScribe の外部での変更を含む）、Anchor が' +
      '自動的に起動します。誰かが依頼する必要はなく、この自律動作レベルがその際にどこまで' +
      '対応してよいかを決めます。この自律動作レベルは全体に適用されます。同じ上限が、ここで' +
      '行うチャットの依頼に対する他のエージェントチームの動作にも適用されます。自ら実行を開始するのは' +
      'Anchor だけです。',

    // ---- AutonomyPill ----
    'capability.autonomyPill.checking': 'モード・確認中…',
    'capability.autonomyPill.retryAria': '自律動作レベルの状態を読み込めませんでした。再試行する',
    'capability.autonomyPill.retryTitle':
      '自律動作レベルの状態を読み込めませんでした。フェイルクローズにより監視のみとして' +
      '動作しています。クリックで再試行できます。',
    'capability.autonomyPill.stateUnknown': '状態不明・再試行',
    'capability.autonomyPill.toggleAria': '自律動作レベル：{label}。変更する。',
    'capability.autonomyPill.ariaLabel': '自律動作レベル',
    'capability.autonomyPill.current': '現在',
    // <strong> で囲むモード名の前後に分割: "モードを{モード名}に切り替えますか？{blurb}"。
    'capability.autonomyPill.confirmSwitchLead': 'モードを',
    'capability.autonomyPill.confirmSwitchTrail': 'に切り替えますか？{blurb}',
    'capability.autonomyPill.confirm': '確認',
    'capability.autonomyPill.reasonPlaceholder': '変更する理由（監査ログに記録されます）',
    'capability.autonomyPill.error': '保存できませんでした。自律動作レベルは変更されていません。もう一度お試しください。',
    'capability.autonomyPill.setBy': '設定者',
    'capability.autonomyPill.readErrorWarn':
      '自律動作レベルの状態を読み込めず、フェイルクローズにより監視のみとして動作しています',

    // ---- PausePill ----
    'capability.pausePill.checking': '確認中…',
    'capability.pausePill.activeAria':
      'DriftScribe は稼働中です。ガードレールの範囲内でエージェントの活動が許可されています。DriftScribe を一時停止する。',
    'capability.pausePill.dialogAria': 'DriftScribe を一時停止',
    'capability.pausePill.active': '稼働中',
    'capability.pausePill.hint':
      'エージェントのすべての活動を一時停止しますか？ 再開するまで、新規チャット、再チェック、承認はすべて拒否されます。',
    'capability.pausePill.reasonPlaceholder': '例：定期メンテナンス',
    'capability.pausePill.confirmPause': '一時停止する',
    'capability.pausePill.error': '一時停止できませんでした。状態は変更されていません。もう一度お試しください。',
    'capability.pausePill.paused': '一時停止中',
    'capability.pausePill.stateUnknown': '状態不明',

    // ---- PauseBanner ----
    'capability.pauseBanner.unknown':
      '一時停止の状態が不明です。DriftScribe はフェイルクローズにより、解決するまで変更をブロックします。',
    'capability.pauseBanner.pausedState': 'DriftScribe は一時停止中です。新しいエージェントの活動は開始されません。',
    'capability.pauseBanner.resume': '再開',
    'capability.pauseBanner.pausedBy': '一時停止の実行者',
    'capability.pauseBanner.readErrorWarn': '一時停止の状態を読み込めず、フェイルクローズしています',
    'capability.pauseBanner.confirmHint':
      'エージェントの活動を再開しますか？ DriftScribe は新規チャット、再チェック、承認を開始できるようになります。',
    'capability.pauseBanner.confirmResume': '再開する',
    'capability.pauseBanner.error': '保存できませんでした。一時停止の状態は変更されていません。もう一度お試しください。',

    // ---- Backend metadata: human gates ----
    'capability.gate.iacApply.title': 'IaC プランの適用',
    'capability.gate.iacApply.description':
      '適用ワーカーが tofu apply を実行する前に、オペレーターが承認ページで保存済みの' +
      '特定のプランを承認する必要があります。承認はプラン固有の HMAC と署名付きの有効期限に' +
      'よって、その特定のプランに紐づけられます。あるプランの承認が別のプランを承認すること' +
      'はできません。',
    'capability.gate.rollback.title': 'ロールバック',
    'capability.gate.rollback.description':
      'ロールバックワーカーは、Cloud Run のロールバックを実行する前に、有効なオペレーター' +
      '承認トークンを必要とします。この承認は1回限りで、有効期限は15分、HMAC によって特定の' +
      'ロールバック要求に紐づけられます。ワーカーは実行時にもトークンを再検証します。',

    // ---- Backend metadata: denylist rules ----
    'capability.rule.planJsonUnparseable': 'プランファイルが正しい JSON 形式ではありません（フェイルクローズ）。',
    'capability.rule.planJsonMissingResourceChanges':
      'プランに resource-changes のリストが含まれていません（フェイルクローズ）。',
    'capability.rule.planJsonMalformedChange':
      '変更エントリの形式が不正であるか、保護対象のリソースがその識別情報を隠しています' +
      '（フェイルクローズ）。',
    'capability.rule.controlPlaneService': 'Cloud Run のサービス。',
    'capability.rule.controlPlaneSa': 'サービスアカウント。',
    'capability.rule.controlPlaneBucket':
      'IaC のステートおよびアーティファクト用バケットと、その中のすべてのオブジェクト。',
    'capability.rule.serviceManagedBucket':
      'Cloud Build、App Engine、Cloud Functions、Cloud Run のソースデプロイは、それぞれ' +
      '独自のバケットを自動作成します。これらは Google が管理するものであり、DriftScribe が' +
      '追跡する対象ではありません。',
    'capability.rule.serviceManagedPubsub':
      'Eventarc は各トリガーのイベントを配信するために、Pub/Sub のトピックとサブスクリプション' +
      'を自動作成します。これらは Eventarc が管理するものであり、DriftScribe が追跡する対象' +
      'ではありません。',
    'capability.rule.controlPlaneSecret': 'シークレット類：承認鍵、GitHub トークン、およびそのすべてのバージョン。',
    'capability.rule.controlPlaneKms': 'ステート暗号化用の KMS 鍵とその鍵リング。',
    'capability.rule.wifConfigChange': 'Workload Identity Federation のプールとプロバイダ。',
    'capability.rule.iamChangeForbiddenV1':
      '無関係なリソースに対するものも含め、あらゆる IAM の変更（v1 の最低限の制約）。',
    'capability.rule.importWithChangesForbiddenV1':
      'リソースを IaC 管理に取り込む際は、何も変更してはなりません。取り込みによってそのリソース' +
      'が変更されてしまう場合、プランは拒否され、コーディネーターは実環境に正確に一致する設定を' +
      '作成し直す必要があります。',
    'capability.rule.importTypeNotAdoptableV1':
      'v1 で IaC 管理に取り込めるのは、Cloud Storage バケット、Pub/Sub のトピックとサブスクリプション、' +
      'Cloud Run サービスのみです。それ以外の種類はすべて拒否されます。',
    'capability.rule.importMixedPlanForbiddenV1':
      'IaC 管理への取り込みのプランには、その取り込み以外の内容を含めることはできません。' +
      '同じプランに他の変更が含まれている場合は拒否されます。',
    'capability.rule.importBatchForbiddenV1':
      'IaC 管理への取り込みは一度に1件までです。複数のリソースを取り込むプランは拒否されます。',
    'capability.rule.deleteActionForbiddenV1': 'リソースの削除（v1 の最低限の制約）。',
    'capability.rule.forgetActionForbiddenV1': 'ステートからのリソースの除外（forget）（v1 の最低限の制約）。',
    'capability.rule.replaceActionForbiddenV1':
      'リソースの置換（削除してから再作成すること）（v1 の最低限の制約）。',
    'capability.rule.unknownActionForbiddenV1':
      '監査済みの OpenTofu の操作語彙に含まれないあらゆる操作（新しい操作に対してフェイルクローズ）。',

    // ---- Backend metadata: tools ----
    'capability.tool.driftReadLiveEnv':
      '実環境の Cloud Run サービス情報を読み取ります：デプロイ済みのイメージ、リビジョン、環境変数、' +
      'サービス設定。',
    'capability.tool.readProjectInventory':
      'infra-reader ワーカーを通じて、プロジェクト全体の読み取り専用アセットインベントリを' +
      '取得します（Cloud Asset Viewer 権限のみ、書き込みアクセスなし）。',
    'capability.tool.driftPatchDocs':
      'ドリフト検知の実行後、現在の観測状態を記録するために ops-contract のドキュメントを' +
      '更新します。',
    'capability.tool.driftProposeRollback':
      'ロールバックを提案します。実行は行いません。オペレーターの承認を待つ承認リクエストを' +
      '作成します。',
    'capability.tool.notify':
      'notifier ワーカーを通じて通知を送信します（送信用の資格情報を利用するため、書き込み' +
      '可能として扱われます）。',
    'capability.tool.loadContract':
      'ops-contract の YAML（宣言された正しい状態の基準）を読み込み、観測状態と比較できるように' +
      'します。',
    'capability.tool.searchRecentPrs':
      '対象リポジトリの最近のプルリクエストを検索します（リポジトリの資格情報を利用するため、' +
      '書き込み可能として扱われます）。',
    'capability.tool.loadIacPlan':
      '承認待ちのインフラ PR について、検証済みの最新プランのアーティファクトを読み取り、' +
      '平易な言葉で要約します。読み取り専用で、承認・却下・適用のいずれも行えません。',
    'capability.tool.readTeamLog':
      'DriftScribe 自身の判断履歴を「エージェントチームの共有メモリ」として読み取ります。各エージェントチームが' +
      '最近何を行い、何を判断したか（IaC 管理への取り込み、docs PR、ロールバック、アップグレード）' +
      'を新しい順に取得し、特定の PR に絞り込むこともできます。読み取り専用で、許可リストに' +
      '基づいて情報が選別されており、記録されたステータスと参照先のみを表示し、判断理由や差分、承認トークンは' +
      '一切表示しません。表示するのはステータスのみで、適用が失敗した原因や現在のマージ状態は' +
      '表示しません。',
    'capability.tool.readConversations':
      '各エージェントチームの最近のチャットを「エージェントチームの共有メモリ」として読み取ります。他の' +
      'エージェントチームが最近何を話し合ったかを新しい順に取得でき、エージェントチームを指定して絞り込む、クエリで' +
      'タイトル検索する、または conversation_id を指定して1つのスレッドを読むこともできます。' +
      '読み取り専用で、許可リストに基づいて情報が選別されており、各発言はシークレットが伏字化され、制御文字・' +
      '双方向制御文字が除去され、長さにも上限が設けられています。ツール呼び出しの詳細や承認' +
      'トークンが表示されることはありません。',
    'capability.tool.upgradeReadDependencies':
      '対象リポジトリの依存関係ロックファイルを読み取り、古くなったパッケージを特定します。',
    'capability.tool.upgradeProposePr':
      '対象リポジトリに依存関係アップグレードのプルリクエストを作成します。',
    'capability.tool.upgradeClosePr':
      'このエージェントが作成したアップグレード PR を、安全に行える場合に限りクローズします' +
      '（driftscribe ラベル、upgrade/ ブランチ、正しいベースブランチであることが条件）。',
    'capability.tool.upgradeMergePr':
      'このエージェントが作成したアップグレード PR を、対象の HEAD コミットで CI が成功している場合' +
      'に限りマージします。フェイルクローズです。',
    'capability.tool.searchDeveloperDocs':
      '現在のタスクに関連するドキュメントを、開発者向けナレッジベースから検索します。',
    'capability.tool.retrieveDeveloperDoc':
      '開発者向けナレッジベースから、ID を指定して特定のドキュメントを取得します。',
    'capability.tool.provisionOpenInfraPr':
      'iac/ 配下に OpenTofu のファイルを作成し、プルリクエストを1件だけ開きます。適用は' +
      '一切行わず、適用は承認ゲート付きの承認・適用パイプラインを通じてのみ行われます。',
    'capability.tool.provisionProposeAdoption':
      '変更を伴わない取り込み PR を通じて、既存のリソースを IaC 管理に取り込みます。設定は' +
      '常に同じ結果になるよう生成され、実環境のインフラを変更することはできません。',
    'capability.tool.getSessionState': '予約済み。未実装です。どのワークロードも使用できません。',
    'capability.tool.setSessionState': '予約済み。未実装です。どのワークロードも使用できません。',

    // ---- Backend metadata: workers ----
    'capability.worker.driftReader':
      'ドリフト検知のために、実環境の Cloud Run サービスの状態を読み取ります。発行する呼び' +
      '出しの範囲において読み取り専用です。',
    'capability.worker.driftDocs':
      '観測した状態を記録するために ops-contract のドキュメントを更新します。',
    'capability.worker.driftRollback':
      'Cloud Run を以前のリビジョンへロールバックします。有効なオペレーター承認トークンが' +
      'ない場合は、いかなる実行も拒否します。',
    'capability.worker.infraReader':
      'プロジェクト全体の GCP アセットインベントリを読み取ります。IAM 上、読み取り専用です' +
      '（asset viewer 権限のみ）。',
    'capability.worker.notifier':
      '通知を送信します（Slack や webhook など）。送信用の資格情報を保持しています。',
    'capability.worker.upgradeReader':
      '対象リポジトリの依存関係ロックファイルを読み取ります。発行する呼び出しの範囲において' +
      '読み取り専用です。',
    'capability.worker.upgradeDocs':
      '対象リポジトリでアップグレードのプルリクエストを作成・管理します。',
    'capability.worker.tofuEditor':
      'iac/ 配下のファイルのみを作成し、プルリクエストを開きます。実環境のインフラには一切' +
      '触れません。',

    // ---- Backend metadata: actions ----
    'capability.action.docsPr': 'ドキュメント PR',
    'capability.action.driftIssue': 'ドリフトの Issue',
    'capability.action.escalation': '人による対応へエスカレーション',
    'capability.action.rollback': 'ロールバック（人による確認・承認）',
    'capability.action.upgradePr': '依存関係アップグレード PR',

    // ---- Backend metadata: adoptable resource types ----
    'capability.adoptableType.googleStorageBucket': 'Cloud Storage バケット',
    'capability.adoptableType.googlePubsubTopic': 'Pub/Sub トピック',
    'capability.adoptableType.googlePubsubSubscription': 'Pub/Sub サブスクリプション',
    'capability.adoptableType.googleCloudRunV2Service': 'Cloud Run サービス',
  },
};
