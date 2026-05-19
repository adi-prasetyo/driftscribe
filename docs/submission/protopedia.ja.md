# DriftScribe — ProtoPedia 提出原稿 (日本語)

> [English version](protopedia.en.md)
>
> 提出先: ProtoPedia (https://protopedia.net) — DevOps × AI Agent Hackathon 2026 (Google Cloud Japan / Findy)。各セクションはフォームの入力欄にそのまま貼り付けられる粒度で書いています。

## タイトル

DriftScribe — Cloud Run の構成ドリフトを検知する多層防御 AI DevOps エージェント

## 概要

DriftScribe はライブの Cloud Run サービス (`payment-demo`) を監視し、稼働中の環境を宣言された ops コントラクトと照合する AI エージェントです。ドリフトを検知すると、Google ADK で実装されたコーディネーターが `no_op` / `docs_pr` / `rollback` / `escalate` のいずれかを判断し、実行専用ワーカーへ処理を委譲します。破壊的な操作 (rollback) は HMAC 署名付きのワンショット URL による人手承認 (HITL) を必須とし、AI による提案と人による裁定を分離した「層状の安全性」を単一クラウド (GCP) 上で実現します。

## ハイライト

- **多層防御を持つマルチエージェント構成**: コーディネーター 1 つ + 実行専用ワーカー 4 つ (`reader` / `docs` / `rollback` / `notifier`) の合計 5 つの Cloud Run サービスで構成。各ワーカーはハードコードされた payload-intent ポリシーと専用サービスアカウントで分離されており、コーディネーターは `payment-demo` への変更権限を一切持ちません (ネガティブスペース設計)。
- **HITL 承認ゲート**: rollback などの破壊的操作は、HMAC 署名された使い切りリンクからオペレーターが承認しない限り実行されません。承認状態は Firestore のトランザクションでコーディネーターとワーカー双方が一度ずつ遷移させ、二重クリックやリプレイを構造的に防ぎます。
- **Google ADK + Vertex AI による意思決定ループ**: Agent Development Kit を用い、Vertex AI Gemini 2.5 Flash でドリフト原因の推論とアクション選択を行います。LLM は登録済みの 6 ツール (Layer 0 capability-bounded tool registry) からしか呼び出せないため、プロンプトインジェクションが「想定外の操作」へ波及しません。
- **二重に独立した認証境界**: オペレーター → コーディネーターは `X-DriftScribe-Token` (定数時間比較)、コーディネーター → ワーカーは audience バインドの Google ID トークン。どちらか一方が漏れても、もう一方の境界が成立しているため横展開できません。
- **コスト最適化された運用**: `min-instances=0` によりアイドル時のコストは $0、1 呼び出しあたり概算 $0.0003 (GCP + Gemini)。トレース ID (`X-Trace-Id`) はコーディネーターからワーカーまで伝搬し、Cloud Logging で 1 リクエストを横串で追跡できます。

## 技術スタック

- 言語 / ランタイム: Python 3.12
- Web フレームワーク: FastAPI + uvicorn
- エージェントフレームワーク: Google ADK (Agent Development Kit)
- LLM: Gemini 2.5 Flash (Vertex AI, asia-northeast1)
- 実行基盤: Cloud Run × 5 サービス (asia-northeast1)
- データストア: Firestore (decisions, approvals)
- イベント: Eventarc (Cloud Run audit-log トリガー)
- 認証: Google ID Token (audience バインド), Secret Manager, HMAC
- 通知: 外部 Webhook (デモでは webhook.site)
- ビルド / 品質: uv, ruff, pytest, Cloud Build
- CI: GitHub Actions (PR と main への push で ruff + pytest)

## デモ

90 秒のシナリオを 5 つのビート (beat-a 〜 beat-e) で構成し、`scripts/demo.sh` から順に実行します。beat-a はベースラインで `no_op`、beat-b で意図的にドリフトを発生させて `drift_issue` 化、beat-c で ADK エージェントが原因を推論、beat-d で docs ワーカーが PR をプレビュー、beat-e で rollback ワーカーが HITL 承認ゲートを通って復旧します。コーディネーターからワーカーまで `X-Trace-Id` が伝搬するため、Cloud Logging で 1 リクエストの軌跡を追えるのが見どころです。

- 90 秒デモ動画: [TBD: 90 秒デモ動画]
- アーキテクチャ図: [`docs/architecture/architecture.html`](../architecture/architecture.html) (単体 HTML、ブラウザで開けます)
- デモ・ランブック: [`docs/demo-script.ja.md`](../demo-script.ja.md)

## リポジトリ

https://github.com/adi-prasetyo/driftscribe

## デプロイ済み URL

- Coordinator (`driftscribe-agent`, 公開): [TBD after deploy: https://driftscribe-agent-xxxxx-an.a.run.app]
- Reader (`driftscribe-reader`, 非公開): [TBD after deploy]
- Docs (`driftscribe-docs`, 非公開): [TBD after deploy]
- Rollback (`driftscribe-rollback`, 非公開): [TBD after deploy]
- Notifier (`driftscribe-notifier`, 非公開): [TBD after deploy]
- 監視対象サービス (`payment-demo`, デモ用): [TBD after deploy]

> 非公開のワーカー 4 つは `--no-allow-unauthenticated` でデプロイされており、コーディネーターのサービスアカウントが発行する audience バインド ID トークンからのみ到達できます。
