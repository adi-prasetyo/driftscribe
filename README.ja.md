# DriftScribe
> [English version](README.md)

[![CI](https://github.com/adi-prasetyo/driftscribe/actions/workflows/ci.yml/badge.svg)](https://github.com/adi-prasetyo/driftscribe/actions/workflows/ci.yml)

Cloud Run 上で安全に AI 駆動 DevOps を行うための、マルチエージェント
コーディネーター/ワーカーパターンです。デモワークロードは現在 2 つ:
ライブのドリフト検知 (`payment-demo` Cloud Run の環境変数 vs ops コントラクト) と、
依存パッケージのアップグレードレビュー (npm `package.json` vs GitHub Advisory DB) です。
どちらの推論ループも Google の Developer Knowledge MCP によって裏付けされます。
DevOps × AI Agent Hackathon 2026 (Google Cloud Japan / Findy) への提出作品です。

**アーキテクチャ図:** [`docs/architecture/architecture.html`](docs/architecture/architecture.html) — 単体ファイルで完結しており、ブラウザで開けます (英語版のみ)。

## パターン

DriftScribe はどのワークロードにも共通する 4 つの不変条件 (invariants) を軸に構築されています。

- **ワークロード対応のコーディネーター。** 公開サービスは 1 つだけで、`POST /chat workload=<name>` を受け取ると、そのワークロード専用のエージェントプロンプトとツール集合へルーティングします。LLM はワークロードを跨いだツールを見ることがありません — 能力 (capability) はレジストリ層だけでなく、ワークロード単位で境界が引かれます。
- **ワークロードごとに細く絞ったワーカー。** 各ワークロードは実行専用のワーカーをペア (または 3 つ組) で持ちます。ワーカーは payload-intent ポリシーをハードコードしており、リクエストボディが別のリポジトリ、ファイル、サービスにワーカーを向け直すことはできません。ワーカーのコードは `agent.*` を一切 import せず、別プロセスとして隔離されます。
- **Layer 0 / 1 / 2 の多層防御。** Layer 0: ワークロードごとに能力を絞ったツールレジストリ。Layer 1: サービスごとに分離された IAM スコープ — コーディネーターがドリフトワーカーに対して持つ `run.invoker` 権限は、アップグレードワーカーには及びません。Layer 2: 各ワーカーの payload-intent ポリシー、加えてアップグレードの書き込み経路には決定論的な post-LLM バリデータ (semver の形、パス regex、GHSA URL の形)、ドリフトの rollback 経路には HITL 承認ゲート。
- **MCP による推論の裏付け。** Google の Developer Knowledge MCP はコーディネーターにアタッチされます。ドリフトワークロードでは Cloud Run の環境変数に関する公式ガイダンスを、アップグレードワークロードではバンプ対象パッケージのマイグレーションガイドを引用できます。ワーカーは MCP に一切アクセスできません — 推論ステップを担うコーディネーターだけが利用します。

完全なトポロジーと IAM 境界については [`docs/architecture/multi-agent-design.md`](docs/architecture/multi-agent-design.md) を参照してください。

## ワークロード

### Workload 1: ドリフト検知

- `payment-demo` Cloud Run サービスの環境変数を [`demo/ops-contract.yaml`](demo/ops-contract.yaml) と照合します。
- アクション: `no_op` / `docs_pr` / `drift_issue` / `rollback` / `escalation`。
- ワーカー: `reader` (Cloud Run の読み取り専用)、`docs` (docs PR の作成)、`rollback` (リビジョンのロールバック)、加えて共有の `notifier`。
- `rollback` には HITL 承認ゲート: HMAC 署名付きのワンショット URL、TTL 15 分、Firestore のトランザクションで単一使用を保証。コーディネーター自身はロールバックを実行せず、承認 URL の発行だけを行います。

### Workload 2: 依存パッケージのアップグレード

- [`demo/upgrade-target/package.json`](demo/upgrade-target/package.json) を GitHub Advisory DB と照合します。
- アクション: `no_op` / `docs_pr` / `upgrade_pr` / `escalation`。
- ワーカー: `upgrade-reader` (lockfile + advisory の読み取り専用)、`upgrade-docs` (upgrade PR の作成)、加えて共有の `notifier`。
- 書き込み経路に対する決定論的な post-LLM バリデータ: lockfile path の regex、`package_name` が現在の lockfile に存在すること、`target_version` が現在より新しいこと (ダウングレード不可)、バージョンジャンプが {patch, minor} のいずれかであること、`advisory_url` が `https://github.com/advisories/GHSA-...` の形であること。メジャーバンプはバリデータが拒否します — LLM 側にもメジャーは `escalation` に回すよう指示してあり、それを破った場合でもバリデータが fail-closed で防ぎます。

## デモ

```bash
# Workload 1: ドリフト
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh beat-a   # baseline → no_op
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh beat-b   # drift → drift_issue
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh beat-c   # ADK reasoning beat
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh beat-d   # docs PR preview
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh beat-e   # rollback w/ HITL gate

# Workload 2: アップグレード (upgrade-b は本物の PR を作成 — 確認ゲート必須)
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh upgrade-a              # 読み取り: 依存と advisory を列挙
PROJECT=driftscribe-hack-2026 CONFIRM_UPGRADE_PR=1 \
  ./scripts/demo.sh upgrade-b                                          # 提案: lodash 4.17.20 → 4.17.21
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh upgrade-c              # 安全側: バリデータがメジャーバンプを拒否

PROJECT=driftscribe-hack-2026 ./scripts/demo.sh cleanup                # ドリフトのベースラインを復元 (ドリフトのみ)
```

`upgrade-b` は呼び出すたびに `CONFIRM_UPGRADE_PR=1` を要求します。これは設定済みの
`GITHUB_REPO` に対して実際の pull request を開くためです。ゲートは一度限り
有効になる設計で、シェル履歴からの再実行だけでは — 環境変数がシェルに
残っていない限り — 再度 PR を開くことはできません。

オペレーター向けの完全なランブック (画面レイアウト、タイミング、期待される出力、後片付け):
[`docs/demo-script.md`](docs/demo-script.md) (日本語版: [`docs/demo-script.ja.md`](docs/demo-script.ja.md))。

## コストとレイテンシ

`/chat` 1 呼び出しあたり: ~$0.0002 GCP + ~$0.0001 Gemini = ~$0.0003 (見積もり、
下記の 20 回ベンチマークで検証)。Developer Knowledge MCP の呼び出しは、
`docs_pr` / `upgrade_pr` の経路で 1 ラウンドトリップ追加されます。
コーディネーターは MCP の結果を 60 秒間プロセス内でキャッシュするため、
同一セッション内で同じトピックが繰り返し出てもコストは倍にはなりません。
p50 レイテンシ: classifier 経路 TBD ms、ADK 経路 TBD ms。p95: TBD ms。
`min-instances=0` でのアイドルコスト: $0。ハッカソン期間中のデモ総支出: TBD
(提出前に GCP の請求内訳から取得)。

実数値を取得するには、デプロイ済みのコーディネーターに対して `/chat` を 20 回連続で呼び出し、
各呼び出しの `X-Trace-Id` + ウォールクロック時間を記録し、得られた系列から p50/p95 を計算します。
手順はデモランナーと併せて配置されています — リクエストの形と operator-token の解決方法は
[`scripts/demo.sh`](scripts/demo.sh) を参照してください。

**ログ保持期間:** Cloud Logging の `_Default` バケットは
`infra/scripts/setup_secrets.sh` によって 365 日まで延長されます。
すべての DriftScribe ログ (エージェントの思考要約、ツール呼び出しイベント、
LLM 利用量レコードを含む) は 1 年間保持され、Logs Explorer から照会可能です。
30 日を超えたストレージは $0.01/GiB-月で課金されますが、ハッカソン規模では
ほぼ無視できます。確認手順とサンプルクエリは
[`docs/runbooks/deploy.md`](docs/runbooks/deploy.md) を参照してください。

## ドリフトワークロードと既存ツールの比較

下表は Workload 1 (ドリフト) に限定した比較です。アップグレードワークロードは
別カテゴリー (Dependabot / Renovate 系) なのでここでは比較していません。

| | DriftScribe (Workload 1) | Drift (CloudPosse) | Steampipe | Cloud Custodian | AWS Config Rules |
| --- | --- | --- | --- | --- | --- |
| AI による判断 | ✓ | ✗ | ✗ | ✗ | ✗ |
| HITL 承認ゲート | ✓ | ✗ | ✗ | ✗ | ✗ |
| 多層防御 (OS + ポリシー) | ✓ | ✗ | ✗ | partial | partial |
| マルチクラウド対応 | ✗ (GCP のみ) | ✓ (Terraform-aware, multi) | ✓ | ✓ (AWS-primary) | ✗ (AWS) |
| オープンソース | ✓ | ✓ | ✓ | ✓ | ✗ |
| デプロイ形態 | Cloud Run (Phase 17 完了後: DriftScribe 7 サービス + デモターゲット 1) | Terraform | Plugin host | Lambda | Managed service |
| 想定ユーザー | GCP 上の DevOps + SRE | IaC プラットフォームチーム | SQL に明るい運用者 | AWS 運用者 | AWS コンプライアンスチーム |

DriftScribe はマルチクラウドの幅広さを犠牲にして、単一プラットフォーム上での多層防御を選びました。
ハッカソン段階の試作であり、他はプロダクション成熟済みです。賭けは、AI + HITL が欠けている軸だという点にあります — 既存ツールはドリフトの検出は得意ですが、レポートで止まるか (Drift、Steampipe)、人を介さずに自動修復するのが既定 (Custodian、Config Rules は承認フローを組み合わせること自体は可能ですが既定の中心ではありません) のどちらかです。
DriftScribe はその中間に位置します: エージェントが提案し、オペレーターが裁定し、ワーカー境界によって「提案」を安全に公開できるようにしています。

## リポジトリ構成

- [`agent/`](agent/) — コーディネーターサービス (ADK エージェント、分類器、承認、認証、MCP アタッチ)
- [`workloads/`](workloads/) — ワークロードごとのマニフェスト (drift、upgrade): システムプロンプト、コントラクト、アクション一覧
- [`workers/`](workers/) — 実行専用のワーカーサービス: ワークロード別のワーカーセット (drift `reader` / `docs` / `rollback`、upgrade `upgrade-reader` / `upgrade-docs`) と共有の `notifier`
- [`demo/`](demo/) — `payment-demo` ドリフトターゲット + ops コントラクト、`upgrade-target` の npm lockfile (ピン留め)
- [`docs/architecture/`](docs/architecture/) — 図、マルチエージェント設計、IAM マトリクス
- [`docs/runbooks/`](docs/runbooks/) — デプロイ + 運用手順
- [`docs/plans/`](docs/plans/) — フェーズ別の実装計画
- [`scripts/`](scripts/) — デモランナー
- [`infra/`](infra/) — Cloud Build + smoke テスト
- [`tests/`](tests/) — ユニット + 統合テストスイート

## ステータス

Phase 17 (マルチエージェントフレームワーク + Developer Knowledge MCP) 完了。
ハッカソンの提出締切は 2026-07-10。現在の実装計画:
[`docs/plans/2026-05-19-driftscribe-phase17-framework-mcp.md`](docs/plans/2026-05-19-driftscribe-phase17-framework-mcp.md)。
