# DriftScribe i18n glossary (用語集) — EN → JA canonical terms

**Purpose:** one canonical Japanese rendering per domain term so wording is
**identical across the whole UI and every explanation**. Every fan-out agent MUST
use these renderings; do not invent synonyms. If a term is missing, add it here
(and flag it) rather than guessing per-file.

Status: **FINALIZED after Codex terminology review** (2026-07-11, thread
`019f4fce…`). The five most-important canonical choices: `IaC 管理率`, `拒否リスト`,
`自律動作レベル`, `運用管理`, `エージェントチーム`.

## Do NOT translate (proper nouns / product / tech identifiers)

DriftScribe · Anchor · Patch · Provision · Explore (crew names) · GitHub ·
Cloud Run · GCP · Google · Pub/Sub · Eventarc · OpenTofu · Terraform · Vertex ·
MCP · IaC · PR (abbreviation) · Firestore · Cloud Build · SHA · JSON/YAML/`.tf`

Crew names stay Latin. Keep PR numbers, SHAs, resource ids, tool `__name__`s,
enum/code values verbatim. Retain raw `apply` ONLY when naming a literal command,
enum, or code value — in prose, `apply` → `適用`.

## Core domain terms

| EN | JA (canonical) | Notes |
|----|----------------|-------|
| drift | ドリフト | Katakana (standard in JP infra-ops). First/anchor use: `ドリフト（IaC の定義と実環境のずれ）`. |
| adopt / adoption | IaC 管理に取り込む / IaC 管理への取り込み | Generate config + import an untracked live resource. NOT 採用, NOT インポート (broader than `terraform import`). Compact Adopt button may read `取り込む` with the full phrase in its aria-label. |
| approve / approval | 承認 / 承認する | |
| approver | 承認者 | |
| reject | 却下 / 却下する | Non-binding no-op in this app. |
| propose / proposal | 提案 / 提案する | |
| apply (IaC) | 適用 / 適用する | `applied` → 適用済み. |
| plan (IaC plan) | IaC プラン → プラン | `IaC プラン` on first mention in a surface, `プラン` thereafter. |
| coverage | IaC 管理率 | % of supported resources under IaC. NOT カバレッジ. |
| rollback | ロールバック | |
| denylist | 拒否リスト | Denied *operations* (counterpart of 許可リスト). NOT 禁止リスト. |
| autonomy (the dial) | 自律動作レベル | Operator-controlled ceiling on agent behavior. NOT 自律性 / 自動化レベル. Usage: `自律動作レベル：提案`. |
| — Observe (mode) | 監視のみ | No changes. (Put "reporting" in the explanation, not the label.) |
| — Propose (mode) | 提案 | May prepare reviewable changes. |
| — Propose + Apply (mode) | 提案＋適用 | May complete permitted changes. |
| provision (verb) | プロビジョニングする | Provision crew keeps its Latin name. |
| superseded | 置き換え済み | `#N に置き換え済み`. |
| steward / stewardship | 運用管理 / 運用管理を担う | create→guard→maintain→explain lifecycle. NOT 保守, NOT スチュワード. |
| managed (status/pill) | IaC 管理済み | |
| under IaC management (prose) | IaC 管理下 | |
| unmanaged | IaC 未管理 | |
| live environment | 実環境 | |
| currently running infrastructure | 稼働中のインフラ | |
| infrastructure | インフラ | |
| IaC | IaC | Anchor-use gloss: `コードで管理するインフラ (IaC)`. |
| resource | リソース | |
| resource-type labels | `infra.type.*` カタログ | Backend `_TYPE_LABELS` (Storage bucket, Pub/Sub topic, …) localize via `infra_graph.infraTypeLabel` (ストレージバケット, Pub/Sub トピック, …); unknown future types pass through untranslated. |
| IaC declaration / declared in IaC | （IaC の）定義 / IaC に定義済み | An HCL resource definition. NOT 宣言. (The `ops-contract.yaml` desired-state contract may keep 宣言された.) |
| planned-change verbs (PR-preview ghosts) | 作成予定・変更予定・削除予定 | Legend AND node badges share the 〜予定 form (matches `{n}件を作成予定`). NOT 〜されます. |
| configuration / config | 設定 / 構成 | 設定 for env-var config; 構成 for structural. |
| migration (ClickOps→IaC) | 移行 | |

## Actors / system parts

| EN | JA | Notes |
|----|----|-------|
| crew | エージェントチーム | The collective (Anchor/Patch/…). A single crew = one エージェントチーム. NOT クルー / チーム alone. |
| workload | ワークロード | The API contract value behind a crew. |
| operator | オペレーター | The human using the app. |
| coordinator | コーディネーター | The LLM that orchestrates. |
| worker | ワーカー | A sub-agent/tool executor. |
| tool | ツール | |
| human-in-the-loop (HITL) | 人による確認・承認 | Canonical expansion. An approval gate alone may read 承認ゲート. |
| human gate / gate | 承認ゲート | The POST that needs approval. |

## Reasoning / trace surface

| EN | JA | Notes |
|----|----|-------|
| reasoning | 推論 | "view reasoning" → `推論を見る`. |
| trace / trace id | トレース / トレース ID | "copy trace id" → `トレース ID をコピー`. |
| timeline | タイムライン | |
| reasoning summary | 推論の要約 | Vertex thought summaries. NOT 推論サマリー. |
| historical / replay | 過去の実行 | "reviewing a past run" → `過去の実行を表示`; a status pill → `履歴`. NOT 再生. |
| live (stream / streaming) | リアルタイム | `リアルタイム表示` / `リアルタイム更新`. Reserve 実環境 for live *infra*. |

## Decisions / GitHub

| EN | JA | Notes |
|----|----|-------|
| decision (log) | 判断 | `判断履歴` (the rail/log), `過去の判断`, `判断内容` per surface. NOT 意思決定. |
| action (registry / decision field) | アクション | The action-id enum on decision rows and workload capability lists. |
| operation (generic act) | 操作 | Something a user or OpenTofu does (`常に禁止される操作`, `操作ペース`). NOT for the action registry. |
| Infra apply (`iac_apply`) | IaC 適用（ラベル）/ IaC の適用（文中） | e.g. `IaC の適用には承認が必要です`. NOT インフラの適用 / インフラへの適用. |
| pull request / PR | プルリクエスト / PR | |
| merge / merged | マージ / マージ済み | |
| PR body | PR 本文 | |
| env-diff | 環境変数の差分 | The card compares env vars. NOT 環境差分. |
| head SHA | HEAD SHA | Keep Latin; explanatory form `最新コミット SHA`. |

## Denylist categories (CapabilityCard) — headings, no final 。

| EN heading | JA heading | Short name |
|----|----|----|
| Its own control plane is off-limits | 自身のコントロールプレーンの変更は禁止 | コントロールプレーン |
| It leaves Google-created buckets alone | Google が作成・管理するリソースの変更は禁止 | Google 管理リソース |
| It cannot change who has access | アクセス権（IAM）の変更は禁止 | IAM |
| It cannot destroy or replace anything | リソースの削除・置換は禁止 | 削除・置換 |
| Malformed plans are rejected outright | 不正な構造のプランは拒否 | 不正なプラン構造 |

Use 削除 (not 破壊) for destroy. Explanatory text below each heading is です・ます,
e.g. `DriftScribe 自身のコントロールプレーンは変更できません。`

## Common UI verbs / chrome

| EN | JA | Notes |
|----|----|-------|
| Send | 送信 | |
| New chat | 新規チャット | |
| Close | 閉じる | |
| Cancel | キャンセル | |
| Retry | 再試行 | |
| Tour | ガイドツアー | Feature name; compact header button may stay `ツアー`. |
| View | 表示 / 見る | `…を見る` for reasoning; `…を表示` for pages. |
| Copy | コピー | |
| Adopt (button) | 取り込む | Full action phrase: `IaC 管理に取り込む`. |
| Loading… | 読み込み中… | |
| Ask the coordinator… | コーディネーターに質問… | Composer placeholder. |

## Tone / style rules for JA copy

- Plain, professional 敬体 (です・ます) for operator-facing sentences; noun-style
  (体言止め) is fine for short labels/pills/meta and for the denylist headings.
- Do not mix sentence styles within one list (headings all `〜は禁止`/`〜は拒否`).
- No em dashes in JA either; use 、。（）：・ as needed.
- Keep numbers/PR#/SHAs/ids/units Latin. tok → `トークン` (`1,234 トークン`).
- Prefer whole-sentence keys; never stitch a JA sentence from translated fragments
  around markup (word order differs).
- CJK wraps without spaces — do not add spaces to force breaks; rely on layout.
