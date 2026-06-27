# DriftScribe

**エージェントが提案し、人間が承認する。**

> [English version](README.md)

[![CI](https://github.com/adi-prasetyo/driftscribe/actions/workflows/ci.yml/badge.svg)](https://github.com/adi-prasetyo/driftscribe/actions/workflows/ci.yml)
[![E2E](https://github.com/adi-prasetyo/driftscribe/actions/workflows/e2e.yml/badge.svg?event=workflow_dispatch)](https://github.com/adi-prasetyo/driftscribe/actions/workflows/e2e.yml)

**Google Cloud 上のインフラを監視し、修正を提案する AI DevOps エージェント。
ただし、リスクのある変更を自分の判断だけで適用することは決してありません。**
4 つのクルーが動いており、それぞれが自分の担当範囲ではすべてを読み取れますが、
変更を適用するのではなく提案にとどまり、各クルーは右端の安全境界に従います:

| クルー | トリガー | 担当範囲 | 安全境界 |
| --- | --- | --- | --- |
| **Anchor**（`drift`） | 自律 (Eventarc) | Cloud Run のライブ config と ops コントラクトの突合 → docs PR、ドリフト issue、またはロールバック | ロールバックは一回限り有効な HITL 承認の背後で待つ |
| **Patch**（`upgrade`） | オンデマンド (チャット) | npm 依存パッケージと GitHub Advisory DB の突合 → upgrade PR | メジャーバンプは決定論的バリデータが拒否 |
| **Provision**（`provision`） | オンデマンド (チャット) | `iac/` のみを変更する OpenTofu PR を作成 | ライブインフラには触れず、apply は別のゲート付きワーカー |
| **Explore**（`explore`） | オンデマンド (チャット) | プロジェクト全体の読み取り専用インベントリ。DriftScribe 自体の仕組みも説明する | ミューテーションツールはゼロ (テストで保証) |

コーディネーター (Google の Agent Development Kit 上の Gemini、Developer Knowledge
MCP による裏付け) は、自らインフラに手を下す力を持ちません。実行はハードコード
された制約の中で動く単一目的のワーカーが担い、ロールバックとライブインフラへの
apply は必ず一回限り有効な人間の承認ゲートで待ち、すべての判断は推論トレース
とともにオペレーター UI に記録されます。(Patch の自律トリガーは将来作業であり、
現時点ではチャットから起動します。`/recheck` 経路は未実装です。)

DevOps × AI Agent Hackathon 2026 (Google Cloud Japan / Findy) への提出作品です。

**ライブデモ:** <https://driftscribe.adp-app.com> — オペレーター UI。ハッカソンの
審査期間中は誰でもアクセスできます (期間外は Cloudflare Access で保護)。

**はじめての方へ:** [`docs/OVERVIEW.md`](docs/OVERVIEW.md) — システム全体を
約 10 分で読める平易な解説です (英語)。

**アーキテクチャ図:** [`docs/architecture/architecture.html`](docs/architecture/architecture.html) — 単体ファイルで完結しており、ブラウザで開けます (英語版のみ)。

## パターン

DriftScribe はどのワークロードにも共通する 4 つの不変条件 (invariants) を軸に構築されています。

- **ワークロード対応のコーディネーター。** 公開サービスは 1 つだけで、`POST /chat workload=<name>` を受け取ると、そのワークロード専用のエージェントプロンプトとツール集合へルーティングします。LLM はワークロードを跨いだツールを見ることがありません。能力 (capability) はレジストリ層だけでなく、ワークロード単位で境界が引かれます。
- **ワークロードごとに細く絞ったワーカー。** 各ワークロードは実行専用のワーカーをペア (または 3 つ組) で持ちます。ワーカーは payload-intent ポリシーをハードコードしており、リクエストボディが別のリポジトリ、ファイル、サービスにワーカーを向け直すことはできません。ワーカーのコードは `agent.*` を一切 import せず、別プロセスとして隔離されます。
- **Layer 0 / 1 / 2 の多層防御。** Layer 0: ワークロードごとに能力を絞ったツールレジストリ。Layer 1: サービスごとに分離された IAM スコープ。コーディネーターが Anchor ワーカーに対して持つ `run.invoker` 権限は、Patch ワーカーには及びません。Layer 2: 各ワーカーの payload-intent ポリシー、加えて Patch の書き込み経路には決定論的な post-LLM バリデータ (semver の形、パス regex、GHSA URL の形)、Anchor の rollback 経路には HITL 承認ゲート。
- **MCP による推論の裏付け。** Google の Developer Knowledge MCP はコーディネーターにアタッチされます。Anchor では Cloud Run の環境変数に関する公式ガイダンスを、Patch ではバンプ対象パッケージのマイグレーションガイドを引用できます。ワーカーは MCP に一切アクセスできません。推論ステップを担うコーディネーターだけが利用します。

完全なトポロジーと IAM 境界については [`docs/architecture/multi-agent-design.md`](docs/architecture/multi-agent-design.md) を参照してください。

## ワークロード

### Anchor — Cloud Run config ドリフト (`drift`)

自律的に動作します。Cloud Run config の変更に反応するライブ Eventarc トリガーが組み込まれており（ポーリングではなくイベント駆動）、チャットから呼び出すことも可能です。

- `payment-demo` Cloud Run サービスの環境変数を [`demo/ops-contract.yaml`](demo/ops-contract.yaml) と照合します。
- アクション: `no_op` / `docs_pr` / `drift_issue` / `rollback` / `escalation`。
- ワーカー: `reader` (Cloud Run の読み取り専用)、`docs` (docs PR の作成)、`rollback` (リビジョンのロールバック)、加えて共有の `notifier`。
- `rollback` には HITL 承認ゲート: HMAC 署名付きのワンショット URL、TTL 15 分、Firestore のトランザクションで単一使用を保証。コーディネーター自身はロールバックを実行せず、承認 URL の発行だけを行います。

### Patch — 依存関係アップグレード (`upgrade`)

チャットからのオンデマンドです。自律トリガーは将来作業であり、現時点ではチャットから起動します。

- [`demo/upgrade-target/package.json`](demo/upgrade-target/package.json) を GitHub Advisory DB と照合します。
- アクション: `no_op` / `docs_pr` / `upgrade_pr` / `escalation`。
- ワーカー: `upgrade-reader` (lockfile + advisory の読み取り専用)、`upgrade-docs` (upgrade PR の作成)、加えて共有の `notifier`。
- 書き込み経路に対する決定論的な post-LLM バリデータ: lockfile path の regex、`package_name` が現在の lockfile に存在すること、`target_version` が現在より新しいこと (ダウングレード不可)、バージョンジャンプが {patch, minor} のいずれかであること、`advisory_url` が `https://github.com/advisories/GHSA-...` の形であること。メジャーバンプはバリデータが拒否します。LLM 側にもメジャーは `escalation` に回すよう指示してあり、それを破った場合でもバリデータが fail-closed で防ぎます。
- PR ライフサイクルのツール (`upgrade-close-pr`、`upgrade-merge-pr`) も備えており、エージェントは自分が開いた upgrade PR をクローズしたり、CI ゲート付きでマージしたりできます。`upgrade-docs` ワーカーは実行前に適格性を再検証します (driftscribe ラベル + `upgrade/` ブランチ + base が `main` + 必須チェックが green であること)。

### Explore と Provision — インフラストラクチャ (`explore` / `provision`)

infrastructure-as-code を扱う 2 つの**チャット専用**ワークロードです (infra-IaC イニシアチブ)。どちらもオンデマンドで、自律トリガーはありません:

- **Explore**（`explore`、読み取り専用）— Cloud Asset Inventory (`infra-reader` ワーカー) によるプロジェクト全体のリソース調査に加えて、Cloud Run のライブ環境変数、ops コントラクト、依存パッケージの lockfile、開発者ドキュメントを参照します。DriftScribe 自体の仕組みを尋ねるならこのクルーです — プロンプトにシステム全体の概要を持つため、初めての人もドキュメントを読む前にチャットで全体像をつかめます。**ミューテーションツールはゼロ。** すべてを読めて、何も変更できません (この読み取り専用保証は、ツール集合がミューテーション集合と交わらないことを検証するテストで保証されています)。
- **Provision**（`provision`、インフラ編集）— チャットのリクエストから OpenTofu の変更を書き、`tofu-editor` ワーカー経由で **`iac/` のみを変更する PR を 1 つ**開きます (ワーカーは全ファイルを再検証: `iac/` プレフィックス、foundation 禁止、シークレット禁止、AGENT モードの静的ゲート)。ライブインフラには一切触れません。実際の `tofu apply` は**下流**の `tofu-apply` ワーカー (ライブインフラを変更できる唯一のサービス) が、プランに紐付いた HMAC 署名付きオペレーター承認の背後で実行します。これはチャットエージェントが直接呼び出すことはできない経路です。

どちらもチャット専用です: `/recheck` はこの 2 つを拒否します (自律的な観測ソースがないため)。オペレーター UI は、判断タイムラインと並べてライブのインフラリソースマップ (管理下 vs ドリフト) を表示します。

## デモ

```bash
# Anchor (drift): Cloud Run config ドリフト
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh beat-a   # baseline → no_op
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh beat-b   # drift → drift_issue
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh beat-c   # ADK reasoning beat
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh beat-d   # docs PR preview
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh beat-e   # rollback w/ HITL gate

# Patch (upgrade): 依存パッケージのアップグレード (upgrade-b は本物の PR を作成 — 確認ゲート必須)
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh upgrade-a              # 読み取り: 依存と advisory を列挙
PROJECT=driftscribe-hack-2026 CONFIRM_UPGRADE_PR=1 \
  ./scripts/demo.sh upgrade-b                                          # 提案: lodash 4.17.20 → 4.17.21
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh upgrade-c              # 安全側: バリデータがメジャーバンプを拒否

PROJECT=driftscribe-hack-2026 ./scripts/demo.sh cleanup                # Anchor のベースラインを復元 (Anchor のみ)
```

`upgrade-b` は呼び出すたびに `CONFIRM_UPGRADE_PR=1` を要求します。これは設定済みの
`GITHUB_REPO` に対して実際の pull request を開くためです。ゲートは一度限り
有効になる設計で、シェル履歴からの再実行だけでは（環境変数がシェルに
残っていない限り）再度 PR を開くことはできません。

オペレーター向けの完全なランブック (画面レイアウト、タイミング、期待される出力、後片付け):
[`docs/demo-script.md`](docs/demo-script.md) (日本語版: [`docs/demo-script.ja.md`](docs/demo-script.ja.md))。

## コストとレイテンシ

`/chat` 1 呼び出しあたり: ~$0.0002 GCP + ~$0.0001 Gemini = ~$0.0003 (見積もり)。
Developer Knowledge MCP の呼び出しは、`docs_pr` / `upgrade_pr` の経路で
1 ラウンドトリップ追加されます。コーディネーターは MCP の結果を 60 秒間
プロセス内でキャッシュするため、同一セッション内で同じトピックが繰り返し
出てもコストは倍にはなりません。

**レイテンシ** (実測: ライブコーディネーターに対する 20 回連続のウォームな
`/chat` 呼び出し、Explore の説明経路、固定プロンプト、ウォームアップは除外):
**p50 ≈ 3.2 秒、p95 ≈ 5.5 秒** (最小 2.2 秒、最大 5.6 秒)。これは対話的な
ADK チャット経路を対象としています。自律的なドリフト検知は `/recheck` 経路で
動作し、こちらはイベント駆動 (Eventarc) で、E2E スイートが別途検証しています。
`min-instances=0` のため、サービスがゼロまでスケールした後の最初の呼び出しは、
これらのウォーム値にコンテナ + モデルクライアントのコールドスタートが加わります。

**支出。** `min-instances=0` でのアイドルコストは $0 です。デモプロジェクトでは
BigQuery への請求エクスポートを有効化していないため、本 README では正確な
プロジェクト総額は報告しません (請求エクスポートは遡及せず、捏造した数値は
載せません)。デモの規模は小さく、`/chat` 呼び出し数十回と Cloud Build
デプロイ数回程度です。正確な数値が必要な場合は、GCP の請求コンソール →
レポートでプロジェクト `driftscribe-hack-2026` をハッカソン期間で絞り込むと
権威ある総額が得られます。

レイテンシ値を再現するには、デプロイ済みのコーディネーターに対して `/chat` を
(ウォームアップ 3 回を破棄し、同じ Explore の説明プロンプトを使って) 20 回連続で
呼び出し、各呼び出しの `X-Trace-Id` + ウォールクロック時間を記録し、得られた系列
から p50/p95 を計算します。リクエストの形と operator-token の解決方法は
[`scripts/demo.sh`](scripts/demo.sh) を参照してください。

**ログ保持期間:** Cloud Logging の `_Default` バケットは
`infra/scripts/setup_secrets.sh` によって 365 日まで延長されます。
すべての DriftScribe ログ (エージェントの思考要約、ツール呼び出しイベント、
LLM 利用量レコードを含む) は 1 年間保持され、Logs Explorer から照会可能です。
30 日を超えたストレージは $0.01/GiB-月で課金されますが、ハッカソン規模では
ほぼ無視できます。確認手順とサンプルクエリは
[`docs/runbooks/deploy.md`](docs/runbooks/deploy.md) を参照してください。

## Anchor と既存ツールの比較

下表は Anchor（`drift` ワークロード）に限定した比較です。Patch は
別カテゴリー (Dependabot / Renovate 系) なのでここでは比較していません。

| | DriftScribe (Anchor) | Drift (CloudPosse) | Steampipe | Cloud Custodian | AWS Config Rules |
| --- | --- | --- | --- | --- | --- |
| AI による判断 | ✓ | ✗ | ✗ | ✗ | ✗ |
| HITL 承認ゲート | ✓ | ✗ | ✗ | ✗ | ✗ |
| 多層防御 (OS + ポリシー) | ✓ | ✗ | ✗ | partial | partial |
| マルチクラウド対応 | ✗ (GCP のみ) | ✓ (Terraform-aware, multi) | ✓ | ✓ (AWS-primary) | ✗ (AWS) |
| オープンソース | ✓ | ✓ | ✓ | ✓ | ✗ |
| デプロイ形態 | Cloud Run (DriftScribe 10 サービス + デモ 3 サービス) | Terraform | Plugin host | Lambda | Managed service |
| 想定ユーザー | GCP 上の DevOps + SRE | IaC プラットフォームチーム | SQL に明るい運用者 | AWS 運用者 | AWS コンプライアンスチーム |

DriftScribe はマルチクラウドの幅広さを犠牲にして、単一プラットフォーム上での多層防御を選びました。
ハッカソン段階の試作であり、他はプロダクション成熟済みです。賭けは、AI + HITL が欠けている軸だという点にあります。既存ツールはドリフトの検出は得意ですが、レポートで止まるか (Drift、Steampipe)、人を介さずに自動修復するのが既定 (Custodian、Config Rules は承認フローを組み合わせること自体は可能ですが既定の中心ではありません) のどちらかです。
DriftScribe はその中間に位置します: エージェントが提案し、オペレーターが裁定し、ワーカー境界によって「提案」を安全に公開できるようにしています。

## リポジトリ構成

- [`agent/`](agent/) — コーディネーターサービス (ADK エージェント、分類器、承認、認証、MCP アタッチ、IaC オーサリング)
- [`workloads/`](workloads/) — クルーごとのマニフェスト (`drift`、`upgrade`、`explore`、`provision`): システムプロンプト、コントラクト、ツール/ワーカー/アクション一覧
- [`workers/`](workers/) — 実行専用のワーカーサービス: Anchor `reader` / `docs` / `rollback`、Patch `upgrade-reader` / `upgrade-docs`、インフラ `infra-reader` / `tofu-editor` / `tofu-apply`、加えて共有の `notifier`
- [`driftscribe_lib/`](driftscribe_lib/) — 共有ライブラリ (構造化ログ + トレース ID、GitHub ヘルパー、HCL パーサー、プラン承認スキーマ)
- [`iac/`](iac/) — エージェントが読み、書く OpenTofu (このデモ自身のインフラ)
- [`frontend/`](frontend/) — オペレーター UI (Svelte + Vite SPA、`/` で配信)
- [`demo/`](demo/) — `payment-demo` ドリフトターゲット + ops コントラクト、`upgrade-target` の npm lockfile (ピン留め)
- [`docs/`](docs/) — [`OVERVIEW.md`](docs/OVERVIEW.md) (まずはここから)、`architecture/`、`runbooks/`、`plans/`
- [`scripts/`](scripts/) — デモランナー
- [`infra/`](infra/) — Cloud Build + smoke テスト
- [`tests/`](tests/) — ユニット + 統合テストスイート

## スコープと今後の展望

**現在のスコープ。** DriftScribe は単一テナント構成で、1 つの GitHub リポジトリと
1 つの Google Cloud プロジェクトに紐づいて動作します。これは意図的な設計判断です。
マルチテナントの薄い外殻よりも、「ドリフト検知 → IaC PR を提案 → 人間が承認 →
適用」というループをエンドツーエンドで安全に動かしきることを優先しました。
単一テナントだからこそ、すべてのインフラ変更は人間の承認ゲートを通り、ワーカー
同士はサービスアカウントで相互認証し、`tofu-apply` ワーカーは自身のイメージに
焼き込まれたハッシュと一致する IaC のプランしか実行しません。

**プロダクト化への道筋。** 他のユーザーが自身の GitHub・自身のクラウド上で
DriftScribe を利用できるようにすることは明確な次のステップであり、顧客ごとに
分離されたデプロイ、あるいは共有型のマルチテナントサービスのいずれの形でも
実現可能です。マルチテナントの認証基盤とクロスプロジェクトアクセスは
セキュリティ的にデリケートな作業であり、急いで作るより正しく作るべきと考え、
コアループの完成度を優先して今回のハッカソンの範囲からは意図的に外しました。
単一テナント結合の全体マップとプロダクト化の各経路は
[`docs/plans/2026-06-24-multi-tenant-productization-scope.md`](docs/plans/2026-06-24-multi-tenant-productization-scope.md)
(英語) にまとめています。

## ステータス

ハッカソンの MVP を超えて作り込みが進んでいます。Phase 17 のマルチエージェント
フレームワークの上に、3 つのイニシアチブが載っています:

- **Infra-IaC エージェント** — プロジェクト全体のインベントリリーダー
  (`infra-reader`、Cloud Asset Inventory)、`tofu-editor` ワーカーによる
  エージェント主導の OpenTofu オーサリング、そしてプランに紐付いた HMAC 署名付き
  承認の背後にあるゲート付き `tofu-apply` ワーカー (ライブインフラを変更できる
  唯一のサービス)。Explore と Provision が読み取り側と
  オーサリング側を公開します。DriftScribe はこのパイプライン
  (author → approve → apply) を自分自身で駆動して、チェックアウトデモ
  (`storefront` + `orders-worker`) をプロビジョニングしました。
- **オペレーター UI** — Svelte + Vite SPA として再構築され、サイトルート `/` で
  配信されます (オペレータートークンが必要)。ライブのインフラリソースマップ
  (管理下 vs ドリフト) と、判断ごとのトレース + 環境差分ビューを備えます。
- **マルチターンチャット + チームメモリ** — 各ワークロードとのオペレーターの
  チャットが永続化され、オペレーター UI の履歴レールから再開できます。各ワーク
  ロードは、共有された読み取り専用の「チームメモリ」として、他のワークロードの
  最近の会話を読むこともできます (会話テキストは秘匿情報がマスクされ、スニペット
  に切り詰められます)。あるワークロードへの質問が、他のワークロードの参考に
  なります。

この土台は Phase 20 (アサーション付き E2E スイート — Anchor は `/recheck` 経由、
Patch は GitHub ブランチ観測経由、HITL は明示的なリビジョンキャプチャを
伴う form-POST フロー、UI は安定した `data-testid` セレクタ上の Playwright —
専用の `driftscribe-e2e` GCP プロジェクトで WIF + Required-reviewer ゲートの
もとに実行)、Phase 19.B (透明性 UI)、Phase 18.A (Cloud Logging 365 日保持)、
Phase 17 (マルチエージェントフレームワーク) です。ハッカソンの提出締切は
2026-07-10。

実装計画は [`docs/plans/`](docs/plans/) にあります (日付つき、新しいものが後ろ)。
E2E ランブック: [`docs/runbooks/e2e-environment.md`](docs/runbooks/e2e-environment.md)
(プロジェクト + シークレット + cloudbuild) と
[`docs/runbooks/e2e-ci.md`](docs/runbooks/e2e-ci.md)
(WIF + GitHub Environment)。

オペレーター UI: `/` (コーディネーターのルート; オペレータートークンが必要)。ウォークスルーは [`docs/demo-script.ja.md`](docs/demo-script.ja.md#透明性-ui-ウォークスルー) を参照。
