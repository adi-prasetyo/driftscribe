# DriftScribe — ProtoPedia 提出原稿 (日本語)

> [English version](protopedia.en.md)
>
> 提出先: ProtoPedia (https://protopedia.net) — DevOps × AI Agent Hackathon 2026 (Google Cloud Japan / Findy)。各セクションはフォームの入力欄にそのまま貼り付けられる粒度で書いています。

## タイトル

DriftScribe — Cloud Run 上で安全に AI 主導の DevOps を実現するマルチエージェント・コーディネーター/ワーカーパターン

## 概要

DriftScribe は、Cloud Run 上で安全に AI 主導の DevOps を行うためのマルチエージェント・フレームワークです。単一のワークロード対応コーディネーター (Google ADK + Vertex AI Gemini 2.5 Flash) がオペレーターのリクエストをワークロード別のエージェントへルーティングし、各エージェントは自分のワークロードに許可されたツールしか見えません。本日時点で 4 つのクルーが動いています: **Anchor**（`drift`）はライブの Cloud Run サービス (`payment-demo`) を ops コントラクトと照合し、`no_op` / `docs_pr` / `drift_issue` / `rollback` / `escalation` から選択。**Patch**（`upgrade`）は npm `package.json` を GitHub Advisory DB と照合し、`no_op` / `docs_pr` / `upgrade_pr` / `escalation` から選択。**Explore**（`explore`）はライブインフラとコードを横断する読み取り専用の調査役で、ミューテーションツールをゼロしか持ちません。**Provision**（`provision`）は OpenTofu の変更を書き、ゲート付き・HMAC 署名付きの apply パイプラインを通る `iac/` のみの PR を 1 つ開きます。Anchor は自律的（Cloud Run config 変更に反応するライブ Eventarc トリガー）に、残り 3 つはチャットからのオンデマンドで動作します。推論は Google の Developer Knowledge MCP (コーディネーターにのみアタッチ) によって裏付けられます。破壊的経路は二重にガードされています。rollback は HMAC 署名付きの 1 回限りの HITL 承認、upgrade-PR は LLM 直後の決定論的バリデーター (semver、GHSA URL 形式、パス正規表現) が fail-closed で守ります。エージェントが提案し、オペレーター (またはバリデーター) が裁定し、ワーカー境界が「提案」を安全に公開できるようにします。

## ハイライト

- **ワークロード対応コーディネーター + ワークロードごとに細く絞ったワーカー**: 公開されているのはコーディネーター 1 サービスのみ。`POST /chat workload=<name>` をワークロード別のエージェント (そのワークロードのプロンプト + ツールのサブセット) にルーティングします。LLM はクロスワークロードなツールを「文字どおり一度も」見ません。ワーカーはそれぞれが独立した Cloud Run サービスで、payload-intent ポリシーをハードコード (リクエストボディは別のリポジトリ/ファイル/サービスへワーカーを向け直せない) し、コーディネーターのコードからは完全に隔離されています (`agent.*` を一切 import せず、サブプロセスベースのテストで検証)。
- **MCP による推論の裏付け**: Google の Developer Knowledge MCP はコーディネーターにのみアタッチされます (`developerknowledge.googleapis.com/mcp` への Streamable HTTP、10 秒タイムアウト、60 秒レスポンスキャッシュ、fail-closed なエラー封筒)。Anchor ワークロードは docs PR の本文で Cloud Run 環境変数の権威ガイドを引用し、Patch ワークロードはバンプ対象パッケージのマイグレーションガイドを引用します。ワーカーは MCP アクセスを持ちません。認証/ネットワーク/オブザーバビリティ面の attack surface を最小化しています。
- **ワークロードごとにスコープされた多層防御**: Layer 0 = ワークロードごとの capability-bounded ツールレジストリ。各ワークロードは自分のツールサブセットしか見えません (`YAML ⇄ コード定数 ⇄ ランタイム解決`の 3 方向の一致を pinned するテストで守られる)。Layer 1 = サービス別 IAM、ワークロードスコープ (コーディネーターの `run.invoker` 権限は、drift ワーカーから upgrade ワーカーへ「広がらない」、逆も同様)。Layer 2 = ワーカーの payload-intent ポリシー。Patch の書き込み経路には LLM 直後の決定論的バリデーター (lockfile-path 正規表現、package_name の存在、target_version > current、version_jump ∈ {patch, minor}、GHSA-URL 形式)、drift の rollback 経路には HMAC 署名付き HITL 承認が加わります。
- **二重に独立した認証境界**: オペレーター → コーディネーターは `X-DriftScribe-Token` (定数時間比較)。コーディネーター → ワーカーは audience バインドの Google ID トークンを発行し、ワーカーは audience と caller email の両方を検証します。どちらか一方が漏れても、もう一方の境界が成立しているため横展開できません。
- **コスト最適化された運用**: `min-instances=0` によりアイドル時のコストは $0、1 `/chat` 呼び出しあたり概算 $0.0003 (GCP + Gemini)。MCP は docs-PR / upgrade-PR 経路で 1 往復追加 (コーディネーター内 60 秒キャッシュ)。`X-Trace-Id` はコーディネーターからワーカーまで伝搬し、Cloud Logging で 1 リクエストを横串で追跡できます。

## 技術スタック

- 言語 / ランタイム: Python 3.12
- Web フレームワーク: FastAPI + uvicorn
- エージェントフレームワーク: Google ADK (Agent Development Kit) のワークロード対応 factory
- LLM: Gemini 2.5 Flash (Vertex AI、`GOOGLE_CLOUD_LOCATION=global`。コーディネーターが推論サマリーを受け取るため。Cloud Run 自体は asia-northeast1)
- MCP: Google Developer Knowledge MCP (Streamable HTTP)
- 実行基盤: Cloud Run × DriftScribe 10 サービス (コーディネーター + drift `reader`/`docs`/`rollback` + upgrade `upgrade-reader`/`upgrade-docs` + infra `infra-reader`/`tofu-editor`/`tofu-apply` + 共有 `notifier`)。加えてデモ 3 サービス (`payment-demo`, `storefront`, `orders-worker`)。
- データストア: Firestore (decisions, approvals, plan approvals, events, infra-graph cache)
- イベント: Eventarc (Cloud Run audit-log トリガー)
- 認証: Google ID Token (audience バインド), Secret Manager, HMAC, 1 回限りの approval token
- 通知: 外部 Webhook (デモでは webhook.site)
- ビルド / 品質: uv, ruff, pytest (3,000 件超), Cloud Build
- CI: GitHub Actions (PR と main への push で ruff + pytest)

## デモ

シナリオは 2 ワークロード合計 8 ビートで構成し、`scripts/demo.sh` から実行します。

**Workload 1 (Anchor / `drift`, 5 ビート):** beat-a はベースラインで `no_op`、beat-b で意図的にドリフトを発生させて `drift_issue` 化、beat-c で ADK エージェントが原因を推論、beat-d で docs ワーカーが PR をプレビュー、beat-e で rollback ワーカーが HITL 承認ゲートを通って復旧します。

**Workload 2 (Patch / `upgrade`, 3 ビート):** `upgrade-a` は発見ビートです。エージェントが `demo/upgrade-target/package.json` を読み、lodash 4.17.20 / GHSA-35jh-r3h4-6jhm のアドバイザリを報告します。`upgrade-b` はクライマックスビートです。エージェントが `search_developer_docs` でマイグレーションガイドを引用し、lodash を 4.17.21 に bump する**実際の** GitHub PR を提案します (`CONFIRM_UPGRADE_PR=1` 環境変数が必要; 実 PR が開く)。`upgrade-c` は安全性のビートです。エージェントが major バージョン bump を試みると、LLM 直後のバリデーターが 403 で拒否し、notifier 経由で `escalation` に振り替えます。

**Explore と Provision**（インフラ系の 2 クルー）はスクリプト化されたビートではなくチャットから対話的に実行します: Explore は Cloud Asset Inventory によるプロジェクト全体の読み取り専用インベントリを返し（何も変更しません）、Provision は `iac/` のみの OpenTofu PR を作成し、人間が承認したうえで `tofu-apply` ワーカー（ライブインフラを変更できる唯一のサービス）が適用します。

コーディネーターからワーカーまで `X-Trace-Id` が伝搬するため、すべてのワークロードで 1 リクエストの軌跡を Cloud Logging で追えるのが見どころです。

- 90 秒デモ動画: [TBD: 90 秒デモ動画]
- アーキテクチャ図: [`docs/architecture/architecture.html`](../architecture/architecture.html) (単体 HTML、2 段構成 — trigger fan-in + layered safety、ブラウザで開けます)
- デモ・ランブック: [`docs/demo-script.ja.md`](../demo-script.ja.md) (オペレーター事前準備 + 各ビートの期待値 + upgrade-b 後の後処理手順)

## スコープと今後の展望

**現在のスコープ（意図的な単一テナント構成）。** DriftScribe は 1 つの GitHub リポジトリと 1 つの Google Cloud プロジェクトに紐づいて動作します。これは意図的な判断です。マルチテナントの薄い外殻よりも、「ドリフト検知 → IaC PR を提案 → 人間が承認 → 適用」というループをエンドツーエンドで安全に動かしきることを優先しました。上述の保証（人間の承認ゲート、ワーカー間のサービスアカウントによる相互認証、自身のイメージに焼き込まれたハッシュと一致する IaC のプランしか適用しないワーカー）は、単一テナントだからこそ成立します。

**プロダクト化への道筋。** 他のユーザーが自身の GitHub・自身のクラウド上で DriftScribe を利用できるようにすることが明確な次のステップであり、顧客ごとに分離されたデプロイ、あるいは共有型のマルチテナントサービスのいずれの形でも実現可能です。マルチテナントの認証基盤とクロスプロジェクトのクラウドアクセスはセキュリティ的にデリケートな作業であり、急いで作るより正しく作るべきと考え、今回のハッカソンの範囲からは意図的に外しました。「GitHub コネクター」自体は小さな部分（全体の約 10〜15%）にすぎず、本質的な作業はテナントごとのクラウドアクセス・認証・データ分離です。単一テナント結合の全体マップとプロダクト化の各経路は `docs/plans/2026-06-24-multi-tenant-productization-scope.md` (英語) にまとめています。

## リポジトリ

https://github.com/adi-prasetyo/driftscribe

## デプロイ済み URL

- オペレーター UI / コーディネーター (`driftscribe-agent`, 公開): <https://driftscribe.adp-app.com> (Cloudflare Access の背後、審査期間中は匿名でも閲覧可)
- drift ワーカー (非公開): `driftscribe-reader`, `driftscribe-docs`, `driftscribe-rollback`
- upgrade ワーカー (非公開): `driftscribe-upgrade-reader`, `driftscribe-upgrade-docs`
- infra ワーカー (非公開): `driftscribe-infra-reader`, `driftscribe-tofu-editor`, `driftscribe-tofu-apply` (ライブインフラを変更できる唯一のサービス)
- 共有 (非公開): `driftscribe-notifier`
- デモサービス: `payment-demo` (drift ターゲット)、加えて `storefront` + `orders-worker` (DriftScribe 自身の author → approve → apply ループでプロビジョニングしたチェックアウトデモ)
- デモ用 upgrade ターゲット: `demo/upgrade-target/package.json` (lodash@4.17.20 にピン留め)

> 非公開のワーカー 9 つは `--no-allow-unauthenticated` でデプロイされており、コーディネーターのサービスアカウントが発行する audience バインド ID トークンからのみ到達できます。コーディネーターの `run.invoker` 権限はワークロードスコープで、drift ワーカーから upgrade ワーカーへ「広がらない」 (逆も同様)。これがフレームワーク Layer 1 の本質です。
