# DriftScribe デモスクリプト (日本語)

> English version: [`docs/demo-script.md`](demo-script.md)

ハッカソン向けの 90 秒程度のライブデモのためのオペレーター用ランブックです。観客はハッカソン審査員 1 名 (画面録画を視聴) を想定しています。デモの操作者がコマンドを実行し、本ファイルはキーボード横に置くチートシートとして機能します。

ランナー: `scripts/demo.sh` (Phase 16.2)。すべての beat はデプロイ済みのコーディネーター `driftscribe-agent` に POST し、レスポンスから `X-Trace-Id` を出力します。これにより Cloud Logging 上のログを追跡できます。

## 事前準備 (録画の 5 分前に実行)

```bash
export PROJECT=driftscribe-hack-2026
export REGION=asia-northeast1

# 1. Confirm coordinator is up and reachable.
gcloud run services describe driftscribe-agent \
  --project="$PROJECT" --region="$REGION" \
  --format='value(status.url)'

# 2. Confirm USE_ADK=true on the current revision (beats c, e need this).
gcloud run services describe driftscribe-agent \
  --project="$PROJECT" --region="$REGION" \
  --format='value(spec.template.spec.containers[0].env)' \
  | grep -o 'USE_ADK=[a-z]*'

# 3. Open the architecture diagram in a browser tab.
#    File: docs/architecture/architecture.html
#    Tip: open it locally (file://) so you can scroll/zoom without
#    network jitter on the recording.

# 4. Restore baseline env on payment-demo. Idempotent.
./scripts/demo.sh cleanup

# 5. Sanity check — beat-a should print action=no_op.
./scripts/demo.sh beat-a

# 6. Operator token works. (call_coordinator inside the script
#    already exercised it on step 5; no separate check needed.)
```

これらのいずれかが失敗した場合は、録画前に根本原因を解決してください。本番のデモ中はコーディネーターのデプロイをデバッグするタイミングではありません。

## 画面レイアウト

```
+-------------------------------+-------------------------------+
|                               |                               |
|  Terminal (~80x24)            |  Browser: architecture.html   |
|                               |                               |
|  $ ./scripts/demo.sh beat-a   |  [diagram 1: Reader path]    |
|  ...                          |                               |
|                               |                               |
+-------------------------------+-------------------------------+
```

- 左半分: ~80×24 のターミナル。観客が判断 JSON を目を細めずに読めるようにフォントをスケールアップ (推奨: 16pt)。
- 右半分: ブラウザで `docs/architecture/architecture.html`。Beat A のナレーションが画面と一致するよう、事前に最上部の図までスクロールしておきます。
- オプションの 3 つ目のタブ (フレーム外、必要に応じて切替): `resource.labels.service_name="driftscribe-agent"` でフィルタした Cloud Logging — 最後の Trace ID の見せ場用です。

## タイミング (目標: 90 秒)

各 beat に約 12〜15 秒。各行の冒頭で Enter を押し、レスポンスが描画される間にナレーションをします。

| t (s) | ターミナル操作                        | ブラウザ操作                       | ナレーション                                                                                          |
| ----- | ------------------------------------- | --------------------------------- | -------------------------------------------------------------------------------------------------- |
| 0:00  | `./scripts/demo.sh beat-a`            | "Read" 経路までスクロール          | 「DriftScribe はライブ Cloud Run サービスを ops コントラクトと照合します。ベースラインチェックは no_op を返します」 |
| 0:12  | `./scripts/demo.sh beat-b`            | Drift-Issue ワーカーのボックスにホバー | 「PAYMENT_MODE を live に切り替えます。この変数はコントラクトでロックされている — エージェントはドリフトの issue を立てます」 |
| 0:25  | `./scripts/demo.sh beat-c`            | ADK 推論のボックスにホバー          | 「今度は未知の変数です。ADK の推論エージェントが docs を書くかエスカレーションするかを判断します」      |
| 0:42  | `./scripts/demo.sh beat-d`            | Docs ワーカーのボックスにホバー     | 「オペレーター切替可能な変数。エージェントは新しい値のプレビュー付きで docs PR を提案します」          |
| 0:58  | `./scripts/demo.sh beat-e`            | Rollback ワーカー + HITL にホバー   | 「コンボ: 実際のドリフト + 自然言語による rollback 要求。エージェントは承認 URL を返します — HITL です」 |
| 1:15  | 承認 URL をクリック → Approve         | 承認ページを前面に                  | 「オペレーターが Approve をクリック。Rollback ワーカーがリビジョン固定 を実行します。ドリフトは解消されました」 |
| 1:25  | `./scripts/demo.sh cleanup`           | アーキテクチャ図に戻る              | 「Cleanup でベースラインに復元。すべての beat が監査証跡用の X-Trace-Id を出力していました」          |

時間を超過しそうな場合は beat-c を落としてください — これが最も切り捨てやすい beat です。Beat-e はクライマックス (HITL + rollback)、beat-b はコントラクト強制の最も明快な例なので、両方残します。

## Beat ごとの期待される出力

スクリプトはこれらをアサートしていません — ターミナルを目視で確認してください。

**beat-a** (baseline):
```
<- HTTP 200  X-Trace-Id: <uuid>
{
  "action": "no_op",
  "trigger": "manual_recheck",
  ...
}
```

**beat-b** (PAYMENT_MODE drift, allow_manual_change=false):
```
<- HTTP 200  X-Trace-Id: <uuid>
{
  "action": "drift_issue",
  "target_var": "PAYMENT_MODE",
  "github": { "url": "https://github.com/.../issues/...", ... },
  ...
}
```

**beat-c** (unknown var):
- USE_ADK=true の場合: `action` は `docs_pr` または `escalate` (ADK 呼び出し)。
- USE_ADK=false の場合: `action` は `escalate` (classifier のデフォルト)。

**beat-d** (FEATURE_NEW_CHECKOUT drift, allow_manual_change=true):
```
<- HTTP 200  X-Trace-Id: <uuid>
{
  "action": "docs_pr",
  "target_var": "FEATURE_NEW_CHECKOUT",
  "target_docs_file": "demo/docs/runbook.md",
  "github": { "url": "https://github.com/.../pull/...", ... },
  ...
}
```

**beat-e** (rollback via /chat, USE_ADK=true):
```
<- HTTP 200  X-Trace-Id: <uuid>
{
  "action": "rollback",
  "approval_url": "https://driftscribe-agent-.../approval/<id>?t=...",
  ...
}
```
USE_ADK=false では beat-e は次を返します:
```
<- HTTP 503  X-Trace-Id: <uuid>
{ "detail": "ADK not enabled (set USE_ADK=true to enable /chat)" }
```

## アップグレード ワークロードの beat

Phase 17.C.6 は `upgrade` ワークロードを `/chat workload=upgrade` 経由で実行する 3 つの beat を追加します。ドリフト beat と異なり、これらは `payment-demo` の Cloud Run env を変更しません — upgrade ワーカーは Contents API 経由で `demo/upgrade-target/package.json` を GitHub から読み取るため、「ベースライン」は `main` 上のリポジトリ状態であり Cloud Run env ではありません。

### 事前準備 (アップグレード beat)

```bash
# 1. コーディネーターは USE_ADK=true である必要があります (beat-c/e と同じ要件)。
gcloud run services describe driftscribe-agent \
  --project="$PROJECT" --region="$REGION" \
  --format='value(spec.template.spec.containers[0].env)' \
  | grep -oE 'USE_ADK=[a-z]+'

# 2. コーディネーターは UPGRADE_READER_URL と UPGRADE_DOCS_URL が設定されている
#    必要があります (17.E のデプロイインフラがこれらを配線します)。17.E が
#    リリースされるまでは、アップグレード beat は HTTP 503 を返し、ボディは
#    `{"detail":"workload 'upgrade' is not deployed: ..."}` となります —
#    これは Phase 17.A.3 のワークロード事前解決ガードの妥当なデモンストレーション
#    でもあります。スクリプトはそのボディを呑み込まずに表面化します。
gcloud run services describe driftscribe-agent \
  --project="$PROJECT" --region="$REGION" \
  --format='value(spec.template.spec.containers[0].env)' \
  | grep -oE 'UPGRADE_(READER|DOCS)_URL=[^,]+'

# 3. `main` 上の demo/upgrade-target/package.json はデモのベースライン
#    (lodash@4.17.20) である必要があります。このピンは意図的なものです —
#    バージョンを上げないでください。demo/upgrade-target/README.md を参照。
git show main:demo/upgrade-target/package.json | grep '"lodash"'
# 期待値: "lodash": "4.17.20"
```

### upgrade-a — 依存関係の発見 (読み取り専用)

```bash
./scripts/demo.sh upgrade-a
```

期待されるエージェントのツール呼び出し:
- `upgrade_read_dependencies` (引数なし; `target_repo` と `lockfile_path` は `UPGRADE_TARGET_REGISTRY["phase17_demo"]` からサーバー側で導出)。

期待されるレスポンスボディ: `demo/upgrade-target/package.json` の依存関係を要約する自由形式のテキスト。`lodash@4.17.20` と一致するアドバイザリ `GHSA-35jh-r3h4-6jhm` (CVE-2021-23337) を明示します。プロンプトはアクションを提案しないようにエージェントに指示する — そのため PR は開かれず、notify も発火しません。レスポンス中のアクションラベルは `no_op` または記述的な要約のいずれかになります。目視で確認してください。

見せ場: LLM は `target_repo` や `lockfile_path` を一切目にしません — これらは `agent/workloads/registry.py::UPGRADE_TARGET_REGISTRY` でピンされています (権限はコードにあり、YAML やプロンプトには存在しない)。

### upgrade-b — アップグレード PR の提案 (LIVE)

```bash
CONFIRM_UPGRADE_PR=1 ./scripts/demo.sh upgrade-b
```

**警告: これは `$GITHUB_REPO` (デフォルト `adi-prasetyo/driftscribe`) に実際の pull request を開きます。** スクリプトは毎回の呼び出しで `CONFIRM_UPGRADE_PR=1` を必須にしています — 設定しないと beat は発火を拒否し、ステータス 2 と再アームの方法を説明するメッセージを表示して終了します。設計上、env var がオペレータのシェルに残っていない限り、シェル履歴からコマンドを貼り付けるだけでは beat を再実行できません。

期待されるエージェントのツール呼び出しシーケンス:
1. `upgrade_read_dependencies` — アドバイザリの確認。
2. `search_developer_docs` — chat プロンプトの引用ルール (`workloads/upgrade/chat_system_prompt.md`) に従う。
3. `upgrade_propose_pr` 引数:
   - `package_name="lodash"`,
   - `target_version="4.17.21"`,
   - `advisory_url="https://github.com/advisories/GHSA-35jh-r3h4-6jhm"`,
   - `body=<アドバイザリ + developer-docs 結果を引用した本文>`。
   upgrade-docs ワーカーの post-LLM バリデーター (Phase 17.C.3a) は、バンプが patch/minor レベルかつ lockfile パスがピンされた `demo/upgrade-target/package.json` であることを確認します。両方とも通過します。
4. `notify` (alert チャンネル)。

期待されるレスポンスボディ (自由形式のテキスト): `$GITHUB_REPO` 上の PR URL を含み、notify 呼び出しを確認するもの。

見せ場: `upgrade_propose_pr` は authority-clean です — LLM はパッケージ名、ターゲットバージョン、アドバイザリ URL、本文の散文のみを選びました。リポジトリ / lockfile パス / ブランチ / base / PR タイトルはサーバー側で導出されます。PR を別のリポジトリにリダイレクトしようとするプロンプトインジェクションは、それらのフィールドがレジストリから出ないため成功しません。

#### upgrade-b の後処理

開かれた PR は実際の GitHub PR です。録画後にクリーンアップしてください:

```bash
# 1. PR をクローズします (<N> をエージェントのレスポンスの PR 番号に置き換える)。
gh pr close <N> --delete-branch --repo "$GITHUB_REPO"
```

`gh` のローカル認証が切れている場合は、PR ページの GitHub Web UI からクローズ + ブランチ削除を行います。ブランチ名は `upgrade/<package>-<バージョンのドットをハイフンに置換>` のパターンに従います — upgrade-b では具体的に `upgrade/lodash-4-17-21` となります (導出ルールは `agent/adk_tools.py::upgrade_propose_pr_tool` を参照)。`main` に影響せず安全に削除できます。

注意: PR をクローズせずに `upgrade-b` を再実行すると、同じブランチ名で衝突します (`upgrade/lodash-4-17-21` は `package_name` + `target_version` から決定論的に導出される)。ワーカーは PyGithub のエラーをエージェントのレスポンスに表面化します — これは Q&A 中にデモンストレーションする価値のある正当な失敗モードですが、通常のデモフローでは先に直前の PR をクローズしてください。

### upgrade-c — major-bump によるエスカレーション (layered safety)

```bash
./scripts/demo.sh upgrade-c
```

2 つの有効な結果 — どちらも良い見せ場です:

1. **LLM がプロンプトに従う** (一般的な経路): `notify` を `channel=alert`、`severity=high`、メジャーバージョンバンプが必要でバリデーターが major bump を拒否することを説明するエスカレーション本文で呼び出します。`upgrade_propose_pr` は呼び出しません。アクションラベルは `escalation`。
2. **LLM がそれでも `upgrade_propose_pr` を試みる**: upgrade-docs ワーカーの post-LLM バリデーターが 403 を返し、reason は `"major version bump refused at validator ... agent should have routed this to the 'escalation' action"` となります。(LLM が `5.0.0` のような semver triple ではなく `"5.x"` のような形式を渡した場合、バリデーターは major-bump ルールが発火する前に parse 不能な semver で 422 を返します — これも拒否ですが、ポリシーゲートではなくスキーマゲート経由です。) エージェントは拒否をレスポンスに表面化します。

両方とも **layered safety プロパティ** をデモンストレーションします: プロンプト (またはプロンプトインジェクション) が LLM にメジャーバンプを試みるよう説得しても、ワーカー側のバリデーターが真の安全ゲートです。chat プロンプトはポリシーを文書化し、ワーカーはそれを強制します。

見せ場: バリデーターはコード側のアロウリスト (patch/minor のみ) です。YAML、システムプロンプト、プロンプトインジェクションで上書きできません。

### 録画時のバリエーション

デモをエンドツーエンドで録画する場合、ドリフト beat の後に以下の順序でアップグレード beat を実行してください:

```bash
./scripts/demo.sh upgrade-a                              # 発見; 舞台を整える
CONFIRM_UPGRADE_PR=1 ./scripts/demo.sh upgrade-b         # クライマックス — 実際の PR が開く
./scripts/demo.sh upgrade-c                              # 安全性の物語 — バリデーターが major を拒否
```

または一括で実行します (`cleanup` ステップなし — アップグレードにはリセット対象の Cloud Run env がありません):

```bash
CONFIRM_UPGRADE_PR=1 ./scripts/demo.sh all-upgrade
```

`CONFIRM_UPGRADE_PR=1` は `upgrade-b` と `all-upgrade` の両方に必須です (`all-upgrade` は内部で `upgrade-b` を実行するため)。設定なしで実行すると `upgrade-b` が発火を拒否 (exit 2) し、一括実行はそこで停止 — PR は開かれません。

`all-upgrade` は意図的に `all` と分離されています — ドリフト専用の録画でアップグレード PR を誤って開くことを防ぐためです。

## Trace ID によるログ検索

すべてのレスポンスは `X-Trace-Id` ヘッダーを保持しています (Phase 15.2 のミドルウェア)。スクリプトはボディの前の専用行に出力するので、Q&A 中に Cloud Logging へコピーできます:

```bash
gcloud logging read 'jsonPayload.trace_id="<id>"' \
  --project=$PROJECT --limit=20 --format=json
```

これはリクエスト処理中に発行されたすべてのログ行を返します — コーディネーターのツール呼び出し、ワーカーへのリクエスト、ワーカー自身のログ (すべてのワーカーは `agent/worker_client.py` 経由で `X-Trace-Id` として同じ trace ID を伝搬します)。これが「監査証跡」の見せ場です: 1 つの ID で全体の流れがわかります。

録画する場合は、事前に Cloud Logging のタブを開いておき、最後に beat-e の trace ID をフィルタに貼り付けます — 「AI が何をしたか監査できる」を最も強く印象付ける視覚的なヒットです。

## Beat E の HITL フロー

beat-e は `approval_url` を返します。ランナーはこれをヘッドレスにクリックできません — そこが設計上の human-in-the-loop ポイントです。デモ中は:

1. `./scripts/demo.sh beat-e` を実行します。
2. ターミナル出力中の `approval_url` フィールドがリンクです。
3. それをコピーしてブラウザに貼り付けます (または URL を認識するターミナルでクリックします)。
4. `agent/templates/approval.html` のページに着地します — 「Rollback payment-demo to revision X?」と **Approve** / **Reject** ボタンを表示するシングルページのフォームです。
5. **Approve** をクリックします。ページはコーディネーターに POST を返し、コーディネーターは rollback ワーカーの `/execute` エンドポイントを呼び出し、対象のリビジョンを pin します。
6. ページは実行結果を再描画します。

承認トークン (URL の `?t=...`) は 15 分後に失効します (`agent/main.py` の `_cached_rollback_is_expired` を参照)。beat-e からクリックまでの間にデモを止めないでください。

## 復旧手順

beat がハングするか、想定された意図的失敗 (例: USE_ADK=false での beat-e → 503) ではない 5xx を返した場合:

1. 実行中のコマンドを **Ctrl-C** します。
2. `./scripts/demo.sh cleanup` で payment-demo のベースライン env を復元します (デモ途中の実行も安全です。コーディネーターには触れません)。
3. 失敗した beat を再試行します。

beat-b/c/d がコーディネーターから 502 を返した場合、背後のワーカーが不健全です。`driftscribe-reader`、`driftscribe-docs`、`driftscribe-rollback` の Cloud Run サービスのヘルスを確認してください。502 でも `X-Trace-Id` はログ検索に使えます — それがトリアージの起点です。

スクリプト自体が `could not resolve coordinator URL` で起動に失敗する場合、gcloud の認証が切れています:

```bash
gcloud auth login
gcloud config set project $PROJECT
```

その後、事前準備を再実行してください。
