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
