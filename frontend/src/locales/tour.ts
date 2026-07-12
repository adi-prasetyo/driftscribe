// tour namespace — TourCard, TourBanner, and the tour.ts step titles/copy.
//
// `tour.adopt.target.*` and `tour.controls.body`/`tour.next.body` embed
// backend/graph-derived values as raw params ({groupLabel}, {nodeLabel},
// {hint}): resource type labels and resource names are data, not chrome, and
// `adopt_hint` is free backend prose with no stable id, so all three pass
// through untranslated (same precedent as InfraDiagram's degraded_reason).
export const tour = {
  en: {
    // Step titles (TOUR_STEPS, resolved by TourCard via step.titleKey).
    'tour.step.welcome.title': 'Welcome',
    'tour.step.estate.title': 'Your estate',
    'tour.step.controls.title': 'You set the pace',
    'tour.step.adopt.title': 'Adopt your first resource',
    'tour.step.next.title': 'What happens next',

    // Step 1 — welcome (welcomeLine).
    'tour.welcome.subjectKnown': 'the GCP project {project}',
    'tour.welcome.subjectUnknown': 'your GCP project',
    'tour.welcome.body':
      'DriftScribe is a small crew keeping {subject} honest, from creation ' +
      'onward, and it works as a loop. Provision stands infrastructure up: you ' +
      'describe a change, it opens the IaC pull request. Anchor then guards what ' +
      'is live. It runs on its own, the only crew that does, watching your Cloud ' +
      'Run config and reacting the moment it drifts from its contract. Patch ' +
      'keeps your dependencies current, and Explore answers questions read-only, ' +
      'including how DriftScribe itself works. Provision, Patch, and Explore ' +
      'wait for you to ask. Infrastructure applies and rollbacks always wait for ' +
      'your approval. Only routine dependency updates can run end-to-end, and ' +
      'only at the Propose + Apply setting.',

    // Step 2 — estate (estateLine).
    'tour.estate.loading':
      'Your estate is still loading. The Infrastructure panel below will ' +
      'fill in shortly.',
    'tour.estate.degraded':
      'The resource inventory is unavailable right now (Cloud Asset ' +
      'Inventory may still be initializing). You can keep going and check ' +
      'the panel later.',
    'tour.estate.zeroWithOther':
      '{total} resources indexed, none are in resource types DriftScribe ' +
      'supports. They are types like Cloud Run revisions and container images ' +
      'it does not manage. The coverage meter below tracks your migration.',
    'tour.estate.zeroAlone':
      '{total} resources indexed, none are in resource types DriftScribe ' +
      'supports yet. The coverage meter below tracks your migration.',
    'tour.estate.inScope':
      '{total} resources indexed. In the resource types DriftScribe ' +
      'supports, {managed} of {resources} are under IaC management ({pct}%), ' +
      '{drift} not yet. The coverage meter below tracks your migration.',
    'tour.estate.inScopeWithOther':
      '{total} resources indexed. In the resource types DriftScribe ' +
      'supports, {managed} of {resources} are under IaC management ({pct}%), ' +
      '{drift} not yet. The other {other} are types it does not manage, like ' +
      'Cloud Run revisions and container images. The coverage meter below ' +
      'tracks your migration.',

    // Step 3 — controls (controlsLine). Honesty T2: the always-gated claim is
    // scoped to INFRASTRUCTURE edits; Propose + Apply may finish routine
    // dependency updates.
    'tour.controls.body':
      'The Mode control in the top bar governs what Anchor does on its own when it ' +
      'spots a change, and what the other agents may do when you ask: Observe (they ' +
      'only watch and report), Propose (they draft changes for your review), or ' +
      'Propose + Apply (they may also complete routine dependency updates ' +
      'end-to-end). At every setting, infrastructure edits pass your explicit ' +
      'approval gate. The Pause control sits next to it in the top bar and suspends ' +
      'all agent activity in one click.',

    // Step 5 — next (nextLine). Honesty T6: scoped to THIS adopt request.
    'tour.next.body':
      'When you send this adopt request, the agent drafts it as a GitHub pull ' +
      'request with a plan you can read in plain language: what it changes, ' +
      'what it can never touch, and what it is estimated to cost. The ' +
      'infrastructure change is applied only after you approve it on the ' +
      'review page. You can reopen this tour anytime from the Tour button in ' +
      'the header.',

    // Step 4 — adopt suggestion (adoptStepState).
    'tour.adopt.unavailable':
      'The estate inventory is not available yet, so the tour cannot ' +
      'suggest a first adoption. When it returns, the Adopt buttons live ' +
      'in the Infrastructure panel.',
    'tour.adopt.target.plain':
      'A good first adoption: the {groupLabel} `{nodeLabel}`. Adopting ' +
      'imports a resource into IaC exactly as it is. This zero-change ' +
      'import goes through the same review and approval as any ' +
      'other change.',
    'tour.adopt.target.withHint':
      'A good first adoption: the {groupLabel} `{nodeLabel}`. Adopting ' +
      'imports a resource into IaC exactly as it is. This zero-change ' +
      'import goes through the same review and approval as any ' +
      'other change. {hint}',
    'tour.adopt.allManaged':
      'Everything in your estate is already under IaC management, so ' +
      'there is nothing left to adopt. You are ahead of this tour.',
    'tour.adopt.allPending':
      'Everything the tour could suggest adopting next already has an ' +
      'adoption PR open and waiting for review. Open it from the Open infra ' +
      'changes band at the top of the Infrastructure panel instead of ' +
      'starting a second adoption of the same resource.',
    'tour.adopt.systemManagedOnly':
      'The unmanaged resources the agent could otherwise adopt are ' +
      'system-managed infrastructure: DriftScribe control-plane services ' +
      'and IaC state/artifact buckets, or resources a Google service ' +
      'auto-creates, like Cloud Build buckets and Eventarc trigger ' +
      'transport. The always-on denylist blocks the agent from ' +
      'changing these, adoption included. The Infrastructure panel shows ' +
      'everything that is there.',
    'tour.adopt.noNamedTarget':
      'There are unmanaged resources the agent could adopt, but none ' +
      'has a named adopt target the tour can prefill. The ' +
      'Infrastructure panel shows what the live graph can show.',
    'tour.adopt.notAdoptableTypes':
      'Your remaining unmanaged resources are not adoptable types. ' +
      'The Infrastructure panel shows what is there, and you can ask ' +
      'about any of them in chat.',

    // TourCard chrome.
    'tour.card.ariaLabel': 'Guided tour',
    'tour.card.closeAria': 'Close tour',
    'tour.card.progress': '{current} of {total}',
    'tour.card.adoptDisabledTitle':
      'Unavailable while the chat is busy or reviewing past reasoning.',
    'tour.card.adoptButton': 'Prefill the request',
    'tour.card.adoptNote':
      'This only prefills the chat. Nothing is sent until you press Send.',
    'tour.card.busyNote':
      'The chat is busy or showing past reasoning right now, so sending becomes ' +
      'available when it finishes.',
    'tour.card.back': 'Back',
    'tour.card.next': 'Next',
    'tour.card.finish': 'Finish',

    // TourBanner.
    'tour.banner.lead': 'New here? Take the 5-minute tour.',
    'tour.banner.sub':
      'See your estate, the controls you hold, and how to adopt your first ' +
      'resource into IaC.',
    'tour.banner.start': 'Start the tour',
    'tour.banner.dismiss': 'Dismiss',
  },
  ja: {
    'tour.step.welcome.title': 'ようこそ',
    'tour.step.estate.title': '保有リソース',
    'tour.step.controls.title': '操作ペースはあなた次第',
    'tour.step.adopt.title': '最初のリソースを IaC 管理に取り込む',
    'tour.step.next.title': 'この後の流れ',

    'tour.welcome.subjectKnown': 'GCP プロジェクト「{project}」',
    'tour.welcome.subjectUnknown': 'あなたの GCP プロジェクト',
    'tour.welcome.body':
      'DriftScribe は{subject}を常に健全な状態に保つ、少人数のエージェントチーム' +
      'です。リソースの作成時から一貫して見守り、ループとして機能します。Provision ' +
      'がインフラをプロビジョニングします。あなたが変更内容を伝えると、IaC の' +
      'プルリクエストを作成します。続いて Anchor が稼働中のインフラを守ります。' +
      '自律的に動作する唯一のエージェントチームで、Cloud Run の設定を監視し、' +
      'ドリフト（IaC の定義と実環境のずれ）を検知するとすぐに反応します。Patch は依存関係を最新の状態に' +
      '保ち、Explore は DriftScribe 自体の仕組みも含めて、質問に読み取り専用で' +
      '回答します。Provision、Patch、Explore はあなたからの依頼を待って動きます。' +
      'IaC の適用とロールバックは、常にあなたの承認を待ちます。自動で最後まで' +
      '完了できるのは日常的な依存関係の更新のみで、しかも自律動作レベルが' +
      '「提案＋適用」の場合に限られます。',

    'tour.estate.loading':
      '保有リソースの情報を読み込んでいます。下のインフラパネルにまもなく表示され' +
      'ます。',
    'tour.estate.degraded':
      '現在、リソース一覧を取得できません（Cloud Asset Inventory がまだ初期化中の' +
      '可能性があります）。そのままツアーを進めて、パネルは後で確認できます。',
    'tour.estate.zeroWithOther':
      '{total}件のリソースがインデックスされていますが、いずれも DriftScribe が' +
      '対応するリソースタイプではありません。Cloud Run のリビジョンやコンテナ' +
      'イメージなど、管理対象外の種類です。下の IaC 管理率メーターが移行の進み' +
      '具合を示します。',
    'tour.estate.zeroAlone':
      '{total}件のリソースがインデックスされていますが、DriftScribe が対応する' +
      'リソースタイプはまだありません。下の IaC 管理率メーターが移行の進み具合を' +
      '示します。',
    'tour.estate.inScope':
      '{total}件のリソースがインデックスされています。DriftScribe が対応する' +
      'リソースタイプのうち、{resources}件中{managed}件が IaC 管理下にあり' +
      '（{pct}%）、残り{drift}件は IaC 未管理です。下の IaC 管理率メーターが移行の進み具合を' +
      '示します。',
    'tour.estate.inScopeWithOther':
      '{total}件のリソースがインデックスされています。DriftScribe が対応する' +
      'リソースタイプのうち、{resources}件中{managed}件が IaC 管理下にあり' +
      '（{pct}%）、残り{drift}件は IaC 未管理です。残り {other}件は、Cloud Run の' +
      'リビジョンやコンテナイメージなど、管理対象外の種類です。下の IaC 管理率' +
      'メーターが移行の進み具合を示します。',

    'tour.controls.body':
      '画面上部の自律動作レベルが、変化を検知したときに Anchor が自律的に' +
      '何を行うか、また依頼したときに他のエージェントが何を行えるかを決めます。' +
      '監視のみ（見て報告するだけ）、提案（レビュー用の変更案を作成）、提案＋適用' +
      '（日常的な依存関係の更新であれば最後まで完了できる）のいずれかです。どの' +
      '設定であっても、インフラの変更は必ず明示的な承認ゲートを通ります。上部' +
      'バーの隣には一時停止コントロールがあり、ワンクリックですべてのエージェント' +
      'の動作を止められます。',

    'tour.next.body':
      'この IaC 管理への取り込みリクエストを送信すると、エージェントが GitHub の' +
      'プルリクエストとして下書きを作成します。何を変更するか、絶対に触れない' +
      '範囲、概算コストを、平易な言葉で書かれた IaC プランで確認できます。インフラ' +
      'の変更は、レビューページであなたが承認した後にのみ適用されます。この' +
      'ツアーは、ヘッダーの「ツアー」ボタンからいつでも再開できます。',

    'tour.adopt.unavailable':
      '保有リソースの一覧がまだ取得できないため、ツアーは最初の IaC 管理への取り込み候補を' +
      '提案できません。取得でき次第、インフラパネルに「IaC 管理に取り込む」ボタンが表示' +
      'されます。',
    'tour.adopt.target.plain':
      '最初の IaC 管理への取り込み候補としておすすめなのは、{groupLabel} の「{nodeLabel}」です。' +
      'IaC 管理に取り込むと、リソースの現在の状態がそのまま IaC に反映されます。この' +
      '変更を伴わない取り込みも、他の変更と同じレビューと承認のプロセスを経ます。',
    'tour.adopt.target.withHint':
      '最初の IaC 管理への取り込み候補としておすすめなのは、{groupLabel} の「{nodeLabel}」です。' +
      'IaC 管理に取り込むと、リソースの現在の状態がそのまま IaC に反映されます。この' +
      '変更を伴わない取り込みも、他の変更と同じレビューと承認のプロセスを経ます。 ' +
      '{hint}',
    'tour.adopt.allManaged':
      '保有リソースはすべてすでに IaC 管理下にあるため、これ以上取り込むものは' +
      'ありません。このツアーで案内する作業はすでに完了しています。',
    'tour.adopt.allPending':
      'ツアーが次に提案できる IaC 管理への取り込み候補は、すべてすでにレビュー待ちの取り込み ' +
      'PR が開かれています。同じリソースを二重に取り込むのではなく、インフラ' +
      'パネル上部の「開いているインフラの変更」から開いて確認してください。',
    'tour.adopt.systemManagedOnly':
      'エージェントが本来取り込めるはずの IaC 未管理リソースは、システムが管理する' +
      'インフラです。DriftScribe 自身のコントロールプレーンのサービスや IaC の' +
      '状態・成果物用バケット、あるいは Cloud Build 用バケットや Eventarc トリガー' +
      'の転送経路のように Google のサービスが自動作成するリソースが該当します。' +
      '常時有効な拒否リストが、これらの変更を IaC 管理への取り込みも含めて禁止して' +
      'います。インフラパネルには、そこにあるものがすべて表示されます。',
    'tour.adopt.noNamedTarget':
      'エージェントが取り込める IaC 未管理リソースはありますが、ツアーが事前入力' +
      'できる名前付きの IaC 管理への取り込み対象がありません。インフラパネルには、' +
      '実環境のグラフで確認できる範囲が表示されます。',
    'tour.adopt.notAdoptableTypes':
      '残りの IaC 未管理リソースは、IaC 管理に取り込めるリソースタイプではありません。' +
      'インフラパネルにはそこにあるものが表示されており、チャットでいつでも質問' +
      'できます。',

    'tour.card.ariaLabel': 'ガイドツアー',
    'tour.card.closeAria': 'ツアーを閉じる',
    'tour.card.progress': '全{total}ステップ中 {current}',
    'tour.card.adoptDisabledTitle':
      'チャットが処理中か、過去の実行を表示している間は利用できません。',
    'tour.card.adoptButton': 'リクエストを入力欄に反映',
    'tour.card.adoptNote':
      'これは入力欄に反映するだけです。送信ボタンを押すまで何も送信されません。',
    'tour.card.busyNote':
      '現在チャットが処理中か過去の実行を表示しているため、完了すると送信できる' +
      'ようになります。',
    'tour.card.back': '戻る',
    'tour.card.next': '次へ',
    'tour.card.finish': '完了',

    'tour.banner.lead': '初めてですか？ 5分のツアーを試してみましょう。',
    'tour.banner.sub':
      '保有リソースの状況、あなたの操作権限、そして最初のリソースを IaC ' +
      '管理に取り込む方法を確認できます。',
    'tour.banner.start': 'ツアーを始める',
    'tour.banner.dismiss': '閉じる',
  },
};
