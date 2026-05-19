# DriftScribe
> [English version](README.md)

[![CI](https://github.com/adi-prasetyo/driftscribe/actions/workflows/ci.yml/badge.svg)](https://github.com/adi-prasetyo/driftscribe/actions/workflows/ci.yml)

Cloud Run のライブ環境ドリフトを検知する AI DevOps エージェント。DevOps × AI Agent Hackathon 2026 (Google Cloud Japan / Findy) への提出作品です。

**アーキテクチャ図:** [`docs/architecture/architecture.html`](docs/architecture/architecture.html) — 単体ファイルで完結しており、ブラウザで開けます (英語版のみ)。

## 概要

DriftScribe はライブの Cloud Run サービス (`payment-demo`) を監視し、稼働中の環境を宣言された ops コントラクトと照合します。ドリフトが検出されると、ADK ベースのエージェントが 4 つの結果 — `no_op`、`docs_pr`、`rollback`、`escalate` — から選択し、実行専用のワーカーに処理を委譲します。破壊的な操作 (rollback) は、HMAC 署名付きのワンショット URL による人間の承認を必要とします。Cloud Run サービスは合計 5 つ: コーディネーター 1 つ + ワーカー 4 つです。

## デモ

```bash
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh beat-a   # baseline → no_op
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh beat-b   # drift → drift_issue
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh beat-c   # ADK reasoning beat
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh beat-d   # docs PR preview
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh beat-e   # rollback w/ HITL gate
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh cleanup  # restore baseline
```

オペレーター向けの完全なランブック (画面レイアウト、タイミング、期待される出力):
[`docs/demo-script.md`](docs/demo-script.md) (日本語版: [`docs/demo-script.ja.md`](docs/demo-script.ja.md))。

## 仕組み

コーディネーター (`driftscribe-agent`) は唯一の公開サービスです。ADK エージェントループ、インテント分類器、承認用の HMAC ページをホストします。実行専用のワーカー 4 つ (`reader`、`docs`、`rollback`、`notifier`) は `--no-allow-unauthenticated` の背後にあり、人間からの直接アクセスをすべて拒否します — 到達できるのはコーディネーターのサービスアカウントが発行する、audience バインドされた Google ID トークンのみです。各ワーカーはハードコードされた payload-intent ポリシーを強制します: リクエストボディが別のターゲットサービス、リポジトリパス、または webhook URL へのリダイレクトを指示することはできません。

トリガーは 3 方向から集約されます: Eventarc の監査ログイベント、オペレーター駆動の `/chat` 自然言語リクエスト、手動の `/recheck` 呼び出し。完全なトポロジーと、重ならない 2 層の認証については [`docs/architecture/multi-agent-design.md`](docs/architecture/multi-agent-design.md) に記載しています。

## コストとレイテンシ

`/chat` 1 呼び出しあたり: ~$0.0002 GCP + ~$0.0001 Gemini = ~$0.0003 (見積もり、下記の 20 回ベンチマークで検証)。p50 レイテンシ: classifier 経路 TBD ms、ADK 経路 TBD ms。p95: TBD ms。`min-instances=0` でのアイドルコスト: $0。ハッカソン期間中のデモ総支出: TBD (提出前に GCP の請求内訳から取得)。

実数値を取得するには、デプロイ済みのコーディネーターに対して `/chat` を 20 回連続で呼び出し、各呼び出しの `X-Trace-Id` + ウォールクロック時間を記録し、得られた系列から p50/p95 を計算します。手順はデモランナーと併せて配置されています — リクエストの形と operator-token の解決方法は `scripts/demo.sh` を参照してください。

## 既存ツールとの比較

| | DriftScribe | Drift (CloudPosse) | Steampipe | Cloud Custodian | AWS Config Rules |
| --- | --- | --- | --- | --- | --- |
| AI による判断 | ✓ | ✗ | ✗ | ✗ | ✗ |
| HITL 承認ゲート | ✓ | ✗ | ✗ | ✗ | ✗ |
| 多層防御 (OS + ポリシー) | ✓ | ✗ | ✗ | partial | partial |
| マルチクラウド対応 | ✗ (GCP のみ) | ✓ (Terraform-aware, multi) | ✓ | ✓ (AWS-primary) | ✗ (AWS) |
| オープンソース | ✓ | ✓ | ✓ | ✓ | ✗ |
| デプロイ形態 | Cloud Run (5 svcs) | Terraform | Plugin host | Lambda | Managed service |
| 想定ユーザー | GCP 上の DevOps + SRE | IaC プラットフォームチーム | SQL に明るい運用者 | AWS 運用者 | AWS コンプライアンスチーム |

DriftScribe はマルチクラウドの幅広さを犠牲にして、単一プラットフォーム上での多層防御を選びました。ハッカソン段階の試作であり、他はプロダクション成熟済みです。賭けは、AI + HITL が欠けている軸だという点にあります — 既存ツールはドリフトの検出は得意ですが、レポートで止まるか (Drift、Steampipe)、人を介さずに自動修復するか (Custodian、Config Rules) のどちらかです。DriftScribe はその中間に位置します: エージェントが提案し、オペレーターが裁定し、ワーカー境界によって「提案」を安全に公開できるようにしています。

## リポジトリ構成

- [`agent/`](agent/) — コーディネーターサービス (ADK エージェント、分類器、承認、認証)
- [`workers/`](workers/) — 実行専用のワーカーサービス 4 つ
- [`demo/`](demo/) — `payment-demo` ターゲットサービス + ops コントラクト
- [`docs/architecture/`](docs/architecture/) — 図、マルチエージェント設計、IAM マトリクス
- [`docs/runbooks/`](docs/runbooks/) — デプロイ + 運用手順
- [`docs/plans/`](docs/plans/) — フェーズ別の実装計画
- [`scripts/`](scripts/) — デモランナー
- [`infra/`](infra/) — Cloud Build + smoke テスト
- [`tests/`](tests/) — ユニット + 統合テストスイート

## ステータス

Phase 16 (提出に向けた仕上げ) を進行中です。ハッカソンの提出締切は 2026-07-10。現在の実装計画: [`docs/plans/2026-05-19-driftscribe-v3-multi-agent.md`](docs/plans/2026-05-19-driftscribe-v3-multi-agent.md)。
